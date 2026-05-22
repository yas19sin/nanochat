#!/usr/bin/env python3
"""Rebuild a clean held-out validation parquet from existing train shards.

nanochat's base dataloader treats the lexicographically final parquet file as
validation. This script samples quality-filtered rows from the train shards,
writes a new `zzzz_val_mix.parquet`, and by default rewrites the train shards
into a new output directory with those validation rows removed by normalized
exact text match.

The default validation mix is intentionally target-heavy while still covering
English/code/reasoning sources:

- 50% Darija
- 20% Arabic
- 30% English

Use `--validation-only` only for quick diagnostics; it leaves train/validation
leakage in place because the sampled rows remain in the train shards.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.parquet as pq

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.clean_pretraining_val import (
    exact_key,
    normalize_text,
    percentile,
    quality_reason,
)


MIX_TRAIN_RE = re.compile(r"^\d+_(?P<source>.+)_train\.parquet$")

DEFAULT_SOURCE_FRACTIONS = {
    # Darija: 50%
    "darija_fineweb_edu_clean": 0.20,
    "darija_pure": 0.15,
    "darija_bilingual": 0.15,
    # Arabic: 20%
    "arabic_raw": 0.20,
    # English: 30%
    "eng_general_finepdfs_dclm_fwe": 0.12,
    "eng_code_nemotron": 0.04,
    "eng_math_stackexchange": 0.04,
    "eng_reason_algorithmic": 0.02,
    "eng_reason_formal_logic": 0.02,
    "eng_reason_economics": 0.01,
    "eng_reason_multiple_choice": 0.05,
}


def default_source_dir() -> Path:
    base_dir = Path(os.environ.get("NANOCHAT_BASE_DIR", Path.home() / ".cache" / "nanochat"))
    return Path(os.environ.get("NANOCHAT_DATA_DIR", base_dir / "pretrain_mix_darija_english"))


def source_from_path(path: Path) -> str | None:
    match = MIX_TRAIN_RE.match(path.name)
    if match:
        return match.group("source")
    return None


def group_for_source(source: str) -> str:
    if source.startswith("darija_"):
        return "darija"
    if source == "arabic_raw":
        return "arabic"
    if source.startswith("eng_"):
        return "english"
    return "other"


def stable_seed_offset(value: str) -> int:
    return sum((idx + 1) * ord(ch) for idx, ch in enumerate(value)) % 1_000_000


def parse_fraction_overrides(values: Iterable[str], defaults: dict[str, float]) -> dict[str, float]:
    fractions = dict(defaults)
    for value in values:
        if "=" not in value:
            raise SystemExit(f"Expected NAME=FRACTION, got: {value}")
        name, raw = value.split("=", 1)
        fractions[name.strip()] = float(raw)
    return {name: frac for name, frac in fractions.items() if frac > 0}


def normalize_fractions(fractions: dict[str, float], available_sources: Iterable[str]) -> dict[str, float]:
    available = set(available_sources)
    filtered = {name: frac for name, frac in fractions.items() if name in available and frac > 0}
    missing = sorted(name for name in fractions if name not in available)
    if missing:
        print("[warn] fraction sources not present:", ", ".join(missing), flush=True)
    if not filtered:
        raise SystemExit("No validation fraction sources are present in the source directory.")
    total = sum(filtered.values())
    return {name: frac / total for name, frac in filtered.items()}


def quota_map(fractions: dict[str, float], target_docs: int) -> dict[str, int]:
    quotas = {source: int(round(target_docs * frac)) for source, frac in fractions.items()}
    while sum(quotas.values()) > target_docs:
        source = max(quotas, key=lambda key: quotas[key])
        quotas[source] -= 1
    while sum(quotas.values()) < target_docs:
        source = max(fractions, key=lambda key: fractions[key])
        quotas[source] += 1
    return {source: quota for source, quota in quotas.items() if quota > 0}


def find_train_paths(source_dir: Path) -> dict[str, list[Path]]:
    by_source: dict[str, list[Path]] = {}
    for path in sorted(source_dir.glob("[0-9]*_train.parquet")):
        source = source_from_path(path)
        if source is None:
            continue
        by_source.setdefault(source, []).append(path)
    if not by_source:
        raise SystemExit(f"No train shards found in {source_dir}")
    return by_source


def iter_random_texts(paths: list[Path], rng: random.Random, batch_size: int):
    path_order = list(paths)
    rng.shuffle(path_order)
    for path in path_order:
        pf = pq.ParquetFile(path)
        row_groups = list(range(pf.num_row_groups))
        rng.shuffle(row_groups)
        for rg_idx in row_groups:
            table = pf.read_row_group(rg_idx, columns=["text"])
            values = table.column(0).to_pylist()
            rng.shuffle(values)
            for start in range(0, len(values), batch_size):
                batch = values[start:start + batch_size]
                rng.shuffle(batch)
                for raw in batch:
                    yield path, raw


def quality_kwargs(args: argparse.Namespace, source: str) -> dict[str, Any]:
    kwargs = {
        "min_chars": args.val_min_chars,
        "max_chars": args.val_max_chars,
        "min_words": args.val_min_words,
        "min_median_line_chars": args.val_min_median_line_chars,
        "max_punct_ratio": args.val_max_punct_ratio,
        "max_digit_ratio": args.val_max_digit_ratio,
        "min_letter_ratio": args.val_min_letter_ratio,
    }
    if "code" in source:
        kwargs["min_words"] = min(kwargs["min_words"], args.code_val_min_words)
        kwargs["max_punct_ratio"] = max(kwargs["max_punct_ratio"], args.code_val_max_punct_ratio)
        kwargs["min_letter_ratio"] = min(kwargs["min_letter_ratio"], args.code_val_min_letter_ratio)
    elif "math" in source or "reason" in source:
        kwargs["max_punct_ratio"] = max(kwargs["max_punct_ratio"], args.reason_val_max_punct_ratio)
        kwargs["min_words"] = min(kwargs["min_words"], args.reason_val_min_words)
    return kwargs


def make_validation(
    by_source: dict[str, list[Path]],
    quotas: dict[str, int],
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], set[str], dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    rejected = Counter()
    attempts_by_source = Counter()
    selected_by_source = Counter()
    selected_by_file = Counter()

    for source, quota in quotas.items():
        rng = random.Random(args.seed + stable_seed_offset(source))
        source_attempt_limit = args.max_source_attempts
        source_paths = by_source[source]
        for path, raw in iter_random_texts(source_paths, rng, args.sample_batch_size):
            attempts_by_source[source] += 1
            if source_attempt_limit > 0 and attempts_by_source[source] > source_attempt_limit:
                break

            text = normalize_text(raw or "")
            reason = quality_reason(text, **quality_kwargs(args, source))
            if reason:
                rejected[f"{source}:{reason}"] += 1
                continue
            key = exact_key(text)
            if key in selected_keys:
                rejected[f"{source}:duplicate"] += 1
                continue
            selected_keys.add(key)
            selected.append({
                "text": text,
                "source": source,
                "group": group_for_source(source),
                "file": path.name,
            })
            selected_by_source[source] += 1
            selected_by_file[path.name] += 1
            if selected_by_source[source] >= quota:
                break

        if selected_by_source[source] < quota:
            print(
                f"[warn] {source} filled {selected_by_source[source]:,}/{quota:,} validation docs",
                flush=True,
            )

    random.Random(args.seed).shuffle(selected)
    lengths = sorted(len(item["text"]) for item in selected)
    summary = {
        "target_docs": args.target_docs,
        "selected_docs": len(selected),
        "quotas": quotas,
        "selected_by_source": dict(selected_by_source),
        "selected_by_group": dict(Counter(item["group"] for item in selected)),
        "selected_by_file": dict(selected_by_file),
        "attempts_by_source": dict(attempts_by_source),
        "rejected": dict(rejected.most_common()),
        "lengths": {
            "min": percentile(lengths, 0.0),
            "p50": percentile(lengths, 0.5),
            "p90": percentile(lengths, 0.9),
            "p99": percentile(lengths, 0.99),
            "max": percentile(lengths, 1.0),
        },
    }
    return selected, selected_keys, summary


def write_text_parquet(path: Path, rows: list[str], row_group_size: int) -> None:
    table = pa.Table.from_pydict({"text": rows})
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(table, tmp_path, row_group_size=row_group_size)
    tmp_path.replace(path)


def rewrite_train_file(
    source_path: Path,
    output_path: Path,
    holdout_keys: set[str],
    *,
    batch_size: int,
    row_group_size: int,
) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()

    schema = pa.schema([("text", pa.string())])
    writer = pq.ParquetWriter(tmp_path, schema)
    rows_in = rows_out = removed = chars = utf8_bytes = 0
    pf = None
    try:
        pf = pq.ParquetFile(source_path)
        for batch in pf.iter_batches(columns=["text"], batch_size=batch_size):
            raw_texts = batch.column(0).to_pylist()
            rows_in += len(raw_texts)
            kept = []
            for raw in raw_texts:
                text = raw or ""
                if exact_key(normalize_text(text)) in holdout_keys:
                    removed += 1
                    continue
                kept.append(text)
                chars += len(text)
                utf8_bytes += len(text.encode("utf-8"))
            if kept:
                table = pa.Table.from_pydict({"text": kept}, schema=schema)
                writer.write_table(table, row_group_size=row_group_size)
                rows_out += len(kept)
    finally:
        writer.close()
        if pf is not None and hasattr(pf, "close"):
            pf.close()

    tmp_path.replace(output_path)
    return {
        "file": output_path.name,
        "source_file": source_path.name,
        "rows_in": rows_in,
        "rows_out": rows_out,
        "removed": removed,
        "chars": chars,
        "utf8_bytes": utf8_bytes,
        "file_size_bytes": output_path.stat().st_size,
    }


def backup_validation_file(data_dir: Path) -> Path | None:
    val_path = data_dir / "zzzz_val_mix.parquet"
    if not val_path.exists():
        return None
    backup_dir = data_dir / "_validation_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"zzzz_val_mix.before_rebuild.{stamp}.parquet"
    shutil.copy2(val_path, backup_path)
    return backup_path


def prepare_output_dir(path: Path, overwrite: bool) -> None:
    if path.exists() and any(path.iterdir()):
        if not overwrite:
            raise SystemExit(f"Output directory is not empty: {path} (use --overwrite)")
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def format_seconds(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=default_source_dir())
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="New nanochat data dir. Defaults to SOURCE_DIR sibling with `_valfix` suffix.")
    parser.add_argument("--target-docs", type=int, default=50_000)
    parser.add_argument("--source-fraction", action="append", default=[],
                        help="Override validation fraction, e.g. arabic_raw=0.20. May be repeated.")
    parser.add_argument("--in-place", action="store_true",
                        help="Rewrite SOURCE_DIR directly. Requires no full second copy, but train shards are replaced.")
    parser.add_argument("--validation-only", action="store_true",
                        help="Only write zzzz_val_mix.parquet to output-dir; does not remove rows from train.")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sample-batch-size", type=int, default=8192)
    parser.add_argument("--rewrite-batch-size", type=int, default=8192)
    parser.add_argument("--train-row-group-size", type=int, default=25_000)
    parser.add_argument("--val-row-group-size", type=int, default=2_000)
    parser.add_argument("--max-source-attempts", type=int, default=-1)
    parser.add_argument("--progress-every-files", type=int, default=1)

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
    if args.in_place:
        if args.output_dir is not None:
            raise SystemExit("--in-place cannot be combined with --output-dir")
        args.output_dir = args.source_dir
    elif args.output_dir is None:
        args.output_dir = args.source_dir.with_name(args.source_dir.name + "_valfix")
    args.output_dir = args.output_dir.resolve()

    if not args.source_dir.exists():
        raise SystemExit(f"Source directory does not exist: {args.source_dir}")

    by_source = find_train_paths(args.source_dir)
    fractions = parse_fraction_overrides(args.source_fraction, DEFAULT_SOURCE_FRACTIONS)
    fractions = normalize_fractions(fractions, by_source.keys())
    quotas = quota_map(fractions, args.target_docs)

    print(f"Source: {args.source_dir}")
    print(f"Output: {args.output_dir}")
    print(f"In-place: {args.in_place}")
    print(f"Validation-only: {args.validation_only}")
    print("Validation quotas:")
    for source, quota in sorted(quotas.items(), key=lambda item: item[0]):
        print(f"  - {source}: {quota:,}")

    if args.in_place:
        if not args.overwrite:
            raise SystemExit("--in-place requires --overwrite to acknowledge replacing local data files.")
    else:
        prepare_output_dir(args.output_dir, args.overwrite)

    t0 = time.time()
    selected, holdout_keys, val_summary = make_validation(by_source, quotas, args)
    if not selected:
        raise SystemExit("No validation rows selected; refusing to write empty validation.")

    val_path = args.output_dir / "zzzz_val_mix.parquet"
    pending_val_path = val_path
    validation_backup = None
    if args.in_place:
        validation_backup = backup_validation_file(args.output_dir)
        if validation_backup:
            print(f"  backed up existing validation to {validation_backup}")
        pending_val_path = args.output_dir / "zzzz_val_mix.rebuild_pending.parquet"
    write_text_parquet(pending_val_path, [item["text"] for item in selected], args.val_row_group_size)
    print(f"  wrote {pending_val_path} ({len(selected):,} rows)")

    train_records: list[dict[str, Any]] = []
    if not args.validation_only:
        all_paths = [path for paths in by_source.values() for path in paths]
        for idx, source_path in enumerate(sorted(all_paths), 1):
            output_path = args.output_dir / source_path.name
            record = rewrite_train_file(
                source_path,
                output_path,
                holdout_keys,
                batch_size=args.rewrite_batch_size,
                row_group_size=args.train_row_group_size,
            )
            train_records.append(record)
            if args.progress_every_files > 0 and idx % args.progress_every_files == 0:
                print(
                    f"  [{idx:,}/{len(all_paths):,}] {source_path.name}: "
                    f"removed={record['removed']:,} rows_out={record['rows_out']:,} "
                    f"elapsed={format_seconds(time.time() - t0)}",
                    flush=True,
                )

    if args.in_place:
        pending_val_path.replace(val_path)
        print(f"  replaced validation parquet: {val_path}")

    manifest = {
        "format": "nanochat_text_only_parquet",
        "kind": "validation_rebuild",
        "source_dir": str(args.source_dir),
        "output_dir": str(args.output_dir),
        "created_unix": int(time.time()),
        "settings": {
            "target_docs": args.target_docs,
            "source_fractions": fractions,
            "validation_only": args.validation_only,
            "seed": args.seed,
            "in_place": args.in_place,
            "validation_backup": str(validation_backup) if validation_backup else None,
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
            "files": train_records,
            "totals": {
                "rows_in": sum(record["rows_in"] for record in train_records),
                "rows_out": sum(record["rows_out"] for record in train_records),
                "removed": sum(record["removed"] for record in train_records),
                "chars": sum(record["chars"] for record in train_records),
                "utf8_bytes": sum(record["utf8_bytes"] for record in train_records),
            },
        },
        "elapsed_seconds": round(time.time() - t0, 3),
    }
    manifest_path = args.output_dir / "validation_rebuild_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Manifest: {manifest_path}")
    print("\nSummary")
    print(json.dumps({
        "validation_docs": len(selected),
        "validation_by_group": val_summary["selected_by_group"],
        "train_removed": manifest["train_rewrite"]["totals"]["removed"],
        "elapsed_seconds": manifest["elapsed_seconds"],
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
