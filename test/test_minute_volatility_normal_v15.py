from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_minute_volatility_normal_v15 import (  # noqa: E402
    NormalProfile,
    PROFILES,
    apply_shared_cooldown,
    build_normal_features,
    build_volatility_states,
    generate_profile_candidates,
    load_minutes,
    select_profile,
)


START = pd.Timestamp("2026-01-01T00:00:00Z")


def _minutes(rows: int = 12_000) -> pd.DataFrame:
    rng = np.random.default_rng(17)
    returns = rng.normal(0.0, 0.00025, rows)
    returns[4_000:8_000] *= 0.45
    returns[8_000:] *= 1.8
    close = 100.0 * np.exp(np.cumsum(returns))
    index = pd.date_range(START, periods=rows, freq="1min")
    return pd.DataFrame(
        {
            "open": np.r_[100.0, close[:-1]],
            "high": close * 1.0002,
            "low": close * 0.9998,
            "close": close,
            "volume": 1.0,
            "market": "futures",
        },
        index=index,
    )


def test_volatility_states_are_unchanged_when_future_is_removed() -> None:
    minutes = _minutes()
    cutoff = minutes.index[10_500]
    full = build_volatility_states(minutes).loc[:cutoff]
    truncated = build_volatility_states(minutes.loc[:cutoff])
    assert_frame_equal(full, truncated, check_exact=True)


def test_normal_features_use_only_prior_and_current_minutes() -> None:
    minutes = _minutes(1_000)
    cutoff = minutes.index[800]
    profile = NormalProfile("edge_test", 60, "edge", 1.25)
    full = build_normal_features(minutes, profile).loc[:cutoff]
    truncated = build_normal_features(minutes.loc[:cutoff], profile)
    assert_frame_equal(full, truncated, check_exact=True)


def test_generated_trade_enters_next_open_and_settles_ten_minutes_later() -> None:
    minutes = _minutes()
    # Force an extreme completed candle at a ten-minute boundary.
    position = 10_009
    minutes.iloc[position, minutes.columns.get_loc("close")] *= 0.97
    volatility = build_volatility_states(minutes)
    profile = NormalProfile("edge_test", 30, "edge", 1.25, inside_min=0.0, inside_max=1.0)
    candidates = generate_profile_candidates(minutes, volatility, profile)
    row = candidates.loc[candidates["signal_bar_time"].eq(minutes.index[position])].iloc[0]

    assert row["signal"] == "UP"
    assert row["signal_time"] == minutes.index[position] + pd.Timedelta(minutes=1)
    assert row["entry_time_d0"] == minutes.index[position + 1]
    assert row["settle_time_d0"] == minutes.index[position + 11]
    assert row["entry_time_d1"] == minutes.index[position + 2]
    assert row["settle_time_d1"] == minutes.index[position + 12]


def test_shared_cooldown_allows_exact_ten_minute_boundary() -> None:
    frame = pd.DataFrame(
        {
            "signal_time": [START, START + pd.Timedelta(minutes=9), START + pd.Timedelta(minutes=10)],
            "profile": ["a", "b", "c"],
        }
    )
    kept = apply_shared_cooldown(frame)
    assert list(kept["profile"]) == ["a", "c"]


def _selection_rows(holdout_wins_for_b: bool) -> pd.DataFrame:
    rows = []
    for profile in ("a", "b"):
        for index in range(50):
            won = index < (32 if profile == "a" else 29)
            rows.append(
                {
                    "profile": profile,
                    "vol_state": "low",
                    "signal_time": START + pd.Timedelta(minutes=10 * index),
                    "status_d0": "won" if won else "lost",
                    "pnl_u_d0": 4.0 if won else -5.0,
                }
            )
        for index in range(20):
            won = holdout_wins_for_b if profile == "b" else False
            rows.append(
                {
                    "profile": profile,
                    "vol_state": "low",
                    "signal_time": START + pd.Timedelta(days=2, minutes=10 * index),
                    "status_d0": "won" if won else "lost",
                    "pnl_u_d0": 4.0 if won else -5.0,
                }
            )
    return pd.DataFrame(rows)


def test_profile_selection_cannot_see_rows_after_train_end() -> None:
    train_end = START + pd.Timedelta(days=2)
    first = select_profile(_selection_rows(False), "low", train_end)
    mutated_future = select_profile(_selection_rows(True), "low", train_end)
    assert first == mutated_future
    assert first is not None
    assert first["profile"] == "a"


def test_loader_rejects_unlabelled_or_spot_minutes(tmp_path: Path) -> None:
    raw = _minutes(10).reset_index(names="open_time")
    spot = tmp_path / "spot.csv"
    raw.assign(market="spot").to_csv(spot, index=False)
    with pytest.raises(ValueError, match="only futures"):
        load_minutes(spot)


def test_candidate_grid_is_small_and_predeclared() -> None:
    assert len(PROFILES) == 18
    assert {profile.mode for profile in PROFILES} == {"edge", "reclaim"}
    assert {profile.window_min for profile in PROFILES} == {30, 60, 120}
    assert {profile.z_entry for profile in PROFILES} == {1.25, 1.75, 2.25}
