from __future__ import annotations

import copy
import math
from dataclasses import dataclass

import torch
from torch import nn
import torch.nn.functional as F


@dataclass
class JEPAOutput:
    predicted: torch.Tensor
    target: torch.Tensor
    context_embedding: torch.Tensor


class PatchEncoder(nn.Module):
    def __init__(self, input_channels: int, patch_minutes: int, d_model: int, layers: int,
                 heads: int, ffn_dim: int, dropout: float, max_tokens: int = 64):
        super().__init__()
        self.patch_minutes = int(patch_minutes)
        self.projection = nn.Linear(input_channels * patch_minutes, d_model)
        self.position = nn.Parameter(torch.zeros(1, max_tokens, d_model))
        self.mask_token = nn.Parameter(torch.zeros(1, 1, d_model))
        nn.init.trunc_normal_(self.position, std=0.02)
        nn.init.trunc_normal_(self.mask_token, std=0.02)
        layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=heads, dim_feedforward=ffn_dim, dropout=dropout,
            activation="gelu", batch_first=True, norm_first=True,
        )
        self.transformer = nn.TransformerEncoder(layer, num_layers=layers)
        self.norm = nn.LayerNorm(d_model)

    def patchify(self, values: torch.Tensor) -> torch.Tensor:
        batch, minutes, channels = values.shape
        if minutes % self.patch_minutes:
            raise ValueError("minutes must be divisible by patch_minutes")
        tokens = minutes // self.patch_minutes
        return values.reshape(batch, tokens, self.patch_minutes * channels)

    def forward(self, values: torch.Tensor, mask: torch.Tensor | None = None) -> torch.Tensor:
        patches = self.patchify(values)
        tokens = self.projection(patches)
        if tokens.shape[1] > self.position.shape[1]:
            raise ValueError("sequence exceeds positional embedding")
        if mask is not None:
            tokens = torch.where(mask.unsqueeze(-1), self.mask_token.expand_as(tokens), tokens)
        tokens = tokens + self.position[:, :tokens.shape[1]]
        return self.norm(self.transformer(tokens))


class LatentPredictor(nn.Module):
    def __init__(self, d_model: int, layers: int, heads: int, ffn_dim: int, dropout: float,
                 target_count: int, asset_count: int = 2):
        super().__init__()
        self.target_query = nn.Parameter(torch.zeros(1, target_count, d_model))
        self.asset_embedding = nn.Embedding(asset_count, d_model)
        nn.init.trunc_normal_(self.target_query, std=0.02)
        layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=heads, dim_feedforward=ffn_dim, dropout=dropout,
            activation="gelu", batch_first=True, norm_first=True,
        )
        self.network = nn.TransformerEncoder(layer, num_layers=layers)
        self.norm = nn.LayerNorm(d_model)

    def forward(self, context_tokens: torch.Tensor, asset_id: torch.Tensor) -> torch.Tensor:
        batch = context_tokens.shape[0]
        asset = self.asset_embedding(asset_id).unsqueeze(1)
        queries = self.target_query.expand(batch, -1, -1) + asset
        joined = torch.cat([context_tokens + asset, queries], dim=1)
        encoded = self.network(joined)
        return self.norm(encoded[:, -queries.shape[1]:])


class TemporalJEPA(nn.Module):
    def __init__(self, *, input_channels: int, context_minutes: int, patch_minutes: int,
                 target_end_offsets_minutes: list[int], d_model: int, encoder_layers: int,
                 predictor_layers: int, heads: int, ffn_dim: int, dropout: float):
        super().__init__()
        self.context_minutes = int(context_minutes)
        self.target_end_offsets_minutes = tuple(int(item) for item in target_end_offsets_minutes)
        if (not self.target_end_offsets_minutes
                or tuple(sorted(set(self.target_end_offsets_minutes))) != self.target_end_offsets_minutes
                or self.target_end_offsets_minutes[0] < patch_minutes):
            raise ValueError("target end offsets must be unique, increasing, and at least one patch")
        max_tokens = context_minutes // patch_minutes + 8
        self.context_encoder = PatchEncoder(input_channels, patch_minutes, d_model, encoder_layers,
                                            heads, ffn_dim, dropout, max_tokens=max_tokens)
        self.target_encoder = copy.deepcopy(self.context_encoder)
        for parameter in self.target_encoder.parameters():
            parameter.requires_grad_(False)
        self.target_encoder.eval()
        self.predictor = LatentPredictor(d_model, predictor_layers, heads, ffn_dim, dropout,
                                         len(self.target_end_offsets_minutes))

    def train(self, mode: bool = True) -> "TemporalJEPA":
        super().train(mode)
        self.target_encoder.eval()
        return self

    @torch.no_grad()
    def update_target(self, momentum: float) -> None:
        online_parameters = dict(self.context_encoder.named_parameters())
        for name, target in self.target_encoder.named_parameters():
            target.data.mul_(momentum).add_(online_parameters[name].data, alpha=1.0 - momentum)
        online_buffers = dict(self.context_encoder.named_buffers())
        for name, target in self.target_encoder.named_buffers():
            source = online_buffers[name]
            if target.is_floating_point():
                target.data.mul_(momentum).add_(source.data, alpha=1.0 - momentum)
            else:
                target.data.copy_(source.data)

    def encode(self, context: torch.Tensor) -> torch.Tensor:
        return self.context_encoder(context).mean(dim=1)

    def forward(self, context: torch.Tensor, targets: torch.Tensor, asset_id: torch.Tensor,
                context_mask: torch.Tensor | None = None) -> JEPAOutput:
        context_tokens = self.context_encoder(context, context_mask)
        predicted = self.predictor(context_tokens, asset_id)
        batch, target_count, minutes, channels = targets.shape
        with torch.no_grad():
            flat = targets.reshape(batch * target_count, minutes, channels)
            target_tokens = self.target_encoder(flat)
            target_embedding = target_tokens.mean(dim=1).reshape(batch, target_count, -1)
        return JEPAOutput(predicted=predicted, target=target_embedding,
                          context_embedding=context_tokens.mean(dim=1))


def model_parameter_count(model: nn.Module) -> dict[str, int]:
    return {
        "trainable": sum(p.numel() for p in model.parameters() if p.requires_grad),
        "total": sum(p.numel() for p in model.parameters()),
    }
