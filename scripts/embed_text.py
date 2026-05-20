"""
Extract pooled NanoChat embeddings for text, sentence similarity, or indexing.

This can use either the raw base/SFT checkpoint or a trained downstream
text-embedding checkpoint.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch

from nanochat.checkpoint_manager import load_model
from nanochat.common import autodetect_device_type
from nanochat.downstream import NanochatTextEmbedder, load_downstream_checkpoint


def batch_encode(tokenizer, texts: list[str], max_seq_len: int, device):
    bos = tokenizer.get_bos_token_id()
    encoded = [tokenizer.encode(text, prepend=bos)[:max_seq_len] or [bos] for text in texts]
    max_len = max(len(ids) for ids in encoded)
    input_ids = []
    attention_mask = []
    for ids in encoded:
        pad = max_len - len(ids)
        input_ids.append(ids + [bos] * pad)
        attention_mask.append([1] * len(ids) + [0] * pad)
    return (
        torch.tensor(input_ids, dtype=torch.long, device=device),
        torch.tensor(attention_mask, dtype=torch.long, device=device),
    )


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
    parser.add_argument("--input", type=Path, default=None, help="UTF-8 text file, one text per line")
    parser.add_argument("--output", type=Path, default=None, help="JSONL output path")
    parser.add_argument("--max-seq-len", type=int, default=512)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--pooling", choices=["mean", "last", "first"], default="mean")
    parser.add_argument("--projection-dim", type=int, default=0)
    parser.add_argument("--no-normalize", action="store_true")
    parser.add_argument("--device-type", default="")
    return parser.parse_args()


def load_embedder(args, device):
    if args.checkpoint_dir:
        state, meta, step = load_downstream_checkpoint(args.checkpoint_dir, args.step)
        if meta.get("task") != "text_embedding":
            raise ValueError(f"Expected text_embedding checkpoint, got {meta.get('task')!r}")
        backbone, tokenizer, _ = load_model(
            meta["base_source"],
            device,
            phase="eval",
            model_tag=meta["base_model_tag"],
            step=meta["base_model_step"],
        )
        embedder = NanochatTextEmbedder(
            backbone=backbone,
            pooling=meta.get("pooling", "mean"),
            projection_dim=int(meta.get("projection_dim", 0) or 0),
            normalize=bool(meta.get("normalize", True)),
        ).to(device)
        embedder.load_state_dict(state)
        return embedder, tokenizer, {"downstream_step": step, "meta": meta}

    if not args.model_tag:
        raise SystemExit("Use --model-tag for raw base/SFT embeddings, or --checkpoint-dir for a trained embedder.")
    backbone, tokenizer, meta = load_model(
        args.source, device, phase="eval", model_tag=args.model_tag, step=args.model_step)
    embedder = NanochatTextEmbedder(
        backbone=backbone,
        pooling=args.pooling,
        projection_dim=args.projection_dim,
        normalize=not args.no_normalize,
    ).to(device)
    return embedder, tokenizer, {"meta": meta}


def main():
    args = parse_args()
    texts = read_texts(args)
    if not texts:
        raise SystemExit("No text provided. Use --text or --input.")
    device_type = autodetect_device_type() if not args.device_type else args.device_type
    device = torch.device(device_type)
    embedder, tokenizer, _ = load_embedder(args, device)
    embedder.eval()

    rows = []
    with torch.inference_mode():
        for start in range(0, len(texts), args.batch_size):
            batch = texts[start:start + args.batch_size]
            input_ids, attention_mask = batch_encode(tokenizer, batch, args.max_seq_len, device)
            embeddings = embedder(input_ids, attention_mask).cpu().tolist()
            for text, emb in zip(batch, embeddings):
                rows.append({"text": text, "embedding": emb})

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"Wrote {len(rows):,} embeddings to {args.output}")
    else:
        print(json.dumps(rows, ensure_ascii=False)[:4000])


if __name__ == "__main__":
    main()
