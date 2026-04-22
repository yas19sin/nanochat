"""
Random-sample translation probe. Pulls N random docs from:
  - HuggingFaceFW/fineweb-edu (sample-10BT config, pure English)
  - HuggingFaceFW/finephrase (faq + tutorial subsets, uses 'text' field only)

Translates each via Lyte/Moroccan-Darija-Translator Gradio Space (EN -> Darija),
clearing chat state between calls. Writes side-by-side markdown for review.

Usage:
  $env:HF_TOKEN = "..."
  .\.venv\Scripts\python.exe -m scripts.probe_random_translate --n 5

Output: dev/random_translation_probe.md
"""

import os
import argparse
import random
import time
from datasets import load_dataset

MAX_INPUT_CHARS = 1200
# Number of docs to skim from streaming before taking the N-th random pick.
# Random draws from a streaming dataset means "skip a random number of rows".
MAX_SKIP = 500

SOURCES = [
    # (label, dataset_id, config, split, text_field, reservoir_size)
    ("fineweb-edu", "HuggingFaceFW/fineweb-edu", "sample-10BT", "train", "text", 500),
    ("finephrase/faq", "HuggingFaceFW/finephrase", "faq", "train", "text", 500),
    ("finephrase/tutorial", "HuggingFaceFW/finephrase", "tutorial", "train", "text", 500),
]


def extract_assistant_text(chat_history):
    for msg in reversed(chat_history):
        if msg.get("role") == "assistant":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(item.get("text", ""))
                return "".join(parts).strip()
    return "<no assistant reply>"


def translate(client, text, direction="🇬🇧 English → 🇲🇦 Darija"):
    text = text[:MAX_INPUT_CHARS]
    try:
        try:
            client.predict(api_name="/clear_chat")
        except Exception:
            pass
        result = client.predict(
            message=text,
            direction=direction,
            custom_prompt=text,
            max_new_tokens=512,
            temperature=0.3,
            top_k=300,
            top_p=0.98,
            repetition_penalty=1.15,
            api_name="/chat_fn",
        )
        chat_history = result[0] if isinstance(result, (list, tuple)) else result
        return extract_assistant_text(chat_history)
    except Exception as exc:  # noqa: BLE001
        return f"<translation error: {exc}>"


def reservoir_sample(stream, k, rng, reservoir_size):
    """Simple reservoir over the first `reservoir_size` docs of the stream.
    We cap it because the fineweb-edu stream is huge and we just want
    uncorrelated samples, not a uniform sample of the whole dataset."""
    pool = []
    for i, row in enumerate(stream):
        if i >= reservoir_size:
            break
        pool.append(row)
    rng.shuffle(pool)
    return pool[:k]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=5,
                        help="random samples per source")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=str,
                        default="dev/random_translation_probe.md")
    args = parser.parse_args()

    try:
        from gradio_client import Client
    except ImportError:
        raise SystemExit(
            "gradio_client not installed. Run: .\\.venv\\Scripts\\pip install gradio_client")

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    rng = random.Random(args.seed)

    print("Connecting to Lyte/Moroccan-Darija-Translator...")
    try:
        client = Client("Lyte/Moroccan-Darija-Translator", hf_token=token)
    except TypeError:
        client = Client("Lyte/Moroccan-Darija-Translator")
    print("Connected.")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    report = [
        "# random translation probe (fineweb-edu + finephrase subsets)",
        "",
        f"Samples per source: **{args.n}**. Seed: {args.seed}. "
        f"Input clamped to {MAX_INPUT_CHARS} chars. "
        f"Direction: English -> Darija via `Lyte/tiny-aya-darija-v5`.",
        "",
    ]

    for label, ds_id, cfg, split, text_field, reservoir_size in SOURCES:
        print(f"\n=== {label} ===")
        report.append(f"## source: `{label}`\n")
        try:
            ds = load_dataset(ds_id, name=cfg, split=split,
                              streaming=True, token=token)
        except Exception as exc:  # noqa: BLE001
            msg = f"!! failed to load {ds_id}/{cfg}: {exc}"
            print(msg)
            report.append(f"> {msg}\n")
            continue

        # Skip a small random offset so we aren't always sampling shard 0 head.
        skip = rng.randint(0, MAX_SKIP)
        print(f"  skipping first {skip} rows then reservoir-sampling {reservoir_size}")
        it = iter(ds)
        for _ in range(skip):
            try:
                next(it)
            except StopIteration:
                break

        try:
            picks = reservoir_sample(it, args.n, rng, reservoir_size)
        except Exception as exc:  # noqa: BLE001
            msg = f"!! sampling failed: {exc}"
            print(msg)
            report.append(f"> {msg}\n")
            continue

        for i, row in enumerate(picks):
            source = (row.get(text_field) or "").strip()
            if not source:
                continue
            print(f"  [{label} #{i}] translating ({len(source)} chars)...")
            t0 = time.time()
            translation = translate(client, source)
            dt = time.time() - t0
            report.append(f"### {label} #{i}  ({dt:.1f}s, {len(source)} src chars)")
            report.append("**English source:**\n")
            report.append("```\n" + source[:MAX_INPUT_CHARS] + "\n```\n")
            report.append("**Darija translation:**\n")
            report.append("```\n" + translation + "\n```\n")
            report.append("---\n")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(report))
    print(f"\nWrote: {args.out}")


if __name__ == "__main__":
    main()
