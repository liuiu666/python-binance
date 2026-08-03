from __future__ import annotations

import sys
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from v14_candidates import (  # noqa: E402
    CANDIDATE_NAMES,
    SHADOW_ONLY_POLICY,
    V14CandidateRules,
    build_core_features,
    build_robust_features,
    candidate_metadata,
    generate_candidates_from_features,
    parameter_neighborhood,
)


def _input_frame(periods: int = 2600) -> pd.DataFrame:
    index = pd.date_range("2026-01-01T00:00:00Z", periods=periods, freq="s")
    position = np.arange(periods, dtype=float)
    close = 100.0 + np.sin(position / 31.0) * 0.35 + position * 0.0001
    imbalance = np.sin(position / 19.0) * 0.15
    micro = imbalance * 0.02
    bid = 10.0 + np.maximum(imbalance, 0.0) * 10.0
    ask = 10.0 + np.maximum(-imbalance, 0.0) * 10.0
    buy = 1.0 + np.maximum(imbalance, 0.0)
    sell = 1.0 + np.maximum(-imbalance, 0.0)
    return pd.DataFrame(
        {
            "close": close,
            "high": close + 0.01,
            "low": close - 0.01,
            "volume": buy + sell,
            "buy_qty": buy,
            "sell_qty": sell,
            "observed": True,
            "bid_qty_20": bid,
            "ask_qty_20": ask,
            "imbalance_5": imbalance,
            "imbalance_20": imbalance,
            "microprice_edge_bps": micro,
            "spread_bps": 0.02,
            "bid_wall_qty": bid,
            "ask_wall_qty": ask,
            "ob_available": True,
            "ob_age_sec": 0.0,
        },
        index=index,
    )


def _crafted_features() -> pd.DataFrame:
    index = pd.date_range("2026-01-01T01:00:00Z", periods=2, freq="10min")
    return pd.DataFrame(
        {
            "z": [-0.20, 0.20],
            "z_min_retest": [-1.50, -0.10],
            "z_max_retest": [0.10, 1.50],
            "inside1_ratio": [0.70, 0.70],
            "observed_pct": [100.0, 100.0],
            "center_slope_bps": [0.0, 0.0],
            "sigma_bps": [10.0, 10.0],
            "sigma_expand": [1.0, 1.0],
            "flow_60": [0.05, -0.05],
            "imbalance_20": [0.12, -0.12],
            "micro_bps": [0.002, -0.002],
            "bid_qty_20": [12.0, 8.0],
            "ask_qty_20": [8.0, 12.0],
            "bid20_chg_30": [0.0, 0.0],
            "ask20_chg_30": [0.0, 0.0],
            "ob_available": [True, True],
            "ob_age_sec": [0.0, 0.0],
            "book_coverage": [1.0, 1.0],
            "book_flow": [0.10, -0.10],
            "book_imbalance": [0.10, -0.10],
            "book_micro_bps": [0.002, -0.002],
            "book_votes_up": [3, 0],
            "book_votes_down": [0, 3],
            "regime": ["range", "uptrend"],
            "ret_600s_bps": [0.0, 10.0],
            "ret_1800s_bps": [0.0, 20.0],
            "pos_1800s": [0.5, 0.9],
        },
        index=index,
    )


def test_core_and_robust_features_are_unchanged_when_future_is_removed() -> None:
    data = _input_frame()
    cutoff = data.index[2100]
    columns = [
        "center", "sigma", "z", "inside1_ratio", "center_slope_bps",
        "sigma_bps", "sigma_expand", "book_flow", "book_imbalance",
        "book_micro_bps", "book_coverage", "regime",
    ]
    for builder in (build_core_features, build_robust_features):
        full = builder(data).loc[:cutoff, columns]
        truncated = builder(data.loc[:cutoff]).loc[:, columns]
        assert_frame_equal(full, truncated, check_exact=True)


def test_candidate_skeleton_requires_reclaim_and_two_of_three_book_votes() -> None:
    features = _crafted_features()
    core = generate_candidates_from_features(features, "V14-Core")
    robust = generate_candidates_from_features(features, "V14-Robust")

    assert list(core["signal"]) == ["UP", "DOWN"]
    assert list(robust["signal"]) == ["UP", "DOWN"]
    assert set(core["branch"]) == {"core_reclaim_2of3"}
    assert set(robust["branch"]) == {"robust_quantile_reclaim_2of3"}

    low_votes = features.copy()
    low_votes.loc[low_votes.index[0], "book_votes_up"] = 1
    filtered = generate_candidates_from_features(low_votes, "V14-Core")
    assert list(filtered["signal"]) == ["DOWN"]


def test_regime_candidate_adds_only_the_fixed_countertrend_veto() -> None:
    features = _crafted_features()
    core = generate_candidates_from_features(features, "V14-Core")
    regime = generate_candidates_from_features(features, "V14-Regime")

    assert list(core["signal"]) == ["UP", "DOWN"]
    assert list(regime["signal"]) == ["UP"]
    assert regime.iloc[0]["branch"] == "core_reclaim_regime_2of3"


def test_regime_threshold_perturbation_changes_only_the_dynamic_state_gate() -> None:
    features = _crafted_features()
    features.loc[features.index[1], "ret_1800s_bps"] = 16.0
    baseline = generate_candidates_from_features(features, "V14-Regime")
    wider = generate_candidates_from_features(
        features,
        "V14-Regime",
        replace(V14CandidateRules(), regime_trend_ret_1800_bps=18.0),
    )

    assert list(baseline["signal"]) == ["UP"]
    assert list(wider["signal"]) == ["UP", "DOWN"]


def test_parameter_neighborhood_is_one_at_a_time_and_contains_10_20_percent() -> None:
    rules = V14CandidateRules()
    variants = parameter_neighborhood(rules, candidate="V14-Regime")
    labels = {label for label, _ in variants}

    assert "baseline" in labels
    assert "z_entry:-20%" in labels
    assert "z_entry:-10%" in labels
    assert "z_entry:+10%" in labels
    assert "z_entry:+20%" in labels
    assert "regime_trend_ret_1800_bps:+20%" in labels
    baseline = asdict(rules)
    for label, variant in variants[1:]:
        changed = [key for key, value in asdict(variant).items() if value != baseline[key]]
        assert changed == [label.split(":", 1)[0]]


def test_every_candidate_metadata_is_hard_shadow_only() -> None:
    assert SHADOW_ONLY_POLICY == {
        "enabled": True,
        "tradeEnabled": False,
        "observationMode": "shadow",
        "realTradingAllowed": False,
    }
    for candidate in CANDIDATE_NAMES:
        policy = candidate_metadata(candidate)["policy"]
        assert policy["tradeEnabled"] is False
        assert policy["realTradingAllowed"] is False
        assert policy["observationMode"] == "shadow"
