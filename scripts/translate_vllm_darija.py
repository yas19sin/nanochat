#!/usr/bin/env python3
"""
Phase 3: Production vLLM batch translation of English -> Moroccan Darija.

Designed for a rented vast.ai / Lambda / RunPod GPU. Streams an HF dataset,
translates with Lyte/tiny-aya-darija-v5, writes parquet shards, uploads to
the HF Hub incrementally, and is resumable if the machine is preempted.

Recommended config (RTX 5090, bench-validated):
    --batch-size 384                # peak throughput ~6900 tok/s (output)
    --max-model-len 4096
    --gpu-mem-util 0.90
    --shard-rows 10000              # ~1 upload every ~3 min at bs=384

Env:
    HF_TOKEN            read token for fineweb-edu + tokenizer
    HF_WRITE_TOKEN      write token for pushing to --repo-id  (falls back to HF_TOKEN)

Example:
    python -m scripts.translate_vllm_darija \
        --model Lyte/tiny-aya-darija-v5 \
        --dataset HuggingFaceFW/fineweb-edu --config sample-10BT \
        --target-tokens 2000000000 \
        --batch-size 384 \
        --shard-rows 10000 \
        --out-dir /workspace/darija_out \
        --repo-id Lyte/fineweb-edu-darija-translated

Resuming: just re-run with the same --out-dir. The script reads
<out-dir>/manifest.json and skips already-processed rows.
"""

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

MAX_INPUT_CHARS_DEFAULT = 1500
MIN_INPUT_CHARS_DEFAULT = 200

SYSTEM_PROMPT = (
    "Translate the following English text into Moroccan Darija (الدارجة المغربية). "
    "Output ONLY the Darija translation."
)


# ------------------------------------------------------------------ manifest
def load_manifest(out_dir: Path):
    mpath = out_dir / "manifest.json"
    if mpath.exists():
        return json.loads(mpath.read_text(encoding="utf-8"))
    return {
        "next_shard_idx": 0,
        "rows_consumed": 0,     # how many source rows we've iterated past
        "rows_accepted": 0,     # how many passed the filter + got written
        "output_tokens": 0,     # total generated tokens
        "shards": [],           # list of {"file","rows","tokens"}
        "started_at": time.time(),
    }


def save_manifest(out_dir: Path, manifest: dict):
    mpath = out_dir / "manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                     encoding="utf-8")


# ------------------------------------------------------------------ streaming
def stream_source(dataset_id, config, split, text_field, skip, hf_token,
                  min_chars, max_chars):
    """Yield (idx, english_text). `idx` counts ALL rows seen (skipped + kept)."""
    from datasets import load_dataset
    ds = load_dataset(dataset_id, name=config, split=split,
                      streaming=True, token=hf_token)
    idx = 0
    for row in ds:
        if idx < skip:
            idx += 1
            continue
        text = (row.get(text_field) or "").strip()
        idx += 1
        if not (min_chars <= len(text) <= max_chars * 8):  # prefilter huge rows
            continue
        yield idx, text[:max_chars]


# ------------------------------------------------------------------ filtering
def keep_translation(en: str, darija: str, min_ratio: float, max_ratio: float):
    """Basic sanity filter. Returns (keep: bool, reason: str)."""
    if not darija or len(darija) < 20:
        return False, "empty"
    ratio = len(darija) / max(len(en), 1)
    if ratio < min_ratio or ratio > max_ratio:
        return False, f"ratio={ratio:.2f}"
    # require at least some Arabic script; ignore latinized Darija to keep quality consistent
    arabic_chars = sum(1 for c in darija if "\u0600" <= c <= "\u06FF")
    if arabic_chars < 10:
        return False, "no-arabic"
    return True, "ok"


# ------------------------------------------------------------------ shard io
def write_shard(out_dir: Path, shard_idx: int, rows: list) -> dict:
    """Write rows to parquet. Returns shard manifest entry."""
    import pandas as pd
    fname = f"shard_{shard_idx:05d}.parquet"
    fpath = out_dir / fname
    df = pd.DataFrame(rows)
    df.to_parquet(fpath, index=False)
    tokens = int(df["output_tokens"].sum())
    return {"file": fname, "rows": len(rows), "tokens": tokens}


def _upload_file_worker(queue, hf_write_token: str, path_or_fileobj: str,
                        path_in_repo: str, repo_id: str,
                        commit_message: str):
    """Run one Hub upload in a child process.

    The parent may kill this process on timeout. Keep this function top-level so
    multiprocessing "spawn" can import it safely after CUDA/vLLM is initialized.
    """
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=hf_write_token)
        api.upload_file(
            path_or_fileobj=path_or_fileobj,
            path_in_repo=path_in_repo,
            repo_id=repo_id,
            repo_type="dataset",
            commit_message=commit_message,
        )
        queue.put({"ok": True})
    except BaseException as exc:  # pragma: no cover - network paths
        queue.put({"err": repr(exc)})


def _upload_with_retry(*, hf_write_token, path_or_fileobj, path_in_repo, repo_id,
                       commit_message, max_attempts: int = 4,
                       timeout_s: int = 300):
    """Upload a single file with bounded retries and a hard wall-clock timeout.

    Uses a child process so a stuck TCP socket (the hf_transfer hang we saw on
    shard_00198) cannot outlive the retry attempt or crash Python shutdown.
    """
    import multiprocessing as mp

    last_exc: Exception | None = None
    path_str = os.fspath(path_or_fileobj)
    ctx = mp.get_context("spawn")
    for attempt in range(1, max_attempts + 1):
        queue = ctx.Queue()
        proc = ctx.Process(
            target=_upload_file_worker,
            args=(
                queue,
                hf_write_token,
                path_str,
                path_in_repo,
                repo_id,
                commit_message,
            ),
        )
        proc.start()
        proc.join(timeout_s)
        if proc.is_alive():
            last_exc = TimeoutError(
                f"upload of {path_in_repo} stalled > {timeout_s}s "
                f"(attempt {attempt}/{max_attempts})"
            )
            proc.terminate()
            proc.join(10)
            if proc.is_alive():
                proc.kill()
                proc.join()
        else:
            try:
                result = queue.get(timeout=1)
            except Exception:
                result = {}
            if result.get("ok"):
                queue.close()
                queue.join_thread()
                return
            err = result.get("err")
            if err:
                last_exc = RuntimeError(err)
            else:
                last_exc = RuntimeError(
                    f"upload worker exited {proc.exitcode} without success"
                )
        queue.close()
        queue.join_thread()
        backoff = min(30, 5 * attempt)
        print(f"    !! upload retry {attempt}/{max_attempts} for {path_in_repo}: "
              f"{last_exc}; sleeping {backoff}s")
        time.sleep(backoff)
    raise last_exc  # type: ignore[misc]


def upload_shard(repo_id: str, out_dir: Path, entry: dict, hf_write_token: str):
    """Push a single shard file + manifest to the HF dataset repo.

    Each upload is wrapped in a child process with a 5 min timeout and 4
    retries so a silent socket stall can't block the run forever. On permanent
    failure we log and continue; resume sync will retry missing Hub files.
    """
    try:
        _upload_with_retry(
            hf_write_token=hf_write_token,
            path_or_fileobj=out_dir / entry["file"],
            path_in_repo=f"data/{entry['file']}",
            repo_id=repo_id,
            commit_message=f"add {entry['file']} ({entry['rows']} rows, {entry['tokens']} toks)",
        )
        _upload_with_retry(
            hf_write_token=hf_write_token,
            path_or_fileobj=out_dir / "manifest.json",
            path_in_repo="manifest.json",
            repo_id=repo_id,
            commit_message=f"manifest after {entry['file']}",
        )
        print(f"    uploaded {entry['file']} -> {repo_id}")
    except Exception as exc:
        print(f"    !! upload failed ({exc}); continuing, will retry on next shard")


def ensure_repo(repo_id: str, hf_write_token: str):
    from huggingface_hub import HfApi
    api = HfApi(token=hf_write_token)
    api.create_repo(repo_id=repo_id, repo_type="dataset",
                    exist_ok=True, private=False)


def sync_manifest_shards(repo_id: str, out_dir: Path, manifest: dict,
                         hf_write_token: str):
    """Upload local manifest shards that are missing from the Hub repo."""
    shards = manifest.get("shards") or []
    if not shards:
        return

    try:
        from huggingface_hub import HfApi
        api = HfApi(token=hf_write_token)
        remote_files = set(api.list_repo_files(repo_id=repo_id,
                                               repo_type="dataset"))
    except Exception as exc:
        print(f"[hub] could not list repo files for sync ({exc}); continuing")
        return

    missing = [
        entry for entry in shards
        if f"data/{entry['file']}" not in remote_files
    ]
    if not missing:
        print("[hub] all manifest shards are present on the Hub")
        if "manifest.json" not in remote_files:
            print("[hub] manifest.json missing on Hub; uploading local manifest")
            _upload_with_retry(
                hf_write_token=hf_write_token,
                path_or_fileobj=out_dir / "manifest.json",
                path_in_repo="manifest.json",
                repo_id=repo_id,
                commit_message="sync manifest",
            )
        return

    print(f"[hub] found {len(missing)} manifest shard(s) missing on Hub; syncing")
    for entry in missing:
        local_path = out_dir / entry["file"]
        if not local_path.exists():
            print(f"    !! missing local shard {local_path}; cannot upload")
            continue
        upload_shard(repo_id, out_dir, entry, hf_write_token)


# ------------------------------------------------------------------ main
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="Lyte/tiny-aya-darija-v5")
    p.add_argument("--dataset", default="HuggingFaceFW/fineweb-edu")
    p.add_argument("--config", default="sample-10BT")
    p.add_argument("--split", default="train")
    p.add_argument("--text-field", default="text")
    p.add_argument("--target-tokens", type=int, default=2_000_000_000)
    p.add_argument("--batch-size", type=int, default=384)
    p.add_argument("--max-new-tokens", type=int, default=512)
    p.add_argument("--max-model-len", type=int, default=4096)
    p.add_argument("--gpu-mem-util", type=float, default=0.90)
    p.add_argument("--enforce-eager", action="store_true")
    p.add_argument("--shard-rows", type=int, default=10_000)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--repo-id", default=None,
                   help="HF dataset repo to push shards to; omit to skip upload")
    p.add_argument("--min-input-chars", type=int, default=MIN_INPUT_CHARS_DEFAULT)
    p.add_argument("--max-input-chars", type=int, default=MAX_INPUT_CHARS_DEFAULT)
    p.add_argument("--min-ratio", type=float, default=0.4)
    p.add_argument("--max-ratio", type=float, default=3.0)
    p.add_argument("--progress-every", type=int, default=10,
                   help="print throughput stats every N batches")
    args = p.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    hf_token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    hf_write_token = os.environ.get("HF_WRITE_TOKEN") or hf_token
    if not hf_token:
        sys.exit("ERROR: HF_TOKEN not set")

    if args.repo_id:
        print(f"[hub] ensuring dataset repo exists: {args.repo_id}")
        ensure_repo(args.repo_id, hf_write_token)

    # ------------------------------------------------------- resume state
    manifest = load_manifest(out_dir)
    rows_consumed_start = manifest["rows_consumed"]
    tokens_start = manifest["output_tokens"]
    shard_idx = manifest["next_shard_idx"]
    print(f"[resume] skipping {rows_consumed_start} source rows; "
          f"{tokens_start:,} tokens already done; next shard idx = {shard_idx}")

    if args.repo_id:
        sync_manifest_shards(args.repo_id, out_dir, manifest, hf_write_token)

    if tokens_start >= args.target_tokens:
        print("[done] target token count already satisfied; no translation needed")
        return

    # ------------------------------------------------------- load vLLM
    print(f"[model] loading {args.model}...")
    t0 = time.time()
    from vllm import LLM, SamplingParams
    from transformers import AutoTokenizer
    llm = LLM(
        model=args.model,
        dtype="bfloat16",
        max_model_len=args.max_model_len,
        gpu_memory_utilization=args.gpu_mem_util,
        enforce_eager=args.enforce_eager,
        trust_remote_code=True,
    )
    tokenizer = AutoTokenizer.from_pretrained(args.model, token=hf_token)
    sampling_params = SamplingParams(
        temperature=0.3, top_p=0.98, top_k=300,
        repetition_penalty=1.15,
        max_tokens=args.max_new_tokens,
    )
    print(f"[model] ready in {time.time()-t0:.1f}s")

    # ------------------------------------------------------- signal handler
    stop_requested = {"v": False}

    def _stop(signum, frame):
        print(f"\n[signal] got {signum}, will flush after current batch")
        stop_requested["v"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # ------------------------------------------------------- main loop
    shard_buffer: list[dict] = []
    stream = stream_source(
        args.dataset, args.config, args.split, args.text_field,
        skip=rows_consumed_start, hf_token=hf_token,
        min_chars=args.min_input_chars, max_chars=args.max_input_chars,
    )

    batch_english: list[str] = []
    batch_src_idx: list[int] = []
    batches_done = 0
    t_loop = time.time()
    tokens_this_session = 0
    rows_consumed = rows_consumed_start
    total_tokens = tokens_start

    def flush_shard(final: bool = False):
        nonlocal shard_idx
        if not shard_buffer:
            return
        entry = write_shard(out_dir, shard_idx, shard_buffer)
        manifest["shards"].append(entry)
        manifest["next_shard_idx"] = shard_idx + 1
        manifest["rows_consumed"] = rows_consumed
        manifest["rows_accepted"] = manifest.get("rows_accepted", 0) + entry["rows"]
        manifest["output_tokens"] = total_tokens
        save_manifest(out_dir, manifest)
        print(f"  wrote shard_{shard_idx:05d}.parquet "
              f"({entry['rows']} rows, {entry['tokens']:,} tokens)"
              + (" [FINAL]" if final else ""))
        if args.repo_id:
            upload_shard(args.repo_id, out_dir, entry, hf_write_token)
        shard_buffer.clear()
        shard_idx += 1

    def run_batch_and_store():
        nonlocal total_tokens, tokens_this_session
        if not batch_english:
            return
        prompts = [
            tokenizer.apply_chat_template(
                [{"role": "system", "content": SYSTEM_PROMPT},
                 {"role": "user", "content": en}],
                tokenize=False, add_generation_prompt=True,
            )
            for en in batch_english
        ]
        outputs = llm.generate(prompts, sampling_params, use_tqdm=False)
        for src_idx, en, out in zip(batch_src_idx, batch_english, outputs):
            gen = out.outputs[0]
            dar = gen.text.strip()
            keep, reason = keep_translation(en, dar, args.min_ratio, args.max_ratio)
            if not keep:
                continue
            n_toks = len(gen.token_ids)
            shard_buffer.append({
                "src_idx": src_idx,
                "en": en,
                "darija": dar,
                "output_tokens": n_toks,
            })
            total_tokens += n_toks
            tokens_this_session += n_toks
        batch_english.clear()
        batch_src_idx.clear()

    try:
        for src_idx, en in stream:
            rows_consumed = src_idx
            batch_english.append(en)
            batch_src_idx.append(src_idx)

            if len(batch_english) >= args.batch_size:
                run_batch_and_store()
                batches_done += 1

                if batches_done % args.progress_every == 0:
                    elapsed = time.time() - t_loop
                    tps = tokens_this_session / max(elapsed, 1e-6)
                    frac = total_tokens / args.target_tokens
                    eta_s = (args.target_tokens - total_tokens) / max(tps, 1)
                    print(f"  [{batches_done:>5} batches] "
                          f"tokens={total_tokens:>11,} ({frac*100:.1f}%)  "
                          f"tok/s={tps:>6.0f}  "
                          f"eta={eta_s/3600:.1f}h  "
                          f"buf={len(shard_buffer)}/{args.shard_rows}")

                if len(shard_buffer) >= args.shard_rows:
                    flush_shard()

                if total_tokens >= args.target_tokens:
                    print("[done] hit target token count")
                    break
                if stop_requested["v"]:
                    print("[stop] flushing and exiting")
                    break
    except KeyboardInterrupt:
        print("[kbint] flushing and exiting")

    # final flush (partial batch + partial shard)
    run_batch_and_store()
    flush_shard(final=True)

    elapsed = time.time() - t_loop
    print(f"\n[finish] elapsed {elapsed/60:.1f} min  "
          f"tokens this session {tokens_this_session:,}  "
          f"total {total_tokens:,}  "
          f"rows consumed {rows_consumed:,}")


if __name__ == "__main__":
    main()
