"""Estimate nanochat-tokenizer token count for Lyte/fineweb-edu-darija-translated.

Streams N sample rows from the HF dataset, tokenizes the `darija` field with
our trained RustBPE/tiktoken tokenizer, and extrapolates to the full dataset
using manifest.json's reported row count.

Usage:
    python dev/count_darija_tokens.py --n 2000
"""

import argparse
import os
import sys
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-id", default="Lyte/fineweb-edu-darija-translated")
    ap.add_argument("--n", type=int, default=2000, help="sample rows")
    ap.add_argument("--total-rows", type=int, default=2_000_000,
                    help="assumed total rows in dataset for extrapolation")
    args = ap.parse_args()

    from nanochat.tokenizer import RustBPETokenizer
    from nanochat.common import get_base_dir
    from huggingface_hub import HfApi, hf_hub_download
    import pyarrow.parquet as pq

    tok_dir = os.path.join(get_base_dir(), "tokenizer")
    if not os.path.isdir(tok_dir):
        sys.exit(f"tokenizer dir not found: {tok_dir}")
    tok = RustBPETokenizer.from_directory(tok_dir)
    print(f"loaded tokenizer  vocab={tok.get_vocab_size()}")

    hf_token = os.environ.get("HF_TOKEN")
    api = HfApi(token=hf_token)
    # stream parquet shards directly (bypasses gated dataset-module check)
    files = [f for f in api.list_repo_files(args.repo_id, repo_type="dataset")
             if f.startswith("data/shard_") and f.endswith(".parquet")]
    files.sort()
    if not files:
        sys.exit("no shard_*.parquet files found in repo")
    print(f"found {len(files)} shard files; using first few until --n rows reached")

    def row_stream():
        for fname in files:
            local = hf_hub_download(args.repo_id, fname, repo_type="dataset",
                                    token=hf_token)
            table = pq.read_table(local, columns=["en", "darija", "output_tokens"])
            for row in table.to_pylist():
                yield row

    ds_iter = row_stream()

    t0 = time.time()
    n_rows = 0
    n_chars_darija = 0
    n_chars_en = 0
    n_toks_nc_darija = 0
    n_toks_nc_en = 0
    n_toks_aya_reported = 0

    for row in ds_iter:
        en = row.get("en") or ""
        dr = row.get("darija") or ""
        n_toks_nc_darija += len(tok.encode(dr))
        n_toks_nc_en += len(tok.encode(en))
        n_toks_aya_reported += int(row.get("output_tokens") or 0)
        n_chars_darija += len(dr)
        n_chars_en += len(en)
        n_rows += 1
        if n_rows >= args.n:
            break

    dt = time.time() - t0
    print(f"\nsampled {n_rows} rows in {dt:.1f}s")
    print(f"  avg EN chars/row:     {n_chars_en / n_rows:7.1f}")
    print(f"  avg DARIJA chars/row: {n_chars_darija / n_rows:7.1f}")
    print()
    print(f"  avg nanochat toks/row  (en):     {n_toks_nc_en / n_rows:7.1f}")
    print(f"  avg nanochat toks/row  (darija): {n_toks_nc_darija / n_rows:7.1f}")
    print(f"  avg Aya toks/row       (darija): {n_toks_aya_reported / n_rows:7.1f}  (reported)")
    print()
    ratio = n_toks_nc_darija / max(n_toks_aya_reported, 1)
    print(f"  nanochat/Aya ratio on Darija: {ratio:.3f}")
    print(f"  chars/nanochat-tok (Darija): {n_chars_darija / max(n_toks_nc_darija,1):.2f}")
    print(f"  chars/nanochat-tok (English): {n_chars_en / max(n_toks_nc_en,1):.2f}")
    print()
    est_total_darija = n_toks_nc_darija * args.total_rows / n_rows
    est_total_en = n_toks_nc_en * args.total_rows / n_rows
    print(f"Extrapolated to {args.total_rows:,} rows:")
    print(f"  Darija tokens (nanochat):  {est_total_darija/1e6:8.1f} M")
    print(f"  English tokens (nanochat): {est_total_en/1e6:8.1f} M")


if __name__ == "__main__":
    main()
