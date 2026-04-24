"""Inspect schemas of the datasets we'll translate."""
import json
import os
from datasets import load_dataset

HF_TOKEN = os.environ.get("HF_TOKEN")

def peek(name, **kw):
    print("=" * 70)
    print(f"DATASET: {name}  kw={kw}")
    print("=" * 70)
    try:
        ds = load_dataset(name, streaming=True, token=HF_TOKEN, **kw)
        if hasattr(ds, "keys"):
            split = list(ds.keys())[0]
            ds = ds[split]
        it = iter(ds)
        for i in range(2):
            row = next(it)
            print(f"\n--- row {i} keys: {list(row.keys())}")
            for k, v in row.items():
                s = repr(v)
                if len(s) > 800:
                    s = s[:800] + f"...<truncated {len(s)} total>"
                print(f"  [{k}] = {s}")
    except Exception as e:
        print(f"ERROR: {e}")
    print()

peek("NousResearch/hermes-function-calling-v1", split="train")
peek("Team-ACE/ToolACE", split="train")
peek("m-a-p/CodeFeedback-Filtered-Instruction", split="train")
peek("HuggingFaceTB/finemath", name="finemath-4plus", split="train")
