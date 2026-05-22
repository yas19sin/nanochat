#!/usr/bin/env python3
"""Build only the held-out pretraining validation parquet.

This is the fast path for fixing `zzzz_val_mix.parquet`. It samples clean rows
from existing train shards and writes a new validation parquet, but it never
rewrites train shards. That means it is appropriate for quickly getting a
better validation split, but it does not remove train/validation overlap.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.rebuild_pretraining_validation import (
    DEFAULT_SOURCE_FRACTIONS,
    default_source_dir,
    find_train_paths,
    make_validation,
    normalize_fractions,
    parse_fraction_overrides,
    quota_map,
    write_text_parquet,
)


def backup_existing(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup_dir = path.parent / "_validation_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = backup_dir / f"{path.stem}.before_fast_rebuild.{stamp}{path.suffix}"
    shutil.copy2(path, backup)
    return backup


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=default_source_dir())
    parser.add_argument("--output", type=Path, default=None,
                        help="Validation parquet to write. Defaults to SOURCE_DIR/zzzz_val_mix.parquet.")
    parser.add_argument("--manifest-output", type=Path, default=None,
                        help="Manifest path. Defaults to SOURCE_DIR/validation_rebuild_manifest.json.")
    parser.add_argument("--target-docs", type=int, default=50_000)
    parser.add_argument("--source-fraction", action="append", default=[],
                        help="Override validation fraction, e.g. arabic_raw=0.20. May be repeated.")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sample-batch-size", type=int, default=8192)
    parser.add_argument("--val-row-group-size", type=int, default=2_000)
    parser.add_argument("--max-source-attempts", type=int, default=-1)

    parser.add_argument("--val-min-chars", type=int, default=220)
    parser.add_argument("--val-max-chars", type=int, default=12_000)
    parser.add_argument("--val-min-words", type=int, default=24)
    parser.add_argument("--val-min-median-line-chars", type=int, default=28)
    parser.add_argument("--val-max-punct-ratio", type=float, default=0.28)
    parser.add_argument("--val-max-digit-ratio", type=float, default=0.35)
    parser.add_argument("--val-min-letter-ratio", type=float, default=0.45)
    parser.add_argument("--code-val-min-words", type=int, default=8)
    parser.add_argument("--code-val-max-punct-ratio", type=float, default=0.55)
    parser.add_argument("--code-val-min-letter-ratio", type=float, default=0.25)
    parser.add_argument("--reason-val-min-words", type=int, default=12)
    parser.add_argument("--reason-val-max-punct-ratio", type=float, default=0.45)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.source_dir = args.source_dir.resolve()
    output = (args.output or (args.source_dir / "zzzz_val_mix.parquet")).resolve()
    manifest_output = (
        args.manifest_output or (args.source_dir / "validation_rebuild_manifest.json")
    ).resolve()

    if not args.source_dir.exists():
        raise SystemExit(f"Source directory does not exist: {args.source_dir}")
    if output.exists() and not args.overwrite:
        raise SystemExit(f"Output exists: {output} (use --overwrite)")

    by_source = find_train_paths(args.source_dir)
    fractions = parse_fraction_overrides(args.source_fraction, DEFAULT_SOURCE_FRACTIONS)
    fractions = normalize_fractions(fractions, by_source.keys())
    quotas = quota_map(fractions, args.target_docs)

    print(f"Source: {args.source_dir}")
    print(f"Output: {output}")
    print("Validation quotas:")
    for source, quota in sorted(quotas.items(), key=lambda item: item[0]):
        print(f"  - {source}: {quota:,}")

    t0 = time.time()
    selected, _holdout_keys, val_summary = make_validation(by_source, quotas, args)
    if not selected:
        raise SystemExit("No validation rows selected; refusing to write empty validation.")

    backup = None if args.no_backup else backup_existing(output)
    if backup:
        print(f"  backed up existing validation to {backup}")

    pending = output.with_suffix(output.suffix + ".pending")
    write_text_parquet(pending, [item["text"] for item in selected], args.val_row_group_size)
    pending.replace(output)
    print(f"  wrote {output} ({len(selected):,} rows)")

    manifest = {
        "format": "nanochat_text_only_parquet",
        "kind": "validation_fast_rebuild",
        "source_dir": str(args.source_dir),
        "output_file": str(output),
        "created_unix": int(time.time()),
        "settings": {
            "target_docs": args.target_docs,
            "source_fractions": fractions,
            "validation_only": True,
            "train_rewrite": False,
            "seed": args.seed,
            "validation_backup": str(backup) if backup else None,
            "quality": {
                "val_min_chars": args.val_min_chars,
                "val_max_chars": args.val_max_chars,
                "val_min_words": args.val_min_words,
                "val_min_median_line_chars": args.val_min_median_line_chars,
                "val_max_punct_ratio": args.val_max_punct_ratio,
                "val_max_digit_ratio": args.val_max_digit_ratio,
                "val_min_letter_ratio": args.val_min_letter_ratio,
            },
        },
        "validation": val_summary,
        "train_rewrite": {
            "files": [],
            "totals": {
                "rows_in": 0,
                "rows_out": 0,
                "removed": 0,
                "chars": 0,
                "utf8_bytes": 0,
            },
        },
        "elapsed_seconds": round(time.time() - t0, 3),
    }
    manifest_output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Manifest: {manifest_output}")
    print(json.dumps({
        "validation_docs": len(selected),
        "validation_by_group": val_summary["selected_by_group"],
        "train_removed": 0,
        "elapsed_seconds": manifest["elapsed_seconds"],
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
