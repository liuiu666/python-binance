from __future__ import annotations

import torch
import torch.nn.functional as F


def jepa_loss(
    predicted: torch.Tensor,
    target: torch.Tensor,
    context_embedding: torch.Tensor | None = None,
    *,
    variance_weight: float = 0.05,
    context_variance_weight: float = 0.1,
    context_covariance_weight: float = 0.05,
    context_std_target: float = 0.25,
) -> tuple[torch.Tensor, dict[str, float]]:
    predicted_norm = F.normalize(predicted, dim=-1)
    target_norm = F.normalize(target.detach(), dim=-1)
    smooth = F.smooth_l1_loss(predicted_norm, target_norm)
    cosine = 1.0 - (predicted_norm * target_norm).sum(dim=-1).mean()
    # Measure diversity across samples separately for each target horizon. If the
    # target-query dimension were flattened into the batch, fixed 10/30/60m
    # query differences could satisfy this penalty without encoding any market
    # state variation.
    predicted_std = torch.sqrt(
        predicted.var(dim=0, unbiased=False) + 1e-4
    )
    variance = torch.relu(0.1 - predicted_std).mean()
    target_std = torch.sqrt(
        target.detach().var(dim=0, unbiased=False) + 1e-4
    )

    context_variance = predicted.new_zeros(())
    context_covariance = predicted.new_zeros(())
    context_std = predicted.new_full((), float("nan"))
    if context_embedding is not None:
        if context_embedding.ndim != 2 or context_embedding.shape[0] != predicted.shape[0]:
            raise ValueError("context_embedding must be [batch, dimension]")
        centered = context_embedding - context_embedding.mean(dim=0, keepdim=True)
        per_dimension_std = torch.sqrt(
            centered.var(dim=0, unbiased=False) + 1e-4
        )
        context_std = per_dimension_std.mean()
        context_variance = torch.relu(
            float(context_std_target) - per_dimension_std
        ).mean()
        standardized = centered / per_dimension_std
        correlation = standardized.T @ standardized / max(len(standardized), 1)
        off_diagonal = correlation - torch.diag_embed(torch.diagonal(correlation))
        dimension = max(int(correlation.shape[0]), 1)
        context_covariance = off_diagonal.square().sum() / max(
            dimension * (dimension - 1),
            1,
        )

    loss = (
        smooth
        + cosine
        + float(variance_weight) * variance
        + float(context_variance_weight) * context_variance
        + float(context_covariance_weight) * context_covariance
    )
    return loss, {
        "smoothL1": float(smooth.detach()),
        "cosine": float(cosine.detach()),
        "variancePenalty": float(variance.detach()),
        "predictedDimStd": float(predicted_std.mean().detach()),
        "targetDimStd": float(target_std.mean().detach()),
        "contextVariancePenalty": float(context_variance.detach()),
        "contextCovariancePenalty": float(context_covariance.detach()),
        "contextDimStd": float(context_std.detach()),
    }


@torch.no_grad()
def representation_diagnostics(embedding: torch.Tensor) -> dict[str, float]:
    values = embedding.float()
    centered = values - values.mean(dim=0, keepdim=True)
    std = centered.std(dim=0, unbiased=False)
    covariance = centered.T @ centered / max(len(centered), 1)
    eigenvalues = torch.linalg.eigvalsh(covariance).clamp_min(1e-12)
    probability = eigenvalues / eigenvalues.sum()
    effective_rank = torch.exp(-(probability * probability.log()).sum())
    normalized = F.normalize(values, dim=-1)
    similarity = normalized @ normalized.T
    off_diagonal = (similarity.sum() - similarity.diag().sum()) / max(values.shape[0] * (values.shape[0] - 1), 1)
    return {"meanDimStd": float(std.mean()), "minDimStd": float(std.min()),
            "effectiveRank": float(effective_rank), "meanPairCosine": float(off_diagonal)}
