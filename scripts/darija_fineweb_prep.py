"""
Prepare the Darija FineWeb-style pretraining dataset for nanochat.

Downloads **Lyte/darija-translation-data** (split=`fineweb`) from HuggingFace,
which contains two columns:
    - text     : Darija (Moroccan Arabic) translation
    - text_en  : original English text

For pretraining we emit BOTH columns as separate documents (doubling the
effective token budget and giving the model natural bilingual exposure).
Output is standard nanochat parquet shards (single `text` column).

Usage:
    # token picked up from $HF_TOKEN automatically
    pip install "datasets>=2.19" pyarrow huggingface_hub
    python -m scripts.darija_fineweb_prep

    # or explicit
    python -m scripts.darija_fineweb_prep --hf-token hf_xxx --no-streaming

Output goes to $NANOCHAT_DATA_DIR (default: ~/.cache/nanochat/darija_fineweb).
Train shards are written first, a single val shard is written LAST
(dataloader convention: last file = val) with a small row_group_size so
every DDP rank gets at least one row group.
"""

import os
import argparse
import pyarrow as pa
import pyarrow.parquet as pq

# ---------------------------------------------------------------------------
# Defaults

HF_DATASET = "Lyte/darija-translation-data"
HF_CONFIG = "fineweb"     # dataset config name (per HF hub)
HF_SPLIT = "train"        # the only split inside that config
TRAIN_SHARD_SIZE = 500_000    # rows per train shard (each row ≈ 1 doc)
VAL_SIZE = 20_000             # rows held out for validation
VAL_ROW_GROUP_SIZE = 2_000    # ≥8 row groups so multi-GPU validation never hangs
TRAIN_ROW_GROUP_SIZE = 25_000
MIN_CHARS = 16                # skip near-empty rows


def get_output_dir():
    env = os.environ.get("NANOCHAT_DATA_DIR")
    if env:
        return env
    cache = os.environ.get(
        "NANOCHAT_BASE_DIR",
        os.path.join(os.path.expanduser("~"), ".cache", "nanochat"),
    )
    return os.path.join(cache, "darija_fineweb")


def _write_shard(output_dir, tag, idx, rows, row_group_size):
    filename = f"{tag}_train_{idx:05d}.parquet"
    filepath = os.path.join(output_dir, filename)
    table = pa.table({"text": rows})
    pq.write_table(table, filepath, row_group_size=row_group_size)
    print(f"  Wrote {filepath} ({len(rows):,} rows)")


def main():
    parser = argparse.ArgumentParser(
        description="Prepare Darija FineWeb translation data")
    parser.add_argument("--val-size", type=int, default=VAL_SIZE)
    parser.add_argument("--shard-size", type=int, default=TRAIN_SHARD_SIZE)
    parser.add_argument("--output-dir", type=str, default=None)
    parser.add_argument("--no-streaming", action="store_true",
                        help="download locally first (faster iteration, more disk)")
    parser.add_argument("--cache-dir", type=str, default=None,
                        help="HuggingFace datasets cache directory")
    parser.add_argument("--hf-token", type=str, default=None,
                        help="HF access token (defaults to $HF_TOKEN / $HUGGINGFACE_HUB_TOKEN)")
    parser.add_argument("--darija-only", action="store_true",
                        help="emit only the Darija `text` column (skip `text_en`)")
    parser.add_argument("--max-rows", type=int, default=-1,
                        help="stop after N source rows (-1 = all). Useful for smoke tests")
    args = parser.parse_args()

    # Resolve HF token (env var is the standard path; flag overrides)
    token = args.hf_token or os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    if token:
        os.environ["HF_TOKEN"] = token
        os.environ["HUGGINGFACE_HUB_TOKEN"] = token
        print("Using HF token from " + ("--hf-token flag" if args.hf_token else "environment"))
    else:
        print("No HF token provided (dataset may still work if public).")

    # Import here so the help text / arg parsing doesn't require datasets installed
    from datasets import load_dataset

    output_dir = args.output_dir or get_output_dir()
    os.makedirs(output_dir, exist_ok=True)
    print(f"Output directory: {output_dir}")
    print(f"Loading {HF_DATASET} config={HF_CONFIG} split={HF_SPLIT} "
          f"({'local' if args.no_streaming else 'streaming'})")

    ds = load_dataset(
        HF_DATASET,
        name=HF_CONFIG,
        split=HF_SPLIT,
        streaming=not args.no_streaming,
        cache_dir=args.cache_dir,
        token=token,
    )

    val_rows = []
    train_idx = 0
    train_buf = []
    total_train = 0
    seen = 0

    for row in ds:
        seen += 1
        if args.max_rows > 0 and seen > args.max_rows:
            break

        darija = (row.get("text") or "").strip()
        english = (row.get("text_en") or "").strip()

        # Hold out some Darija rows for validation (Darija is the target language)
        if darija and len(darija) >= MIN_CHARS and len(val_rows) < args.val_size:
            val_rows.append(darija)
            # still use the matching English as training data if present
            if not args.darija_only and len(english) >= MIN_CHARS:
                train_buf.append(english)
        else:
            if darija and len(darija) >= MIN_CHARS:
                train_buf.append(darija)
            if not args.darija_only and len(english) >= MIN_CHARS:
                train_buf.append(english)

        if len(train_buf) >= args.shard_size:
            _write_shard(output_dir, "fineweb", train_idx,
                         train_buf, TRAIN_ROW_GROUP_SIZE)
            total_train += len(train_buf)
            train_idx += 1
            train_buf = []

        if seen % 100_000 == 0:
            print(f"  ...seen {seen:,} rows | train_rows={total_train + len(train_buf):,} "
                  f"| val_rows={len(val_rows):,}")

    # Flush remaining train rows
    if train_buf:
        _write_shard(output_dir, "fineweb", train_idx,
                     train_buf, TRAIN_ROW_GROUP_SIZE)
        total_train += len(train_buf)
        train_idx += 1

    # Write val shard LAST (dataloader convention: last file = val)
    if not val_rows:
        print("WARNING: no validation rows collected; dataloader will fail on val split.")
    else:
        val_path = os.path.join(output_dir, "zzz_val_00000.parquet")
        table = pa.table({"text": val_rows})
        pq.write_table(table, val_path, row_group_size=VAL_ROW_GROUP_SIZE)
        print(f"\nWrote val shard: {val_path} "
              f"({len(val_rows):,} rows, row_group_size={VAL_ROW_GROUP_SIZE})")

    print(f"\nDone! {total_train:,} train rows across {train_idx} shards, "
          f"{len(val_rows):,} val rows.")
    print(f"Source rows streamed: {seen:,}")
    print(f"Data directory: {output_dir}")


if __name__ == "__main__":
    main()
