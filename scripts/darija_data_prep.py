"""
Convert the Darija pretraining corpus on HuggingFace into parquet shards that
nanochat can train on.

The output directory will contain parquet files with a single `text` column.
The final file is reserved for validation (held out from the `pure` subset) so
that `parquets_iter_batched` will naturally pick it up as the val split.
"""

import argparse
import os
from typing import List, Tuple

import pyarrow as pa
import pyarrow.parquet as pq
from datasets import load_dataset

from nanochat.common import get_base_dir

DATASET_NAME = "Lyte/darija-pretraining-corpus"
SUBSETS = ("arabic_raw", "bilingual", "pure")
TEXT_CANDIDATES = ("text", "content", "body", "raw_content")


def detect_text_key(example: dict) -> str:
    for key in TEXT_CANDIDATES:
        val = example.get(key)
        if isinstance(val, str):
            return key
    raise ValueError(f"Could not find a text column in example keys: {list(example.keys())}")


def write_parquet(texts: List[str], filepath: str):
    table = pa.table({"text": texts})
    pq.write_table(table, filepath, compression="zstd")


def convert_subset(
    dataset: str,
    subset: str,
    data_dir: str,
    shard_size: int,
    val_holdback: int,
    shuffle_buffer: int,
    seed: int,
    max_rows: int | None,
) -> Tuple[int, int, List[str]]:
    """
    Stream a subset and write training shards. Optionally hold back the last
    `val_holdback` rows for validation (used only for the `pure` subset).
    Returns (train_rows, num_shards, val_rows).
    """
    ds = load_dataset(dataset, subset, split="train", streaming=True)
    if shuffle_buffer:
        ds = ds.shuffle(seed=seed, buffer_size=shuffle_buffer)
    iterator = iter(ds)

    try:
        first_example = next(iterator)
    except StopIteration:
        return 0, 0, []

    text_key = detect_text_key(first_example)

    train_rows = 0
    shard_idx = 0
    train_buffer: List[str] = []
    val_buffer: List[str] = []

    def flush_train():
        nonlocal shard_idx, train_buffer, train_rows
        if not train_buffer:
            return
        filename = f"{subset}_train_{shard_idx:05d}.parquet"
        filepath = os.path.join(data_dir, filename)
        write_parquet(train_buffer, filepath)
        shard_idx += 1
        train_rows += len(train_buffer)
        train_buffer = []

    def maybe_push_train(text: str):
        train_buffer.append(text)
        if len(train_buffer) >= shard_size:
            flush_train()

    # Handle the first example before entering the main loop.
    if val_holdback > 0:
        val_buffer.append(first_example[text_key])
    else:
        maybe_push_train(first_example[text_key])

    processed = 1
    for row in iterator:
        text = row.get(text_key, "")
        if not isinstance(text, str):
            continue
        processed += 1
        if max_rows is not None and processed > max_rows:
            break
        if val_holdback > 0:
            val_buffer.append(text)
            if len(val_buffer) > val_holdback:
                maybe_push_train(val_buffer.pop(0))
        else:
            maybe_push_train(text)

    # Flush any remaining training rows
    flush_train()

    # If we didn't accumulate enough for validation, warn but still return what we have.
    if val_holdback > 0 and len(val_buffer) < val_holdback:
        print(f"[WARN] Requested {val_holdback} validation rows from {subset} but only found {len(val_buffer)}.")

    return train_rows, shard_idx, val_buffer


def main():
    parser = argparse.ArgumentParser(description="Prepare Darija pretraining parquet shards")
    parser.add_argument("--dataset", type=str, default=DATASET_NAME, help="Source HuggingFace dataset")
    parser.add_argument("--data-dir", type=str, default=os.path.join(get_base_dir(), "darija_data"), help="Output directory for parquet shards")
    parser.add_argument("--shard-size", type=int, default=250_000, help="Number of rows per training shard")
    parser.add_argument("--val-size", type=int, default=50_000, help="Validation rows to hold out from the pure subset")
    parser.add_argument("--shuffle-buffer", type=int, default=50_000, help="Buffer size for streaming shuffle (0 disables shuffling)")
    parser.add_argument("--seed", type=int, default=42, help="Shuffle seed")
    parser.add_argument("--max-rows", type=int, default=None, help="Optional cap per subset for quick dry-runs")
    args = parser.parse_args()

    os.makedirs(args.data_dir, exist_ok=True)
    print(f"Writing parquet shards to {args.data_dir}")

    total_rows = 0
    total_shards = 0
    val_rows: List[str] = []

    for subset in SUBSETS:
        holdback = args.val_size if subset == "pure" else 0
        train_rows, shard_count, subset_val = convert_subset(
            args.dataset,
            subset,
            args.data_dir,
            args.shard_size,
            holdback,
            args.shuffle_buffer,
            args.seed,
            args.max_rows,
        )
        total_rows += train_rows
        total_shards += shard_count
        if subset_val:
            val_rows = subset_val
        print(f"{subset}: wrote {train_rows:,} rows across {shard_count} shards")

    # Write validation shard from held-back pure rows
    if val_rows:
        val_path = os.path.join(args.data_dir, "pure_val_00000.parquet")
        write_parquet(val_rows, val_path)
        print(f"pure (val): wrote {len(val_rows):,} rows to {val_path}")
    else:
        print("No validation rows were held back; training/validation split will be unavailable.")

    print(f"Done. Total train rows: {total_rows:,} across {total_shards} shards.")


if __name__ == "__main__":
    main()
