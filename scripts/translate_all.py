#!/usr/bin/env python3
"""
On-device Darija translation pipeline for Vast.ai.
Translates Nemotron, Alpaca, SmolTalk → Moroccan Darija via local vLLM instances.
Periodic HuggingFace uploads to prevent data loss.

Run on the Vast.ai machine directly (localhost endpoints, no DNS issues).

Usage:
    # Install deps first:
    pip install openai httpx datasets huggingface_hub

    # Translate all datasets:
    python translate_all.py

    # Single dataset:
    python translate_all.py --dataset nemotron
    python translate_all.py --dataset alpaca
    python translate_all.py --dataset smoltalk

    # Resume after crash (automatic via checkpoints):
    python translate_all.py

    # Custom settings:
    python translate_all.py --workers 48 --upload-every 2000
"""

import json
import os
import time
import argparse
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI
import httpx
from datasets import load_dataset

# ---------------------------------------------------------------------------
# Config

ENDPOINTS = [f"http://localhost:{p}/v1" for p in range(8000, 8004)]
API_KEY = "EMPTY"
MODEL_NAME = "gemma4-31b-awq"
HF_READ_TOKEN = os.environ["HF_TOKEN"]
HF_WRITE_TOKEN = os.environ["HF_WRITE_TOKEN"]
HF_REPO = None  # Auto-detected from token username

SYSTEM_PROMPT = (
    "Translate the following English text into Moroccan Darija (الدارجة المغربية). "
    "Output ONLY the Darija translation."
)

GEN_PARAMS = dict(
    max_tokens=4096,
    temperature=0.3,
    top_p=0.95,
    extra_body={"top_k": 64, "repetition_penalty": 1.0},
)

MAX_RETRIES = 3
RETRY_DELAY = 5
UPLOAD_EVERY = 250  # Upload to HF every N records

OUTPUT_DIR = Path("output")
CHECKPOINT_DIR = Path("checkpoints")

# ---------------------------------------------------------------------------
# Client helpers


def make_client(endpoint: str) -> OpenAI:
    return OpenAI(
        base_url=endpoint,
        api_key=API_KEY,
        http_client=httpx.Client(timeout=httpx.Timeout(300.0)),
    )


def make_clients(n: int) -> list[OpenAI]:
    return [make_client(ENDPOINTS[i % len(ENDPOINTS)]) for i in range(n)]


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


# ---------------------------------------------------------------------------
# HuggingFace upload


def get_hf_repo(override: str | None = None) -> str:
    """Get or auto-detect HF repo name from token username."""
    if override:
        return override
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=HF_WRITE_TOKEN)
        user = api.whoami()["name"]
        return f"{user}/darija-translation-data"
    except Exception as e:
        print(f"  Could not detect HF username: {e}")
        return "darija-translation-data"


def upload_to_hf(filepath: Path, repo_id: str):
    """Upload a file to HuggingFace Hub."""
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=HF_WRITE_TOKEN)
        api.upload_file(
            path_or_fileobj=str(filepath),
            path_in_repo=filepath.name,
            repo_id=repo_id,
            repo_type="dataset",
        )
        print(f"  ☁ Uploaded {filepath.name} to {repo_id}")
    except Exception as e:
        print(f"  ☁ Upload failed: {e}")


def ensure_hf_repo(repo_id: str):
    """Create HF dataset repo if it doesn't exist."""
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=HF_WRITE_TOKEN)
        api.create_repo(repo_id=repo_id, repo_type="dataset",
                        exist_ok=True, private=True)
        print(f"  HF repo ready: {repo_id}")
    except Exception as e:
        print(f"  HF repo check failed: {e}")


# ---------------------------------------------------------------------------
# Checkpoint helpers


def load_checkpoint(name: str) -> int:
    path = CHECKPOINT_DIR / f"{name}.json"
    if path.exists():
        with open(path, "r") as f:
            return json.load(f).get("next_idx", 0)
    return 0


def save_checkpoint(name: str, next_idx: int, total: int):
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    with open(CHECKPOINT_DIR / f"{name}.json", "w") as f:
        json.dump({"next_idx": next_idx, "total": total}, f)


# ---------------------------------------------------------------------------
# Nemotron translation


def translate_nemotron_record(client: OpenAI, record: dict) -> dict:
    """Translate question + options concurrently, keep answer letter."""
    texts = [record["question"]] + list(record["options"])
    results = [None] * len(texts)

    with ThreadPoolExecutor(max_workers=len(texts)) as pool:
        futures = {
            pool.submit(translate_text, client, text): i
            for i, text in enumerate(texts)
        }
        for future in as_completed(futures):
            idx = futures[future]
            results[idx] = future.result()

    return {
        "question_en": record["question"],
        "question": results[0],
        "options_en": list(record["options"]),
        "options": results[1:],
        "answer": record["answer"],
        "split": record["split"],
    }


def load_nemotron(limit: int) -> list[dict]:
    """Load Nemotron records, round-robin across math/code/stem."""
    splits = ["math", "code", "stem"]
    print(f"Loading Nemotron splits: {splits} (limit {limit})...")
    records = []
    per_split = limit // len(splits)
    remainder = limit % len(splits)
    for i, split in enumerate(splits):
        n = per_split + (1 if i < remainder else 0)
        ds = load_dataset("nvidia/Nemotron-Research-GooseReason-0.7M",
                          split=split, streaming=True)
        for j, rec in enumerate(ds):
            if j >= n:
                break
            records.append({"split": split, **rec})
    print(f"  Loaded {len(records)} Nemotron records")
    return records


def run_nemotron(workers: int, limit: int, upload_every: int, hf_repo: str):
    name = "nemotron"
    output = OUTPUT_DIR / "nemotron_darija.jsonl"
    records = load_nemotron(limit)
    total = len(records)
    start_idx = load_checkpoint(name)

    if start_idx >= total:
        print(f"[{name}] Already complete ({total} records)")
        return

    remaining = total - start_idx
    clients = make_clients(workers)
    print(f"[{name}] Translating {remaining} records with {workers} workers\n")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    last_upload = start_idx

    for batch_start in range(start_idx, total, workers):
        batch_end = min(batch_start + workers, total)
        batch = list(range(batch_start, batch_end))

        batch_results = {}
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            futures = {}
            for i, idx in enumerate(batch):
                cl = clients[i % workers]
                fut = pool.submit(translate_nemotron_record, cl, records[idx])
                futures[fut] = idx
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    batch_results[idx] = future.result()
                except Exception as e:
                    print(f"  [{idx}] FAILED: {e}")
                    batch_results[idx] = {
                        "question": records[idx]["question"],
                        "options": list(records[idx]["options"]),
                        "answer": records[idx]["answer"],
                        "split": records[idx]["split"],
                        "error": str(e),
                    }

        for idx in batch:
            mode = "a" if idx > 0 or start_idx > 0 else "w"
            with open(output, mode, encoding="utf-8") as f:
                f.write(json.dumps(
                    batch_results[idx], ensure_ascii=False) + "\n")
            save_checkpoint(name, idx + 1, total)

        # Progress
        elapsed = time.time() - t_start
        done = batch_end - start_idx
        rate = done / max(elapsed, 1)
        eta = (total - batch_end) / rate / 60 if rate > 0 else 0
        print(f"  [{name}] [{done}/{remaining}] {rate:.1f} rec/s, ~{eta:.1f}m left")

        # Periodic upload
        if batch_end - last_upload >= upload_every:
            upload_to_hf(output, hf_repo)
            last_upload = batch_end

    # Final upload
    upload_to_hf(output, hf_repo)
    elapsed = time.time() - t_start
    print(f"[{name}] Done! {remaining} records in {elapsed/60:.1f} minutes\n")


# ---------------------------------------------------------------------------
# Conversation translation (Alpaca, SmolTalk)


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
        "messages_en": [{"role": m["role"], "content": m["content"]} for m in messages],
        "messages": [
            {"role": messages[i]["role"], "content": results[i]}
            for i in range(len(messages))
        ],
    }


def load_alpaca(limit: int) -> list[dict]:
    print(f"Loading yahma/alpaca-cleaned (limit {limit})...")
    ds = load_dataset("yahma/alpaca-cleaned", split="train", streaming=True,
                      token=HF_READ_TOKEN)
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


def load_smoltalk(limit: int) -> list[dict]:
    print(f"Loading SmolTalk/everyday-conversations (limit {limit})...")
    ds = load_dataset("HuggingFaceTB/smoltalk", "everyday-conversations",
                      split="train", streaming=True, token=HF_READ_TOKEN)
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
    print(f"  Loaded {len(samples)} SmolTalk samples")
    return samples


def run_conversations(name: str, loader, workers: int, limit: int,
                      upload_every: int, hf_repo: str):
    output = OUTPUT_DIR / f"{name}_darija.jsonl"
    records = loader(limit)
    total = len(records)
    start_idx = load_checkpoint(name)

    if start_idx >= total:
        print(f"[{name}] Already complete ({total} records)")
        return

    remaining = total - start_idx
    clients = make_clients(workers)
    print(f"[{name}] Translating {remaining} conversations with {workers} workers\n")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    last_upload = start_idx

    for batch_start in range(start_idx, total, workers):
        batch_end = min(batch_start + workers, total)
        batch = list(range(batch_start, batch_end))

        batch_results = {}
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            futures = {}
            for i, idx in enumerate(batch):
                cl = clients[i % workers]
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

        for idx in batch:
            mode = "a" if idx > 0 or start_idx > 0 else "w"
            with open(output, mode, encoding="utf-8") as f:
                f.write(json.dumps(
                    batch_results[idx], ensure_ascii=False) + "\n")
            save_checkpoint(name, idx + 1, total)

        # Progress
        elapsed = time.time() - t_start
        done = batch_end - start_idx
        rate = done / max(elapsed, 1)
        eta = (total - batch_end) / rate / 60 if rate > 0 else 0
        print(f"  [{name}] [{done}/{remaining}] {rate:.1f} rec/s, ~{eta:.1f}m left")

        # Periodic upload
        if batch_end - last_upload >= upload_every:
            upload_to_hf(output, hf_repo)
            last_upload = batch_end

    # Final upload
    upload_to_hf(output, hf_repo)
    elapsed = time.time() - t_start
    print(f"[{name}] Done! {remaining} records in {elapsed/60:.1f} minutes\n")


# ---------------------------------------------------------------------------
# Main


DATASETS = {
    "alpaca":   {"limit": 52_000, "loader": load_alpaca},
    "smoltalk": {"limit": 50_000, "loader": load_smoltalk},
}


def main():
    parser = argparse.ArgumentParser(
        description="Darija translation pipeline (on-device)")
    parser.add_argument("--dataset", choices=list(DATASETS.keys()) + ["all"], default="all",
                        help="Dataset to translate (default: all)")
    parser.add_argument("--workers", type=int, default=32,
                        help="Concurrent records (default: 32)")
    parser.add_argument("--upload-every", type=int, default=UPLOAD_EVERY,
                        help=f"Upload to HF every N records (default: {UPLOAD_EVERY})")
    parser.add_argument("--hf-repo", default=None,
                        help="HF dataset repo (default: auto-detect from token)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Override per-dataset limit")
    args = parser.parse_args()

    print("=" * 60)
    print("Darija Translation Pipeline")
    print("=" * 60)

    # Check endpoints
    print(f"\nChecking {len(ENDPOINTS)} endpoints...")
    if not check_endpoints():
        print("ERROR: Not all endpoints reachable. Aborting.")
        return

    # Resolve HF repo
    args.hf_repo = get_hf_repo(args.hf_repo)
    print()
    ensure_hf_repo(args.hf_repo)

    datasets = list(DATASETS.keys()) if args.dataset == "all" else [
        args.dataset]

    t_total = time.time()

    for ds_name in datasets:
        ds_cfg = DATASETS[ds_name]
        limit = args.limit or ds_cfg["limit"]

        print(f"\n{'='*60}")
        print(f"  Dataset: {ds_name} (limit: {limit:,})")
        print(f"{'='*60}\n")

        if ds_name == "nemotron":
            run_nemotron(args.workers, limit, args.upload_every, args.hf_repo)
        else:
            run_conversations(ds_name, ds_cfg["loader"], args.workers, limit,
                              args.upload_every, args.hf_repo)

    elapsed = time.time() - t_total
    print(f"\n{'='*60}")
    print(f"All done! Total time: {elapsed/3600:.1f} hours")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
