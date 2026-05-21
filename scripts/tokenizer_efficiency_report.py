#!/usr/bin/env python3
"""Report tokenizer efficiency from exact pretraining token counts.

This script is intentionally cheap: it reads `exact_token_counts.json`, produced
by `scripts.count_pretraining_tokens`, and computes chars/token and bytes/token
by source/group. It can also run a few short sample strings through the current
tokenizer to inspect behavior without scanning the corpus.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from statistics import mean
from typing import Any


ARABIC_RE = re.compile(r"[\u0600-\u06ff]")
LATIN_RE = re.compile(r"[A-Za-z]")


DEFAULT_SAMPLES = {
    "darija_arabic_script": "هاد النهار مشيت للسوق وشريت شوية خضر وفواكه.",
    "darija_latin": "ana ghadi l souq daba bach nchri khodra.",
    "arabic_msa": "المغرب بلد عربي إفريقي يقع في شمال غرب القارة الإفريقية.",
    "english": "Moroccan Arabic is a low-resource language with rich morphology.",
    "mixed": "شرح ليا بالدارجة: what is machine learning?",
    "code": "for i in range(10): print(i)",
}


def default_data_dir() -> Path:
    base_dir = Path(os.environ.get("NANOCHAT_BASE_DIR", Path.home() / ".cache" / "nanochat"))
    return Path(os.environ.get("NANOCHAT_DATA_DIR", base_dir / "base_data_climbmix"))


def default_tokenizer_dir() -> Path:
    base_dir = Path(os.environ.get("NANOCHAT_BASE_DIR", Path.home() / ".cache" / "nanochat"))
    return Path(os.environ.get("NANOCHAT_TOKENIZER_DIR", base_dir / "tokenizer"))


def group_for_source(source: str) -> str:
    if source == "validation":
        return "validation"
    if source.startswith("eng_") or source.startswith("agentic_"):
        return "english"
    if source == "arabic_raw":
        return "arabic"
    if source.startswith("darija_"):
        return "darija"
    return "other"


def empty_counts() -> dict[str, int]:
    return {"docs": 0, "chars": 0, "utf8_bytes": 0, "tokens": 0}


def add_counts(dst: dict[str, int], src: dict[str, Any]) -> None:
    for key in ("docs", "chars", "utf8_bytes", "tokens"):
        dst[key] += int(src.get(key, 0))


def safe_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def metrics(counts: dict[str, int], total_tokens: int) -> dict[str, float]:
    tokens = int(counts["tokens"])
    docs = int(counts["docs"])
    chars = int(counts["chars"])
    utf8_bytes = int(counts["utf8_bytes"])
    return {
        "docs": docs,
        "chars": chars,
        "utf8_bytes": utf8_bytes,
        "tokens": tokens,
        "token_share": safe_div(tokens, total_tokens),
        "tokens_per_doc": safe_div(tokens, docs),
        "chars_per_token": safe_div(chars, tokens),
        "bytes_per_token": safe_div(utf8_bytes, tokens),
        "tokens_per_1k_chars": safe_div(tokens * 1000, chars),
        "tokens_per_1k_bytes": safe_div(tokens * 1000, utf8_bytes),
    }


def fmt_int(value: int | float) -> str:
    return f"{int(value):,}"


def fmt_float(value: float, digits: int = 3) -> str:
    return f"{value:.{digits}f}"


def print_table(title: str, rows: list[dict[str, Any]], *, include_group: bool = False) -> None:
    print(f"\n## {title}")
    if include_group:
        header = ["name", "group", "docs", "tokens", "share", "chars/tok", "bytes/tok", "tok/1k chars", "tok/doc"]
    else:
        header = ["name", "docs", "tokens", "share", "chars/tok", "bytes/tok", "tok/1k chars", "tok/doc"]
    print("| " + " | ".join(header) + " |")
    print("| " + " | ".join(["---"] * len(header)) + " |")
    for row in rows:
        values = [
            row["name"],
        ]
        if include_group:
            values.append(row["group"])
        values.extend([
            fmt_int(row["docs"]),
            fmt_int(row["tokens"]),
            f"{100 * row['token_share']:.2f}%",
            fmt_float(row["chars_per_token"]),
            fmt_float(row["bytes_per_token"]),
            fmt_float(row["tokens_per_1k_chars"], 1),
            fmt_float(row["tokens_per_doc"], 1),
        ])
        print("| " + " | ".join(values) + " |")


def load_counts(args: argparse.Namespace) -> dict[str, Any]:
    counts_path = args.counts_json or (args.data_dir / "exact_token_counts.json")
    if not counts_path.exists():
        raise SystemExit(
            f"Counts JSON not found: {counts_path}\n"
            "Run `python -m scripts.count_pretraining_tokens --resume` first."
        )
    return json.loads(counts_path.read_text(encoding="utf-8"))


def build_report(summary: dict[str, Any]) -> dict[str, Any]:
    train_sources = summary.get("split_sources", {}).get("train") or summary.get("sources", {})
    total_tokens = int(summary["totals"]["tokens"])
    train_tokens = int(summary.get("splits", {}).get("train", {}).get("tokens", total_tokens))

    groups: dict[str, dict[str, int]] = {}
    sources = []
    for source, counts in sorted(train_sources.items()):
        group = group_for_source(source)
        groups.setdefault(group, empty_counts())
        add_counts(groups[group], counts)
        item = metrics(counts, train_tokens)
        item.update({"name": source, "group": group})
        sources.append(item)

    group_rows = []
    for group, counts in sorted(groups.items()):
        item = metrics(counts, train_tokens)
        item["name"] = group
        group_rows.append(item)

    split_rows = []
    for split, counts in sorted((summary.get("splits") or {}).items()):
        item = metrics(counts, total_tokens)
        item["name"] = split
        split_rows.append(item)

    return {
        "settings": summary.get("settings", {}),
        "totals": metrics(summary["totals"], total_tokens),
        "splits": split_rows,
        "groups": sorted(group_rows, key=lambda row: row["tokens"], reverse=True),
        "sources": sorted(sources, key=lambda row: row["tokens"], reverse=True),
    }


def load_tokenizer(tokenizer_dir: Path):
    from nanochat.tokenizer import RustBPETokenizer

    return RustBPETokenizer.from_directory(str(tokenizer_dir))


def sample_report(tokenizer, samples: dict[str, str], *, prepend_bos: bool, show_tokens: int) -> list[dict[str, Any]]:
    bos_id = tokenizer.get_bos_token_id()
    rows = []
    for name, text in samples.items():
        ids = tokenizer.encode(text, prepend=bos_id if prepend_bos else None)
        token_preview = []
        for token_id in ids[:show_tokens]:
            try:
                piece = tokenizer.decode([token_id])
            except Exception:
                piece = "<decode-error>"
            token_preview.append({"id": int(token_id), "piece": piece})
        rows.append({
            "name": name,
            "text": text,
            "chars": len(text),
            "utf8_bytes": len(text.encode("utf-8")),
            "tokens": len(ids),
            "chars_per_token": safe_div(len(text), len(ids)),
            "bytes_per_token": safe_div(len(text.encode("utf-8")), len(ids)),
            "tokens_per_1k_chars": safe_div(len(ids) * 1000, len(text)),
            "preview": token_preview,
        })
    return rows


def vocab_report(tokenizer) -> dict[str, Any]:
    special = set(tokenizer.get_special_tokens())
    rows = []
    for token_id in range(tokenizer.get_vocab_size()):
        text = tokenizer.decode([token_id])
        if text in special:
            kind = "special"
        elif ARABIC_RE.search(text):
            kind = "arabic"
        elif LATIN_RE.search(text):
            kind = "latin"
        elif any(ch.isdigit() for ch in text):
            kind = "digit"
        elif text.strip():
            kind = "punct_or_symbol"
        else:
            kind = "space"
        rows.append({
            "id": token_id,
            "text": text,
            "kind": kind,
            "chars": len(text),
            "utf8_bytes": len(text.encode("utf-8")),
        })
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_kind.setdefault(row["kind"], []).append(row)
    return {
        kind: {
            "tokens": len(items),
            "avg_chars": mean(item["chars"] for item in items),
            "avg_bytes": mean(item["utf8_bytes"] for item in items),
        }
        for kind, items in sorted(by_kind.items())
    }


def read_sample_file(path: Path) -> dict[str, str]:
    samples = {}
    for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if "\t" in line:
            name, text = line.split("\t", 1)
        else:
            name, text = f"sample_{idx}", line
        samples[name] = text
    return samples


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--counts-json", type=Path, default=None)
    parser.add_argument("--tokenizer-dir", type=Path, default=default_tokenizer_dir())
    parser.add_argument("--sample-file", type=Path, default=None,
                        help="Optional UTF-8 text file. Lines can be `name<TAB>text` or plain text.")
    parser.add_argument("--no-samples", action="store_true")
    parser.add_argument("--no-bos", action="store_true",
                        help="Do not prepend BOS for sample strings.")
    parser.add_argument("--show-token-pieces", type=int, default=24)
    parser.add_argument("--inspect-vocab", action="store_true")
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.data_dir = args.data_dir.resolve()
    args.tokenizer_dir = args.tokenizer_dir.resolve()
    summary = load_counts(args)
    report = build_report(summary)

    print("# Tokenizer Efficiency Report")
    print(f"\nData: `{summary.get('settings', {}).get('data_dir', args.data_dir)}`")
    print(f"Tokenizer: `{summary.get('settings', {}).get('tokenizer_dir', args.tokenizer_dir)}`")
    print(f"BOS prepended in counts: `{summary.get('settings', {}).get('bos_prepended', 'unknown')}`")
    print_table("Splits", report["splits"])
    print_table("Train Groups", report["groups"])
    print_table("Train Sources", report["sources"], include_group=True)

    if not args.no_samples or args.inspect_vocab:
        tokenizer = load_tokenizer(args.tokenizer_dir)
        if not args.no_samples:
            samples = read_sample_file(args.sample_file) if args.sample_file else DEFAULT_SAMPLES
            sample_rows = sample_report(
                tokenizer,
                samples,
                prepend_bos=not args.no_bos,
                show_tokens=args.show_token_pieces,
            )
            report["samples"] = sample_rows
            print("\n## Samples")
            for row in sample_rows:
                print(
                    f"- {row['name']}: tokens={row['tokens']:,}, "
                    f"chars/tok={row['chars_per_token']:.3f}, "
                    f"bytes/tok={row['bytes_per_token']:.3f}, "
                    f"tok/1k chars={row['tokens_per_1k_chars']:.1f}"
                )
                preview = " ".join(f"{item['id']}:{item['piece']!r}" for item in row["preview"])
                print(f"  preview: {preview}")
        if args.inspect_vocab:
            report["vocab"] = vocab_report(tokenizer)
            print("\n## Vocab Composition")
            for kind, row in report["vocab"].items():
                print(
                    f"- {kind}: tokens={row['tokens']:,}, "
                    f"avg_chars={row['avg_chars']:.2f}, avg_bytes={row['avg_bytes']:.2f}"
                )

    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nWrote JSON report: {args.output_json}")


if __name__ == "__main__":
    main()
