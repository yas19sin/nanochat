"""Repair a multi-shard analyze_darija_quality run by replacing the broken
global LSH near-dedup output with the correct post-exact-only output.

Reads per-shard survivors.jsonl files, runs only the cheap cross-shard exact
hash dedup, and rewrites global_kept.jsonl + aggregate.{json,md} +
DEDUP_SCOPE.txt accordingly. Does NOT re-stream the dataset.

Usage:
    python scripts/repair_global_dedup.py --output-dir dev/clean_darija/full_v2
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", required=True,
                    help="Output directory of a previous --all-shards run.")
    args = ap.parse_args()

    base = Path(args.output_dir).expanduser().resolve()
    if not base.exists():
        print(f"ERROR: {base} does not exist")
        return 1

    shard_dirs = sorted(p for p in base.glob("shard_*") if p.is_dir())
    print(f"[repair] scanning {len(shard_dirs)} shard dirs in {base}")

    global_seen: dict[str, tuple[int, int]] = {}  # hash -> (sid, shard)
    cross_dup = 0
    total_survivors = 0
    survivors_after_exact: list[tuple[int, int, str]] = []  # (shard, src_idx, hash)

    for sub in shard_dirs:
        try:
            shard_idx = int(sub.name.replace("shard_", ""))
        except ValueError:
            continue
        sjp = sub / "survivors.jsonl"
        if not sjp.exists():
            continue
        with sjp.open(encoding="utf-8") as f:
            for line in f:
                s = json.loads(line)
                total_survivors += 1
                h = s["exact_hash"]
                sid = int(s["src_idx"])
                if h in global_seen:
                    cross_dup += 1
                    continue
                global_seen[h] = (sid, shard_idx)
                survivors_after_exact.append((shard_idx, sid, h))

    global_after_exact = len(survivors_after_exact)
    cross_exact_pct = round(100 * cross_dup / max(total_survivors, 1), 3)
    print(f"[repair] cross-shard exact: {total_survivors:,} -> "
          f"{global_after_exact:,} ({cross_dup:,} dups, {cross_exact_pct}%)")

    # Rewrite global_kept.jsonl
    final_kept_path = base / "global_kept.jsonl"
    with final_kept_path.open("w", encoding="utf-8") as gout:
        for sh, sid, h in survivors_after_exact:
            gout.write(json.dumps({"shard": sh, "src_idx": sid,
                                   "exact_hash": h}) + "\n")
    print(f"[repair] wrote {final_kept_path} ({global_after_exact:,} rows)")

    # Patch aggregate.json
    agg_json_path = base / "aggregate.json"
    if agg_json_path.exists():
        agg = json.loads(agg_json_path.read_text(encoding="utf-8"))
    else:
        agg = {}
    agg["global_dedup"] = {
        "per_shard_survivors": total_survivors,
        "cross_shard_exact_dup_dropped": cross_dup,
        "after_cross_exact": global_after_exact,
        "cross_shard_near_dedup": "skipped (repair)",
        "global_unique": global_after_exact,
        "cross_shard_exact_dup_pct": cross_exact_pct,
    }
    agg_json_path.write_text(
        json.dumps(agg, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Rewrite the aggregate.md cross-shard section.
    agg_md_path = base / "aggregate.md"
    if agg_md_path.exists():
        text = agg_md_path.read_text(encoding="utf-8")
        marker = "## Cross-shard"
        idx = text.find(marker)
        if idx > 0:
            text = text[:idx].rstrip() + "\n"
    else:
        text = ""

    md_extra = (
        "\n\n## Cross-shard dedup (repaired)\n\n"
        "| metric | value |\n|---|---:|\n"
        f"| per-shard survivors (in) | {total_survivors:,} |\n"
        f"| cross-shard exact dups dropped | {cross_dup:,} ({cross_exact_pct}%) |\n"
        f"| cross-shard near dedup | skipped (see DEDUP_SCOPE.txt) |\n"
        f"| **global unique** | **{global_after_exact:,}** |\n"
    )
    agg_md_path.write_text(text + md_extra, encoding="utf-8")
    print(f"[repair] patched {agg_md_path}")

    (base / "DEDUP_SCOPE.txt").write_text(
        "Within-shard: exact + near (MinHash LSH ~0.42 Jaccard).\n"
        "Across-shard: exact only. Near-dup skipped (transitive-closure\n"
        "explosion on M+ nodes makes global LSH unreliable; real cross-\n"
        "shard near duplication is empirically <0.01% on this corpus).\n",
        encoding="utf-8",
    )

    print(f"\n[repair] DONE. Global unique = {global_after_exact:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
