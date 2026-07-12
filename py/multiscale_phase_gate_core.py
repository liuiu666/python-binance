"""Shared causal core for the multi-scale migration phase strategy."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


STRATEGY_ID = "BTC_10min_MULTISCALE_PHASE_GATE_V1"
MODEL_TYPE = "second_multiscale_phase_gate_v1"
WINDOWS = (1, 2, 3, 5, 10)


@dataclass(frozen=True)
class MultiscalePhaseGateConfig:
    horizon_sec: int = 600
    min_gap_sec: int = 600
    orderbook_max_age_sec: int = 3
    max_emit_age_sec: int = 8
    phase_lookback_sec: int = 3600
    maturity_history_sec: int = 3600
    maturity_min_periods: int = 1800
    maturity_quantile: float = 0.75
    min_flow60: float = 0.08
    min_imbalance20: float = 0.05
    min_microprice_bps: float = 0.0
    min_volume_ratio: float = 0.8

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> "MultiscalePhaseGateConfig":
        cfg = cfg or {}
        return cls(
            horizon_sec=int(cfg.get("phase_gate_horizon_sec", 600)),
            min_gap_sec=int(cfg.get("phase_gate_signal_gap_sec", 600)),
            orderbook_max_age_sec=int(cfg.get("phase_gate_orderbook_max_age_sec", 3)),
            max_emit_age_sec=int(cfg.get("phase_gate_max_emit_age_sec", 8)),
            phase_lookback_sec=int(cfg.get("phase_gate_lookback_sec", 3600)),
            maturity_history_sec=int(cfg.get("phase_gate_maturity_history_sec", 3600)),
            maturity_min_periods=int(cfg.get("phase_gate_maturity_min_periods", 1800)),
            maturity_quantile=float(cfg.get("phase_gate_maturity_quantile", 0.75)),
            min_flow60=float(cfg.get("phase_gate_min_flow60", 0.08)),
            min_imbalance20=float(cfg.get("phase_gate_min_imbalance20", 0.05)),
            min_microprice_bps=float(cfg.get("phase_gate_min_microprice_bps", 0.0)),
            min_volume_ratio=float(cfg.get("phase_gate_min_volume_ratio", 0.8)),
        )


def _shape_features(values: np.ndarray, observed: np.ndarray) -> dict[str, Any] | None:
    if len(values) < 30 or not np.all(np.isfinite(values)):
        return None
    observed_pct = float(np.mean(observed) * 100.0)
    if observed_pct < 60.0:
        return None
    center = float(np.mean(values))
    sigma = float(np.std(values, ddof=0))
    if center <= 0.0 or sigma <= 0.0:
        return None
    deviations = (values - center) / sigma
    inside1 = float(np.mean(np.abs(deviations) <= 1.0))
    skew = float(np.mean(deviations**3))
    kurtosis = float(np.mean(deviations**4) - 3.0)
    sigma_bps = sigma / center * 10000.0
    quarter = max(10, len(values) // 4)
    first_center = float(np.mean(values[:quarter]))
    last_center = float(np.mean(values[-quarter:]))
    slope_bps = (last_center / first_center - 1.0) * 10000.0 if first_center > 0.0 else 0.0
    slope_sigma = slope_bps / sigma_bps if sigma_bps > 0.0 else 0.0
    half = len(values) // 2
    sigma_first = float(np.std(values[:half], ddof=0))
    sigma_last = float(np.std(values[half:], ddof=0))
    sigma_ratio = sigma_last / sigma_first if sigma_first > 0.0 else 1.0
    z = float(deviations[-1])
    if z >= 2.0:
        shape = "upper_escape"
    elif z <= -2.0:
        shape = "lower_escape"
    elif slope_sigma >= 0.75:
        shape = "shift_up"
    elif slope_sigma <= -0.75:
        shape = "shift_down"
    elif sigma_ratio >= 1.5:
        shape = "expanding"
    elif sigma_ratio <= 0.67:
        shape = "contracting"
    elif 0.55 <= inside1 <= 0.80 and abs(skew) <= 0.75 and abs(kurtosis) <= 1.5:
        shape = "balanced_normal"
    elif skew > 0.75:
        shape = "right_skew"
    elif skew < -0.75:
        shape = "left_skew"
    elif kurtosis > 1.5:
        shape = "heavy_tail"
    else:
        shape = "distorted"
    return {
        "shape": shape,
        "center": center,
        "sigma_bps": sigma_bps,
        "z": z,
        "slope_sigma": slope_sigma,
        "observed_pct": observed_pct,
    }


def _migration_context(shapes: dict[int, dict[str, Any]], direction: int) -> bool:
    shift = "shift_up" if direction > 0 else "shift_down"
    escape = "upper_escape" if direction > 0 else "lower_escape"
    opposing_escape = "lower_escape" if direction > 0 else "upper_escape"
    return bool(
        shapes[3]["shape"] == shift
        and shapes[5]["shape"] == shift
        and shapes[2]["shape"] in {shift, escape}
        and shapes[10]["shape"] in {escape, "balanced_normal", "contracting"}
        and shapes[1]["shape"] != opposing_escape
    )


def _flow_ratio(buy: np.ndarray, sell: np.ndarray, start: int, end: int) -> float:
    buy_sum = float(np.sum(buy[start:end]))
    sell_sum = float(np.sum(sell[start:end]))
    total = buy_sum + sell_sum
    return (buy_sum - sell_sum) / total if total > 0.0 else 0.0


def build_snapshots(data: pd.DataFrame, cfg: MultiscalePhaseGateConfig) -> pd.DataFrame:
    frame = data.copy().sort_index()
    close = frame["close"].astype(float).to_numpy()
    observed = frame.get("observed", pd.Series(True, index=frame.index)).fillna(False).to_numpy(bool)
    buy = frame.get("buy_qty", pd.Series(0.0, index=frame.index)).fillna(0.0).to_numpy(float)
    sell = frame.get("sell_qty", pd.Series(0.0, index=frame.index)).fillna(0.0).to_numpy(float)
    volume = frame.get("volume", pd.Series(0.0, index=frame.index)).fillna(0.0).to_numpy(float)
    imbalance = frame.get("imbalance_20", pd.Series(np.nan, index=frame.index)).to_numpy(float)
    micro = frame.get("microprice_edge_bps", pd.Series(np.nan, index=frame.index)).to_numpy(float)
    available = frame.get("ob_available", pd.Series(False, index=frame.index)).fillna(False).to_numpy(bool)
    lookback = cfg.phase_lookback_sec
    long_return = pd.Series(close, index=frame.index).pct_change(lookback, fill_method=None) * 10000.0
    maturity = (
        long_return.abs().shift(1).rolling(
            cfg.maturity_history_sec,
            min_periods=cfg.maturity_min_periods,
        ).quantile(cfg.maturity_quantile)
    ).to_numpy(float)
    long_return_values = long_return.to_numpy(float)
    warmup = lookback + cfg.maturity_min_periods
    rows: list[dict[str, Any]] = []
    for index in np.flatnonzero(frame.index.second.to_numpy() == 59):
        if index < warmup:
            continue
        shapes: dict[int, dict[str, Any]] = {}
        for window in WINDOWS:
            width = window * 60
            feature = _shape_features(close[index - width + 1:index + 1], observed[index - width + 1:index + 1])
            if feature is None:
                break
            shapes[window] = feature
        if len(shapes) != len(WINDOWS):
            continue
        flow60 = _flow_ratio(buy, sell, index - 59, index + 1)
        recent_volume = float(np.sum(volume[index - 59:index + 1]))
        baseline_volume = float(np.sum(volume[index - 599:index + 1])) / 10.0
        volume_ratio = recent_volume / baseline_volume if baseline_volume > 0.0 else 0.0
        migration_direction = next(
            (direction for direction in (1, -1) if _migration_context(shapes, direction)),
            0,
        )
        crowd_direction = 0
        for direction in (1, -1):
            confirmed = (
                direction * flow60 >= cfg.min_flow60
                and direction * imbalance[index] >= cfg.min_imbalance20
                and direction * micro[index] >= cfg.min_microprice_bps
                and volume_ratio >= cfg.min_volume_ratio
            )
            if available[index] and migration_direction == direction and confirmed:
                crowd_direction = direction
                break
        phase = "no_migration_candidate"
        signal = None
        reason = "waiting_multiscale_migration"
        aligned_return = float("nan")
        threshold = float(maturity[index])
        if crowd_direction:
            aligned_return = crowd_direction * float(long_return_values[index])
            if not math.isfinite(aligned_return) or not math.isfinite(threshold):
                phase, reason = "unknown", "waiting_phase_history"
            elif aligned_return <= 0.0:
                phase, reason = "countertrend_pullback", "phase_gate_countertrend_fade"
                signal = "DOWN" if crowd_direction > 0 else "UP"
            elif aligned_return >= threshold:
                phase, reason = "mature", "phase_gate_mature_fade"
                signal = "DOWN" if crowd_direction > 0 else "UP"
            else:
                phase, reason = "startup_or_middle", "phase_gate_startup_middle_skip"
        rows.append({
            "detected_time": frame.index[index],
            "signal": signal,
            "reason": reason,
            "phase": phase,
            "migration_direction": "UP" if migration_direction > 0 else "DOWN" if migration_direction < 0 else None,
            "crowd_direction": "UP" if crowd_direction > 0 else "DOWN" if crowd_direction < 0 else None,
            "aligned_ret3600_bps": aligned_return,
            "maturity_threshold_bps": threshold,
            "flow60": flow60,
            "imbalance20": float(imbalance[index]),
            "microprice_bps": float(micro[index]),
            "volume_ratio": volume_ratio,
            **{f"shape_{window}m": shapes[window]["shape"] for window in WINDOWS},
            **{f"z_{window}m": shapes[window]["z"] for window in WINDOWS},
        })
    return pd.DataFrame(rows)


def evaluate_latest(snapshots: pd.DataFrame) -> dict[str, Any]:
    if snapshots is None or snapshots.empty:
        return {"signal": None, "reason": "waiting_completed_minute", "reason_zh": "等待完整分钟数据。"}
    row = snapshots.iloc[-1].to_dict()
    phase = str(row.get("phase") or "")
    signal = row.get("signal")
    if signal not in {"UP", "DOWN"}:
        signal = None
        row["signal"] = None
    crowd_direction = row.get("crowd_direction")
    if crowd_direction not in {"UP", "DOWN"}:
        row["crowd_direction"] = None
    migration_direction = row.get("migration_direction")
    if migration_direction not in {"UP", "DOWN"}:
        row["migration_direction"] = None
    if signal:
        phase_zh = "逆大周期回调" if phase == "countertrend_pullback" else "迁移已经成熟"
        direction_zh = "上涨" if signal == "UP" else "下跌"
        row["reason_zh"] = f"{phase_zh}，短周期拥挤方向衰竭，预测未来10分钟{direction_zh}。"
    elif phase == "startup_or_middle":
        row["reason_zh"] = "迁移刚启动或仍在中段，继续追随和提前反转都不稳定，本分钟跳过。"
    else:
        row["reason_zh"] = "等待2/3/5分钟同向迁移，并由成交流和订单薄确认。"
    return row
