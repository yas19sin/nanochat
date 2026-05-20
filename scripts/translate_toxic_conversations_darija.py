"""
Translate mteb/toxic_conversations_50k to Moroccan Darija for classification SFT.

The script writes one JSONL file per split with both the original English text
and the Darija translation, preserving toxicity labels exactly. It can resume by
reading already-written src_idx values from the output files.

Example:
    export VLLM_URLS=http://localhost:8000/v1,http://localhost:8001/v1
    export VLLM_MODEL=gemma4-31b-awq
    export HF_TOKEN=...
    export HF_WRITE_TOKEN=...

    python -m scripts.translate_toxic_conversations_darija \
      --output-dir /workspace/nanochat-cache/toxic_conversations_darija \
      --repo-id Lyte/darija-toxic-conversations-50k \
      --workers 32
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx
from datasets import load_dataset
from openai import OpenAI


DEFAULT_DATASET = "mteb/toxic_conversations_50k"
DEFAULT_MODEL = os.environ.get("VLLM_MODEL", "gemma4-31b-awq")
DEFAULT_ENDPOINTS = "http://localhost:8000/v1"

SYSTEM_PROMPT = (
    "You translate dataset examples for safety classification research. "
    "Translate the user's English text into Moroccan Darija written mostly in Arabic script. "
    "Preserve the meaning, tone, toxicity, insults, profanity, threats, and harassment if present. "
    "Do not censor, soften, explain, label, summarize, or answer the text. "
    "Output only the Moroccan Darija translation."
)

PREFIX_RE = re.compile(
    r"^\s*(?:moroccan\s+darija|darija|translation|translated\s+text|"
    r"الترجمة|الدارجة|الترجمة\s+بالدارجة)\s*[:：-]\s*",
    re.IGNORECASE,
)

LABEL_MAP = {
    "0": "non-toxic",
    "false": "non-toxic",
    "non-toxic": "non-toxic",
    "nontoxic": "non-toxic",
    "not-toxic": "non-toxic",
    "not toxic": "non-toxic",
    "1": "toxic",
    "true": "toxic",
    "toxic": "toxic",
}


def normalize_label(row: dict[str, Any]) -> tuple[int, str]:
    raw = row.get("label_text", row.get("label"))
    if isinstance(raw, bool):
        label = int(raw)
        return label, "toxic" if label else "non-toxic"
    if isinstance(raw, int):
        label = 1 if raw == 1 else 0
        return label, "toxic" if label else "non-toxic"

    text = str(raw).strip().lower().replace("_", "-")
    if text in LABEL_MAP:
        label_text = LABEL_MAP[text]
        return (1 if label_text == "toxic" else 0), label_text
    raise ValueError(f"Unsupported label value: {raw!r}")


def clean_translation(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:\w+)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    text = PREFIX_RE.sub("", text).strip()
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1].strip()
    return text


def make_client(endpoint: str, api_key: str) -> OpenAI:
    return OpenAI(
        base_url=endpoint,
        api_key=api_key,
        http_client=httpx.Client(timeout=httpx.Timeout(300.0)),
    )


def translate_text(
    client: OpenAI,
    model: str,
    text: str,
    max_tokens: int,
    temperature: float,
    top_p: float,
    top_k: int,
    repetition_penalty: float,
    retries: int,
) -> str:
    if not text or not text.strip():
        return text
    last_error = None
    for attempt in range(retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                extra_body={
                    "top_k": top_k,
                    "repetition_penalty": repetition_penalty,
                },
            )
            return clean_translation(resp.choices[0].message.content or "")
        except Exception as exc:  # pragma: no cover - depends on remote endpoint
            last_error = exc
            print(f"    [retry {attempt + 1}/{retries}] {exc}")
            if attempt + 1 < retries:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"translation failed after {retries} retries: {last_error}")


def translated_record(
    client: OpenAI,
    args: argparse.Namespace,
    split: str,
    src_idx: int,
    row: dict[str, Any],
) -> dict[str, Any]:
    text_en = str(row.get("text") or "").strip()
    label, label_text = normalize_label(row)
    text_darija = translate_text(
        client=client,
        model=args.model,
        text=text_en,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        top_p=args.top_p,
        top_k=args.top_k,
        repetition_penalty=args.repetition_penalty,
        retries=args.retries,
    )
    return {
        "source_dataset": args.dataset,
        "split": split,
        "src_idx": src_idx,
        "text_en": text_en,
        "text_darija": text_darija,
        "text": text_darija,
        "label": label,
        "label_text": label_text,
        "translation_model": args.model,
    }


def read_done_indices(path: Path) -> set[int]:
    done = set()
    if not path.exists():
        return done
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "src_idx" in row:
                done.add(int(row["src_idx"]))
    return done


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_readme(output_dir: Path, repo_id: str | None) -> None:
    pretty_name = "Darija Toxic Conversations 50K"
    title = repo_id or pretty_name
    readme = f"""---
language:
- ary
- ar
license: other
pretty_name: {pretty_name}
task_categories:
- text-classification
tags:
- darija
- moroccan-arabic
- toxicity-classification
- safety
- translated
- private
configs:
- config_name: default
  data_files:
  - split: train
    path: train.jsonl
  - split: test
    path: test.jsonl
---

# {title}

Moroccan Darija translation of `mteb/toxic_conversations_50k` for toxicity
classification fine-tuning. Each row preserves the original English text,
translated Darija text, integer `label`, and string `label_text`.

Columns:

- `text_en`: original English text.
- `text_darija`: Moroccan Darija translation.
- `text`: alias of `text_darija` for simple dataset loading.
- `label`: `0` for non-toxic, `1` for toxic.
- `label_text`: `non-toxic` or `toxic`.
- `split`, `src_idx`, `source_dataset`, `translation_model`.

This dataset intentionally contains offensive and toxic text because it is built
for safety classification research. It should not be used as general chat SFT
data.
"""
    (output_dir / "README.md").write_text(readme, encoding="utf-8")


def summarize_outputs(output_dir: Path, splits: list[str]) -> dict[str, Any]:
    summary: dict[str, Any] = {"splits": {}}
    total = 0
    labels = Counter()
    for split in splits:
        path = output_dir / f"{split}.jsonl"
        split_labels = Counter()
        rows = 0
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    row = json.loads(line)
                    rows += 1
                    split_labels[row.get("label_text", "unknown")] += 1
        summary["splits"][split] = {
            "rows": rows,
            "labels": dict(split_labels),
        }
        total += rows
        labels.update(split_labels)
    summary["rows"] = total
    summary["labels"] = dict(labels)
    return summary


def upload_folder(output_dir: Path, repo_id: str, private: bool) -> None:
    token = os.environ.get("HF_WRITE_TOKEN") or os.environ.get("HF_TOKEN")
    if not token:
        raise RuntimeError("Set HF_WRITE_TOKEN or HF_TOKEN to upload to Hugging Face.")
    from huggingface_hub import HfApi

    api = HfApi(token=token)
    api.create_repo(repo_id=repo_id, repo_type="dataset", private=private, exist_ok=True)
    api.upload_folder(
        folder_path=str(output_dir),
        repo_id=repo_id,
        repo_type="dataset",
    )
    print(f"[upload] {output_dir} -> {repo_id}")


def process_split(args: argparse.Namespace, split: str, endpoints: list[str]) -> None:
    kwargs = {"split": split}
    token = os.environ.get("HF_TOKEN")
    if token:
        kwargs["token"] = token
    ds = load_dataset(args.dataset, **kwargs)
    if args.limit and args.limit > 0:
        total = min(len(ds), args.limit)
    else:
        total = len(ds)

    output_path = args.output_dir / f"{split}.jsonl"
    done = read_done_indices(output_path)
    pending = [idx for idx in range(total) if idx not in done]
    print(f"[{split}] total={total:,} done={len(done):,} pending={len(pending):,}")

    if args.dry_run:
        for idx in pending[:5]:
            row = ds[idx]
            print({"src_idx": idx, "text": row.get("text", "")[:160], "label": row.get("label"), "label_text": row.get("label_text")})
        return

    clients = [
        make_client(endpoints[i % len(endpoints)], args.api_key)
        for i in range(args.workers)
    ]
    started = time.time()
    processed_since_upload = 0

    for batch_start in range(0, len(pending), args.workers):
        batch_indices = pending[batch_start:batch_start + args.workers]
        results = {}
        with ThreadPoolExecutor(max_workers=len(batch_indices)) as pool:
            futures = {}
            for offset, src_idx in enumerate(batch_indices):
                client = clients[offset % len(clients)]
                futures[pool.submit(translated_record, client, args, split, src_idx, ds[src_idx])] = src_idx
            for future in as_completed(futures):
                src_idx = futures[future]
                try:
                    results[src_idx] = future.result()
                except Exception as exc:
                    print(f"  [{split}:{src_idx}] failed: {exc}")
                    row = ds[src_idx]
                    label, label_text = normalize_label(row)
                    results[src_idx] = {
                        "source_dataset": args.dataset,
                        "split": split,
                        "src_idx": src_idx,
                        "text_en": str(row.get("text") or "").strip(),
                        "text_darija": "",
                        "text": "",
                        "label": label,
                        "label_text": label_text,
                        "translation_model": args.model,
                        "error": str(exc),
                    }

        ordered = [results[idx] for idx in batch_indices]
        write_jsonl(output_path, ordered)

        done_now = batch_start + len(batch_indices)
        elapsed = max(time.time() - started, 1e-6)
        rate = done_now / elapsed
        eta = (len(pending) - done_now) / max(rate, 1e-6) / 60
        print(f"  [{split}] {done_now:,}/{len(pending):,} translated | {rate:.2f} rows/s | eta {eta:.1f}m")

        processed_since_upload += len(batch_indices)
        if args.repo_id and args.upload_every > 0 and processed_since_upload >= args.upload_every:
            write_readme(args.output_dir, args.repo_id)
            summary = summarize_outputs(args.output_dir, args.splits)
            (args.output_dir / "manifest.json").write_text(
                json.dumps(summary, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            upload_folder(args.output_dir, args.repo_id, args.private)
            processed_since_upload = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--splits", nargs="+", default=["train", "test"])
    parser.add_argument("--output-dir", type=Path, default=Path("dev-ignore/toxic_conversations_darija"))
    parser.add_argument("--repo-id", default=None, help="Optional HF dataset repo to upload.")
    parser.add_argument("--private", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--limit", type=int, default=-1, help="Limit rows per split for smoke tests.")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--endpoints", default=os.environ.get("VLLM_URLS", DEFAULT_ENDPOINTS))
    parser.add_argument("--api-key", default=os.environ.get("VLLM_API_KEY", "EMPTY"))
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--top-p", type=float, default=0.9)
    parser.add_argument("--top-k", type=int, default=300)
    parser.add_argument("--repetition-penalty", type=float, default=1.1)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--upload-every", type=int, default=0, help="Upload every N new rows; 0 disables periodic upload.")
    parser.add_argument("--no-upload-final", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    endpoints = [ep.strip() for ep in args.endpoints.split(",") if ep.strip()]
    if not endpoints:
        raise ValueError("No vLLM endpoints configured.")

    if args.overwrite:
        for split in args.splits:
            path = args.output_dir / f"{split}.jsonl"
            if path.exists():
                path.unlink()

    print(f"Dataset: {args.dataset}")
    print(f"Model: {args.model}")
    print(f"Endpoints: {len(endpoints)}")
    print(f"Output: {args.output_dir}")

    for split in args.splits:
        process_split(args, split, endpoints)

    write_readme(args.output_dir, args.repo_id)
    summary = summarize_outputs(args.output_dir, args.splits)
    manifest = {
        "settings": {
            "dataset": args.dataset,
            "splits": args.splits,
            "model": args.model,
            "temperature": args.temperature,
            "top_p": args.top_p,
            "top_k": args.top_k,
            "repetition_penalty": args.repetition_penalty,
        },
        **summary,
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps(manifest, indent=2, ensure_ascii=False))

    if args.repo_id and not args.no_upload_final and not args.dry_run:
        upload_folder(args.output_dir, args.repo_id, args.private)


if __name__ == "__main__":
    main()
