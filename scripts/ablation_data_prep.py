"""
Multi-source pretraining data prep for nanochat architecture ablations.

Streams from several HuggingFace datasets, normalizes each row to a single
`text` document, and writes standard nanochat parquet shards. The last
shard (alphabetically) is the validation shard per dataloader convention.

Default sources (edit SOURCES list below to change):
  1. Lyte/darija-translation-data  config=fineweb   → text (darija) + text_en
  2. Lyte/darija-pretraining-corpus config=pure     → text (darija)
  3. HuggingFaceFW/finephrase       config=all      → text (high-quality English
                                                      from FineWeb-Edu source col)

Each source has its own row cap to keep total download reasonable (~few GB).

Usage (respects $HF_TOKEN / $HUGGINGFACE_HUB_TOKEN):
    python -m scripts.ablation_data_prep                # defaults
    python -m scripts.ablation_data_prep --darija-only  # skip English
    python -m scripts.ablation_data_prep --max-rows 200000  # smoke cap all sources
"""

import os
import argparse
import glob
import pyarrow as pa
import pyarrow.parquet as pq

# ---------------------------------------------------------------------------
# Source specs.  Each entry:
#   key       : short tag used as parquet filename prefix
#   dataset   : HF dataset id
#   config    : HF config name (or None)
#   split     : HF split
#   extractor : row -> list[str]  (one row may yield 1 or 2 docs)
#   cap       : max rows to read from this source (None = unlimited)
#   lang      : 'darija' | 'english' (for val-set reservation; we only hold
#                out darija rows for validation since darija is the target)
# ---------------------------------------------------------------------------

def _fineweb_extract(row):
    out = []
    d = (row.get("text") or "").strip()
    e = (row.get("text_en") or "").strip()
    if len(d) >= MIN_CHARS:
        out.append(d)
    if len(e) >= MIN_CHARS:
        out.append(e)
    return out


def _plain_text_extract(row):
    t = (row.get("text") or "").strip()
    return [t] if len(t) >= MIN_CHARS else []


# If you want FinePhrase's synthetic generated content instead of the
# FineWeb-Edu source text, swap `_plain_text_extract` for this:
def _finephrase_generated_extract(row):
    rr = row.get("rollout_results") or []
    if rr and isinstance(rr, list):
        t = ((rr[0] or {}).get("text") or "").strip()
        return [t] if len(t) >= MIN_CHARS else []
    return []


MIN_CHARS = 16

SOURCES = [
    # (key,          dataset,                         config,       split,   extractor,             cap,       lang)
    ("fineweb",      "Lyte/darija-translation-data",  "fineweb",    "train", _fineweb_extract,        None,     "darija"),
    ("darijapure",   "Lyte/darija-pretraining-corpus", "pure",      "train", _plain_text_extract,     500_000,  "darija"),
    ("finephrase",   "HuggingFaceFW/finephrase",      "all",        "train", _plain_text_extract,     1_000_000, "english"),
]

TRAIN_SHARD_SIZE = 500_000
VAL_SIZE = 20_000
VAL_ROW_GROUP_SIZE = 2_000
TRAIN_ROW_GROUP_SIZE = 25_000


def get_output_dir():
    env = os.environ.get("NANOCHAT_DATA_DIR")
    if env:
        return env
    cache = os.environ.get(
        "NANOCHAT_BASE_DIR",
        os.path.join(os.path.expanduser("~"), ".cache", "nanochat"),
    )
    return os.path.join(cache, "ablation_mixed")


def _write_shard(output_dir, key, idx, rows, row_group_size):
    filename = f"{key}_train_{idx:05d}.parquet"
    filepath = os.path.join(output_dir, filename)
    table = pa.table({"text": rows})
    pq.write_table(table, filepath, row_group_size=row_group_size)
    print(f"  Wrote {filepath} ({len(rows):,} rows)")


def main():
    parser = argparse.ArgumentParser(description="Mixed pretraining prep for ablation")
    parser.add_argument("--output-dir", type=str, default=None)
    parser.add_argument("--cache-dir", type=str, default=None)
    parser.add_argument("--hf-token", type=str, default=None)
    parser.add_argument("--darija-only", action="store_true",
                        help="skip English sources (only keep lang=='darija')")
    parser.add_argument("--max-rows", type=int, default=-1,
                        help="global cap per source (-1 = use per-source caps above)")
    parser.add_argument("--val-size", type=int, default=VAL_SIZE)
    parser.add_argument("--shard-size", type=int, default=TRAIN_SHARD_SIZE)
    parser.add_argument("--no-streaming", action="store_true",
                        help="download locally (uses lots of disk for finephrase!)")
    parser.add_argument("--force", action="store_true",
                        help="re-download sources even if their shards already exist")
    args = parser.parse_args()

    token = args.hf_token or os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    if token:
        os.environ["HF_TOKEN"] = token
        os.environ["HUGGINGFACE_HUB_TOKEN"] = token
        print("Using HF token.")
    else:
        print("No HF token (public datasets still work).")

    from datasets import load_dataset  # import after token envs are set

    output_dir = args.output_dir or get_output_dir()
    os.makedirs(output_dir, exist_ok=True)
    print(f"Output directory: {output_dir}")

    val_rows = []      # darija only
    train_idx_by_key = {}
    total_train = 0

    for key, ds_id, config, split, extractor, cap, lang in SOURCES:
        if args.darija_only and lang != "darija":
            print(f"\n--- SKIPPING source '{key}' ({ds_id}) because --darija-only ---")
            continue

        effective_cap = args.max_rows if args.max_rows > 0 else cap
        print(f"\n--- Loading source '{key}': {ds_id} config={config} split={split} "
              f"cap={effective_cap or 'unlimited'} ---")

        # idempotent rerun: if shards already exist for this source, skip it
        existing = sorted(glob.glob(os.path.join(output_dir, f"{key}_train_*.parquet")))
        if existing and not args.force:
            print(f"  ** found {len(existing)} existing shard(s) for '{key}', skipping "
                  f"(use --force to re-download).")
            train_idx_by_key[key] = len(existing)
            continue

        try:
            ds = load_dataset(
                ds_id,
                name=config,
                split=split,
                streaming=not args.no_streaming,
                cache_dir=args.cache_dir,
                token=token,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  !! failed to load {ds_id}: {exc}")
            print(f"  !! skipping source '{key}' and continuing.")
            continue

        buf = []
        idx = 0
        seen = 0
        # wrap iteration so a mid-stream network error doesn't kill the whole job
        try:
            for row in ds:
                seen += 1
                if effective_cap and seen > effective_cap:
                    break
                docs = extractor(row)
                for doc in docs:
                    if lang == "darija" and len(val_rows) < args.val_size:
                        val_rows.append(doc)
                        continue
                    buf.append(doc)
                    if len(buf) >= args.shard_size:
                        _write_shard(output_dir, key, idx, buf, TRAIN_ROW_GROUP_SIZE)
                        total_train += len(buf)
                        idx += 1
                        buf = []
                if seen % 100_000 == 0:
                    print(f"  ...{key}: seen {seen:,} rows, train_so_far={total_train + len(buf):,}")
        except Exception as exc:  # noqa: BLE001
            print(f"  !! stream for '{key}' died after {seen:,} rows: {exc}")
            print(f"  !! flushing partial buffer ({len(buf)} docs) and moving on.")

        if buf:
            _write_shard(output_dir, key, idx, buf, TRAIN_ROW_GROUP_SIZE)
            total_train += len(buf)
            idx += 1
        train_idx_by_key[key] = idx
        print(f"  source '{key}' done: {seen:,} rows streamed, {idx} shards written.")

    # Write val shard LAST (dataloader reads the last file as val)
    if not val_rows:
        print("WARNING: no validation rows collected.")
    else:
        val_path = os.path.join(output_dir, "zzz_val_00000.parquet")
        table = pa.table({"text": val_rows})
        pq.write_table(table, val_path, row_group_size=VAL_ROW_GROUP_SIZE)
        print(f"\nWrote val shard: {val_path} ({len(val_rows):,} rows)")

    print(f"\nDone. Total train docs: {total_train:,}. Val docs: {len(val_rows):,}.")
    print(f"Shards per source: {train_idx_by_key}")
    print(f"Data directory: {output_dir}")


if __name__ == "__main__":
    main()
