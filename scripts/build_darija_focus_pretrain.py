#!/usr/bin/env python3
"""Build a shuffled Darija/Arabic-focused nanochat pretraining directory.

This script consumes the already-built large pretraining parquet directory and
writes a smaller directory for fast small-model experiments. It keeps nanochat's
flat parquet contract:

- numeric train parquet files sort first
- the final `zzzz_val_mix.parquet` is validation
- every parquet has exactly one column: `text`

Unlike the main curriculum dataset, train rows here are source-interleaved so
small runs see Darija/Arabic diversity early. Validation rows are sampled first,
quality-filtered, and excluded from train by normalized exact text match.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import time
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pyarrow as pa
import pyarrow.parquet as pq

from scripts.clean_pretraining_val import (
    exact_key,
    normalize_text,
    percentile,
    quality_reason,
)


MIX_TRAIN_RE = re.compile(r"^\d+_(?P<source>.+)_train\.parquet$")

DEFAULT_TRAIN_WEIGHTS = {
    "darija_fineweb_edu_clean": 4.0,
    "darija_pure": 3.0,
    "darija_bilingual": 2.5,
    "arabic_raw": 1.0,
}

DEFAULT_VAL_FRACTIONS = {
    "darija_fineweb_edu_clean": 0.35,
    "darija_pure": 0.25,
    "darija_bilingual": 0.25,
    "arabic_raw": 0.15,
}

DEFAULT_SOURCE_ORDER = (
    "darija_fineweb_edu_clean",
    "darija_pure",
    "darija_bilingual",
    "arabic_raw",
)


@dataclass
class SourceReader:
    source: str
    paths: list[Path]
    val_hashes: set[str]
    train_min_chars: int
    train_min_words: int
    train_max_chars: int
    rng: random.Random
    shuffle_files: bool
    max_doc_chars: int

    def __post_init__(self) -> None:
        paths = list(self.paths)
        if self.shuffle_files:
            self.rng.shuffle(paths)
        self.paths = paths
        self.path_index = 0
        self.batch_iter = None
        self.current_texts: list[str] = []
        self.text_index = 0
        self.rows_seen = 0
        self.rows_emitted = 0
        self.rows_skipped = Counter()

    def next_text(self) -> str:
        while True:
            if self.text_index < len(self.current_texts):
                raw = self.current_texts[self.text_index]
                self.text_index += 1
                self.rows_seen += 1
                text = normalize_text(raw or "")
                if self.max_doc_chars > 0 and len(text) > self.max_doc_chars:
                    text = text[: self.max_doc_chars].rstrip()
                reason = quality_reason(
                    text,
                    min_chars=self.train_min_chars,
                    max_chars=max(self.train_max_chars, self.max_doc_chars),
                    min_words=self.train_min_words,
                    min_median_line_chars=12,
                    max_punct_ratio=0.40,
                    max_digit_ratio=0.45,
                    min_letter_ratio=0.35,
                )
                if reason:
                    self.rows_skipped[reason] += 1
                    continue
                if exact_key(text) in self.val_hashes:
                    self.rows_skipped["held_out_validation"] += 1
                    continue
                self.rows_emitted += 1
                return text

            self._advance_batch()

    def _advance_batch(self) -> None:
        while True:
            if self.batch_iter is not None:
                try:
                    batch = next(self.batch_iter)
                    self.current_texts = batch.column(0).to_pylist()
                    self.text_index = 0
                    return
                except StopIteration:
                    self.batch_iter = None

            if self.path_index >= len(self.paths):
                raise StopIteration

            path = self.paths[self.path_index]
            self.path_index += 1
            pf = pq.ParquetFile(path)
            self.batch_iter = pf.iter_batches(columns=["text"], batch_size=8192)


class ShardWriter:
    def __init__(
        self,
        output_dir: Path,
        shard_size: int,
        row_group_size: int,
        prefix: str,
    ) -> None:
        self.output_dir = output_dir
        self.shard_size = shard_size
        self.row_group_size = row_group_size
        self.prefix = prefix
        self.rows: list[str] = []
        self.shard_index = 0
        self.records: list[dict] = []

    def add(self, text: str) -> None:
        self.rows.append(text)
        if len(self.rows) >= self.shard_size:
            self.flush()

    def flush(self) -> None:
        if not self.rows:
            return
        name = f"{self.shard_index:05d}_{self.prefix}_train.parquet"
        path = self.output_dir / name
        write_text_parquet(path, self.rows, self.row_group_size)
        record = {
            "file": name,
            "rows": len(self.rows),
            "chars": sum(len(text) for text in self.rows),
            "utf8_bytes": sum(len(text.encode("utf-8")) for text in self.rows),
        }
        self.records.append(record)
        print(f"  wrote {path} ({record['rows']:,} rows)", flush=True)
        self.rows = []
        self.shard_index += 1


def default_source_dir() -> Path:
    base_dir = Path(os.environ.get("NANOCHAT_BASE_DIR", Path.home() / ".cache" / "nanochat"))
    return Path(os.environ.get("NANOCHAT_SOURCE_DATA_DIR", base_dir / "pretrain_mix_darija_english"))


def default_output_dir() -> Path:
    base_dir = Path(os.environ.get("NANOCHAT_BASE_DIR", Path.home() / ".cache" / "nanochat"))
    return Path(os.environ.get("NANOCHAT_FOCUS_DATA_DIR", base_dir / "pretrain_mix_darija_focus"))


def source_from_path(path: Path) -> str | None:
    match = MIX_TRAIN_RE.match(path.name)
    if match:
        return match.group("source")
    return None


def parse_weight_map(values: Iterable[str], defaults: dict[str, float]) -> dict[str, float]:
    result = dict(defaults)
    for value in values:
        if "=" not in value:
            raise SystemExit(f"Expected NAME=VALUE weight, got: {value}")
        name, raw_weight = value.split("=", 1)
        result[name.strip()] = float(raw_weight)
    return {key: val for key, val in result.items() if val > 0}


def parse_fraction_map(values: Iterable[str], defaults: dict[str, float]) -> dict[str, float]:
    result = dict(defaults)
    for value in values:
        if "=" not in value:
            raise SystemExit(f"Expected NAME=VALUE fraction, got: {value}")
        name, raw_fraction = value.split("=", 1)
        result[name.strip()] = float(raw_fraction)
    result = {key: val for key, val in result.items() if val > 0}
    total = sum(result.values())
    if total <= 0:
        raise SystemExit("Validation fractions sum to zero.")
    return {key: val / total for key, val in result.items()}


def find_source_paths(source_dir: Path, sources: Iterable[str]) -> dict[str, list[Path]]:
    by_source: dict[str, list[Path]] = {source: [] for source in sources}
    for path in sorted(source_dir.glob("*.parquet")):
        source = source_from_path(path)
        if source in by_source:
            by_source[source].append(path)
    missing = [source for source, paths in by_source.items() if not paths]
    if missing:
        raise SystemExit(f"Missing source shards in {source_dir}: {', '.join(missing)}")
    return by_source


def write_text_parquet(path: Path, rows: list[str], row_group_size: int) -> None:
    table = pa.Table.from_pydict({"text": rows})
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(table, tmp_path, row_group_size=row_group_size)
    tmp_path.replace(path)


def iter_random_batches(paths: list[Path], rng: random.Random):
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
            yield values


def make_validation(
    by_source: dict[str, list[Path]],
    val_fractions: dict[str, float],
    target_docs: int,
    args: argparse.Namespace,
    rng: random.Random,
) -> tuple[list[dict], set[str], dict]:
    quotas = {source: int(round(target_docs * frac)) for source, frac in val_fractions.items()}
    while sum(quotas.values()) > target_docs:
        source = max(quotas, key=lambda key: quotas[key])
        quotas[source] -= 1
    while sum(quotas.values()) < target_docs:
        source = max(val_fractions, key=lambda key: val_fractions[key])
        quotas[source] += 1

    selected: list[dict] = []
    seen: set[str] = set()
    rejected = Counter()
    attempts_by_source = Counter()
    selected_by_source = Counter()

    for source, quota in quotas.items():
        if quota <= 0:
            continue
        paths = by_source[source]
        for batch in iter_random_batches(paths, rng):
            for raw in batch:
                attempts_by_source[source] += 1
                text = normalize_text(raw or "")
                reason = quality_reason(
                    text,
                    min_chars=args.val_min_chars,
                    max_chars=args.val_max_chars,
                    min_words=args.val_min_words,
                    min_median_line_chars=args.val_min_median_line_chars,
                    max_punct_ratio=args.val_max_punct_ratio,
                    max_digit_ratio=args.val_max_digit_ratio,
                    min_letter_ratio=args.val_min_letter_ratio,
                )
                if reason:
                    rejected[f"{source}:{reason}"] += 1
                    continue
                key = exact_key(text)
                if key in seen:
                    rejected[f"{source}:duplicate"] += 1
                    continue
                seen.add(key)
                selected.append({"text": text, "source": source})
                selected_by_source[source] += 1
                if selected_by_source[source] >= quota:
                    break
            if selected_by_source[source] >= quota:
                break
        if selected_by_source[source] < quota:
            print(
                f"[warn] source {source} filled {selected_by_source[source]:,}/{quota:,} validation docs",
                flush=True,
            )

    rng.shuffle(selected)
    summary = {
        "target_docs": target_docs,
        "selected_docs": len(selected),
        "quotas": quotas,
        "selected_by_source": dict(selected_by_source),
        "attempts_by_source": dict(attempts_by_source),
        "rejected": dict(rejected.most_common()),
    }
    return selected, seen, summary


def format_seconds(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def build_train(
    by_source: dict[str, list[Path]],
    train_weights: dict[str, float],
    val_hashes: set[str],
    args: argparse.Namespace,
    rng: random.Random,
) -> tuple[list[dict], dict]:
    readers = {
        source: SourceReader(
            source=source,
            paths=paths,
            val_hashes=val_hashes,
            train_min_chars=args.train_min_chars,
            train_min_words=args.train_min_words,
            train_max_chars=args.train_max_chars,
            rng=random.Random(args.seed + 10_000 + idx),
            shuffle_files=args.shuffle_source_files,
            max_doc_chars=args.train_max_chars,
        )
        for idx, (source, paths) in enumerate(by_source.items())
        if train_weights.get(source, 0) > 0
    }
    active = deque(readers.keys())
    writer = ShardWriter(
        args.output_dir,
        shard_size=args.shard_size,
        row_group_size=args.train_row_group_size,
        prefix=args.train_prefix,
    )

    train_counts = Counter()
    source_counts = Counter()
    source_chars = Counter()
    source_bytes = Counter()
    t0 = time.time()
    approx_tokens = 0
    last_progress_docs = 0

    while active:
        source_names = list(active)
        weights = [train_weights[source] for source in source_names]
        source = rng.choices(source_names, weights=weights, k=1)[0]
        reader = readers[source]
        try:
            text = reader.next_text()
        except StopIteration:
            active.remove(source)
            continue

        writer.add(text)
        nbytes = len(text.encode("utf-8"))
        approx = max(1, int(len(text) / args.chars_per_token))
        approx_tokens += approx
        train_counts["docs"] += 1
        train_counts["chars"] += len(text)
        train_counts["utf8_bytes"] += nbytes
        train_counts["approx_tokens"] += approx
        source_counts[source] += 1
        source_chars[source] += len(text)
        source_bytes[source] += nbytes

        if (
            args.progress_every > 0
            and train_counts["docs"] - last_progress_docs >= args.progress_every
        ):
            elapsed = time.time() - t0
            print(
                f"  ...train_docs={train_counts['docs']:,} "
                f"approx_tokens={approx_tokens:,} bytes={train_counts['utf8_bytes'] / 2**30:.2f}GiB "
                f"elapsed={format_seconds(elapsed)}",
                flush=True,
            )
            last_progress_docs = train_counts["docs"]

        if args.target_train_docs > 0 and train_counts["docs"] >= args.target_train_docs:
            break
        if args.target_train_approx_tokens > 0 and approx_tokens >= args.target_train_approx_tokens:
            break

    writer.flush()
    elapsed = time.time() - t0
    reader_stats = {
        source: {
            "rows_seen": reader.rows_seen,
            "rows_emitted": reader.rows_emitted,
            "rows_skipped": dict(reader.rows_skipped.most_common()),
        }
        for source, reader in readers.items()
    }
    summary = {
        "train": dict(train_counts),
        "train_by_source": {
            source: {
                "docs": source_counts[source],
                "chars": source_chars[source],
                "utf8_bytes": source_bytes[source],
            }
            for source in sorted(source_counts)
        },
        "reader_stats": reader_stats,
        "elapsed_seconds": round(elapsed, 3),
    }
    return writer.records, summary


def write_dataset_card(output_dir: Path, summary: dict) -> None:
    readme = f"""---
language:
- ar
- ary
license: other
pretty_name: Darija Arabic Focused Nanochat Pretraining Mix
task_categories:
- text-generation
tags:
- nanochat
- pretraining
- darija
- moroccan-arabic
- arabic
- private
configs:
- config_name: default
  data_files:
  - split: train
    path: "[0-9]*_train.parquet"
  - split: validation
    path: "zzzz_val_mix.parquet"
---

# Darija Arabic Focused Nanochat Pretraining Mix

Small-run focused text-only shard set derived from the larger Darija/Arabic/English
pretraining mix. Every parquet file has exactly one column: `text`.

The train split is source-interleaved to avoid tiny models overfitting one
contiguous source/domain before seeing the next. Validation is quality-filtered,
sampled from the same source families, and excluded from train by normalized exact
text match.

## Summary

```json
{json.dumps(summary, indent=2, ensure_ascii=False)}
```
"""
    (output_dir / "README.md").write_text(readme, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=default_source_dir())
    parser.add_argument("--output-dir", type=Path, default=default_output_dir())
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--source", action="append", default=[],
                        help="Source to include. May be repeated. Defaults to Darija+Arabic sources.")
    parser.add_argument("--train-weight", action="append", default=[],
                        help="Override train source weight, e.g. darija_pure=4.0")
    parser.add_argument("--val-fraction", action="append", default=[],
                        help="Override validation source fraction, e.g. arabic_raw=0.20")
    parser.add_argument("--target-train-approx-tokens", type=int, default=1_500_000_000)
    parser.add_argument("--target-train-docs", type=int, default=-1)
    parser.add_argument("--chars-per-token", type=float, default=4.0)
    parser.add_argument("--shard-size", type=int, default=250_000)
    parser.add_argument("--train-row-group-size", type=int, default=25_000)
    parser.add_argument("--val-row-group-size", type=int, default=2_000)
    parser.add_argument("--train-prefix", type=str, default="darija_focus")
    parser.add_argument("--shuffle-source-files", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--progress-every", type=int, default=250_000)

    parser.add_argument("--val-docs", type=int, default=20_000)
    parser.add_argument("--val-min-chars", type=int, default=220)
    parser.add_argument("--val-max-chars", type=int, default=12_000)
    parser.add_argument("--val-min-words", type=int, default=18)
    parser.add_argument("--val-min-median-line-chars", type=int, default=24)
    parser.add_argument("--val-max-punct-ratio", type=float, default=0.28)
    parser.add_argument("--val-max-digit-ratio", type=float, default=0.35)
    parser.add_argument("--val-min-letter-ratio", type=float, default=0.45)

    parser.add_argument("--train-min-chars", type=int, default=80)
    parser.add_argument("--train-min-words", type=int, default=6)
    parser.add_argument("--train-max-chars", type=int, default=12_000)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.source_dir = args.source_dir.resolve()
    args.output_dir = args.output_dir.resolve()

    sources = tuple(args.source or DEFAULT_SOURCE_ORDER)
    train_weights = parse_weight_map(args.train_weight, DEFAULT_TRAIN_WEIGHTS)
    train_weights = {source: train_weights.get(source, 0.0) for source in sources}
    val_defaults = {source: DEFAULT_VAL_FRACTIONS.get(source, 0.0) for source in sources}
    val_fractions = parse_fraction_map(args.val_fraction, val_defaults)
    val_fractions = {source: val_fractions.get(source, 0.0) for source in sources}
    val_fractions = parse_fraction_map(
        [f"{source}={fraction}" for source, fraction in val_fractions.items()],
        {},
    )

    if not args.source_dir.exists():
        raise SystemExit(f"Source data dir not found: {args.source_dir}")
    if args.output_dir.exists():
        if not args.overwrite:
            raise SystemExit(f"Output dir exists; pass --overwrite: {args.output_dir}")
        shutil.rmtree(args.output_dir)
    args.output_dir.mkdir(parents=True)

    by_source = find_source_paths(args.source_dir, sources)
    rng = random.Random(args.seed)

    print("Source:", args.source_dir)
    print("Output:", args.output_dir)
    print("Train weights:", json.dumps(train_weights, ensure_ascii=False))
    print("Validation fractions:", json.dumps(val_fractions, ensure_ascii=False))
    print("Sources:")
    for source, paths in by_source.items():
        print(f"  - {source}: {len(paths)} files")

    print("\n[validation] sampling cleaned holdout rows", flush=True)
    val_rows, val_hashes, val_summary = make_validation(
        by_source,
        val_fractions,
        args.val_docs,
        args,
        rng,
    )
    if not val_rows:
        raise SystemExit("No validation rows selected; refusing to continue.")
    val_texts = [item["text"] for item in val_rows]
    write_text_parquet(args.output_dir / "zzzz_val_mix.parquet", val_texts, args.val_row_group_size)
    val_lengths = sorted(len(text) for text in val_texts)
    val_summary["lengths"] = {
        "min": percentile(val_lengths, 0.0),
        "p50": percentile(val_lengths, 0.5),
        "p90": percentile(val_lengths, 0.9),
        "p99": percentile(val_lengths, 0.99),
        "max": percentile(val_lengths, 1.0),
    }
    print(json.dumps(val_summary, indent=2, ensure_ascii=False))

    print("\n[train] writing interleaved train shards", flush=True)
    shard_records, train_summary = build_train(by_source, train_weights, val_hashes, args, rng)

    summary = {
        "settings": {
            "source_dir": str(args.source_dir),
            "output_dir": str(args.output_dir),
            "sources": list(sources),
            "train_weights": train_weights,
            "val_fractions": val_fractions,
            "target_train_approx_tokens": args.target_train_approx_tokens,
            "target_train_docs": args.target_train_docs,
            "chars_per_token": args.chars_per_token,
            "shard_size": args.shard_size,
            "seed": args.seed,
        },
        "validation": val_summary,
        "train": train_summary,
        "shards": shard_records,
    }
    (args.output_dir / "focused_manifest.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    write_dataset_card(args.output_dir, summary)
    print("\nSummary")
    print(json.dumps(summary["train"]["train"], indent=2, ensure_ascii=False))
    print(f"Manifest: {args.output_dir / 'focused_manifest.json'}")
    print(f"Validation: {args.output_dir / 'zzzz_val_mix.parquet'}")


if __name__ == "__main__":
    main()
