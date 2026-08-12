from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np
import pandas as pd

MINUTE_NS = 60 * 1_000_000_000


@dataclass(frozen=True)
class DecisionSemantics:
    """Causal convention used by every probe sample.

    A row whose K-line ``open_time`` is t is complete at t + one minute.
    Consequently, decision index i includes row i in its context, enters at
    close[i], and a H-minute label settles at close[i + H].
    """

    context_minutes: int
    horizon_minutes: int
    step_minutes: int


def _utc_timestamp(value: str | pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def build_decision_frame(
    asset: Any,
    start: str | pd.Timestamp,
    end_exclusive: str | pd.Timestamp,
    *,
    step_minutes: int,
    context_minutes: int,
    horizon_minutes: int,
    max_settle_time: str | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Build aligned BTC decision rows without crossing an outcome boundary.

    ``max_settle_time`` is inclusive. This matches the walk-forward rule that a
    training outcome is usable exactly at its settlement timestamp.
    """
    step = int(step_minutes)
    context = int(context_minutes)
    horizon = int(horizon_minutes)
    if min(step, context, horizon) <= 0:
        raise ValueError("step_minutes, context_minutes, and horizon_minutes must be positive")

    times = np.asarray(asset.times, dtype=np.int64)
    close = np.asarray(asset.close)
    if len(times) != len(close):
        raise ValueError("asset times and close arrays have different lengths")
    if len(times) < context + horizon:
        return _empty_decision_frame()

    # i is the last fully observed one-minute bar. Its close becomes available
    # one minute after open_time[i]. The horizon settlement is close[i + H].
    indices = np.arange(context - 1, len(times) - horizon, dtype=np.int64)
    decision_ns = times[indices] + MINUTE_NS
    settle_indices = indices + horizon
    settle_ns = times[settle_indices] + MINUTE_NS

    expected_settle_ns = decision_ns + horizon * MINUTE_NS
    if not np.array_equal(settle_ns, expected_settle_ns):
        raise ValueError("asset history is not contiguous across decision horizons")

    start_ns = _utc_timestamp(start).value
    end_ns = _utc_timestamp(end_exclusive).value
    mask = (decision_ns >= start_ns) & (decision_ns < end_ns)
    mask &= (decision_ns // MINUTE_NS) % step == 0
    if max_settle_time is not None:
        mask &= settle_ns <= _utc_timestamp(max_settle_time).value

    indices = indices[mask]
    decision_ns = decision_ns[mask]
    settle_indices = settle_indices[mask]
    settle_ns = settle_ns[mask]
    entry = close[indices].astype(np.float64, copy=False)
    settle = close[settle_indices].astype(np.float64, copy=False)
    raw_move = settle / entry - 1.0
    return pd.DataFrame(
        {
            "sample_index": indices,
            "context_start_index": indices - context + 1,
            "time": pd.to_datetime(decision_ns, utc=True),
            "context_end_open_time": pd.to_datetime(times[indices], utc=True),
            "settle_time": pd.to_datetime(settle_ns, utc=True),
            "entry": entry,
            "settle": settle,
            "raw_move": raw_move,
            "up": settle > entry,
            "tie": settle == entry,
        }
    )


def _empty_decision_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "sample_index": pd.Series(dtype="int64"),
            "context_start_index": pd.Series(dtype="int64"),
            "time": pd.Series(dtype="datetime64[ns, UTC]"),
            "context_end_open_time": pd.Series(dtype="datetime64[ns, UTC]"),
            "settle_time": pd.Series(dtype="datetime64[ns, UTC]"),
            "entry": pd.Series(dtype="float64"),
            "settle": pd.Series(dtype="float64"),
            "raw_move": pd.Series(dtype="float64"),
            "up": pd.Series(dtype="bool"),
            "tie": pd.Series(dtype="bool"),
        }
    )


def handcrafted_features(
    asset: Any,
    decision_indices: Sequence[int] | np.ndarray,
    *,
    windows: Sequence[int] = (10, 30, 60, 240, 480),
) -> tuple[np.ndarray, list[str]]:
    """Create fixed, causal rolling aggregates from the eight input channels."""
    indices = np.asarray(decision_indices, dtype=np.int64)
    widths = tuple(int(item) for item in windows)
    if any(width <= 0 for width in widths):
        raise ValueError("feature windows must be positive")
    if len(indices) and (indices.min() < max(widths) - 1 or indices.max() >= len(asset.values)):
        raise ValueError("decision index lacks feature history or exceeds asset data")
    # Stop at the final decision row: future/frozen raw rows are neither needed
    # nor loaded into the handcrafted baseline's prefix statistics.
    end_exclusive = int(indices.max()) + 1 if len(indices) else 0
    values = np.asarray(asset.values[:end_exclusive], dtype=np.float64)

    prefix = np.vstack([np.zeros((1, values.shape[1])), np.cumsum(values, axis=0)])
    square_prefix = np.vstack([np.zeros((1, values.shape[1])), np.cumsum(values * values, axis=0)])
    parts: list[np.ndarray] = []
    names: list[str] = []
    for width in widths:
        end = indices + 1
        begin = end - width
        mean = (prefix[end] - prefix[begin]) / width
        second = (square_prefix[end] - square_prefix[begin]) / width
        std = np.sqrt(np.maximum(second - mean * mean, 0.0))
        parts.extend([mean, std])
        names.extend([f"mean_{width}m_c{channel}" for channel in range(values.shape[1])])
        names.extend([f"std_{width}m_c{channel}" for channel in range(values.shape[1])])
    parts.append(values[indices])
    names.extend([f"last_c{channel}" for channel in range(values.shape[1])])
    matrix = np.column_stack(parts).astype(np.float32) if len(indices) else np.empty((0, len(names)), np.float32)
    if not np.isfinite(matrix).all():
        raise ValueError("non-finite handcrafted features")
    return matrix, names


def freeze_encoder(model: Any) -> Any:
    """Put a JEPA model/encoder in inference mode and freeze all parameters."""
    model.eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    return model


def extract_embeddings(
    asset: Any,
    decision_indices: Sequence[int] | np.ndarray,
    model: Any,
    *,
    context_minutes: int,
    batch_size: int = 512,
    device: str | Any = "cuda",
) -> np.ndarray:
    """Extract mean-pooled frozen-encoder embeddings for causal contexts.

    Torch is imported lazily so sample construction and leakage tests remain
    usable in a CPU-only environment where torch is not installed.
    """
    import torch

    indices = np.asarray(decision_indices, dtype=np.int64)
    context = int(context_minutes)
    if context <= 0 or batch_size <= 0:
        raise ValueError("context_minutes and batch_size must be positive")
    if len(indices) and (indices.min() < context - 1 or indices.max() >= len(asset.values)):
        raise ValueError("decision index lacks encoder context or exceeds asset data")

    device_object = torch.device(device)
    frozen = freeze_encoder(model).to(device_object)
    outputs: list[np.ndarray] = []
    with torch.inference_mode():
        for offset in range(0, len(indices), int(batch_size)):
            batch_indices = indices[offset : offset + int(batch_size)]
            contexts = np.stack(
                [np.asarray(asset.values[index - context + 1 : index + 1], dtype=np.float32) for index in batch_indices]
            )
            tensor = torch.from_numpy(contexts).to(device_object, non_blocking=device_object.type == "cuda")
            with torch.autocast(
                device_type=device_object.type,
                dtype=torch.float16,
                enabled=device_object.type == "cuda",
            ):
                if hasattr(frozen, "encode"):
                    encoded = frozen.encode(tensor)
                else:
                    encoded = frozen(tensor).mean(dim=1)
            outputs.append(encoded.float().cpu().numpy())
    if not outputs:
        width = int(getattr(getattr(model, "context_encoder", model), "position").shape[-1])
        return np.empty((0, width), dtype=np.float32)
    result = np.concatenate(outputs).astype(np.float32, copy=False)
    if not np.isfinite(result).all():
        raise ValueError("encoder produced non-finite embeddings")
    return result
