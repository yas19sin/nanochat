#!/usr/bin/env python3
"""
Universal vLLM batch translation pipeline for Moroccan Darija.
Translates cherry-picked high-quality datasets via local vLLM server.

Supports:
  - Opus Reasoning (w/ thinking chains)
  - GooseReason-0.7M (math/code/stem MCQ)
  - SmolTalk2 SFT subsets (OpenHermes, magpie-ultra, OpenThoughts)
  - FineWeb-Edu (pretraining text)
  - Custom JSONL files

Setup on rented GPU (RTX 3090/4090):
    pip install vllm openai httpx datasets huggingface_hub

    # Start vLLM server with your translation model:
    vllm serve Lyte/tiny-aya-darija-v5 --dtype bfloat16 --max-model-len 8192 \
         --port 8000 --gpu-memory-utilization 0.92 --max-num-seqs 128

    # Then run translation:
    python translate_vllm.py --dataset opus           # 2,160 rows
    python translate_vllm.py --dataset goose-math      # 235K rows
    python translate_vllm.py --dataset goose-stem       # 155K rows
    python translate_vllm.py --dataset smoltalk-hermes  # 385K rows
    python translate_vllm.py --dataset smoltalk-magpie  # 407K rows
    python translate_vllm.py --dataset smoltalk-thoughts # 100K sample
    python translate_vllm.py --dataset fineweb          # pretraining text
    python translate_vllm.py --dataset all              # everything

    # Or limit for testing:
    python translate_vllm.py --dataset opus --limit 20 --workers 4
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

ENDPOINT = os.environ.get("VLLM_ENDPOINT", "http://localhost:8000/v1")
API_KEY = "EMPTY"
MODEL_NAME = os.environ.get("VLLM_MODEL", "Lyte/tiny-aya-darija-v5")

HF_READ_TOKEN = os.environ["HF_TOKEN"]
HF_WRITE_TOKEN = os.environ["HF_WRITE_TOKEN"]

SYSTEM_PROMPT = (
    "Translate the following English text into Moroccan Darija (الدارجة المغربية). "
    "Output ONLY the Darija translation."
)

# Proven gen params from translate_identity.py / v5 testing
GEN_PARAMS = dict(
    max_tokens=512,
    temperature=0.3,
    top_p=0.98,
    extra_body={"top_k": 300, "repetition_penalty": 1.15},
)

# Longer max_tokens for thinking chains & solutions
GEN_PARAMS_LONG = dict(
    max_tokens=2048,
    temperature=0.3,
    top_p=0.98,
    extra_body={"top_k": 300, "repetition_penalty": 1.15},
)

MAX_RETRIES = 3
RETRY_DELAY = 5
UPLOAD_EVERY = 500
DEFAULT_WORKERS = 32

OUTPUT_DIR = Path("output")
CHECKPOINT_DIR = Path("checkpoints")


# ---------------------------------------------------------------------------
# Client helpers

def make_client() -> OpenAI:
    return OpenAI(
        base_url=ENDPOINT,
        api_key=API_KEY,
        http_client=httpx.Client(timeout=httpx.Timeout(300.0)),
    )


def make_clients(n: int) -> list[OpenAI]:
    return [make_client() for _ in range(n)]


def check_endpoint() -> bool:
    try:
        c = make_client()
        models = c.models.list()
        print(f"  OK: {ENDPOINT} → {models.data[0].id}")
        return True
    except Exception as e:
        print(f"  FAIL: {ENDPOINT} → {e}")
        return False


class TranslationError(Exception):
    """Raised when all translation retries are exhausted."""
    pass


def translate_text(client: OpenAI, text: str, long: bool = False) -> str:
    """Translate a single text via vLLM OpenAI-compatible API."""
    if not text or not text.strip():
        return text
    params = GEN_PARAMS_LONG if long else GEN_PARAMS
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
    raise TranslationError(f"All {MAX_RETRIES} retries failed")


# ---------------------------------------------------------------------------
# HuggingFace upload

def get_hf_repo() -> str:
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=HF_WRITE_TOKEN)
        user = api.whoami()["name"]
        return f"{user}/darija-translation-data"
    except Exception:
        return "darija-translation-data"


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

def sync_from_hf(repo_id: str, ds_names: list[str]):
    """Download existing outputs from HF to avoid re-translating.
    Creates local output files and checkpoints for datasets already on HF."""
    try:
        from huggingface_hub import HfApi, hf_hub_download
    except ImportError:
        return

    api = HfApi(token=HF_READ_TOKEN)
    try:
        remote_files = api.list_repo_files(
            repo_id=repo_id, repo_type="dataset")
    except Exception:
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

    for name in ds_names:
        fname = f"{name}_darija.jsonl"
        if fname not in remote_files:
            continue
        local_output = OUTPUT_DIR / fname
        local_ckpt = CHECKPOINT_DIR / f"{name}.json"
        # Skip if we already have local output (trust local over remote)
        if local_output.exists() and count_output_lines(local_output) > 0:
            continue
        # Download from HF
        try:
            downloaded = hf_hub_download(
                repo_id=repo_id, filename=fname,
                repo_type="dataset", token=HF_READ_TOKEN,
                cache_dir="cache_hf_sync",
            )
            # Copy to output dir
            import shutil
            shutil.copy2(downloaded, str(local_output))
            n_lines = count_output_lines(local_output)
            if n_lines > 0:
                save_checkpoint(name, n_lines, n_lines)
                print(f"  ↓ Synced {fname} from HF ({n_lines:,} records)")
        except Exception as e:
            print(f"  ↓ Sync failed for {fname}: {e}")


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


def count_output_lines(path: Path) -> int:
    """Count valid JSON lines in the output file."""
    if not path.exists():
        return 0
    count = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                json.loads(line)
                count += 1
            except json.JSONDecodeError:
                break  # corrupt/partial line = stop
    return count


def truncate_output(path: Path, n_lines: int):
    """Keep only the first n_lines valid JSON lines."""
    if not path.exists():
        return
    if n_lines <= 0:
        path.unlink()
        return
    lines = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if len(lines) >= n_lines:
                break
            line = line.strip()
            if line:
                lines.append(line + "\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


# ---------------------------------------------------------------------------
# Dataset loaders
#
# Each loader returns list[dict] with a uniform schema per dataset type.
# Three types:
#   1. "text" — single text field (pretraining)
#   2. "mcq"  — question + options + answer (GooseReason, Nemotron)
#   3. "conv" — messages list (SFT conversations)
#   4. "reasoning" — problem + thinking + solution (Opus)

def load_opus(limit: int) -> list[dict]:
    """Opus-4.6-Reasoning-3300x: problem, thinking, solution."""
    print(f"Loading Opus Reasoning (limit {limit:,})...")
    ds = load_dataset("Crownelius/Opus-4.6-Reasoning-3300x", split="train",
                      token=HF_READ_TOKEN)
    records = []
    for row in ds:
        records.append({
            "type": "reasoning",
            "id": row.get("id", ""),
            "problem": row.get("problem", ""),
            "thinking": row.get("thinking", ""),
            "solution": row.get("solution", ""),
            "difficulty": row.get("difficulty", ""),
            "category": row.get("category", ""),
        })
        if limit and len(records) >= limit:
            break
    print(f"  Loaded {len(records)} Opus records")
    return records


def load_goose(split: str, limit: int) -> list[dict]:
    """GooseReason-0.7M: question + options + answer MCQ."""
    print(f"Loading GooseReason/{split} (limit {limit:,})...")
    ds = load_dataset("nvidia/Nemotron-Research-GooseReason-0.7M",
                      split=split, streaming=True)
    records = []
    for row in ds:
        records.append({
            "type": "mcq",
            "split": split,
            "question": row["question"],
            "options": list(row["options"]),
            "answer": row["answer"],
        })
        if len(records) >= limit:
            break
    print(f"  Loaded {len(records)} GooseReason/{split} records")
    return records


def load_smoltalk_subset(subset: str, limit: int) -> list[dict]:
    """SmolTalk2 SFT subsets — conversation format."""
    print(f"Loading SmolTalk2/SFT/{subset} (limit {limit:,})...")
    ds = load_dataset("HuggingFaceTB/smoltalk2", "SFT", split=subset,
                      streaming=True, token=HF_READ_TOKEN)
    records = []
    for row in ds:
        msgs = row.get("messages", [])
        if not msgs:
            continue
        # Skip very long conversations (>6 turns or >2000 chars per msg)
        if len(msgs) > 6:
            continue
        if any(len(m.get("content", "")) > 2000 for m in msgs):
            continue
        records.append({
            "type": "conv",
            "source": subset,
            "messages": [{"role": m["role"], "content": m["content"]} for m in msgs],
        })
        if len(records) >= limit:
            break
    print(f"  Loaded {len(records)} SmolTalk2/{subset} records")
    return records


def load_fineweb(limit: int) -> list[dict]:
    """FineWeb-Edu scored >=3 for pretraining translation."""
    print(f"Loading FineWeb-Edu (limit {limit:,})...")
    ds = load_dataset("HuggingFaceFW/fineweb-edu", "sample-10BT",
                      split="train", streaming=True)
    records = []
    for row in ds:
        text = row.get("text", "")
        # Skip very short or very long docs
        if len(text) < 200 or len(text) > 3000:
            continue
        records.append({
            "type": "text",
            "text": text,
            "url": row.get("url", ""),
        })
        if len(records) >= limit:
            break
    print(f"  Loaded {len(records)} FineWeb-Edu records")
    return records


# ---------------------------------------------------------------------------
# Translators per record type

def translate_reasoning(client: OpenAI, record: dict) -> dict:
    """Translate Opus-style: problem + thinking + solution."""
    problem_darija = translate_text(client, record["problem"])
    thinking_darija = translate_text(
        client, record["thinking"], long=True) if record["thinking"] else ""
    solution_darija = translate_text(client, record["solution"], long=True)

    return {
        "id": record["id"],
        "problem_en": record["problem"],
        "problem": problem_darija,
        "thinking_en": record["thinking"],
        "thinking": thinking_darija,
        "solution_en": record["solution"],
        "solution": solution_darija,
        "difficulty": record["difficulty"],
        "category": record["category"],
    }


def translate_mcq(client: OpenAI, record: dict) -> dict:
    """Translate GooseReason MCQ: question + all options."""
    texts = [record["question"]] + record["options"]
    # question gets long=True (can be large masked passage), options get long=False (shorter choices)
    long_flags = [True] + [False] * len(record["options"])
    results = [None] * len(texts)

    with ThreadPoolExecutor(max_workers=min(len(texts), 8)) as pool:
        futures = {
            pool.submit(translate_text, client, text, long_flags[i]): i
            for i, text in enumerate(texts)
        }
        for future in as_completed(futures):
            idx = futures[future]
            results[idx] = future.result()

    return {
        "question_en": record["question"],
        "question": results[0],
        "options_en": record["options"],
        "options": results[1:],
        "answer": record["answer"],
        "split": record["split"],
    }


def translate_conv(client: OpenAI, record: dict) -> dict:
    """Translate conversation: all messages concurrently."""
    messages = record["messages"]
    results = [None] * len(messages)

    with ThreadPoolExecutor(max_workers=min(len(messages), 8)) as pool:
        futures = {
            pool.submit(translate_text, client, msg["content"]): i
            for i, msg in enumerate(messages)
        }
        for future in as_completed(futures):
            idx = futures[future]
            results[idx] = future.result()

    return {
        "source": record["source"],
        "messages_en": [{"role": m["role"], "content": m["content"]} for m in messages],
        "messages": [
            {"role": messages[i]["role"], "content": results[i]}
            for i in range(len(messages))
        ],
    }


def translate_plain_text(client: OpenAI, record: dict) -> dict:
    """Translate pretraining text."""
    translated = translate_text(client, record["text"], long=True)
    return {
        "text_en": record["text"],
        "text": translated,
    }


TRANSLATORS = {
    "reasoning": translate_reasoning,
    "mcq": translate_mcq,
    "conv": translate_conv,
    "text": translate_plain_text,
}


# ---------------------------------------------------------------------------
# Main translation engine

def run_dataset(name: str, records: list[dict], workers: int,
                upload_every: int, hf_repo: str):
    """Generic translation loop for any record type."""
    output = OUTPUT_DIR / f"{name}_darija.jsonl"
    total = len(records)

    # --- Safe resume: use file line count as ground truth ---
    ckpt_idx = load_checkpoint(name)
    file_lines = count_output_lines(output)
    start_idx = min(ckpt_idx, file_lines)

    # If fully complete, skip entirely (no rewind needed)
    if start_idx >= total:
        print(f"[{name}] Already complete ({total:,} records)")
        return

    # Rewind by one batch to re-translate potentially corrupt tail records
    # (only when resuming a partial run, not when complete)
    if start_idx > 0:
        rewind = min(workers, start_idx)
        start_idx -= rewind
        truncate_output(output, start_idx)
        save_checkpoint(name, start_idx, total)
        print(f"[{name}] Resuming from {start_idx:,} "
              f"(file had {file_lines:,} lines, ckpt had {ckpt_idx:,}, "
              f"rewound {rewind} for safety)")

    remaining = total - start_idx
    clients = make_clients(workers)
    print(f"[{name}] Translating {remaining:,} records with {workers} workers\n")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    last_upload = start_idx
    consecutive_failures = 0

    for batch_start in range(start_idx, total, workers):
        batch_end = min(batch_start + workers, total)
        batch = list(range(batch_start, batch_end))

        batch_results = {}
        batch_failures = 0
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            futures = {}
            for i, idx in enumerate(batch):
                rec = records[idx]
                translator = TRANSLATORS[rec["type"]]
                cl = clients[i % workers]
                fut = pool.submit(translator, cl, rec)
                futures[fut] = idx
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    batch_results[idx] = future.result()
                except Exception as e:
                    print(f"  [{idx}] FAILED: {e}")
                    batch_failures += 1

        # If most of the batch failed, server is probably down — stop
        if batch_failures > len(batch) // 2:
            consecutive_failures += 1
            print(f"  ⚠ Batch mostly failed ({batch_failures}/{len(batch)}). "
                  f"Server may be down.")
            if consecutive_failures >= 2:
                print(f"  ✖ {consecutive_failures} consecutive failed batches. "
                      f"Stopping {name}. Fix server and re-run to resume.")
                break
            continue  # skip writing this batch, retry next
        else:
            consecutive_failures = 0

        # Write only successful results in order
        written = 0
        for idx in batch:
            if idx not in batch_results:
                continue  # skip failed records — will be retried on next run
            mode = "a" if idx > 0 or start_idx > 0 else "w"
            with open(output, mode, encoding="utf-8") as f:
                f.write(json.dumps(
                    batch_results[idx], ensure_ascii=False) + "\n")
            written += 1
        # Checkpoint only advances to the last contiguously-written index
        if written == len(batch):
            save_checkpoint(name, batch_end, total)
        # If partial, checkpoint stays where it was — the whole batch
        # will be re-attempted on resume (some dupes, but safe)

        # Progress
        elapsed = time.time() - t_start
        done = batch_end - start_idx
        rate = done / max(elapsed, 1)
        eta = (total - batch_end) / rate / 60 if rate > 0 else 0
        pct = (batch_end / total) * 100
        print(f"  [{name}] {batch_end:,}/{total:,} ({pct:.1f}%) "
              f"| {rate:.1f} rec/s | ~{eta:.0f}m left")

        # Periodic upload
        if batch_end - last_upload >= upload_every:
            upload_to_hf(output, hf_repo)
            last_upload = batch_end

    # Final upload
    upload_to_hf(output, hf_repo)
    elapsed = time.time() - t_start
    print(f"\n[{name}] Done! {remaining:,} records in {elapsed/60:.1f} minutes")
    print(f"  Output: {output}\n")


# ---------------------------------------------------------------------------
# Dataset registry

DATASETS = {
    # Reasoning with thinking chains
    "opus": {
        "loader": lambda lim: load_opus(lim),
        "default_limit": 2_160,
        "desc": "Opus-4.6-Reasoning (problem+thinking+solution)",
    },
    # GooseReason splits
    "goose-math": {
        "loader": lambda lim: load_goose("math", lim),
        "default_limit": 235_836,
        "desc": "GooseReason Math (Olympiad MCQ)",
    },
    "goose-stem": {
        "loader": lambda lim: load_goose("stem", lim),
        "default_limit": 155_496,
        "desc": "GooseReason STEM (science MCQ)",
    },
    # SmolTalk2 SFT subsets
    "smoltalk-hermes": {
        "loader": lambda lim: load_smoltalk_subset("OpenHermes_2.5_no_think", lim),
        "default_limit": 384_900,
        "desc": "SmolTalk2 OpenHermes-2.5 (general instruct)",
    },
    "smoltalk-magpie": {
        "loader": lambda lim: load_smoltalk_subset("smoltalk_smollm3_smol_magpie_ultra_no_think", lim),
        "default_limit": 406_843,
        "desc": "SmolTalk2 magpie-ultra (diverse high-quality)",
    },
    "smoltalk-thoughts": {
        "loader": lambda lim: load_smoltalk_subset("OpenThoughts3_1.2M_think", lim),
        "default_limit": 100_000,
        "desc": "SmolTalk2 OpenThoughts (deep reasoning, sampled)",
    },
    # Pretraining text
    "fineweb": {
        "loader": lambda lim: load_fineweb(lim),
        "default_limit": 1_000_000,
        "desc": "FineWeb-Edu (pretraining, scored >=3)",
    },
}


def main():
    ds_names = list(DATASETS.keys())
    parser = argparse.ArgumentParser(
        description="Universal vLLM Darija translation pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Datasets:\n" + "\n".join(
            f"  {k:22s} {v['desc']} ({v['default_limit']:,} rows)"
            for k, v in DATASETS.items()
        ),
    )
    parser.add_argument("--dataset", choices=ds_names + ["all", "goose-all",
                        "smoltalk-all", "sft-all"],
                        default="all", help="Dataset(s) to translate")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Concurrent workers (default: {DEFAULT_WORKERS})")
    parser.add_argument("--upload-every", type=int, default=UPLOAD_EVERY,
                        help=f"Upload to HF every N records (default: {UPLOAD_EVERY})")
    parser.add_argument("--hf-repo", default=None,
                        help="HF dataset repo (default: auto-detect)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Override row limit for selected dataset(s)")
    parser.add_argument("--endpoint", default=None,
                        help="vLLM endpoint URL (default: http://localhost:8000/v1)")
    parser.add_argument("--model", default=None,
                        help="Model name for vLLM (default: Lyte/tiny-aya-darija-v5)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Load data and show stats, don't translate")
    args = parser.parse_args()

    global ENDPOINT, MODEL_NAME
    if args.endpoint:
        ENDPOINT = args.endpoint
    if args.model:
        MODEL_NAME = args.model

    # Resolve dataset groups
    if args.dataset == "all":
        selected = ds_names
    elif args.dataset == "goose-all":
        selected = [k for k in ds_names if k.startswith("goose-")]
    elif args.dataset == "smoltalk-all":
        selected = [k for k in ds_names if k.startswith("smoltalk-")]
    elif args.dataset == "sft-all":
        selected = [k for k in ds_names if k != "fineweb"]
    else:
        selected = [args.dataset]

    print("=" * 60)
    print("  Darija Translation Pipeline (vLLM)")
    print("=" * 60)
    print(f"\n  Endpoint:  {ENDPOINT}")
    print(f"  Model:     {MODEL_NAME}")
    print(f"  Datasets:  {', '.join(selected)}")
    print(f"  Workers:   {args.workers}")
    print(f"  Gen:       temp={GEN_PARAMS['temperature']}, "
          f"top_p={GEN_PARAMS['top_p']}, "
          f"top_k={GEN_PARAMS['extra_body']['top_k']}, "
          f"rep_penalty={GEN_PARAMS['extra_body']['repetition_penalty']}")

    if not args.dry_run:
        print(f"\n  Checking endpoint...")
        if not check_endpoint():
            print("ERROR: vLLM endpoint not reachable. Start it with:")
            print(f"  vllm serve {MODEL_NAME} --dtype bfloat16 "
                  f"--max-model-len 2048 --port 8000")
            return

    # Resolve HF repo
    hf_repo = args.hf_repo or get_hf_repo()
    if not args.dry_run:
        ensure_hf_repo(hf_repo)

    # Sync existing outputs from HF to avoid re-translating on new machines
    print(f"\n  Syncing existing outputs from HF...")
    sync_from_hf(hf_repo, [n for n in selected])

    # Estimate totals
    total_rows = 0
    total_est_tokens = 0
    token_estimates = {
        "opus": 4_300_000,
        "goose-math": 175_000_000,
        "goose-stem": 115_000_000,
        "smoltalk-hermes": 160_000_000,
        "smoltalk-magpie": 620_000_000,
        "smoltalk-thoughts": 150_000_000,
        "fineweb": 500_000_000,
    }

    print(f"\n  {'Dataset':22s} {'Rows':>12s} {'Est. tokens':>14s}")
    print(f"  {'-'*50}")
    for ds_name in selected:
        ds_cfg = DATASETS[ds_name]
        lim = args.limit or ds_cfg["default_limit"]
        est_tok = int(token_estimates.get(ds_name, 0)
                      * (lim / ds_cfg["default_limit"]))
        total_rows += lim
        total_est_tokens += est_tok
        print(f"  {ds_name:22s} {lim:>12,} {est_tok:>14,}")
    print(f"  {'-'*50}")
    print(f"  {'TOTAL':22s} {total_rows:>12,} {total_est_tokens:>14,}")

    rate_tok_per_sec = 3500  # conservative vLLM batch estimate
    est_hours = total_est_tokens / rate_tok_per_sec / 3600
    est_cost = est_hours * 0.25  # $0.25/hr for RTX 3090
    print(f"\n  Est. time: ~{est_hours:.1f} hours")
    print(f"  Est. cost: ~${est_cost:.2f} (RTX 3090 @ $0.25/hr)")
    print(f"  2x double-dip effective: ~{total_est_tokens*2:,} SFT tokens")

    if args.dry_run:
        print("\n  [DRY RUN] Loading sample data to verify...")
        for ds_name in selected:
            ds_cfg = DATASETS[ds_name]
            records = ds_cfg["loader"](min(args.limit or 5, 5))
            rec = records[0] if records else {}
            print(f"\n  [{ds_name}] Sample keys: {list(rec.keys())}")
            if rec.get("type") == "conv":
                print(f"    Messages: {len(rec.get('messages', []))} turns")
            elif rec.get("type") == "mcq":
                print(f"    Options: {len(rec.get('options', []))}")
                print(f"    Question[:80]: {rec.get('question', '')[:80]}")
            elif rec.get("type") == "reasoning":
                print(f"    Problem[:80]: {rec.get('problem', '')[:80]}")
            elif rec.get("type") == "text":
                print(f"    Text[:80]: {rec.get('text', '')[:80]}")
        print("\n  [DRY RUN] All loaders OK. Remove --dry-run to translate.")
        return

    t_total = time.time()

    for ds_name in selected:
        ds_cfg = DATASETS[ds_name]
        limit = args.limit or ds_cfg["default_limit"]

        print(f"\n{'='*60}")
        print(f"  {ds_name}: {ds_cfg['desc']}")
        print(f"  Limit: {limit:,}")
        print(f"{'='*60}\n")

        records = ds_cfg["loader"](limit)
        run_dataset(ds_name, records, args.workers, args.upload_every, hf_repo)

    elapsed = time.time() - t_total
    print(f"\n{'='*60}")
    print(f"  All done! Total: {elapsed/3600:.1f} hours")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
