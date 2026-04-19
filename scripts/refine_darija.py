#!/usr/bin/env python3
"""
Darija refinement pass — reviews translated data and fixes:
  1. Dropped/untranslated instructions (model treated them as commands)
  2. MSA leakage where Darija is expected
  3. Unnatural phrasing
  4. Any other quality issues

Reads from Lyte/darija-translation-data, outputs refined version as a
separate dataset file (alpaca_darija_refined.jsonl, smoltalk_darija_refined.jsonl).

Usage:
    pip install openai httpx datasets huggingface_hub

    # Refine all datasets:
    python refine_darija.py

    # Single dataset:
    python refine_darija.py --dataset alpaca

    # Resume after crash (automatic via checkpoints):
    python refine_darija.py
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

ENDPOINTS = [f"http://localhost:{p}/v1" for p in range(8000, 8004)]
API_KEY = "EMPTY"
MODEL_NAME = "gemma4-31b-awq"
HF_READ_TOKEN = os.environ["HF_TOKEN"]
HF_WRITE_TOKEN = os.environ["HF_WRITE_TOKEN"]
HF_REPO = None  # Auto-detected

SYSTEM_PROMPT = """\
You are a Moroccan Darija (الدارجة المغربية) expert reviewer.

You will receive an English original and its Darija translation. Your job is to fix any issues:

1. **Missing text**: If any part of the English was not translated (e.g. instructions like "Rewrite the following..." were executed instead of translated), translate it now.
2. **MSA leakage**: Replace formal/Standard Arabic words or grammar with natural Darija equivalents (e.g. أما → أشمن/إينا, لكن → ولكن/والاكين, هل → واش).
3. **Unnatural phrasing**: Fix anything that sounds stiff or bookish — it should sound like a Moroccan person speaking naturally.
4. **Completeness**: The Darija must cover ALL content from the English. Nothing should be dropped.

Output ONLY the corrected Darija text. If the translation is already perfect, output it unchanged.
Do NOT add commentary, explanations, or notes — just the Darija text."""

GEN_PARAMS = dict(
    max_tokens=4096,
    temperature=0.5,
    top_p=0.95,
    extra_body={"top_k": 64, "repetition_penalty": 1.0},
)

MAX_RETRIES = 3
RETRY_DELAY = 5
UPLOAD_EVERY = 250

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


def refine_text(client: OpenAI, english: str, darija: str) -> str:
    """Send English + Darija to the model for review/correction."""
    if not english or not english.strip() or not darija or not darija.strip():
        return darija

    user_msg = f"**English original:**\n{english}\n\n**Current Darija translation:**\n{darija}"

    for attempt in range(MAX_RETRIES):
        try:
            resp = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                **GEN_PARAMS,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            print(f"    [retry {attempt+1}/{MAX_RETRIES}] {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
    return darija  # Return original on total failure


# ---------------------------------------------------------------------------
# HuggingFace


def get_hf_repo(override: str | None = None) -> str:
    if override:
        return override
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=HF_WRITE_TOKEN)
        user = api.whoami()["name"]
        return f"{user}/darija-translation-data-refined"
    except Exception as e:
        print(f"  Could not detect HF username: {e}")
        return "darija-translation-data-refined"


def upload_to_hf(filepath: Path, repo_id: str):
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
    path = CHECKPOINT_DIR / f"refine_{name}.json"
    if path.exists():
        with open(path, "r") as f:
            return json.load(f).get("next_idx", 0)
    return 0


def save_checkpoint(name: str, next_idx: int, total: int):
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    with open(CHECKPOINT_DIR / f"refine_{name}.json", "w") as f:
        json.dump({"next_idx": next_idx, "total": total}, f)


# ---------------------------------------------------------------------------
# Refinement logic


def refine_conversation(client: OpenAI, record: dict) -> dict:
    """Refine all messages in a conversation concurrently."""
    en_messages = record["messages_en"]
    da_messages = record["messages"]
    results = [None] * len(en_messages)
    changed_flags = [False] * len(en_messages)

    with ThreadPoolExecutor(max_workers=len(en_messages)) as pool:
        futures = {}
        for i in range(len(en_messages)):
            en_text = en_messages[i]["content"]
            da_text = da_messages[i]["content"]
            fut = pool.submit(refine_text, client, en_text, da_text)
            futures[fut] = i
        for future in as_completed(futures):
            idx = futures[future]
            refined = future.result()
            results[idx] = refined
            changed_flags[idx] = (refined != da_messages[idx]["content"])

    any_changed = any(changed_flags)

    return {
        "source": record.get("source", "unknown"),
        "messages_en": en_messages,
        "messages": [
            {"role": en_messages[i]["role"], "content": results[i]}
            for i in range(len(en_messages))
        ],
        "refined": any_changed,
    }


def load_translated_data(name: str) -> list[dict]:
    """Load previously translated data from HF."""
    source_repo = None
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=HF_READ_TOKEN)
        user = api.whoami()["name"]
        source_repo = f"{user}/darija-translation-data"
    except Exception:
        source_repo = "Lyte/darija-translation-data"

    filename = f"{name}_darija.jsonl"
    print(f"Loading {filename} from {source_repo}...")
    ds = load_dataset(source_repo, data_files=filename, split="train",
                      token=HF_READ_TOKEN)
    records = [ds[i] for i in range(len(ds))]
    print(f"  Loaded {len(records)} records")
    return records


def run_refinement(name: str, workers: int, upload_every: int, hf_repo: str):
    output = OUTPUT_DIR / f"{name}_darija_refined.jsonl"
    records = load_translated_data(name)
    total = len(records)
    start_idx = load_checkpoint(name)

    if start_idx >= total:
        print(f"[refine-{name}] Already complete ({total} records)")
        return

    remaining = total - start_idx
    clients = make_clients(workers)
    print(
        f"[refine-{name}] Refining {remaining} records with {workers} workers\n")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    last_upload = start_idx
    changed_count = 0

    for batch_start in range(start_idx, total, workers):
        batch_end = min(batch_start + workers, total)
        batch = list(range(batch_start, batch_end))

        batch_results = {}
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            futures = {}
            for i, idx in enumerate(batch):
                cl = clients[i % workers]
                fut = pool.submit(refine_conversation, cl, records[idx])
                futures[fut] = idx
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    batch_results[idx] = future.result()
                except Exception as e:
                    print(f"  [{idx}] FAILED: {e}")
                    batch_results[idx] = {
                        "source": records[idx].get("source", "unknown"),
                        "messages_en": records[idx]["messages_en"],
                        "messages": records[idx]["messages"],
                        "refined": False,
                        "error": str(e),
                    }

        for idx in batch:
            result = batch_results[idx]
            if result.get("refined"):
                changed_count += 1

            mode = "a" if idx > 0 or start_idx > 0 else "w"
            with open(output, mode, encoding="utf-8") as f:
                f.write(json.dumps(result, ensure_ascii=False) + "\n")
            save_checkpoint(name, idx + 1, total)

        # Progress
        elapsed = time.time() - t_start
        done = batch_end - start_idx
        rate = done / max(elapsed, 1)
        eta = (total - batch_end) / rate / 60 if rate > 0 else 0
        print(f"  [refine-{name}] [{done}/{remaining}] {rate:.1f} rec/s, "
              f"~{eta:.1f}m left | {changed_count} changed")

        # Periodic upload
        if batch_end - last_upload >= upload_every:
            upload_to_hf(output, hf_repo)
            last_upload = batch_end

    # Final upload
    upload_to_hf(output, hf_repo)
    elapsed = time.time() - t_start
    print(
        f"\n[refine-{name}] Done! {remaining} records in {elapsed/60:.1f} minutes")
    print(f"  {changed_count}/{remaining} records modified ({100*changed_count/max(remaining, 1):.1f}%)")


# ---------------------------------------------------------------------------
# Main

DATASETS = ["alpaca", "smoltalk"]


def main():
    parser = argparse.ArgumentParser(
        description="Darija refinement pass — review and fix translations")
    parser.add_argument("--dataset", choices=DATASETS + ["all"], default="all",
                        help="Dataset to refine (default: all)")
    parser.add_argument("--workers", type=int, default=32,
                        help="Concurrent records (default: 32)")
    parser.add_argument("--upload-every", type=int, default=UPLOAD_EVERY,
                        help=f"Upload to HF every N records (default: {UPLOAD_EVERY})")
    parser.add_argument("--hf-repo", default=None,
                        help="HF dataset repo for refined output (default: auto-detect)")
    args = parser.parse_args()

    print("=" * 60)
    print("Darija Refinement Pipeline")
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

    datasets = DATASETS if args.dataset == "all" else [args.dataset]

    t_total = time.time()

    for ds_name in datasets:
        print(f"\n{'='*60}")
        print(f"  Refining: {ds_name}")
        print(f"{'='*60}\n")

        run_refinement(ds_name, args.workers, args.upload_every, args.hf_repo)

    elapsed = time.time() - t_total
    print(f"\n{'='*60}")
    print(f"All refinement complete in {elapsed/60:.1f} minutes")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
