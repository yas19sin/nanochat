#!/usr/bin/env python3
"""Clean the held-out nanochat validation parquet.

This rewrites only the final validation file, keeping train shards untouched.
It is intentionally conservative: short fragments, line-poem-like rows, noisy
punctuation rows, and near-duplicates are removed.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
from collections import Counter
from pathlib import Path
from statistics import median

import pyarrow as pa
import pyarrow.parquet as pq


SPACE_RE = re.compile(r"[ \t\r\f\v]+")
BLANK_LINES_RE = re.compile(r"\n{3,}")
ARABIC_RE = re.compile(r"[\u0600-\u06ff]")
LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)


def default_data_dir() -> Path:
    base_dir = Path(os.environ.get("NANOCHAT_BASE_DIR", Path.home() / ".cache" / "nanochat"))
    return Path(os.environ.get("NANOCHAT_DATA_DIR", base_dir / "base_data_climbmix"))


def normalize_text(value: str) -> str:
    text = str(value).replace("\x00", " ").replace("\u00a0", " ")
    text = "\n".join(SPACE_RE.sub(" ", line).strip() for line in text.splitlines())
    text = BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def exact_key(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def quality_reason(
    text: str,
    *,
    min_chars: int,
    max_chars: int,
    min_words: int,
    min_median_line_chars: int,
    max_punct_ratio: float,
    max_digit_ratio: float,
    min_letter_ratio: float,
) -> str | None:
    n_chars = len(text)
    if n_chars < min_chars:
        return "too_short"
    if n_chars > max_chars:
        return "too_long"

    words = re.findall(r"\S+", text)
    if len(words) < min_words:
        return "too_few_words"

    nonspace = [ch for ch in text if not ch.isspace()]
    if not nonspace:
        return "empty"

    letters = LETTER_RE.findall(text)
    if len(letters) / max(len(nonspace), 1) < min_letter_ratio:
        return "low_letter_ratio"

    punct = sum(1 for ch in nonspace if not ch.isalnum())
    if punct / max(len(nonspace), 1) > max_punct_ratio:
        return "high_punct_ratio"

    digits = sum(1 for ch in nonspace if ch.isdigit())
    if digits / max(len(nonspace), 1) > max_digit_ratio:
        return "high_digit_ratio"

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 6 and median(len(line) for line in lines) < min_median_line_chars:
        return "short_line_fragment"

    if text.count("...") + text.count("…") > 12:
        return "many_ellipses"

    return None


def iter_texts(path: Path):
    pf = pq.ParquetFile(path)
    for batch in pf.iter_batches(columns=["text"], batch_size=8192):
        for text in batch.column(0).to_pylist():
            yield text or ""


def write_text_parquet(path: Path, rows: list[str], row_group_size: int) -> None:
    table = pa.Table.from_pydict({"text": rows})
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(table, tmp_path, row_group_size=row_group_size)
    tmp_path.replace(path)


def percentile(sorted_values: list[int], frac: float) -> int:
    if not sorted_values:
        return 0
    idx = min(len(sorted_values) - 1, max(0, int(round(frac * (len(sorted_values) - 1)))))
    return sorted_values[idx]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--target-docs", type=int, default=20_000)
    parser.add_argument("--min-chars", type=int, default=240)
    parser.add_argument("--max-chars", type=int, default=12_000)
    parser.add_argument("--min-words", type=int, default=24)
    parser.add_argument("--min-median-line-chars", type=int, default=28)
    parser.add_argument("--max-punct-ratio", type=float, default=0.28)
    parser.add_argument("--max-digit-ratio", type=float, default=0.35)
    parser.add_argument("--min-letter-ratio", type=float, default=0.45)
    parser.add_argument("--row-group-size", type=int, default=2_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.data_dir = args.data_dir.resolve()
    input_path = (args.input or (args.data_dir / "zzzz_val_mix.parquet")).resolve()
    output_path = (args.output or input_path).resolve()

    if not input_path.exists():
        raise SystemExit(f"Validation parquet not found: {input_path}")

    accepted: list[str] = []
    rejected = Counter()
    seen = set()
    input_docs = 0

    for raw_text in iter_texts(input_path):
        input_docs += 1
        text = normalize_text(raw_text)
        reason = quality_reason(
            text,
            min_chars=args.min_chars,
            max_chars=args.max_chars,
            min_words=args.min_words,
            min_median_line_chars=args.min_median_line_chars,
            max_punct_ratio=args.max_punct_ratio,
            max_digit_ratio=args.max_digit_ratio,
            min_letter_ratio=args.min_letter_ratio,
        )
        if reason:
            rejected[reason] += 1
            continue
        key = exact_key(text)
        if key in seen:
            rejected["duplicate"] += 1
            continue
        seen.add(key)
        accepted.append(text)

    rng = random.Random(args.seed)
    rng.shuffle(accepted)
    if args.target_docs > 0:
        accepted = accepted[: args.target_docs]

    lengths = sorted(len(text) for text in accepted)
    summary = {
        "input_file": str(input_path),
        "output_file": str(output_path),
        "input_docs": input_docs,
        "accepted_docs": len(accepted),
        "target_docs": args.target_docs,
        "rejected": dict(rejected.most_common()),
        "lengths": {
            "min": percentile(lengths, 0.0),
            "p50": percentile(lengths, 0.5),
            "p90": percentile(lengths, 0.9),
            "p99": percentile(lengths, 0.99),
            "max": percentile(lengths, 1.0),
        },
        "settings": {
            "min_chars": args.min_chars,
            "max_chars": args.max_chars,
            "min_words": args.min_words,
            "min_median_line_chars": args.min_median_line_chars,
            "max_punct_ratio": args.max_punct_ratio,
            "max_digit_ratio": args.max_digit_ratio,
            "min_letter_ratio": args.min_letter_ratio,
            "seed": args.seed,
        },
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    if args.dry_run:
        return
    if not accepted:
        raise SystemExit("No validation rows passed quality filters; refusing to write empty validation.")

    if output_path == input_path and not args.no_backup:
        backup_dir = input_path.parent / "_validation_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / (input_path.stem + ".before_clean.parquet")
        if not backup_path.exists():
            shutil.copy2(input_path, backup_path)
            print(f"Backed up original validation file to {backup_path}")

    write_text_parquet(output_path, accepted, args.row_group_size)
    print(f"Wrote cleaned validation parquet: {output_path} ({len(accepted):,} rows)")


if __name__ == "__main__":
    main()
