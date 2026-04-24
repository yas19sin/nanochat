"""Download shards of Lyte/fineweb-edu-darija-translated and convert them
into two nanochat-style parquet files with a `text` column, using the
existing SUBSET_PREFIXES in scripts/tok_train.py:

    data/shard_NNNNN.parquet  ->  darijapure_fwedu_00000.parquet (darija column)
                              ->  fineweb_fwedu_00000.parquet    (en column)

Usage:
    python dev/prep_fwedu_for_tokenizer.py --n-shards 30
"""

import argparse
import os
import sys
import time


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-id", default="Lyte/fineweb-edu-darija-translated")
    ap.add_argument("--n-shards", type=int, default=30,
                    help="number of shards to download (each ~10k rows / ~19MB)")
    ap.add_argument("--out-subdir", default="ablation_mixed",
                    help="subdir under ~/.cache/nanochat to merge into")
    args = ap.parse_args()

    from huggingface_hub import HfApi, hf_hub_download
    import pyarrow as pa
    import pyarrow.parquet as pq
    from nanochat.common import get_base_dir

    base = get_base_dir()
    out_dir = os.path.join(base, args.out_subdir)
    os.makedirs(out_dir, exist_ok=True)
    darija_path = os.path.join(out_dir, "darijapure_fwedu_00000.parquet")
    en_path = os.path.join(out_dir, "fineweb_fwedu_00000.parquet")
    print(f"output dir: {out_dir}")

    hf_token = os.environ.get("HF_TOKEN")
    if not hf_token:
        sys.exit("set HF_TOKEN env var")
    api = HfApi(token=hf_token)
    files = sorted(f for f in api.list_repo_files(args.repo_id, repo_type="dataset")
                   if f.startswith("data/shard_") and f.endswith(".parquet"))
    files = files[: args.n_shards]
    if not files:
        sys.exit("no shards found")
    print(f"will pull {len(files)} shards")

    darija_docs: list[str] = []
    en_docs: list[str] = []
    t0 = time.time()
    for i, f in enumerate(files):
        local = hf_hub_download(args.repo_id, f, repo_type="dataset",
                                token=hf_token)
        table = pq.read_table(local, columns=["en", "darija"])
        en_col = table.column("en").to_pylist()
        dr_col = table.column("darija").to_pylist()
        for en, dr in zip(en_col, dr_col):
            if en:
                en_docs.append(en)
            if dr:
                darija_docs.append(dr)
        if (i + 1) % 5 == 0 or i == len(files) - 1:
            dt = time.time() - t0
            print(f"  [{i+1}/{len(files)}] darija_docs={len(darija_docs):,} "
                  f"en_docs={len(en_docs):,}  {dt:.1f}s")

    print(f"\nwriting {darija_path}")
    pq.write_table(pa.table({"text": darija_docs}), darija_path)
    print(f"writing {en_path}")
    pq.write_table(pa.table({"text": en_docs}), en_path)

    print("\nsummary")
    print(f"  darija docs: {len(darija_docs):,}  "
          f"chars: {sum(len(x) for x in darija_docs):,}")
    print(f"  en docs:     {len(en_docs):,}  "
          f"chars: {sum(len(x) for x in en_docs):,}")


if __name__ == "__main__":
    main()
