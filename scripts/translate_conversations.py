"""
Translate conversation datasets (SmolTalk, Alpaca) to Moroccan Darija
using multiple vLLM instances via OpenAI-compatible API.

Round-robin across endpoints for max throughput on multi-GPU setups.
Supports resuming via checkpoint file.

Usage:
    python scripts/translate_conversations.py --dataset smoltalk --limit 100000
    python scripts/translate_conversations.py --dataset alpaca --limit 52000
    python scripts/translate_conversations.py --dataset smoltalk --limit 50 --dry-run
    python scripts/translate_conversations.py --workers 32
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

# Multi-endpoint: one vLLM instance per GPU, round-robin distribution
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
HF_TOKEN = os.environ["HF_TOKEN"]

SYSTEM_PROMPT = (
    "Translate the following English text into Moroccan Darija (الدارجة المغربية). "
    "Output ONLY the Darija translation."
)

GEN_PARAMS = dict(
    max_tokens=512,
    temperature=0.3,
    top_p=0.98,
    extra_body={"top_k": 300, "repetition_penalty": 1.0},
)

MAX_RETRIES = 3
RETRY_DELAY = 5

# ---------------------------------------------------------------------------
# Helpers


def make_client(endpoint: str) -> OpenAI:
    return OpenAI(
        base_url=endpoint,
        api_key=API_KEY,
        http_client=httpx.Client(timeout=httpx.Timeout(300.0)),
    )


def make_clients(n: int) -> list[OpenAI]:
    """Create n clients, round-robin across all endpoints."""
    return [make_client(ENDPOINTS[i % len(ENDPOINTS)]) for i in range(n)]


def translate_text(client: OpenAI, text: str) -> str:
    if not text or not text.strip():
        return text
    for attempt in range(MAX_RETRIES):
        try:
            resp = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                **GEN_PARAMS,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            print(f"    [retry {attempt+1}/{MAX_RETRIES}] {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
    return text


def translate_conversation(client: OpenAI, sample: dict) -> dict:
    """Translate all messages in a conversation concurrently."""
    messages = sample["messages"]
    results = [None] * len(messages)

    with ThreadPoolExecutor(max_workers=len(messages)) as pool:
        futures = {
            pool.submit(translate_text, client, msg["content"]): i
            for i, msg in enumerate(messages)
        }
        for future in as_completed(futures):
            idx = futures[future]
            results[idx] = future.result()

    return {
        "source": sample["source"],
        "messages": [
            {"role": messages[i]["role"], "content": results[i]}
            for i in range(len(messages))
        ],
    }


def check_endpoints() -> bool:
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
# Dataset loaders


def load_smoltalk(limit: int) -> list[dict]:
    print(f"Loading SmolTalk/everyday-conversations (limit {limit})...")
    ds = load_dataset("HuggingFaceTB/smoltalk", "everyday-conversations",
                      split="train", streaming=True, token=HF_TOKEN)
    samples = []
    for row in ds:
        msgs = row.get("messages", [])
        if len(msgs) > 6:
            continue
        if any(len(m.get("content", "")) > 800 for m in msgs):
            continue
        samples.append({
            "source": "smoltalk",
            "messages": [{"role": m["role"], "content": m["content"]} for m in msgs],
        })
        if len(samples) >= limit:
            break
    print(f"  Loaded {len(samples)} SmolTalk conversations")
    return samples


def load_alpaca(limit: int) -> list[dict]:
    print(f"Loading yahma/alpaca-cleaned (limit {limit})...")
    ds = load_dataset("yahma/alpaca-cleaned", split="train", streaming=True,
                      token=HF_TOKEN)
    samples = []
    for row in ds:
        inst = row.get("instruction", "")
        inp = row.get("input", "")
        out = row.get("output", "")
        if len(out) > 800 or len(inst) > 500:
            continue
        user_text = f"{inst}\n{inp}" if inp else inst
        samples.append({
            "source": "alpaca",
            "messages": [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": out},
            ],
        })
        if len(samples) >= limit:
            break
    print(f"  Loaded {len(samples)} Alpaca samples")
    return samples


LOADERS = {
    "smoltalk": load_smoltalk,
    "alpaca": load_alpaca,
}

# ---------------------------------------------------------------------------
# Main


def main():
    parser = argparse.ArgumentParser(
        description="Translate conversation datasets to Darija")
    parser.add_argument("--dataset", required=True, choices=list(LOADERS.keys()),
                        help="Dataset to translate")
    parser.add_argument("--output", default=None,
                        help="Output JSONL (default: dev-ignore/{dataset}_darija.jsonl)")
    parser.add_argument("--checkpoint", default=None,
                        help="Checkpoint file (default: dev-ignore/.{dataset}_checkpoint.json)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max records to translate")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, default=32,
                        help="Concurrent conversations (default: 32)")
    args = parser.parse_args()

    output = args.output or f"dev-ignore/{args.dataset}_darija.jsonl"
    checkpoint = args.checkpoint or f"dev-ignore/.{args.dataset}_checkpoint.json"

    # Load checkpoint
    start_idx = 0
    if os.path.exists(checkpoint):
        with open(checkpoint, "r", encoding="utf-8") as f:
            ckpt = json.load(f)
        start_idx = ckpt.get("next_idx", 0)
        print(f"Resuming from record {start_idx}")

    # Load dataset
    limit = args.limit or 999_999_999
    records = LOADERS[args.dataset](limit)
    total = len(records)
    print(f"Total: {total} records")

    if start_idx >= total:
        print("All records already translated!")
        return

    remaining = total - start_idx
    total_msgs = sum(len(r["messages"]) for r in records[start_idx:])

    if args.dry_run:
        print(
            f"\n[DRY RUN] Would translate {remaining} records ({total_msgs} messages)")
        for r in records[start_idx:start_idx + 5]:
            preview = r["messages"][0]["content"][:100]
            print(f"  [{r['source']}] {len(r['messages'])} msgs: {preview}...")
        return

    # Setup clients
    clients = make_clients(args.workers)
    print(f"\nChecking {len(ENDPOINTS)} endpoints...")
    if not check_endpoints():
        print("ERROR: One or more endpoints not reachable.")
        return

    print(f"\nTranslating {remaining} conversations ({total_msgs} messages) "
          f"with {args.workers} workers across {len(ENDPOINTS)} GPUs\n")

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    t_start = time.time()

    # Process in batches
    for batch_start in range(start_idx, total, args.workers):
        batch_end = min(batch_start + args.workers, total)
        batch = list(range(batch_start, batch_end))

        batch_results = {}
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            futures = {}
            for i, idx in enumerate(batch):
                cl = clients[i % args.workers]
                fut = pool.submit(translate_conversation, cl, records[idx])
                futures[fut] = idx

            for future in as_completed(futures):
                idx = futures[future]
                try:
                    batch_results[idx] = future.result()
                except Exception as e:
                    print(f"  [{idx}] FAILED: {e}")
                    batch_results[idx] = {
                        "source": records[idx]["source"],
                        "messages": records[idx]["messages"],
                        "error": str(e),
                    }

        # Write in order + checkpoint
        for idx in batch:
            mode = "a" if idx > 0 else "w"
            with open(output, mode, encoding="utf-8") as f:
                f.write(json.dumps(
                    batch_results[idx], ensure_ascii=False) + "\n")
            with open(checkpoint, "w", encoding="utf-8") as f:
                json.dump({"next_idx": idx + 1, "total": total}, f)

        # Progress
        elapsed = time.time() - t_start
        done = batch_end - start_idx
        rate = done / max(elapsed, 1)
        eta = (total - batch_end) / rate / 60 if rate > 0 else 0
        print(f"  [{done}/{remaining}] {rate:.1f} rec/s, ~{eta:.1f}m left")

    elapsed = time.time() - t_start
    print(f"\nDone! {remaining} records in {elapsed/60:.1f} minutes")
    print(f"Output: {output}")


if __name__ == "__main__":
    main()
