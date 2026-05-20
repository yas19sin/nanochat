"""
Train a NanoChat embedding head for sentence similarity / retrieval.

Supported dataset shapes:
- Positive pairs: columns text1/text2, no score column. Uses in-batch InfoNCE.
- Scored pairs: columns text1/text2/score. Uses cosine-similarity MSE.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import time
from pathlib import Path

import torch
import torch.nn.functional as F
from datasets import load_dataset

from nanochat.checkpoint_manager import load_model
from nanochat.common import autodetect_device_type, get_base_dir
from nanochat.downstream import NanochatTextEmbedder, save_downstream_checkpoint
from scripts.embed_text import batch_encode


def load_local_or_hf_dataset(dataset: str, split: str):
    path = Path(dataset)
    if path.exists():
        if path.is_dir():
            for name in (f"{split}.jsonl", f"{split}.json", f"{split}.parquet"):
                candidate = path / name
                if candidate.exists():
                    path = candidate
                    break
        suffix = path.suffix.lower()
        if suffix in {".json", ".jsonl"}:
            return load_dataset("json", data_files={split: str(path)}, split=split)
        if suffix == ".parquet":
            return load_dataset("parquet", data_files={split: str(path)}, split=split)
        raise ValueError(f"Unsupported local dataset path: {path}")

    kwargs = {"split": split}
    token = os.environ.get("HF_TOKEN")
    if token:
        kwargs["token"] = token
    return load_dataset(dataset, **kwargs)


def materialize_pairs(ds, args, max_examples: int):
    rows = []
    limit = len(ds) if max_examples <= 0 else min(max_examples, len(ds))
    for idx in range(limit):
        row = ds[idx]
        text1 = row.get(args.text1_column)
        text2 = row.get(args.text2_column)
        if not isinstance(text1, str) or not isinstance(text2, str):
            continue
        item = {"text1": text1.strip(), "text2": text2.strip()}
        if args.score_column:
            score = float(row[args.score_column])
            if args.score_max > args.score_min:
                score = (score - args.score_min) / (args.score_max - args.score_min)
            item["score"] = max(0.0, min(1.0, score))
        if item["text1"] and item["text2"]:
            rows.append(item)
    return rows


def pair_batch(rows, tokenizer, args, device):
    text1 = [row["text1"] for row in rows]
    text2 = [row["text2"] for row in rows]
    ids1, mask1 = batch_encode(tokenizer, text1, args.max_seq_len, device)
    ids2, mask2 = batch_encode(tokenizer, text2, args.max_seq_len, device)
    scores = None
    if "score" in rows[0]:
        scores = torch.tensor([row["score"] for row in rows], dtype=torch.float32, device=device)
    return ids1, mask1, ids2, mask2, scores


def contrastive_loss(emb1, emb2, temperature: float):
    logits = emb1 @ emb2.T / temperature
    labels = torch.arange(emb1.size(0), device=emb1.device)
    return (F.cross_entropy(logits, labels) + F.cross_entropy(logits.T, labels)) / 2


@torch.inference_mode()
def evaluate(model, rows, tokenizer, args, device):
    model.eval()
    losses = []
    for start in range(0, len(rows), args.eval_batch_size):
        batch = rows[start:start + args.eval_batch_size]
        ids1, mask1, ids2, mask2, scores = pair_batch(batch, tokenizer, args, device)
        emb1 = model(ids1, mask1)
        emb2 = model(ids2, mask2)
        if scores is None:
            loss = contrastive_loss(emb1, emb2, args.temperature)
        else:
            sims = (emb1 * emb2).sum(dim=-1)
            loss = F.mse_loss((sims + 1) / 2, scores)
        losses.append(float(loss.item()))
    model.train()
    return {"loss": sum(losses) / max(len(losses), 1)}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["base", "sft"], default="sft")
    parser.add_argument("--model-tag", required=True)
    parser.add_argument("--model-step", type=int, default=None)
    parser.add_argument("--output-tag", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--train-split", default="train")
    parser.add_argument("--val-split", default=None)
    parser.add_argument("--text1-column", default="text1")
    parser.add_argument("--text2-column", default="text2")
    parser.add_argument("--score-column", default=None)
    parser.add_argument("--score-min", type=float, default=0.0)
    parser.add_argument("--score-max", type=float, default=1.0)
    parser.add_argument("--max-seq-len", type=int, default=512)
    parser.add_argument("--pooling", choices=["mean", "last", "first"], default="mean")
    parser.add_argument("--projection-dim", type=int, default=256)
    parser.add_argument("--freeze-backbone", action="store_true")
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--eval-batch-size", type=int, default=128)
    parser.add_argument("--backbone-lr", type=float, default=1e-5)
    parser.add_argument("--head-lr", type=float, default=1e-4)
    parser.add_argument("--weight-decay", type=float, default=0.01)
    parser.add_argument("--temperature", type=float, default=0.05)
    parser.add_argument("--eval-every", type=int, default=200)
    parser.add_argument("--save-every", type=int, default=500)
    parser.add_argument("--max-train-examples", type=int, default=-1)
    parser.add_argument("--max-val-examples", type=int, default=-1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--device-type", default="")
    return parser.parse_args()


def main():
    args = parse_args()
    random.seed(args.seed)
    device_type = autodetect_device_type() if not args.device_type else args.device_type
    device = torch.device(device_type)
    backbone, tokenizer, base_meta = load_model(
        args.source, device, phase="train", model_tag=args.model_tag, step=args.model_step)
    if args.freeze_backbone:
        for param in backbone.parameters():
            param.requires_grad = False
    model = NanochatTextEmbedder(
        backbone=backbone,
        pooling=args.pooling,
        projection_dim=args.projection_dim,
        normalize=True,
    ).to(device)
    optimizer = model.configure_optimizer(args.backbone_lr, args.head_lr, args.weight_decay)

    train_rows = materialize_pairs(
        load_local_or_hf_dataset(args.dataset, args.train_split),
        args,
        args.max_train_examples,
    )
    val_rows = []
    if args.val_split:
        val_rows = materialize_pairs(
            load_local_or_hf_dataset(args.dataset, args.val_split),
            args,
            args.max_val_examples,
        )
    print(f"Train pairs: {len(train_rows):,}")
    if val_rows:
        print(f"Val pairs: {len(val_rows):,}")

    out_dir = Path(get_base_dir()) / "downstream_checkpoints" / args.output_tag
    step = 0
    best_val = math.inf
    started = time.time()
    for epoch in range(1, args.epochs + 1):
        random.Random(args.seed + epoch).shuffle(train_rows)
        for start in range(0, len(train_rows), args.batch_size):
            batch = train_rows[start:start + args.batch_size]
            ids1, mask1, ids2, mask2, scores = pair_batch(batch, tokenizer, args, device)
            emb1 = model(ids1, mask1)
            emb2 = model(ids2, mask2)
            if scores is None:
                loss = contrastive_loss(emb1, emb2, args.temperature)
            else:
                sims = (emb1 * emb2).sum(dim=-1)
                loss = F.mse_loss((sims + 1) / 2, scores)
            loss.backward()
            optimizer.step()
            optimizer.zero_grad(set_to_none=True)
            step += 1
            if step == 1 or step % 10 == 0:
                elapsed = max(time.time() - started, 1e-6)
                print(f"step {step:05d} | epoch {epoch} | loss {loss.item():.4f} | pairs/s {step * args.batch_size / elapsed:.1f}")

            metrics = None
            if val_rows and args.eval_every > 0 and step % args.eval_every == 0:
                metrics = evaluate(model, val_rows, tokenizer, args, device)
                best_val = min(best_val, metrics["loss"])
                print(f"eval step {step:05d} | loss {metrics['loss']:.4f}")

            if args.save_every > 0 and step % args.save_every == 0:
                save_downstream_checkpoint(out_dir, step, model, {
                    "step": step,
                    "task": "text_embedding",
                    "pooling": args.pooling,
                    "projection_dim": args.projection_dim,
                    "normalize": True,
                    "val_metrics": metrics,
                    "best_val_loss": best_val,
                    "base_source": args.source,
                    "base_model_tag": args.model_tag,
                    "base_model_step": args.model_step,
                    "base_meta": base_meta,
                    "user_config": vars(args),
                })

    metrics = evaluate(model, val_rows, tokenizer, args, device) if val_rows else None
    if metrics:
        best_val = min(best_val, metrics["loss"])
    save_downstream_checkpoint(out_dir, step, model, {
        "step": step,
        "task": "text_embedding",
        "pooling": args.pooling,
        "projection_dim": args.projection_dim,
        "normalize": True,
        "val_metrics": metrics,
        "best_val_loss": best_val,
        "base_source": args.source,
        "base_model_tag": args.model_tag,
        "base_model_step": args.model_step,
        "base_meta": base_meta,
        "user_config": vars(args),
    })
    print(json.dumps({"output_dir": str(out_dir), "step": step, "val": metrics}, indent=2))


if __name__ == "__main__":
    main()
