"""Rigorous token count for Lyte/darija-pretraining-corpus.

Strategy:
  1) Pull exact total bytes per subset from HF datasets-server info endpoint
  2) Sample chars/tok ratio from N rows DISTRIBUTED ACROSS SHARDS (not just first)
  3) Extrapolate: est_tokens = total_chars / (chars_per_tok)

Usage:
  python dev/count_corpus_exact.py --n 3000
"""

import argparse
import os
import sys
import time


def sample_chars_per_tok(tok, repo_id, subset, n_target, hf_token):
    """Sample evenly across shards of a subset using datasets streaming
    with shuffle buffer so we get coverage beyond the first parquet."""
    from datasets import load_dataset
    ds = load_dataset(repo_id, subset, split="train",
                      streaming=True, token=hf_token)
    # Shuffle buffer spreads the draw across shards as it iterates
    ds = ds.shuffle(seed=42, buffer_size=10_000)
    n_rows = 0
    n_chars = 0
    n_toks = 0
    t0 = time.time()
    for row in ds:
        text = row.get("text") or ""
        if not text:
            continue
        n_chars += len(text)
        n_toks += len(tok.encode(text))
        n_rows += 1
        if n_rows >= n_target:
            break
    dt = time.time() - t0
    return {
        "rows": n_rows,
        "chars": n_chars,
        "tokens": n_toks,
        "avg_chars_per_row": n_chars / max(n_rows, 1),
        "avg_tokens_per_row": n_toks / max(n_rows, 1),
        "chars_per_tok": n_chars / max(n_toks, 1),
        "wall": dt,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-id", default="Lyte/darija-pretraining-corpus")
    ap.add_argument("--subsets", nargs="+",
                    default=["arabic_raw", "bilingual", "pure"])
    ap.add_argument("--n", type=int, default=3000,
                    help="sample rows per subset (shuffle-buffered for shard spread)")
    args = ap.parse_args()

    import requests
    from nanochat.tokenizer import RustBPETokenizer
    from nanochat.common import get_base_dir

    tok_dir = os.path.join(get_base_dir(), "tokenizer")
    tok = RustBPETokenizer.from_directory(tok_dir)
    print(f"loaded tokenizer  vocab={tok.get_vocab_size()}")

    hf_token = os.environ.get("HF_TOKEN")

    # Pull exact totals from HF info endpoint
    r = requests.get(
        f"https://datasets-server.huggingface.co/info?dataset={args.repo_id}",
        headers={"Authorization": f"Bearer {hf_token}"} if hf_token else {},
    )
    info = r.json()["dataset_info"]
    exact = {}
    for subset, meta in info.items():
        rows = sum(s["num_examples"] for s in meta["splits"].values())
        bytes_ = sum(s.get("num_bytes", 0) for s in meta["splits"].values())
        # num_bytes from HF is the total byte size of the `text` column payload,
        # which for UTF-8 text equals total chars only if all ASCII. Darija/MSA
        # uses Arabic script (2-3 bytes/char in UTF-8), so we need to sample to
        # get the byte/char ratio.
        exact[subset] = {"rows": rows, "bytes": bytes_}

    print(f"\n{'subset':<15} {'rows':>12} {'bytes (MB)':>12}")
    for s, d in exact.items():
        print(f"{s:<15} {d['rows']:>12,} {d['bytes']/1e6:>12.1f}")

    # Sample each subset with shard spread
    print(f"\nSampling {args.n} rows per subset with shuffle buffer "
          f"(spreads draw across shards)...")
    totals_unique_chars = 0
    totals_unique_toks = 0
    totals_rows_actual = 0
    rows_all = []
    for subset in args.subsets:
        if subset not in exact:
            print(f"WARN: {subset} not in info endpoint, skipping")
            continue
        print(f"\n--- {subset} (shuffle-buffered sample) ---")
        sample = sample_chars_per_tok(
            tok, args.repo_id, subset, args.n, hf_token)
        bytes_per_char = exact[subset]["bytes"] / max(
            sample["avg_chars_per_row"] * exact[subset]["rows"], 1)
        # Direct extrapolation: est_total_tokens = avg_tokens_per_row * total_rows
        est_toks = sample["avg_tokens_per_row"] * exact[subset]["rows"]
        est_chars = sample["avg_chars_per_row"] * exact[subset]["rows"]
        print(f"  sampled rows:            {sample['rows']:>8,}  in {sample['wall']:.1f}s")
        print(f"  avg chars/row:           {sample['avg_chars_per_row']:>8.1f}")
        print(f"  avg tokens/row:          {sample['avg_tokens_per_row']:>8.1f}")
        print(f"  chars/tok:               {sample['chars_per_tok']:>8.2f}")
        print(f"  total rows in subset:    {exact[subset]['rows']:>12,}")
        print(f"  est. total chars:        {est_chars/1e9:>8.2f} B")
        print(f"  est. total tokens:       {est_toks/1e9:>8.2f} B")
        rows_all.append((subset, est_toks, est_chars, sample["chars_per_tok"]))
        totals_unique_chars += est_chars
        totals_unique_toks += est_toks
        totals_rows_actual += sample["rows"]

    print("\n" + "=" * 60)
    print("ESTIMATED TOTAL (full darija-pretraining-corpus)")
    print("=" * 60)
    for subset, toks, chars, cpt in rows_all:
        print(f"  {subset:<15} {toks/1e9:>6.2f} B tok   "
              f"(chars/tok={cpt:.2f})")
    print(f"  {'—'*42}")
    print(f"  {'total':<15} {totals_unique_toks/1e9:>6.2f} B tok")


if __name__ == "__main__":
    main()
