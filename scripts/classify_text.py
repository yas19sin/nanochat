"""
Run a native NanoChat downstream sequence-classifier checkpoint.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch

from nanochat.checkpoint_manager import load_model
from nanochat.common import autodetect_device_type
from nanochat.downstream import NanochatSequenceClassifier, load_downstream_checkpoint
from scripts.train_sequence_classifier import collate_batch


def read_texts(args):
    texts = []
    if args.text:
        texts.extend(args.text)
    if args.input:
        with args.input.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    texts.append(line)
    return texts


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint-dir", type=Path, required=True)
    parser.add_argument("--step", type=int, default=None)
    parser.add_argument("--text", action="append", default=[])
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--device-type", default="")
    return parser.parse_args()


def main():
    args = parse_args()
    texts = read_texts(args)
    if not texts:
        raise SystemExit("No text provided. Use --text or --input.")
    state, meta, step = load_downstream_checkpoint(args.checkpoint_dir, args.step)
    device_type = autodetect_device_type() if not args.device_type else args.device_type
    device = torch.device(device_type)
    backbone, tokenizer, _ = load_model(
        meta["base_source"],
        device,
        phase="eval",
        model_tag=meta["base_model_tag"],
        step=meta["base_model_step"],
    )
    model = NanochatSequenceClassifier(
        backbone=backbone,
        num_labels=meta["num_labels"],
        pooling=meta.get("pooling", "last"),
        dropout=0.0,
    ).to(device)
    model.load_state_dict(state)
    model.eval()

    class Args:
        max_seq_len = meta.get("user_config", {}).get("max_seq_len", 512)
        prompt_template = meta.get("user_config", {}).get("prompt_template", "{text}")

    rows = [{"text": text, "label": 0} for text in texts]
    with torch.inference_mode():
        for start in range(0, len(rows), args.batch_size):
            batch = rows[start:start + args.batch_size]
            input_ids, attention_mask, _ = collate_batch(batch, tokenizer, Args, device)
            logits = model(input_ids, attention_mask=attention_mask)["logits"]
            probs = logits.softmax(dim=-1)
            for text, row_probs in zip(texts[start:start + args.batch_size], probs):
                pred = int(row_probs.argmax().item())
                print(json.dumps({
                    "text": text,
                    "step": step,
                    "label": meta["label_names"][pred],
                    "score": float(row_probs[pred].item()),
                    "probabilities": {
                        label: float(prob.item())
                        for label, prob in zip(meta["label_names"], row_probs)
                    },
                }, ensure_ascii=False))


if __name__ == "__main__":
    main()
