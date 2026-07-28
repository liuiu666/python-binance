"""Shared causal core for the current V2 augmented V9 strategy."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class AugmentedV9Rules:
    efficiency_min: float = 0.60
    trend_strength_min: float = 1.25
    opposing_min_bps: float = 2.0
    z30_min: float = 1.0
    volume_ratio_min: float = 0.80
    observed_min: float = 0.98
    minute_seconds_min: int = 55
    book_coverage_min: float = 0.90
    book_votes_min: int = 2
    max_emit_age_sec: int = 8
    supplement_min_abs_normal_z: float = 0.0
    supplement_normal_window_sec: int = 600
    original_regime_veto_enabled: bool = False
    original_veto_mature_downtrend: bool = True
    original_veto_short_migration_up_down: bool = True
    original_mature_trend_ret_1800_bps: float = 15.0
    original_mature_down_pos_1800_max: float = 0.30
    original_short_migration_ret_600_bps: float = 10.0
    original_short_migration_sigma_mult: float = 0.75
    original_allow_mature_downtrend_down_flow_min: float | None = None
    supplement_loose_short_migration_reversion_enabled: bool = False
    supplement_loose_mature_uptrend_down_enabled: bool = False
    supplement_mature_uptrend_down_flow_min: float = -0.30

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "AugmentedV9Rules":
        allow_flow = cfg.get(
            "v9_original_allow_mature_downtrend_down_flow_min",
            cfg.get("v9OriginalAllowMatureDowntrendDownFlowMin", None),
        )
        return cls(
            efficiency_min=float(cfg.get("v9_efficiency_min", cfg.get("v9EfficiencyMin", 0.60))),
            trend_strength_min=float(cfg.get("v9_trend_strength_min", cfg.get("v9TrendStrengthMin", 1.25))),
            opposing_min_bps=float(cfg.get("v9_opposing_min_bps", cfg.get("v9OpposingMinBps", 2.0))),
            z30_min=float(cfg.get("v9_z30_min", cfg.get("v9Z30Min", 1.0))),
            volume_ratio_min=float(cfg.get("v9_volume_ratio_min", cfg.get("v9VolumeRatioMin", 0.80))),
            observed_min=float(cfg.get("v9_observed_min", cfg.get("v9ObservedMin", 0.98))),
            minute_seconds_min=int(cfg.get("v9_minute_seconds_min", cfg.get("v9MinuteSecondsMin", 55))),
            book_coverage_min=float(cfg.get("v9_book_coverage_min", cfg.get("v9BookCoverageMin", 0.90))),
            book_votes_min=int(cfg.get("v9_book_votes_min", cfg.get("v9BookVotesMin", 2))),
            max_emit_age_sec=int(cfg.get("v9_max_emit_age_sec", cfg.get("v9MaxEmitAgeSec", 8))),
            supplement_min_abs_normal_z=float(
                cfg.get("v9_supplement_min_abs_normal_z", cfg.get("v9SupplementMinAbsNormalZ", 0.0))
            ),
            supplement_normal_window_sec=int(
                cfg.get("v9_supplement_normal_window_sec", cfg.get("normalWindowSec", 600))
            ),
            original_regime_veto_enabled=bool(
                cfg.get("v9_original_regime_veto_enabled", cfg.get("v9OriginalRegimeVetoEnabled", False))
            ),
            original_veto_mature_downtrend=bool(
                cfg.get("v9_original_veto_mature_downtrend", cfg.get("v9OriginalVetoMatureDowntrend", True))
            ),
            original_veto_short_migration_up_down=bool(
                cfg.get(
                    "v9_original_veto_short_migration_up_down",
                    cfg.get("v9OriginalVetoShortMigrationUpDown", True),
                )
            ),
            original_mature_trend_ret_1800_bps=float(
                cfg.get(
                    "v9_original_mature_trend_ret_1800_bps",
                    cfg.get("v9OriginalMatureTrendRet1800Bps", 15.0),
                )
            ),
            original_mature_down_pos_1800_max=float(
                cfg.get(
                    "v9_original_mature_down_pos_1800_max",
                    cfg.get("v9OriginalMatureDownPos1800Max", 0.30),
                )
            ),
            original_short_migration_ret_600_bps=float(
                cfg.get(
                    "v9_original_short_migration_ret_600_bps",
                    cfg.get("v9OriginalShortMigrationRet600Bps", 10.0),
                )
            ),
            original_short_migration_sigma_mult=float(
                cfg.get(
                    "v9_original_short_migration_sigma_mult",
                    cfg.get("v9OriginalShortMigrationSigmaMult", 0.75),
                )
            ),
            original_allow_mature_downtrend_down_flow_min=(
                None if allow_flow is None else float(allow_flow)
            ),
            supplement_loose_short_migration_reversion_enabled=bool(
                cfg.get(
                    "v9_supplement_loose_short_migration_reversion_enabled",
                    cfg.get("v9SupplementLooseShortMigrationReversionEnabled", False),
                )
            ),
            supplement_loose_mature_uptrend_down_enabled=bool(
                cfg.get(
                    "v9_supplement_loose_mature_uptrend_down_enabled",
                    cfg.get("v9SupplementLooseMatureUptrendDownEnabled", False),
                )
            ),
            supplement_mature_uptrend_down_flow_min=float(
                cfg.get(
                    "v9_supplement_mature_uptrend_down_flow_min",
                    cfg.get("v9SupplementMatureUptrendDownFlowMin", -0.30),
                )
            ),
        )


def _finite_float(row: Any, key: str) -> float:
    try:
        return float(row.get(key, np.nan))
    except (TypeError, ValueError):
        return float("nan")


def original_v2_price_state(row: Any, rules: AugmentedV9Rules) -> str:
    ret600 = _finite_float(row, "ret_600s_bps")
    ret1800 = _finite_float(row, "ret_1800s_bps")
    pos1800 = _finite_float(row, "pos_1800s")
    sigma = _finite_float(row, "sigma_bps")
    if not all(math.isfinite(value) for value in (ret600, ret1800, pos1800, sigma)):
        return "unknown"
    if (
        ret1800 <= -abs(rules.original_mature_trend_ret_1800_bps)
        and pos1800 <= rules.original_mature_down_pos_1800_max
    ):
        return "mature_downtrend"
    if abs(ret600) >= max(
        abs(rules.original_short_migration_ret_600_bps),
        sigma * rules.original_short_migration_sigma_mult,
    ):
        return "short_migration_up" if ret600 > 0.0 else "short_migration_down"
    return "range_or_chop"


def original_v2_regime_veto_code(
    signal: str | None,
    row: Any,
    rules: AugmentedV9Rules,
) -> str | None:
    if not rules.original_regime_veto_enabled or signal not in {"UP", "DOWN"}:
        return None
    state = original_v2_price_state(row, rules)
    if rules.original_veto_mature_downtrend and state == "mature_downtrend":
        flow_60 = _finite_float(row, "flow_60")
        if (
            signal == "DOWN"
            and rules.original_allow_mature_downtrend_down_flow_min is not None
            and math.isfinite(flow_60)
            and flow_60 < rules.original_allow_mature_downtrend_down_flow_min
        ):
            return None
        return "v9_original_skip_mature_downtrend"
    if (
        rules.original_veto_short_migration_up_down
        and state == "short_migration_up"
        and signal == "DOWN"
    ):
        return "v9_original_skip_short_migration_up_down"
    return None


def trailing_book_confirmation(
    data: pd.DataFrame,
    signal: str,
    end_time: pd.Timestamp,
    rules: AugmentedV9Rules,
) -> dict[str, Any]:
    end_pos = int(data.index.searchsorted(end_time, side="right") - 1)
    if end_pos < 59 or abs((data.index[end_pos] - end_time).total_seconds()) > 2:
        return {"ok": False, "reason": "book_window_missing", "votes": 0}
    window = data.iloc[end_pos - 59:end_pos + 1]
    available = window.get("ob_available", pd.Series(False, index=window.index)).fillna(False)
    coverage = float(available.mean())
    if coverage < rules.book_coverage_min:
        return {
            "ok": False,
            "reason": "book_coverage_low",
            "votes": 0,
            "coverage": coverage,
        }
    buy = float(pd.to_numeric(window.get("buy_qty"), errors="coerce").fillna(0.0).sum())
    sell = float(pd.to_numeric(window.get("sell_qty"), errors="coerce").fillna(0.0).sum())
    flow = (buy - sell) / (buy + sell) if buy + sell > 0.0 else 0.0
    imbalance = float(pd.to_numeric(window.get("imbalance_20"), errors="coerce").mean())
    micro = float(pd.to_numeric(window.get("microprice_edge_bps"), errors="coerce").mean())
    if not all(math.isfinite(value) for value in (flow, imbalance, micro)):
        return {"ok": False, "reason": "book_feature_missing", "votes": 0, "coverage": coverage}
    direction = 1.0 if signal == "UP" else -1.0
    votes = int(direction * flow > 0.0) + int(direction * imbalance > 0.0) + int(direction * micro > 0.0)
    return {
        "ok": votes >= rules.book_votes_min,
        "reason": "book_confirmed" if votes >= rules.book_votes_min else "book_votes_low",
        "votes": votes,
        "coverage": coverage,
        "flow_60": flow,
        "imbalance_60": imbalance,
        "micro_60": micro,
    }


def build_minute_features(data: pd.DataFrame, rules: AugmentedV9Rules) -> pd.DataFrame:
    seconds = data.sort_index()
    minute = seconds.resample("1min").agg(
        open=("close", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        seconds=("close", "count"),
    )
    close = minute.close.astype(float)
    ret1 = (close / close.shift(1) - 1.0) * 10000.0
    for width in (1, 3, 5, 10, 30):
        minute[f"ret_{width}"] = (close / close.shift(width) - 1.0) * 10000.0
    minute["path_10"] = ret1.abs().rolling(10, min_periods=10).sum()
    minute["efficiency_10"] = minute.ret_10.abs() / minute.path_10.replace(0.0, np.nan)
    minute["noise_30"] = ret1.rolling(30, min_periods=30).std(ddof=0)
    minute["trend_strength"] = minute.ret_10.abs() / (
        minute.noise_30 * math.sqrt(10.0)
    ).replace(0.0, np.nan)
    center30 = close.rolling(30, min_periods=30).mean()
    sigma30 = close.rolling(30, min_periods=30).std(ddof=0)
    minute["z_30"] = (close - center30) / sigma30.replace(0.0, np.nan)
    minute["volume_ratio"] = (
        minute.volume.rolling(5, min_periods=5).mean()
        / minute.volume.rolling(30, min_periods=30).mean().replace(0.0, np.nan)
    )
    observed = minute.seconds.ge(rules.minute_seconds_min).astype(float)
    minute["observed_ratio"] = observed.rolling(120, min_periods=120).mean()
    minute["detected_time"] = minute.index + pd.Timedelta(seconds=59)
    setup = (
        minute.efficiency_10.ge(rules.efficiency_min)
        & minute.trend_strength.ge(rules.trend_strength_min)
        & (minute.ret_3 * minute.ret_10).gt(0.0)
        & (minute.ret_1 * minute.ret_10).lt(0.0)
        & minute.ret_1.abs().ge(rules.opposing_min_bps)
        & (minute.z_30 * minute.ret_10).gt(0.0)
        & minute.z_30.abs().ge(rules.z30_min)
        & minute.volume_ratio.ge(rules.volume_ratio_min)
        & minute.observed_ratio.ge(rules.observed_min)
    )
    minute["candidate_signal"] = np.where(
        setup & minute.ret_10.gt(0.0),
        "DOWN",
        np.where(setup, "UP", None),
    )
    return minute


def build_second_normal_z(data: pd.DataFrame, rules: AugmentedV9Rules) -> pd.Series:
    close = data["close"].astype(float)
    volume = data.get("volume", pd.Series(1.0, index=data.index)).astype(float).clip(lower=0.0)
    window = max(120, int(rules.supplement_normal_window_sec))
    sw = volume.rolling(window, min_periods=max(120, window // 3)).sum()
    sx = (close * volume).rolling(window, min_periods=max(120, window // 3)).sum()
    sx2 = (close * close * volume).rolling(window, min_periods=max(120, window // 3)).sum()
    mean = close.rolling(window, min_periods=max(120, window // 3)).mean()
    std = close.rolling(window, min_periods=max(120, window // 3)).std(ddof=1)
    vwap = sx / sw.replace(0.0, np.nan)
    var = sx2 / sw.replace(0.0, np.nan) - vwap * vwap
    sigma = np.sqrt(var.clip(lower=0.0)).where(lambda s: s > 1e-9, std)
    center = vwap.fillna(mean)
    return (close - center) / sigma.replace(0.0, np.nan)


def build_second_state_features(data: pd.DataFrame, rules: AugmentedV9Rules) -> pd.DataFrame:
    close = data["close"].astype(float)
    volume = data.get("volume", pd.Series(1.0, index=data.index)).astype(float).clip(lower=0.0)
    window = max(120, int(rules.supplement_normal_window_sec))
    sw = volume.rolling(window, min_periods=max(120, window // 3)).sum()
    sx = (close * volume).rolling(window, min_periods=max(120, window // 3)).sum()
    sx2 = (close * close * volume).rolling(window, min_periods=max(120, window // 3)).sum()
    mean = close.rolling(window, min_periods=max(120, window // 3)).mean()
    std = close.rolling(window, min_periods=max(120, window // 3)).std(ddof=1)
    vwap = sx / sw.replace(0.0, np.nan)
    var = sx2 / sw.replace(0.0, np.nan) - vwap * vwap
    sigma = np.sqrt(var.clip(lower=0.0)).where(lambda s: s > 1e-9, std)
    out = pd.DataFrame(index=data.index)
    out["sigma_bps"] = sigma / close * 10000.0
    out["ret_600s_bps"] = np.log(close / close.shift(600)) * 10000.0
    out["ret_1800s_bps"] = np.log(close / close.shift(1800)) * 10000.0
    high_1800 = close.rolling(1800, min_periods=600).max()
    low_1800 = close.rolling(1800, min_periods=600).min()
    out["pos_1800s"] = (close - low_1800) / (high_1800 - low_1800).replace(0.0, np.nan)
    return out


def supplement_normal_gate(
    normal_z: pd.Series | None,
    detected: pd.Timestamp,
    rules: AugmentedV9Rules,
) -> tuple[bool, float | None]:
    if rules.supplement_min_abs_normal_z <= 0.0:
        return True, None
    if normal_z is None or normal_z.empty:
        return False, None
    pos = int(normal_z.index.searchsorted(detected, side="right") - 1)
    if pos < 0:
        return False, None
    value = float(normal_z.iloc[pos])
    if not math.isfinite(value):
        return False, None
    return abs(value) >= rules.supplement_min_abs_normal_z, value


def supplement_dynamic_gate(
    signal: str,
    state_row: Any | None,
    book: dict[str, Any],
    normal_ok: bool,
    rules: AugmentedV9Rules,
) -> tuple[bool, str]:
    if normal_ok:
        return True, "base_z"
    if state_row is None:
        return False, "normal_z_low"
    state = original_v2_price_state(state_row, rules)
    if (
        rules.supplement_loose_short_migration_reversion_enabled
        and signal == "UP"
        and state == "short_migration_down"
    ):
        return True, "loose_short_migration_down_up"
    flow_60 = _finite_float(book, "flow_60")
    if (
        rules.supplement_loose_mature_uptrend_down_enabled
        and signal == "DOWN"
        and state == "mature_uptrend"
        and math.isfinite(flow_60)
        and flow_60 > rules.supplement_mature_uptrend_down_flow_min
    ):
        return True, "loose_mature_uptrend_down_flow"
    return False, "normal_z_low"


def state_row_at(features: pd.DataFrame | None, timestamp: pd.Timestamp) -> pd.Series | None:
    if features is None or features.empty:
        return None
    pos = int(features.index.searchsorted(timestamp, side="right") - 1)
    if pos < 0:
        return None
    return features.iloc[pos]


def build_confirmed_supplement_candidates(
    data: pd.DataFrame,
    rules: AugmentedV9Rules,
) -> pd.DataFrame:
    minute = build_minute_features(data, rules)
    normal_z = build_second_normal_z(data, rules) if rules.supplement_min_abs_normal_z > 0.0 else None
    state_features = build_second_state_features(data, rules) if rules.supplement_min_abs_normal_z > 0.0 else None
    rows: list[dict[str, Any]] = []
    for timestamp, row in minute[minute.candidate_signal.notna()].iterrows():
        signal = str(row.candidate_signal)
        detected = pd.Timestamp(row.detected_time)
        normal_ok, second_normal_z = supplement_normal_gate(normal_z, detected, rules)
        book = trailing_book_confirmation(data, signal, detected, rules)
        if not book["ok"]:
            continue
        state_row = state_row_at(state_features, detected)
        dynamic_ok, gate_reason = supplement_dynamic_gate(signal, state_row, book, normal_ok, rules)
        if not dynamic_ok:
            continue
        rows.append({
            "minute_time": timestamp,
            "detected_time": detected,
            "signal": signal,
            "reason": "one_sided_exhaustion_reclaim_orderbook_2of3",
            "efficiency_10": float(row.efficiency_10),
            "trend_strength": float(row.trend_strength),
            "z_30": float(row.z_30),
            "ret_1": float(row.ret_1),
            "ret_3": float(row.ret_3),
            "ret_10": float(row.ret_10),
            "volume_ratio": float(row.volume_ratio),
            "second_normal_z": second_normal_z,
            "supplement_gate": gate_reason,
            "price_state": original_v2_price_state(state_row, rules) if state_row is not None else "unknown",
            **book,
        })
    return pd.DataFrame(rows)


def latest_confirmed_supplement(
    data: pd.DataFrame,
    now: pd.Timestamp,
    rules: AugmentedV9Rules,
) -> dict[str, Any] | None:
    minute = build_minute_features(data, rules)
    candidates = minute[minute.candidate_signal.notna()]
    if candidates.empty:
        return None
    candidate = candidates.iloc[-1]
    detected = pd.Timestamp(candidate.detected_time)
    age = (now - detected).total_seconds()
    if age < 0.0 or age > rules.max_emit_age_sec:
        return None
    normal_z = build_second_normal_z(data, rules) if rules.supplement_min_abs_normal_z > 0.0 else None
    state_features = build_second_state_features(data, rules) if rules.supplement_min_abs_normal_z > 0.0 else None
    normal_ok, second_normal_z = supplement_normal_gate(normal_z, detected, rules)
    signal = str(candidate.candidate_signal)
    book = trailing_book_confirmation(data, signal, detected, rules)
    if not book["ok"]:
        return None
    state_row = state_row_at(state_features, detected)
    dynamic_ok, gate_reason = supplement_dynamic_gate(signal, state_row, book, normal_ok, rules)
    if not dynamic_ok:
        return None
    return {
        "minute_time": candidates.index[-1],
        "detected_time": detected,
        "signal": signal,
        "reason": "one_sided_exhaustion_reclaim_orderbook_2of3",
        "efficiency_10": float(candidate.efficiency_10),
        "trend_strength": float(candidate.trend_strength),
        "z_30": float(candidate.z_30),
        "ret_1": float(candidate.ret_1),
        "ret_3": float(candidate.ret_3),
        "ret_10": float(candidate.ret_10),
        "volume_ratio": float(candidate.volume_ratio),
        "second_normal_z": second_normal_z,
        "supplement_gate": gate_reason,
        "price_state": original_v2_price_state(state_row, rules) if state_row is not None else "unknown",
        "emit_age_sec": age,
        **book,
    }
