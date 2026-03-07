"""Diagnostic script to identify why lm-eval scores are near random.

Run on the H200 box:
    python scripts/diagnose_hf.py --hf-model Lyte/Nanochat-Moroccan-Instruct-702M-hf
"""

import argparse
import pickle
import sys
from pathlib import Path

import torch


def check_tokenizer(hf_dir: str, tokenizer_dir: str | None):
    """Compare token IDs between original tiktoken and exported HF tokenizer."""
    from transformers import AutoTokenizer

    print("=" * 60)
    print("1. TOKENIZER COMPARISON")
    print("=" * 60)

    hf_tok = AutoTokenizer.from_pretrained(hf_dir, trust_remote_code=True)
    print(f"HF tokenizer vocab size: {hf_tok.vocab_size}")
    print(f"HF tokenizer type: {type(hf_tok).__name__}")

    # Try to load original tiktoken tokenizer
    if tokenizer_dir is None:
        from nanochat.common import get_base_dir
        tokenizer_dir = Path(get_base_dir()) / "tokenizer"
    else:
        tokenizer_dir = Path(tokenizer_dir)

    tok_path = tokenizer_dir / "tokenizer.pkl"
    if not tok_path.exists():
        print(f"WARNING: Original tokenizer not found at {tok_path}")
        print("Skipping tokenizer comparison.")
        return

    with open(tok_path, "rb") as f:
        encoding = pickle.load(f)
    print(f"Original tiktoken vocab size: {encoding.n_vocab}")

    # Test strings in various languages
    test_strings = [
        "Hello world",
        "شنو هي الدارجة المغربية؟",
        "كيفاش نقدر نتعلم الدارجة؟",
        "The quick brown fox",
        "1 + 1 = 2",
        "<|bos|>",
        "<|user_start|>",
        "<|assistant_start|>",
    ]

    mismatches = 0
    for text in test_strings:
        try:
            orig_ids = encoding.encode(text, allowed_special="all")
        except Exception:
            orig_ids = encoding.encode(text)
        hf_ids = hf_tok.encode(text, add_special_tokens=False)
        match = orig_ids == hf_ids
        status = "OK" if match else "MISMATCH"
        if not match:
            mismatches += 1
        print(f"\n  [{status}] '{text}'")
        print(f"    tiktoken: {orig_ids}")
        print(f"    HF:       {hf_ids}")

    if mismatches > 0:
        print(
            f"\n*** {mismatches}/{len(test_strings)} tokenizer mismatches found! ***")
        print("This is likely the root cause of near-random eval scores.")
        print("Re-export the model to fix.")
    else:
        print("\nAll tokenizer checks passed. Issue is NOT the tokenizer.")


def check_logits(hf_dir: str):
    """Check if model logits look reasonable (not uniform)."""
    from transformers import AutoModelForCausalLM, AutoTokenizer

    print("\n" + "=" * 60)
    print("2. LOGIT DISTRIBUTION CHECK")
    print("=" * 60)

    tok = AutoTokenizer.from_pretrained(hf_dir, trust_remote_code=True)
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    model = AutoModelForCausalLM.from_pretrained(
        hf_dir, trust_remote_code=True, dtype=torch.bfloat16,
    ).to(device)
    model.eval()

    text = "شنو هي الدارجة المغربية؟"
    inputs = tok(text, return_tensors="pt")
    inputs = {k: v.to(device) for k, v in inputs.items()
              if k in {"input_ids", "attention_mask"}}

    with torch.no_grad():
        out = model(**inputs)
        logits = out.logits[0, -1]  # last token logits

    probs = torch.softmax(logits, dim=-1)
    top_k = torch.topk(probs, k=10)

    print(f"Input: '{text}'")
    print(f"Input IDs: {inputs['input_ids'][0].tolist()}")
    print(f"\nLogit stats: min={logits.min():.2f}, max={logits.max():.2f}, "
          f"mean={logits.mean():.2f}, std={logits.std():.2f}")
    print(f"Entropy: {-(probs * probs.log()).sum():.2f} nats "
          f"(uniform would be {torch.tensor(float(probs.shape[0])).log():.2f})")

    print(f"\nTop-10 predictions:")
    for i, (prob, idx) in enumerate(zip(top_k.values, top_k.indices)):
        token_str = tok.decode([idx.item()])
        print(f"  {i+1}. '{token_str}' (id={idx.item()}) p={prob.item():.4f}")

    # Check if distribution is near-uniform (sign of broken model)
    max_prob = probs.max().item()
    if max_prob < 0.01:
        print("\n*** WARNING: Top probability < 1% — logits are near-uniform. ***")
        print("This suggests the model weights are not being used correctly.")
    else:
        print(
            f"\nTop probability is {max_prob:.4f} — model is making non-trivial predictions.")


def check_mc_loglikelihoods(hf_dir: str):
    """Simulate what lm-eval does: score multiple choice answers via log-likelihood."""
    from transformers import AutoModelForCausalLM, AutoTokenizer

    print("\n" + "=" * 60)
    print("3. MULTIPLE-CHOICE LOG-LIKELIHOOD CHECK")
    print("=" * 60)

    tok = AutoTokenizer.from_pretrained(hf_dir, trust_remote_code=True)
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    model = AutoModelForCausalLM.from_pretrained(
        hf_dir, trust_remote_code=True, dtype=torch.bfloat16,
    ).to(device)
    model.eval()

    # Simple MC question where we know the right answer
    question = "1 + 1 = "
    choices = ["2", "3", "5", "7"]
    correct = 0

    print(f"Question: '{question}'")
    print(f"Choices: {choices} (correct: {choices[correct]})")

    log_likelihoods = []
    for choice in choices:
        full_text = question + choice
        ids = tok.encode(full_text, add_special_tokens=False)
        q_ids = tok.encode(question, add_special_tokens=False)
        answer_start = len(q_ids)

        input_ids = torch.tensor([ids], device=device)
        with torch.no_grad():
            logits = model(input_ids=input_ids).logits[0]  # (seq_len, vocab)
        # Log-likelihood of the answer tokens
        ll = 0.0
        for i in range(answer_start, len(ids)):
            token_logprobs = torch.log_softmax(logits[i - 1], dim=-1)
            ll += token_logprobs[ids[i]].item()
        log_likelihoods.append(ll)
        print(f"  '{choice}': log_likelihood = {ll:.4f}")

    predicted = max(range(len(choices)), key=lambda i: log_likelihoods[i])
    print(
        f"\nModel picks: '{choices[predicted]}' (correct: '{choices[correct]}')")
    if predicted == correct:
        print("PASS")
    else:
        print("FAIL — model can't even do 1+1")


def check_weight_stats(hf_dir: str):
    """Quick check that weights look reasonable."""
    from transformers import AutoModelForCausalLM

    print("\n" + "=" * 60)
    print("4. WEIGHT STATISTICS")
    print("=" * 60)

    model = AutoModelForCausalLM.from_pretrained(
        hf_dir, trust_remote_code=True, dtype=torch.bfloat16,
    )

    for name, param in model.named_parameters():
        if any(k in name for k in ["wte", "lm_head", "resid_lambdas", "x0_lambdas",
                                   "h.0.attn.c_q", "h.0.attn.c_proj",
                                   "value_embeds.0"]):
            p = param.float()
            print(f"  {name:50s} shape={str(list(param.shape)):20s} "
                  f"mean={p.mean():.6f} std={p.std():.6f} "
                  f"min={p.min():.4f} max={p.max():.4f}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hf-model", type=str, required=True,
                        help="HF model name or path")
    parser.add_argument("--tokenizer-dir", type=str, default=None,
                        help="Path to original nanochat tokenizer dir (with tokenizer.pkl)")
    args = parser.parse_args()

    check_tokenizer(args.hf_model, args.tokenizer_dir)
    check_logits(args.hf_model)
    check_mc_loglikelihoods(args.hf_model)
    check_weight_stats(args.hf_model)

    print("\n" + "=" * 60)
    print("DIAGNOSIS COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
