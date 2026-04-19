"""
Translate Crownelius/Opus-4.6-Reasoning-3300x to Moroccan Darija
using the HF Space API (Lyte/Moroccan-Darija-Translator-New).

Translates: problem, solution, thinking
Saves progress after each row (resume-safe).

Usage:
    python scripts/translate_opus_reasoning.py
    python scripts/translate_opus_reasoning.py --limit 10      # test with 10 rows
    python scripts/translate_opus_reasoning.py --dry-run        # preview only
"""

import json
import os
import time
import argparse
from gradio_client import Client

# ---------------------------------------------------------------------------
# Config

HF_SPACE = "Lyte/Moroccan-Darija-Translator-New"
HF_TOKEN = os.environ["HF_TOKEN"]
DIRECTION = "🇬🇧 English → 🇲🇦 Darija"
OUTPUT_FILE = "dev-ignore/opus_reasoning_darija.jsonl"
CHECKPOINT_FILE = "dev-ignore/.opus_reasoning_checkpoint.json"

GEN_PARAMS = dict(
    max_new_tokens=1024,
    temperature=0.3,
    top_k=300,
    top_p=0.98,
    repetition_penalty=1.15,
)

MAX_RETRIES = 3
RETRY_DELAY = 5


def translate_text(client: Client, text: str) -> str:
    """Translate a single text via the HF Space API."""
    if not text or not text.strip():
        return ""

    for attempt in range(MAX_RETRIES):
        try:
            result = client.predict(
                message=text,
                direction=DIRECTION,
                custom_prompt="",
                max_new_tokens=GEN_PARAMS["max_new_tokens"],
                temperature=GEN_PARAMS["temperature"],
                top_k=GEN_PARAMS["top_k"],
                top_p=GEN_PARAMS["top_p"],
                repetition_penalty=GEN_PARAMS["repetition_penalty"],
                api_name="/chat_fn",
            )
            # result is a tuple: (chatbot_messages, input_text, dropdown, markdown)
            # chatbot_messages is a list of dicts with role/content
            messages = result[0]
            if messages:
                last_msg = messages[-1]
                # Extract text from the content structure
                content = last_msg.get("content", "")
                if isinstance(content, list):
                    # content is list of dicts like [{"text": "...", "type": "text"}]
                    parts = []
                    for part in content:
                        if isinstance(part, dict) and "text" in part:
                            parts.append(part["text"])
                        elif isinstance(part, str):
                            parts.append(part)
                    return "".join(parts).strip()
                elif isinstance(content, str):
                    return content.strip()
            return text  # fallback
        except Exception as e:
            print(f"    [retry {attempt+1}/{MAX_RETRIES}] {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))

    print(f"    [FAILED] Keeping original text")
    return text


def main():
    parser = argparse.ArgumentParser(
        description="Translate Opus-4.6-Reasoning dataset to Darija")
    parser.add_argument("--output", default=OUTPUT_FILE)
    parser.add_argument("--checkpoint", default=CHECKPOINT_FILE)
    parser.add_argument("--limit", type=int, default=0,
                        help="Max rows to translate (0=all)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-thinking", action="store_true",
                        help="Skip translating the thinking column (saves time)")
    args = parser.parse_args()

    # Load dataset
    from datasets import load_dataset
    print("Loading Opus-4.6-Reasoning dataset...")
    ds = load_dataset("Crownelius/Opus-4.6-Reasoning-3300x",
                      split="train", token=HF_TOKEN)
    total = len(ds)
    print(f"  Loaded {total:,} rows")

    if args.limit > 0:
        total = min(total, args.limit)
        print(f"  Limiting to {total} rows")

    # Resume support
    start_idx = 0
    if os.path.exists(args.checkpoint):
        with open(args.checkpoint, "r", encoding="utf-8") as f:
            ckpt = json.load(f)
        start_idx = ckpt.get("next_row", 0)
        print(f"  Resuming from row {start_idx}/{total}")

    if start_idx >= total:
        print("All rows already translated!")
        return

    if args.dry_run:
        remaining = total - start_idx
        # ~3 fields per row, ~5s per API call
        fields_per_row = 2 if args.skip_thinking else 3
        est_hours = remaining * fields_per_row * 5 / 3600
        print(
            f"\n[DRY RUN] Would translate {remaining} rows ({remaining * fields_per_row} API calls)")
        print(f"Estimated time: ~{est_hours:.1f} hours at ~5s/call")
        for i in range(start_idx, min(start_idx + 3, total)):
            row = ds[i]
            print(
                f"\nRow {i}: [{row.get('category', '?')}] {row.get('difficulty', '?')}")
            print(f"  problem: {str(row.get('problem', ''))[:120]}...")
            print(f"  solution: {str(row.get('solution', ''))[:120]}...")
        return

    # Ensure output dir exists
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)

    # Connect to Space
    print(f"\nConnecting to {HF_SPACE}...")
    client = Client(HF_SPACE, token=HF_TOKEN)
    print("Connected!")

    t_start = time.time()

    for row_idx in range(start_idx, total):
        row = ds[row_idx]
        problem = str(row.get("problem", "") or "")
        solution = str(row.get("solution", "") or "")
        thinking = str(row.get("thinking", "") or "")

        print(f"\n[{row_idx+1}/{total}] category={row.get('category', '?')} "
              f"difficulty={row.get('difficulty', '?')}")

        # Translate problem
        print(f"  translating problem ({len(problem)} chars)...")
        problem_da = translate_text(client, problem)

        # Clear chat between translations to avoid context bleed
        try:
            client.predict(api_name="/clear_chat")
        except Exception:
            pass

        # Translate solution
        print(f"  translating solution ({len(solution)} chars)...")
        solution_da = translate_text(client, solution)

        try:
            client.predict(api_name="/clear_chat")
        except Exception:
            pass

        # Translate thinking (optional)
        thinking_da = ""
        if not args.skip_thinking and thinking:
            print(f"  translating thinking ({len(thinking)} chars)...")
            thinking_da = translate_text(client, thinking)
            try:
                client.predict(api_name="/clear_chat")
            except Exception:
                pass

        # Build output record
        record = {
            "id": row.get("id", row_idx),
            "problem_en": problem,
            "problem": problem_da,
            "thinking_en": thinking,
            "thinking": thinking_da,
            "solution_en": solution,
            "solution": solution_da,
            "difficulty": row.get("difficulty", ""),
            "category": row.get("category", ""),
        }

        # Append to output file
        mode = "a" if row_idx > 0 or start_idx > 0 else "w"
        with open(args.output, mode, encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        # Update checkpoint
        with open(args.checkpoint, "w", encoding="utf-8") as f:
            json.dump({"next_row": row_idx + 1}, f)

        # Progress
        elapsed = time.time() - t_start
        done = row_idx - start_idx + 1
        rate = done / max(elapsed, 1)
        remaining = total - row_idx - 1
        eta_min = remaining / rate / 60 if rate > 0 else 0
        print(
            f"  done ({done}/{total - start_idx}, ~{eta_min:.0f}m remaining)")

    elapsed = time.time() - t_start
    print(
        f"\nDone! Translated {total - start_idx} rows in {elapsed/60:.1f} minutes")
    print(f"Output: {args.output}")

    # Clean up checkpoint
    if os.path.exists(args.checkpoint):
        os.remove(args.checkpoint)
        print("Checkpoint cleaned up")


if __name__ == "__main__":
    main()
