"""
Train a tokenizer using our own BPE Tokenizer library.
In the style of GPT-4 tokenizer.
"""
from nanochat.report import get_report
import os
import time
import argparse
import random
import pyarrow.parquet as pq
import torch
from nanochat.tokenizer import RustBPETokenizer
from nanochat.common import get_base_dir
from nanochat.dataset import list_parquet_files

# -----------------------------------------------------------------------------
# Parse command line arguments

parser = argparse.ArgumentParser(description='Train a BPE tokenizer')
parser.add_argument('--max-chars', type=int, default=2_000_000_000, help='Maximum characters to train on (default: 2B)')
parser.add_argument('--doc-cap', type=int, default=10_000, help='Maximum characters per document (default: 10,000)')
parser.add_argument('--vocab-size', type=int, default=32768, help='Vocabulary size (default: 32768 = 2^15)')
args = parser.parse_args()
print(f"max_chars: {args.max_chars:,}")
print(f"doc_cap: {args.doc_cap:,}")
print(f"vocab_size: {args.vocab_size:,}")

SUBSET_WEIGHTS = {
    "pure": 1.3,
    "bilingual": 1.0,
    "arabic_raw": 0.9,
    "other": 1.0,
}
SUBSET_PREFIXES = ("arabic_raw", "bilingual", "pure")

# -----------------------------------------------------------------------------
# Text iterator


def text_iterator():
    """
    1) Build per-subset iterators from train parquet files
    2) Sample subsets with mild weighting toward pure Darija
    3) Crop every document to args.doc_cap characters
    4) Break when we've seen args.max_chars characters
    """

    def iter_docs(parquet_paths):
        for filepath in parquet_paths:
            pf = pq.ParquetFile(filepath)
            for rg_idx in range(pf.num_row_groups):
                rg = pf.read_row_group(rg_idx)
                for doc in rg.column('text').to_pylist():
                    yield doc

    parquet_paths = list_parquet_files()
    train_paths = parquet_paths[:-
                                1] if len(parquet_paths) > 1 else parquet_paths
    grouped = {"other": []}
    for path in train_paths:
        name = os.path.basename(path)
        matched = False
        for prefix in SUBSET_PREFIXES:
            if name.startswith(prefix):
                grouped.setdefault(prefix, []).append(path)
                matched = True
                break
        if not matched:
            grouped["other"].append(path)

    subset_iters = {subset: iter_docs(paths)
                    for subset, paths in grouped.items() if paths}
    active_subsets = list(subset_iters.keys())
    if not active_subsets:
        raise RuntimeError(
            "No training parquet files found. Did you run scripts.darija_data_prep?")

    rng = random.Random(42)
    print("subset sampling weights:", ", ".join(
        f"{subset}={SUBSET_WEIGHTS.get(subset, 1.0)}" for subset in active_subsets
    ))

    nchars = 0
    while active_subsets and nchars <= args.max_chars:
        weights = [SUBSET_WEIGHTS.get(subset, 1.0)
                   for subset in active_subsets]
        subset = rng.choices(active_subsets, weights=weights, k=1)[0]
        try:
            doc = next(subset_iters[subset])
        except StopIteration:
            active_subsets.remove(subset)
            continue

        doc_text = doc[:args.doc_cap] if len(doc) > args.doc_cap else doc
        nchars += len(doc_text)
        yield doc_text


text_iter = text_iterator()

# -----------------------------------------------------------------------------
# Train the tokenizer
t0 = time.time()
tokenizer = RustBPETokenizer.train_from_iterator(text_iter, args.vocab_size)
t1 = time.time()
train_time = t1 - t0
print(f"Training time: {train_time:.2f}s")

# -----------------------------------------------------------------------------
# Save the tokenizer to disk
base_dir = get_base_dir()
tokenizer_dir = os.path.join(base_dir, "tokenizer")
tokenizer.save(tokenizer_dir)

# -----------------------------------------------------------------------------
# Quick inline sanity check
test_text = """Hello world! This is a test.
Numbers: 123, 4567, 89
Contractions: I'm, you're, it's
Special chars: @#$%^&*()
Unicode: 你好世界 🌍"""
encoded = tokenizer.encode(test_text)
decoded = tokenizer.decode(encoded)
assert decoded == test_text

# -----------------------------------------------------------------------------
# One more thing: we wish to cache a mapping from token id to number of bytes of that token
# for efficient evaluation of bits per byte. Unlike the typical mean loss, this
# allows us to report a loss that is invariant to the vocab size of the tokenizer.
# The bits per byte on the validation set is then one of the primary metrics we care about.
vocab_size = tokenizer.get_vocab_size()
special_set = set(tokenizer.get_special_tokens())
token_strings = [tokenizer.decode([token_id])
                 for token_id in range(vocab_size)]
token_bytes = []
for token_id in range(vocab_size):
    # the Python string representation of this token
    token_str = token_strings[token_id]
    if token_str in special_set:
        token_bytes.append(0)  # special characters are not counted
    else:
        # number of bytes that make up this token
        id_bytes = len(token_str.encode("utf-8"))
        token_bytes.append(id_bytes)
token_bytes = torch.tensor(token_bytes, dtype=torch.int32, device='cpu')
token_bytes_path = os.path.join(tokenizer_dir, "token_bytes.pt")
with open(token_bytes_path, "wb") as f:
    torch.save(token_bytes, f)
print(f"Saved token_bytes to {token_bytes_path}")

# Log to report
token_bytes_nonzero = (token_bytes[token_bytes > 0]).to(dtype=torch.float32)
get_report().log(section="Tokenizer training", data=[
    vars(args),  # argparse command line arguments
    {"train_time": train_time},
    {"num_special_tokens": len(special_set)},
    {
        "token_bytes_min": int(token_bytes_nonzero.min().item()),
        "token_bytes_max": int(token_bytes_nonzero.max().item()),
        "token_bytes_mean": token_bytes_nonzero.mean().item(),
        "token_bytes_std": token_bytes_nonzero.std().item(),
    }
])
