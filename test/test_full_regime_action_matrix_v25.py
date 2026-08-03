from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_full_regime_action_matrix_v25 import (  # noqa: E402
    CONFIRMED_REVERSAL,
    DIRECT_REVERSION,
    EXHAUSTION_REVERSAL,
    FAMILIES,
    HORIZONS_MIN,
    LOOKBACKS_MIN,
    NO_TRADE,
    PROFILES,
    STATES,
    THRESHOLDS,
    TREND_CONTINUATION,
    _family_signal_arrays,
    apply_horizon_cooldown,
    generate_candidate_matrix,
    select_walkforward_variant,
)


START = pd.Timestamp("2024-01-01T00:00:00Z")


def _minutes(rows: int = 360) -> pd.DataFrame:
    rng = np.random.default_rng(2525)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.0004, rows)))
    index = pd.date_range(START, periods=rows, freq="1min")
    return pd.DataFrame(
        {
            "open": np.r_[100.0, close[:-1]],
            "high": close * 1.0002,
            "low": close * 0.9998,
            "close": close,
            "volume": 1.0,
        },
        index=index,
    )


def test_grid_compares_every_family_window_threshold_in_every_state() -> None:
    expected = len(FAMILIES) * len(LOOKBACKS_MIN) * len(THRESHOLDS)
    assert len(PROFILES) == expected
    assert {profile.family for profile in PROFILES} == set(FAMILIES)
    for family in FAMILIES:
        family_profiles = [profile for profile in PROFILES if profile.family == family]
        assert {profile.lookback_min for profile in family_profiles} == set(LOOKBACKS_MIN)
        assert {profile.threshold for profile in family_profiles} == set(THRESHOLDS)
    assert set(STATES) == {"low", "mid", "high"}
    assert NO_TRADE not in FAMILIES


def test_signal_features_are_causal_when_future_minutes_are_removed() -> None:
    minutes = _minutes()
    position = 249
    full = _family_signal_arrays(minutes, 60, np.array([position]))
    truncated = _family_signal_arrays(
        minutes.iloc[: position + 1], 60, np.array([position])
    )
    assert full.keys() == truncated.keys()
    for key in full:
        for full_value, truncated_value in zip(full[key], truncated[key]):
            np.testing.assert_allclose(
                full_value, truncated_value, equal_nan=True, rtol=0.0, atol=0.0
            )


def test_direct_reversion_and_continuation_point_in_opposite_directions() -> None:
    minutes = _minutes()
    position = 249
    base = float(minutes["close"].iloc[position - 30])
    minutes.iloc[position - 29: position + 1, minutes.columns.get_loc("close")] = (
        base * np.exp(np.linspace(0.002, 0.060, 30))
    )
    signals = _family_signal_arrays(minutes, 30, np.array([position]))
    mean_up, mean_down, _ = signals[f"{DIRECT_REVERSION}|1.5"]
    trend_up, trend_down, _ = signals[f"{TREND_CONTINUATION}|1.5"]
    assert not bool(mean_up[0])
    assert bool(mean_down[0])
    assert bool(trend_up[0])
    assert not bool(trend_down[0])


def test_candidate_execution_uses_next_open_and_full_delayed_horizon() -> None:
    minutes = _minutes(320)
    minutes["open"] = 100.0 + np.arange(len(minutes), dtype=float) * 0.1
    position = 249  # 04:09 completed bar -> 04:10 boundary.
    minutes.iloc[position, minutes.columns.get_loc("close")] *= 0.85
    volatility = pd.DataFrame(
        {"vol_state": "mid", "rv10m_bps": 5.0}, index=minutes.index
    )
    candidates = generate_candidate_matrix(minutes, volatility)
    profile = "v25_meanrev_w30_s1p5"
    row = candidates.loc[
        candidates["profile"].astype(str).eq(profile)
        & candidates["signal_pos"].eq(position)
    ].iloc[0]
    assert int(row["direction"]) == 1
    for horizon in HORIZONS_MIN:
        exact = (
            minutes["open"].iloc[position + 1 + horizon]
            / minutes["open"].iloc[position + 1]
            - 1.0
        ) * 10_000.0
        delayed = (
            minutes["open"].iloc[position + 2 + horizon]
            / minutes["open"].iloc[position + 2]
            - 1.0
        ) * 10_000.0
        assert np.isclose(row[f"signed_bps_h{horizon}_d0"], exact, rtol=1e-6)
        assert np.isclose(row[f"signed_bps_h{horizon}_d1"], delayed, rtol=1e-6)


def test_horizon_cooldown_prevents_overlapping_twenty_minute_positions() -> None:
    frame = pd.DataFrame(
        {"signal_pos": [9, 19, 29, 39], "profile": ["p"] * 4}
    )
    kept = apply_horizon_cooldown(frame, 20)
    assert kept["signal_pos"].tolist() == [9, 29]
    assert apply_horizon_cooldown(frame, 10)["signal_pos"].tolist() == [9, 19, 29, 39]


def _monthly_selection_fixture(future_winner: str) -> pd.DataFrame:
    profiles = ("v25_meanrev_w30_s1p5", "v25_meanrev_w30_s2p0")
    rows = []
    for month in pd.period_range("2024-01", "2024-07", freq="M"):
        key = month.strftime("%Y-%m")
        for profile in profiles:
            for execution in ("exact", "delayed"):
                training = key != "2024-07"
                wins = 3 if training else (4 if profile == future_winner else 0)
                losses = 1 if training else (0 if profile == future_winner else 4)
                rows.append(
                    {
                        "month": key,
                        "vol_state": "mid",
                        "profile": profile,
                        "family": DIRECT_REVERSION,
                        "lookback_min": 30,
                        "threshold": 1.5 if profile.endswith("1p5") else 2.0,
                        "horizon_min": 10,
                        "execution": execution,
                        "trades": wins + losses,
                        "wins": wins,
                        "losses": losses,
                        "ties": 0,
                        "pnlU": wins * 4.0 - losses * 5.0,
                    }
                )
    return pd.DataFrame(rows)


def test_walkforward_selection_cannot_see_test_month() -> None:
    training_months = [f"2024-{month:02d}" for month in range(1, 7)]
    first = select_walkforward_variant(
        _monthly_selection_fixture("v25_meanrev_w30_s1p5"),
        "mid",
        training_months,
    )
    mutated = select_walkforward_variant(
        _monthly_selection_fixture("v25_meanrev_w30_s2p0"),
        "mid",
        training_months,
    )
    assert first == mutated
    assert first is not None
    assert first["parameterSupportCount"] == 2


def test_walkforward_returns_explicit_no_trade_when_training_is_weak() -> None:
    weak = _monthly_selection_fixture("v25_meanrev_w30_s1p5")
    weak.loc[weak["month"].ne("2024-07"), "pnlU"] = -5.0
    weak.loc[weak["month"].ne("2024-07"), ["wins", "losses"]] = [0, 4]
    selection = select_walkforward_variant(
        weak, "mid", [f"2024-{month:02d}" for month in range(1, 7)]
    )
    assert selection is None


def test_all_four_family_names_are_distinct() -> None:
    assert len({
        DIRECT_REVERSION,
        CONFIRMED_REVERSAL,
        TREND_CONTINUATION,
        EXHAUSTION_REVERSAL,
    }) == 4
