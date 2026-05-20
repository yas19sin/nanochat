"""
Task-specific heads on top of a NanoChat GPT backbone.

These wrappers deliberately keep the base/SFT causal-LM checkpoint untouched:
- NanochatSequenceClassifier: supervised text classification.
- NanochatTextEmbedder: pooled hidden-state embeddings for retrieval/similarity.
- NanochatZeroShotClassifier: embedding similarity against label descriptions.
"""

from __future__ import annotations

import json
from pathlib import Path

import torch
import torch.nn as nn
import torch.nn.functional as F


def pool_hidden_states(
    hidden_states: torch.Tensor,
    attention_mask: torch.Tensor | None = None,
    mode: str = "last",
) -> torch.Tensor:
    if mode not in {"last", "mean", "first"}:
        raise ValueError(f"Unsupported pooling mode: {mode}")

    if mode == "first":
        return hidden_states[:, 0]

    if attention_mask is None:
        if mode == "last":
            return hidden_states[:, -1]
        return hidden_states.mean(dim=1)

    mask = attention_mask.to(device=hidden_states.device, dtype=torch.long)
    if mode == "last":
        lengths = mask.sum(dim=1).clamp(min=1) - 1
        batch = torch.arange(hidden_states.size(0), device=hidden_states.device)
        return hidden_states[batch, lengths]

    weights = mask.to(dtype=hidden_states.dtype).unsqueeze(-1)
    summed = (hidden_states * weights).sum(dim=1)
    denom = weights.sum(dim=1).clamp(min=1)
    return summed / denom


class NanochatSequenceClassifier(nn.Module):
    def __init__(
        self,
        backbone: nn.Module,
        num_labels: int,
        pooling: str = "last",
        dropout: float = 0.1,
        freeze_backbone: bool = False,
    ):
        super().__init__()
        self.backbone = backbone
        self.num_labels = num_labels
        self.pooling = pooling
        self.dropout = nn.Dropout(dropout)
        self.classifier = nn.Linear(backbone.config.n_embd, num_labels)
        if freeze_backbone:
            for param in self.backbone.parameters():
                param.requires_grad = False

    def forward(self, input_ids, attention_mask=None, labels=None):
        hidden_states = self.backbone.forward_hidden(input_ids)
        pooled = pool_hidden_states(hidden_states, attention_mask, self.pooling)
        pooled_for_head = self.dropout(pooled).to(dtype=self.classifier.weight.dtype)
        logits = self.classifier(pooled_for_head).float()
        loss = None
        if labels is not None:
            labels = labels.to(device=logits.device)
            loss = F.cross_entropy(logits, labels)
        return {"loss": loss, "logits": logits, "embeddings": pooled}

    def configure_optimizer(self, backbone_lr: float, head_lr: float, weight_decay: float = 0.01):
        backbone_params = [p for p in self.backbone.parameters() if p.requires_grad]
        head_params = [p for p in self.classifier.parameters() if p.requires_grad]
        groups = []
        if backbone_params:
            groups.append({"params": backbone_params, "lr": backbone_lr, "weight_decay": weight_decay})
        groups.append({"params": head_params, "lr": head_lr, "weight_decay": weight_decay})
        return torch.optim.AdamW(groups)


class NanochatTextEmbedder(nn.Module):
    def __init__(
        self,
        backbone: nn.Module,
        pooling: str = "mean",
        projection_dim: int = 0,
        normalize: bool = True,
    ):
        super().__init__()
        self.backbone = backbone
        self.pooling = pooling
        self.normalize = normalize
        self.projection = (
            nn.Linear(backbone.config.n_embd, projection_dim, bias=False)
            if projection_dim and projection_dim > 0
            else nn.Identity()
        )

    def forward(self, input_ids, attention_mask=None):
        hidden_states = self.backbone.forward_hidden(input_ids)
        pooled = pool_hidden_states(hidden_states, attention_mask, self.pooling)
        if isinstance(self.projection, nn.Linear):
            pooled = pooled.to(dtype=self.projection.weight.dtype)
        embeddings = self.projection(pooled).float()
        if self.normalize:
            embeddings = F.normalize(embeddings, p=2, dim=-1)
        return embeddings

    def configure_optimizer(self, backbone_lr: float, head_lr: float, weight_decay: float = 0.01):
        backbone_params = [p for p in self.backbone.parameters() if p.requires_grad]
        head_params = [p for p in self.projection.parameters() if p.requires_grad]
        groups = []
        if backbone_params:
            groups.append({"params": backbone_params, "lr": backbone_lr, "weight_decay": weight_decay})
        if head_params:
            groups.append({"params": head_params, "lr": head_lr, "weight_decay": weight_decay})
        return torch.optim.AdamW(groups)


class NanochatZeroShotClassifier:
    def __init__(
        self,
        embedder: NanochatTextEmbedder,
        tokenizer,
        label_texts: list[str],
        max_seq_len: int = 512,
        template: str = "{text}",
    ):
        self.embedder = embedder
        self.tokenizer = tokenizer
        self.label_texts = label_texts
        self.max_seq_len = max_seq_len
        self.template = template
        self._label_embeddings = None

    def _batch_encode(self, texts: list[str]):
        bos = self.tokenizer.get_bos_token_id()
        encoded = [
            self.tokenizer.encode(self.template.format(text=text), prepend=bos)[:self.max_seq_len]
            for text in texts
        ]
        max_len = max(len(ids) for ids in encoded)
        input_ids = []
        attention_mask = []
        for ids in encoded:
            pad = max_len - len(ids)
            input_ids.append(ids + [bos] * pad)
            attention_mask.append([1] * len(ids) + [0] * pad)
        device = self.embedder.backbone.get_device()
        return (
            torch.tensor(input_ids, dtype=torch.long, device=device),
            torch.tensor(attention_mask, dtype=torch.long, device=device),
        )

    @torch.inference_mode()
    def encode(self, texts: list[str]) -> torch.Tensor:
        input_ids, attention_mask = self._batch_encode(texts)
        return self.embedder(input_ids, attention_mask)

    @torch.inference_mode()
    def classify(self, texts: list[str]) -> list[dict]:
        if self._label_embeddings is None:
            self._label_embeddings = self.encode(self.label_texts)
        text_embeddings = self.encode(texts)
        scores = text_embeddings @ self._label_embeddings.T
        probs = scores.softmax(dim=-1)
        outputs = []
        for row_scores, row_probs in zip(scores, probs):
            pred = int(row_probs.argmax().item())
            outputs.append({
                "label": self.label_texts[pred],
                "score": float(row_probs[pred].item()),
                "scores": {
                    label: float(score.item())
                    for label, score in zip(self.label_texts, row_scores)
                },
                "probabilities": {
                    label: float(prob.item())
                    for label, prob in zip(self.label_texts, row_probs)
                },
            })
        return outputs


def save_downstream_checkpoint(
    output_dir: str | Path,
    step: int,
    model: nn.Module,
    meta: dict,
) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), output_dir / f"model_{step:06d}.pt")
    with (output_dir / f"meta_{step:06d}.json").open("w", encoding="utf-8") as handle:
        json.dump(meta, handle, indent=2, ensure_ascii=False)


def find_last_downstream_step(output_dir: str | Path) -> int:
    output_dir = Path(output_dir)
    steps = []
    for path in output_dir.glob("model_*.pt"):
        steps.append(int(path.stem.split("_")[-1]))
    if not steps:
        raise FileNotFoundError(f"No downstream model_*.pt checkpoint found in {output_dir}")
    return max(steps)


def load_downstream_checkpoint(output_dir: str | Path, step: int | None = None, map_location="cpu"):
    output_dir = Path(output_dir)
    if step is None:
        step = find_last_downstream_step(output_dir)
    state = torch.load(output_dir / f"model_{step:06d}.pt", map_location=map_location)
    with (output_dir / f"meta_{step:06d}.json").open("r", encoding="utf-8") as handle:
        meta = json.load(handle)
    return state, meta, step
