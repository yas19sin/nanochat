"""Count nanochat-tokenizer tokens for Lyte/fineweb-edu-darija-translated.

The translation run stores `output_tokens`, which are generated-token counts
from Lyte/tiny-aya-darija-v5's tokenizer. Nanochat trains with its local
RustBPE tokenizer, so use this script to recount the `darija` field.

Usage:
    # Fast estimate from local Vast output:
    python dev/count_darija_tokens.py --out-dir /workspace/darija_out --n 20000

    # Exact local count:
    python dev/count_darija_tokens.py --out-dir /workspace/darija_out --exact
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path


def load_manifest(path: Path | None, repo_id: str, hf_token: str | None) -> tuple[dict | None, Path | None]:
    if path and path.exists():
        return json.loads(path.read_text(encoding="utf-8")), path

    try:
        from huggingface_hub import hf_hub_download
        downloaded = Path(hf_hub_download(repo_id, "manifest.json",
                                          repo_type="dataset", token=hf_token))
        return json.loads(downloaded.read_text(encoding="utf-8")), downloaded
    except Exception:
        return None, None


def manifest_summary(manifest: dict | None) -> tuple[int | None, int | None]:
    if not manifest:
        return None, None
    shards = manifest.get("shards") or []
    rows = sum(int(s.get("rows") or 0) for s in shards) or manifest.get("rows_accepted")
    aya_tokens = manifest.get("output_tokens")
    if not aya_tokens:
        aya_tokens = sum(int(s.get("tokens") or 0) for s in shards)
    return int(rows or 0), int(aya_tokens or 0)


def iter_shard_paths(entries: list[dict], out_dir: Path | None, repo_id: str,
                     hf_token: str | None):
    from huggingface_hub import hf_hub_download

    for entry in entries:
        fname = entry["file"]
        if out_dir:
            local = out_dir / fname
            if local.exists():
                yield local
                continue
        yield Path(hf_hub_download(repo_id, f"data/{fname}",
                                   repo_type="dataset", token=hf_token))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-id", default="Lyte/fineweb-edu-darija-translated")
    ap.add_argument("--out-dir", default=os.environ.get("DARIJA_OUT_DIR", "/workspace/darija_out"),
                    help="local translation output directory with parquet shards")
    ap.add_argument("--manifest", default=None,
                    help="manifest.json path; defaults to <out-dir>/manifest.json or Hub")
    ap.add_argument("--n", type=int, default=20_000,
                    help="sample rows for estimate; ignored with --exact")
    ap.add_argument("--exact", action="store_true",
                    help="count every row instead of sampling")
    ap.add_argument("--threads", type=int, default=8,
                    help="tokenizer threads for batch encoding")
    ap.add_argument("--tokenizer-dir", default=None,
                    help="directory containing nanochat tokenizer.pkl; defaults to get_base_dir()/tokenizer")
    args = ap.parse_args()

    from nanochat.tokenizer import RustBPETokenizer
    from nanochat.common import get_base_dir
    import pyarrow.parquet as pq

    tok_dir = args.tokenizer_dir or os.path.join(get_base_dir(), "tokenizer")
    if not os.path.isdir(tok_dir):
        sys.exit(f"tokenizer dir not found: {tok_dir}")
    if not os.path.exists(os.path.join(tok_dir, "tokenizer.pkl")):
        sys.exit(f"tokenizer.pkl not found in: {tok_dir}")
    tok = RustBPETokenizer.from_directory(tok_dir)
    print(f"loaded tokenizer: {tok_dir}")
    print(f"loaded tokenizer  vocab={tok.get_vocab_size()}")

    hf_token = (
        os.environ.get("HF_TOKEN")
        or os.environ.get("HF_WRITE_TOKEN")
        or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    )
    out_dir = Path(args.out_dir) if args.out_dir else None
    if out_dir and not out_dir.exists():
        out_dir = None
    manifest_path = Path(args.manifest) if args.manifest else (
        out_dir / "manifest.json" if out_dir else None
    )
    manifest, manifest_source = load_manifest(manifest_path, args.repo_id, hf_token)
    rows_manifest, aya_manifest_tokens = manifest_summary(manifest)
    if manifest_source:
        print(f"loaded manifest: {manifest_source}")
    if rows_manifest is not None:
        print(f"manifest rows: {rows_manifest:,}")
        print(f"manifest Aya tokens: {aya_manifest_tokens:,}")

    entries = manifest.get("shards") if manifest else None
    if not entries:
        sys.exit("no manifest shards found; provide --manifest or --out-dir")

    limit = None if args.exact else args.n
    print("count mode:", "exact" if args.exact else f"sample first {limit:,} rows")

    t0 = time.time()
    n_rows = 0
    n_chars_darija = 0
    n_chars_en = 0
    n_toks_nc_darija = 0
    n_toks_nc_en = 0
    n_toks_aya_reported = 0

    for shard_path in iter_shard_paths(entries, out_dir, args.repo_id, hf_token):
        pf = pq.ParquetFile(shard_path)
        for rg_idx in range(pf.num_row_groups):
            table = pf.read_row_group(rg_idx, columns=["en", "darija", "output_tokens"])
            rows = table.to_pylist()
            if limit is not None:
                rows = rows[:max(0, limit - n_rows)]
                if not rows:
                    break
            ens = [(row.get("en") or "") for row in rows]
            drs = [(row.get("darija") or "") for row in rows]
            n_toks_nc_darija += sum(len(ids) for ids in tok.encode(drs, num_threads=args.threads))
            n_toks_nc_en += sum(len(ids) for ids in tok.encode(ens, num_threads=args.threads))
            n_toks_aya_reported += sum(int(row.get("output_tokens") or 0) for row in rows)
            n_chars_darija += sum(len(x) for x in drs)
            n_chars_en += sum(len(x) for x in ens)
            n_rows += len(rows)
            if limit is not None and n_rows >= limit:
                break
        if limit is not None and n_rows >= limit:
            break

    if n_rows == 0:
        sys.exit("no rows counted")

    dt = time.time() - t0
    print(f"\ncounted {n_rows:,} rows in {dt:.1f}s")
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
    if args.exact:
        print("Exact counted totals:")
        print(f"  Darija tokens (nanochat):  {n_toks_nc_darija:,}")
        print(f"  English tokens (nanochat): {n_toks_nc_en:,}")
    elif rows_manifest:
        est_total_darija = n_toks_nc_darija * rows_manifest / n_rows
        est_total_en = n_toks_nc_en * rows_manifest / n_rows
        est_total_aya = n_toks_aya_reported * rows_manifest / n_rows
        print(f"Extrapolated to manifest rows ({rows_manifest:,}):")
        print(f"  Darija tokens (nanochat):  {est_total_darija/1e6:8.1f} M")
        print(f"  English tokens (nanochat): {est_total_en/1e6:8.1f} M")
        print(f"  Aya tokens sampled est.:   {est_total_aya/1e6:8.1f} M")


if __name__ == "__main__":
    main()
