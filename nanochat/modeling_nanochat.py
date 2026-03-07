from __future__ import annotations

from typing import Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from transformers import PreTrainedModel
from transformers.generation.utils import GenerationMixin
from transformers.modeling_outputs import BaseModelOutputWithPast, CausalLMOutputWithPast

try:
    from .configuration_nanochat import NanochatConfig
except ImportError:
    from configuration_nanochat import NanochatConfig


def norm(x: torch.Tensor) -> torch.Tensor:
    return F.rms_norm(x, (x.size(-1),))


class Linear(nn.Linear):
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return F.linear(x, self.weight.to(dtype=x.dtype))


def has_ve(layer_idx: int, n_layer: int) -> bool:
    return layer_idx % 2 == (n_layer - 1) % 2


def apply_rotary_emb(x: torch.Tensor, cos: torch.Tensor, sin: torch.Tensor) -> torch.Tensor:
    half = x.shape[-1] // 2
    x1, x2 = x[..., :half], x[..., half:]
    y1 = x1 * cos + x2 * sin
    y2 = x1 * (-sin) + x2 * cos
    return torch.cat([y1, y2], dim=-1)


class NanochatAttention(nn.Module):
    def __init__(self, config: NanochatConfig, layer_idx: int):
        super().__init__()
        self.layer_idx = layer_idx
        self.n_head = config.n_head
        self.n_kv_head = config.n_kv_head
        self.n_embd = config.n_embd
        self.head_dim = config.head_dim
        self.ve_gate_channels = 32
        self.c_q = Linear(self.n_embd, self.n_head * self.head_dim, bias=False)
        self.c_k = Linear(self.n_embd, self.n_kv_head *
                          self.head_dim, bias=False)
        self.c_v = Linear(self.n_embd, self.n_kv_head *
                          self.head_dim, bias=False)
        self.c_proj = Linear(self.n_embd, self.n_embd, bias=False)
        self.ve_gate = Linear(self.ve_gate_channels, self.n_kv_head, bias=False) if has_ve(
            layer_idx, config.n_layer) else None

    def _build_attn_mask(
        self,
        batch_size: int,
        query_len: int,
        key_len: int,
        past_len: int,
        window_size: int,
        attention_mask: Optional[torch.Tensor],
        device: torch.device,
    ) -> torch.Tensor:
        query_positions = torch.arange(
            past_len, past_len + query_len, device=device)
        key_positions = torch.arange(key_len, device=device)
        mask = key_positions.unsqueeze(0) <= query_positions.unsqueeze(1)
        if window_size < key_len:
            min_key = query_positions.unsqueeze(1) - window_size + 1
            mask = mask & (key_positions.unsqueeze(0) >= min_key)
        mask = mask.unsqueeze(0).unsqueeze(1).expand(
            batch_size, 1, query_len, key_len)
        if attention_mask is not None:
            key_mask = attention_mask[:, -
                                      key_len:].to(dtype=torch.bool, device=device)
            mask = mask & key_mask[:, None, None, :]
        return mask

    def forward(
        self,
        x: torch.Tensor,
        ve: Optional[torch.Tensor],
        cos: torch.Tensor,
        sin: torch.Tensor,
        window_size: int,
        attention_mask: Optional[torch.Tensor],
        past_key_value: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
        use_cache: bool = False,
    ) -> Tuple[torch.Tensor, Optional[Tuple[torch.Tensor, torch.Tensor]]]:
        batch_size, query_len, _ = x.shape
        q = self.c_q(x).view(batch_size, query_len, self.n_head, self.head_dim)
        k = self.c_k(x).view(batch_size, query_len,
                             self.n_kv_head, self.head_dim)
        v = self.c_v(x).view(batch_size, query_len,
                             self.n_kv_head, self.head_dim)

        if ve is not None:
            ve = ve.view(batch_size, query_len, self.n_kv_head, self.head_dim)
            gate = 2 * \
                torch.sigmoid(self.ve_gate(x[..., :self.ve_gate_channels]))
            v = v + gate.unsqueeze(-1) * ve

        q = norm(apply_rotary_emb(q, cos, sin))
        k = norm(apply_rotary_emb(k, cos, sin))

        q = q.transpose(1, 2)
        k = k.transpose(1, 2)
        v = v.transpose(1, 2)

        past_len = 0
        if past_key_value is not None:
            past_k, past_v = past_key_value
            past_len = past_k.size(-2)
            k = torch.cat((past_k, k), dim=-2)
            v = torch.cat((past_v, v), dim=-2)

        present = (k, v) if use_cache else None

        if self.n_kv_head != self.n_head:
            repeats = self.n_head // self.n_kv_head
            k_for_attn = k.repeat_interleave(repeats, dim=1)
            v_for_attn = v.repeat_interleave(repeats, dim=1)
        else:
            k_for_attn = k
            v_for_attn = v

        key_len = k_for_attn.size(-2)
        attn_mask = self._build_attn_mask(
            batch_size=batch_size,
            query_len=query_len,
            key_len=key_len,
            past_len=past_len,
            window_size=window_size,
            attention_mask=attention_mask,
            device=x.device,
        )
        y = F.scaled_dot_product_attention(
            q, k_for_attn, v_for_attn, attn_mask=attn_mask)
        y = y.transpose(1, 2).contiguous().view(
            batch_size, query_len, self.n_embd)
        y = self.c_proj(y)
        return y, present


class NanochatMLP(nn.Module):
    def __init__(self, config: NanochatConfig):
        super().__init__()
        self.c_fc = Linear(config.n_embd, 4 * config.n_embd, bias=False)
        self.c_proj = Linear(4 * config.n_embd, config.n_embd, bias=False)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.c_proj(F.relu(self.c_fc(x)).square())


class NanochatBlock(nn.Module):
    def __init__(self, config: NanochatConfig, layer_idx: int):
        super().__init__()
        self.attn = NanochatAttention(config, layer_idx)
        self.mlp = NanochatMLP(config)

    def forward(
        self,
        x: torch.Tensor,
        ve: Optional[torch.Tensor],
        cos: torch.Tensor,
        sin: torch.Tensor,
        window_size: int,
        attention_mask: Optional[torch.Tensor],
        past_key_value: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
        use_cache: bool = False,
    ) -> Tuple[torch.Tensor, Optional[Tuple[torch.Tensor, torch.Tensor]]]:
        attn_out, present = self.attn(
            norm(x),
            ve=ve,
            cos=cos,
            sin=sin,
            window_size=window_size,
            attention_mask=attention_mask,
            past_key_value=past_key_value,
            use_cache=use_cache,
        )
        x = x + attn_out
        x = x + self.mlp(norm(x))
        return x, present


class NanochatPreTrainedModel(PreTrainedModel):
    config_class = NanochatConfig
    base_model_prefix = "model"
    supports_gradient_checkpointing = False
    _no_split_modules = ["NanochatBlock"]

    def _init_weights(self, module: nn.Module) -> None:
        return None


class NanochatModel(NanochatPreTrainedModel):
    def __init__(self, config: NanochatConfig):
        super().__init__(config)
        self.window_sizes = self._compute_window_sizes(config)
        padded_vocab_size = config.padded_vocab_size
        self.transformer = nn.ModuleDict(
            {
                "wte": nn.Embedding(padded_vocab_size, config.n_embd),
                "h": nn.ModuleList([NanochatBlock(config, layer_idx) for layer_idx in range(config.n_layer)]),
            }
        )
        self.resid_lambdas = nn.Parameter(torch.ones(config.n_layer))
        self.x0_lambdas = nn.Parameter(torch.zeros(config.n_layer))
        kv_dim = config.n_kv_head * config.head_dim
        self.value_embeds = nn.ModuleDict(
            {str(i): nn.Embedding(padded_vocab_size, kv_dim)
             for i in range(config.n_layer) if has_ve(i, config.n_layer)}
        )
        self.rotary_seq_len = config.sequence_len * 10
        cos, sin = self._precompute_rotary_embeddings(
            self.rotary_seq_len, config.head_dim)
        self.register_buffer("cos", cos, persistent=False)
        self.register_buffer("sin", sin, persistent=False)

    def _precompute_rotary_embeddings(
        self,
        seq_len: int,
        head_dim: int,
        base: int = 10000,
        device: Optional[torch.device] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        if device is None:
            device = self.transformer["wte"].weight.device
        channel_range = torch.arange(
            0, head_dim, 2, dtype=torch.float32, device=device)
        inv_freq = 1.0 / (base ** (channel_range / head_dim))
        t = torch.arange(seq_len, dtype=torch.float32, device=device)
        freqs = torch.outer(t, inv_freq)
        cos, sin = freqs.cos(), freqs.sin()
        return cos[None, :, None, :], sin[None, :, None, :]

    def _compute_window_sizes(self, config: NanochatConfig) -> list[int]:
        pattern = config.window_pattern.upper()
        long_window = config.sequence_len
        short_window = long_window // 2
        sizes = []
        for layer_idx in range(config.n_layer):
            char = pattern[layer_idx % len(pattern)]
            sizes.append(short_window if char == "S" else long_window)
        sizes[-1] = long_window
        return sizes

    def get_input_embeddings(self) -> nn.Embedding:
        return self.transformer["wte"]

    def set_input_embeddings(self, value: nn.Embedding) -> None:
        self.transformer["wte"] = value

    def forward(
        self,
        input_ids: torch.LongTensor,
        attention_mask: Optional[torch.Tensor] = None,
        past_key_values: Optional[Tuple[Tuple[torch.Tensor,
                                              torch.Tensor], ...]] = None,
        token_type_ids: Optional[torch.Tensor] = None,
        use_cache: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        **kwargs,
    ):
        use_cache = self.config.use_cache if use_cache is None else use_cache
        output_hidden_states = self.config.output_hidden_states if output_hidden_states is None else output_hidden_states
        return_dict = self.config.use_return_dict if return_dict is None else return_dict
        batch_size, seq_len = input_ids.shape

        past_len = 0
        if past_key_values is not None and len(past_key_values) > 0:
            past_len = past_key_values[0][0].size(-2)
        end = past_len + seq_len
        if end > self.cos.size(1):
            cos, sin = self._precompute_rotary_embeddings(
                end * 2, self.config.head_dim, device=input_ids.device)
            self.cos = cos
            self.sin = sin
        cos = self.cos[:, past_len:end].to(
            dtype=self.transformer["wte"].weight.dtype, device=input_ids.device)
        sin = self.sin[:, past_len:end].to(
            dtype=self.transformer["wte"].weight.dtype, device=input_ids.device)

        hidden_states = self.transformer["wte"](input_ids)
        hidden_states = hidden_states.to(
            dtype=self.transformer["wte"].weight.dtype)
        hidden_states = norm(hidden_states)
        x0 = hidden_states

        all_hidden_states = () if output_hidden_states else None
        presents = () if use_cache else None
        for layer_idx, block in enumerate(self.transformer["h"]):
            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)
            hidden_states = self.resid_lambdas[layer_idx] * \
                hidden_states + self.x0_lambdas[layer_idx] * x0
            ve = self.value_embeds[str(layer_idx)](input_ids).to(
                hidden_states.dtype) if str(layer_idx) in self.value_embeds else None
            layer_past = None if past_key_values is None else past_key_values[layer_idx]
            hidden_states, present = block(
                hidden_states,
                ve=ve,
                cos=cos,
                sin=sin,
                window_size=self.window_sizes[layer_idx],
                attention_mask=attention_mask,
                past_key_value=layer_past,
                use_cache=use_cache,
            )
            if use_cache:
                presents = presents + (present,)

        hidden_states = norm(hidden_states)
        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        if not return_dict:
            outputs = (hidden_states, presents, all_hidden_states)
            return tuple(output for output in outputs if output is not None)
        return BaseModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=presents,
            hidden_states=all_hidden_states,
        )


class NanochatForCausalLM(NanochatPreTrainedModel, GenerationMixin):
    _tied_weights_keys = []
    all_tied_weights_keys = {}

    def __init__(self, config: NanochatConfig):
        super().__init__(config)
        self.model = NanochatModel(config)
        self.lm_head = Linear(
            config.n_embd, config.padded_vocab_size, bias=False)

    def get_input_embeddings(self) -> nn.Embedding:
        return self.model.get_input_embeddings()

    def set_input_embeddings(self, value: nn.Embedding) -> None:
        self.model.set_input_embeddings(value)

    def get_output_embeddings(self) -> nn.Module:
        return self.lm_head

    def set_output_embeddings(self, new_embeddings: nn.Module) -> None:
        self.lm_head = new_embeddings

    def forward(
        self,
        input_ids: torch.LongTensor,
        attention_mask: Optional[torch.Tensor] = None,
        past_key_values: Optional[Tuple[Tuple[torch.Tensor,
                                              torch.Tensor], ...]] = None,
        token_type_ids: Optional[torch.Tensor] = None,
        labels: Optional[torch.LongTensor] = None,
        use_cache: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        **kwargs,
    ):
        return_dict = self.config.use_return_dict if return_dict is None else return_dict
        outputs = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            past_key_values=past_key_values,
            token_type_ids=token_type_ids,
            use_cache=use_cache,
            output_hidden_states=output_hidden_states,
            return_dict=True,
            **kwargs,
        )
        logits = self.lm_head(
            outputs.last_hidden_state)[..., : self.config.vocab_size]
        logits = logits.float()

        softcap = 20.0
        logits = softcap * torch.tanh(logits / softcap)

        loss = None
        if labels is not None:
            shift_logits = logits[:, :-1, :].contiguous()
            shift_labels = labels[:, 1:].contiguous()
            loss = F.cross_entropy(
                shift_logits.view(-1, shift_logits.size(-1)),
                shift_labels.view(-1),
                ignore_index=-100,
            )

        if not return_dict:
            result = (logits, outputs.past_key_values, outputs.hidden_states)
            return ((loss,) + result) if loss is not None else result
        return CausalLMOutputWithPast(
            loss=loss,
            logits=logits,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
        )

    def prepare_inputs_for_generation(
        self,
        input_ids: torch.LongTensor,
        past_key_values: Optional[Tuple[Tuple[torch.Tensor,
                                              torch.Tensor], ...]] = None,
        attention_mask: Optional[torch.Tensor] = None,
        **kwargs,
    ):
        if attention_mask is None:
            attention_mask = torch.ones_like(input_ids, dtype=torch.long)
        if past_key_values:
            input_ids = input_ids[:, -1:]
        return {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "past_key_values": past_key_values,
            "use_cache": kwargs.get("use_cache", True),
        }

    @staticmethod
    def _reorder_cache(
        past_key_values: Tuple[Tuple[torch.Tensor, torch.Tensor], ...],
        beam_idx: torch.LongTensor,
    ) -> Tuple[Tuple[torch.Tensor, torch.Tensor], ...]:
        reordered = []
        for key_states, value_states in past_key_values:
            reordered.append((key_states.index_select(
                0, beam_idx), value_states.index_select(0, beam_idx)))
        return tuple(reordered)
