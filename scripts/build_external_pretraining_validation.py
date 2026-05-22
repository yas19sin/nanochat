#!/usr/bin/env python3
"""Build a clean external pretraining validation parquet.

This script does not sample from the pretraining shards. It builds
`zzzz_val_mix.parquet` from external held-out instruction/QA datasets, rendered
as plain text with a single `text` column for nanochat's base dataloader.

Default mix:
- 25k Darija: Lyte/Moroccan-Darija-Instruct-573K, test split
- 15k English: yahma/alpaca-cleaned
- 10k Arabic: FreedomIntelligence/alpaca-gpt4-arabic

Because these rows are not drawn from the pretraining train shards, this is the
fast path for an actually held-out validation file.
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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.clean_pretraining_val import exact_key, normalize_text, percentile, quality_reason


DEFAULT_BASE_DIR = Path(os.environ.get("NANOCHAT_BASE_DIR", Path.home() / ".cache" / "nanochat"))
DEFAULT_OUTPUT = Path(os.environ.get("NANOCHAT_DATA_DIR", DEFAULT_BASE_DIR / "pretrain_mix_darija_english")) / "zzzz_val_mix.parquet"
LABEL_PREFIX_RE = re.compile(r"(?m)^\s*(?:User|Assistant|Instruction|Input|Response)\s*:\s*")


@dataclass(frozen=True)
class ExternalSource:
    name: str
    group: str
    repo_id: str
    split: str
    quota: int
    formatter: str
    config: str | None = None


DEFAULT_SOURCES = [
    ExternalSource(
        name="darija_moroccan_instruct_test",
        group="darija",
        repo_id="Lyte/Moroccan-Darija-Instruct-573K",
        split="test",
        quota=25_000,
        formatter="messages",
    ),
    ExternalSource(
        name="english_alpaca_cleaned",
        group="english",
        repo_id="yahma/alpaca-cleaned",
        split="train",
        quota=15_000,
        formatter="instruction",
    ),
    ExternalSource(
        name="arabic_alpaca_gpt4",
        group="arabic",
        repo_id="FreedomIntelligence/alpaca-gpt4-arabic",
        split="train",
        quota=10_000,
        formatter="conversation",
    ),
]


def strip_label_prefixes(text: str) -> str:
    return normalize_text(LABEL_PREFIX_RE.sub("", text))


def render_messages(messages: Any) -> str:
    if not isinstance(messages, list):
        return normalize_text(messages or "")
    parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            text = normalize_text(message)
            if text:
                parts.append(text)
            continue
        content = normalize_text(
            message.get("content")
            or message.get("value")
            or message.get("text")
            or message.get("message")
            or ""
        )
        if content:
            parts.append(content)
    return "\n\n".join(parts)


def render_row(row: dict[str, Any], formatter: str) -> str:
    if formatter == "messages":
        return render_messages(row.get("messages"))
    if formatter == "conversation":
        return render_messages(row.get("conversations") or row.get("conversation") or row.get("messages"))
    if formatter == "instruction":
        instruction = normalize_text(row.get("instruction") or row.get("prompt") or "")
        input_text = normalize_text(row.get("input") or row.get("context") or "")
        output = normalize_text(row.get("output") or row.get("response") or row.get("completion") or "")
        parts = []
        if instruction:
            parts.append(instruction)
        if input_text:
            parts.append(input_text)
        if output:
            parts.append(output)
        return "\n\n".join(parts)
    if formatter == "text":
        return normalize_text(row.get("text") or "")
    raise ValueError(f"Unknown formatter: {formatter}")


def parse_source_override(value: str) -> ExternalSource:
    parts = {}
    for chunk in value.split(","):
        if "=" not in chunk:
            raise SystemExit(f"Expected comma-separated key=value source override, got {value!r}")
        key, raw = chunk.split("=", 1)
        parts[key.strip()] = raw.strip()
    required = {"name", "group", "repo_id", "split", "quota", "formatter"}
    missing = sorted(required - set(parts))
    if missing:
        raise SystemExit(f"Source override missing keys {missing}: {value!r}")
    return ExternalSource(
        name=parts["name"],
        group=parts["group"],
        repo_id=parts["repo_id"],
        split=parts["split"],
        quota=int(parts["quota"]),
        formatter=parts["formatter"],
        config=parts.get("config") or None,
    )


def load_dataset_rows(source: ExternalSource):
    from datasets import load_dataset

    token = os.environ.get("HF_TOKEN")
    kwargs: dict[str, Any] = {"split": source.split}
    if token:
        kwargs["token"] = token
    if source.config:
        return load_dataset(source.repo_id, source.config, **kwargs)
    return load_dataset(source.repo_id, **kwargs)


def select_source_rows(source: ExternalSource, args: argparse.Namespace) -> tuple[list[dict[str, str]], dict[str, Any]]:
    ds = load_dataset_rows(source)
    indices = list(range(len(ds)))
    random.Random(args.seed + sum(ord(ch) for ch in source.name)).shuffle(indices)

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    rejected = Counter()
    attempts = 0
    for idx in indices:
        attempts += 1
        if args.max_attempts_per_source > 0 and attempts > args.max_attempts_per_source:
            break
        text = strip_label_prefixes(render_row(ds[idx], source.formatter))
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
        rows.append({"text": text, "source": source.name, "group": source.group})
        if len(rows) >= source.quota:
            break

    if len(rows) < source.quota:
        raise SystemExit(
            f"{source.name} selected {len(rows):,}/{source.quota:,} rows; "
            "relax quality filters or lower quota."
        )

    lengths = sorted(len(item["text"]) for item in rows)
    return rows, {
        **asdict(source),
        "dataset_rows": len(ds),
        "attempts": attempts,
        "selected": len(rows),
        "rejected": dict(rejected.most_common()),
        "lengths": {
            "min": percentile(lengths, 0.0),
            "p50": percentile(lengths, 0.5),
            "p90": percentile(lengths, 0.9),
            "p99": percentile(lengths, 0.99),
            "max": percentile(lengths, 1.0),
        },
    }


def write_text_parquet(path: Path, rows: list[str], row_group_size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pydict({"text": rows})
    pending = path.with_suffix(path.suffix + ".pending")
    pq.write_table(table, pending, row_group_size=row_group_size)
    pending.replace(path)


def backup_existing(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup_dir = path.parent / "_validation_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = backup_dir / f"{path.stem}.before_external_rebuild.{stamp}{path.suffix}"
    shutil.copy2(path, backup)
    return backup


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest-output", type=Path, default=None)
    parser.add_argument("--source", action="append", default=[],
                        help="Override defaults with key=value source spec: name,group,repo_id,split,quota,formatter[,config].")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--row-group-size", type=int, default=2_000)
    parser.add_argument("--max-attempts-per-source", type=int, default=-1)
    parser.add_argument("--min-chars", type=int, default=80)
    parser.add_argument("--max-chars", type=int, default=12_000)
    parser.add_argument("--min-words", type=int, default=8)
    parser.add_argument("--min-median-line-chars", type=int, default=8)
    parser.add_argument("--max-punct-ratio", type=float, default=0.55)
    parser.add_argument("--max-digit-ratio", type=float, default=0.50)
    parser.add_argument("--min-letter-ratio", type=float, default=0.20)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output = args.output.resolve()
    manifest_output = (args.manifest_output or output.parent / "validation_external_manifest.json").resolve()
    if output.exists() and not args.overwrite:
        raise SystemExit(f"Output exists: {output} (use --overwrite)")

    sources = [parse_source_override(value) for value in args.source] if args.source else DEFAULT_SOURCES
    print(f"Output: {output}")
    print("Sources:")
    for source in sources:
        print(f"  - {source.name}: {source.quota:,} rows from {source.repo_id} {source.split}")

    t0 = time.time()
    selected: list[dict[str, str]] = []
    source_reports = {}
    for source in sources:
        rows, report = select_source_rows(source, args)
        selected.extend(rows)
        source_reports[source.name] = report
        print(f"  selected {len(rows):,} from {source.name}", flush=True)

    random.Random(args.seed).shuffle(selected)
    backup = None if args.no_backup else backup_existing(output)
    if backup:
        print(f"Backed up existing validation to {backup}")
    write_text_parquet(output, [item["text"] for item in selected], args.row_group_size)
    print(f"Wrote {output} ({len(selected):,} rows)")

    lengths = sorted(len(item["text"]) for item in selected)
    manifest = {
        "format": "nanochat_text_only_parquet",
        "kind": "external_validation_rebuild",
        "output_file": str(output),
        "created_unix": int(time.time()),
        "settings": {
            "seed": args.seed,
            "row_group_size": args.row_group_size,
            "rendering": "plain_content_joined_with_line_label_prefixes_stripped",
            "quality": {
                "min_chars": args.min_chars,
                "max_chars": args.max_chars,
                "min_words": args.min_words,
                "min_median_line_chars": args.min_median_line_chars,
                "max_punct_ratio": args.max_punct_ratio,
                "max_digit_ratio": args.max_digit_ratio,
                "min_letter_ratio": args.min_letter_ratio,
            },
            "validation_backup": str(backup) if backup else None,
        },
        "sources": source_reports,
        "validation": {
            "selected_docs": len(selected),
            "selected_by_group": dict(Counter(item["group"] for item in selected)),
            "selected_by_source": dict(Counter(item["source"] for item in selected)),
            "lengths": {
                "min": percentile(lengths, 0.0),
                "p50": percentile(lengths, 0.5),
                "p90": percentile(lengths, 0.9),
                "p99": percentile(lengths, 0.99),
                "max": percentile(lengths, 1.0),
            },
        },
        "train_rewrite": {
            "files": [],
            "totals": {"rows_in": 0, "rows_out": 0, "removed": 0, "chars": 0, "utf8_bytes": 0},
        },
        "elapsed_seconds": round(time.time() - t0, 3),
    }
    manifest_output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Manifest: {manifest_output}")
    print(json.dumps({
        "validation_docs": manifest["validation"]["selected_docs"],
        "validation_by_group": manifest["validation"]["selected_by_group"],
        "elapsed_seconds": manifest["elapsed_seconds"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
