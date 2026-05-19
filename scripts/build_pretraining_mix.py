"""
Build text-only nanochat pretraining shards from the Darija/Arabic/English mix.

nanochat's base dataloader expects a flat directory of parquet files. It sorts
the filenames, uses every file except the last for train, and uses the final
file for validation. Every parquet written here contains exactly one column:
`text`.

The default source plan is:
- all Lyte/darija-pretraining-corpus configs
- all Lyte/fineweb-edu-darija-clean
- all enabled English sources. Use --english-budget-tokens to cap and allocate
  a smaller pool across 70/5/5/5/15 buckets:
  general pretraining / code / math / agentic / remaining reasoning.

Use --smoke-test to exercise the writer locally without Hugging Face access.
"""

from __future__ import annotations

import argparse
import gzip
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
from typing import Any, Iterable


NEMOTRON_PRETRAIN = "nvidia/Nemotron-Pretraining-Specialized-v1.1"
FINEPDFS_SHUFFLED = "HuggingFaceFW/finepdfs_edu_50BT-dclm_30BT-fineweb_edu_20BT-shuffled"
LEGACY_DARIJA = "Lyte/darija-pretraining-corpus"
DARIJA_FINEWEB_CLEAN = "Lyte/fineweb-edu-darija-clean"
STACKEXCHANGE_MATH = "flax-sentence-embeddings/stackexchange_math_jsonl"
NEMOTRON_AGENTIC = "nvidia/Nemotron-SFT-Agentic-v2"
XLAM_FUNCTION_CALLING = "Salesforce/xlam-function-calling-60k"
MATHGLM_NUMBERS = "jonathanasdf/MathGLM-dataset-5M"

TRAIN_ROW_GROUP_SIZE = 25_000
VAL_ROW_GROUP_SIZE = 2_000
DEFAULT_SHARD_SIZE = 250_000
DEFAULT_VAL_SIZE = 50_000
DEFAULT_ENGLISH_BUDGET_TOKENS = -1
DEFAULT_CHARS_PER_TOKEN = 4.0
MIN_CHARS = 16

SPACE_RE = re.compile(r"[ \t\r\f\v]+")
BLANK_LINES_RE = re.compile(r"\n{4,}")


@dataclass(frozen=True)
class SourceSpec:
    name: str
    group: str
    repo_id: str
    config: str | None
    split: str
    formatter: str
    target_tokens: int | None
    priority: int
    weight: int
    enabled: bool = True
    note: str = ""


@dataclass
class SourceStats:
    rows_seen: int = 0
    docs_emitted: int = 0
    docs_train: int = 0
    docs_val: int = 0
    rows_skipped: int = 0
    chars_emitted: int = 0
    bytes_emitted: int = 0
    approx_tokens_emitted: int = 0
    stopped_reason: str = ""


class TokenCounter:
    def __init__(self, tokenizer_dir: Path | None) -> None:
        self.tokenizer_dir = tokenizer_dir
        self.tokenizer = None
        if tokenizer_dir is not None:
            from nanochat.tokenizer import RustBPETokenizer

            self.tokenizer = RustBPETokenizer.from_directory(str(tokenizer_dir))
            self.bos_token_id = self.tokenizer.get_bos_token_id()
        else:
            self.bos_token_id = None

    @property
    def exact(self) -> bool:
        return self.tokenizer is not None

    def count(self, text: str, chars_per_token: float) -> int:
        if self.tokenizer is not None:
            # nanochat's dataloader prepends BOS to every document.
            return len(self.tokenizer.encode(text, prepend=self.bos_token_id))
        return max(1, int(len(text) / chars_per_token))


class SourceRuntime:
    def __init__(
        self,
        spec: SourceSpec,
        rows: Iterable[dict[str, Any]],
        *,
        token_counter: TokenCounter,
        chars_per_token: float,
        max_docs: int,
        max_doc_chars: int,
    ) -> None:
        self.spec = spec
        self._rows = iter(rows)
        self.token_counter = token_counter
        self.chars_per_token = chars_per_token
        self.max_docs = max_docs
        self.max_doc_chars = max_doc_chars
        self.stats = SourceStats()

    def next_text(self) -> str:
        if self.max_docs > 0 and self.stats.docs_emitted >= self.max_docs:
            self.stats.stopped_reason = "max_docs_per_source"
            raise StopIteration
        if (
            self.spec.target_tokens is not None
            and self.stats.approx_tokens_emitted >= self.spec.target_tokens
        ):
            self.stats.stopped_reason = "target_tokens"
            raise StopIteration

        while True:
            row = next(self._rows)
            self.stats.rows_seen += 1
            text = format_row(row, self.spec.formatter)
            text = normalize_text(text)
            if len(text) < MIN_CHARS:
                self.stats.rows_skipped += 1
                continue
            if self.max_doc_chars > 0 and len(text) > self.max_doc_chars:
                text = text[: self.max_doc_chars].rstrip()
            if not text:
                self.stats.rows_skipped += 1
                continue

            approx_tokens = self.token_counter.count(text, self.chars_per_token)
            nbytes = len(text.encode("utf-8"))
            self.stats.docs_emitted += 1
            self.stats.chars_emitted += len(text)
            self.stats.bytes_emitted += nbytes
            self.stats.approx_tokens_emitted += approx_tokens
            return text


class ValidationMixer:
    def __init__(
        self,
        val_size: int,
        darija_frac: float,
        arabic_frac: float,
    ) -> None:
        self.val_size = val_size
        darija_quota = int(val_size * darija_frac)
        arabic_quota = int(val_size * arabic_frac)
        english_quota = max(0, val_size - darija_quota - arabic_quota)
        self.quotas = {
            "darija": darija_quota,
            "arabic": arabic_quota,
            "english": english_quota,
        }
        self.counts: Counter[str] = Counter()
        self.rows: list[str] = []

    def category_for(self, group: str) -> str:
        if group == "darija":
            return "darija"
        if group == "arabic":
            return "arabic"
        return "english"

    def maybe_take(self, spec: SourceSpec, text: str) -> bool:
        if self.val_size <= 0 or len(self.rows) >= self.val_size:
            return False
        category = self.category_for(spec.group)
        if self.counts[category] >= self.quotas[category]:
            return False
        self.rows.append(text)
        self.counts[category] += 1
        return True


class ShardWriter:
    def __init__(
        self,
        output_dir: Path,
        shard_size: int,
        row_group_size: int,
        fixed_source_name: str | None = None,
        initial_idx: int = 0,
        existing_shards: list[dict[str, Any]] | None = None,
    ) -> None:
        self.output_dir = output_dir
        self.shard_size = shard_size
        self.row_group_size = row_group_size
        self.fixed_source_name = fixed_source_name
        self.rows: list[str] = []
        self.shards: list[dict[str, Any]] = list(existing_shards or [])
        self.shard_idx = initial_idx

    def add(self, text: str, source_name: str) -> None:
        self.rows.append(text)
        if len(self.rows) >= self.shard_size:
            self.flush(source_name=self.fixed_source_name or source_name)

    def flush(self, *, source_name: str = "mix", final: bool = False) -> None:
        if not self.rows:
            return
        filename = f"{self.shard_idx:05d}_{safe_name(source_name)}_train.parquet"
        path = self.output_dir / filename
        write_text_parquet(path, self.rows, self.row_group_size)
        entry = {"file": filename, "rows": len(self.rows), "final": final}
        self.shards.append(entry)
        print(f"  wrote {path} ({len(self.rows):,} rows)")
        self.rows = []
        self.shard_idx += 1


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def env_token(cli_token: str | None) -> str | None:
    return (
        cli_token
        or os.environ.get("HF_TOKEN")
        or os.environ.get("HUGGINGFACE_HUB_TOKEN")
        or os.environ.get("HF_READ_TOKEN")
    )


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\x00", " ").replace("\u00a0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(SPACE_RE.sub(" ", line).strip() for line in text.split("\n"))
    text = BLANK_LINES_RE.sub("\n\n\n", text)
    return text.strip()


def jsonish(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ""
        try:
            return json.dumps(json.loads(stripped), ensure_ascii=False, sort_keys=True)
        except Exception:
            return stripped
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def format_row(row: dict[str, Any], formatter: str) -> str:
    if formatter == "text":
        return row.get("text") or ""
    if formatter == "stackexchange_math":
        question = row.get("title_body") or row.get("title") or ""
        answer = row.get("upvoted_answer") or row.get("answer") or ""
        return f"Question:\n{question}\n\nAnswer:\n{answer}"
    if formatter == "xlam_function_calling":
        query = row.get("query") or ""
        tools = jsonish(row.get("tools"))
        answers = jsonish(row.get("answers"))
        return f"Query:\n{query}\n\nTools:\n{tools}\n\nTool calls:\n{answers}"
    if formatter == "agentic_messages":
        parts: list[str] = []
        tools = jsonish(row.get("tools"))
        if tools:
            parts.append(f"Tools:\n{tools}")
        reasoning = normalize_text(row.get("reasoning"))
        if reasoning:
            parts.append(f"Reasoning:\n{reasoning}")
        messages = row.get("messages") or row.get("conversation") or row.get("conversations")
        if isinstance(messages, str):
            try:
                messages = json.loads(messages)
            except Exception:
                messages = [{"role": "conversation", "content": messages}]
        if isinstance(messages, list):
            conv: list[str] = []
            for message in messages:
                if isinstance(message, dict):
                    role = normalize_text(message.get("role") or message.get("from") or "message")
                    content = normalize_text(message.get("content") or message.get("value") or "")
                    tool_calls = jsonish(message.get("tool_calls"))
                    line = f"{role}: {content}" if content else f"{role}:"
                    if tool_calls:
                        line = f"{line}\ntool_calls: {tool_calls}"
                    conv.append(line)
                else:
                    conv.append(normalize_text(message))
            if conv:
                parts.append("Conversation:\n" + "\n".join(conv))
        return "\n\n".join(parts)
    raise ValueError(f"Unknown formatter: {formatter}")


def write_text_parquet(path: Path, rows: list[str], row_group_size: int) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    table = pa.table({"text": rows})
    pq.write_table(table, tmp_path, row_group_size=row_group_size)
    os.replace(tmp_path, path)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def write_dataset_card(output_dir: Path, manifest: dict[str, Any]) -> None:
    source_lines = []
    for source in sorted(manifest["sources"].values(), key=lambda item: item["priority"]):
        target = "all" if source["target_tokens"] is None else f"{source['target_tokens']:,}"
        note = f" {source['note']}" if source.get("note") else ""
        source_lines.append(
            f"- `{source['name']}`: `{source['repo_id']}`"
            f" config=`{source['config']}` split=`{source['split']}` target={target}.{note}"
        )

    card = f"""---
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

The local nanochat dataloader sorts parquet files lexicographically, treats all but the final parquet as train, and uses the final parquet as validation. This repo therefore keeps `zzzz_val_mix.parquet` as the final file. The dataset card declares Hugging Face `train` and `validation` splits for viewer/download convenience.

## Build Summary

- Layout: `{manifest['layout']}`
- Train docs: {manifest['counts']['train_docs']:,}
- Train chars: {manifest['counts']['train_chars']:,}
- Train UTF-8 bytes: {manifest['counts']['train_bytes']:,}
- Validation docs: {manifest['counts']['val_docs']:,}
- Validation mix: `{json.dumps(manifest['counts']['val_counts'], ensure_ascii=False)}`
- Token count mode: `{manifest['args']['token_count_mode']}`

## Sources

{chr(10).join(source_lines)}

## Notes

- Generated artifacts include `manifest.json` for source-level counts and build settings.
- License is marked `other` because the mix combines multiple upstream datasets with different licenses and usage constraints.
- The agentic/tool-use sources are included only as raw pretraining text exposure; a separate Darija-focused SFT dataset should be built for conversational/tool-use behavior.
"""
    tmp_path = output_dir / "README.md.tmp"
    tmp_path.write_text(card, encoding="utf-8")
    os.replace(tmp_path, output_dir / "README.md")


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")[:80] or "source"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def progress_line(total_counts: Counter[str], start_time: float, source_name: str | None = None) -> str:
    elapsed = time.time() - start_time
    docs = int(total_counts["train_docs"])
    rate = docs / max(elapsed, 1e-6)
    gib = int(total_counts["train_bytes"]) / (1024 ** 3)
    prefix = f"{source_name}: " if source_name else ""
    return (
        f"  ...{prefix}train_docs={docs:,} val_docs={int(total_counts['val_docs']):,} "
        f"bytes={gib:.2f}GiB elapsed={format_duration(elapsed)} rate={rate:,.0f} docs/s"
    )


def default_output_dir() -> Path:
    base = os.environ.get(
        "NANOCHAT_BASE_DIR",
        os.path.join(os.path.expanduser("~"), ".cache", "nanochat"),
    )
    return Path(base) / "pretrain_mix_darija_english"


def build_sources(args: argparse.Namespace) -> list[SourceSpec]:
    budget = args.english_budget_tokens
    if budget > 0:
        general = int(budget * 0.70)
        code = int(budget * 0.05)
        math = int(budget * 0.05)
        agentic = int(budget * 0.05)
        remaining = max(0, budget - general - code - math - agentic)
        reasoning_algo = int(remaining * 0.20)
        reasoning_logic = int(remaining * 0.20)
        reasoning_econ = int(remaining * 0.10)
        reasoning_mcq = max(0, remaining - reasoning_algo - reasoning_logic - reasoning_econ)
        agentic_tool = int(agentic * 0.80)
        agentic_search = int(agentic * 0.15)
        agentic_xlam = max(1, int(agentic * 0.05)) if agentic > 0 else 0
        mathglm_budget = int(math * 0.25)
    else:
        general = code = math = None
        reasoning_algo = reasoning_logic = reasoning_econ = reasoning_mcq = None
        agentic_tool = agentic_search = agentic_xlam = None
        mathglm_budget = None

    arabic_budget = args.arabic_budget_tokens if args.arabic_budget_tokens > 0 else None
    if args.darija_budget_tokens > 0:
        darija_bilingual_budget = int(args.darija_budget_tokens * 0.20)
        darija_pure_budget = int(args.darija_budget_tokens * 0.30)
        darija_fineweb_budget = max(
            0,
            args.darija_budget_tokens - darija_bilingual_budget - darija_pure_budget,
        )
    else:
        darija_bilingual_budget = None
        darija_pure_budget = None
        darija_fineweb_budget = None

    sources = [
        SourceSpec(
            "eng_general_finepdfs_dclm_fwe",
            "english",
            FINEPDFS_SHUFFLED,
            None,
            "train",
            "text",
            general,
            priority=10,
            weight=70,
        ),
        SourceSpec(
            "eng_code_nemotron",
            "english",
            NEMOTRON_PRETRAIN,
            "Nemotron-Pretraining-Code-Concepts",
            "train",
            "text",
            code,
            priority=20,
            weight=5,
        ),
        SourceSpec(
            "eng_math_stackexchange",
            "english",
            STACKEXCHANGE_MATH,
            None,
            "titlebody_answer",
            "stackexchange_math",
            math,
            priority=22,
            weight=5,
            note="Use Q/A math. The arithmetic-only MathGLM corpus is disabled by default.",
        ),
        SourceSpec(
            "eng_reason_algorithmic",
            "english",
            NEMOTRON_PRETRAIN,
            "Nemotron-Pretraining-Unconditional-Algorithmic",
            "train",
            "text",
            reasoning_algo,
            priority=30,
            weight=2,
        ),
        SourceSpec(
            "eng_reason_formal_logic",
            "english",
            NEMOTRON_PRETRAIN,
            "Nemotron-Pretraining-Formal-Logic",
            "train",
            "text",
            reasoning_logic,
            priority=31,
            weight=2,
        ),
        SourceSpec(
            "eng_reason_economics",
            "english",
            NEMOTRON_PRETRAIN,
            "Nemotron-Pretraining-Economics",
            "train",
            "text",
            reasoning_econ,
            priority=32,
            weight=1,
        ),
        SourceSpec(
            "eng_reason_multiple_choice",
            "english",
            NEMOTRON_PRETRAIN,
            "Nemotron-Pretraining-Multiple-Choice",
            "train",
            "text",
            reasoning_mcq,
            priority=33,
            weight=8,
        ),
        SourceSpec(
            "agentic_nemotron_tool_calling",
            "english",
            NEMOTRON_AGENTIC,
            None,
            "tool_calling",
            "agentic_messages",
            agentic_tool,
            priority=40,
            weight=4,
            enabled=not args.skip_agentic,
            note="SFT-shaped tool-use data; kept small for base pretraining exposure.",
        ),
        SourceSpec(
            "agentic_nemotron_search",
            "english",
            NEMOTRON_AGENTIC,
            None,
            "search",
            "agentic_messages",
            agentic_search,
            priority=41,
            weight=1,
            enabled=not args.skip_agentic,
        ),
        SourceSpec(
            "agentic_xlam_function_calling",
            "english",
            XLAM_FUNCTION_CALLING,
            None,
            "train",
            "xlam_function_calling",
            agentic_xlam,
            priority=42,
            weight=1,
            enabled=not args.skip_agentic,
        ),
        SourceSpec(
            "arabic_raw",
            "arabic",
            LEGACY_DARIJA,
            "arabic_raw",
            "train",
            "text",
            arabic_budget,
            priority=60,
            weight=8,
        ),
        SourceSpec(
            "darija_bilingual",
            "darija",
            LEGACY_DARIJA,
            "bilingual",
            "train",
            "text",
            darija_bilingual_budget,
            priority=70,
            weight=6,
        ),
        SourceSpec(
            "darija_pure",
            "darija",
            LEGACY_DARIJA,
            "pure",
            "train",
            "text",
            darija_pure_budget,
            priority=80,
            weight=6,
        ),
        SourceSpec(
            "darija_fineweb_edu_clean",
            "darija",
            DARIJA_FINEWEB_CLEAN,
            None,
            "train",
            "text",
            darija_fineweb_budget,
            priority=85,
            weight=12,
        ),
        SourceSpec(
            "mathglm_numbers_disabled",
            "english",
            MATHGLM_NUMBERS,
            None,
            "train",
            "text",
            mathglm_budget,
            priority=23,
            weight=1,
            enabled=args.include_mathglm_numbers,
            note="Arithmetic-only equation traces. Disabled by default.",
        ),
    ]
    return [source for source in sources if source.enabled]


def synthetic_rows_for(spec: SourceSpec) -> Iterable[dict[str, Any]]:
    samples = {
        "text": [
            f"{spec.name}: The model should learn from compact text documents.",
            f"{spec.name}: Moroccan Darija and English are both represented in this shard test.",
            f"{spec.name}: This is a deterministic smoke-test document.",
        ],
        "stackexchange_math": [
            {
                "title_body": "How do I prove two vectors are orthogonal?",
                "upvoted_answer": "Compute the dot product and show that it is zero.",
            }
        ],
        "xlam_function_calling": [
            {
                "query": "Find the weather in Casablanca.",
                "tools": [{"name": "weather", "parameters": {"city": "string"}}],
                "answers": [{"name": "weather", "arguments": {"city": "Casablanca"}}],
            }
        ],
        "agentic_messages": [
            {
                "tools": [{"type": "function", "function": {"name": "search"}}],
                "messages": [
                    {"role": "user", "content": "Search for a short factual answer."},
                    {"role": "assistant", "content": "I will call search.", "tool_calls": [{"name": "search"}]},
                    {"role": "tool", "content": "Result: smoke test."},
                    {"role": "assistant", "content": "The result is smoke test."},
                ],
            }
        ],
    }
    rows = samples.get(spec.formatter, samples["text"])
    idx = 0
    while True:
        item = rows[idx % len(rows)]
        if isinstance(item, dict):
            row = dict(item)
        else:
            row = {"text": item}
        idx += 1
        yield row


def load_hf_rows(
    spec: SourceSpec,
    *,
    streaming: bool,
    cache_dir: str | None,
    token: str | None,
    retries: int,
) -> Iterable[dict[str, Any]]:
    from datasets import load_dataset

    # This repo is an old HF dataset-script dataset. datasets>=4 refuses to
    # execute scripts, and the packaged JSON loader infers an invalid schema on
    # this mixed JSONL. Stream/decode line-by-line instead.
    if spec.repo_id == STACKEXCHANGE_MATH:
        return iter_stackexchange_math_jsonl(spec.split, token)

    kwargs: dict[str, Any] = {
        "split": spec.split,
        "streaming": streaming,
        "cache_dir": cache_dir,
        "token": token,
    }
    for attempt in range(1, max(1, retries) + 1):
        try:
            if spec.config:
                return load_dataset(spec.repo_id, spec.config, **kwargs)
            return load_dataset(spec.repo_id, **kwargs)
        except Exception as exc:
            if attempt >= max(1, retries):
                raise RuntimeError(
                    f"Failed to load {spec.repo_id} config={spec.config} split={spec.split}"
                ) from exc
            wait_s = min(30, 2 ** attempt)
            print(
                f"[load retry {attempt}/{retries}] {spec.name} metadata/load failed; "
                f"retrying in {wait_s}s",
                file=sys.stderr,
            )
            time.sleep(wait_s)
    raise RuntimeError("unreachable")


def iter_stackexchange_math_jsonl(split: str, token: str | None) -> Iterable[dict[str, Any]]:
    from huggingface_hub import hf_hub_url
    import requests

    url = hf_hub_url(
        repo_id=STACKEXCHANGE_MATH,
        filename=f"{split}.jsonl.gz",
        repo_type="dataset",
    )
    headers = {"Authorization": f"Bearer {token}"} if token else None
    with requests.get(url, headers=headers, stream=True, timeout=60) as response:
        response.raise_for_status()
        response.raw.decode_content = True
        with gzip.GzipFile(fileobj=response.raw) as handle:
            for line_no, raw_line in enumerate(handle, 1):
                if not raw_line.strip():
                    continue
                try:
                    yield json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"Invalid JSON in {STACKEXCHANGE_MATH}/{split}.jsonl.gz line {line_no}"
                    ) from exc


def make_runtimes(
    args: argparse.Namespace,
    sources: list[SourceSpec],
    token: str | None,
    token_counter: TokenCounter,
) -> dict[str, SourceRuntime]:
    runtimes = {}
    for spec in sources:
        if args.smoke_test:
            rows = synthetic_rows_for(spec)
            max_docs = args.max_docs_per_source if args.max_docs_per_source > 0 else 12
        else:
            rows = load_hf_rows(
                spec,
                streaming=not args.no_streaming,
                cache_dir=args.cache_dir,
                token=token,
                retries=args.load_retries,
            )
            max_docs = args.max_docs_per_source
        runtimes[spec.name] = SourceRuntime(
            spec,
            rows,
            token_counter=token_counter,
            chars_per_token=args.chars_per_token,
            max_docs=max_docs,
            max_doc_chars=args.max_doc_chars,
        )
    return runtimes


def build_slot_cycle(runtimes: dict[str, SourceRuntime], rng: random.Random) -> list[str]:
    slots: list[str] = []
    for name, runtime in runtimes.items():
        slots.extend([name] * max(1, runtime.spec.weight))
    rng.shuffle(slots)
    return slots


def emit_text(
    runtime: SourceRuntime,
    text: str,
    *,
    writer: ShardWriter,
    val: ValidationMixer,
    total_counts: Counter[str],
) -> None:
    if val.maybe_take(runtime.spec, text):
        runtime.stats.docs_val += 1
        total_counts["val_docs"] += 1
        return
    writer.add(text, runtime.spec.name)
    runtime.stats.docs_train += 1
    total_counts["train_docs"] += 1
    total_counts["train_chars"] += len(text)
    total_counts["train_bytes"] += len(text.encode("utf-8"))


def run_curriculum(
    runtimes: dict[str, SourceRuntime],
    *,
    writer: ShardWriter,
    val: ValidationMixer,
    total_counts: Counter[str],
    progress_every: int,
    max_total_docs: int,
    completed_sources: set[str] | None = None,
) -> None:
    global_t0 = time.time()
    completed_sources = completed_sources or set()
    for runtime in sorted(runtimes.values(), key=lambda item: item.spec.priority):
        source_key = safe_name(runtime.spec.name)
        if source_key in completed_sources:
            runtime.stats.stopped_reason = "resumed_existing_complete"
            print(f"[source resume] {runtime.spec.name}: existing complete shards found; skipping")
            continue

        source_t0 = time.time()
        source_train_start = int(total_counts["train_docs"])
        print(
            f"[source] {runtime.spec.name} "
            f"repo={runtime.spec.repo_id} config={runtime.spec.config} split={runtime.spec.split}"
        )
        while True:
            if max_total_docs > 0 and total_counts["train_docs"] >= max_total_docs:
                runtime.stats.stopped_reason = "max_total_docs"
                return
            try:
                text = runtime.next_text()
            except StopIteration:
                if not runtime.stats.stopped_reason:
                    runtime.stats.stopped_reason = "exhausted"
                break
            emit_text(runtime, text, writer=writer, val=val, total_counts=total_counts)
            if (
                progress_every > 0
                and total_counts["train_docs"] > 0
                and total_counts["train_docs"] % progress_every == 0
            ):
                print(progress_line(total_counts, global_t0, runtime.spec.name))
        writer.flush(source_name=runtime.spec.name)
        source_docs = int(total_counts["train_docs"]) - source_train_start
        source_rate = source_docs / max(time.time() - source_t0, 1e-6)
        print(
            f"[source done] {runtime.spec.name}: {runtime.stats.stopped_reason} "
            f"docs={source_docs:,} elapsed={format_duration(time.time() - source_t0)} "
            f"rate={source_rate:,.0f} docs/s"
        )


def run_interleaved(
    runtimes: dict[str, SourceRuntime],
    *,
    writer: ShardWriter,
    val: ValidationMixer,
    total_counts: Counter[str],
    progress_every: int,
    max_total_docs: int,
    seed: int,
) -> None:
    rng = random.Random(seed)
    active = dict(runtimes)
    cycle = build_slot_cycle(active, rng)
    cycle_idx = 0
    global_t0 = time.time()
    print(f"[layout] interleaving {len(active)} sources")

    while active:
        if max_total_docs > 0 and total_counts["train_docs"] >= max_total_docs:
            for runtime in active.values():
                runtime.stats.stopped_reason = "max_total_docs"
            break
        if cycle_idx >= len(cycle):
            cycle = build_slot_cycle(active, rng)
            cycle_idx = 0
        name = cycle[cycle_idx]
        cycle_idx += 1
        runtime = active.get(name)
        if runtime is None:
            continue
        try:
            text = runtime.next_text()
        except StopIteration:
            if not runtime.stats.stopped_reason:
                runtime.stats.stopped_reason = "exhausted"
            print(f"[source done] {name}: {runtime.stats.stopped_reason}")
            active.pop(name, None)
            cycle = build_slot_cycle(active, rng) if active else []
            cycle_idx = 0
            continue
        emit_text(runtime, text, writer=writer, val=val, total_counts=total_counts)
        if (
            progress_every > 0
            and total_counts["train_docs"] > 0
            and total_counts["train_docs"] % progress_every == 0
        ):
            print(progress_line(total_counts, global_t0))


def prepare_output_dir(output_dir: Path, overwrite: bool, resume: bool) -> None:
    if overwrite and resume:
        raise SystemExit("--overwrite and --resume are mutually exclusive")
    if output_dir.exists() and any(output_dir.iterdir()):
        if resume:
            return
        if not overwrite:
            raise SystemExit(f"Output directory is not empty: {output_dir} (use --overwrite)")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)


def scan_existing_train_shards(output_dir: Path, shard_size: int) -> tuple[list[dict[str, Any]], set[str], int, int, int]:
    import pyarrow.parquet as pq

    entries: list[dict[str, Any]] = []
    source_last_rows: dict[str, int] = {}
    completed_sources: set[str] = set()
    total_rows = 0
    total_file_bytes = 0
    max_idx = -1
    pattern = re.compile(r"^(\d{5})_(.+)_train\.parquet$")

    for path in sorted(output_dir.glob("[0-9]*_train.parquet")):
        match = pattern.match(path.name)
        if not match:
            continue
        shard_idx = int(match.group(1))
        source_name = match.group(2)
        rows = pq.ParquetFile(path).metadata.num_rows
        total_rows += rows
        total_file_bytes += path.stat().st_size
        max_idx = max(max_idx, shard_idx)
        source_last_rows[source_name] = rows
        entries.append({
            "file": path.name,
            "rows": rows,
            "final": rows < shard_size,
            "resumed_existing": True,
        })

    for source_name, rows in source_last_rows.items():
        if source_name != "final" and rows < shard_size:
            completed_sources.add(source_name)

    return entries, completed_sources, total_rows, total_file_bytes, max_idx + 1


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build nanochat text-only pretraining parquet shards.")
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument("--layout", choices=["interleaved", "curriculum"], default="interleaved")
    p.add_argument("--english-budget-tokens", type=int, default=DEFAULT_ENGLISH_BUDGET_TOKENS,
                   help="Cap English-side data to this many tokens using the default bucket mix; <=0 means all.")
    p.add_argument("--arabic-budget-tokens", type=int, default=-1,
                   help="Cap Arabic raw data to this many tokens; <=0 means all.")
    p.add_argument("--darija-budget-tokens", type=int, default=-1,
                   help="Cap Darija data to this many tokens; <=0 means all.")
    p.add_argument("--chars-per-token", type=float, default=DEFAULT_CHARS_PER_TOKEN)
    p.add_argument("--tokenizer-dir", type=Path, default=None,
                   help="Optional nanochat tokenizer directory for exact token counting.")
    p.add_argument("--shard-size", type=int, default=DEFAULT_SHARD_SIZE)
    p.add_argument("--val-size", type=int, default=DEFAULT_VAL_SIZE)
    p.add_argument("--val-darija-frac", type=float, default=0.60)
    p.add_argument("--val-arabic-frac", type=float, default=0.20)
    p.add_argument("--max-doc-chars", type=int, default=50_000,
                   help="Truncate documents longer than this many chars; <=0 disables truncation.")
    p.add_argument("--max-docs-per-source", type=int, default=-1)
    p.add_argument("--max-total-docs", type=int, default=-1)
    p.add_argument("--skip-agentic", action="store_true",
                   help="Skip SFT-shaped agentic/function-calling sources for base pretraining.")
    p.add_argument("--include-mathglm-numbers", action="store_true",
                   help="Include jonathanasdf/MathGLM-dataset-5M arithmetic traces. Disabled by default.")
    p.add_argument("--cache-dir", default=None)
    p.add_argument("--no-streaming", action="store_true")
    p.add_argument("--hf-token", default=None)
    p.add_argument("--env-file", type=Path, default=Path(".env"))
    p.add_argument("--load-retries", type=int, default=5)
    p.add_argument("--progress-every", type=int, default=100_000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--resume", action="store_true",
                   help="Resume a curriculum build from existing completed source shards.")
    p.add_argument("--smoke-test", action="store_true",
                   help="Use local synthetic rows instead of Hugging Face datasets.")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir or default_output_dir()
    if args.resume and args.layout != "curriculum":
        raise SystemExit("--resume is only supported with --layout curriculum")
    prepare_output_dir(output_dir, args.overwrite, args.resume)

    load_dotenv(args.env_file)
    token = env_token(args.hf_token)
    if token:
        os.environ["HF_TOKEN"] = token
        os.environ["HUGGINGFACE_HUB_TOKEN"] = token
        print("Using HF token from " + ("--hf-token flag" if args.hf_token else "environment/.env"))
    elif not args.smoke_test:
        print("No HF token found; gated/private datasets may fail.", file=sys.stderr)

    sources = build_sources(args)
    token_counter = TokenCounter(args.tokenizer_dir)
    token_label = "exact tokens" if token_counter.exact else "approx tokens"
    print(f"Output: {output_dir}")
    print(f"Layout: {args.layout}")
    print(f"Token counting: {'exact via ' + str(args.tokenizer_dir) if token_counter.exact else 'approx chars/token'}")
    print(f"Sources: {len(sources)}")
    for source in sorted(sources, key=lambda item: item.priority):
        target = "all" if source.target_tokens is None else f"{source.target_tokens:,} {token_label}"
        print(f"  - {source.name}: {target}")

    runtimes = make_runtimes(args, sources, token, token_counter)
    existing_shards: list[dict[str, Any]] = []
    completed_sources: set[str] = set()
    existing_rows = 0
    existing_file_bytes = 0
    next_shard_idx = 0
    if args.resume:
        (
            existing_shards,
            completed_sources,
            existing_rows,
            existing_file_bytes,
            next_shard_idx,
        ) = scan_existing_train_shards(output_dir, args.shard_size)
        print(
            f"[resume] found {len(existing_shards)} train shards, "
            f"{existing_rows:,} rows, next shard index {next_shard_idx:05d}"
        )
        if completed_sources:
            print("[resume] completed sources:", ", ".join(sorted(completed_sources)))

    fixed_source_name = "mix" if args.layout == "interleaved" else None
    writer = ShardWriter(
        output_dir,
        args.shard_size,
        TRAIN_ROW_GROUP_SIZE,
        fixed_source_name,
        initial_idx=next_shard_idx,
        existing_shards=existing_shards,
    )
    val = ValidationMixer(args.val_size, args.val_darija_frac, args.val_arabic_frac)
    total_counts: Counter[str] = Counter()
    total_counts["train_docs"] = existing_rows
    total_counts["existing_file_bytes"] = existing_file_bytes
    t0 = time.time()

    if args.layout == "curriculum":
        run_curriculum(
            runtimes,
            writer=writer,
            val=val,
            total_counts=total_counts,
            progress_every=args.progress_every,
            max_total_docs=args.max_total_docs,
            completed_sources=completed_sources,
        )
    else:
        run_interleaved(
            runtimes,
            writer=writer,
            val=val,
            total_counts=total_counts,
            progress_every=args.progress_every,
            max_total_docs=args.max_total_docs,
            seed=args.seed,
        )

    writer.flush(source_name="final", final=True)

    if not val.rows:
        raise SystemExit("No validation rows collected; cannot make a nanochat-safe data directory.")
    val_path = output_dir / "zzzz_val_mix.parquet"
    write_text_parquet(val_path, val.rows, VAL_ROW_GROUP_SIZE)
    print(f"  wrote {val_path} ({len(val.rows):,} rows, val)")

    elapsed = time.time() - t0
    source_report = {}
    for name, runtime in runtimes.items():
        source_report[name] = {
            **asdict(runtime.spec),
            "stats": asdict(runtime.stats),
        }

    manifest = {
        "format": "nanochat_text_only_parquet",
        "created_unix": int(time.time()),
        "output_dir": str(output_dir),
        "layout": args.layout,
        "args": {
            "english_budget_tokens": args.english_budget_tokens,
            "arabic_budget_tokens": args.arabic_budget_tokens,
            "darija_budget_tokens": args.darija_budget_tokens,
            "chars_per_token": args.chars_per_token,
            "token_count_mode": "exact" if token_counter.exact else "approx_chars_per_token",
            "tokenizer_dir": str(args.tokenizer_dir) if args.tokenizer_dir else None,
            "resume": args.resume,
            "shard_size": args.shard_size,
            "val_size": args.val_size,
            "val_darija_frac": args.val_darija_frac,
            "val_arabic_frac": args.val_arabic_frac,
            "max_doc_chars": args.max_doc_chars,
            "skip_agentic": args.skip_agentic,
            "include_mathglm_numbers": args.include_mathglm_numbers,
            "smoke_test": args.smoke_test,
        },
        "counts": {
            "train_docs": int(total_counts["train_docs"]),
            "train_chars": int(total_counts["train_chars"]),
            "train_bytes": int(total_counts["train_bytes"]),
            "existing_file_bytes": int(total_counts["existing_file_bytes"]),
            "val_docs": len(val.rows),
            "val_counts": dict(val.counts),
            "elapsed_seconds": round(elapsed, 2),
        },
        "train_shards": writer.shards,
        "val_file": val_path.name,
        "sources": source_report,
        "note": "All parquet files contain exactly one column: text. Sorted final parquet is validation.",
    }
    write_json(output_dir / "manifest.json", manifest)
    write_dataset_card(output_dir, manifest)

    print("\nSummary")
    print(json.dumps(manifest["counts"], ensure_ascii=False, indent=2))
    print(f"Manifest: {output_dir / 'manifest.json'}")


if __name__ == "__main__":
    main()
