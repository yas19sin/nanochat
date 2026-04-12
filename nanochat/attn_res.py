"""
Block Attention Residuals for NanoChat.
Ported from MoonshotAI/Attention-Residuals (Chen et al., 2026).

Standard residuals do h_{l+1} = h_l + f_l(h_l) — magnitude grows O(L) and
early layer contributions get diluted.  Block AttnRes groups layers into N
blocks, accumulates within blocks via standard residuals, and applies learned
depth-wise softmax attention over block-level representations.  This gives
input-dependent weighting across depth at O(N·d) memory instead of O(L·d).

Usage:
    See GPTConfig fields `use_attn_res` and `attn_res_num_blocks` in gpt.py.
    When enabled, the forward loop replaces fixed residual connections with
    block_attn_res() calls.
"""

import torch
import torch.nn as nn
import torch.nn.functional as F


def block_attn_res(
    completed_blocks: list[torch.Tensor],
    partial_block: torch.Tensor | None,
    w_l: torch.Tensor,
    norm_fn,
) -> torch.Tensor:
    """
    Inter-block attention: softmax over depth dimension to aggregate block
    representations.  (Eq. 6 in the AttnRes paper.)

    Args:
        completed_blocks: list of N finalized block reps, each [B, T, d]
        partial_block: running intra-block sum [B, T, d], or None at block start
        w_l: learnable pseudo-query vector [d]
        norm_fn: RMSNorm applied to keys (prevents magnitude bias between
                 full blocks and partial sums with fewer outputs)

    Returns:
        Weighted aggregation over all blocks [B, T, d]
    """
    if partial_block is not None:
        sources = completed_blocks + [partial_block]
    else:
        sources = completed_blocks
    # V: [num_sources, B, T, d]
    V = torch.stack(sources, dim=0)
    K = norm_fn(V)
    # logits: [num_sources, B, T] — dot product of pseudo-query with each key
    # Cast pseudo-query to match K dtype (K may be bf16 during generation without autocast)
    w_cast = w_l.to(K.dtype) if w_l.dtype != K.dtype else w_l
    logits = torch.einsum("d, n b t d -> n b t", w_cast, K)
    # alpha: softmax over block dimension (dim=0), NOT sequence dimension
    alpha = logits.softmax(dim=0)
    # Ensure matching dtypes (softmax may produce fp32 while V is bf16 during generation)
    if alpha.dtype != V.dtype:
        alpha = alpha.to(V.dtype)
    # weighted sum over blocks → [B, T, d]
    return torch.einsum("n b t, n b t d -> b t d", alpha, V)


class AttnResNorm(nn.Module):
    """RMSNorm for AttnRes keys — learnable weight, applied per-token."""

    def __init__(self, d: int, eps: float = 1e-6):
        super().__init__()
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(d))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        rms = x.float().pow(2).mean(-1, keepdim=True).add(self.eps).rsqrt()
        return (x.float() * rms).to(x.dtype) * self.weight


class AttnResState:
    """Mutable state threaded through the forward loop when AttnRes is active."""

    __slots__ = ("completed_blocks", "partial_block",
                 "layer_in_block", "block_size")

    def __init__(self, embedding: torch.Tensor, block_size: int):
        # b_0 = initial token embedding (before any transformer layer)
        self.completed_blocks: list[torch.Tensor] = [embedding]
        self.partial_block: torch.Tensor | None = None
        self.layer_in_block: int = 0
        self.block_size: int = block_size

    def maybe_commit(self):
        """If we've reached a block boundary, finalize the current partial block."""
        if self.layer_in_block == self.block_size:
            assert self.partial_block is not None
            self.completed_blocks.append(self.partial_block)
            self.partial_block = None
            self.layer_in_block = 0

    def accumulate(self, sublayer_output: torch.Tensor):
        """Add a sublayer's output to the running intra-block sum."""
        if self.partial_block is None:
            self.partial_block = sublayer_output
        else:
            self.partial_block = self.partial_block + sublayer_output
        self.layer_in_block += 1

    def finalize(self) -> list[torch.Tensor]:
        """Commit any remaining partial block and return all block reps."""
        if self.partial_block is not None:
            self.completed_blocks.append(self.partial_block)
            self.partial_block = None
        return self.completed_blocks


class AttnResLayer(nn.Module):
    """
    Per-layer AttnRes parameters: two pseudo-query vectors and two RMSNorms
    (one pair for the attention sub-layer, one for the MLP sub-layer).

    These are lightweight — 2×d params + 2×d norm weights per layer.
    """

    def __init__(self, d: int):
        super().__init__()
        # AttnRes (Chen et al., 2026): separate pseudo-queries for attn and MLP
        self.attn_res_query = nn.Parameter(torch.zeros(d))
        self.attn_res_norm = AttnResNorm(d)
        self.mlp_res_query = nn.Parameter(torch.zeros(d))
        self.mlp_res_norm = AttnResNorm(d)

    def get_input_for_attn(self, state: AttnResState) -> torch.Tensor:
        """Compute the input to the attention sub-layer via depth-wise attention."""
        state.maybe_commit()
        return block_attn_res(
            state.completed_blocks, state.partial_block,
            self.attn_res_query, self.attn_res_norm,
        )

    def get_input_for_mlp(self, state: AttnResState) -> torch.Tensor:
        """Compute the input to the MLP sub-layer via depth-wise attention."""
        state.maybe_commit()
        return block_attn_res(
            state.completed_blocks, state.partial_block,
            self.mlp_res_query, self.mlp_res_norm,
        )
