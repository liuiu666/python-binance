from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pandas.testing as pdt


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_ohlcv_absorption_router_v34 import (  # noqa: E402
    GATES,
    PROFILES,
    VOL_CELLS,
    build_ohlcv_features,
    directional_confirmation,
)
from research_full_history_stationarity_router_v32 import (  # noqa: E402
    fixed_profile_audit,
)


def _feature_rows() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "is_up_candle": [True, False],
            "is_down_candle": [False, True],
            "body_fraction": [0.6, 0.6],
            "lower_wick_fraction": [0.4, 0.0],
            "upper_wick_fraction": [0.0, 0.4],
            "close_location": [0.8, 0.2],
        }
    )


def test_directional_rejection_is_symmetric_for_up_and_down() -> None:
    features = _feature_rows()
    up = pd.Series([True, False])
    down = pd.Series([False, True])
    reversal = directional_confirmation(
        up, down, features, trend_continuation=False
    )
    trend = directional_confirmation(
        up, down, features, trend_continuation=True
    )
    assert reversal.tolist() == [True, True]
    assert trend.tolist() == [True, True]


def test_ohlcv_thresholds_do_not_change_before_future_mutation() -> None:
    rows = 5_000
    rng = np.random.default_rng(20260730)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.0002, rows)))
    index = pd.date_range("2024-01-01", periods=rows, freq="1min", tz="UTC")
    minutes = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.0002,
            "low": close * 0.9998,
            "close": close,
            "volume": rng.lognormal(0.0, 0.3, rows),
        },
        index=index,
    )
    original = build_ohlcv_features(minutes)
    altered = minutes.copy()
    altered.loc[index[4_900]:, "volume"] *= 1_000.0
    altered.loc[index[4_900]:, "high"] *= 1.2
    changed = build_ohlcv_features(altered)
    pdt.assert_series_equal(
        original["volume_threshold"].iloc[:4_900],
        changed["volume_threshold"].iloc[:4_900],
    )
    pdt.assert_series_equal(
        original["range_threshold"].iloc[:4_900],
        changed["range_threshold"].iloc[:4_900],
    )


def test_v34_frozen_profile_matrix_has_two_windows_per_family_gate() -> None:
    assert VOL_CELLS == ("low", "mid", "high")
    assert len(GATES) == 4
    assert len(PROFILES) == 32
    counts: dict[str, int] = {}
    for profile in PROFILES:
        counts[profile.family] = counts.get(profile.family, 0) + 1
    assert set(counts.values()) == {2}


def test_generic_fixed_audit_accepts_plain_volatility_cell() -> None:
    profile = PROFILES[0]
    row = {
        "cell": "low",
        "profile": profile.name,
        "family": profile.family,
        "signal_time": pd.Timestamp("2024-01-10T00:00:00Z"),
        "entry_time_h20_d1": pd.Timestamp("2024-01-10T00:01:00Z"),
        "settle_time_h20_d1": pd.Timestamp("2024-01-10T00:21:00Z"),
    }
    for execution in (
        "h5_d0",
        "h5_d1",
        "h10_d0",
        "h10_d1",
        "h10_fixed_d1",
        "h20_d0",
        "h20_d1",
    ):
        row[f"status_{execution}"] = "won"
        row[f"pnl_u_{execution}"] = 4.0
    candidates = pd.DataFrame([row])
    folds = [
        (
            "2024-01",
            "2024-01",
            pd.Timestamp("2024-01-01T00:00:00Z"),
            pd.Timestamp("2024-02-01T00:00:00Z"),
            True,
        )
    ]
    audit, _ = fixed_profile_audit(
        candidates, folds, cells=("low",), profiles=(profile,)
    )
    assert audit.iloc[0]["vol_state"] == "low"
    assert audit.iloc[0]["structure_state"] == "all"
