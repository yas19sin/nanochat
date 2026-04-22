"""
Probe how well Lyte/Moroccan-Darija-Translator (Lyte/tiny-aya-darija-v5) handles
the different finephrase subsets. Samples N rows per subset, translates both the
source English ('text') and the synthetic rewrite ('rollout_results[0].text'),
and saves side-by-side outputs for manual quality review.

Usage:
  $env:HF_TOKEN = "..."
  .\.venv\Scripts\python.exe -m scripts.probe_translate_finephrase --n 3

Output: dev/finephrase_translation_probe.md (pretty markdown for review)
"""

import os
import argparse
import json
import time
from datasets import load_dataset

SUBSETS = ("faq", "math", "table", "tutorial")

# clamp input length so the gradio endpoint doesn't time out
MAX_INPUT_CHARS = 1200


def extract_assistant_text(chat_history):
    """The /chat_fn endpoint returns (chat_history, input_text, ...).
    chat_history is a list of dicts with role+content. We want the last
    assistant message's text."""
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
        # Reset the Space's chat history so prior calls don't bleed into this one.
        # /chat_fn is a chatbot endpoint — without clearing, each sample is treated
        # as a follow-up turn of the previous conversation.
        try:
            client.predict(api_name="/clear_chat")
        except Exception:
            pass
        result = client.predict(
            message=text,
            direction=direction,
            custom_prompt=text,  # ignored for preset directions
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=3,
                        help="samples per subset")
    parser.add_argument("--out", type=str,
                        default="dev/finephrase_translation_probe.md")
    parser.add_argument("--only", type=str, default=None,
                        help="comma-separated subset names to probe (default: all)")
    args = parser.parse_args()

    try:
        from gradio_client import Client
    except ImportError:
        raise SystemExit(
            "gradio_client not installed. Run: .\\.venv\\Scripts\\pip install gradio_client")

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    if not token:
        print("WARN: no HF_TOKEN; private/gated datasets will fail")

    print("Connecting to Lyte/Moroccan-Darija-Translator...")
    try:
        client = Client("Lyte/Moroccan-Darija-Translator", hf_token=token)
    except TypeError:
        # older gradio_client versions take the token positionally
        client = Client("Lyte/Moroccan-Darija-Translator")
    print("Connected.")

    subsets = SUBSETS if not args.only else tuple(s.strip() for s in args.only.split(","))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    report = ["# finephrase translation probe (Lyte/tiny-aya-darija-v5)",
              "",
              f"Samples per subset: **{args.n}**. "
              f"Direction: English -> Darija. "
              f"Input clamped to {MAX_INPUT_CHARS} chars.",
              ""]

    for subset in subsets:
        print(f"\n=== subset: {subset} ===")
        report.append(f"## subset: `{subset}`\n")
        try:
            ds = load_dataset(
                "HuggingFaceFW/finephrase",
                name=subset,
                split="train",
                streaming=True,
                token=token,
            )
        except Exception as exc:  # noqa: BLE001
            msg = f"!! failed to load subset '{subset}': {exc}"
            print(msg)
            report.append(f"> {msg}\n")
            continue

        it = iter(ds)
        for i in range(args.n):
            try:
                row = next(it)
            except StopIteration:
                break

            source = (row.get("text") or "").strip()
            rollout = ""
            rr = row.get("rollout_results")
            if isinstance(rr, list) and rr:
                rollout = (rr[0].get("text") or "").strip()

            print(f"  [{subset} #{i}] translating source ({len(source)} chars)...")
            t0 = time.time()
            dar_source = translate(client, source) if source else "<empty source>"
            dt_s = time.time() - t0

            print(f"  [{subset} #{i}] translating rollout ({len(rollout)} chars)...")
            t0 = time.time()
            dar_rollout = translate(client, rollout) if rollout else "<empty rollout>"
            dt_r = time.time() - t0

            report.append(f"### {subset} #{i}")
            report.append(f"**source (EN, {len(source)} chars):**\n")
            report.append("```\n" + source[:MAX_INPUT_CHARS] + "\n```\n")
            report.append(f"**source -> Darija ({dt_s:.1f}s):**\n")
            report.append("```\n" + dar_source + "\n```\n")
            report.append(f"**rollout (EN synthetic, {len(rollout)} chars):**\n")
            report.append("```\n" + rollout[:MAX_INPUT_CHARS] + "\n```\n")
            report.append(f"**rollout -> Darija ({dt_r:.1f}s):**\n")
            report.append("```\n" + dar_rollout + "\n```\n")
            report.append("---\n")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(report))
    print(f"\nWrote: {args.out}")


if __name__ == "__main__":
    main()
