#!/usr/bin/env python3
"""
Phase 1: 1-hour benchmark for Lyte/tiny-aya-darija-v5 on a rented GPU.

Goal: measure real output tokens/sec at various batch sizes, then report
the sweet-spot config for the production translation run. Prints a small
sample of outputs so the user can eyeball quality matches the local probe.

Run on a rented RTX 5090 / 4090 / A100 / H100 vast.ai box:

  # 1) clone the repo and install deps
  apt-get update -y && apt-get install -y git
  git clone https://github.com/<your_fork>/nanochat && cd nanochat
  pip install vllm==0.7.3 datasets huggingface_hub pandas pyarrow

  # 2) export tokens
  export HF_TOKEN=hf_xxx
  export HUGGINGFACE_HUB_TOKEN=$HF_TOKEN

  # 3) run benchmark (~30-40 min)
  python -m scripts.bench_translate_vllm \
      --model Lyte/tiny-aya-darija-v5 \
      --dataset HuggingFaceFW/fineweb-edu --config sample-10BT \
      --n-samples 200 \
      --batches 8,16,32,64 \
      --out bench_results.json

  # 4) paste bench_results.json back to assistant

The script:
  - loads the model once
  - for each batch size, runs N prompts in lockstep and measures wall time
  - prints output tok/s, prefill time, sampling time split
  - reports VRAM peak, any OOM / error events
  - saves sample outputs for quality spot-check
"""

import argparse
import json
import os
import time
import traceback
from pathlib import Path

MAX_INPUT_CHARS = 1200   # same clamp as the local probe
N_SAMPLE_OUTPUTS = 6      # outputs to save per batch size for quality review

SYSTEM_PROMPT = (
    "Translate the following English text into Moroccan Darija (الدارجة المغربية). "
    "Output ONLY the Darija translation."
)


def build_prompts(tokenizer, english_texts):
    """Apply the model's chat template to each English snippet."""
    prompts = []
    for text in english_texts:
        msgs = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text[:MAX_INPUT_CHARS]},
        ]
        prompt = tokenizer.apply_chat_template(
            msgs, tokenize=False, add_generation_prompt=True
        )
        prompts.append(prompt)
    return prompts


def load_samples(dataset_id, config, split, text_field, n, token):
    """Stream n samples from the dataset."""
    from datasets import load_dataset
    print(f"[data] streaming {n} samples from {dataset_id}/{config}...")
    ds = load_dataset(dataset_id, name=config, split=split,
                      streaming=True, token=token)
    samples = []
    for row in ds:
        text = (row.get(text_field) or "").strip()
        if len(text) < 200:  # skip junk/empty
            continue
        samples.append(text)
        if len(samples) >= n:
            break
    print(f"[data] got {len(samples)} samples, avg {sum(len(s) for s in samples)/len(samples):.0f} chars")
    return samples


def run_batch(llm, sampling_params, prompts, batch_size):
    """Run prompts in fixed-size chunks and measure throughput.

    Returns (total_output_tokens, wall_time_s, sample_outputs)."""
    total_out_tokens = 0
    sample_outputs = []
    t0 = time.time()

    # vLLM handles its own batching internally, but we still submit in groups
    # of `batch_size` to emulate how the production script will stream in.
    for start in range(0, len(prompts), batch_size):
        chunk = prompts[start:start + batch_size]
        outputs = llm.generate(chunk, sampling_params, use_tqdm=False)
        for out in outputs:
            gen = out.outputs[0]
            total_out_tokens += len(gen.token_ids)
            if len(sample_outputs) < N_SAMPLE_OUTPUTS:
                sample_outputs.append({
                    "prompt_chars": len(out.prompt),
                    "output_tokens": len(gen.token_ids),
                    "text": gen.text[:800],
                })

    wall = time.time() - t0
    return total_out_tokens, wall, sample_outputs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="Lyte/tiny-aya-darija-v5")
    parser.add_argument("--dataset", default="HuggingFaceFW/fineweb-edu")
    parser.add_argument("--config", default="sample-10BT")
    parser.add_argument("--split", default="train")
    parser.add_argument("--text-field", default="text")
    parser.add_argument("--n-samples", type=int, default=200,
                        help="prompts used per batch-size run")
    parser.add_argument("--batches", default="8,16,32,64",
                        help="comma-separated batch sizes to test")
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument("--gpu-mem-util", type=float, default=0.90)
    parser.add_argument("--enforce-eager", action="store_true",
                        help="Disable torch.compile + CUDA graphs (use when FA2 PTX mismatches the driver).")
    parser.add_argument("--max-model-len", type=int, default=4096)
    parser.add_argument("--out", default="bench_results.json")
    args = parser.parse_args()

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    if not token:
        print("WARN: no HF_TOKEN in env")

    # --- load vLLM + tokenizer ---
    from vllm import LLM, SamplingParams
    from transformers import AutoTokenizer

    print(f"[model] loading {args.model}...")
    t0 = time.time()
    llm = LLM(
        model=args.model,
        dtype="bfloat16",
        max_model_len=args.max_model_len,
        gpu_memory_utilization=args.gpu_mem_util,
        enforce_eager=args.enforce_eager,
        download_dir=os.environ.get("HF_HUB_CACHE"),
        trust_remote_code=True,
    )
    load_time = time.time() - t0
    print(f"[model] loaded in {load_time:.1f}s")

    tokenizer = AutoTokenizer.from_pretrained(args.model, token=token)

    sampling_params = SamplingParams(
        temperature=0.3,
        top_p=0.98,
        top_k=300,
        repetition_penalty=1.15,
        max_tokens=args.max_new_tokens,
    )

    # --- pick enough samples for the largest batch run ---
    samples = load_samples(args.dataset, args.config, args.split,
                           args.text_field, args.n_samples, token)
    prompts = build_prompts(tokenizer, samples)

    # --- warmup (don't count toward throughput) ---
    print("[warmup] running 8 prompts to warm kernels...")
    t0 = time.time()
    llm.generate(prompts[:8], sampling_params, use_tqdm=False)
    print(f"[warmup] done in {time.time()-t0:.1f}s")

    # --- benchmark each batch size ---
    batch_sizes = [int(b) for b in args.batches.split(",")]
    results = {
        "model": args.model,
        "dataset": f"{args.dataset}/{args.config}",
        "n_samples_per_run": args.n_samples,
        "max_new_tokens": args.max_new_tokens,
        "max_model_len": args.max_model_len,
        "gpu_mem_util": args.gpu_mem_util,
        "load_time_s": round(load_time, 1),
        "runs": [],
    }

    for bs in batch_sizes:
        print(f"\n=== batch_size={bs} ===")
        try:
            total_out_tokens, wall, sample_outs = run_batch(
                llm, sampling_params, prompts, bs,
            )
            tok_per_s = total_out_tokens / max(wall, 1e-6)
            est_tokens_per_usd = tok_per_s * 3600 / 0.50   # at $0.50/hr
            result = {
                "batch_size": bs,
                "output_tokens": total_out_tokens,
                "wall_time_s": round(wall, 1),
                "tok_per_s": round(tok_per_s, 1),
                "estimate_tok_per_usd_at_0.50hr": int(est_tokens_per_usd),
                "estimate_hours_for_2B_tokens": round(2_000_000_000 / tok_per_s / 3600, 1),
                "samples": sample_outs,
            }
            print(f"  output_tokens: {total_out_tokens}")
            print(f"  wall:          {wall:.1f}s")
            print(f"  THROUGHPUT:    {tok_per_s:.0f} tok/s")
            print(f"  2B tokens ETA: {result['estimate_hours_for_2B_tokens']} hrs")
        except Exception as exc:  # noqa: BLE001
            print(f"  !! FAILED at batch={bs}: {exc}")
            traceback.print_exc()
            result = {"batch_size": bs, "error": str(exc)}
        results["runs"].append(result)

        # give GPU a sec to settle between runs
        time.sleep(2)

    # --- peak VRAM (best-effort) ---
    try:
        import torch
        results["peak_vram_gb"] = round(
            torch.cuda.max_memory_allocated() / 1e9, 2
        )
    except Exception:  # noqa: BLE001
        pass

    Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False),
                              encoding="utf-8")
    print(f"\nWrote: {args.out}")
    print("\n=== SUMMARY ===")
    for r in results["runs"]:
        if "error" in r:
            print(f"  bs={r['batch_size']:>3}  FAILED: {r['error']}")
        else:
            print(f"  bs={r['batch_size']:>3}  {r['tok_per_s']:>6} tok/s  "
                  f"2B-token ETA: {r['estimate_hours_for_2B_tokens']:>5} hrs")


if __name__ == "__main__":
    main()
