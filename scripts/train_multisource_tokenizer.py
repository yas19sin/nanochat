#!/usr/bin/env python3
"""Train and audit a nanochat tokenizer from all Darija production sources.

Sources included by default:
  1. FineWeb-Edu Darija translated local shards:
     /workspace/darija_out, columns: en + darija
  2. Structured Darija translated local shards:
     /workspace/darija_struct_out, domains: code + math + toolcall, columns: en + dr
  3. Legacy Darija pretraining corpus:
     Lyte/darija-pretraining-corpus configs: arabic_raw + bilingual + pure
  4. Raw English capability corpora:
     code, math, and tool-call datasets used to preserve technical/code/math
     tokenization and support cross-lingual transfer.

The trained tokenizer is saved in nanochat's raw format under:
  <output-dir>/tokenizer/tokenizer.pkl
  <output-dir>/tokenizer/token_bytes.pt

It also attempts to export a Hugging Face fast tokenizer at <output-dir>/hf
and can upload the whole folder to a Hub model repo.
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


FINEWEB_REPO = "Lyte/fineweb-edu-darija-translated"
STRUCTURED_REPO = "Lyte/darija-structured-translated"
LEGACY_REPO = "Lyte/darija-pretraining-corpus"
RAW_CODE_REPO = "m-a-p/CodeFeedback-Filtered-Instruction"
RAW_MATH_REPO = "HuggingFaceTB/finemath"
RAW_MATH_CONFIG = "finemath-4plus"
RAW_TOOLCALL_REPO = "NousResearch/hermes-function-calling-v1"
EXTRA_ENGLISH_REPO = "HuggingFaceFW/fineweb-edu"
EXTRA_ENGLISH_CONFIG = "sample-10BT"
LEGACY_CONFIGS = ("arabic_raw", "bilingual", "pure")
STRUCTURED_DOMAINS = ("code", "math", "toolcall")
SPECIAL_TOKENS = [
    "<|bos|>",
    "<|user_start|>",
    "<|user_end|>",
    "<|assistant_start|>",
    "<|assistant_end|>",
    "<|python_start|>",
    "<|python_end|>",
    "<|output_start|>",
    "<|output_end|>",
]


@dataclass
class TextDoc:
    source: str
    text: str


class SourceStats:
    def __init__(self) -> None:
        self.docs: Counter[str] = Counter()
        self.chars: Counter[str] = Counter()
        self.tokens: Counter[str] = Counter()

    def add_text(self, source: str, text: str) -> None:
        self.docs[source] += 1
        self.chars[source] += len(text)

    def add_tokens(self, source: str, n_tokens: int) -> None:
        self.tokens[source] += n_tokens

    def as_dict(self) -> dict:
        labels = sorted(set(self.docs) | set(self.chars) | set(self.tokens))
        return {
            label: {
                "docs": int(self.docs[label]),
                "chars": int(self.chars[label]),
                "tokens": int(self.tokens[label]),
            }
            for label in labels
        }


def env_token() -> str | None:
    return (
        os.environ.get("HF_TOKEN")
        or os.environ.get("HF_WRITE_TOKEN")
        or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    )


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def clean_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def load_hub_manifest(repo_id: str, token: str | None) -> dict:
    from huggingface_hub import hf_hub_download

    local = hf_hub_download(repo_id, "manifest.json", repo_type="dataset", token=token)
    return read_json(Path(local))


def fineweb_entries(out_dir: Path | None, repo_id: str, token: str | None) -> tuple[list[dict], Path | None]:
    manifest_path = out_dir / "manifest.json" if out_dir else None
    if manifest_path and manifest_path.exists():
        manifest = read_json(manifest_path)
    else:
        manifest = load_hub_manifest(repo_id, token)
        out_dir = None
    return manifest.get("shards") or [], out_dir


def structured_entries(out_dir: Path | None, repo_id: str, token: str | None,
                       domains: Iterable[str]) -> tuple[list[tuple[str, dict]], Path | None]:
    manifest_path = out_dir / "manifest.json" if out_dir else None
    if manifest_path and manifest_path.exists():
        manifest = read_json(manifest_path)
    else:
        manifest = load_hub_manifest(repo_id, token)
        out_dir = None

    entries: list[tuple[str, dict]] = []
    per_domain = manifest.get("per_domain") or {}
    for domain in domains:
        for entry in (per_domain.get(domain) or {}).get("shards") or []:
            entries.append((domain, entry))
    return entries, out_dir


def resolve_dataset_shard(local_root: Path | None, repo_id: str, file_rel: str,
                          token: str | None) -> Path:
    if local_root:
        local = local_root / file_rel
        if local.exists():
            return local

    from huggingface_hub import hf_hub_download

    return Path(hf_hub_download(
        repo_id,
        f"data/{file_rel}",
        repo_type="dataset",
        token=token,
    ))


def iter_parquet_columns(path: Path, columns: list[str]) -> Iterator[dict]:
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(path)
    for rg_idx in range(pf.num_row_groups):
        table = pf.read_row_group(rg_idx, columns=columns)
        for row in table.to_pylist():
            yield row


def iter_fineweb(out_dir: Path | None, repo_id: str, token: str | None,
                 include_en: bool, include_darija: bool) -> Iterator[TextDoc]:
    entries, local_root = fineweb_entries(out_dir, repo_id, token)
    if not entries:
        raise RuntimeError("No FineWeb translated shards found")

    for entry in entries:
        shard = resolve_dataset_shard(local_root, repo_id, entry["file"], token)
        for row in iter_parquet_columns(shard, ["en", "darija"]):
            if include_en:
                text = (row.get("en") or "").strip()
                if text:
                    yield TextDoc("fineweb_en", text)
            if include_darija:
                text = (row.get("darija") or "").strip()
                if text:
                    yield TextDoc("fineweb_darija", text)


def iter_structured(out_dir: Path | None, repo_id: str, token: str | None,
                    domains: Iterable[str], include_en: bool,
                    include_darija: bool) -> Iterator[TextDoc]:
    entries, local_root = structured_entries(out_dir, repo_id, token, domains)
    if not entries:
        raise RuntimeError("No structured translated shards found")

    for domain, entry in entries:
        shard = resolve_dataset_shard(local_root, repo_id, entry["file"], token)
        for row in iter_parquet_columns(shard, ["en", "dr"]):
            if include_en:
                text = (row.get("en") or "").strip()
                if text:
                    yield TextDoc(f"structured_{domain}_en", text)
            if include_darija:
                text = (row.get("dr") or "").strip()
                if text:
                    yield TextDoc(f"structured_{domain}_darija", text)


def iter_legacy_hf(repo_id: str, configs: Iterable[str], token: str | None) -> Iterator[TextDoc]:
    from datasets import load_dataset

    for config in configs:
        name = None if config in ("", "default", "none", "null") else config
        label = config if name is not None else "default"
        print(f"[source] streaming {repo_id}/{label}")
        ds = load_dataset(repo_id, name, split="train", streaming=True, token=token)
        for row in ds:
            text = (row.get("text") or "").strip()
            if text:
                yield TextDoc(f"legacy_{label}", text)


def iter_legacy_local(data_dir: Path) -> Iterator[TextDoc]:
    for path in sorted(data_dir.glob("*.parquet")):
        if path.name.endswith(".tmp"):
            continue
        label = "legacy_local"
        for prefix in LEGACY_CONFIGS:
            if path.name.startswith(prefix):
                label = f"legacy_{prefix}"
                break
        for row in iter_parquet_columns(path, ["text"]):
            text = (row.get("text") or "").strip()
            if text:
                yield TextDoc(label, text)


def reached_limit(n_docs: int, n_chars: int, max_docs: int, max_chars: int) -> bool:
    if max_docs >= 0 and n_docs >= max_docs:
        return True
    if max_chars >= 0 and n_chars >= max_chars:
        return True
    return False


def iter_raw_code(repo_id: str, split: str, token: str | None,
                  max_docs: int, max_chars: int) -> Iterator[TextDoc]:
    from datasets import load_dataset

    print(f"[source] streaming raw code {repo_id}/{split}")
    ds = load_dataset(repo_id, split=split, streaming=True, token=token)
    n_docs = 0
    n_chars = 0
    for row in ds:
        q = clean_text(row.get("query") or row.get("instruction") or row.get("prompt"))
        a = clean_text(row.get("answer") or row.get("response") or row.get("output"))
        if q and a:
            text = f"### Question\n{q}\n\n### Answer\n{a}"
        else:
            text = clean_text(row.get("text"))
        if not text:
            continue
        if reached_limit(n_docs, n_chars, max_docs, max_chars):
            break
        n_docs += 1
        n_chars += len(text)
        yield TextDoc("raw_code_en", text)


def iter_raw_math(repo_id: str, config: str, split: str, token: str | None,
                  max_docs: int, max_chars: int) -> Iterator[TextDoc]:
    from datasets import load_dataset

    name = None if config in ("", "default", "none", "null") else config
    label = config if name is not None else "default"
    print(f"[source] streaming raw math {repo_id}/{label}/{split}")
    ds = load_dataset(repo_id, name=name, split=split, streaming=True, token=token)
    n_docs = 0
    n_chars = 0
    for row in ds:
        text = (
            row.get("text")
            or row.get("problem")
            or row.get("question")
            or row.get("solution")
            or ""
        )
        text = str(text).strip()
        if not text:
            continue
        if reached_limit(n_docs, n_chars, max_docs, max_chars):
            break
        n_docs += 1
        n_chars += len(text)
        yield TextDoc("raw_math_en", text)


def render_conversation_row(row: dict) -> str:
    convs = row.get("conversations") or row.get("messages") or row.get("dialog") or []
    if isinstance(convs, list) and convs:
        parts = []
        for turn in convs:
            if not isinstance(turn, dict):
                continue
            role = (
                turn.get("from")
                or turn.get("role")
                or turn.get("speaker")
                or "unknown"
            )
            value = (
                turn.get("value")
                or turn.get("content")
                or turn.get("text")
                or ""
            )
            value = str(value).strip()
            if value:
                parts.append(f"### {str(role).upper()}\n{value}")
        if parts:
            return "\n\n".join(parts)

    for key in ("text", "prompt", "query", "instruction"):
        value = clean_text(row.get(key))
        if value:
            return value
    return ""


def iter_raw_toolcall(repo_id: str, split: str, token: str | None,
                      max_docs: int, max_chars: int) -> Iterator[TextDoc]:
    from datasets import load_dataset

    print(f"[source] streaming raw toolcall {repo_id}/{split}")
    ds = load_dataset(repo_id, split=split, streaming=True, token=token)
    n_docs = 0
    n_chars = 0
    for row in ds:
        text = render_conversation_row(row).strip()
        if not text:
            continue
        if reached_limit(n_docs, n_chars, max_docs, max_chars):
            break
        n_docs += 1
        n_chars += len(text)
        yield TextDoc("raw_toolcall_en", text)


def iter_extra_english(repo_id: str, config: str, split: str, text_field: str,
                       token: str | None, max_docs: int,
                       max_chars: int) -> Iterator[TextDoc]:
    from datasets import load_dataset

    if max_docs == 0 or max_chars == 0:
        return
    name = None if config in ("", "default", "none", "null") else config
    label = config if name is not None else "default"
    print(f"[source] streaming extra English {repo_id}/{label}/{split}")
    ds = load_dataset(repo_id, name=name, split=split, streaming=True, token=token)
    n_docs = 0
    n_chars = 0
    for row in ds:
        text = clean_text(row.get(text_field))
        if not text:
            continue
        if reached_limit(n_docs, n_chars, max_docs, max_chars):
            break
        n_docs += 1
        n_chars += len(text)
        yield TextDoc("extra_english", text)


def build_docs(args, token: str | None) -> Iterator[TextDoc]:
    if not args.skip_fineweb:
        yield from iter_fineweb(
            Path(args.fineweb_out_dir) if args.fineweb_out_dir else None,
            args.fineweb_repo,
            token,
            include_en=not args.darija_only,
            include_darija=True,
        )

    if not args.skip_structured:
        yield from iter_structured(
            Path(args.structured_out_dir) if args.structured_out_dir else None,
            args.structured_repo,
            token,
            domains=args.structured_domains,
            include_en=not args.darija_only,
            include_darija=True,
        )

    if not args.skip_legacy:
        if args.legacy_data_dir:
            yield from iter_legacy_local(Path(args.legacy_data_dir))
        else:
            yield from iter_legacy_hf(args.legacy_repo, args.legacy_configs, token)

    if not args.skip_raw_code:
        yield from iter_raw_code(
            args.raw_code_repo,
            args.raw_code_split,
            token,
            args.raw_code_max_docs,
            args.raw_code_max_chars,
        )

    if not args.skip_raw_math:
        yield from iter_raw_math(
            args.raw_math_repo,
            args.raw_math_config,
            args.raw_math_split,
            token,
            args.raw_math_max_docs,
            args.raw_math_max_chars,
        )

    if not args.skip_raw_toolcall:
        yield from iter_raw_toolcall(
            args.raw_toolcall_repo,
            args.raw_toolcall_split,
            token,
            args.raw_toolcall_max_docs,
            args.raw_toolcall_max_chars,
        )

    if not args.skip_extra_english:
        yield from iter_extra_english(
            args.extra_english_repo,
            args.extra_english_config,
            args.extra_english_split,
            args.extra_english_text_field,
            token,
            args.extra_english_max_docs,
            args.extra_english_max_chars,
        )


def capped_text(text: str, doc_cap: int) -> str:
    if doc_cap > 0 and len(text) > doc_cap:
        return text[:doc_cap]
    return text


def training_iterator(args, token: str | None, stats: SourceStats) -> Iterator[str]:
    max_chars = args.max_chars
    seen_chars = 0
    seen_docs = 0
    t0 = time.time()

    for doc in build_docs(args, token):
        text = capped_text(doc.text, args.doc_cap)
        if not text:
            continue
        if max_chars > 0 and seen_chars >= max_chars:
            break
        if max_chars > 0 and seen_chars + len(text) > max_chars:
            text = text[:max_chars - seen_chars]
        stats.add_text(doc.source, text)
        seen_chars += len(text)
        seen_docs += 1
        if args.progress_every and seen_docs % args.progress_every == 0:
            dt = time.time() - t0
            print(f"[train data] docs={seen_docs:,} chars={seen_chars:,} "
                  f"rate={seen_chars / max(dt, 1e-6) / 1e6:.1f}M chars/s")
        yield text


def save_token_bytes(tokenizer, tokenizer_dir: Path) -> None:
    import torch

    vocab_size = tokenizer.get_vocab_size()
    special_set = set(tokenizer.get_special_tokens())
    token_strings = [tokenizer.decode([token_id]) for token_id in range(vocab_size)]
    token_bytes = []
    for token_str in token_strings:
        if token_str in special_set:
            token_bytes.append(0)
        else:
            token_bytes.append(len(token_str.encode("utf-8")))
    tensor = torch.tensor(token_bytes, dtype=torch.int32, device="cpu")
    path = tokenizer_dir / "token_bytes.pt"
    with path.open("wb") as handle:
        torch.save(tensor, handle)
    print(f"[save] wrote {path}")


def export_hf_fast_tokenizer(tokenizer_dir: Path, output_dir: Path) -> None:
    try:
        from transformers import PreTrainedTokenizerFast
        from transformers.integrations.tiktoken import convert_tiktoken_to_fast
    except Exception as exc:
        print(f"[hf export] skipped: transformers tiktoken export unavailable ({exc})")
        return

    with (tokenizer_dir / "tokenizer.pkl").open("rb") as handle:
        encoding = pickle.load(handle)

    output_dir.mkdir(parents=True, exist_ok=True)
    convert_tiktoken_to_fast(encoding, str(output_dir))
    fast = PreTrainedTokenizerFast(
        tokenizer_file=str(output_dir / "tokenizer.json"),
        bos_token="<|bos|>",
        eos_token=None,
        pad_token="<|bos|>",
        additional_special_tokens=SPECIAL_TOKENS[1:],
        clean_up_tokenization_spaces=False,
    )
    fast.save_pretrained(output_dir)
    print(f"[hf export] wrote {output_dir}")


def write_readme(output_dir: Path, args, train_stats: SourceStats,
                 count_stats: SourceStats | None) -> None:
    lines = [
        "# Nanochat Darija Multisource Tokenizer",
        "",
        "Nanochat RustBPE tokenizer trained from the Darija production corpus mix.",
        "",
        "Raw nanochat artifacts:",
        "",
        "- `tokenizer/tokenizer.pkl`",
        "- `tokenizer/token_bytes.pt`",
        "",
        "Hugging Face fast tokenizer export, when available, is under `hf/`.",
        "",
        "## Training Arguments",
        "",
        "```json",
        json.dumps(vars(args), indent=2, ensure_ascii=False),
        "```",
        "",
        "## Training Source Stats",
        "",
        "```json",
        json.dumps(train_stats.as_dict(), indent=2, ensure_ascii=False),
        "```",
    ]
    if count_stats is not None:
        lines.extend([
            "",
            "## Exact Token Counts",
            "",
            "```json",
            json.dumps(count_stats.as_dict(), indent=2, ensure_ascii=False),
            "```",
        ])
    (output_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def train_tokenizer(args, token: str | None) -> tuple[Path, SourceStats]:
    from nanochat.tokenizer import RustBPETokenizer

    output_dir = Path(args.output_dir)
    tokenizer_dir = output_dir / "tokenizer"
    tokenizer_dir.mkdir(parents=True, exist_ok=True)

    train_stats = SourceStats()
    print(f"[train] vocab_size={args.vocab_size:,} max_chars={args.max_chars:,} "
          f"doc_cap={args.doc_cap:,}")
    t0 = time.time()
    tokenizer = RustBPETokenizer.train_from_iterator(
        training_iterator(args, token, train_stats),
        args.vocab_size,
    )
    print(f"[train] done in {(time.time() - t0) / 60:.1f} min")
    tokenizer.save(tokenizer_dir)
    save_token_bytes(tokenizer, tokenizer_dir)
    export_hf_fast_tokenizer(tokenizer_dir, output_dir / "hf")
    return tokenizer_dir, train_stats


def load_tokenizer(tokenizer_dir: Path):
    from nanochat.tokenizer import RustBPETokenizer

    return RustBPETokenizer.from_directory(tokenizer_dir)


def count_tokens(args, token: str | None, tokenizer_dir: Path) -> SourceStats:
    tok = load_tokenizer(tokenizer_dir)
    stats = SourceStats()
    batch_texts: list[str] = []
    batch_sources: list[str] = []
    t0 = time.time()
    docs = 0

    def flush() -> None:
        nonlocal batch_texts, batch_sources
        if not batch_texts:
            return
        encoded = tok.encode(batch_texts, num_threads=args.threads)
        for source, text, ids in zip(batch_sources, batch_texts, encoded):
            stats.add_text(source, text)
            stats.add_tokens(source, len(ids))
        batch_texts = []
        batch_sources = []

    for doc in build_docs(args, token):
        text = capped_text(doc.text, args.doc_cap)
        if not text:
            continue
        batch_texts.append(text)
        batch_sources.append(doc.source)
        docs += 1
        if len(batch_texts) >= args.count_batch_size:
            flush()
        if args.progress_every and docs % args.progress_every == 0:
            total_tokens = sum(stats.tokens.values())
            dt = time.time() - t0
            print(f"[count] docs={docs:,} tokens={total_tokens:,} "
                  f"rate={total_tokens / max(dt, 1e-6):.0f} tok/s")
    flush()
    total_tokens = sum(stats.tokens.values())
    print(f"[count] total tokens={total_tokens:,}")
    for label, values in stats.as_dict().items():
        print(f"  {label:28s} docs={values['docs']:>12,} "
              f"chars={values['chars']:>16,} tokens={values['tokens']:>16,}")
    return stats


def write_manifest(output_dir: Path, args, train_stats: SourceStats,
                   count_stats: SourceStats | None) -> None:
    rec = {
        "created_at": time.time(),
        "args": vars(args),
        "train_stats": train_stats.as_dict(),
        "count_stats": count_stats.as_dict() if count_stats else None,
    }
    path = output_dir / "tokenizer_training_manifest.json"
    path.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[save] wrote {path}")


def push_to_hub(output_dir: Path, repo_id: str, token: str | None, private: bool) -> None:
    from huggingface_hub import HfApi

    api = HfApi(token=token)
    api.create_repo(repo_id=repo_id, repo_type="model", exist_ok=True, private=private)
    api.upload_folder(
        folder_path=str(output_dir),
        repo_id=repo_id,
        repo_type="model",
        commit_message="add multisource nanochat tokenizer",
    )
    print(f"[hub] uploaded {output_dir} -> {repo_id}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["train", "count", "train-and-count"],
                   default="train-and-count")
    p.add_argument("--output-dir", default="/workspace/nanochat_tokenizer_multisource")
    p.add_argument("--tokenizer-dir", default=None,
                   help="existing tokenizer dir for --mode count")
    p.add_argument("--vocab-size", type=int, default=32768)
    p.add_argument("--max-chars", type=int, default=-1,
                   help="-1 means no global cap")
    p.add_argument("--doc-cap", type=int, default=10_000,
                   help="0 disables per-document truncation")
    p.add_argument("--threads", type=int, default=8)
    p.add_argument("--count-batch-size", type=int, default=4096)
    p.add_argument("--progress-every", type=int, default=100_000)

    p.add_argument("--fineweb-out-dir", default="/workspace/darija_out")
    p.add_argument("--fineweb-repo", default=FINEWEB_REPO)
    p.add_argument("--structured-out-dir", default="/workspace/darija_struct_out")
    p.add_argument("--structured-repo", default=STRUCTURED_REPO)
    p.add_argument("--structured-domains", nargs="+", default=list(STRUCTURED_DOMAINS))
    p.add_argument("--legacy-repo", default=LEGACY_REPO)
    p.add_argument("--legacy-configs", nargs="+", default=list(LEGACY_CONFIGS))
    p.add_argument("--legacy-data-dir", default=None,
                   help="optional local nanochat-style legacy parquet dir")

    p.add_argument("--raw-code-repo", default=RAW_CODE_REPO)
    p.add_argument("--raw-code-split", default="train")
    p.add_argument("--raw-code-max-docs", type=int, default=250_000)
    p.add_argument("--raw-code-max-chars", type=int, default=-1)
    p.add_argument("--raw-math-repo", default=RAW_MATH_REPO)
    p.add_argument("--raw-math-config", default=RAW_MATH_CONFIG)
    p.add_argument("--raw-math-split", default="train")
    p.add_argument("--raw-math-max-docs", type=int, default=250_000)
    p.add_argument("--raw-math-max-chars", type=int, default=-1)
    p.add_argument("--raw-toolcall-repo", default=RAW_TOOLCALL_REPO)
    p.add_argument("--raw-toolcall-split", default="train")
    p.add_argument("--raw-toolcall-max-docs", type=int, default=-1)
    p.add_argument("--raw-toolcall-max-chars", type=int, default=-1)
    p.add_argument("--extra-english-repo", default=EXTRA_ENGLISH_REPO)
    p.add_argument("--extra-english-config", default=EXTRA_ENGLISH_CONFIG)
    p.add_argument("--extra-english-split", default="train")
    p.add_argument("--extra-english-text-field", default="text")
    p.add_argument("--extra-english-max-docs", type=int, default=0,
                   help="0 disables generic extra English by default")
    p.add_argument("--extra-english-max-chars", type=int, default=-1)

    p.add_argument("--darija-only", action="store_true",
                   help="skip English columns from translated datasets")
    p.add_argument("--skip-fineweb", action="store_true")
    p.add_argument("--skip-structured", action="store_true")
    p.add_argument("--skip-legacy", action="store_true")
    p.add_argument("--skip-raw-code", action="store_true")
    p.add_argument("--skip-raw-math", action="store_true")
    p.add_argument("--skip-raw-toolcall", action="store_true")
    p.add_argument("--skip-extra-english", action="store_true")

    p.add_argument("--push-to-hub", default=None,
                   help="Hub model repo id for tokenizer upload")
    p.add_argument("--private", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    token = env_token()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    train_stats = SourceStats()
    count_stats: SourceStats | None = None

    if args.mode in ("train", "train-and-count"):
        tokenizer_dir, train_stats = train_tokenizer(args, token)
    else:
        if not args.tokenizer_dir:
            raise SystemExit("--tokenizer-dir is required for --mode count")
        tokenizer_dir = Path(args.tokenizer_dir)

    if args.mode in ("count", "train-and-count"):
        count_stats = count_tokens(args, token, tokenizer_dir)

    write_manifest(output_dir, args, train_stats, count_stats)
    write_readme(output_dir, args, train_stats, count_stats)

    if args.push_to_hub:
        push_to_hub(output_dir, args.push_to_hub, token, args.private)


if __name__ == "__main__":
    main()
