from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from .execution import apply_signal_gap


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(frozen=True)
class SecondNormalConfig:
    strategy_id: str = "BTC_10min_SECOND_3600_20"
    lookback_sec: int = 3600
    horizon_sec: int = 600
    signal_gap_sec: int = 0
    tail_pct: float = 0.20
    second_filter: str = "none"
    amount: float = 5.0
    label: str = "second_normal"


@dataclass(frozen=True)
class SecondChipConfig:
    strategy_id: str = "BTC_10min_SECOND_CHIP_3600_20"
    lookback_sec: int = 3600
    horizon_sec: int = 600
    signal_gap_sec: int = 0
    target_share: float = 0.20
    bin_mode: str = "fixed"
    bin_size: float = 20.0
    bin_pct: float = 0.0003
    break_pct: float = 0.0023
    direction_filter: str = "breakout_up_only"
    chip_filter: str = "none"
    signal_hold_sec: int = 60
    amount: float = 5.0
    label: str = "second_chip"


def settle_signal(
    *,
    bars: pd.DataFrame,
    idx: int,
    strategy_id: str,
    model_type: str,
    signal: str,
    horizon_sec: int,
    amount: float,
    extra: dict[str, Any] | None = None,
) -> dict:
    close = bars["close"].to_numpy(float)
    times = bars.index
    entry = float(close[idx])
    settle = float(close[idx + horizon_sec])
    row = {
        "strategy_id": strategy_id,
        "model_type": model_type,
        "idx": int(idx),
        "time": times[idx],
        "signal": signal,
        "entry": entry,
        "settle_time": times[idx + horizon_sec],
        "settle": settle,
        "won": bool(settle > entry if signal == "UP" else settle < entry),
        "horizon_sec": int(horizon_sec),
        "amount": float(amount),
    }
    if extra:
        row.update(extra)
    return row


def generate_normal_signals(
    bars: pd.DataFrame,
    cfg: SecondNormalConfig,
    *,
    apply_config_gap: bool = True,
) -> list[dict]:
    close = bars["close"].to_numpy(float)
    if len(close) <= cfg.lookback_sec + cfg.horizon_sec:
        return []
    logp = np.log(close)
    lr = np.diff(logp, prepend=np.nan)
    series = pd.Series(lr, index=bars.index)
    min_periods = max(60, min(cfg.lookback_sec, cfg.lookback_sec // 4))
    mu = series.rolling(cfg.lookback_sec, min_periods=min_periods).mean().to_numpy()
    sigma = series.rolling(cfg.lookback_sec, min_periods=min_periods).std(ddof=1).to_numpy()
    filter_state = _second_filter_state(bars, cfg.lookback_sec, cfg.second_filter)

    rows: list[dict] = []
    threshold_hi = 1.0 - cfg.tail_pct
    for i in range(cfg.lookback_sec, len(close) - cfg.horizon_sec):
        if not np.isfinite(mu[i]) or not np.isfinite(sigma[i]) or sigma[i] < 1e-12:
            continue
        z = float(cfg.horizon_sec * mu[i] / (math.sqrt(cfg.horizon_sec) * sigma[i]))
        p_up = normal_cdf(z)
        signal = None
        if p_up >= threshold_hi:
            signal = "DOWN"
        elif p_up <= cfg.tail_pct:
            signal = "UP"
        if not signal:
            continue
        ok, reason, vol_rank, flow_ratio = _second_filter_allows(
            cfg.second_filter, signal, filter_state, i
        )
        if not ok:
            continue
        rows.append(
            settle_signal(
                bars=bars,
                idx=i,
                strategy_id=cfg.strategy_id,
                model_type="second_normal",
                signal=signal,
                horizon_sec=cfg.horizon_sec,
                amount=cfg.amount,
                extra={
                    "p_up": round(float(p_up), 6),
                    "z_score": round(float(z), 6),
                    "tail_pct": float(cfg.tail_pct),
                    "lookback_sec": int(cfg.lookback_sec),
                    "filter": cfg.second_filter,
                    "filter_reason": reason,
                    "vol_rank_60s": None
                    if vol_rank is None or not np.isfinite(vol_rank)
                    else round(float(vol_rank), 6),
                    "flow_ratio_60s": None
                    if flow_ratio is None or not np.isfinite(flow_ratio)
                    else round(float(flow_ratio), 6),
                },
            )
        )
    return apply_signal_gap(rows, cfg.signal_gap_sec) if apply_config_gap else rows


def generate_chip_signals(
    bars: pd.DataFrame,
    cfg: SecondChipConfig,
    *,
    apply_config_gap: bool = True,
) -> list[dict]:
    if cfg.bin_mode.lower() != "fixed":
        raise ValueError("second chip backtest currently supports fixed bin_mode only")
    close = bars["close"].to_numpy(float)
    volume = bars["volume"].to_numpy(float)
    buy_qty = bars["buy_qty"].to_numpy(float)
    sell_qty = bars["sell_qty"].to_numpy(float)
    if len(close) <= cfg.lookback_sec + cfg.horizon_sec:
        return []

    features = build_chip_features(close, volume, cfg.lookback_sec, cfg.bin_size, cfg.target_share)
    flow300 = pd.Series(buy_qty - sell_qty).rolling(300, min_periods=1).sum().to_numpy()
    states = _chip_states(close, features, cfg.break_pct)
    rows: list[dict] = []
    last_transition_idx = -10**12
    last_transition_key = None
    for i in range(cfg.lookback_sec, len(close) - cfg.horizon_sec):
        state = states[i]
        prev_state = states[i - 1] if i > 0 else "unknown"
        signal, breakout, distance_pct = _transition_signal(
            state, prev_state, close[i], features, i
        )
        if not signal:
            continue
        transition_key = (i, signal, breakout)
        if i - last_transition_idx <= cfg.signal_hold_sec and transition_key == last_transition_key:
            continue
        last_transition_idx = i
        last_transition_key = transition_key
        if not _direction_allowed(cfg.direction_filter, breakout):
            continue
        zone = _zone_at(features, i)
        filter_ok, filter_reason = _chip_filter_allows(
            cfg.chip_filter, signal, zone, flow300[i]
        )
        if not filter_ok:
            continue
        rows.append(
            settle_signal(
                bars=bars,
                idx=i,
                strategy_id=cfg.strategy_id,
                model_type="second_chip",
                signal=signal,
                horizon_sec=cfg.horizon_sec,
                amount=cfg.amount,
                extra={
                    "breakout": breakout,
                    "lookback_sec": int(cfg.lookback_sec),
                    "target_share": float(cfg.target_share),
                    "bin_size": float(cfg.bin_size),
                    "break_pct": float(cfg.break_pct),
                    "direction_filter": cfg.direction_filter,
                    "chip_filter": cfg.chip_filter,
                    "filter_reason": filter_reason,
                    "distance_pct": round(float(distance_pct), 8),
                    "flow300": round(float(flow300[i]), 8),
                    "poc": round(float(zone["poc"]), 4),
                    "zone_low": round(float(zone["low"]), 4),
                    "zone_high": round(float(zone["high"]), 4),
                    "zone_share": round(float(zone["share"]), 6),
                    "zone_volume_share": round(float(zone["volume_share"]), 6),
                    "zone_width_bins": int(zone["width_bins"]),
                },
            )
        )
    return apply_signal_gap(rows, cfg.signal_gap_sec) if apply_config_gap else rows


def build_chip_features(
    close: np.ndarray,
    volume: np.ndarray,
    lookback: int,
    bin_size: float,
    target_share: float,
) -> dict[str, np.ndarray]:
    bins = np.rint(close / float(bin_size)).astype(int)
    offset = int(bins.min())
    size = int(bins.max() - offset + 1)
    counts = np.zeros(size, dtype=float)
    vols = np.zeros(size, dtype=float)
    out = {
        name: np.full(len(close), np.nan)
        for name in ("low", "high", "share", "volume_share", "poc", "width_bins")
    }
    for i, bin_raw in enumerate(bins):
        bucket = bin_raw - offset
        counts[bucket] += 1.0
        vols[bucket] += volume[i]
        if i >= lookback:
            old_bucket = bins[i - lookback] - offset
            counts[old_bucket] -= 1.0
            vols[old_bucket] -= volume[i - lookback]
        if i < lookback:
            continue
        total = counts.sum()
        total_vol = vols.sum()
        poc = int(np.argmax(counts))
        lo = hi = poc
        zone_count = counts[poc]
        while zone_count / max(total, 1e-12) < target_share:
            left = counts[lo - 1] if lo > 0 else -1
            right = counts[hi + 1] if hi + 1 < size else -1
            if left < 0 and right < 0:
                break
            if right > left:
                hi += 1
                zone_count += counts[hi]
            else:
                lo -= 1
                zone_count += counts[lo]
        zone_slice = slice(lo, hi + 1)
        out["low"][i] = (lo + offset) * bin_size
        out["high"][i] = (hi + offset) * bin_size
        out["poc"][i] = (poc + offset) * bin_size
        out["share"][i] = counts[zone_slice].sum() / max(total, 1e-12)
        out["volume_share"][i] = (
            vols[zone_slice].sum() / max(total_vol, 1e-12) if total_vol > 0 else 0.0
        )
        out["width_bins"][i] = hi - lo + 1
    return out


def prod_configs_to_second_configs(config_map: dict, amount_map: dict | None = None) -> list:
    amount_map = amount_map or {}
    configs = []
    for strategy_id, cfg in config_map.items():
        if not cfg or cfg.get("enabled", True) is False:
            continue
        amount = _amount_for(strategy_id, cfg, amount_map)
        model_type = cfg.get("model_type")
        if model_type == "second_normal":
            configs.append(
                SecondNormalConfig(
                    strategy_id=strategy_id,
                    lookback_sec=int(cfg.get("second_lookback_sec", 1800)),
                    horizon_sec=int(cfg.get("second_horizon_sec", 600)),
                    signal_gap_sec=int(
                        cfg.get("second_signal_gap_sec", cfg.get("second_min_gap_sec", 0))
                    ),
                    tail_pct=float(cfg.get("second_tail_pct", 0.20)),
                    second_filter=str(cfg.get("second_filter", "none")).lower(),
                    amount=amount,
                    label=str(cfg.get("model_label", strategy_id)),
                )
            )
        elif model_type == "second_chip":
            configs.append(
                SecondChipConfig(
                    strategy_id=strategy_id,
                    lookback_sec=int(cfg.get("second_chip_lookback_sec", 3600)),
                    horizon_sec=int(cfg.get("second_chip_horizon_sec", 600)),
                    signal_gap_sec=int(
                        cfg.get(
                            "second_chip_signal_gap_sec",
                            cfg.get("second_chip_min_gap_sec", 0),
                        )
                    ),
                    target_share=float(cfg.get("second_chip_target_share", 0.20)),
                    bin_mode=str(cfg.get("second_chip_bin_mode", "fixed")).lower(),
                    bin_size=float(cfg.get("second_chip_bin_size", 20.0)),
                    bin_pct=float(cfg.get("second_chip_bin_pct", 0.0003)),
                    break_pct=float(cfg.get("second_chip_break_pct", 0.0023)),
                    direction_filter=str(
                        cfg.get("second_chip_direction_filter", "breakout_up_only")
                    ).lower(),
                    chip_filter=str(cfg.get("second_chip_filter", "none")).lower(),
                    signal_hold_sec=int(cfg.get("second_chip_signal_hold_sec", 60)),
                    amount=amount,
                    label=str(cfg.get("model_label", strategy_id)),
                )
            )
    return configs


def _amount_for(strategy_id: str, cfg: dict, amount_map: dict) -> float:
    for source in (cfg, amount_map):
        value = source.get(strategy_id) if source is amount_map else source.get("amount")
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            pass
    return 5.0


def _second_filter_state(bars: pd.DataFrame, lookback: int, filter_name: str) -> dict:
    if str(filter_name).lower() in ("", "none", "off", "false"):
        return {}
    volume = bars["volume"].astype(float)
    vol60 = volume.rolling(60, min_periods=1).sum()
    rank_window = max(int(lookback), 1800)
    vol_rank = vol60.rolling(rank_window, min_periods=30).apply(
        lambda values: float((values <= values[-1]).mean()),
        raw=True,
    )
    buy60 = bars["buy_qty"].astype(float).rolling(60, min_periods=1).sum()
    sell60 = bars["sell_qty"].astype(float).rolling(60, min_periods=1).sum()
    flow_ratio = buy60 / sell60.clip(lower=1e-12)
    return {"vol_rank": vol_rank.to_numpy(float), "flow_ratio": flow_ratio.to_numpy(float)}


def _second_filter_allows(filter_name: str, signal: str, state: dict, idx: int):
    name = str(filter_name or "none").lower()
    if name in ("", "none", "off", "false"):
        return True, "disabled", None, None
    vol_rank = state.get("vol_rank", [np.nan])[idx]
    flow_ratio = state.get("flow_ratio", [np.nan])[idx]
    if name == "vol_high":
        ok = np.isfinite(vol_rank) and vol_rank >= 0.6
        return bool(ok), "vol_high" if ok else "vol_not_high", vol_rank, flow_ratio
    if name == "vol_not_high":
        ok = not np.isfinite(vol_rank) or vol_rank <= 0.8
        return bool(ok), "vol_not_high" if ok else "vol_too_high", vol_rank, flow_ratio
    if name in ("flow_align", "flow_strong_align", "flow_align_vol_not_high"):
        up_min = 1.2 if name == "flow_strong_align" else 1.05
        down_max = 0.8 if name == "flow_strong_align" else 0.95
        flow_ok = flow_ratio >= up_min if signal == "UP" else flow_ratio <= down_max
        vol_ok = True
        if name == "flow_align_vol_not_high":
            vol_ok = not np.isfinite(vol_rank) or vol_rank <= 0.8
        ok = bool(flow_ok and vol_ok)
        return ok, "flow_align" if ok else "flow_not_aligned", vol_rank, flow_ratio
    return False, f"unknown_second_filter_{name}", vol_rank, flow_ratio


def _chip_states(close: np.ndarray, features: dict[str, np.ndarray], break_pct: float) -> list[str]:
    states = ["unknown"] * len(close)
    low = features["low"]
    high = features["high"]
    for i in range(len(close)):
        if not np.isfinite(low[i]) or not np.isfinite(high[i]):
            continue
        upper = high[i] * (1.0 + break_pct)
        lower = low[i] * (1.0 - break_pct)
        if close[i] > upper:
            states[i] = "above"
        elif close[i] < lower:
            states[i] = "below"
        else:
            states[i] = "inside"
    return states


def _transition_signal(state: str, prev_state: str, price: float, features: dict, idx: int):
    if state == "above" and prev_state != "above":
        distance = price / max(features["high"][idx], 1e-12) - 1.0
        return "DOWN", "UP", distance
    if state == "below" and prev_state != "below":
        distance = features["low"][idx] / max(price, 1e-12) - 1.0
        return "UP", "DOWN", distance
    return None, None, 0.0


def _direction_allowed(direction_filter: str, breakout: str) -> bool:
    name = str(direction_filter or "all").lower()
    if name in ("", "none", "all"):
        return True
    if name == "breakout_up_only":
        return breakout == "UP"
    if name == "breakout_down_only":
        return breakout == "DOWN"
    return True


def _chip_filter_allows(chip_filter: str, signal: str, zone: dict, flow300: float):
    name = str(chip_filter or "none").lower()
    if name in ("", "none", "off", "false"):
        return True, "disabled"
    if name == "width_lte_3":
        return int(zone.get("width_bins", 999999)) <= 3, "width_lte_3"
    if name == "width_lte_5":
        return int(zone.get("width_bins", 999999)) <= 5, "width_lte_5"
    if name == "flow_reversal":
        if not np.isfinite(flow300):
            return False, "flow_missing"
        ok = (signal == "UP" and flow300 < 0) or (signal == "DOWN" and flow300 > 0)
        return bool(ok), "flow_reversal"
    return False, f"unknown_chip_filter_{name}"


def _zone_at(features: dict[str, np.ndarray], idx: int) -> dict[str, float]:
    return {
        "low": float(features["low"][idx]),
        "high": float(features["high"][idx]),
        "poc": float(features["poc"][idx]),
        "share": float(features["share"][idx]),
        "volume_share": float(features["volume_share"][idx]),
        "width_bins": int(features["width_bins"][idx]),
    }
