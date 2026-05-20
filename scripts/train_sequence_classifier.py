"""
Train a classification head on top of a NanoChat base/SFT checkpoint.

This is for discriminative tasks such as toxicity classification. It saves a
separate downstream checkpoint under:
  $NANOCHAT_BASE_DIR/downstream_checkpoints/<output-tag>/

Example:
    python -m scripts.train_sequence_classifier \
      --source sft \
      --model-tag AtlasionNano-125M-Instruct-FullSFT-v2 \
      --model-step 2000 \
      --dataset Lyte/darija-toxic-conversations-50k \
      --output-tag AtlasionNano-125M-ToxicClassifier \
      --label-names non-toxic toxic \
      --balance \
      --epochs 3
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import time
from collections import Counter
from pathlib import Path

import torch
import torch.nn.functional as F
from datasets import load_dataset

from nanochat.checkpoint_manager import load_model
from nanochat.common import autodetect_device_type, get_base_dir, get_peak_flops, COMPUTE_DTYPE, COMPUTE_DTYPE_REASON
from nanochat.downstream import NanochatSequenceClassifier, save_downstream_checkpoint


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


def normalize_label(value, label_names: list[str]) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value).strip().lower().replace("_", "-")
    normalized = [label.lower().replace("_", "-") for label in label_names]
    if text in normalized:
        return normalized.index(text)
    if text == "nontoxic" and "non-toxic" in normalized:
        return normalized.index("non-toxic")
    raise ValueError(f"Could not map label {value!r} to labels {label_names}")


def build_indices(labels: list[int], balance: bool, seed: int) -> list[int]:
    by_label: dict[int, list[int]] = {}
    for idx, label in enumerate(labels):
        by_label.setdefault(label, []).append(idx)
    if not balance:
        indices = list(range(len(labels)))
        random.Random(seed).shuffle(indices)
        return indices

    max_count = max(len(v) for v in by_label.values())
    rng = random.Random(seed)
    indices = []
    for group in by_label.values():
        if not group:
            continue
        repeat = math.ceil(max_count / len(group))
        expanded = (group * repeat)[:max_count]
        indices.extend(expanded)
    rng.shuffle(indices)
    return indices


def encode_text(tokenizer, text: str, max_seq_len: int):
    bos = tokenizer.get_bos_token_id()
    ids = tokenizer.encode(text, prepend=bos)[:max_seq_len]
    if not ids:
        ids = [bos]
    return ids


def collate_batch(rows, tokenizer, args, device):
    encoded = []
    labels = []
    for row in rows:
        text = row["text"]
        if args.prompt_template:
            text = args.prompt_template.format(text=text)
        ids = encode_text(tokenizer, text, args.max_seq_len)
        encoded.append(ids)
        labels.append(row["label"])

    bos = tokenizer.get_bos_token_id()
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
        torch.tensor(labels, dtype=torch.long, device=device),
    )


def materialize_rows(ds, text_column: str, label_column: str, label_names: list[str], max_examples: int):
    rows = []
    labels = []
    limit = len(ds) if max_examples <= 0 else min(max_examples, len(ds))
    for idx in range(limit):
        row = ds[idx]
        text = row.get(text_column) or row.get("text") or row.get("text_darija") or row.get("darija")
        if not isinstance(text, str) or not text.strip():
            continue
        label_value = row.get(label_column)
        if label_value is None:
            label_value = row.get("label_text", row.get("label"))
        label = normalize_label(label_value, label_names)
        rows.append({"text": text.strip(), "label": label})
        labels.append(label)
    return rows, labels


@torch.inference_mode()
def evaluate(model, rows, tokenizer, args, device):
    model.eval()
    total = 0
    correct = 0
    loss_sum = 0.0
    counts = Counter()
    pred_counts = Counter()
    for start in range(0, len(rows), args.eval_batch_size):
        batch_rows = rows[start:start + args.eval_batch_size]
        input_ids, attention_mask, labels = collate_batch(batch_rows, tokenizer, args, device)
        out = model(input_ids, attention_mask=attention_mask, labels=labels)
        logits = out["logits"]
        preds = logits.argmax(dim=-1)
        total += labels.numel()
        correct += int((preds == labels).sum().item())
        loss_sum += float(out["loss"].item()) * labels.numel()
        counts.update(labels.cpu().tolist())
        pred_counts.update(preds.cpu().tolist())
    model.train()
    return {
        "loss": loss_sum / max(total, 1),
        "accuracy": correct / max(total, 1),
        "label_counts": dict(counts),
        "pred_counts": dict(pred_counts),
    }


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["base", "sft"], default="sft")
    parser.add_argument("--model-tag", required=True)
    parser.add_argument("--model-step", type=int, default=None)
    parser.add_argument("--output-tag", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--train-split", default="train")
    parser.add_argument("--val-split", default="test")
    parser.add_argument("--text-column", default="text_darija")
    parser.add_argument("--label-column", default="label_text")
    parser.add_argument("--label-names", nargs="+", default=["non-toxic", "toxic"])
    parser.add_argument("--max-seq-len", type=int, default=512)
    parser.add_argument("--prompt-template", default="{text}")
    parser.add_argument("--pooling", choices=["last", "mean", "first"], default="last")
    parser.add_argument("--dropout", type=float, default=0.1)
    parser.add_argument("--freeze-backbone", action="store_true")
    parser.add_argument("--balance", action="store_true")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--eval-batch-size", type=int, default=128)
    parser.add_argument("--backbone-lr", type=float, default=2e-5)
    parser.add_argument("--head-lr", type=float, default=2e-4)
    parser.add_argument("--weight-decay", type=float, default=0.01)
    parser.add_argument("--grad-clip", type=float, default=1.0)
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
    print(f"COMPUTE_DTYPE: {COMPUTE_DTYPE} ({COMPUTE_DTYPE_REASON})")
    if device_type == "cuda":
        name = torch.cuda.get_device_name(0)
        print(f"GPU: {name} | Peak FLOPS (BF16): {get_peak_flops(name):.2e}")

    backbone, tokenizer, base_meta = load_model(
        args.source, device, phase="train", model_tag=args.model_tag, step=args.model_step)
    model = NanochatSequenceClassifier(
        backbone=backbone,
        num_labels=len(args.label_names),
        pooling=args.pooling,
        dropout=args.dropout,
        freeze_backbone=args.freeze_backbone,
    ).to(device)
    optimizer = model.configure_optimizer(args.backbone_lr, args.head_lr, args.weight_decay)

    train_ds = load_local_or_hf_dataset(args.dataset, args.train_split)
    val_ds = load_local_or_hf_dataset(args.dataset, args.val_split)
    train_rows, train_labels = materialize_rows(
        train_ds, args.text_column, args.label_column, args.label_names, args.max_train_examples)
    val_rows, val_labels = materialize_rows(
        val_ds, args.text_column, args.label_column, args.label_names, args.max_val_examples)
    print(f"Train rows: {len(train_rows):,} {dict(Counter(train_labels))}")
    print(f"Val rows: {len(val_rows):,} {dict(Counter(val_labels))}")

    indices = build_indices(train_labels, args.balance, args.seed)
    base_dir = Path(get_base_dir())
    out_dir = base_dir / "downstream_checkpoints" / args.output_tag
    user_config = vars(args)
    best_acc = -1.0
    step = 0
    started = time.time()

    for epoch in range(1, args.epochs + 1):
        random.Random(args.seed + epoch).shuffle(indices)
        for offset in range(0, len(indices), args.batch_size):
            batch_indices = indices[offset:offset + args.batch_size]
            batch_rows = [train_rows[i] for i in batch_indices]
            input_ids, attention_mask, labels = collate_batch(batch_rows, tokenizer, args, device)

            out = model(input_ids, attention_mask=attention_mask, labels=labels)
            loss = out["loss"]
            loss.backward()
            if args.grad_clip and args.grad_clip > 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.grad_clip)
            optimizer.step()
            optimizer.zero_grad(set_to_none=True)

            step += 1
            if step == 1 or step % 10 == 0:
                elapsed = max(time.time() - started, 1e-6)
                print(f"step {step:05d} | epoch {epoch} | loss {loss.item():.4f} | rows/s {step * args.batch_size / elapsed:.1f}")

            should_eval = args.eval_every > 0 and step % args.eval_every == 0
            should_save = args.save_every > 0 and step % args.save_every == 0
            if should_eval or should_save:
                metrics = evaluate(model, val_rows, tokenizer, args, device)
                print(f"eval step {step:05d} | loss {metrics['loss']:.4f} | acc {metrics['accuracy']:.4f} | preds {metrics['pred_counts']}")
                best_acc = max(best_acc, metrics["accuracy"])
                if should_save:
                    meta = {
                        "step": step,
                        "task": "sequence_classification",
                        "label_names": args.label_names,
                        "num_labels": len(args.label_names),
                        "pooling": args.pooling,
                        "dropout": args.dropout,
                        "val_metrics": metrics,
                        "best_accuracy": best_acc,
                        "base_source": args.source,
                        "base_model_tag": args.model_tag,
                        "base_model_step": args.model_step,
                        "base_meta": base_meta,
                        "user_config": user_config,
                    }
                    save_downstream_checkpoint(out_dir, step, model, meta)

    metrics = evaluate(model, val_rows, tokenizer, args, device)
    best_acc = max(best_acc, metrics["accuracy"])
    meta = {
        "step": step,
        "task": "sequence_classification",
        "label_names": args.label_names,
        "num_labels": len(args.label_names),
        "pooling": args.pooling,
        "dropout": args.dropout,
        "val_metrics": metrics,
        "best_accuracy": best_acc,
        "base_source": args.source,
        "base_model_tag": args.model_tag,
        "base_model_step": args.model_step,
        "base_meta": base_meta,
        "user_config": user_config,
    }
    save_downstream_checkpoint(out_dir, step, model, meta)
    print(json.dumps({"output_dir": str(out_dir), "step": step, **metrics}, indent=2))


if __name__ == "__main__":
    main()
