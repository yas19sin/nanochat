"""
Translate identity_conversations.jsonl from English to Moroccan Darija
using the HF Inference Endpoint REST API with batch translation.

Supports resuming: writes a checkpoint file after each batch.
If interrupted, re-run and it picks up where it left off.

Usage:
    python scripts/translate_identity.py
    python scripts/translate_identity.py --input dev-ignore/identity_conversations.jsonl
    python scripts/translate_identity.py --dry-run   # preview without calling API
"""

import json
import os
import time
import argparse
import requests

# ---------------------------------------------------------------------------
# Config

HF_TOKEN = os.environ["HF_TOKEN"]
# "https://env16toatbeollz1.us-east-1.aws.endpoints.huggingface.cloud"
ENDPOINT_URL = "https://wbmzrrm75z29b5tj.us-east-2.aws.endpoints.huggingface.cloud"

# Generation params
GEN_PARAMS = dict(
    max_new_tokens=256,
    temperature=0.3,
    top_k=300,
    top_p=0.98,
    repetition_penalty=1.15,
    do_sample=True,
    return_full_text=False,
)

BATCH_SIZE = 8
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds


def extract_darija(raw: str) -> str:
    """Extract clean Darija translation from raw model output."""
    # With return_full_text=False, output is just the generated continuation
    # Stop at any continuation the model may generate
    darija = raw
    for stop in ["\nEnglish:", "\nTranslate:", "\n\n"]:
        darija = darija.split(stop)[0]
    return darija.strip()


def is_still_english(text: str) -> bool:
    """Check if text is mostly ASCII (i.e. not translated to Darija)."""
    alpha = [ch for ch in text if ch.isalpha()]
    if not alpha:
        return False
    ascii_alpha = sum(1 for ch in alpha if ch.isascii())
    return ascii_alpha / len(alpha) > 0.6


def translate_batch(session: requests.Session, texts: list[str]) -> list[str]:
    """Translate a batch of English texts to Darija in one API call."""
    prompts = [f"English: {t}\nDarija:" for t in texts]
    payload = {
        "inputs": prompts,
        "parameters": GEN_PARAMS,
    }
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.post(ENDPOINT_URL, json=payload, timeout=300)
            resp.raise_for_status()
            results = resp.json()
            # Response: [[{"generated_text": " <translation>..."}], ...]
            translations = []
            for i, res in enumerate(results):
                raw = res[0]["generated_text"]
                darija = extract_darija(raw)
                translations.append(darija if darija else texts[i])
            return translations
        except Exception as e:
            print(f"    [retry {attempt+1}/{MAX_RETRIES}] {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
    # All retries failed — fall back to original text
    print(
        f"    [FAILED] Keeping original English text for {len(texts)} messages")
    return texts


def translate_single(session: requests.Session, text: str) -> str:
    """Translate a single message (fallback for untranslated batch items)."""
    results = translate_batch(session, [text])
    return results[0]


def check_endpoint(session: requests.Session):
    """Verify the endpoint is running before starting."""
    try:
        resp = session.get(ENDPOINT_URL.rstrip("/") + "/health", timeout=30)
        if resp.status_code == 200:
            print("Endpoint healthy")
            return True
        print(f"Endpoint health check returned {resp.status_code}")
    except Exception as e:
        print(f"Endpoint health check failed: {e}")
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Translate identity conversations to Darija")
    parser.add_argument("--input", default="dev-ignore/identity_conversations.jsonl",
                        help="Input JSONL file")
    parser.add_argument("--output", default="dev-ignore/identity_conversations_darija.jsonl",
                        help="Output JSONL file")
    parser.add_argument("--checkpoint", default="dev-ignore/.translate_checkpoint.json",
                        help="Checkpoint file for resume support")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                        help="Messages per API call (default: 8)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without calling API")
    args = parser.parse_args()

    # Load conversations
    with open(args.input, "r", encoding="utf-8") as f:
        conversations = [json.loads(line) for line in f]

    total_convs = len(conversations)
    total_msgs = sum(len(c) for c in conversations)
    print(f"Loaded {total_convs} conversations, {total_msgs} messages")

    # Load checkpoint (resume support)
    translated = []
    start_idx = 0
    if os.path.exists(args.checkpoint):
        with open(args.checkpoint, "r", encoding="utf-8") as f:
            ckpt = json.load(f)
        start_idx = ckpt.get("next_conv", 0)
        if os.path.exists(args.output):
            with open(args.output, "r", encoding="utf-8") as f:
                translated = [json.loads(line) for line in f]
        print(
            f"Resuming from conversation {start_idx}/{total_convs} ({len(translated)} already done)")

    if start_idx >= total_convs:
        print("All conversations already translated!")
        return

    if args.dry_run:
        remaining = total_convs - start_idx
        remaining_msgs = sum(len(conversations[i])
                             for i in range(start_idx, total_convs))
        est_hours = remaining_msgs * 2.9 / 3600
        print(
            f"\n[DRY RUN] Would translate {remaining} conversations ({remaining_msgs} messages)")
        print(
            f"Estimated time: ~{est_hours:.1f} hours at ~2.9s/msg with batch={args.batch_size}")
        for i in range(start_idx, min(start_idx + 3, total_convs)):
            conv = conversations[i]
            print(f"\nConversation {i+1}: {len(conv)} messages")
            for msg in conv[:2]:
                print(f"  [{msg['role']}] {msg['content'][:80]}...")
        return

    # Setup session with auth
    session = requests.Session()
    session.headers.update({
        "Accept": "application/json",
        "Authorization": f"Bearer {HF_TOKEN}",
        "Content-Type": "application/json",
    })

    print(f"Connecting to {ENDPOINT_URL}...")
    if not check_endpoint(session):
        print("ERROR: Endpoint not reachable. Is it running?")
        return

    msgs_done = sum(len(c) for c in conversations[:start_idx])
    t_start = time.time()

    for conv_idx in range(start_idx, total_convs):
        conv = conversations[conv_idx]
        texts = [msg["content"] for msg in conv]
        roles = [msg["role"] for msg in conv]

        # Translate in batches
        all_darija = []
        for batch_start in range(0, len(texts), args.batch_size):
            batch_texts = texts[batch_start:batch_start + args.batch_size]
            batch_darija = translate_batch(session, batch_texts)
            # Retry any that came back in English as single requests
            for j, (src, tgt) in enumerate(zip(batch_texts, batch_darija)):
                if is_still_english(tgt):
                    print(f"    retrying msg {batch_start+j} (still English)")
                    batch_darija[j] = translate_single(session, src)
            all_darija.extend(batch_darija)
            msgs_done += len(batch_texts)

            # Progress
            elapsed = time.time() - t_start
            rate = msgs_done / max(elapsed, 1) if conv_idx > start_idx else 0
            eta = (total_msgs - msgs_done) / rate / 60 if rate > 0 else 0
            print(f"  [{conv_idx+1}/{total_convs}] batch {batch_start//args.batch_size+1} "
                  f"({msgs_done}/{total_msgs} total, ~{eta:.0f}m remaining)")

        translated_conv = [{"role": r, "content": d}
                           for r, d in zip(roles, all_darija)]
        translated.append(translated_conv)

        # Write output (append if resuming, write-fresh only on first run)
        mode = "a" if start_idx > 0 or conv_idx > 0 else "w"
        with open(args.output, mode, encoding="utf-8") as f:
            f.write(json.dumps(translated_conv, ensure_ascii=False) + "\n")

        # Update checkpoint
        with open(args.checkpoint, "w", encoding="utf-8") as f:
            json.dump({"next_conv": conv_idx + 1, "msgs_done": msgs_done}, f)

    elapsed = time.time() - t_start
    print(
        f"\nDone! Translated {total_convs} conversations ({total_msgs} messages) in {elapsed/60:.1f} minutes")
    print(f"Output: {args.output}")

    # Clean up checkpoint
    if os.path.exists(args.checkpoint):
        os.remove(args.checkpoint)
        print("Checkpoint cleaned up")


if __name__ == "__main__":
    main()
