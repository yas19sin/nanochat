#!/usr/bin/env python3
"""Count exact nanochat tokenizer tokens in a parquet pretraining directory.

The nanochat base dataloader prepends BOS to every document, so this script does
the same. It writes per-file JSONL records as it goes, making long scans
resumable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq


MIX_TRAIN_RE = re.compile(r"^\d+_(?P<source>.+)_train\.parquet$")


def default_data_dir() -> Path:
    base_dir = Path(os.environ.get("NANOCHAT_BASE_DIR", Path.home() / ".cache" / "nanochat"))
    return Path(os.environ.get("NANOCHAT_DATA_DIR", base_dir / "base_data_climbmix"))


def default_tokenizer_dir() -> Path:
    base_dir = Path(os.environ.get("NANOCHAT_BASE_DIR", Path.home() / ".cache" / "nanochat"))
    return Path(os.environ.get("NANOCHAT_TOKENIZER_DIR", base_dir / "tokenizer"))


def source_from_filename(path: Path) -> str:
    match = MIX_TRAIN_RE.match(path.name)
    if match:
        return match.group("source")
    if path.name.startswith("zzzz_") or "val" in path.stem:
        return "validation"
    return path.stem


def split_from_filename(path: Path, last_path: Path) -> str:
    if path.name.startswith("zzzz_") or path == last_path:
        return "validation"
    return "train"


def load_done_records(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    records: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            records[record["file"]] = record
    return records


def add_record(aggregate: dict[str, Any], record: dict[str, Any]) -> None:
    split = record["split"]
    source = record["source"]
    for key in ("docs", "chars", "utf8_bytes", "tokens"):
        aggregate["totals"][key] += record[key]
        aggregate["splits"][split][key] += record[key]
        aggregate["sources"][source][key] += record[key]
        aggregate["split_sources"][split][source][key] += record[key]


def empty_counts() -> dict[str, int]:
    return {"docs": 0, "chars": 0, "utf8_bytes": 0, "tokens": 0}


def count_file(
    path: Path,
    tokenizer: Any,
    split: str,
    source: str,
    batch_size: int,
    threads: int,
    progress_every: int,
) -> dict[str, Any]:
    bos_token_id = tokenizer.get_bos_token_id()
    counts = empty_counts()
    t0 = time.time()
    last_progress_docs = 0

    pf = pq.ParquetFile(path)
    for batch in pf.iter_batches(batch_size=batch_size, columns=["text"]):
        texts = [text or "" for text in batch.column(0).to_pylist()]
        encoded = tokenizer.encode(texts, prepend=bos_token_id, num_threads=threads)
        counts["docs"] += len(texts)
        counts["chars"] += sum(len(text) for text in texts)
        counts["utf8_bytes"] += sum(len(text.encode("utf-8")) for text in texts)
        counts["tokens"] += sum(len(ids) for ids in encoded)

        if progress_every > 0 and counts["docs"] - last_progress_docs >= progress_every:
            elapsed = time.time() - t0
            rate = counts["docs"] / max(elapsed, 1e-6)
            print(
                f"  ...{path.name}: docs={counts['docs']:,} "
                f"tokens={counts['tokens']:,} elapsed={format_seconds(elapsed)} "
                f"rate={rate:,.0f} docs/s",
                flush=True,
            )
            last_progress_docs = counts["docs"]

    elapsed = time.time() - t0
    record = {
        "file": path.name,
        "split": split,
        "source": source,
        "docs": counts["docs"],
        "chars": counts["chars"],
        "utf8_bytes": counts["utf8_bytes"],
        "tokens": counts["tokens"],
        "file_size_bytes": path.stat().st_size,
        "elapsed_seconds": round(elapsed, 3),
    }
    return record


def format_seconds(seconds: float) -> str:
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def write_summary(path: Path, args: argparse.Namespace, records: list[dict[str, Any]], elapsed: float) -> None:
    aggregate: dict[str, Any] = {
        "settings": {
            "data_dir": str(args.data_dir),
            "tokenizer_dir": str(args.tokenizer_dir),
            "batch_size": args.batch_size,
            "threads": args.threads,
            "bos_prepended": True,
        },
        "totals": empty_counts(),
        "splits": defaultdict(empty_counts),
        "sources": defaultdict(empty_counts),
        "split_sources": defaultdict(lambda: defaultdict(empty_counts)),
        "files": sorted(records, key=lambda item: item["file"]),
        "elapsed_seconds": round(elapsed, 3),
    }
    for record in records:
        add_record(aggregate, record)

    # Convert defaultdicts to plain dicts for stable JSON.
    aggregate["splits"] = {k: dict(v) for k, v in sorted(aggregate["splits"].items())}
    aggregate["sources"] = {k: dict(v) for k, v in sorted(aggregate["sources"].items())}
    aggregate["split_sources"] = {
        split: {source: dict(counts) for source, counts in sorted(source_counts.items())}
        for split, source_counts in sorted(aggregate["split_sources"].items())
    }
    path.write_text(json.dumps(aggregate, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--tokenizer-dir", type=Path, default=default_tokenizer_dir())
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument("--file-jsonl", type=Path, default=None)
    parser.add_argument("--batch-size", type=int, default=8192)
    parser.add_argument("--threads", type=int, default=max(1, min(16, os.cpu_count() or 1)))
    parser.add_argument("--progress-every", type=int, default=250_000)
    parser.add_argument("--max-files", type=int, default=-1)
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.data_dir = args.data_dir.resolve()
    args.tokenizer_dir = args.tokenizer_dir.resolve()
    args.output_json = args.output_json or (args.data_dir / "exact_token_counts.json")
    args.file_jsonl = args.file_jsonl or (args.data_dir / "exact_token_counts.files.jsonl")

    all_parquet_paths = sorted(path for path in args.data_dir.glob("*.parquet") if not path.name.endswith(".tmp"))
    if not all_parquet_paths:
        raise SystemExit(f"No parquet files found in {args.data_dir}")

    last_path = all_parquet_paths[-1]
    parquet_paths = all_parquet_paths
    if args.max_files > 0:
        parquet_paths = parquet_paths[: args.max_files]

    done = load_done_records(args.file_jsonl) if args.resume else {}
    from nanochat.tokenizer import RustBPETokenizer

    tokenizer = RustBPETokenizer.from_directory(str(args.tokenizer_dir))

    print(f"Data: {args.data_dir}")
    print(f"Tokenizer: {args.tokenizer_dir}")
    print(f"Files: {len(parquet_paths):,}/{len(all_parquet_paths):,} selected ({len(done):,} already counted)")
    print(f"Output JSONL: {args.file_jsonl}")
    print(f"Summary JSON: {args.output_json}")
    print("Counting includes one prepended BOS token per document.")

    records = dict(done)
    t0 = time.time()
    with args.file_jsonl.open("a", encoding="utf-8") as jsonl:
        for file_idx, path in enumerate(parquet_paths, start=1):
            if path.name in records:
                print(f"[skip] {file_idx:,}/{len(parquet_paths):,} {path.name}")
                continue
            split = split_from_filename(path, last_path)
            source = source_from_filename(path)
            print(f"[file] {file_idx:,}/{len(parquet_paths):,} {path.name} split={split} source={source}")
            record = count_file(
                path,
                tokenizer,
                split,
                source,
                args.batch_size,
                args.threads,
                args.progress_every,
            )
            jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")
            jsonl.flush()
            records[path.name] = record
            print(
                f"[done] {path.name}: docs={record['docs']:,} tokens={record['tokens']:,} "
                f"bytes={record['utf8_bytes']:,} elapsed={format_seconds(record['elapsed_seconds'])}",
                flush=True,
            )

    elapsed = time.time() - t0
    all_records = [records[path.name] for path in parquet_paths if path.name in records]
    write_summary(args.output_json, args, all_records, elapsed)

    summary = json.loads(args.output_json.read_text(encoding="utf-8"))
    print("\nSummary")
    print(json.dumps({
        "docs": summary["totals"]["docs"],
        "tokens": summary["totals"]["tokens"],
        "utf8_bytes": summary["totals"]["utf8_bytes"],
        "splits": summary["splits"],
        "elapsed_seconds": summary["elapsed_seconds"],
    }, indent=2))


if __name__ == "__main__":
    main()
