#!/usr/bin/env python3
"""Write an updated Hugging Face dataset card from exact token counts."""

from __future__ import annotations

import argparse
import json
import os
import random
from collections import defaultdict
from pathlib import Path
from typing import Any


LANG_LABELS = {
    "english": "English",
    "arabic": "Arabic",
    "darija": "Darija",
    "unknown": "Unknown",
    "validation": "Validation",
}

SOURCE_NOTES = {
    "eng_general_finepdfs_dclm_fwe": "General English pretraining mixture: FinePDFs-Edu, DCLM, and FineWeb-Edu, globally shuffled upstream.",
    "eng_code_nemotron": "English code/concepts data.",
    "eng_math_stackexchange": "StackExchange math Q/A formatted as plain text.",
    "eng_reason_algorithmic": "Algorithmic reasoning text.",
    "eng_reason_formal_logic": "Formal logic reasoning text.",
    "eng_reason_economics": "Economics reasoning text.",
    "eng_reason_multiple_choice": "Multiple-choice reasoning text.",
    "arabic_raw": "Arabic raw subset from the Darija pretraining corpus.",
    "darija_bilingual": "Bilingual Darija/Arabic-style subset from the Darija pretraining corpus.",
    "darija_pure": "Pure Darija subset from the Darija pretraining corpus.",
    "darija_fineweb_edu_clean": "Cleaned Darija FineWeb-Edu subset.",
}


def default_data_dir() -> Path:
    base_dir = Path(os.environ.get("NANOCHAT_BASE_DIR", Path.home() / ".cache" / "nanochat"))
    return Path(os.environ.get("NANOCHAT_DATA_DIR", base_dir / "pretrain_mix_darija_english"))


def default_tokenizer_dir() -> Path:
    base_dir = Path(os.environ.get("NANOCHAT_BASE_DIR", Path.home() / ".cache" / "nanochat"))
    return Path(os.environ.get("NANOCHAT_TOKENIZER_DIR", base_dir / "tokenizer"))


def empty_counts() -> dict[str, int]:
    return {"docs": 0, "chars": 0, "utf8_bytes": 0, "tokens": 0}


def add_counts(dst: dict[str, int], src: dict[str, int]) -> None:
    for key in ("docs", "chars", "utf8_bytes", "tokens"):
        dst[key] += int(src.get(key, 0))


def pct(part: int, total: int) -> str:
    if total <= 0:
        return "0.00%"
    return f"{100 * part / total:.2f}%"


def fmt_int(value: int | float | None) -> str:
    if value is None:
        return "all"
    return f"{int(value):,}"


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
    return records


def group_for_source(source: str, manifest: dict[str, Any]) -> str:
    sources = manifest.get("sources") or {}
    if source in sources:
        group = sources[source].get("group")
        if group == "english":
            return "english"
        if group in {"arabic", "darija"}:
            return group
    if source.startswith("eng_"):
        return "english"
    if source.startswith("arabic"):
        return "arabic"
    if source.startswith("darija"):
        return "darija"
    return "unknown"


def summarize_counts(records: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    totals = empty_counts()
    split_totals: dict[str, dict[str, int]] = defaultdict(empty_counts)
    source_totals: dict[str, dict[str, int]] = defaultdict(empty_counts)
    group_totals: dict[str, dict[str, int]] = defaultdict(empty_counts)
    split_group_totals: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(empty_counts))

    for record in records:
        split = record["split"]
        source = record["source"]
        group = group_for_source(source, manifest) if split == "train" else "validation"
        add_counts(totals, record)
        add_counts(split_totals[split], record)
        add_counts(source_totals[source], record)
        add_counts(group_totals[group], record)
        add_counts(split_group_totals[split][group], record)

    return {
        "totals": totals,
        "splits": {k: dict(v) for k, v in sorted(split_totals.items())},
        "sources": {k: dict(v) for k, v in sorted(source_totals.items())},
        "groups": {k: dict(v) for k, v in sorted(group_totals.items())},
        "split_groups": {
            split: {group: dict(counts) for group, counts in sorted(groups.items())}
            for split, groups in sorted(split_group_totals.items())
        },
    }


def reconstruct_validation_counts(args: argparse.Namespace, manifest: dict[str, Any]) -> dict[str, Any] | None:
    backup = args.validation_backup
    if backup is None or not backup.exists() or not args.tokenizer_dir.exists():
        return None

    from nanochat.tokenizer import RustBPETokenizer
    from scripts.clean_pretraining_val import (
        exact_key,
        iter_texts,
        label_for_index,
        normalize_text,
        quality_reason,
        select_with_group_mix,
        val_spans_from_manifest,
    )

    input_texts = list(iter_texts(backup))
    spans = val_spans_from_manifest(args.manifest, len(input_texts))
    accepted = []
    rejected: dict[str, int] = defaultdict(int)
    seen = set()
    for idx, raw_text in enumerate(input_texts):
        source, group = label_for_index(idx, spans)
        text = normalize_text(raw_text)
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
            rejected[reason] += 1
            continue
        key = exact_key(text)
        if key in seen:
            rejected["duplicate"] += 1
            continue
        seen.add(key)
        accepted.append({"text": text, "source": source, "group": group})

    selected = select_with_group_mix(accepted, args.val_target_docs, random.Random(args.val_seed))
    tokenizer = RustBPETokenizer.from_directory(str(args.tokenizer_dir))
    bos = tokenizer.get_bos_token_id()

    by_group: dict[str, dict[str, int]] = defaultdict(empty_counts)
    by_source: dict[str, dict[str, int]] = defaultdict(empty_counts)
    batch_size = args.batch_size
    for i in range(0, len(selected), batch_size):
        batch = selected[i:i + batch_size]
        texts = [item["text"] for item in batch]
        encoded = tokenizer.encode(texts, prepend=bos, num_threads=args.threads)
        for item, text, ids in zip(batch, texts, encoded):
            counts = {
                "docs": 1,
                "chars": len(text),
                "utf8_bytes": len(text.encode("utf-8")),
                "tokens": len(ids),
            }
            add_counts(by_group[item["group"]], counts)
            add_counts(by_source[item["source"]], counts)

    total = empty_counts()
    for counts in by_group.values():
        add_counts(total, counts)
    return {
        "input_file": str(backup),
        "selected_docs": len(selected),
        "accepted_docs": len(accepted),
        "rejected": dict(sorted(rejected.items())),
        "settings": {
            "target_docs": args.val_target_docs,
            "min_chars": args.val_min_chars,
            "max_chars": args.val_max_chars,
            "min_words": args.val_min_words,
            "min_median_line_chars": args.val_min_median_line_chars,
            "seed": args.val_seed,
        },
        "totals": total,
        "groups": {k: dict(v) for k, v in sorted(by_group.items())},
        "sources": {k: dict(v) for k, v in sorted(by_source.items())},
    }


def source_target(info: dict[str, Any]) -> str:
    target = info.get("target_tokens")
    return "all" if target is None else f"{int(target):,}"


def build_readme(args: argparse.Namespace, manifest: dict[str, Any], summary: dict[str, Any], val_lang: dict[str, Any] | None) -> str:
    counts = summary["totals"]
    train = summary["splits"].get("train", empty_counts())
    validation = summary["splits"].get("validation", empty_counts())
    train_groups = {
        group: counts
        for group, counts in summary["split_groups"].get("train", {}).items()
        if group in {"english", "arabic", "darija"}
    }

    group_rows = []
    for group in ("english", "arabic", "darija"):
        c = train_groups.get(group, empty_counts())
        group_rows.append([
            LANG_LABELS[group],
            fmt_int(c["docs"]),
            fmt_int(c["chars"]),
            fmt_int(c["utf8_bytes"]),
            fmt_int(c["tokens"]),
            pct(c["tokens"], train["tokens"]),
        ])

    val_rows = []
    if val_lang:
        for group in ("arabic", "darija"):
            c = val_lang["groups"].get(group, empty_counts())
            val_rows.append([
                LANG_LABELS[group],
                fmt_int(c["docs"]),
                fmt_int(c["chars"]),
                fmt_int(c["utf8_bytes"]),
                fmt_int(c["tokens"]),
                pct(c["tokens"], val_lang["totals"]["tokens"]),
            ])
    else:
        val_rows.append([
            "Validation",
            fmt_int(validation["docs"]),
            fmt_int(validation["chars"]),
            fmt_int(validation["utf8_bytes"]),
            fmt_int(validation["tokens"]),
            "100.00%",
        ])

    source_rows = []
    sources = manifest.get("sources") or {}
    for name, info in sorted(sources.items(), key=lambda item: item[1].get("priority", 999)):
        if name not in summary["sources"]:
            continue
        c = summary["sources"][name]
        group = group_for_source(name, manifest)
        source_rows.append([
            name,
            LANG_LABELS.get(group, group),
            info.get("repo_id") or "",
            str(info.get("config")),
            info.get("split") or "",
            source_target(info),
            fmt_int(c["docs"]),
            fmt_int(c["tokens"]),
        ])

    exact_json_name = args.exact_counts.name
    exact_files_name = args.file_counts.name
    val_note = ""
    if val_lang:
        val_note = (
            f"- Cleaned validation language reconstruction: {fmt_int(val_lang['totals']['tokens'])} exact tokens "
            f"across {fmt_int(val_lang['totals']['docs'])} docs.\n"
            f"- Validation cleaning filters: min chars `{val_lang['settings']['min_chars']}`, "
            f"min words `{val_lang['settings']['min_words']}`, min median line chars "
            f"`{val_lang['settings']['min_median_line_chars']}`.\n"
        )

    return f"""---
language:
- en
- ar
- ary
license: other
pretty_name: Darija English Arabic Nanochat Pretraining Mix
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

# Darija English Arabic Nanochat Pretraining Mix

Private text-only pretraining shard set for nanochat. Every parquet file has exactly one column: `text`.

The nanochat dataloader sorts local parquet files lexicographically, uses all but the final parquet as train, and uses the final parquet as validation. This repo keeps `zzzz_val_mix.parquet` as the final file. Hugging Face split metadata is declared for viewer/download convenience.

## Current Summary

Counts below are exact counts with the `Lyte/darija-nanochat-tokenizer-32k` tokenizer and include one prepended `<|bos|>` token per document, matching nanochat base pretraining.

{md_table(
    ["Split", "Docs", "Chars", "UTF-8 bytes", "Exact tokens"],
    [
        ["train", fmt_int(train["docs"]), fmt_int(train["chars"]), fmt_int(train["utf8_bytes"]), fmt_int(train["tokens"])],
        ["validation", fmt_int(validation["docs"]), fmt_int(validation["chars"]), fmt_int(validation["utf8_bytes"]), fmt_int(validation["tokens"])],
        ["total", fmt_int(counts["docs"]), fmt_int(counts["chars"]), fmt_int(counts["utf8_bytes"]), fmt_int(counts["tokens"])],
    ],
)}

## Train Language Mix

{md_table(["Language group", "Docs", "Chars", "UTF-8 bytes", "Exact tokens", "Token share"], group_rows)}

## Validation Mix

`zzzz_val_mix.parquet` was cleaned from the initial held-out validation file to remove short fragments and obvious noisy rows. It is small and intended only for training-time BPB monitoring; a separate curated evaluation set should be built for final model assessment.

{md_table(["Language group", "Docs", "Chars", "UTF-8 bytes", "Exact tokens", "Token share"], val_rows)}

{val_note}## Source Breakdown

{md_table(["Source", "Group", "Repo", "Config", "Split", "Target tokens", "Docs", "Exact tokens"], source_rows)}

## Files

- Train shards: `[0-9]*_train.parquet`
- Validation shard: `zzzz_val_mix.parquet`
- Build manifest: `manifest.json`
- Exact count summary: `{exact_json_name}`
- Per-file exact counts: `{exact_files_name}`
- Machine-readable language counts: `{args.language_counts.name}`

## Build Notes

- Layout: `curriculum`, ordered English first, then Arabic, then Darija.
- English-side target during build: approximately `20B` tokens before exact tokenizer training/counting.
- Arabic and Darija sources were included fully.
- Agentic/tool-use SFT-shaped sources were skipped for this base-pretraining dataset. A separate Darija-focused SFT/tool-use dataset should be built for instruction and agentic behavior.
- The arithmetic-only `jonathanasdf/MathGLM-dataset-5M` corpus was disabled.
- Tokenizer repo: `Lyte/darija-nanochat-tokenizer-32k`.
- Recommended first pretraining horizon: one pass, about `22.5B` train tokens.

## Licensing

License is marked `other` because this is a private mixture of multiple upstream datasets with different licenses and usage constraints. Check the upstream dataset cards before redistributing or using outside the intended private training workflow.

## Source Notes

{chr(10).join(f"- `{name}`: {SOURCE_NOTES.get(name, '')}" for name in [row[0] for row in source_rows])}
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--tokenizer-dir", type=Path, default=default_tokenizer_dir())
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--exact-counts", type=Path, default=None)
    parser.add_argument("--file-counts", type=Path, default=None)
    parser.add_argument("--language-counts", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--validation-backup", type=Path, default=None)
    parser.add_argument("--batch-size", type=int, default=8192)
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--val-target-docs", type=int, default=10_000)
    parser.add_argument("--val-min-chars", type=int, default=180)
    parser.add_argument("--val-max-chars", type=int, default=12_000)
    parser.add_argument("--val-min-words", type=int, default=16)
    parser.add_argument("--val-min-median-line-chars", type=int, default=24)
    parser.add_argument("--val-max-punct-ratio", type=float, default=0.28)
    parser.add_argument("--val-max-digit-ratio", type=float, default=0.35)
    parser.add_argument("--val-min-letter-ratio", type=float, default=0.45)
    parser.add_argument("--val-seed", type=int, default=42)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.data_dir = args.data_dir.resolve()
    args.tokenizer_dir = args.tokenizer_dir.resolve()
    args.manifest = (args.manifest or (args.data_dir / "manifest.json")).resolve()
    args.exact_counts = (args.exact_counts or (args.data_dir / "exact_token_counts.json")).resolve()
    args.file_counts = (args.file_counts or (args.data_dir / "exact_token_counts.files.jsonl")).resolve()
    args.language_counts = (args.language_counts or (args.data_dir / "language_token_counts.json")).resolve()
    args.output = (args.output or (args.data_dir / "README.md")).resolve()
    args.validation_backup = (args.validation_backup or (args.data_dir / "_validation_backups" / "zzzz_val_mix.before_clean.parquet")).resolve()

    manifest = read_json(args.manifest)
    records = read_jsonl(args.file_counts)
    summary = summarize_counts(records, manifest)
    val_lang = reconstruct_validation_counts(args, manifest)

    language_counts = {
        "exact_count_file": str(args.exact_counts),
        "per_file_count_file": str(args.file_counts),
        "tokenizer_dir": str(args.tokenizer_dir),
        "bos_prepended": True,
        "summary": summary,
        "validation_reconstructed": val_lang,
    }
    args.language_counts.write_text(
        json.dumps(language_counts, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    readme = build_readme(args, manifest, summary, val_lang)
    args.output.write_text(readme, encoding="utf-8")

    print(f"Wrote {args.output}")
    print(f"Wrote {args.language_counts}")
    train_groups = summary["split_groups"].get("train", {})
    print(json.dumps({
        "train": train_groups,
        "validation_reconstructed": val_lang["groups"] if val_lang else None,
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
