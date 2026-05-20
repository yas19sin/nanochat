"""
Zero-shot text classification using NanoChat pooled embeddings.

This is lightweight and headless: it embeds input texts and label descriptions,
then ranks labels by cosine similarity. Quality depends heavily on the backbone;
for strong zero-shot classification, train the embedder with contrastive/NLI data.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch

from nanochat.common import autodetect_device_type
from nanochat.downstream import NanochatZeroShotClassifier
from scripts.embed_text import load_embedder


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
    parser.add_argument("--checkpoint-dir", type=Path, default=None,
                        help="Optional downstream text-embedding checkpoint directory.")
    parser.add_argument("--step", type=int, default=None,
                        help="Downstream checkpoint step when --checkpoint-dir is used.")
    parser.add_argument("--source", choices=["base", "sft"], default="sft")
    parser.add_argument("--model-tag", default=None)
    parser.add_argument("--model-step", type=int, default=None)
    parser.add_argument("--text", action="append", default=[])
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--labels", nargs="+", required=True,
                        help="Label names or label descriptions.")
    parser.add_argument("--label-template", default="{text}",
                        help="Template applied to each label before embedding.")
    parser.add_argument("--max-seq-len", type=int, default=512)
    parser.add_argument("--pooling", choices=["mean", "last", "first"], default="mean")
    parser.add_argument("--projection-dim", type=int, default=0)
    parser.add_argument("--no-normalize", action="store_true")
    parser.add_argument("--device-type", default="")
    return parser.parse_args()


def main():
    args = parse_args()
    texts = read_texts(args)
    if not texts:
        raise SystemExit("No text provided. Use --text or --input.")
    device_type = autodetect_device_type() if not args.device_type else args.device_type
    device = torch.device(device_type)
    embedder, tokenizer, _ = load_embedder(args, device)
    embedder.eval()
    label_texts = [args.label_template.format(text=label) for label in args.labels]
    classifier = NanochatZeroShotClassifier(
        embedder=embedder,
        tokenizer=tokenizer,
        label_texts=label_texts,
        max_seq_len=args.max_seq_len,
    )
    results = classifier.classify(texts)
    for text, result in zip(texts, results):
        print(json.dumps({"text": text, **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
