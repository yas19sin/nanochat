"""Materialize the cleaned Darija pretraining corpus from a kept-id manifest.

Reads `<output_dir>/global_kept.jsonl` (one entry per row: shard, src_idx,
exact_hash) produced by `analyze_darija_quality.py --all-shards`, then
re-streams the source HuggingFace dataset shard-by-shard and writes the
Darija text of the kept rows into parquet files compatible with
`nanochat/dataset.py` (one `text` column, named `shard_NNNNN.parquet`).

Email masking from the analyzer is reapplied so the on-disk text matches
what was filtered + deduplicated.

The output layout is exactly what nanochat's pretraining loader expects:

    materialized_dir/
        shard_00000.parquet
        shard_00001.parquet
        ...
        shard_NNNNN.parquet    # last shard becomes the val split

Usage:
    python scripts/materialize_darija_clean.py \
        --kept-manifest dev/clean_darija/full_v3/global_kept.jsonl \
        --output-dir dev/clean_darija/materialized \
        --workers 8
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional convenience dependency
    def load_dotenv(*_args, **_kwargs) -> bool:
        return False

# Allow both `python -m scripts.materialize_darija_clean` and
# `python scripts/materialize_darija_clean.py` from the repo root.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Reuse the analyzer's text-normalization helpers so the materialized text
# exactly matches what was hashed/deduplicated.
from scripts.analyze_darija_quality import (
    HF_DATASET,
    EMAIL_RE,
    list_repo_shard_urls,
    normalize_for_hash,
    normalize_text,
)


def _iter_source_rows(src_shard_url: str, token: str | None,
                      cache_dir: str | None):
    """Yield source rows from either a local parquet file or an HF parquet URL."""
    if not (src_shard_url.startswith("hf://")
            or src_shard_url.startswith("http://")
            or src_shard_url.startswith("https://")):
        pf = pq.ParquetFile(src_shard_url)
        for batch in pf.iter_batches(
            batch_size=8192,
            columns=["src_idx", "darija"],
        ):
            cols = batch.to_pydict()
            src_ids = cols["src_idx"]
            darijas = cols["darija"]
            for i, sid in enumerate(src_ids):
                yield {"src_idx": sid, "darija": darijas[i]}
        return

    from datasets import load_dataset
    ds = load_dataset(
        "parquet",
        data_files=[src_shard_url],
        split="train",
        streaming=True,
        token=token,
        cache_dir=cache_dir,
    )
    yield from ds


def _materialize_one_shard(payload: dict) -> dict:
    """Worker: stream one source parquet shard, emit kept rows to an output
    parquet shard. Runs in a separate process."""
    src_shard_idx: int = payload["src_shard_idx"]
    src_shard_url: str = payload["src_shard_url"]
    keep_hash_by_id: dict[int, str] = payload["keep_hash_by_id"]
    keep_ids = set(keep_hash_by_id)
    out_path: Path = Path(payload["out_path"])
    token: str | None = payload["token"]
    cache_dir: str | None = payload["cache_dir"]

    # Resume: if the output already exists with row count >= expected, skip.
    expected = len(keep_ids)
    if out_path.exists():
        try:
            existing_rows = pq.ParquetFile(out_path).metadata.num_rows
            if existing_rows == expected:
                return {
                    "src_shard_idx": src_shard_idx,
                    "out_path": str(out_path),
                    "rows_written": existing_rows,
                    "resumed": True,
                }
        except Exception:
            # Corrupt — re-do it.
            out_path.unlink(missing_ok=True)

    texts: list[str] = []
    found_ids: set[int] = set()
    hash_mismatches: list[dict] = []
    n_seen = 0
    for row in _iter_source_rows(src_shard_url, token, cache_dir):
        n_seen += 1
        sid = int(row.get("src_idx", -1))
        if sid not in keep_ids:
            continue
        darija = normalize_text(row.get("darija"))
        if not darija:
            continue
        # Reapply email masking (matches what analyzer hashed/deduped on).
        darija = EMAIL_RE.sub("<email>", darija)
        h = hashlib.sha1(normalize_for_hash(
            darija).encode("utf-8")).hexdigest()
        expected_h = keep_hash_by_id[sid]
        if h != expected_h:
            if len(hash_mismatches) < 20:
                hash_mismatches.append({
                    "src_idx": sid,
                    "expected_hash": expected_h,
                    "actual_hash": h,
                })
            continue
        found_ids.add(sid)
        texts.append(darija)

    missing_ids = sorted(keep_ids - found_ids)[:20]

    # Write parquet atomically (tmp then rename).
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".parquet.tmp")
    tmp.unlink(missing_ok=True)
    table = pa.table({"text": pa.array(texts, type=pa.string())})
    pq.write_table(table, tmp, compression="zstd",
                   compression_level=6)
    tmp.replace(out_path)

    return {
        "src_shard_idx": src_shard_idx,
        "out_path": str(out_path),
        "rows_seen": n_seen,
        "rows_written": len(texts),
        "rows_expected": expected,
        "rows_missing": expected - len(found_ids),
        "missing_ids_sample": missing_ids,
        "hash_mismatch_count": len(hash_mismatches),
        "hash_mismatches_sample": hash_mismatches,
        "resumed": False,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kept-manifest", required=True,
                    help="Path to global_kept.jsonl from analyze_darija_quality.")
    ap.add_argument("--output-dir", required=True,
                    help="Directory to write shard_NNNNN.parquet files into.")
    ap.add_argument("--workers", type=int,
                    default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--hf-token", default=None)
    args = ap.parse_args()

    load_dotenv(Path(".env"))
    token = (args.hf_token or os.environ.get("HF_TOKEN")
             or os.environ.get("HUGGINGFACE_HUB_TOKEN"))

    kept_path = Path(args.kept_manifest).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Bucket the kept ids by source shard.
    print(f"[materialize] reading {kept_path} ...")
    keep_by_shard: dict[int, dict[int, str]] = {}
    duplicate_manifest_ids = 0
    with kept_path.open(encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            sh = int(o["shard"])
            sid = int(o["src_idx"])
            h = str(o["exact_hash"])
            bucket = keep_by_shard.setdefault(sh, {})
            if sid in bucket:
                duplicate_manifest_ids += 1
            bucket[sid] = h
    total_kept = sum(len(v) for v in keep_by_shard.values())
    print(f"[materialize] manifest: {total_kept:,} kept rows "
          f"across {len(keep_by_shard):,} source shards")
    if duplicate_manifest_ids:
        print(f"[materialize] WARNING: ignored {duplicate_manifest_ids:,} "
              "duplicate (shard, src_idx) manifest entries")

    # Resolve source shard URLs.
    print(f"[materialize] resolving source shard URLs for {HF_DATASET} ...")
    try:
        src_urls = list_repo_shard_urls(HF_DATASET, token)
    except Exception as e:
        print(f"[materialize] ERROR: could not list source shards: {e}")
        print("[materialize] If the dataset is private/gated, set HF_TOKEN "
              "or run `hf auth login` in this environment.")
        return 1
    print(f"[materialize] dataset has {len(src_urls)} source shards")

    # Map source shard index -> output shard filename. We renumber compactly
    # to 0..N-1 so the produced layout is contiguous even if some source
    # shards happened to have zero kept rows.
    src_shards_with_data = sorted(keep_by_shard.keys())
    payloads = []
    for out_idx, src_idx in enumerate(src_shards_with_data):
        out_path = output_dir / f"shard_{out_idx:05d}.parquet"
        payloads.append({
            "src_shard_idx": src_idx,
            "src_shard_url": src_urls[src_idx],
            "keep_hash_by_id": keep_by_shard[src_idx],
            "out_path": str(out_path),
            "token": token,
            "cache_dir": args.cache_dir,
        })

    print(f"[materialize] materializing {len(payloads)} output shards "
          f"with {args.workers} workers ...")

    t0 = time.time()
    done = 0
    rows_written_total = 0
    rows_resumed_total = 0
    failed_results: list[dict] = []
    if args.workers <= 1:
        results = [_materialize_one_shard(p) for p in payloads]
        for r in results:
            done += 1
            rw = r["rows_written"]
            rows_written_total += rw
            if (not r.get("resumed") and
                    (r.get("rows_missing", 0) or r.get("hash_mismatch_count", 0))):
                failed_results.append(r)
            if r.get("resumed"):
                rows_resumed_total += rw
            elapsed = time.time() - t0
            eta = elapsed / done * (len(payloads) - done)
            print(f"[materialize] {done}/{len(payloads)} "
                  f"src_shard={r['src_shard_idx']:04d} "
                  f"rows={rw:,} "
                  f"(elapsed {elapsed/60:.1f}m eta {eta/60:.1f}m)")
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futures = {
                ex.submit(_materialize_one_shard, p): p["src_shard_idx"]
                for p in payloads
            }
            for fut in as_completed(futures):
                src_idx = futures[fut]
                try:
                    r = fut.result()
                except Exception as e:
                    print(f"[materialize] src_shard {src_idx:04d} FAILED: {e}")
                    failed_results.append({
                        "src_shard_idx": src_idx,
                        "error": repr(e),
                    })
                    continue
                done += 1
                rw = r["rows_written"]
                rows_written_total += rw
                if (not r.get("resumed") and
                        (r.get("rows_missing", 0) or r.get("hash_mismatch_count", 0))):
                    failed_results.append(r)
                if r.get("resumed"):
                    rows_resumed_total += rw
                elapsed = time.time() - t0
                eta = elapsed / max(done, 1) * (len(payloads) - done)
                tag = "(resumed)" if r.get("resumed") else ""
                print(f"[materialize] {done}/{len(payloads)} "
                      f"src_shard={r['src_shard_idx']:04d} "
                      f"rows={rw:,} {tag} "
                      f"(elapsed {elapsed/60:.1f}m eta {eta/60:.1f}m)")

    # Final report.
    total_bytes = sum(p.stat().st_size for p in
                      output_dir.glob("shard_*.parquet"))
    print(f"\n[materialize] DONE")
    print(f"[materialize]   output_dir          = {output_dir}")
    print(f"[materialize]   output shards       = {len(payloads):,}")
    print(f"[materialize]   rows expected       = {total_kept:,}")
    print(f"[materialize]   rows written        = {rows_written_total:,}")
    if rows_resumed_total:
        print(f"[materialize]   rows resumed        = {rows_resumed_total:,}")
    print(f"[materialize]   total bytes on disk = {total_bytes/2**30:.2f} GiB")
    print(
        f"[materialize]   elapsed             = {(time.time()-t0)/60:.1f} min")
    report = {
        "kept_manifest": str(kept_path),
        "output_dir": str(output_dir),
        "output_shards": len(payloads),
        "rows_expected": total_kept,
        "rows_written": rows_written_total,
        "rows_resumed": rows_resumed_total,
        "bytes_on_disk": total_bytes,
        "duplicate_manifest_ids_ignored": duplicate_manifest_ids,
        "failed_shards": failed_results,
    }
    (output_dir / "materialization_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    for meta_name in ("aggregate.json", "aggregate.md", "DEDUP_SCOPE.txt"):
        src_meta = kept_path.parent / meta_name
        if src_meta.exists():
            shutil.copy2(src_meta, output_dir / meta_name)
    if rows_written_total != total_kept or failed_results:
        print("[materialize] ERROR: materialization incomplete; see "
              f"{output_dir / 'materialization_report.json'}")
        return 1
    print(f"\n[materialize] To use with nanochat:")
    print(f"    $env:NANOCHAT_DATA_DIR = \"{output_dir}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
