from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

RAW_COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy_volume"]
CHANNEL_NAMES = ["return", "body", "range", "volume_change", "quote_change", "trades_change", "taker_share", "abs_return"]
CACHE_SCHEMA_VERSION = 2
PREPROCESSING_VERSION = "dimensionless-v1-nanosecond-time"


def _sha256(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        with path.open("rb") as stream:
            while chunk := stream.read(8 * 1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def _read(paths: list[Path]) -> pd.DataFrame:
    frames = [pd.read_csv(path, usecols=RAW_COLUMNS) for path in paths]
    frame = pd.concat(frames, ignore_index=True)
    frame["time"] = pd.to_datetime(frame.pop("open_time"), utc=True, errors="raise")
    frame = frame.sort_values("time")
    if frame["time"].duplicated().any():
        raise ValueError("duplicate timestamps in asset history")
    delta = frame["time"].diff().dropna().dt.total_seconds()
    if len(delta) and (delta != 60.0).any():
        raise ValueError("asset history contains minute gaps")
    return frame


def dimensionless_channels(frame: pd.DataFrame) -> np.ndarray:
    """Causal fixed-scale channels; no statistics are fitted on future rows."""
    eps = 1e-12
    open_ = frame["open"].to_numpy(np.float64)
    high = frame["high"].to_numpy(np.float64)
    low = frame["low"].to_numpy(np.float64)
    close = frame["close"].to_numpy(np.float64)
    volume = frame["volume"].to_numpy(np.float64)
    quote = frame["quote_volume"].to_numpy(np.float64)
    trades = frame["trades"].to_numpy(np.float64)
    taker = frame["taker_buy_volume"].to_numpy(np.float64)
    log_close = np.log(np.maximum(close, eps))
    ret = np.diff(log_close, prepend=log_close[0])

    def log_change(values: np.ndarray) -> np.ndarray:
        logged = np.log1p(np.maximum(values, 0.0))
        return np.clip(np.diff(logged, prepend=logged[0]), -5.0, 5.0)

    channels = np.column_stack([
        np.clip(ret * 1000.0, -10.0, 10.0),
        np.clip(np.log(np.maximum(close, eps) / np.maximum(open_, eps)) * 1000.0, -10.0, 10.0),
        np.clip(np.log(np.maximum(high, eps) / np.maximum(low, eps)) * 1000.0, 0.0, 20.0),
        log_change(volume), log_change(quote), log_change(trades),
        np.clip((taker / np.maximum(volume, eps) - 0.5) * 2.0, -1.0, 1.0),
        np.clip(np.abs(ret) * 1000.0, 0.0, 10.0),
    ]).astype(np.float32)
    if not np.isfinite(channels).all():
        raise ValueError("non-finite dimensionless channels")
    return channels


def build_cache(paths: list[Path], cache_prefix: Path, force: bool = False) -> dict[str, Any]:
    values_path = cache_prefix.with_suffix(".values.npy")
    times_path = cache_prefix.with_suffix(".times.npy")
    close_path = cache_prefix.with_suffix(".close.npy")
    meta_path = cache_prefix.with_suffix(".json")
    source_hash = _sha256(paths)
    if not force and all(path.exists() for path in (values_path, times_path, close_path, meta_path)):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if (meta.get("sourceSha256") == source_hash
                and meta.get("cacheSchemaVersion") == CACHE_SCHEMA_VERSION
                and meta.get("preprocessingVersion") == PREPROCESSING_VERSION):
            return meta
    frame = _read(paths)
    values = dimensionless_channels(frame)
    # Pandas may retain microsecond precision after CSV parsing. Store an
    # explicit nanosecond epoch so Timestamp.value/search boundaries agree.
    times = frame["time"].to_numpy(dtype="datetime64[ns]").astype(np.int64)
    close = frame["close"].to_numpy(np.float32)
    cache_prefix.parent.mkdir(parents=True, exist_ok=True)
    np.save(values_path, values)
    np.save(times_path, times)
    np.save(close_path, close)
    meta = {
        "cacheSchemaVersion": CACHE_SCHEMA_VERSION,
        "preprocessingVersion": PREPROCESSING_VERSION,
        "sourceSha256": source_hash, "rows": int(len(frame)),
        "start": frame["time"].iloc[0].isoformat(), "end": frame["time"].iloc[-1].isoformat(),
        "channels": CHANNEL_NAMES, "values": str(values_path), "times": str(times_path), "close": str(close_path),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


class AssetArrays:
    def __init__(self, cache_prefix: Path, asset_id: int):
        self.asset_id = int(asset_id)
        self.values = np.load(cache_prefix.with_suffix(".values.npy"), mmap_mode="r")
        self.times = np.load(cache_prefix.with_suffix(".times.npy"), mmap_mode="r")
        self.close = np.load(cache_prefix.with_suffix(".close.npy"), mmap_mode="r")

    def end_index(self, end_exclusive: str) -> int:
        timestamp = pd.Timestamp(end_exclusive).value
        return int(np.searchsorted(self.times, timestamp, side="left"))

    def range_indices(self, start: str, end_exclusive: str) -> tuple[int, int]:
        return int(np.searchsorted(self.times, pd.Timestamp(start).value, side="left")), self.end_index(end_exclusive)

    def close_maps(self) -> None:
        for array in (self.values, self.times, self.close):
            mapping = getattr(array, "_mmap", None)
            if mapping is not None:
                mapping.close()


def make_context_mask(batch: int, tokens: int, device: Any, mask_ratio: float = 0.25,
                      protected_tail_tokens: int = 6) -> Any:
    import torch
    maskable = max(tokens - int(protected_tail_tokens), 0)
    if maskable == 0 or mask_ratio <= 0:
        return torch.zeros((batch, tokens), dtype=torch.bool, device=device)
    width = min(maskable, max(1, int(round(tokens * mask_ratio))))
    starts = torch.randint(0, max(maskable - width + 1, 1), (batch,), device=device)
    positions = torch.arange(tokens, device=device).unsqueeze(0)
    return (positions >= starts.unsqueeze(1)) & (positions < (starts + width).unsqueeze(1))


class RandomWindowDataset:
    """Random causal contexts and future patches whose offsets mark patch ends."""
    def __init__(self, assets: list[AssetArrays], end_exclusive: str, context_minutes: int,
                 target_end_offsets: list[int], target_minutes: int = 10, seed: int = 7,
                 virtual_length: int = 2_000_000, start_inclusive: str | None = None):
        self.assets = assets
        self.context_minutes = int(context_minutes)
        self.target_end_offsets = [int(item) for item in target_end_offsets]
        self.target_minutes = int(target_minutes)
        if (not self.target_end_offsets
                or sorted(set(self.target_end_offsets)) != self.target_end_offsets
                or self.target_end_offsets[0] < self.target_minutes):
            raise ValueError("target end offsets must be unique, increasing, and at least one target patch")
        self.seed = int(seed)
        self.virtual_length = int(virtual_length)
        self.index_offset = 0
        # When a validation boundary is supplied, keep the entire context as
        # well as every target inside that segment. This makes train and
        # validation raw windows disjoint, not merely their future targets.
        self.starts = [
            self.context_minutes
            if start_inclusive is None
            else max(
                self.context_minutes,
                asset.end_index(start_inclusive) + self.context_minutes,
            )
            for asset in assets
        ]
        self.ends = [asset.end_index(end_exclusive) for asset in assets]
        self.max_offset = max(self.target_end_offsets)
        self.valid_counts = [max(0, end - self.max_offset - start + 1)
                             for start, end in zip(self.starts, self.ends, strict=True)]
        if not sum(self.valid_counts):
            raise ValueError("no valid pretraining windows")
        self.probability = np.asarray(self.valid_counts, dtype=float) / sum(self.valid_counts)

    def __len__(self) -> int:
        return self.virtual_length

    def __getitem__(self, index: int):
        import torch
        global_index = self.index_offset + int(index)
        rng = np.random.default_rng(self.seed + global_index * 104729)
        asset_index = int(rng.choice(len(self.assets), p=self.probability))
        asset = self.assets[asset_index]
        anchor = int(rng.integers(self.starts[asset_index], self.ends[asset_index] - self.max_offset + 1))
        context = np.asarray(asset.values[anchor - self.context_minutes:anchor]).copy()
        targets = []
        for offset in self.target_end_offsets:
            end = anchor + offset
            targets.append(np.asarray(asset.values[end - self.target_minutes:end]).copy())
        return (torch.from_numpy(context), torch.from_numpy(np.stack(targets)),
                torch.tensor(asset.asset_id, dtype=torch.long))


def decision_indices(asset: AssetArrays, start: str, end_exclusive: str, step_minutes: int,
                     context_minutes: int, horizon_minutes: int) -> np.ndarray:
    minute_ns = 60 * 1_000_000_000
    # An index identifies the one-minute bar that has just closed. Therefore
    # its actionable decision time is open_time + one minute.
    start_ns = pd.Timestamp(start).value
    end_ns = pd.Timestamp(end_exclusive).value
    first = max(int(np.searchsorted(asset.times, start_ns - minute_ns, side="left")), context_minutes - 1)
    last = min(int(np.searchsorted(asset.times, end_ns - minute_ns, side="left")),
               len(asset.values) - horizon_minutes)
    candidates = np.arange(first, last, dtype=np.int64)
    decision_time = asset.times[candidates] + minute_ns
    in_range = (decision_time >= start_ns) & (decision_time < end_ns)
    epoch_minutes = decision_time // minute_ns
    return candidates[in_range & (epoch_minutes % step_minutes == 0)]
