#!/usr/bin/env python3
"""
Darija translation correction pipeline.
Downloads translated datasets from HuggingFace, corrects errors via a
reviewer model (gemma-4-26B-A4B-it bf16), and uploads corrected data to a new repo.

Designed to run on the 2x 5090 machine with tensor parallel.

Setup:
    # On 2x 5090 (bf16, TP=2, ~52GB):
    vllm serve google/gemma-4-26B-A4B-it \
        --dtype bfloat16 --max-model-len 8192 --port 8000 \
        --gpu-memory-utilization 0.95 --max-num-seqs 64 \
        --tensor-parallel-size 2 \
        --download-dir /tmp/models

    # Run correction:
    python correct_vllm.py --dataset all             # correct all files
    python correct_vllm.py --dataset goose-math       # single file
    python correct_vllm.py --list                     # show available files
"""

import json
import os
import re
import time
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI
import httpx

# ---------------------------------------------------------------------------
# Config

ENDPOINT = os.environ.get("VLLM_CORRECTION_ENDPOINT",
                          "http://localhost:8000/v1")
API_KEY = "EMPTY"
MODEL_NAME = os.environ.get("VLLM_CORRECTION_MODEL",
                            "google/gemma-4-26B-A4B-it")

HF_READ_TOKEN = os.environ["HF_TOKEN"]
HF_WRITE_TOKEN = os.environ["HF_WRITE_TOKEN"]

SOURCE_REPO = os.environ.get("HF_SOURCE_REPO", "Lyte/darija-translation-data")
DEST_REPO = os.environ.get(
    "HF_DEST_REPO", "Lyte/darija-translation-data-corrected")

SYSTEM_PROMPT = (
    "You are a Moroccan Darija translation reviewer. Your job is to fix errors "
    "in Darija translations by comparing them against the original text.\n\n"
    "Rules:\n"
    "1. Fix ONLY actual errors: wrong numbers, math mistakes, mistranslated words, "
    "or missing/added content.\n"
    "2. DO NOT rephrase, restructure, or \"improve\" correct Darija. If a sentence "
    "is valid Darija and conveys the right meaning, leave it exactly as-is.\n"
    "3. DO NOT shift the language toward Modern Standard Arabic (فصحى). Keep the "
    "same Darija register and vocabulary the translator used.\n"
    "4. Preserve all formatting (markdown, tables, bold, etc.) exactly as-is.\n"
    "5. Output ONLY the corrected translation — no explanations, no commentary."
)

USER_PROMPT_TEMPLATE = (
    "this is the original text:\n```\n{original}\n```\n"
    "this is the translation:\n```\n{translation}\n```\n"
    "Fix ONLY the errors in this translation by comparing it to the original. "
    "Do not rephrase correct Darija or make it more formal/Arabic. "
    "Change only what is factually wrong (wrong numbers, wrong words, wrong meaning). "
    "Keep everything else exactly as the translator wrote it."
)

# Correction gen params — low temperature for faithful correction
GEN_PARAMS = dict(
    max_tokens=2048,
    temperature=0.1,
    top_p=0.95,
)

GEN_PARAMS_LONG = dict(
    max_tokens=4096,
    temperature=0.1,
    top_p=0.95,
)

MAX_RETRIES = 3
RETRY_DELAY = 5
UPLOAD_EVERY = 250
DEFAULT_WORKERS = 16

OUTPUT_DIR = Path("output_corrected")
CHECKPOINT_DIR = Path("checkpoints_corrected")
CACHE_DIR = Path("cache_source")


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


# ---------------------------------------------------------------------------
# Correction logic

def clean_response(text: str) -> str:
    """Strip accidental markdown fences from model output."""
    text = text.strip()
    # Remove wrapping ```...``` if model added them
    if text.startswith("```") and text.endswith("```"):
        text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        # Also strip optional language tag on first line
        first_nl = text.find("\n")
        if first_nl != -1 and first_nl < 20:
            maybe_lang = text[:first_nl].strip()
            if not maybe_lang or maybe_lang.isalpha():
                text = text[first_nl + 1:]
        text = text.strip()
    return text


def correct_text(client: OpenAI, original: str, translation: str,
                 long: bool = False) -> str:
    """Send a single original+translation pair for correction."""
    if not original or not original.strip():
        return translation
    if not translation or not translation.strip():
        return translation

    prompt = USER_PROMPT_TEMPLATE.format(
        original=original, translation=translation)
    params = GEN_PARAMS_LONG if long else GEN_PARAMS

    for attempt in range(MAX_RETRIES):
        try:
            resp = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                **params,
            )
            result = clean_response(resp.choices[0].message.content)
            # Sanity: if result is empty or way too short, keep original
            if len(result) < len(translation) * 0.3:
                print(f"    ⚠ Correction too short ({len(result)} vs "
                      f"{len(translation)}), keeping original")
                return translation
            return result
        except Exception as e:
            print(f"    [retry {attempt+1}/{MAX_RETRIES}] {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
    # All retries failed — return uncorrected
    return translation


# ---------------------------------------------------------------------------
# Record type detection and correction

def detect_type(record: dict) -> str:
    """Auto-detect record type from field names."""
    if "problem_en" in record:
        return "reasoning"
    if "question_en" in record:
        return "mcq"
    if "messages_en" in record:
        return "conv"
    if "text_en" in record:
        return "text"
    return "unknown"


def correct_mcq(client: OpenAI, record: dict) -> dict:
    """Correct MCQ record: question + all options."""
    result = dict(record)  # shallow copy

    # Correct question (can be long — math problems with solutions)
    result["question"] = correct_text(
        client, record["question_en"], record["question"], long=True)

    # Correct each option
    corrected_opts = []
    for orig, trans in zip(record["options_en"], record["options"]):
        corrected_opts.append(correct_text(client, orig, trans, long=True))
    result["options"] = corrected_opts

    return result


def correct_conv(client: OpenAI, record: dict) -> dict:
    """Correct conversation record: all message contents."""
    result = dict(record)
    corrected_messages = []

    for msg_en, msg_darija in zip(record["messages_en"], record["messages"]):
        corrected_content = correct_text(
            client, msg_en["content"], msg_darija["content"],
            long=len(msg_en["content"]) > 500)
        corrected_messages.append({
            "role": msg_darija["role"],
            "content": corrected_content,
        })

    result["messages"] = corrected_messages
    return result


def correct_reasoning(client: OpenAI, record: dict) -> dict:
    """Correct reasoning record: problem + thinking + solution."""
    result = dict(record)

    result["problem"] = correct_text(
        client, record["problem_en"], record["problem"])

    if record.get("thinking") and record.get("thinking_en"):
        result["thinking"] = correct_text(
            client, record["thinking_en"], record["thinking"], long=True)

    result["solution"] = correct_text(
        client, record["solution_en"], record["solution"], long=True)

    return result


def correct_plain_text(client: OpenAI, record: dict) -> dict:
    """Correct pretraining text."""
    result = dict(record)
    result["text"] = correct_text(
        client, record["text_en"], record["text"], long=True)
    return result


CORRECTORS = {
    "reasoning": correct_reasoning,
    "mcq": correct_mcq,
    "conv": correct_conv,
    "text": correct_plain_text,
}


# ---------------------------------------------------------------------------
# HuggingFace helpers

def get_dest_repo(override: str | None = None) -> str:
    if override:
        return override
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=HF_WRITE_TOKEN)
        user = api.whoami()["name"]
        return f"{user}/darija-translation-data-corrected"
    except Exception:
        return "darija-translation-data-corrected"


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


def list_source_files(repo_id: str) -> list[str]:
    """List all _darija.jsonl files in the source repo."""
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=HF_READ_TOKEN)
        files = api.list_repo_files(repo_id=repo_id, repo_type="dataset")
        return [f for f in files if f.endswith("_darija.jsonl")]
    except Exception as e:
        print(f"  Failed to list files: {e}")
        return []


def download_source_file(repo_id: str, filename: str) -> Path:
    """Download a file from HF to local cache."""
    from huggingface_hub import hf_hub_download
    local = hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        repo_type="dataset",
        token=HF_READ_TOKEN,
        cache_dir=str(CACHE_DIR),
    )
    return Path(local)


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


def count_output_lines(path: Path) -> int:
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
                break
    return count


def truncate_output(path: Path, n_lines: int):
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
# Load records from downloaded JSONL

def load_records(filepath: Path) -> list[dict]:
    """Load all records from a JSONL file."""
    records = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                # Skip records that had errors during translation
                if "error" in rec:
                    continue
                records.append(rec)
            except json.JSONDecodeError:
                continue
    return records


# ---------------------------------------------------------------------------
# Main correction engine

def run_correction(name: str, records: list[dict], workers: int,
                   upload_every: int, dest_repo: str):
    """Generic correction loop for any record type."""
    output = OUTPUT_DIR / f"{name}_corrected.jsonl"
    total = len(records)

    if total == 0:
        print(f"[{name}] No records to correct")
        return

    # Detect record type from first record
    rec_type = detect_type(records[0])
    if rec_type == "unknown":
        print(f"[{name}] Unknown record type, skipping. "
              f"Fields: {list(records[0].keys())}")
        return

    corrector = CORRECTORS[rec_type]
    print(f"[{name}] Type: {rec_type}, Records: {total:,}")

    # --- Safe resume ---
    ckpt_idx = load_checkpoint(name)
    file_lines = count_output_lines(output)
    start_idx = min(ckpt_idx, file_lines)

    if start_idx > 0:
        rewind = min(workers, start_idx)
        start_idx -= rewind
        truncate_output(output, start_idx)
        save_checkpoint(name, start_idx, total)
        print(f"[{name}] Resuming from {start_idx:,} "
              f"(file had {file_lines:,}, ckpt had {ckpt_idx:,}, "
              f"rewound {rewind})")

    if start_idx >= total:
        print(f"[{name}] Already complete ({total:,} records)")
        return

    remaining = total - start_idx
    clients = make_clients(workers)
    print(f"[{name}] Correcting {remaining:,} records with {workers} workers\n")

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
                cl = clients[i % workers]
                fut = pool.submit(corrector, cl, records[idx])
                futures[fut] = idx
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    batch_results[idx] = future.result()
                except Exception as e:
                    print(f"  [{idx}] FAILED: {e}")
                    batch_failures += 1

        if batch_failures > len(batch) // 2:
            consecutive_failures += 1
            print(f"  ⚠ Batch mostly failed ({batch_failures}/{len(batch)})")
            if consecutive_failures >= 2:
                print(f"  ✖ {consecutive_failures} consecutive failures. "
                      f"Stopping {name}.")
                break
            continue
        else:
            consecutive_failures = 0

        # Write successful results in order
        written = 0
        for idx in batch:
            if idx not in batch_results:
                continue
            mode = "a" if idx > 0 or start_idx > 0 else "w"
            with open(output, mode, encoding="utf-8") as f:
                f.write(json.dumps(
                    batch_results[idx], ensure_ascii=False) + "\n")
            written += 1

        if written == len(batch):
            save_checkpoint(name, batch_end, total)

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
            upload_to_hf(output, dest_repo)
            last_upload = batch_end

    # Final upload
    upload_to_hf(output, dest_repo)
    elapsed = time.time() - t_start
    print(f"\n[{name}] Done! {remaining:,} records in {elapsed/60:.1f} minutes")
    print(f"  Output: {output}\n")


# ---------------------------------------------------------------------------
# Main

def main():
    global ENDPOINT, MODEL_NAME, SOURCE_REPO

    parser = argparse.ArgumentParser(
        description="Darija translation correction pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python correct_vllm.py --list                    # show available files\n"
            "  python correct_vllm.py --dataset goose-math      # correct one file\n"
            "  python correct_vllm.py --dataset all             # correct everything\n"
        ),
    )
    parser.add_argument("--dataset", default="all",
                        help="Dataset name (without _darija.jsonl) or 'all'")
    parser.add_argument("--list", action="store_true",
                        help="List available files in source repo and exit")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Concurrent workers (default: {DEFAULT_WORKERS})")
    parser.add_argument("--upload-every", type=int, default=UPLOAD_EVERY,
                        help=f"Upload every N records (default: {UPLOAD_EVERY})")
    parser.add_argument("--source-repo", default=None,
                        help=f"Source HF repo (default: {SOURCE_REPO})")
    parser.add_argument("--dest-repo", default=None,
                        help="Destination HF repo (default: auto-detect)")
    parser.add_argument("--endpoint", default=None,
                        help=f"vLLM endpoint (default: {ENDPOINT})")
    parser.add_argument("--model", default=None,
                        help=f"Model name (default: {MODEL_NAME})")
    args = parser.parse_args()

    if args.endpoint:
        ENDPOINT = args.endpoint
    if args.model:
        MODEL_NAME = args.model
    if args.source_repo:
        SOURCE_REPO = args.source_repo

    # List available files
    print(f"\n  Source repo: {SOURCE_REPO}")
    files = list_source_files(SOURCE_REPO)

    if not files:
        print("  No _darija.jsonl files found in source repo.")
        return

    # Extract dataset names from filenames (strip _darija.jsonl)
    available = {}
    for f in files:
        name = f.replace("_darija.jsonl", "")
        available[name] = f

    if args.list:
        print(f"\n  Available files ({len(files)}):")
        for name, fname in sorted(available.items()):
            print(f"    {name:30s} → {fname}")
        return

    # Resolve selection
    if args.dataset == "all":
        selected = list(available.keys())
    else:
        if args.dataset not in available:
            print(f"  Dataset '{args.dataset}' not found. Available: "
                  f"{', '.join(sorted(available.keys()))}")
            return
        selected = [args.dataset]

    # Setup
    dest_repo = args.dest_repo or DEST_REPO or get_dest_repo()

    print("=" * 60)
    print("  Darija Translation Correction Pipeline")
    print("=" * 60)
    print(f"\n  Endpoint:    {ENDPOINT}")
    print(f"  Model:       {MODEL_NAME}")
    print(f"  Source:      {SOURCE_REPO}")
    print(f"  Dest:        {dest_repo}")
    print(f"  Datasets:    {', '.join(selected)}")
    print(f"  Workers:     {args.workers}")
    print(f"  Gen:         temp={GEN_PARAMS['temperature']}, "
          f"top_p={GEN_PARAMS['top_p']}")

    print(f"\n  Checking endpoint...")
    if not check_endpoint():
        print("\nERROR: Correction endpoint not reachable. Start it with:")
        print(f"  vllm serve {MODEL_NAME} "
              f"--dtype bfloat16 --max-model-len 8192 --port 8000 "
              f"--tensor-parallel-size 2 --gpu-memory-utilization 0.95")
        return

    ensure_hf_repo(dest_repo)

    t_total = time.time()

    for ds_name in selected:
        filename = available[ds_name]
        print(f"\n{'='*60}")
        print(f"  Correcting: {ds_name} ({filename})")
        print(f"{'='*60}\n")

        # Download from HF
        print(f"  Downloading {filename} from {SOURCE_REPO}...")
        try:
            local_path = download_source_file(SOURCE_REPO, filename)
            print(f"  Downloaded to: {local_path}")
        except Exception as e:
            print(f"  Download failed: {e}")
            continue

        # Load records
        records = load_records(local_path)
        print(f"  Loaded {len(records):,} records")

        if not records:
            print(f"  No records to correct, skipping")
            continue

        # Run correction
        run_correction(ds_name, records, args.workers, args.upload_every,
                       dest_repo)

    elapsed = time.time() - t_total
    print(f"\n{'='*60}")
    print(f"  All done! Total: {elapsed/3600:.1f} hours")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
