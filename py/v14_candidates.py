"""Causal candidate generators for the V14 second-reversal research family.

The three candidates deliberately share one small signal skeleton:

* a trailing distribution/state gate;
* a completed excursion followed by reclaim;
* current passive order-book support/resistance;
* a causal 60-second, two-of-three order-book confirmation.

``V14-Core`` uses the production VWAP/dispersion features without any V9/V13
supplement or add-back branch. ``V14-Robust`` replaces the centre and scale
with rolling median and empirical quartiles. ``V14-Regime`` is Core plus one
fixed trend-state veto.  This module only creates research candidates.  It has
no order placement or production configuration code.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Iterable

import numpy as np
import pandas as pd

from liquidity_v2_core import LiquidityV2Rules, build_features


CANDIDATE_NAMES = ("V14-Core", "V14-Robust", "V14-Regime")
SHADOW_ONLY_POLICY = {
    "enabled": True,
    "tradeEnabled": False,
    "observationMode": "shadow",
    "realTradingAllowed": False,
}


@dataclass(frozen=True)
class V14CandidateRules:
    """Frozen, low-complexity thresholds shared by the V14 candidates."""

    normal_window_sec: int = 600
    z_entry: float = 1.20
    z_reclaim: float = 0.85
    retest_sec: int = 120
    inside_min: float = 0.55
    observed_min_pct: float = 88.0
    center_slope_sec: int = 300
    center_slope_max_bps: float = 6.0
    sigma_min_bps: float = 5.8
    sigma_max_bps: float = 55.0
    sigma_expand_max: float = 1.6
    orderbook_max_age_sec: int = 3
    ob_imbalance_min: float = 0.08
    micro_min_bps: float = 0.001
    wall_ratio_min: float = 1.0
    flow_guard: float = 0.12
    true_break_flow: float = 0.28
    true_break_imbalance: float = 0.28
    book_window_sec: int = 60
    book_coverage_min: float = 0.90
    book_votes_min: int = 2
    robust_quantile_low: float = 0.25
    robust_quantile_high: float = 0.75
    robust_iqr_normalizer: float = 1.3489795003921634
    regime_trend_ret_1800_bps: float = 15.0
    regime_up_position_min: float = 0.72
    regime_down_position_max: float = 0.28

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _liquidity_rules(rules: V14CandidateRules) -> LiquidityV2Rules:
    """Map the frozen V14 skeleton to the shared production feature builder."""

    return LiquidityV2Rules(
        normal_window_sec=rules.normal_window_sec,
        horizon_sec=600,
        min_gap_sec=600,
        z_entry=rules.z_entry,
        z_reclaim=rules.z_reclaim,
        retest_sec=rules.retest_sec,
        inside_min=rules.inside_min,
        observed_min_pct=rules.observed_min_pct,
        center_slope_sec=rules.center_slope_sec,
        center_slope_max_bps=rules.center_slope_max_bps,
        sigma_min_bps=rules.sigma_min_bps,
        sigma_max_bps=rules.sigma_max_bps,
        sigma_expand_max=rules.sigma_expand_max,
        orderbook_max_age_sec=rules.orderbook_max_age_sec,
        ob_imbalance_min=rules.ob_imbalance_min,
        micro_min_bps=rules.micro_min_bps,
        wall_ratio_min=rules.wall_ratio_min,
        flow_guard=rules.flow_guard,
        true_break_flow=rules.true_break_flow,
        true_break_imbalance=rules.true_break_imbalance,
        bidwall_trap_enabled=False,
        quality_v2_enabled=False,
        trend_space_enabled=False,
        mode="reclaim",
    )


def _require_columns(frame: pd.DataFrame, columns: Iterable[str]) -> None:
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise ValueError(f"V14 input is missing columns: {missing}")
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("V14 input requires a DatetimeIndex")
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError("V14 input index must be unique and increasing")


def _book_confirmation_features(
    data: pd.DataFrame,
    features: pd.DataFrame,
    rules: V14CandidateRules,
) -> None:
    window = int(rules.book_window_sec)
    minimum = max(10, int(np.ceil(window * rules.book_coverage_min)))
    available = data["ob_available"].fillna(False).astype(bool)
    buy = pd.to_numeric(data["buy_qty"], errors="coerce").fillna(0.0)
    sell = pd.to_numeric(data["sell_qty"], errors="coerce").fillna(0.0)
    buy_sum = buy.rolling(window, min_periods=minimum).sum()
    sell_sum = sell.rolling(window, min_periods=minimum).sum()
    features["book_flow"] = (buy_sum - sell_sum) / (buy_sum + sell_sum).replace(0.0, np.nan)
    features["book_imbalance"] = (
        pd.to_numeric(data["imbalance_20"], errors="coerce")
        .where(available)
        .rolling(window, min_periods=minimum)
        .mean()
    )
    features["book_micro_bps"] = (
        pd.to_numeric(data["microprice_edge_bps"], errors="coerce")
        .where(available)
        .rolling(window, min_periods=minimum)
        .mean()
    )
    features["book_coverage"] = available.astype(float).rolling(
        window,
        min_periods=minimum,
    ).mean()
    features["book_votes_up"] = (
        features["book_flow"].gt(0.0).astype(int)
        + features["book_imbalance"].gt(0.0).astype(int)
        + features["book_micro_bps"].gt(0.0).astype(int)
    )
    features["book_votes_down"] = (
        features["book_flow"].lt(0.0).astype(int)
        + features["book_imbalance"].lt(0.0).astype(int)
        + features["book_micro_bps"].lt(0.0).astype(int)
    )


def _regime_labels(features: pd.DataFrame, rules: V14CandidateRules) -> pd.Series:
    ret = pd.to_numeric(features["ret_1800s_bps"], errors="coerce")
    position = pd.to_numeric(features["pos_1800s"], errors="coerce")
    values = np.select(
        [
            ret.ge(rules.regime_trend_ret_1800_bps)
            & position.ge(rules.regime_up_position_min),
            ret.le(-rules.regime_trend_ret_1800_bps)
            & position.le(rules.regime_down_position_max),
        ],
        ["uptrend", "downtrend"],
        default="range",
    )
    labels = pd.Series(values, index=features.index, dtype="object")
    labels.loc[ret.isna() | position.isna()] = "unknown"
    return labels


def build_core_features(
    data: pd.DataFrame,
    rules: V14CandidateRules = V14CandidateRules(),
) -> pd.DataFrame:
    """Build causal VWAP/dispersion features for Core and Regime."""

    _require_columns(
        data,
        (
            "close", "volume", "buy_qty", "sell_qty", "observed",
            "bid_qty_20", "ask_qty_20", "imbalance_5", "imbalance_20",
            "microprice_edge_bps", "spread_bps", "bid_wall_qty",
            "ask_wall_qty", "ob_available", "ob_age_sec",
        ),
    )
    features = build_features(data, _liquidity_rules(rules))
    _book_confirmation_features(data, features, rules)
    features["regime"] = _regime_labels(features, rules)
    return features


def build_robust_features(
    data: pd.DataFrame,
    rules: V14CandidateRules = V14CandidateRules(),
) -> pd.DataFrame:
    """Replace Gaussian centre/scale with trailing empirical robust estimates."""

    features = build_core_features(data, rules)
    close = pd.to_numeric(data["close"], errors="coerce").astype(float)
    observed = data["observed"].fillna(False).astype(bool).astype(float)
    window = int(rules.normal_window_sec)
    minimum = max(120, window // 3)
    rolling = close.rolling(window, min_periods=minimum)
    center = rolling.median()
    low = rolling.quantile(rules.robust_quantile_low)
    high = rolling.quantile(rules.robust_quantile_high)
    scale = (high - low) / float(rules.robust_iqr_normalizer)
    scale = scale.where(scale > 1e-9)
    z = (close - center) / scale
    sigma_bps = scale / close * 10_000.0
    sigma_reference = sigma_bps.rolling(
        max(window, 900),
        min_periods=minimum,
    ).median()

    features["center"] = center
    features["sigma"] = scale
    features["z"] = z
    features["normal_low"] = center - scale
    features["normal_high"] = center + scale
    features["inside1_ratio"] = z.abs().le(1.0).astype(float).rolling(
        window,
        min_periods=minimum,
    ).mean()
    features["observed_pct"] = observed.rolling(
        min(600, window),
        min_periods=min(120, minimum),
    ).mean() * 100.0
    features["center_slope_bps"] = (
        center / center.shift(rules.center_slope_sec) - 1.0
    ) * 10_000.0
    features["sigma_bps"] = sigma_bps
    features["sigma_expand"] = sigma_bps / sigma_reference.replace(0.0, np.nan)
    features["z_max_retest"] = z.rolling(rules.retest_sec, min_periods=10).max()
    features["z_min_retest"] = z.rolling(rules.retest_sec, min_periods=10).min()
    features["regime"] = _regime_labels(features, rules)
    return features


def _finite(series: pd.Series) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    return values.notna() & np.isfinite(values)


def _candidate_masks(
    features: pd.DataFrame,
    rules: V14CandidateRules,
    *,
    apply_regime_gate: bool,
) -> tuple[pd.Series, pd.Series]:
    finite_columns = (
        "z", "inside1_ratio", "observed_pct", "center_slope_bps",
        "sigma_bps", "sigma_expand", "flow_60", "imbalance_20",
        "micro_bps", "bid_qty_20", "ask_qty_20", "book_coverage",
        "book_flow", "book_imbalance", "book_micro_bps",
    )
    finite = pd.Series(True, index=features.index)
    for column in finite_columns:
        finite &= _finite(features[column])

    ready = (
        finite
        & features["ob_available"].fillna(False).astype(bool)
        & pd.to_numeric(features["ob_age_sec"], errors="coerce").le(rules.orderbook_max_age_sec)
        & pd.to_numeric(features["inside1_ratio"], errors="coerce").ge(rules.inside_min)
        & pd.to_numeric(features["observed_pct"], errors="coerce").ge(rules.observed_min_pct)
        & pd.to_numeric(features["center_slope_bps"], errors="coerce").abs().le(rules.center_slope_max_bps)
        & pd.to_numeric(features["sigma_bps"], errors="coerce").between(
            rules.sigma_min_bps,
            rules.sigma_max_bps,
        )
        & pd.to_numeric(features["sigma_expand"], errors="coerce").le(rules.sigma_expand_max)
        & pd.to_numeric(features["book_coverage"], errors="coerce").ge(rules.book_coverage_min)
    )

    z = pd.to_numeric(features["z"], errors="coerce")
    flow = pd.to_numeric(features["flow_60"], errors="coerce")
    imbalance = pd.to_numeric(features["imbalance_20"], errors="coerce")
    micro = pd.to_numeric(features["micro_bps"], errors="coerce")
    bid = pd.to_numeric(features["bid_qty_20"], errors="coerce")
    ask = pd.to_numeric(features["ask_qty_20"], errors="coerce")
    resistance = (
        imbalance.le(-rules.ob_imbalance_min)
        & micro.le(-rules.micro_min_bps)
        & ask.ge(bid.clip(lower=1e-9) * rules.wall_ratio_min)
        & pd.to_numeric(features["ask20_chg_30"], errors="coerce").gt(-0.55)
    )
    support = (
        imbalance.ge(rules.ob_imbalance_min)
        & micro.ge(rules.micro_min_bps)
        & bid.ge(ask.clip(lower=1e-9) * rules.wall_ratio_min)
        & pd.to_numeric(features["bid20_chg_30"], errors="coerce").gt(-0.55)
    )
    true_up = (
        flow.ge(rules.true_break_flow)
        | imbalance.ge(rules.true_break_imbalance)
        | micro.ge(rules.micro_min_bps * 4.0)
    )
    true_down = (
        flow.le(-rules.true_break_flow)
        | imbalance.le(-rules.true_break_imbalance)
        | micro.le(-rules.micro_min_bps * 4.0)
    )
    down = (
        ready
        & pd.to_numeric(features["z_max_retest"], errors="coerce").ge(rules.z_entry)
        & z.between(0.0, rules.z_reclaim)
        & resistance
        & flow.le(rules.flow_guard)
        & ~true_up
        & pd.to_numeric(features["book_votes_down"], errors="coerce").ge(rules.book_votes_min)
    )
    up = (
        ready
        & pd.to_numeric(features["z_min_retest"], errors="coerce").le(-rules.z_entry)
        & z.between(-rules.z_reclaim, 0.0)
        & support
        & flow.ge(-rules.flow_guard)
        & ~true_down
        & pd.to_numeric(features["book_votes_up"], errors="coerce").ge(rules.book_votes_min)
    )
    if apply_regime_gate:
        # Recompute labels from the supplied rule set so the regime threshold
        # actually participates in one-at-a-time neighbourhood tests.
        regime = _regime_labels(features, rules)
        down &= regime.ne("uptrend")
        up &= regime.ne("downtrend")
    return up.fillna(False), down.fillna(False)


def generate_candidates_from_features(
    features: pd.DataFrame,
    candidate: str,
    rules: V14CandidateRules = V14CandidateRules(),
) -> pd.DataFrame:
    """Generate every raw opportunity; validation applies the shared cooldown."""

    if candidate not in CANDIDATE_NAMES:
        raise ValueError(f"unknown V14 candidate {candidate!r}")
    up, down = _candidate_masks(
        features,
        rules,
        apply_regime_gate=candidate == "V14-Regime",
    )
    selected = up | down
    columns = [
        "z", "inside1_ratio", "observed_pct", "center_slope_bps",
        "sigma_bps", "sigma_expand", "flow_60", "imbalance_20",
        "micro_bps", "book_coverage", "book_flow", "book_imbalance",
        "book_micro_bps", "book_votes_up", "book_votes_down", "regime",
        "ret_600s_bps", "ret_1800s_bps", "pos_1800s",
    ]
    out = features.loc[selected, columns].copy().reset_index(names="time")
    if out.empty:
        return pd.DataFrame(columns=[
            "time", "signal", "family", "strategy_id", "branch", "reason", "priority",
            *columns,
        ])
    up_values = up.loc[selected].to_numpy(bool)
    out["signal"] = np.where(up_values, "UP", "DOWN")
    out["family"] = candidate
    out["strategy_id"] = {
        "V14-Core": "BTC_10min_SECOND_REVERSAL_V14_CORE_SHADOW",
        "V14-Robust": "BTC_10min_SECOND_REVERSAL_V14_ROBUST_SHADOW",
        "V14-Regime": "BTC_10min_SECOND_REVERSAL_V14_REGIME_SHADOW",
    }[candidate]
    out["branch"] = {
        "V14-Core": "core_reclaim_2of3",
        "V14-Robust": "robust_quantile_reclaim_2of3",
        "V14-Regime": "core_reclaim_regime_2of3",
    }[candidate]
    out["reason"] = np.where(
        up_values,
        "lower_excursion_reclaim_with_support",
        "upper_excursion_reclaim_with_resistance",
    )
    out["priority"] = 0
    return out.sort_values("time", kind="stable").reset_index(drop=True)


def generate_candidates(
    data: pd.DataFrame,
    candidate: str,
    rules: V14CandidateRules = V14CandidateRules(),
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build the appropriate feature family and return candidates plus features."""

    if candidate == "V14-Robust":
        features = build_robust_features(data, rules)
    elif candidate in {"V14-Core", "V14-Regime"}:
        features = build_core_features(data, rules)
    else:
        raise ValueError(f"unknown V14 candidate {candidate!r}")
    return generate_candidates_from_features(features, candidate, rules), features


NEIGHBORHOOD_FIELDS = (
    "z_entry",
    "z_reclaim",
    "inside_min",
    "center_slope_max_bps",
    "sigma_min_bps",
    "sigma_max_bps",
    "sigma_expand_max",
    "ob_imbalance_min",
    "micro_min_bps",
    "wall_ratio_min",
    "flow_guard",
    "true_break_flow",
    "true_break_imbalance",
    "book_coverage_min",
)


def parameter_neighborhood(
    rules: V14CandidateRules = V14CandidateRules(),
    *,
    candidate: str,
    percentages: Iterable[float] = (-0.20, -0.10, 0.10, 0.20),
) -> list[tuple[str, V14CandidateRules]]:
    """Return baseline and one-at-a-time ±10/±20% threshold perturbations."""

    if candidate not in CANDIDATE_NAMES:
        raise ValueError(f"unknown V14 candidate {candidate!r}")
    fields = list(NEIGHBORHOOD_FIELDS)
    if candidate == "V14-Regime":
        fields.append("regime_trend_ret_1800_bps")
    variants: list[tuple[str, V14CandidateRules]] = [("baseline", rules)]
    for field in fields:
        base = float(getattr(rules, field))
        for percentage in percentages:
            value = base * (1.0 + float(percentage))
            label = f"{field}:{percentage:+.0%}"
            variants.append((label, replace(rules, **{field: value})))
    return variants


def candidate_metadata(
    candidate: str,
    rules: V14CandidateRules = V14CandidateRules(),
) -> dict[str, Any]:
    if candidate not in CANDIDATE_NAMES:
        raise ValueError(f"unknown V14 candidate {candidate!r}")
    return {
        "candidate": candidate,
        "policy": dict(SHADOW_ONLY_POLICY),
        "rules": rules.to_dict(),
        "supplementBranches": False,
        "addBackBranches": False,
        "featureFamily": "robust_empirical" if candidate == "V14-Robust" else "vwap_dispersion",
        "regimeGate": candidate == "V14-Regime",
    }
