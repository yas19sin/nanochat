"""
Translate nvidia/Nemotron-Research-GooseReason-0.7M to Moroccan Darija
using a vLLM-served model via OpenAI-compatible API.

Translates: question, options (answer letter kept as-is).
Supports resuming via checkpoint file. Uses concurrent requests.

Usage:
    python scripts/translate_nemotron.py --limit 100         # pilot batch
    python scripts/translate_nemotron.py --limit 100 --split math
    python scripts/translate_nemotron.py --dry-run
    python scripts/translate_nemotron.py --workers 20        # control concurrency
"""

import json
import os
import time
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI
import httpx
from datasets import load_dataset

# ---------------------------------------------------------------------------
# Config

# Multi-endpoint: 4 vLLM instances across 4 GPUs, round-robin distribution
_DEFAULT_ENDPOINTS = [
    "https://shops-piece-lists-constraints.trycloudflare.com/v1",
    "https://organisation-twist-doors-tent.trycloudflare.com/v1",
    "https://exhibitions-congress-created-grammar.trycloudflare.com/v1",
    "https://tier-gbp-radio-ppc.trycloudflare.com/v1",
]
ENDPOINTS = os.environ.get(
    "VLLM_URLS", ",".join(_DEFAULT_ENDPOINTS)).split(",")
API_KEY = os.environ.get("VLLM_API_KEY", "EMPTY")
MODEL_NAME = "gemma4-31b-awq"

SYSTEM_PROMPT = (
    "Translate the following English text into Moroccan Darija (الدارجة المغربية). "
    "Output ONLY the Darija translation."
)

# Per-split generation params. HF Space params beat per-split judge params
# on code (+15% Darija) and tied on math/stem. top_p=0.98 helps the model
# commit to Darija more often, especially on technical text.
_DEFAULT_PARAMS = dict(
    max_tokens=512,
    temperature=0.3,
    top_p=0.98,
    extra_body={"top_k": 300, "repetition_penalty": 1.0},
)
SPLIT_PARAMS = {
    "math": _DEFAULT_PARAMS,
    "code": _DEFAULT_PARAMS,
    "stem": _DEFAULT_PARAMS,
}

MAX_RETRIES = 3
RETRY_DELAY = 5

SPLITS = ["math", "code", "stem"]

# ---------------------------------------------------------------------------
# Helpers


def make_client(endpoint: str) -> OpenAI:
    """Create OpenAI client for a single vLLM endpoint."""
    return OpenAI(
        base_url=endpoint,
        api_key=API_KEY,
        http_client=httpx.Client(timeout=httpx.Timeout(300.0)),
    )


def make_clients(n: int) -> list[OpenAI]:
    """Create n clients, round-robin across all endpoints."""
    return [make_client(ENDPOINTS[i % len(ENDPOINTS)]) for i in range(n)]


def translate_text(client: OpenAI, text: str, split: str) -> str:
    """Translate a single text from English to Darija."""
    if not text or not text.strip():
        return text

    params = SPLIT_PARAMS[split]

    for attempt in range(MAX_RETRIES):
        try:
            resp = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                **params,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            print(f"    [retry {attempt+1}/{MAX_RETRIES}] {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))

    print(f"    [FAILED] Keeping original text")
    return text


def is_still_english(text: str) -> bool:
    """Check if text is mostly ASCII (i.e. not translated to Darija)."""
    alpha = [ch for ch in text if ch.isalpha()]
    if not alpha:
        return False
    ascii_alpha = sum(1 for ch in alpha if ch.isascii())
    return ascii_alpha / len(alpha) > 0.6


def is_formula_heavy(text: str) -> bool:
    """Check if text is mostly math formulas (not worth retrying)."""
    if not text:
        return False
    alpha = [ch for ch in text if ch.isalpha()]
    # If less than 30% of chars are letters, it's mostly formulas
    return len(alpha) / max(len(text), 1) < 0.3


def translate_record(client: OpenAI, record: dict, split: str) -> dict:
    """Translate a single dataset record (question + options, keep answer).
    Translates all texts concurrently within the record.
    """
    texts = [record["question"]] + list(record["options"])
    results = [None] * len(texts)

    with ThreadPoolExecutor(max_workers=len(texts)) as pool:
        futures = {
            pool.submit(translate_text, client, text, split): i
            for i, text in enumerate(texts)
        }
        for future in as_completed(futures):
            idx = futures[future]
            results[idx] = future.result()

    return {
        "question": results[0],
        "options": results[1:],
        "answer": record["answer"],
    }


def check_endpoints() -> bool:
    """Verify all endpoints are running."""
    all_ok = True
    for ep in ENDPOINTS:
        try:
            c = make_client(ep)
            models = c.models.list()
            print(f"  OK: {ep} → {models.data[0].id}")
        except Exception as e:
            print(f"  FAIL: {ep} → {e}")
            all_ok = False
    return all_ok


# ---------------------------------------------------------------------------
# Main


def main():
    parser = argparse.ArgumentParser(
        description="Translate Nemotron-GooseReason to Darija")
    parser.add_argument("--output", default="dev-ignore/nemotron_darija.jsonl",
                        help="Output JSONL file")
    parser.add_argument("--checkpoint", default="dev-ignore/.nemotron_checkpoint.json",
                        help="Checkpoint file for resume")
    parser.add_argument("--split", default="all", choices=SPLITS + ["all"],
                        help="Which split to translate (default: all)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max records to translate (across all splits)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without calling API")
    parser.add_argument("--workers", type=int, default=16,
                        help="Number of records to translate concurrently (default: 16)")
    args = parser.parse_args()

    splits = SPLITS if args.split == "all" else [args.split]

    # Load checkpoint
    start_idx = 0
    if os.path.exists(args.checkpoint):
        with open(args.checkpoint, "r", encoding="utf-8") as f:
            ckpt = json.load(f)
        start_idx = ckpt.get("next_idx", 0)
        print(f"Resuming from record {start_idx}")

    # Load dataset records (streaming, round-robin across splits)
    print(f"Loading dataset splits: {splits}...")
    records = []
    if len(splits) == 1 or not args.limit:
        # Single split or no limit: sequential loading
        for split in splits:
            ds = load_dataset(
                "nvidia/Nemotron-Research-GooseReason-0.7M",
                split=split, streaming=True,
            )
            for rec in ds:
                records.append({"split": split, **rec})
                if args.limit and len(records) >= args.limit:
                    break
            if args.limit and len(records) >= args.limit:
                break
    else:
        # Multiple splits with limit: round-robin for even sampling
        per_split = args.limit // len(splits)
        remainder = args.limit % len(splits)
        for i, split in enumerate(splits):
            n = per_split + (1 if i < remainder else 0)
            ds = load_dataset(
                "nvidia/Nemotron-Research-GooseReason-0.7M",
                split=split, streaming=True,
            )
            for j, rec in enumerate(ds):
                if j >= n:
                    break
                records.append({"split": split, **rec})

    total = len(records)
    print(f"Loaded {total} records")

    if start_idx >= total:
        print("All records already translated!")
        return

    remaining = total - start_idx
    if args.dry_run:
        print(f"\n[DRY RUN] Would translate {remaining} records")
        for i in range(start_idx, min(start_idx + 3, total)):
            r = records[i]
            print(f"\n  Record {i} [{r['split']}]:")
            print(f"    Q: {r['question'][:120]}...")
            print(f"    Options: {len(r['options'])}, Answer: {r['answer']}")
        return

    # Setup clients (round-robin across endpoints)
    clients = make_clients(args.workers)
    print(f"Checking {len(ENDPOINTS)} endpoints...")
    if not check_endpoints():
        print("ERROR: One or more endpoints not reachable.")
        return

    # Ensure output dir exists
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)

    t_start = time.time()

    # Process records in batches of --workers
    for batch_start in range(start_idx, total, args.workers):
        batch_end = min(batch_start + args.workers, total)
        batch = list(range(batch_start, batch_end))

        # Translate batch concurrently
        batch_results = {}
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            futures = {}
            for i, idx in enumerate(batch):
                rec = records[idx]
                cl = clients[i % args.workers]
                fut = pool.submit(translate_record, cl, rec, rec["split"])
                futures[fut] = idx

            for future in as_completed(futures):
                idx = futures[future]
                batch_results[idx] = future.result()

        # Write results in order, update checkpoint after each
        for idx in batch:
            rec = records[idx]
            translated = batch_results[idx]
            translated["split"] = rec["split"]

            # Append to output
            mode = "a" if idx > 0 else "w"
            with open(args.output, mode, encoding="utf-8") as f:
                f.write(json.dumps(translated, ensure_ascii=False) + "\n")

            # Update checkpoint
            with open(args.checkpoint, "w", encoding="utf-8") as f:
                json.dump({"next_idx": idx + 1, "total": total}, f)

        # Progress
        elapsed = time.time() - t_start
        done = batch_end - start_idx
        rate = done / max(elapsed, 1)
        eta = (total - batch_end) / rate / 60 if rate > 0 else 0
        print(f"  [{done}/{remaining}] {rate:.1f} rec/s, "
              f"~{eta:.1f}m left")

    elapsed = time.time() - t_start
    print(
        f"\nFinished! {total - start_idx} records in {elapsed/60:.1f} minutes")
    print(f"Output: {args.output}")


if __name__ == "__main__":
    main()
