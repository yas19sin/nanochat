"""
A/B translation comparison: LM Studio Q8 GGUF vs HF Space bf16 full precision.

Pulls N random English docs, translates each through BOTH backends with
identical sampling params, writes side-by-side markdown for eyeball review.

Backends:
  A) LM Studio (localhost:1234/v1)  -> Q8 GGUF, OpenAI-compatible API
  B) Gradio Space Lyte/Moroccan-Darija-Translator -> bf16 original

Usage:
  $env:HF_TOKEN = "..."
  .\\.venv\\Scripts\\python.exe dev/probe_q8_vs_bf16.py --n 15
"""

import argparse
import os
import random
import time

from datasets import load_dataset
from openai import OpenAI
from gradio_client import Client

MAX_INPUT_CHARS = 1200
DIRECTION = "🇬🇧 English → 🇲🇦 Darija"

SYSTEM_PROMPT = (
    "Translate the following English text into Moroccan Darija (الدارجة المغربية). "
    "Output ONLY the Darija translation."
)

SOURCES = [
    ("fineweb-edu", "HuggingFaceFW/fineweb-edu", "sample-10BT", "train", "text", 500),
    ("finephrase/faq", "HuggingFaceFW/finephrase", "faq", "train", "text", 500),
    ("finephrase/tutorial", "HuggingFaceFW/finephrase", "tutorial", "train", "text", 500),
]


def reservoir_sample(stream, k, rng, reservoir_size):
    pool = []
    for i, row in enumerate(stream):
        if i >= reservoir_size:
            break
        pool.append(row)
    rng.shuffle(pool)
    return pool[:k]


def collect_samples(n_per_source, rng, hf_token):
    samples = []
    for label, ds_id, config, split, field, reservoir_size in SOURCES:
        print(f"[data] {label}: streaming...")
        ds = load_dataset(ds_id, name=config, split=split,
                          streaming=True, token=hf_token)
        picks = reservoir_sample(ds, n_per_source, rng, reservoir_size)
        for row in picks:
            text = (row.get(field) or "").strip()
            if 200 <= len(text) <= MAX_INPUT_CHARS * 8:
                samples.append((label, text[:MAX_INPUT_CHARS]))
    rng.shuffle(samples)
    return samples


# ---------------------------------------------------------------- backend A
def translate_lmstudio(client, text, model_name):
    t0 = time.time()
    try:
        resp = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0.3,
            top_p=0.98,
            max_tokens=512,
            extra_body={"top_k": 300, "repetition_penalty": 1.15},
        )
        out = resp.choices[0].message.content.strip()
        return out, time.time() - t0, None
    except Exception as exc:  # noqa: BLE001
        return "", time.time() - t0, str(exc)


# ---------------------------------------------------------------- backend B
def extract_assistant_text(chat_history):
    for msg in reversed(chat_history):
        if msg.get("role") == "assistant":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [it.get("text", "") for it in content
                         if isinstance(it, dict) and it.get("type") == "text"]
                return "".join(parts).strip()
    return ""


def translate_space(client, text):
    t0 = time.time()
    try:
        try:
            client.predict(api_name="/clear_chat")
        except Exception:
            pass
        result = client.predict(
            message=text,
            direction=DIRECTION,
            custom_prompt=text,
            max_new_tokens=512,
            temperature=0.3,
            top_k=300,
            top_p=0.98,
            repetition_penalty=1.15,
            api_name="/chat_fn",
        )
        chat_history = result[0] if isinstance(result, (list, tuple)) else result
        return extract_assistant_text(chat_history), time.time() - t0, None
    except Exception as exc:  # noqa: BLE001
        return "", time.time() - t0, str(exc)


# ---------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=15, help="total samples across sources")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--lmstudio-url", default="http://127.0.0.1:11434/v1")
    p.add_argument("--lmstudio-model", default="tiny-aya-darija-v5")
    p.add_argument("--space", default="Lyte/Moroccan-Darija-Translator")
    p.add_argument("--hf-space-token", default=None,
                   help="HF token for private Space access (defaults to HF_TOKEN)")
    p.add_argument("--out", default="dev/q8_vs_bf16_probe.md")
    args = p.parse_args()

    hf_token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    space_token = args.hf_space_token or hf_token
    rng = random.Random(args.seed)

    # split samples roughly equally across sources
    n_per = max(1, args.n // len(SOURCES))
    samples = collect_samples(n_per, rng, hf_token)[:args.n]
    print(f"[data] collected {len(samples)} samples")

    lm = OpenAI(base_url=args.lmstudio_url, api_key="lm-studio")
    print(f"[space] connecting to {args.space}...")
    space = Client(args.space, token=space_token)
    print("[space] ready")

    rows = []
    for i, (source, text) in enumerate(samples, 1):
        print(f"\n[{i}/{len(samples)}] ({source}) len={len(text)}")
        print("  A. LM Studio Q8...")
        q8_out, q8_t, q8_err = translate_lmstudio(lm, text, args.lmstudio_model)
        print(f"     {q8_t:.1f}s  len={len(q8_out)}" + (f"  ERR={q8_err}" if q8_err else ""))
        print("  B. HF Space bf16...")
        bf_out, bf_t, bf_err = translate_space(space, text)
        print(f"     {bf_t:.1f}s  len={len(bf_out)}" + (f"  ERR={bf_err}" if bf_err else ""))
        rows.append({
            "idx": i, "source": source, "en": text,
            "q8": q8_out, "q8_s": q8_t, "q8_err": q8_err,
            "bf": bf_out, "bf_s": bf_t, "bf_err": bf_err,
        })

    # --- write markdown ---
    out_path = args.out
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    lines = []
    lines.append(f"# Q8 GGUF vs bf16 translation probe\n")
    lines.append(f"Seed: {args.seed}  |  Samples: {len(rows)}  |  "
                 f"LM Studio: `{args.lmstudio_model}`  |  Space: `{args.space}`\n")
    q8_total = sum(r["q8_s"] for r in rows)
    bf_total = sum(r["bf_s"] for r in rows)
    lines.append(f"**Latency:** Q8 total {q8_total:.1f}s "
                 f"(avg {q8_total/len(rows):.1f}s/req), "
                 f"bf16 total {bf_total:.1f}s "
                 f"(avg {bf_total/len(rows):.1f}s/req)\n")
    for r in rows:
        lines.append(f"\n## {r['idx']}. `{r['source']}` — en len {len(r['en'])}\n")
        lines.append(f"**English:**\n\n> {r['en']}\n")
        lines.append(f"\n**A — Q8 GGUF** ({r['q8_s']:.1f}s, len {len(r['q8'])})\n\n")
        lines.append(f"> {r['q8'] or r['q8_err']}\n")
        lines.append(f"\n**B — bf16 full precision** ({r['bf_s']:.1f}s, len {len(r['bf'])})\n\n")
        lines.append(f"> {r['bf'] or r['bf_err']}\n")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n[done] wrote {out_path}")


if __name__ == "__main__":
    main()
