"""
Engram: conditional n-gram memory module for NanoChat.
Ported from AutoArk/TinyEngram (Engram architecture, 2026).

Provides O(1) knowledge lookup via hashed n-gram embeddings.  Instead of
forcing transformer layers to reconstruct static patterns (named entities,
common phrases, factual associations) through expensive attention, Engram
retrieves pre-computed embeddings via hash lookup and injects them into the
hidden states with a learned gating mechanism.

Key design choices for NanoChat integration:
- No hyper-connections (NanoChat doesn't use them) — HC_MULT=1
- No CompressedTokenizer dependency — NanoChat uses tiktoken with a fixed
  vocab, so we hash raw token IDs directly (compression is optional and
  can be added later if collision rates are too high)
- Simplified gating: sigmoid gate conditioned on hidden state, no ShortConv
  (we can add it back if ablations show it helps at our scale)
- Bloom-filter-style multi-head hashing for collision reduction

Usage:
    See GPTConfig fields `use_engram`, `engram_table_size`, etc. in gpt.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F


@dataclass
class EngramConfig:
    """Configuration for the Engram module."""
    table_size: int = 131072             # entries per n-gram order per hash head
    # maximum n-gram order (2=bigrams, 3=trigrams)
    max_ngram: int = 3
    # hash heads per n-gram order (Bloom filter anti-collision)
    n_heads: int = 4
    embed_dim: int = 256                 # embedding dimension per hash head
    inject_layers: List[int] = field(default_factory=lambda: [
                                     1, 3])  # which layers get injection
    pad_id: int = 0                      # padding token ID for shifted sequences
    seed: int = 42                       # RNG seed for hash multipliers
    # force gates=1 during warmup (let embeddings train freely)
    warmup_steps: int = 200
    soft_clamp_steps: int = 500          # clamp gates >= 0.1 after warmup


# Module-level step counter (set by training loop)
_engram_step = 0


def set_engram_step(step: int):
    global _engram_step
    _engram_step = step


def get_engram_step() -> int:
    return _engram_step


def _find_next_prime(start: int, seen: set[int]) -> int:
    """Find smallest prime > start not in seen."""
    candidate = start + 1
    while True:
        if candidate > 1 and all(candidate % i != 0 for i in range(2, int(candidate**0.5) + 1)):
            if candidate not in seen:
                return candidate
        candidate += 1


class NgramHasher:
    """
    Computes n-gram hashes from token IDs for embedding table lookup.

    For each n-gram order (2, 3, ..., max_ngram) and each hash head, produces
    an integer index into the corresponding embedding table partition.
    Uses layer-specific random multipliers and prime moduli (Bloom filter
    strategy) to distribute collisions.
    """

    def __init__(self, config: EngramConfig, layer_ids: list[int]):
        self.max_ngram = config.max_ngram
        self.n_heads = config.n_heads
        self.pad_id = config.pad_id
        self.layer_ids = layer_ids

        # Per-layer random odd multipliers for hash mixing
        # Shape per layer: [max_ngram] — one multiplier per n-gram position
        self.layer_multipliers: dict[int, np.ndarray] = {}
        max_long = np.iinfo(np.int64).max
        # Keep multipliers well within int64 range to avoid overflow
        half_bound = max(1, int(max_long // (config.table_size * 4)))

        for layer_id in layer_ids:
            rng = np.random.default_rng(config.seed + 10007 * layer_id)
            r = rng.integers(0, half_bound, size=(
                config.max_ngram,), dtype=np.int64)
            self.layer_multipliers[layer_id] = r * 2 + 1  # ensure odd

        # Per-layer, per-ngram-order, per-head prime moduli
        # vocab_sizes[layer_id][ngram_idx][head_idx] = prime
        self.vocab_sizes: dict[int, list[list[int]]] = {}
        seen_primes: set[int] = set()
        for layer_id in layer_ids:
            per_layer = []
            # 0=bigram, 1=trigram, ...
            for ngram_idx in range(config.max_ngram - 1):
                per_ngram = []
                search_start = config.table_size - 1
                for _ in range(config.n_heads):
                    p = _find_next_prime(search_start, seen_primes)
                    seen_primes.add(p)
                    per_ngram.append(p)
                    search_start = p
                per_layer.append(per_ngram)
            self.vocab_sizes[layer_id] = per_layer

        # Total number of hash heads
        self.total_heads = (config.max_ngram - 1) * config.n_heads

    def all_table_sizes(self, layer_id: int) -> list[int]:
        """Flat list of prime table sizes for MultiHeadEmbedding construction."""
        return [p for ngram_primes in self.vocab_sizes[layer_id] for p in ngram_primes]

    def hash(self, input_ids: np.ndarray, layer_id: int) -> np.ndarray:
        """
        Compute n-gram hashes.

        Args:
            input_ids: [B, T] int64 token IDs
            layer_id: which layer (determines multipliers and prime moduli)

        Returns:
            [B, T, total_heads] int64 hash indices
        """
        x = np.asarray(input_ids, dtype=np.int64)
        B, T = x.shape
        mults = self.layer_multipliers[layer_id]

        # Build shifted copies: shift_k(k) = x shifted right by k positions
        shifts = [x]
        for k in range(1, self.max_ngram):
            shifted = np.pad(x, ((0, 0), (k, 0)),
                             mode='constant', constant_values=self.pad_id)[:, :T]
            shifts.append(shifted)

        all_hashes = []
        for n in range(2, self.max_ngram + 1):
            ngram_idx = n - 2
            # XOR-based hash: mix = tok[t]*m[0] XOR tok[t-1]*m[1] XOR ...
            mix = shifts[0] * mults[0]
            for k in range(1, n):
                mix = np.bitwise_xor(mix, shifts[k] * mults[k])
            # Each head uses a different prime modulus
            for head_idx in range(self.n_heads):
                mod = self.vocab_sizes[layer_id][ngram_idx][head_idx]
                all_hashes.append((mix % mod).astype(np.int64))

        return np.stack(all_hashes, axis=2)  # [B, T, total_heads]


class MultiHeadEmbedding(nn.Module):
    """
    Flattened embedding table for all hash heads.

    Each head has its own table size (a prime), but they share a single
    nn.Embedding with offsets added to indices.
    """

    def __init__(self, table_sizes: list[int], embed_dim: int):
        super().__init__()
        self.num_heads = len(table_sizes)
        self.embed_dim = embed_dim
        self._table_sizes = table_sizes  # saved for reinit
        offsets = [0]
        for s in table_sizes[:-1]:
            offsets.append(offsets[-1] + s)
        self.register_buffer(
            "offsets", torch.tensor(offsets, dtype=torch.long))
        total = sum(table_sizes)
        self.embedding = nn.Embedding(total, embed_dim)

    def reinit_buffers(self):
        """Re-create offset buffer on current device (needed after meta-device init + to_empty)."""
        offsets = [0]
        for s in self._table_sizes[:-1]:
            offsets.append(offsets[-1] + s)
        self.offsets = torch.tensor(offsets, dtype=torch.long, device=self.embedding.weight.device)

    def forward(self, hash_ids: torch.Tensor) -> torch.Tensor:
        """
        Args:
            hash_ids: [B, T, num_heads] int64 indices (per-head, before offset)
        Returns:
            [B, T, num_heads * embed_dim] flattened embeddings
        """
        shifted = hash_ids + self.offsets  # broadcast offsets [num_heads]
        out = self.embedding(shifted)       # [B, T, num_heads, embed_dim]
        B, T, H, D = out.shape
        return out.reshape(B, T, H * D)    # [B, T, engram_hidden_size]


class Engram(nn.Module):
    """
    Single-layer Engram injection module.

    Given input token IDs and the current hidden state, retrieves n-gram
    embeddings via hash lookup and gates them with a learned function of the
    hidden state.

    Integration pattern (in the forward loop):
        if engram is not None:
            x = x + engram(x, idx)
        x = x + attn(norm(x), ...)
        x = x + mlp(norm(x))
    """

    def __init__(self, layer_id: int, config: EngramConfig, hidden_size: int):
        super().__init__()
        self.layer_id = layer_id
        self.config = config
        self.hidden_size = hidden_size

        # Hasher is NOT an nn.Module — it's pure numpy, shared if needed
        self.hasher = NgramHasher(config, [layer_id])

        # Precompute torch hash constants (registered as buffers for device tracking)
        mults_np = self.hasher.layer_multipliers[layer_id]
        self.register_buffer('_hash_mults',
                             torch.from_numpy(mults_np).long(), persistent=False)
        mods = []
        for n in range(2, config.max_ngram + 1):
            ngram_idx = n - 2
            for head_idx in range(config.n_heads):
                mods.append(self.hasher.vocab_sizes[layer_id][ngram_idx][head_idx])
        self.register_buffer('_hash_mods',
                             torch.tensor(mods, dtype=torch.long), persistent=False)

        # Embedding table
        table_sizes = self.hasher.all_table_sizes(layer_id)
        per_head_dim = config.embed_dim // config.n_heads
        self.multi_head_emb = MultiHeadEmbedding(table_sizes, per_head_dim)

        # Total engram hidden size after flattening all heads
        engram_hidden = (config.max_ngram - 1) * config.embed_dim
        # Project retrieved embeddings to model hidden size
        self.value_proj = nn.Linear(engram_hidden, hidden_size, bias=False)
        # Key projection for gating (engram side)
        self.key_proj = nn.Linear(engram_hidden, hidden_size, bias=False)
        # Norms for gate computation
        self.key_norm = nn.RMSNorm(hidden_size)
        self.query_norm = nn.RMSNorm(hidden_size)

    def reinit_hash_buffers(self):
        """Re-create hash constant buffers on current device (needed after meta-device init + to_empty)."""
        device = self.value_proj.weight.device
        mults_np = self.hasher.layer_multipliers[self.layer_id]
        self._hash_mults = torch.from_numpy(mults_np).long().to(device)
        mods = []
        for n in range(2, self.config.max_ngram + 1):
            ngram_idx = n - 2
            for head_idx in range(self.config.n_heads):
                mods.append(self.hasher.vocab_sizes[self.layer_id][ngram_idx][head_idx])
        self._hash_mods = torch.tensor(mods, dtype=torch.long, device=device)
        self.multi_head_emb.reinit_buffers()

    def _hash_torch(self, input_ids: torch.Tensor) -> torch.Tensor:
        """Pure-torch n-gram hashing. Stays entirely on device (no CPU round-trip).
        Args: input_ids [B, T] long tensor
        Returns: [B, T, total_heads] long tensor of hash indices
        """
        B, T = input_ids.shape
        mults = self._hash_mults

        # Build shifted copies
        shifts = [input_ids]
        for k in range(1, self.config.max_ngram):
            shifted = F.pad(input_ids, (k, 0), value=self.config.pad_id)[:, :T]
            shifts.append(shifted)

        all_hashes = []
        mod_idx = 0
        for n in range(2, self.config.max_ngram + 1):
            # XOR-based hash mixing
            mix = shifts[0] * mults[0]
            for k in range(1, n):
                mix = torch.bitwise_xor(mix, shifts[k] * mults[k])
            for _ in range(self.config.n_heads):
                # torch.remainder ensures non-negative results (Python-style modulo)
                # unlike % which uses C-style truncated division and can be negative
                all_hashes.append(torch.remainder(mix, self._hash_mods[mod_idx]))
                mod_idx += 1

        return torch.stack(all_hashes, dim=2)  # [B, T, total_heads]

    def _compute_gate(self, hidden: torch.Tensor, key: torch.Tensor) -> torch.Tensor:
        """
        Scaled dot-product gate: sigmoid of similarity between hidden state
        and retrieved n-gram key.

        The sqrt-abs activation (from TinyEngram) preserves sign while
        dampening magnitude — helps with training stability.
        """
        nk = self.key_norm(key)
        nq = self.query_norm(hidden)
        sim = (nk * nq).sum(dim=-1) / math.sqrt(self.hidden_size)
        # sqrt-abs activation preserves sign, scales magnitude
        sim = sim.abs().clamp_min(1e-6).sqrt() * sim.sign()
        return sim.sigmoid().unsqueeze(-1)  # [B, T, 1]

    def forward(self, hidden_states: torch.Tensor, input_ids: torch.Tensor) -> torch.Tensor:
        """
        Args:
            hidden_states: [B, T, D] current hidden state
            input_ids: [B, T] raw token IDs

        Returns:
            [B, T, D] engram contribution (to be added to hidden state)
        """
        # Hash entirely on device (no CPU round-trip)
        hash_ids_t = self._hash_torch(input_ids)

        # Retrieve and flatten embeddings
        embeddings = self.multi_head_emb(hash_ids_t)  # [B, T, engram_hidden]

        # Gate: controls how much n-gram memory flows into hidden state
        key = self.key_proj(embeddings)
        gate = self._compute_gate(hidden_states, key)

        # Warmup annealing
        step = get_engram_step()
        if step < self.config.warmup_steps:
            gate = torch.ones_like(gate)
        elif step < self.config.warmup_steps + self.config.soft_clamp_steps:
            gate = gate.clamp_min(0.1)

        # Value: project to hidden size and apply gate
        value = self.value_proj(embeddings)  # [B, T, D]
        self.last_gate = gate.detach()  # [B, T, 1] for diagnostics
        return gate * value

    def num_table_params(self) -> int:
        """Return the number of parameters in the embedding tables only."""
        return self.multi_head_emb.embedding.weight.numel()
