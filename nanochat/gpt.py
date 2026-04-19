"""
GPT model (rewrite, a lot simpler)
Notable features:
- rotary embeddings (and no positional embeddings)
- QK norm
- untied weights for token embedding and lm_head
- relu^2 activation in MLP
- norm after token embedding
- no learnable params in rmsnorm
- no bias in linear layers
- Group-Query Attention (GQA) support for more efficient inference
- Flash Attention 3 integration
"""

from functools import partial
from dataclasses import dataclass, field
from typing import List

import torch
import torch.nn as nn
import torch.nn.functional as F

from nanochat.common import get_dist_info, print0, COMPUTE_DTYPE
from nanochat.optim import MuonAdamW, DistMuonAdamW

# Our custom Flash Attention module that automatically uses FA3 on Hopper+ and SDPA fallback elsewhere
from nanochat.flash_attention import flash_attn


@dataclass
class GPTConfig:
    sequence_len: int = 2048
    vocab_size: int = 32768
    n_layer: int = 12
    n_head: int = 6  # number of query heads
    n_kv_head: int = 6  # number of key/value heads (GQA)
    n_embd: int = 768
    # Sliding window attention pattern string, tiled across layers. Final layer always L.
    # Characters: L=long (full context), S=short (quarter context)
    # Examples: "L"=all full context, "SL"=alternating, "SSL"=two short then one long
    window_pattern: str = "SSSL"
    # AttnRes (Chen et al., 2026): depth-wise softmax attention replaces fixed residual accumulation
    use_attn_res: bool = False
    # number of blocks (block_size = n_layer * 2 // num_blocks)
    attn_res_num_blocks: int = 4
    # Engram (TinyEngram, 2026): conditional n-gram memory with O(1) hash-based lookup
    use_engram: bool = False
    engram_table_size: int = 131072      # entries per n-gram order per hash head
    engram_ngram_max: int = 3            # maximum n-gram order
    # hash heads per n-gram order (Bloom filter)
    engram_n_heads: int = 4
    # total embedding dim (split across heads)
    engram_embed_dim: int = 256
    engram_inject_layers: List[int] = field(
        default_factory=list)  # empty = auto-select early layers
    # fraction of total params allocated to Engram tables
    engram_param_ratio: float = 0.15


# ---------------------------------------------------------------------------
# Config presets for ablation experiments (iso-parameter budget)
# ---------------------------------------------------------------------------
def make_ablation_configs(depth: int, model_dim: int, n_head: int, n_kv_head: int,
                          vocab_size: int = 32768, sequence_len: int = 2048,
                          window_pattern: str = "SSSL",
                          engram_table_size: int = 131072, engram_n_heads: int = 4,
                          engram_embed_dim: int = 256, attn_res_num_blocks: int = 4):
    """
    Return a dict of 4 GPTConfig presets for ablation experiments:
      - baseline:     standard NanoChat (no AttnRes, no Engram)
      - attn_res:     AttnRes only
      - engram:       Engram only
      - attn_res_engram: both AttnRes + Engram

    AttnRes adds negligible params (~2 * d per layer + RMSNorm weights),
    so no compensation needed. Engram uses ~15% of total params for hash
    tables by default; at iso-parameter budget you'd reduce depth or dim
    separately (these presets keep architecture fixed for clean ablation).
    """
    base = dict(sequence_len=sequence_len, vocab_size=vocab_size,
                n_layer=depth, n_head=n_head, n_kv_head=n_kv_head,
                n_embd=model_dim, window_pattern=window_pattern)
    engram_kw = dict(engram_table_size=engram_table_size, engram_n_heads=engram_n_heads,
                     engram_embed_dim=engram_embed_dim)
    # inject_layers left empty → auto-selects non-VE layers in GPT.__init__
    return {
        'baseline': GPTConfig(**base),
        'attn_res': GPTConfig(**base, use_attn_res=True, attn_res_num_blocks=attn_res_num_blocks),
        'engram': GPTConfig(**base, use_engram=True, **engram_kw),
        'attn_res_engram': GPTConfig(**base, use_attn_res=True, attn_res_num_blocks=attn_res_num_blocks,
                                     use_engram=True, **engram_kw),
    }


def norm(x):
    # note that this will run in bf16, seems ok
    return F.rms_norm(x, (x.size(-1),))


class Linear(nn.Linear):
    """nn.Linear that casts weights to match input dtype in forward.
    Replaces autocast: master weights stay fp32 for optimizer precision,
    but matmuls run in the activation dtype (typically bf16 from embeddings)."""

    def forward(self, x):
        return F.linear(x, self.weight.to(dtype=x.dtype))


def has_ve(layer_idx, n_layer):
    """Returns True if GPT layer should have Value Embedding (alternating, last layer always included)."""
    return layer_idx % 2 == (n_layer - 1) % 2


def apply_rotary_emb(x, cos, sin):
    assert x.ndim == 4  # multihead attention
    d = x.shape[3] // 2
    x1, x2 = x[..., :d], x[..., d:]  # split up last dim into two halves
    y1 = x1 * cos + x2 * sin  # rotate pairs of dims
    y2 = x1 * (-sin) + x2 * cos
    return torch.cat([y1, y2], 3)


class CausalSelfAttention(nn.Module):
    def __init__(self, config, layer_idx):
        super().__init__()
        self.layer_idx = layer_idx
        self.n_head = config.n_head
        self.n_kv_head = config.n_kv_head
        self.n_embd = config.n_embd
        self.head_dim = self.n_embd // self.n_head
        assert self.n_embd % self.n_head == 0
        assert self.n_kv_head <= self.n_head and self.n_head % self.n_kv_head == 0
        self.c_q = Linear(self.n_embd, self.n_head * self.head_dim, bias=False)
        self.c_k = Linear(self.n_embd, self.n_kv_head *
                          self.head_dim, bias=False)
        self.c_v = Linear(self.n_embd, self.n_kv_head *
                          self.head_dim, bias=False)
        self.c_proj = Linear(self.n_embd, self.n_embd, bias=False)
        self.ve_gate_channels = 12
        self.ve_gate = Linear(self.ve_gate_channels, self.n_kv_head, bias=False) if has_ve(
            layer_idx, config.n_layer) else None

    def forward(self, x, ve, cos_sin, window_size, kv_cache):
        B, T, C = x.size()

        # Project the input to get queries, keys, and values
        # Shape: (B, T, H, D) - FA3's native layout, no transpose needed!
        q = self.c_q(x).view(B, T, self.n_head, self.head_dim)
        k = self.c_k(x).view(B, T, self.n_kv_head, self.head_dim)
        v = self.c_v(x).view(B, T, self.n_kv_head, self.head_dim)

        # Value residual (ResFormer): mix in value embedding with input-dependent gate per head
        if ve is not None:
            ve = ve.view(B, T, self.n_kv_head, self.head_dim)
            # (B, T, n_kv_head), range (0, 3)
            gate = 3 * \
                torch.sigmoid(self.ve_gate(x[..., :self.ve_gate_channels]))
            v = v + gate.unsqueeze(-1) * ve

        # Apply Rotary Embeddings to queries and keys to get relative positional encoding
        cos, sin = cos_sin
        q, k = apply_rotary_emb(q, cos, sin), apply_rotary_emb(k, cos, sin)
        q, k = norm(q), norm(k)  # QK norm
        # sharper attention (split scale between Q and K), TODO think through better
        q = q * 1.2
        k = k * 1.2

        # Flash Attention (FA3 on Hopper+, PyTorch SDPA fallback elsewhere)
        # window_size is (left, right) tuple: (N, 0) for causal, (-1, 0) for full context
        if kv_cache is None:
            # Training: causal attention with optional sliding window
            y = flash_attn.flash_attn_func(
                q, k, v, causal=True, window_size=window_size)
        else:
            # Inference: use flash_attn_with_kvcache which handles cache management
            k_cache, v_cache = kv_cache.get_layer_cache(self.layer_idx)
            y = flash_attn.flash_attn_with_kvcache(
                q, k_cache, v_cache,
                k=k, v=v,
                cache_seqlens=kv_cache.cache_seqlens,
                causal=True,
                window_size=window_size,
            )
            # Advance position after last layer processes
            if self.layer_idx == kv_cache.n_layers - 1:
                kv_cache.advance(T)

        # Re-assemble the heads and project back to residual stream
        y = y.contiguous().view(B, T, -1)
        y = self.c_proj(y)
        return y


class MLP(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.c_fc = Linear(config.n_embd, 4 * config.n_embd, bias=False)
        self.c_proj = Linear(4 * config.n_embd, config.n_embd, bias=False)

    def forward(self, x):
        x = self.c_fc(x)
        x = F.relu(x).square()
        x = self.c_proj(x)
        return x


class Block(nn.Module):
    def __init__(self, config, layer_idx):
        super().__init__()
        self.attn = CausalSelfAttention(config, layer_idx)
        self.mlp = MLP(config)

    def forward(self, x, ve, cos_sin, window_size, kv_cache):
        x = x + self.attn(norm(x), ve, cos_sin, window_size, kv_cache)
        x = x + self.mlp(norm(x))
        return x

    def forward_sublayers(self, x, ve, cos_sin, window_size, kv_cache):
        """Return individual sublayer outputs for AttnRes accumulation."""
        attn_out = self.attn(norm(x), ve, cos_sin, window_size, kv_cache)
        mlp_out = self.mlp(norm(x + attn_out))
        return attn_out, mlp_out


class GPT(nn.Module):
    def __init__(self, config, pad_vocab_size_to=64):
        """
        NOTE a major footgun: this __init__ function runs in meta device context (!!)
        Therefore, any calculations inside here are shapes and dtypes only, no actual data.
        => We actually initialize all data (parameters, buffers, etc.) in init_weights() instead.
        """
        super().__init__()
        self.config = config
        # Compute per-layer window sizes for sliding window attention
        # window_size is (left, right) tuple: (-1, 0) for full context, (N, 0) for sliding window
        self.window_sizes = self._compute_window_sizes(config)
        # Pad vocab for efficiency (DDP, tensor cores). This is just an optimization - outputs are cropped in forward().
        # https://huggingface.co/docs/transformers/main_classes/model#transformers.PreTrainedModel.resize_token_embeddings
        padded_vocab_size = (
            (config.vocab_size + pad_vocab_size_to - 1) // pad_vocab_size_to) * pad_vocab_size_to
        if padded_vocab_size != config.vocab_size:
            print0(
                f"Padding vocab_size from {config.vocab_size} to {padded_vocab_size} for efficiency")
        self.transformer = nn.ModuleDict({
            "wte": nn.Embedding(padded_vocab_size, config.n_embd),
            "h": nn.ModuleList([Block(config, layer_idx) for layer_idx in range(config.n_layer)]),
        })
        self.lm_head = Linear(config.n_embd, padded_vocab_size, bias=False)
        # Per-layer learnable scalars (inspired by modded-nanogpt)
        # resid_lambdas: scales the residual stream at each layer (init 1.0 = neutral)
        # x0_lambdas: blends initial embedding back in at each layer (init 0.0 = disabled)
        # Separate parameters so they can have different optimizer treatment
        # fake init, real init in init_weights()
        self.resid_lambdas = nn.Parameter(torch.ones(config.n_layer))
        # fake init, real init in init_weights()
        self.x0_lambdas = nn.Parameter(torch.zeros(config.n_layer))
        # Smear: mix previous token's embedding into current token (cheap bigram-like info)
        self.smear_gate = Linear(24, 1, bias=False)
        self.smear_lambda = nn.Parameter(torch.zeros(1))
        # Backout: subtract cached mid-layer residual before final norm to remove low-level features
        self.backout_lambda = nn.Parameter(0.2 * torch.ones(1))
        # Value embeddings (ResFormer-style): alternating layers, last layer always included
        head_dim = config.n_embd // config.n_head
        kv_dim = config.n_kv_head * head_dim
        self.value_embeds = nn.ModuleDict({str(i): nn.Embedding(
            padded_vocab_size, kv_dim) for i in range(config.n_layer) if has_ve(i, config.n_layer)})
        # To support meta device initialization, we init the rotary embeddings here, but it's just "fake" meta tensors only.
        # As for rotary_seq_len, these rotary embeddings are pretty small/cheap in memory,
        # so let's just over-compute them by 10X, but assert fail if we ever reach that amount.
        # In the future we can dynamically grow the cache, for now it's fine.
        # 10X over-compute should be enough, TODO make nicer?
        self.rotary_seq_len = config.sequence_len * 10
        head_dim = config.n_embd // config.n_head
        cos, sin = self._precompute_rotary_embeddings(
            self.rotary_seq_len, head_dim)
        # persistent=False means it's not saved to the checkpoint
        self.register_buffer("cos", cos, persistent=False)
        self.register_buffer("sin", sin, persistent=False)

        # AttnRes (Chen et al., 2026): per-layer pseudo-queries + output aggregation
        if config.use_attn_res:
            from nanochat.attn_res import AttnResLayer, AttnResNorm
            self.attn_res_layers = nn.ModuleList(
                [AttnResLayer(config.n_embd) for _ in range(config.n_layer)])
            self.attn_res_out_query = nn.Parameter(torch.zeros(config.n_embd))
            self.attn_res_out_norm = AttnResNorm(config.n_embd)

        # Engram (TinyEngram, 2026): conditional n-gram memory at selected layers
        if config.use_engram:
            from nanochat.engram import Engram, EngramConfig
            inject_layers = config.engram_inject_layers
            if not inject_layers:
                # Auto-select early non-VE layers (avoid stacking with value embeddings)
                non_ve = [i for i in range(
                    config.n_layer) if not has_ve(i, config.n_layer)]
                inject_layers = non_ve[:2] if len(
                    non_ve) >= 2 else non_ve[:1] or [0]
            engram_cfg = EngramConfig(
                table_size=config.engram_table_size,
                max_ngram=config.engram_ngram_max,
                n_heads=config.engram_n_heads,
                embed_dim=config.engram_embed_dim,
                inject_layers=inject_layers,
            )
            self.engram_layers = nn.ModuleDict({
                str(i): Engram(i, engram_cfg, config.n_embd) for i in inject_layers
            })

    @torch.no_grad()
    def init_weights(self):
        """
        Initialize the full model in this one function for maximum clarity.

        wte (embedding):     normal, std=1.0
        lm_head:             normal, std=0.001
        for each block:
            attn.c_q:        uniform, std=1/sqrt(n_embd)
            attn.c_k:        uniform, std=1/sqrt(n_embd)
            attn.c_v:        uniform, std=1/sqrt(n_embd)
            attn.c_proj:     zeros
            mlp.c_fc:        uniform, std=1/sqrt(n_embd)
            mlp.c_proj:      zeros
        """

        # Embedding and unembedding
        torch.nn.init.normal_(self.transformer.wte.weight, mean=0.0, std=0.8)
        torch.nn.init.normal_(self.lm_head.weight, mean=0.0, std=0.001)

        # Transformer blocks: uniform init with bound = sqrt(3) * std (same standard deviation as normal)
        n_embd = self.config.n_embd
        # sqrt(3) multiplier makes sure Uniform achieves the same std as Normal
        s = 3**0.5 * n_embd**-0.5
        for block in self.transformer.h:
            # weights use Uniform to avoid outliers
            torch.nn.init.uniform_(block.attn.c_q.weight, -s, s)
            torch.nn.init.uniform_(block.attn.c_k.weight, -s, s)
            torch.nn.init.uniform_(block.attn.c_v.weight, -s, s)
            # projections are zero
            torch.nn.init.zeros_(block.attn.c_proj.weight)
            # 0.4x init scale for c_fc
            torch.nn.init.uniform_(block.mlp.c_fc.weight, -s * 0.4, s * 0.4)
            torch.nn.init.zeros_(block.mlp.c_proj.weight)

        # Per-layer scalars
        # Per-layer resid init: stronger residual at early layers, weaker at deep layers
        n_layer = self.config.n_layer
        for i in range(n_layer):
            self.resid_lambdas.data[i] = 1.15 - \
                (0.10 * i / max(n_layer - 1, 1))
        # Decaying x0 init: earlier layers get more input embedding blending
        for i in range(n_layer):
            self.x0_lambdas.data[i] = 0.20 - (0.15 * i / max(n_layer - 1, 1))

        # Smear/backout scalars and smear gate must be explicitly initialized
        # (upstream fix: karpathy/nanochat#686 - ensures reproducibility from init_weights)
        torch.nn.init.zeros_(self.smear_lambda)
        torch.nn.init.constant_(self.backout_lambda, 0.2)
        torch.nn.init.uniform_(self.smear_gate.weight, 0.0, 0.02)

        # Value embeddings (init like c_v: uniform with same std)
        for ve in self.value_embeds.values():
            torch.nn.init.uniform_(ve.weight, -s, s)

        # Gate weights init with small positive values so gates start slightly above neutral
        for block in self.transformer.h:
            if block.attn.ve_gate is not None:
                torch.nn.init.uniform_(block.attn.ve_gate.weight, 0.0, 0.02)

        # Rotary embeddings
        head_dim = self.config.n_embd // self.config.n_head
        cos, sin = self._precompute_rotary_embeddings(
            self.rotary_seq_len, head_dim)
        self.cos, self.sin = cos, sin

        # Cast embeddings to COMPUTE_DTYPE: optimizer can tolerate reduced-precision
        # embeddings and it saves memory. Exception: fp16 requires fp32 embeddings
        # because GradScaler cannot unscale fp16 gradients.
        if COMPUTE_DTYPE != torch.float16:
            self.transformer.wte.to(dtype=COMPUTE_DTYPE)
            for ve in self.value_embeds.values():
                ve.to(dtype=COMPUTE_DTYPE)

        # AttnRes init: pseudo-queries start at zero (uniform attention across blocks)
        if self.config.use_attn_res:
            for layer in self.attn_res_layers:
                torch.nn.init.zeros_(layer.attn_res_query)
                torch.nn.init.zeros_(layer.mlp_res_query)
            torch.nn.init.zeros_(self.attn_res_out_query)

        # Engram init: embedding tables get small normal init, projections uniform
        if self.config.use_engram:
            for eng in self.engram_layers.values():
                eng.reinit_hash_buffers()  # re-create hash constants after meta-device init
                torch.nn.init.normal_(
                    eng.multi_head_emb.embedding.weight, mean=0.0, std=0.02)
                torch.nn.init.uniform_(eng.value_proj.weight, -s, s)
                torch.nn.init.uniform_(eng.key_proj.weight, -s, s)

    def _precompute_rotary_embeddings(self, seq_len, head_dim, base=100000, device=None):
        # TODO: bump base theta more? e.g. 100K is more common more recently
        # autodetect the device from model embeddings
        if device is None:
            device = self.transformer.wte.weight.device
        # stride the channels
        channel_range = torch.arange(
            0, head_dim, 2, dtype=torch.float32, device=device)
        inv_freq = 1.0 / (base ** (channel_range / head_dim))
        # stride the time steps
        t = torch.arange(seq_len, dtype=torch.float32, device=device)
        # calculate the rotation frequencies at each (time, channel) pair
        freqs = torch.outer(t, inv_freq)
        cos, sin = freqs.cos(), freqs.sin()
        cos, sin = cos.to(COMPUTE_DTYPE), sin.to(COMPUTE_DTYPE)
        # add batch and head dims for later broadcasting
        cos, sin = cos[None, :, None, :], sin[None, :, None, :]
        return cos, sin

    def _compute_window_sizes(self, config):
        """
        Compute per-layer window sizes for sliding window attention.

        Returns list of (left, right) tuples for FA3's window_size parameter:
        - left: how many tokens before current position to attend to (-1 = unlimited)
        - right: how many tokens after current position to attend to (0 for causal)

        Pattern string is tiled across layers. Final layer always gets L (full context).
        Characters: L=long (full context), S=short (quarter context)
        """
        pattern = config.window_pattern.upper()
        assert all(
            c in "SL" for c in pattern), f"Invalid window_pattern: {pattern}. Use only S and L."
        # Map characters to window sizes
        long_window = config.sequence_len
        # ceil to FA3 tile size (2048 -> 768)
        short_window = -(-long_window // 4 // 128) * 128
        char_to_window = {
            "L": (long_window, 0),
            "S": (short_window, 0),
        }
        # Tile pattern across layers
        window_sizes = []
        for layer_idx in range(config.n_layer):
            char = pattern[layer_idx % len(pattern)]
            window_sizes.append(char_to_window[char])
        # Final layer always gets full context
        window_sizes[-1] = (long_window, 0)
        return window_sizes

    def get_device(self):
        return self.transformer.wte.weight.device

    def estimate_flops(self):
        """
        Return the estimated FLOPs per token for the model (forward + backward).
        Each matmul weight parameter contributes 2 FLOPs (multiply *, accumulate +) in forward, and 2X that in backward => 2+4=6.
        Cleanest explanation of this: https://medium.com/@dzmitrybahdanau/the-flops-calculus-of-language-model-training-3b19c1f025e4
        On top of that, 12 * h * q * effective_seq_len accounts for key @ query matmul flops inside attention.
        With sliding windows, effective_seq_len varies per layer (capped by window size).
        Ref: https://arxiv.org/abs/2204.02311 (PaLM paper).
        This is ~1% off from the exact formulas of Chinchilla paper, the difference is:
        - Chinchilla counts the embedding layer as flops (? weird, it's just a lookup => we ignore)
        - Chinchilla counts exp/sum/divide in attention softmax as flops (a little sus and very tiny => we ignore)
        """
        nparams = sum(p.numel() for p in self.parameters())
        # Exclude non-matmul params: embeddings and per-layer scalars
        value_embeds_numel = sum(ve.weight.numel()
                                 for ve in self.value_embeds.values())
        nparams_exclude = (self.transformer.wte.weight.numel() + value_embeds_numel +
                           self.resid_lambdas.numel() + self.x0_lambdas.numel() +
                           self.smear_gate.weight.numel() + self.smear_lambda.numel() + self.backout_lambda.numel())
        # Exclude AttnRes params (norms + pseudo-queries are ~scalars, not matmuls)
        if self.config.use_attn_res:
            nparams_exclude += sum(p.numel()
                                   for p in self.attn_res_layers.parameters())
            nparams_exclude += self.attn_res_out_query.numel()
            nparams_exclude += sum(p.numel()
                                   for p in self.attn_res_out_norm.parameters())
        # Exclude Engram embedding tables (lookups, not matmuls) but count projections
        if self.config.use_engram:
            for eng in self.engram_layers.values():
                nparams_exclude += eng.multi_head_emb.embedding.weight.numel()
        h, q, t = self.config.n_head, self.config.n_embd // self.config.n_head, self.config.sequence_len
        # Sum attention FLOPs per layer, accounting for sliding window
        attn_flops = 0
        for window_size in self.window_sizes:
            window = window_size[0]  # (left, right) tuple, we use left
            effective_seq = t if window < 0 else min(window, t)
            attn_flops += 12 * h * q * effective_seq
        num_flops_per_token = 6 * (nparams - nparams_exclude) + attn_flops
        return num_flops_per_token

    def num_scaling_params(self):
        """
        Return detailed parameter counts for scaling law analysis.
        Different papers use different conventions:
        - Kaplan et al. excluded embedding parameters
        - Chinchilla included all parameters
        Ref: https://arxiv.org/abs/2203.15556 (Chinchilla paper)
        Ref: https://arxiv.org/abs/2001.08361 (Kaplan et al. original scaling laws paper)

        Returns a dict with counts for each parameter group, so downstream analysis
        can experiment with which combination gives the cleanest scaling laws.
        """
        # Count each group separately (mirrors the grouping in setup_optimizers)
        wte = sum(p.numel() for p in self.transformer.wte.parameters())
        value_embeds = sum(p.numel() for p in self.value_embeds.parameters())
        lm_head = sum(p.numel() for p in self.lm_head.parameters())
        transformer_matrices = sum(p.numel()
                                   for p in self.transformer.h.parameters())
        scalars = self.resid_lambdas.numel() + self.x0_lambdas.numel() + self.smear_gate.weight.numel() + \
            self.smear_lambda.numel() + self.backout_lambda.numel()
        # AttnRes params (pseudo-queries + norms)
        attn_res_params = 0
        if self.config.use_attn_res:
            attn_res_params = sum(p.numel()
                                  for p in self.attn_res_layers.parameters())
            attn_res_params += self.attn_res_out_query.numel()
            attn_res_params += sum(p.numel()
                                   for p in self.attn_res_out_norm.parameters())
        # Engram params (embedding tables + projections + norms)
        engram_table_params = 0
        engram_proj_params = 0
        if self.config.use_engram:
            for eng in self.engram_layers.values():
                engram_table_params += eng.num_table_params()
            engram_proj_params = sum(
                p.numel() for p in self.engram_layers.parameters()) - engram_table_params
        total = wte + value_embeds + lm_head + transformer_matrices + \
            scalars + attn_res_params + engram_table_params + engram_proj_params
        assert total == sum(p.numel() for p in self.parameters()), \
            f"Parameter count mismatch: computed {total} vs actual {sum(p.numel() for p in self.parameters())}"
        result = {
            'wte': wte,
            'value_embeds': value_embeds,
            'lm_head': lm_head,
            'transformer_matrices': transformer_matrices,
            'scalars': scalars,
            'total': total,
        }
        if self.config.use_attn_res:
            result['attn_res'] = attn_res_params
        if self.config.use_engram:
            result['engram_tables'] = engram_table_params
            result['engram_projections'] = engram_proj_params
        return result

    def setup_optimizer(self, unembedding_lr=0.004, embedding_lr=0.2, matrix_lr=0.02, weight_decay=0.0, scalar_lr=0.5):
        model_dim = self.config.n_embd
        ddp, rank, local_rank, world_size = get_dist_info()

        # Separate out all parameters into groups
        matrix_params = list(self.transformer.h.parameters())
        value_embeds_params = list(self.value_embeds.parameters())
        embedding_params = list(self.transformer.wte.parameters())
        lm_head_params = list(self.lm_head.parameters())
        resid_params = [self.resid_lambdas]
        x0_params = [self.x0_lambdas]
        smear_params = [self.smear_gate.weight,
                        self.smear_lambda, self.backout_lambda]
        # AttnRes params: pseudo-queries + norms (small, scalar-like)
        attn_res_all = []
        if self.config.use_attn_res:
            attn_res_all = list(self.attn_res_layers.parameters(
            )) + [self.attn_res_out_query] + list(self.attn_res_out_norm.parameters())
        # Engram params: split into embedding tables (lookup) vs projection matrices
        engram_table_list = []
        engram_proj_list = []
        if self.config.use_engram:
            engram_table_ids = set()
            for eng in self.engram_layers.values():
                engram_table_ids.add(id(eng.multi_head_emb.embedding.weight))
                engram_table_list.append(eng.multi_head_emb.embedding.weight)
            for p in self.engram_layers.parameters():
                if id(p) not in engram_table_ids:
                    engram_proj_list.append(p)
        all_param_lists = [matrix_params, embedding_params, lm_head_params, value_embeds_params,
                           resid_params, x0_params, smear_params, attn_res_all, engram_table_list, engram_proj_list]
        assert len(list(self.parameters())) == sum(len(g) for g in all_param_lists), \
            f"Parameter group mismatch: {len(list(self.parameters()))} vs {sum(len(g) for g in all_param_lists)}"

        # Scale the LR for the AdamW parameters by ∝1/√dmodel (tuned for 768 dim model)
        dmodel_lr_scale = (model_dim / 768) ** -0.5
        print0(
            f"Scaling the LR for the AdamW parameters ∝1/√({model_dim}/768) = {dmodel_lr_scale:.6f}")

        # Build param_groups with all required fields explicit
        param_groups = [
            # AdamW groups (embeddings, lm_head, scalars)
            dict(kind='adamw', params=lm_head_params, lr=unembedding_lr *
                 dmodel_lr_scale, betas=(0.8, 0.96), eps=1e-10, weight_decay=0.01),
            dict(kind='adamw', params=embedding_params, lr=embedding_lr *
                 dmodel_lr_scale, betas=(0.8, 0.995), eps=1e-10, weight_decay=0.001),
            dict(kind='adamw', params=value_embeds_params, lr=embedding_lr *
                 dmodel_lr_scale * 0.5, betas=(0.8, 0.995), eps=1e-10, weight_decay=0.01),
            dict(kind='adamw', params=resid_params, lr=scalar_lr *
                 0.01, betas=(0.8, 0.95), eps=1e-10, weight_decay=0.05),
            dict(kind='adamw', params=x0_params, lr=scalar_lr, betas=(
                0.96, 0.95), eps=1e-10, weight_decay=0.0),  # higher beta1 for x0
            dict(kind='adamw', params=smear_params, lr=0.2,
                 betas=(0.8, 0.95), eps=1e-10, weight_decay=0.0),
        ]
        # AttnRes params use small scalar-like LR (similar to resid_lambdas)
        if attn_res_all:
            param_groups.append(dict(kind='adamw', params=attn_res_all,
                                lr=scalar_lr * 0.01, betas=(0.8, 0.95), eps=1e-10, weight_decay=0.0))
        # Engram embedding tables: AdamW with embedding-like LR
        if engram_table_list:
            param_groups.append(dict(kind='adamw', params=engram_table_list, lr=embedding_lr *
                                dmodel_lr_scale * 0.25, betas=(0.8, 0.995), eps=1e-10, weight_decay=0.01))
        # Engram projection/gating params: AdamW with moderate LR
        if engram_proj_list:
            param_groups.append(dict(kind='adamw', params=engram_proj_list, lr=unembedding_lr *
                                dmodel_lr_scale, betas=(0.8, 0.96), eps=1e-10, weight_decay=0.01))
        # Muon groups (matrix params, grouped by shape for stacking)
        for shape in sorted({p.shape for p in matrix_params}):
            group_params = [p for p in matrix_params if p.shape == shape]
            param_groups.append(dict(
                kind='muon', params=group_params, lr=matrix_lr,
                momentum=0.95, ns_steps=5, beta2=0.9, weight_decay=weight_decay,
            ))

        Factory = DistMuonAdamW if ddp else MuonAdamW
        optimizer = Factory(param_groups)
        for group in optimizer.param_groups:
            group["initial_lr"] = group["lr"]
        return optimizer

    def forward(self, idx, targets=None, kv_cache=None, loss_reduction='mean'):
        B, T = idx.size()

        # Grab the rotary embeddings for the current sequence length (they are of shape (1, seq_len, 1, head_dim/2))
        assert T <= self.cos.size(
            1), f"Sequence length grew beyond the rotary embeddings cache: {T} > {self.cos.size(1)}"
        assert idx.device == self.cos.device, f"Rotary embeddings and idx are on different devices: {idx.device} != {self.cos.device}"
        assert self.cos.dtype == COMPUTE_DTYPE, f"Rotary embeddings must be in {COMPUTE_DTYPE}, got {self.cos.dtype}"
        # if kv cache exists, we need to offset the rotary embeddings to the current position in the cache
        T0 = 0 if kv_cache is None else kv_cache.get_pos()
        # truncate cache to current sequence length
        cos_sin = self.cos[:, T0:T0+T], self.sin[:, T0:T0+T]

        # Embed the tokens
        x = self.transformer.wte(idx)  # embed current token
        # ensure activations are in compute dtype (no-op usually, but active for fp16 code path)
        x = x.to(COMPUTE_DTYPE)
        x = norm(x)

        # Smear: mix previous token's embedding into current position (cheap bigram info)
        if kv_cache is None:
            # Training / naive generate: full sequence available, use fast slice
            assert T > 1, "Training forward pass should have T > 1"
            gate = self.smear_lambda.to(
                x.dtype) * torch.sigmoid(self.smear_gate(x[:, 1:, :24]))
            x = torch.cat([x[:, :1], x[:, 1:] + gate * x[:, :-1]], dim=1)
        else:
            # KV cache inference: read prev embedding from cache, store current for next step
            x_pre_smear = kv_cache.prev_embedding
            kv_cache.prev_embedding = x[:, -1:, :]
            if T > 1:
                # Prefill: apply smear to positions 1+, same as training
                gate = self.smear_lambda.to(
                    x.dtype) * torch.sigmoid(self.smear_gate(x[:, 1:, :24]))
                x = torch.cat([x[:, :1], x[:, 1:] + gate * x[:, :-1]], dim=1)
            elif x_pre_smear is not None:
                # Decode: single token, use cached prev embedding
                gate = self.smear_lambda.to(
                    x.dtype) * torch.sigmoid(self.smear_gate(x[:, :, :24]))
                x = x + gate * x_pre_smear

        # Forward the trunk of the Transformer
        x0 = x  # save initial normalized embedding for x0 residual
        n_layer = self.config.n_layer
        backout_layer = n_layer // 2  # cache at halfway point
        x_backout = None

        use_attn_res = self.config.use_attn_res
        use_engram = self.config.use_engram

        if use_attn_res:
            from nanochat.attn_res import AttnResState, block_attn_res
            # block_size = sublayers per block; each transformer layer has 2 sublayers (attn + MLP)
            block_size = n_layer * 2 // self.config.attn_res_num_blocks
            ar_state = AttnResState(x, block_size)

        for i, block in enumerate(self.transformer.h):
            if use_attn_res:
                # AttnRes path: depth-wise attention replaces fixed residual connections
                ar_layer = self.attn_res_layers[i]
                ve = self.value_embeds[str(i)](idx).to(
                    x.dtype) if str(i) in self.value_embeds else None

                # Attention sub-layer: get input via inter-block attention, compute attn output
                x_attn_in = ar_layer.get_input_for_attn(ar_state)
                # Engram injection into AttnRes input so gradients flow through attn → loss
                if use_engram and str(i) in self.engram_layers:
                    x_attn_in = x_attn_in + \
                        self.engram_layers[str(i)](x_attn_in, idx)
                attn_out = block.attn(
                    norm(x_attn_in), ve, cos_sin, self.window_sizes[i], kv_cache)
                ar_state.accumulate(attn_out)

                # MLP sub-layer: get input via inter-block attention, compute MLP output
                x_mlp_in = ar_layer.get_input_for_mlp(ar_state)
                mlp_out = block.mlp(norm(x_mlp_in))
                ar_state.accumulate(mlp_out)

                # For backout and downstream: reconstruct "current x" from latest block_attn_res
                if i == backout_layer or i == n_layer - 1:
                    x = block_attn_res(
                        ar_state.completed_blocks, ar_state.partial_block,
                        self.attn_res_layers[i].mlp_res_query,
                        self.attn_res_layers[i].mlp_res_norm,
                    )
                    if i == backout_layer:
                        x_backout = x
            else:
                # Standard path: fixed residual connections with per-layer scaling
                x = self.resid_lambdas[i] * x + self.x0_lambdas[i] * x0
                # Engram injection before transformer block
                if use_engram and str(i) in self.engram_layers:
                    x = x + self.engram_layers[str(i)](x, idx)
                ve = self.value_embeds[str(i)](idx).to(
                    x.dtype) if str(i) in self.value_embeds else None
                x = block(x, ve, cos_sin, self.window_sizes[i], kv_cache)
                if i == backout_layer:
                    x_backout = x

        # Final aggregation
        if use_attn_res:
            # AttnRes: final output is softmax attention over all completed blocks
            all_blocks = ar_state.finalize()
            from nanochat.attn_res import block_attn_res as _bar
            x = _bar(all_blocks[:-1] if len(all_blocks) > 1 else [], all_blocks[-1],
                     self.attn_res_out_query, self.attn_res_out_norm)

        # Subtract mid-layer residual to remove low-level features before logit projection
        if x_backout is not None:
            x = x - self.backout_lambda.to(x.dtype) * x_backout
        x = norm(x)

        # Forward the lm_head (compute logits)
        # smoothly cap the logits to the range [-softcap, softcap]
        softcap = 15
        # (B, T, padded_vocab_size) <- very big tensor, large amount of memory
        logits = self.lm_head(x)
        # slice to remove padding
        logits = logits[..., :self.config.vocab_size]
        logits = logits.float()  # switch to fp32 for logit softcap and loss computation
        logits = softcap * torch.tanh(logits / softcap)  # squash the logits

        if targets is not None:
            # training: given the targets, compute and return the loss
            # TODO experiment with chunked cross-entropy?
            loss = F.cross_entropy(logits.view(-1, logits.size(-1)),
                                   targets.view(-1), ignore_index=-1, reduction=loss_reduction)
            return loss
        else:
            # inference: just return the logits directly
            return logits

    @torch.inference_mode()
    def generate(self, tokens, max_tokens, temperature=1.0, top_k=None, seed=42):
        """
        Naive autoregressive streaming inference.
        To make it super simple, let's assume:
        - batch size is 1
        - ids and the yielded tokens are simple Python lists and ints
        """
        assert isinstance(tokens, list)
        device = self.get_device()
        rng = None
        if temperature > 0:
            rng = torch.Generator(device=device)
            rng.manual_seed(seed)
        ids = torch.tensor([tokens], dtype=torch.long,
                           device=device)  # add batch dim
        for _ in range(max_tokens):
            logits = self.forward(ids)  # (B, T, vocab_size)
            logits = logits[:, -1, :]  # (B, vocab_size)
            if top_k is not None and top_k > 0:
                v, _ = torch.topk(logits, min(top_k, logits.size(-1)))
                logits[logits < v[:, [-1]]] = -float('Inf')
            if temperature > 0:
                logits = logits / temperature
                probs = F.softmax(logits, dim=-1)
                next_ids = torch.multinomial(
                    probs, num_samples=1, generator=rng)
            else:
                next_ids = torch.argmax(logits, dim=-1, keepdim=True)
            ids = torch.cat((ids, next_ids), dim=1)
            token = next_ids.item()
            yield token
