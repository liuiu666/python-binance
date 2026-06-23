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
    incident_filter: dict[str, Any] | None = None
    amount: float = 5.0
    label: str = "second_normal"


@dataclass(frozen=True)
class SecondNormalVwConfirmConfig:
    strategy_id: str = "BTC_10min_SECOND_NORMAL_VW_STABLE"
    lookback_sec: int = 2700
    horizon_sec: int = 600
    signal_gap_sec: int = 600
    tail_pct: float = 0.20
    eta_target_bps: float = 2.0
    eta_max_wait_sec: int = 45
    up_reversal_confirm_bps: float = 0.0
    up_reversal_confirm_max_sec: int = 20
    incident_filter: dict[str, Any] | None = None
    amount: float = 5.0
    label: str = "second_normal_vw_confirm"


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
    incident_filter: dict[str, Any] | None = None
    amount: float = 5.0
    label: str = "second_chip"


@dataclass(frozen=True)
class SecondTrendPullbackDownConfig:
    strategy_id: str = "BTC_10min_SECOND_TREND_DOWN_7200_04_300_10"
    regime_lookback_sec: int = 7200
    regime_alt_lookback_sec: int = 5400
    regime_drop_pct: float = 0.004
    regime_alt_drop_pct: float = 0.003
    max_pos_pct: float = 0.6
    max_entry_pos_pct: float = 0.4
    max_recent_ret_pct: float = 0.001
    pullback_sec: int = 300
    pullback_pct: float = 0.001
    horizon_sec: int = 600
    signal_gap_sec: int = 600
    amount: float = 15.0
    suppress_reversal_in_regime: bool = True
    label: str = "second_trend_pullback_down"


@dataclass(frozen=True)
class SecondRangeBreakoutConfig:
    strategy_id: str = "BTC_10min_SECOND_RANGE_BREAKOUT_1800_Z20"
    lookback_sec: int = 1800
    horizon_sec: int = 600
    signal_gap_sec: int = 600
    z_entry: float = 2.0
    pre_slope_sec: int = 300
    min_slope_bps: float = 8.0
    min_flow_imbalance: float = 0.12
    min_volume_ratio: float = 0.45
    min_volatility_ratio: float = 0.55
    max_age_beyond_sec: int = 180
    amount: float = 5.0
    label: str = "second_range_breakout"


@dataclass(frozen=True)
class SecondRangeBreakoutConfirmConfig:
    strategy_id: str = "BTC_10min_SECOND_RANGE_BREAKOUT_CONFIRM"
    lookback_sec: int = 1800
    horizon_sec: int = 600
    signal_gap_sec: int = 600
    z_entry: float = 2.0
    confirm_sec: int = 120
    hold_z: float = 1.0
    min_hold_ratio: float = 0.70
    pre_slope_sec: int = 300
    confirm_slope_sec: int = 120
    min_pre_slope_bps: float = 8.0
    min_confirm_slope_bps: float = 2.0
    min_flow_imbalance: float = 0.12
    min_confirm_flow_imbalance: float = 0.08
    min_volume_ratio: float = 0.45
    min_volatility_ratio: float = 0.55
    max_age_beyond_sec: int = 180
    amount: float = 5.0
    label: str = "second_range_breakout_confirm"


@dataclass(frozen=True)
class SecondNormalDirection3mConfig:
    strategy_id: str = "BTC_10min_SECOND_NORMAL_DIRECTION_600_C180"
    lookback_sec: int = 600
    horizon_sec: int = 600
    confirm_sec: int = 180
    signal_gap_sec: int = 600
    prob_edge: float = 0.58
    min_sigma_bps: float = 8.0
    max_sigma_bps: float = 35.0
    min_range_bps: float = 12.0
    max_range_bps: float = 80.0
    min_confirm_move_bps: float = 3.0
    min_confirm_slope_bps: float = 2.0
    min_confirm_flow_imbalance: float = 0.05
    amount: float = 5.0
    label: str = "second_normal_direction_3m_confirm"


@dataclass(frozen=True)
class SecondNormalMultiframeConfig:
    strategy_id: str = "BTC_10min_SECOND_NORMAL_MULTIFRAME"
    short_window_sec: int = 600
    mid_window_sec: int = 1800
    long_window_sec: int = 3600
    horizon_sec: int = 600
    signal_gap_sec: int = 600
    min_observed_pct: float = 80.0
    min_width2_bps: float = 8.0
    max_width2_bps: float = 80.0
    lower_z: float = -0.5
    upper_z: float = 1.0
    lower_big_mode: str = "align_up"
    upper_big_mode: str = "any"
    core_enabled: bool = True
    core_z_abs_max: float = 1.0
    lower_min_slope1_bps: float = 0.0
    lower_min_flow1: float = 0.0
    upper_min_slope1_bps: float = 0.0
    upper_min_flow1: float = 0.05
    core_min_slope1_bps: float = 0.0
    core_min_flow1: float = 0.05
    amount: float = 5.0
    label: str = "second_normal_multiframe"


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


def generate_range_breakout_signals(
    bars: pd.DataFrame,
    cfg: SecondRangeBreakoutConfig,
    *,
    apply_config_gap: bool = True,
) -> list[dict]:
    """Predict 10m direction when price leaves a rolling normal range.

    This is a continuation strategy: a break above the old +z band predicts UP,
    and a break below the old -z band predicts DOWN. Filters are causal and use
    only the pre-breakout slope, flow, volume, and volatility.
    """

    close = bars["close"].to_numpy(float)
    volume = bars["volume"].to_numpy(float)
    buy_qty = bars["buy_qty"].to_numpy(float)
    sell_qty = bars["sell_qty"].to_numpy(float)
    if len(close) <= cfg.lookback_sec + cfg.horizon_sec:
        return []

    close_s = pd.Series(close, index=bars.index)
    ret = np.diff(np.log(close), prepend=np.nan)
    abs_ret_s = pd.Series(np.abs(ret), index=bars.index)
    mu = close_s.rolling(cfg.lookback_sec, min_periods=max(300, cfg.lookback_sec // 3)).mean().to_numpy(float)
    sigma = close_s.rolling(cfg.lookback_sec, min_periods=max(300, cfg.lookback_sec // 3)).std(ddof=1).to_numpy(float)

    pre = max(30, int(cfg.pre_slope_sec))
    vol_s = pd.Series(volume, index=bars.index)
    vol_pre = vol_s.rolling(pre, min_periods=max(10, pre // 3)).sum().to_numpy(float)
    vol_ref = vol_s.rolling(cfg.lookback_sec, min_periods=max(300, cfg.lookback_sec // 3)).mean().to_numpy(float) * pre
    vol_ratio = vol_pre / np.maximum(vol_ref, 1e-12)

    abs_pre = abs_ret_s.rolling(pre, min_periods=max(10, pre // 3)).mean().to_numpy(float)
    abs_ref = abs_ret_s.rolling(cfg.lookback_sec, min_periods=max(300, cfg.lookback_sec // 3)).mean().to_numpy(float)
    volatility_ratio = abs_pre / np.maximum(abs_ref, 1e-12)

    buy_pre = pd.Series(buy_qty, index=bars.index).rolling(pre, min_periods=1).sum().to_numpy(float)
    sell_pre = pd.Series(sell_qty, index=bars.index).rolling(pre, min_periods=1).sum().to_numpy(float)
    flow_imbalance = (buy_pre - sell_pre) / np.maximum(buy_pre + sell_pre, 1e-12)

    z = (close - mu) / np.maximum(sigma, 1e-12)
    beyond = np.abs(z) >= float(cfg.z_entry)
    beyond_age = np.zeros(len(close), dtype=int)
    for i in range(1, len(close)):
        beyond_age[i] = beyond_age[i - 1] + 1 if beyond[i] and np.sign(z[i]) == np.sign(z[i - 1]) else (1 if beyond[i] else 0)

    rows: list[dict] = []
    last_signal_idx = -10**12
    for i in range(max(cfg.lookback_sec, pre), len(close) - cfg.horizon_sec):
        if apply_config_gap and cfg.signal_gap_sec > 0 and i - last_signal_idx < cfg.signal_gap_sec:
            continue
        if not np.isfinite(z[i]) or abs(z[i]) < float(cfg.z_entry):
            continue
        side = 1.0 if z[i] > 0 else -1.0
        signal = "UP" if side > 0 else "DOWN"
        if cfg.max_age_beyond_sec > 0 and beyond_age[i] > int(cfg.max_age_beyond_sec):
            continue
        slope_bps = (close[i] / close[i - pre] - 1.0) * 10000.0
        aligned_slope = side * slope_bps
        aligned_flow = side * flow_imbalance[i]
        if aligned_slope < float(cfg.min_slope_bps):
            continue
        if aligned_flow < float(cfg.min_flow_imbalance):
            continue
        if not np.isfinite(vol_ratio[i]) or vol_ratio[i] < float(cfg.min_volume_ratio):
            continue
        if not np.isfinite(volatility_ratio[i]) or volatility_ratio[i] < float(cfg.min_volatility_ratio):
            continue
        last_signal_idx = i
        rows.append(
            settle_signal(
                bars=bars,
                idx=i,
                strategy_id=cfg.strategy_id,
                model_type="second_range_breakout",
                signal=signal,
                horizon_sec=cfg.horizon_sec,
                amount=cfg.amount,
                extra={
                    "range_z": round(float(z[i]), 6),
                    "lookback_sec": int(cfg.lookback_sec),
                    "z_entry": float(cfg.z_entry),
                    "pre_slope_sec": int(pre),
                    "pre_slope_bps": round(float(slope_bps), 6),
                    "aligned_slope_bps": round(float(aligned_slope), 6),
                    "pre_flow_imbalance": round(float(flow_imbalance[i]), 6),
                    "aligned_flow_imbalance": round(float(aligned_flow), 6),
                    "pre_volume_ratio": round(float(vol_ratio[i]), 6),
                    "pre_volatility_ratio": round(float(volatility_ratio[i]), 6),
                    "beyond_age_sec": int(beyond_age[i]),
                    "range_mean": round(float(mu[i]), 6),
                    "range_sigma_bps": round(float(sigma[i] / close[i] * 10000.0), 6),
                },
            )
        )
    return apply_signal_gap(rows, cfg.signal_gap_sec) if apply_config_gap else rows


def generate_range_breakout_confirm_signals(
    bars: pd.DataFrame,
    cfg: SecondRangeBreakoutConfirmConfig,
    *,
    apply_config_gap: bool = True,
) -> list[dict]:
    """Predict 10m direction after a breakout confirms outside the old range."""

    close = bars["close"].to_numpy(float)
    volume = bars["volume"].to_numpy(float)
    buy_qty = bars["buy_qty"].to_numpy(float)
    sell_qty = bars["sell_qty"].to_numpy(float)
    if len(close) <= cfg.lookback_sec + cfg.confirm_sec + cfg.horizon_sec:
        return []

    close_s = pd.Series(close, index=bars.index)
    ret = np.diff(np.log(close), prepend=np.nan)
    abs_ret_s = pd.Series(np.abs(ret), index=bars.index)
    min_periods = max(300, cfg.lookback_sec // 3)
    mu = close_s.rolling(cfg.lookback_sec, min_periods=min_periods).mean().to_numpy(float)
    sigma = close_s.rolling(cfg.lookback_sec, min_periods=min_periods).std(ddof=1).to_numpy(float)
    z = (close - mu) / np.maximum(sigma, 1e-12)

    pre = max(30, int(cfg.pre_slope_sec))
    confirm = max(10, int(cfg.confirm_sec))
    confirm_slope = max(10, int(cfg.confirm_slope_sec))

    vol_s = pd.Series(volume, index=bars.index)
    vol_pre = vol_s.rolling(pre, min_periods=max(10, pre // 3)).sum().to_numpy(float)
    vol_ref = vol_s.rolling(cfg.lookback_sec, min_periods=min_periods).mean().to_numpy(float) * pre
    vol_ratio = vol_pre / np.maximum(vol_ref, 1e-12)

    abs_pre = abs_ret_s.rolling(pre, min_periods=max(10, pre // 3)).mean().to_numpy(float)
    abs_ref = abs_ret_s.rolling(cfg.lookback_sec, min_periods=min_periods).mean().to_numpy(float)
    volatility_ratio = abs_pre / np.maximum(abs_ref, 1e-12)

    buy_pre = pd.Series(buy_qty, index=bars.index).rolling(pre, min_periods=1).sum().to_numpy(float)
    sell_pre = pd.Series(sell_qty, index=bars.index).rolling(pre, min_periods=1).sum().to_numpy(float)
    flow_imbalance = (buy_pre - sell_pre) / np.maximum(buy_pre + sell_pre, 1e-12)

    buy_conf = pd.Series(buy_qty, index=bars.index).rolling(confirm_slope, min_periods=1).sum().to_numpy(float)
    sell_conf = pd.Series(sell_qty, index=bars.index).rolling(confirm_slope, min_periods=1).sum().to_numpy(float)
    confirm_flow_imbalance = (buy_conf - sell_conf) / np.maximum(buy_conf + sell_conf, 1e-12)

    beyond = np.abs(z) >= float(cfg.z_entry)
    beyond_age = np.zeros(len(close), dtype=int)
    for i in range(1, len(close)):
        beyond_age[i] = beyond_age[i - 1] + 1 if beyond[i] and np.sign(z[i]) == np.sign(z[i - 1]) else (1 if beyond[i] else 0)

    rows: list[dict] = []
    last_signal_idx = -10**12
    limit = len(close) - cfg.horizon_sec
    for i in range(max(cfg.lookback_sec, pre), limit - confirm):
        j = i + confirm
        if apply_config_gap and cfg.signal_gap_sec > 0 and j - last_signal_idx < cfg.signal_gap_sec:
            continue
        if not np.isfinite(z[i]) or abs(z[i]) < float(cfg.z_entry):
            continue
        side = 1.0 if z[i] > 0 else -1.0
        signal = "UP" if side > 0 else "DOWN"
        if cfg.max_age_beyond_sec > 0 and beyond_age[i] > int(cfg.max_age_beyond_sec):
            continue

        pre_slope_bps = (close[i] / close[i - pre] - 1.0) * 10000.0
        aligned_pre_slope = side * pre_slope_bps
        aligned_pre_flow = side * flow_imbalance[i]
        if aligned_pre_slope < float(cfg.min_pre_slope_bps):
            continue
        if aligned_pre_flow < float(cfg.min_flow_imbalance):
            continue
        if not np.isfinite(vol_ratio[i]) or vol_ratio[i] < float(cfg.min_volume_ratio):
            continue
        if not np.isfinite(volatility_ratio[i]) or volatility_ratio[i] < float(cfg.min_volatility_ratio):
            continue

        old_mu = float(mu[i])
        old_sigma = float(sigma[i])
        if not np.isfinite(old_mu) or not np.isfinite(old_sigma) or old_sigma <= 0:
            continue
        confirm_z_path = side * ((close[i + 1 : j + 1] - old_mu) / old_sigma)
        hold_ratio = float(np.mean(confirm_z_path >= float(cfg.hold_z))) if len(confirm_z_path) else 0.0
        if hold_ratio < float(cfg.min_hold_ratio):
            continue

        confirm_slope_bps = (close[j] / close[max(i, j - confirm_slope)] - 1.0) * 10000.0
        aligned_confirm_slope = side * confirm_slope_bps
        aligned_confirm_flow = side * confirm_flow_imbalance[j]
        if aligned_confirm_slope < float(cfg.min_confirm_slope_bps):
            continue
        if aligned_confirm_flow < float(cfg.min_confirm_flow_imbalance):
            continue

        last_signal_idx = j
        rows.append(
            settle_signal(
                bars=bars,
                idx=j,
                strategy_id=cfg.strategy_id,
                model_type="second_range_breakout_confirm",
                signal=signal,
                horizon_sec=cfg.horizon_sec,
                amount=cfg.amount,
                extra={
                    "break_time": bars.index[i],
                    "break_z": round(float(z[i]), 6),
                    "confirm_z": round(float(z[j]), 6),
                    "hold_ratio": round(float(hold_ratio), 6),
                    "lookback_sec": int(cfg.lookback_sec),
                    "z_entry": float(cfg.z_entry),
                    "confirm_sec": int(confirm),
                    "hold_z": float(cfg.hold_z),
                    "pre_slope_bps": round(float(pre_slope_bps), 6),
                    "aligned_pre_slope_bps": round(float(aligned_pre_slope), 6),
                    "confirm_slope_bps": round(float(confirm_slope_bps), 6),
                    "aligned_confirm_slope_bps": round(float(aligned_confirm_slope), 6),
                    "pre_flow_imbalance": round(float(flow_imbalance[i]), 6),
                    "confirm_flow_imbalance": round(float(confirm_flow_imbalance[j]), 6),
                    "aligned_confirm_flow_imbalance": round(float(aligned_confirm_flow), 6),
                    "pre_volume_ratio": round(float(vol_ratio[i]), 6),
                    "pre_volatility_ratio": round(float(volatility_ratio[i]), 6),
                    "beyond_age_sec": int(beyond_age[i]),
                    "range_mean": round(float(old_mu), 6),
                    "range_sigma_bps": round(float(old_sigma / close[i] * 10000.0), 6),
                },
            )
        )
    return apply_signal_gap(rows, cfg.signal_gap_sec) if apply_config_gap else rows


def generate_normal_direction_3m_signals(
    bars: pd.DataFrame,
    cfg: SecondNormalDirection3mConfig,
    *,
    apply_config_gap: bool = True,
) -> list[dict]:
    """Predict 10m UP/DOWN from a rolling 10m normal distribution, then wait up to 3m for confirmation."""

    close = bars["close"].to_numpy(float)
    high = bars["high"].to_numpy(float) if "high" in bars else close
    low = bars["low"].to_numpy(float) if "low" in bars else close
    buy_qty = bars["buy_qty"].to_numpy(float)
    sell_qty = bars["sell_qty"].to_numpy(float)
    if len(close) <= cfg.lookback_sec + cfg.confirm_sec + cfg.horizon_sec:
        return []

    logp = np.log(close)
    lr = np.diff(logp, prepend=np.nan)
    lr_s = pd.Series(lr, index=bars.index)
    close_s = pd.Series(close, index=bars.index)
    min_periods = max(120, cfg.lookback_sec // 2)
    mu = lr_s.rolling(cfg.lookback_sec, min_periods=min_periods).mean().to_numpy(float)
    sigma = lr_s.rolling(cfg.lookback_sec, min_periods=min_periods).std(ddof=1).to_numpy(float)
    roll_max = close_s.rolling(cfg.lookback_sec, min_periods=min_periods).max().to_numpy(float)
    roll_min = close_s.rolling(cfg.lookback_sec, min_periods=min_periods).min().to_numpy(float)

    rows: list[dict] = []
    last_signal_idx = -10**12
    end = len(close) - cfg.confirm_sec - cfg.horizon_sec
    for i in range(cfg.lookback_sec, end):
        if apply_config_gap and cfg.signal_gap_sec > 0 and i - last_signal_idx < cfg.signal_gap_sec:
            continue
        if not np.isfinite(mu[i]) or not np.isfinite(sigma[i]) or sigma[i] <= 1e-12:
            continue
        range_bps = (roll_max[i] - roll_min[i]) / close[i] * 10000.0
        sigma10_bps = math.sqrt(cfg.horizon_sec) * sigma[i] * 10000.0
        if not np.isfinite(range_bps) or range_bps < cfg.min_range_bps or range_bps > cfg.max_range_bps:
            continue
        if sigma10_bps < cfg.min_sigma_bps or sigma10_bps > cfg.max_sigma_bps:
            continue

        z = float(cfg.horizon_sec * mu[i] / (math.sqrt(cfg.horizon_sec) * sigma[i]))
        p_up = normal_cdf(z)
        signal = "UP" if p_up >= cfg.prob_edge else "DOWN" if p_up <= 1.0 - cfg.prob_edge else None
        if not signal:
            continue
        side = 1.0 if signal == "UP" else -1.0

        confirmed = None
        confirm_extra = None
        for j in range(i + 1, min(i + cfg.confirm_sec, len(close) - cfg.horizon_sec - 1) + 1):
            confirm_move_bps = side * (close[j] / close[i] - 1.0) * 10000.0
            confirm_slope_bps = side * (close[j] / close[max(i, j - 60)] - 1.0) * 10000.0
            buy = float(np.nansum(buy_qty[max(i, j - 60) + 1 : j + 1]))
            sell = float(np.nansum(sell_qty[max(i, j - 60) + 1 : j + 1]))
            raw_flow = (buy - sell) / max(buy + sell, 1e-12)
            aligned_flow = side * raw_flow
            if (
                confirm_move_bps >= cfg.min_confirm_move_bps
                and confirm_slope_bps >= cfg.min_confirm_slope_bps
                and aligned_flow >= cfg.min_confirm_flow_imbalance
            ):
                confirmed = j
                confirm_extra = {
                    "confirm_delay_sec": int(j - i),
                    "confirm_move_bps": round(float(confirm_move_bps), 6),
                    "confirm_slope_bps": round(float(confirm_slope_bps), 6),
                    "confirm_flow_imbalance": round(float(raw_flow), 6),
                    "aligned_confirm_flow_imbalance": round(float(aligned_flow), 6),
                }
                break
        if confirmed is None or confirm_extra is None:
            continue

        last_signal_idx = confirmed
        rows.append(
            settle_signal(
                bars=bars,
                idx=confirmed,
                strategy_id=cfg.strategy_id,
                model_type="second_normal_direction_3m",
                signal=signal,
                horizon_sec=cfg.horizon_sec,
                amount=cfg.amount,
                extra={
                    "signal_time": bars.index[i],
                    "p_up": round(float(p_up), 6),
                    "z_score": round(float(z), 6),
                    "prob_edge": float(cfg.prob_edge),
                    "lookback_sec": int(cfg.lookback_sec),
                    "confirm_sec": int(cfg.confirm_sec),
                    "range_bps": round(float(range_bps), 6),
                    "sigma10_bps": round(float(sigma10_bps), 6),
                    "normal_mean10_bps": round(float(cfg.horizon_sec * mu[i] * 10000.0), 6),
                    "range_low": round(float(roll_min[i]), 6),
                    "range_high": round(float(roll_max[i]), 6),
                    **confirm_extra,
                },
            )
        )
    return apply_signal_gap(rows, cfg.signal_gap_sec) if apply_config_gap else rows


def generate_normal_multiframe_signals(
    bars: pd.DataFrame,
    cfg: SecondNormalMultiframeConfig,
    *,
    apply_config_gap: bool = True,
) -> list[dict]:
    """Use a rolling 10m normal range with 30m/60m context to choose 10m UP/DOWN."""

    close = bars["close"].to_numpy(float)
    buy_qty = bars["buy_qty"].to_numpy(float)
    sell_qty = bars["sell_qty"].to_numpy(float)
    observed = bars["observed"].to_numpy(float) if "observed" in bars else np.ones(len(close), dtype=float)
    start = max(cfg.short_window_sec, cfg.mid_window_sec, cfg.long_window_sec) + max(60, cfg.long_window_sec // 5)
    if len(close) <= start + cfg.horizon_sec:
        return []

    close_s = pd.Series(close, index=bars.index)
    obs_s = pd.Series(observed, index=bars.index)

    short_mean = close_s.rolling(cfg.short_window_sec, min_periods=cfg.short_window_sec).mean().to_numpy(float)
    short_sd = close_s.rolling(cfg.short_window_sec, min_periods=cfg.short_window_sec).std(ddof=1).to_numpy(float)
    mid_mean_s = close_s.rolling(cfg.mid_window_sec, min_periods=cfg.mid_window_sec).mean()
    long_mean_s = close_s.rolling(cfg.long_window_sec, min_periods=cfg.long_window_sec).mean()
    mid_mean = mid_mean_s.to_numpy(float)
    long_mean = long_mean_s.to_numpy(float)
    mid_slope = (mid_mean_s / mid_mean_s.shift(max(60, cfg.mid_window_sec // 5)) - 1.0).to_numpy(float) * 10000.0
    long_slope = (long_mean_s / long_mean_s.shift(max(60, cfg.long_window_sec // 5)) - 1.0).to_numpy(float) * 10000.0
    obs_short = obs_s.rolling(cfg.short_window_sec, min_periods=cfg.short_window_sec).mean().to_numpy(float) * 100.0

    buy1 = pd.Series(buy_qty, index=bars.index).rolling(60, min_periods=1).sum().to_numpy(float)
    sell1 = pd.Series(sell_qty, index=bars.index).rolling(60, min_periods=1).sum().to_numpy(float)
    flow1 = (buy1 - sell1) / np.maximum(buy1 + sell1, 1e-12)
    slope1 = (close / np.roll(close, 60) - 1.0) * 10000.0
    slope1[:60] = np.nan

    def big_ok(mode: str, signal: str, i: int) -> bool:
        mode = str(mode).lower()
        if mode in ("", "any", "none", "off"):
            return True
        up = mid_slope[i] > 0 and long_slope[i] > 0
        down = mid_slope[i] < 0 and long_slope[i] < 0
        if mode == "align_up":
            return up
        if mode == "align_down":
            return down
        if mode == "counter_down":
            return down
        if mode == "counter_up":
            return up
        if mode == "signal_align":
            return up if signal == "UP" else down
        if mode == "signal_counter":
            return down if signal == "UP" else up
        return True

    rows: list[dict] = []
    last_signal_idx = -10**12
    for i in range(start, len(close) - cfg.horizon_sec):
        if apply_config_gap and cfg.signal_gap_sec > 0 and i - last_signal_idx < cfg.signal_gap_sec:
            continue
        sd = short_sd[i]
        if not np.isfinite(sd) or sd <= 1e-12 or not np.isfinite(short_mean[i]):
            continue
        if not np.isfinite(mid_slope[i]) or not np.isfinite(long_slope[i]) or not np.isfinite(slope1[i]):
            continue
        if obs_short[i] < cfg.min_observed_pct:
            continue
        z10 = (close[i] - short_mean[i]) / sd
        width2_bps = 4.0 * sd / close[i] * 10000.0
        if not np.isfinite(z10) or width2_bps < cfg.min_width2_bps or width2_bps > cfg.max_width2_bps:
            continue

        signal = None
        reason = None
        if z10 <= cfg.lower_z:
            if (
                big_ok(cfg.lower_big_mode, "UP", i)
                and slope1[i] >= cfg.lower_min_slope1_bps
                and flow1[i] >= cfg.lower_min_flow1
            ):
                signal = "UP"
                reason = "lower_reversion_multiframe"
        elif z10 >= cfg.upper_z:
            if (
                big_ok(cfg.upper_big_mode, "DOWN", i)
                and slope1[i] <= -cfg.upper_min_slope1_bps
                and flow1[i] <= -cfg.upper_min_flow1
            ):
                signal = "DOWN"
                reason = "upper_reversion_multiframe"
        elif cfg.core_enabled and abs(z10) <= cfg.core_z_abs_max:
            if (
                mid_slope[i] > 0
                and long_slope[i] > 0
                and slope1[i] >= cfg.core_min_slope1_bps
                and flow1[i] >= cfg.core_min_flow1
            ):
                signal = "UP"
                reason = "core_big_up_flow_up"

        if not signal:
            continue
        last_signal_idx = i
        rows.append(
            settle_signal(
                bars=bars,
                idx=i,
                strategy_id=cfg.strategy_id,
                model_type="second_normal_multiframe",
                signal=signal,
                horizon_sec=cfg.horizon_sec,
                amount=cfg.amount,
                extra={
                    "reason": reason,
                    "z10": round(float(z10), 6),
                    "width2_10m_bps": round(float(width2_bps), 6),
                    "mean10": round(float(short_mean[i]), 6),
                    "mid_mean": round(float(mid_mean[i]), 6),
                    "long_mean": round(float(long_mean[i]), 6),
                    "mid_slope_bps": round(float(mid_slope[i]), 6),
                    "long_slope_bps": round(float(long_slope[i]), 6),
                    "slope1_bps": round(float(slope1[i]), 6),
                    "flow1": round(float(flow1[i]), 6),
                    "observed10_pct": round(float(obs_short[i]), 6),
                    "lower_z": float(cfg.lower_z),
                    "upper_z": float(cfg.upper_z),
                    "lower_big_mode": cfg.lower_big_mode,
                    "upper_big_mode": cfg.upper_big_mode,
                },
            )
        )
    return apply_signal_gap(rows, cfg.signal_gap_sec) if apply_config_gap else rows


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


def _first_eta_hit(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    idx: int,
    signal: str,
    target_bps: float,
    max_wait_sec: int,
) -> tuple[int | None, float | None]:
    p0 = close[idx]
    if signal == "UP":
        target = p0 * math.exp(-float(target_bps) / 10000.0)
        for j in range(idx + 1, min(idx + int(max_wait_sec), len(low) - 1) + 1):
            if low[j] <= target:
                return j, float(target)
    else:
        target = p0 * math.exp(float(target_bps) / 10000.0)
        for j in range(idx + 1, min(idx + int(max_wait_sec), len(high) - 1) + 1):
            if high[j] >= target:
                return j, float(target)
    return None, None


def _first_eta_entry_with_up_confirm(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    idx: int,
    signal: str,
    target_bps: float,
    max_wait_sec: int,
    up_confirm_bps: float,
    up_confirm_max_sec: int,
) -> tuple[int | None, float | None, dict[str, float | int | str | bool]]:
    hit_idx, target = _first_eta_hit(high, low, close, idx, signal, target_bps, max_wait_sec)
    if hit_idx is None or target is None:
        return None, None, {"entry_reason": "eta_target_not_hit"}
    if signal != "UP" or float(up_confirm_bps) <= 0:
        return hit_idx, target, {
            "entry_reason": "eta_target_hit",
            "eta_hit_idx": int(hit_idx),
            "up_reversal_confirmed": signal != "UP",
            "up_reversal_confirm_bps": float(up_confirm_bps),
            "up_reversal_confirm_max_sec": int(up_confirm_max_sec),
        }

    end = min(hit_idx + int(up_confirm_max_sec), len(close) - 1)
    running_low = float(low[hit_idx])
    for j in range(hit_idx, end + 1):
        running_low = min(running_low, float(low[j]))
        rebound_bps = ((float(high[j]) - running_low) / running_low) * 10000.0 if running_low > 0 else 0.0
        if rebound_bps >= float(up_confirm_bps):
            return j, float(close[j]), {
                "entry_reason": "up_reversal_confirmed",
                "eta_hit_idx": int(hit_idx),
                "up_reversal_confirmed": True,
                "up_reversal_confirm_bps": float(up_confirm_bps),
                "up_reversal_confirm_max_sec": int(up_confirm_max_sec),
                "up_reversal_rebound_bps": float(rebound_bps),
                "up_reversal_delay_sec": int(j - hit_idx),
            }
    return None, None, {
        "entry_reason": "up_reversal_confirm_failed",
        "eta_hit_idx": int(hit_idx),
        "up_reversal_confirmed": False,
        "up_reversal_confirm_bps": float(up_confirm_bps),
        "up_reversal_confirm_max_sec": int(up_confirm_max_sec),
    }


def _eta_forecast_ok(
    close: np.ndarray,
    buy_qty: np.ndarray,
    sell_qty: np.ndarray,
    idx: int,
    signal: str,
    target_bps: float,
    max_wait_sec: int,
) -> tuple[bool, dict[str, float | str | bool]]:
    speed_window = 30
    accel_window = 10
    min_speed_bps = 0.005
    if idx < speed_window + accel_window + 2:
        return False, {"eta_ok": False, "eta_reason": "warmup"}
    side = 1.0 if signal == "DOWN" else -1.0
    ret_bps = side * 10000.0 * np.diff(np.log(close), prepend=np.nan)
    recent = ret_bps[idx - speed_window + 1 : idx + 1]
    prev = ret_bps[idx - speed_window - accel_window + 1 : idx - accel_window + 1]
    if len(recent) < speed_window or len(prev) < speed_window:
        return False, {"eta_ok": False, "eta_reason": "warmup"}
    weights = np.linspace(1.0, 2.0, len(recent))
    pos_recent = np.clip(recent, 0.0, None)
    weighted_speed = float(np.average(pos_recent, weights=weights))
    net_move = float(np.nansum(recent))
    path = float(np.nansum(np.abs(recent)))
    efficiency = max(0.0, min(1.0, net_move / path)) if path > 1e-12 else 0.0
    volume = buy_qty[idx - speed_window + 1 : idx + 1] + sell_qty[idx - speed_window + 1 : idx + 1]
    flow = side * (buy_qty[idx - speed_window + 1 : idx + 1] - sell_qty[idx - speed_window + 1 : idx + 1])
    flow_eff = float(np.nansum(flow) / max(np.nansum(volume), 1e-12))
    flow_multiplier = max(0.25, min(1.5, 1.0 + flow_eff))
    v_now = float(np.nanmean(np.clip(recent[-accel_window:], 0.0, None)))
    v_prev = float(np.nanmean(np.clip(prev[-accel_window:], 0.0, None)))
    accel = (v_now - v_prev) / max(float(accel_window), 1.0)
    raw_speed = weighted_speed * max(efficiency, 0.15) * flow_multiplier
    if raw_speed < min_speed_bps:
        return False, {
            "eta_ok": False,
            "eta_reason": "no_momentum",
            "eta_sec": 1_000_000_000.0,
            "eta_speed_bps_sec": raw_speed,
            "eta_efficiency": efficiency,
            "eta_flow_eff": flow_eff,
        }
    eta_linear = float(target_bps) / raw_speed
    eta_accel = eta_linear
    if abs(accel) > 1e-9:
        disc = raw_speed * raw_speed + 2.0 * accel * float(target_bps)
        if disc > 0:
            root = (-raw_speed + math.sqrt(disc)) / accel
            if root > 0 and np.isfinite(root):
                eta_accel = float(root)
    eta = max(1.0, 0.65 * eta_linear + 0.35 * eta_accel)
    return eta <= float(max_wait_sec), {
        "eta_ok": bool(eta <= float(max_wait_sec)),
        "eta_reason": "predicted_reachable" if eta <= float(max_wait_sec) else "eta_too_slow",
        "eta_sec": float(eta),
        "eta_speed_bps_sec": float(raw_speed),
        "eta_efficiency": float(efficiency),
        "eta_flow_eff": float(flow_eff),
        "eta_accel_bps_sec2": float(accel),
    }


def generate_normal_vw_confirm_signals(
    bars: pd.DataFrame,
    cfg: SecondNormalVwConfirmConfig,
    *,
    apply_config_gap: bool = True,
) -> list[dict]:
    close = bars["close"].to_numpy(float)
    high = bars["high"].to_numpy(float) if "high" in bars else close
    low = bars["low"].to_numpy(float) if "low" in bars else close
    volume = bars["volume"].to_numpy(float)
    buy_qty = bars["buy_qty"].to_numpy(float)
    sell_qty = bars["sell_qty"].to_numpy(float)
    if len(close) <= cfg.lookback_sec + cfg.horizon_sec + cfg.eta_max_wait_sec:
        return []
    logp = np.log(close)
    lr = np.diff(logp, prepend=np.nan)
    series = pd.Series(lr, index=bars.index)
    min_periods = max(60, min(cfg.lookback_sec, cfg.lookback_sec // 4))
    mu = series.rolling(cfg.lookback_sec, min_periods=min_periods).mean().to_numpy()
    sigma = series.rolling(cfg.lookback_sec, min_periods=min_periods).std(ddof=1).to_numpy()
    w = np.nan_to_num(volume, nan=0.0)
    x = np.nan_to_num(lr, nan=0.0)
    vw_min_periods = max(120, cfg.lookback_sec // 3)
    sw = pd.Series(w, index=bars.index).rolling(cfg.lookback_sec, min_periods=vw_min_periods).sum().to_numpy(float)
    sx = pd.Series(w * x, index=bars.index).rolling(cfg.lookback_sec, min_periods=vw_min_periods).sum().to_numpy(float)
    sx2 = pd.Series(w * x * x, index=bars.index).rolling(cfg.lookback_sec, min_periods=vw_min_periods).sum().to_numpy(float)

    rows: list[dict] = []
    threshold_hi = 1.0 - cfg.tail_pct
    end = len(close) - cfg.horizon_sec - cfg.eta_max_wait_sec
    last_signal_idx = -10**12
    for i in range(cfg.lookback_sec, end):
        if apply_config_gap and cfg.signal_gap_sec > 0 and i - last_signal_idx < cfg.signal_gap_sec:
            continue
        if not np.isfinite(mu[i]) or not np.isfinite(sigma[i]) or sigma[i] < 1e-12:
            continue
        z = float(cfg.horizon_sec * mu[i] / (math.sqrt(cfg.horizon_sec) * sigma[i]))
        p_up = normal_cdf(z)
        signal = "DOWN" if p_up >= threshold_hi else "UP" if p_up <= cfg.tail_pct else None
        if not signal:
            continue
        if not np.isfinite(sw[i]) or sw[i] <= 1e-12:
            continue
        vw_mu = sx[i] / sw[i]
        vw_sigma = math.sqrt(max(sx2[i] / sw[i] - vw_mu * vw_mu, 0.0))
        if vw_sigma < 1e-12:
            continue
        vw_z = float(cfg.horizon_sec * vw_mu / (math.sqrt(cfg.horizon_sec) * vw_sigma))
        vw_p_up = normal_cdf(vw_z)
        vw_signal = "DOWN" if vw_p_up >= threshold_hi else "UP" if vw_p_up <= cfg.tail_pct else None
        if vw_signal != signal:
            continue
        last_signal_idx = i
        eta_ok, eta_extra = _eta_forecast_ok(
            close, buy_qty, sell_qty, i, signal, cfg.eta_target_bps, cfg.eta_max_wait_sec
        )
        if not eta_ok:
            continue
        hit_idx, entry, entry_extra = _first_eta_entry_with_up_confirm(
            high,
            low,
            close,
            i,
            signal,
            cfg.eta_target_bps,
            cfg.eta_max_wait_sec,
            cfg.up_reversal_confirm_bps,
            cfg.up_reversal_confirm_max_sec,
        )
        if hit_idx is None or hit_idx + cfg.horizon_sec >= len(close):
            continue
        settle = float(close[hit_idx + cfg.horizon_sec])
        rows.append(
            {
                "strategy_id": cfg.strategy_id,
                "model_type": "second_normal_vw_confirm",
                "idx": int(hit_idx),
                "signal_idx": int(i),
                "time": bars.index[hit_idx],
                "signal_time": bars.index[i],
                "signal": signal,
                "entry": float(entry),
                "settle_time": bars.index[hit_idx + cfg.horizon_sec],
                "settle": settle,
                "won": bool(settle > entry if signal == "UP" else settle < entry),
                "horizon_sec": int(cfg.horizon_sec),
                "amount": float(cfg.amount),
                "p_up": round(float(p_up), 6),
                "z_score": round(float(z), 6),
                "vw_p_up": round(float(vw_p_up), 6),
                "vw_z_score": round(float(vw_z), 6),
                "tail_pct": float(cfg.tail_pct),
                "lookback_sec": int(cfg.lookback_sec),
                "eta_target_bps": float(cfg.eta_target_bps),
                "eta_max_wait_sec": int(cfg.eta_max_wait_sec),
                "eta_delay_sec": int(hit_idx - i),
                **eta_extra,
                **entry_extra,
            }
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


def generate_trend_pullback_down_signals(
    bars: pd.DataFrame,
    cfg: SecondTrendPullbackDownConfig,
    *,
    apply_config_gap: bool = True,
) -> list[dict]:
    close = bars["close"].to_numpy(float)
    start = max(cfg.regime_lookback_sec, cfg.regime_alt_lookback_sec, cfg.pullback_sec, 1800)
    if len(close) <= start + cfg.horizon_sec:
        return []
    series = pd.Series(close)
    roll_min = series.rolling(cfg.regime_lookback_sec, min_periods=60).min().to_numpy(float)
    roll_max = series.rolling(cfg.regime_lookback_sec, min_periods=60).max().to_numpy(float)
    roll_mean = series.rolling(cfg.regime_lookback_sec, min_periods=60).mean().to_numpy(float)

    rows: list[dict] = []
    for i in range(start, len(close) - cfg.horizon_sec):
        regime_ret = close[i] / close[i - cfg.regime_lookback_sec] - 1.0
        alt_regime_ret = close[i] / close[i - cfg.regime_alt_lookback_sec] - 1.0
        recent_ret = close[i] / close[i - 1800] - 1.0
        pullback_ret = close[i] / close[i - cfg.pullback_sec] - 1.0
        pos = (close[i] - roll_min[i]) / (roll_max[i] - roll_min[i] + 1e-12)
        mean_gap = close[i] / (roll_mean[i] + 1e-12) - 1.0
        regime_active = (
            (regime_ret <= -cfg.regime_drop_pct or alt_regime_ret <= -cfg.regime_alt_drop_pct)
            and pos < cfg.max_pos_pct
            and recent_ret <= cfg.max_recent_ret_pct
            and mean_gap <= 0
        )
        entry_active = pullback_ret >= cfg.pullback_pct and pos < cfg.max_entry_pos_pct
        if not regime_active or not entry_active:
            continue
        rows.append(
            settle_signal(
                bars=bars,
                idx=i,
                strategy_id=cfg.strategy_id,
                model_type="second_trend_pullback_down",
                signal="DOWN",
                horizon_sec=cfg.horizon_sec,
                amount=cfg.amount,
                extra={
                    "regime_lookback_sec": int(cfg.regime_lookback_sec),
                    "regime_alt_lookback_sec": int(cfg.regime_alt_lookback_sec),
                    "regime_drop_pct": float(cfg.regime_drop_pct),
                    "regime_alt_drop_pct": float(cfg.regime_alt_drop_pct),
                    "regime_ret": round(float(regime_ret), 6),
                    "alt_regime_ret": round(float(alt_regime_ret), 6),
                    "recent_ret": round(float(recent_ret), 6),
                    "pos_regime": round(float(pos), 6),
                    "mean_gap_regime": round(float(mean_gap), 6),
                    "pullback_sec": int(cfg.pullback_sec),
                    "pullback_pct": float(cfg.pullback_pct),
                    "pullback_ret": round(float(pullback_ret), 6),
                    "suppress_reversal_in_regime": bool(cfg.suppress_reversal_in_regime),
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
                    incident_filter=_incident_filter_config(cfg),
                    amount=amount,
                    label=str(cfg.get("model_label", strategy_id)),
                )
            )
        elif model_type == "second_normal_vw_confirm":
            configs.append(
                SecondNormalVwConfirmConfig(
                    strategy_id=strategy_id,
                    lookback_sec=int(cfg.get("second_lookback_sec", 2700)),
                    horizon_sec=int(cfg.get("second_horizon_sec", 600)),
                    signal_gap_sec=int(
                        cfg.get("second_signal_gap_sec", cfg.get("second_min_gap_sec", 600))
                    ),
                    tail_pct=float(cfg.get("second_tail_pct", 0.20)),
                    eta_target_bps=float(cfg.get("eta_target_bps", 2.0)),
                    eta_max_wait_sec=int(cfg.get("eta_max_wait_sec", 45)),
                    up_reversal_confirm_bps=float(cfg.get("up_reversal_confirm_bps", 0.0)),
                    up_reversal_confirm_max_sec=int(cfg.get("up_reversal_confirm_max_sec", 20)),
                    incident_filter=_incident_filter_config(cfg),
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
                    incident_filter=_incident_filter_config(cfg),
                    amount=amount,
                    label=str(cfg.get("model_label", strategy_id)),
                )
            )
        elif model_type == "second_trend_pullback_down":
            configs.append(
                SecondTrendPullbackDownConfig(
                    strategy_id=strategy_id,
                    regime_lookback_sec=int(cfg.get("second_trend_regime_lookback_sec", 7200)),
                    regime_alt_lookback_sec=int(cfg.get("second_trend_regime_alt_lookback_sec", 5400)),
                    regime_drop_pct=float(cfg.get("second_trend_regime_drop_pct", 0.004)),
                    regime_alt_drop_pct=float(cfg.get("second_trend_regime_alt_drop_pct", 0.003)),
                    max_pos_pct=float(cfg.get("second_trend_max_pos_pct", 0.6)),
                    max_entry_pos_pct=float(cfg.get("second_trend_max_entry_pos_pct", 0.4)),
                    max_recent_ret_pct=float(cfg.get("second_trend_max_recent_ret_pct", 0.001)),
                    pullback_sec=int(cfg.get("second_trend_pullback_sec", 300)),
                    pullback_pct=float(cfg.get("second_trend_pullback_pct", 0.001)),
                    horizon_sec=int(cfg.get("second_trend_horizon_sec", 600)),
                    signal_gap_sec=int(
                        cfg.get("second_trend_signal_gap_sec", cfg.get("second_trend_min_gap_sec", 600))
                    ),
                    amount=amount,
                    suppress_reversal_in_regime=bool(cfg.get("second_trend_suppress_reversal", True)),
                    label=str(cfg.get("model_label", strategy_id)),
                )
            )
        elif model_type == "second_range_breakout_confirm":
            configs.append(
                SecondRangeBreakoutConfirmConfig(
                    strategy_id=strategy_id,
                    lookback_sec=int(cfg.get("second_range_lookback_sec", cfg.get("second_lookback_sec", 1800))),
                    horizon_sec=int(cfg.get("second_range_horizon_sec", cfg.get("second_horizon_sec", 600))),
                    signal_gap_sec=int(cfg.get("second_range_signal_gap_sec", cfg.get("second_min_gap_sec", 600))),
                    z_entry=float(cfg.get("second_range_z_entry", 2.2)),
                    confirm_sec=int(cfg.get("second_range_confirm_sec", 60)),
                    hold_z=float(cfg.get("second_range_hold_z", 1.0)),
                    min_hold_ratio=float(cfg.get("second_range_min_hold_ratio", 0.75)),
                    pre_slope_sec=int(cfg.get("second_range_pre_slope_sec", 300)),
                    confirm_slope_sec=int(cfg.get("second_range_confirm_slope_sec", 60)),
                    min_pre_slope_bps=float(cfg.get("second_range_min_pre_slope_bps", 8.0)),
                    min_confirm_slope_bps=float(cfg.get("second_range_min_confirm_slope_bps", 4.0)),
                    min_flow_imbalance=float(cfg.get("second_range_min_flow_imbalance", 0.12)),
                    min_confirm_flow_imbalance=float(cfg.get("second_range_min_confirm_flow_imbalance", 0.08)),
                    min_volume_ratio=float(cfg.get("second_range_min_volume_ratio", 0.45)),
                    min_volatility_ratio=float(cfg.get("second_range_min_volatility_ratio", 0.55)),
                    max_age_beyond_sec=int(cfg.get("second_range_max_age_beyond_sec", 180)),
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


def _incident_filter_config(cfg: dict) -> dict[str, Any]:
    return {
        "incident_filter_enabled": cfg.get("incident_filter_enabled", False),
        "incident_filter_mode": cfg.get("incident_filter_mode", "directional_only"),
        "incident_window_sec": cfg.get("incident_window_sec", 10),
        "incident_min_move_bps": cfg.get("incident_min_move_bps", 10.0),
        "incident_min_volume_quantile": cfg.get("incident_min_volume_quantile", 0.99),
        "incident_min_flow_imbalance": cfg.get("incident_min_flow_imbalance", 0.80),
        "incident_cooldown_sec": cfg.get("incident_cooldown_sec", 10),
    }


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
