"""Estimate nanochat-tokenizer token count for Lyte/darija-pretraining-corpus.

Streams N sample rows from each subset (arabic_raw, bilingual, pure),
tokenizes the `text` field with our trained RustBPE/tiktoken tokenizer,
and extrapolates to the full subset if row counts are supplied.

Usage:
    python dev/count_pretrain_corpus_tokens.py --n 2000
    python dev/count_pretrain_corpus_tokens.py --n 5000 \
        --rows-arabic_raw 800000 --rows-bilingual 400000 --rows-pure 200000
"""

import argparse
import os
import sys
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-id", default="Lyte/darija-pretraining-corpus")
    ap.add_argument("--subsets", nargs="+",
                    default=["arabic_raw", "bilingual", "pure"])
    ap.add_argument("--n", type=int, default=2000,
                    help="sample rows per subset")
    ap.add_argument("--rows-arabic_raw", type=int, default=0,
                    help="total rows in arabic_raw subset (0 = skip extrapolation)")
    ap.add_argument("--rows-bilingual", type=int, default=0)
    ap.add_argument("--rows-pure", type=int, default=0)
    args = ap.parse_args()

    from nanochat.tokenizer import RustBPETokenizer
    from nanochat.common import get_base_dir
    from datasets import load_dataset

    tok_dir = os.path.join(get_base_dir(), "tokenizer")
    if not os.path.isdir(tok_dir):
        sys.exit(f"tokenizer dir not found: {tok_dir}")
    tok = RustBPETokenizer.from_directory(tok_dir)
    print(f"loaded tokenizer  vocab={tok.get_vocab_size()}")

    hf_token = os.environ.get("HF_TOKEN")
    row_counts = {
        "arabic_raw": args.rows_arabic_raw,
        "bilingual": args.rows_bilingual,
        "pure": args.rows_pure,
    }

    totals = {"rows": 0, "chars": 0, "tokens": 0}
    per_subset = {}

    for subset in args.subsets:
        print(f"\n--- subset: {subset} ---")
        ds = load_dataset(args.repo_id, subset, split="train",
                          streaming=True, token=hf_token)
        t0 = time.time()
        n_rows = 0
        n_chars = 0
        n_toks = 0
        for row in ds:
            text = row.get("text") or ""
            if not text:
                continue
            n_chars += len(text)
            n_toks += len(tok.encode(text))
            n_rows += 1
            if n_rows >= args.n:
                break
        dt = time.time() - t0
        avg_chars = n_chars / max(n_rows, 1)
        avg_toks = n_toks / max(n_rows, 1)
        chars_per_tok = n_chars / max(n_toks, 1)
        print(f"  sampled {n_rows} rows in {dt:.1f}s")
        print(f"  avg chars/row:  {avg_chars:7.1f}")
        print(f"  avg tokens/row: {avg_toks:7.1f}")
        print(f"  chars/tok:      {chars_per_tok:5.2f}")
        per_subset[subset] = {
            "rows_sampled": n_rows,
            "avg_chars": avg_chars,
            "avg_toks": avg_toks,
            "chars_per_tok": chars_per_tok,
        }
        totals["rows"] += n_rows
        totals["chars"] += n_chars
        totals["tokens"] += n_toks

        # Extrapolate if total rows provided
        total_rows = row_counts.get(subset, 0)
        if total_rows > 0:
            est_toks = avg_toks * total_rows
            print(f"  total rows:     {total_rows:,}")
            print(f"  est. tokens:    {est_toks/1e6:8.1f} M")
            per_subset[subset]["est_total_tokens"] = est_toks

    print("\n=== summary across sampled rows ===")
    print(f"  rows:   {totals['rows']:,}")
    print(f"  chars:  {totals['chars']:,}")
    print(f"  tokens: {totals['tokens']:,}")
    if totals["tokens"]:
        print(f"  chars/tok (overall): {totals['chars']/totals['tokens']:.2f}")

    # Total extrapolation if all row counts supplied
    ext_total = sum(per_subset[s].get("est_total_tokens", 0) for s in args.subsets)
    if ext_total > 0:
        print(f"\n=== extrapolated total ===")
        print(f"  {ext_total/1e6:.1f} M tokens  ({ext_total/1e9:.2f} B)")


if __name__ == "__main__":
    main()
