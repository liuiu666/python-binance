from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_stationarity_router_v19 import (  # noqa: E402
    PROFILES,
    _bootstrap_block_ev,
    _profile_allowed,
)
from stationarity_features_v19 import build_stationarity_features  # noqa: E402


START = pd.Timestamp("2026-01-01T00:00:00Z")


def _minutes(close: np.ndarray) -> pd.DataFrame:
    index = pd.date_range(START, periods=len(close), freq="1min")
    return pd.DataFrame(
        {
            "open": np.r_[close[0], close[:-1]],
            "high": close * 1.0002,
            "low": close * 0.9998,
            "close": close,
            "volume": 1.0,
            "market": "futures",
        },
        index=index,
    )


def test_stationarity_features_are_causal_under_future_truncation() -> None:
    rng = np.random.default_rng(1919)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.0003, 4_000)))
    minutes = _minutes(close)
    cutoff = minutes.index[3_500]
    full = build_stationarity_features(minutes).loc[:cutoff]
    truncated = build_stationarity_features(minutes.loc[:cutoff])
    assert_frame_equal(full, truncated, check_exact=True)


def test_stationarity_state_detects_synthetic_mean_reversion() -> None:
    rng = np.random.default_rng(1920)
    residual = np.zeros(5_000)
    for index in range(1, len(residual)):
        residual[index] = 0.82 * residual[index - 1] + rng.normal(0.0, 0.001)
    features = build_stationarity_features(_minutes(100.0 * np.exp(residual)))
    assert int(features["structure_state"].eq("revertible").sum()) > 1_000


def test_stationarity_state_detects_synthetic_trend() -> None:
    rng = np.random.default_rng(1921)
    returns = 0.00035 + rng.normal(0.0, 0.00008, 5_000)
    close = 100.0 * np.exp(np.cumsum(returns))
    features = build_stationarity_features(_minutes(close))
    assert int(features["structure_state"].eq("trend").sum()) > 1_000


def test_v19_routes_only_pre_registered_actions() -> None:
    edge = next(profile for profile in PROFILES if profile.family == "normal_edge_reversion")
    reclaim = next(
        profile for profile in PROFILES if profile.family == "normal_confirmed_reversal"
    )
    momentum = next(profile for profile in PROFILES if profile.family == "trend_continuation")
    exhaustion = next(
        profile for profile in PROFILES if profile.family == "trend_exhaustion_reversal"
    )
    assert not _profile_allowed(edge, "high", "revertible")
    assert _profile_allowed(reclaim, "high", "revertible")
    assert _profile_allowed(momentum, "low", "trend")
    assert not _profile_allowed(momentum, "high", "trend")
    assert _profile_allowed(exhaustion, "high", "trend")


def test_block_bootstrap_is_deterministic_and_uses_true_seven_day_blocks() -> None:
    times = pd.date_range(START, periods=70, freq="1D")
    frame = pd.DataFrame({"signal_time": times, "pnl": 4.0})
    first = _bootstrap_block_ev(frame, "pnl", seed_key="same")
    second = _bootstrap_block_ev(frame, "pnl", seed_key="same")
    assert first == second
    assert first["blocks"] == 10
    assert first["lower90EvU"] == 4.0
    assert first["probabilityEvNonPositive"] == 0.0

