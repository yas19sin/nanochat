"""
Prepare the Darija pretraining dataset for nanochat.

Downloads **Lyte/darija-pretraining-corpus** from HuggingFace, streams it, and
writes local parquet shards that the standard nanochat dataloader can consume.

Usage (no GPU / no torch required):
    pip install datasets pyarrow   # only deps needed
    python -m scripts.darija_data_prep

The output directory defaults to $NANOCHAT_DATA_DIR (or ~/.cache/nanochat/darija_data).
Train shards are written first, then a single validation shard is written *last*
(the dataloader convention: last file = val).

IMPORTANT: the val parquet is written with a small row_group_size so that every
DDP rank gets at least one row group — otherwise validation hangs on multi-GPU.
"""

import os
import argparse
import pyarrow as pa
import pyarrow.parquet as pq
from datasets import load_dataset

# ---------------------------------------------------------------------------
# Defaults

HF_DATASET = "Lyte/darija-pretraining-corpus"
SUBSETS = ["arabic_raw", "bilingual", "pure"]
TRAIN_SHARD_SIZE = 1_000_000   # rows per train shard
VAL_SIZE = 50_000              # rows held out for validation
VAL_ROW_GROUP_SIZE = 5_000     # ≥8 row groups so 8-GPU validation never hangs
TRAIN_ROW_GROUP_SIZE = 50_000


def get_output_dir():
    """Return the data directory, respecting NANOCHAT_DATA_DIR env var."""
    env = os.environ.get("NANOCHAT_DATA_DIR")
    if env:
        return env
    cache = os.environ.get("NANOCHAT_BASE_DIR",
                           os.path.join(os.path.expanduser("~"), ".cache", "nanochat"))
    return os.path.join(cache, "darija_data")


def main():
    parser = argparse.ArgumentParser(
        description="Prepare Darija pretraining data")
    parser.add_argument("--val-size", type=int, default=VAL_SIZE,
                        help="number of rows to hold out for validation")
    parser.add_argument("--shard-size", type=int, default=TRAIN_SHARD_SIZE,
                        help="rows per train parquet shard")
    parser.add_argument("--output-dir", type=str, default=None,
                        help="override output directory")
    args = parser.parse_args()

    output_dir = args.output_dir or get_output_dir()
    os.makedirs(output_dir, exist_ok=True)
    print(f"Output directory: {output_dir}")

    # ------------------------------------------------------------------
    # Stream all subsets, collect val rows from 'pure', write train shards
    # ------------------------------------------------------------------
    val_rows = []
    shard_idx = 0
    shard_rows = []
    total_train = 0

    for subset in SUBSETS:
        print(f"\n--- Streaming subset: {subset} ---")
        ds = load_dataset(HF_DATASET, subset, split="train", streaming=True)
        for row in ds:
            text = row.get("text", "")
            if not text:
                continue

            # Reserve some 'pure' Darija rows for validation
            if subset == "pure" and len(val_rows) < args.val_size:
                val_rows.append(text)
                continue

            shard_rows.append(text)

            if len(shard_rows) >= args.shard_size:
                _write_shard(output_dir, subset, shard_idx,
                             shard_rows, TRAIN_ROW_GROUP_SIZE)
                total_train += len(shard_rows)
                shard_idx += 1
                shard_rows = []

        # Flush remaining rows for this subset
        if shard_rows:
            _write_shard(output_dir, subset, shard_idx,
                         shard_rows, TRAIN_ROW_GROUP_SIZE)
            total_train += len(shard_rows)
            shard_idx += 1
            shard_rows = []

    # ------------------------------------------------------------------
    # Write validation shard LAST (dataloader convention)
    # ------------------------------------------------------------------
    if not val_rows:
        print("WARNING: No validation rows collected!")
    else:
        val_path = os.path.join(output_dir, "zzz_val_00000.parquet")
        table = pa.table({"text": val_rows})
        pq.write_table(table, val_path, row_group_size=VAL_ROW_GROUP_SIZE)
        print(f"\nWrote val shard: {val_path} ({len(val_rows):,} rows, "
              f"{table.num_columns} cols, "
              f"row_group_size={VAL_ROW_GROUP_SIZE})")

    print(f"\nDone! {total_train:,} train rows across {shard_idx} shards, "
          f"{len(val_rows):,} val rows.")
    print(f"Data directory: {output_dir}")


def _write_shard(output_dir, subset, idx, rows, row_group_size):
    """Write a single train parquet shard."""
    filename = f"{subset}_train_{idx:05d}.parquet"
    filepath = os.path.join(output_dir, filename)
    table = pa.table({"text": rows})
    pq.write_table(table, filepath, row_group_size=row_group_size)
    print(f"  Wrote {filepath} ({len(rows):,} rows)")


if __name__ == "__main__":
    main()
