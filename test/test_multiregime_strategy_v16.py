from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal, assert_series_equal


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_multiregime_strategy_v16 import (  # noqa: E402
    AMOUNT_U,
    HORIZONS_MIN,
    PAYOUT_RATE,
    PROFILES,
    StrategyProfile,
    _score_return,
    _signal_masks,
    build_normal_features,
    generate_candidates,
    metrics,
    select_for_state,
)


START = pd.Timestamp("2026-01-01T00:00:00Z")
NORMAL_FAMILIES = {"normal_edge_reversion", "normal_confirmed_reversal"}


def _minutes(rows: int = 420) -> pd.DataFrame:
    rng = np.random.default_rng(1616)
    returns = rng.normal(0.0, 0.0003, rows)
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


def _raw_signal_features(minutes: pd.DataFrame, profile: StrategyProfile) -> pd.DataFrame:
    if profile.family in NORMAL_FAMILIES:
        compatible_profile = SimpleNamespace(
            window_min=profile.lookback_min,
            retest_min=max(5, profile.lookback_min // 3),
        )
        return build_normal_features(minutes, compatible_profile)
    return _score_return(minutes, profile.lookback_min)


@pytest.mark.parametrize("profile", PROFILES, ids=lambda profile: profile.name)
def test_all_signal_features_are_unchanged_when_future_is_removed(
    profile: StrategyProfile,
) -> None:
    minutes = _minutes()
    cutoff = minutes.index[320]

    full_features = _raw_signal_features(minutes, profile).loc[:cutoff]
    truncated_features = _raw_signal_features(minutes.loc[:cutoff], profile)
    assert_frame_equal(full_features, truncated_features, check_exact=True)

    full_up, full_down, full_diagnostics = _signal_masks(minutes, profile)
    truncated_up, truncated_down, truncated_diagnostics = _signal_masks(
        minutes.loc[:cutoff], profile
    )
    assert_series_equal(full_up.loc[:cutoff], truncated_up, check_exact=True)
    assert_series_equal(full_down.loc[:cutoff], truncated_down, check_exact=True)
    assert_frame_equal(
        full_diagnostics.loc[:cutoff], truncated_diagnostics, check_exact=True
    )


def test_normal_edge_reversion_points_back_toward_the_mean() -> None:
    profile = StrategyProfile("edge_test", "normal_edge_reversion", 30, 1.5)

    below_mean = _minutes(240)
    below_mean.iloc[-1, below_mean.columns.get_loc("close")] *= 0.90
    up, down, _ = _signal_masks(below_mean, profile)
    assert bool(up.iloc[-1])
    assert not bool(down.iloc[-1])

    above_mean = _minutes(240)
    above_mean.iloc[-1, above_mean.columns.get_loc("close")] *= 1.10
    up, down, _ = _signal_masks(above_mean, profile)
    assert not bool(up.iloc[-1])
    assert bool(down.iloc[-1])


def test_momentum_continuation_follows_the_move_direction() -> None:
    profile = StrategyProfile("momentum_test", "trend_continuation", 10, 1.0)

    rising = _minutes(240)
    rising.iloc[-1, rising.columns.get_loc("close")] *= 1.08
    up, down, _ = _signal_masks(rising, profile)
    assert bool(up.iloc[-1])
    assert not bool(down.iloc[-1])

    falling = _minutes(240)
    falling.iloc[-1, falling.columns.get_loc("close")] *= 0.92
    up, down, _ = _signal_masks(falling, profile)
    assert not bool(up.iloc[-1])
    assert bool(down.iloc[-1])


def _exhaustion_minutes(with_reverse_confirmation: bool) -> pd.DataFrame:
    minutes = _minutes(200)
    close = np.full(len(minutes), 100.0)
    trend_start = len(close) - 31
    for position in range(trend_start, len(close)):
        close[position] = 100.0 * np.exp(0.006 * (position - trend_start + 1))
    if with_reverse_confirmation:
        close[-3:] = (116.0, 114.0, 112.0)
    minutes["close"] = close
    return minutes


def test_exhaustion_reversal_requires_countermove_confirmation() -> None:
    profile = StrategyProfile("exhaustion_test", "trend_exhaustion_reversal", 30, 1.5)

    continuing = _exhaustion_minutes(with_reverse_confirmation=False)
    up, down, diagnostics = _signal_masks(continuing, profile)
    assert diagnostics["structure_score"].iloc[-1] >= profile.threshold
    assert not bool(up.iloc[-1])
    assert not bool(down.iloc[-1])

    reversing = _exhaustion_minutes(with_reverse_confirmation=True)
    up, down, diagnostics = _signal_masks(reversing, profile)
    assert diagnostics["structure_score"].iloc[-1] >= profile.threshold
    assert not bool(up.iloc[-1])
    assert bool(down.iloc[-1])


def _selection_rows(future_b_wins: bool) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for month in (1, 2, 3):
        month_start = pd.Timestamp(year=2026, month=month, day=1, tz="UTC")
        for profile, monthly_wins in (("a", 18), ("b", 17)):
            for index in range(30):
                won = index < monthly_wins
                rows.append(
                    {
                        "profile": profile,
                        "family": "normal_edge_reversion",
                        "vol_state": "low",
                        "signal_time": month_start + pd.Timedelta(minutes=10 * index),
                        "settle_time_h10_d0": month_start
                        + pd.Timedelta(minutes=10 * (index + 1)),
                        "status_h10_d0": "won" if won else "lost",
                        "pnl_u_h10_d0": AMOUNT_U * PAYOUT_RATE if won else -AMOUNT_U,
                    }
                )

    future_start = pd.Timestamp("2026-04-01T00:00:00Z")
    for profile in ("a", "b"):
        for index in range(30):
            won = future_b_wins if profile == "b" else not future_b_wins
            rows.append(
                {
                    "profile": profile,
                    "family": "trend_continuation",
                    "vol_state": "low",
                    "signal_time": future_start + pd.Timedelta(minutes=10 * index),
                    "settle_time_h10_d0": future_start
                    + pd.Timedelta(minutes=10 * (index + 1)),
                    "status_h10_d0": "won" if won else "lost",
                    "pnl_u_h10_d0": AMOUNT_U * PAYOUT_RATE if won else -AMOUNT_U,
                }
            )
    return pd.DataFrame(rows)


def test_state_selection_cannot_see_rows_at_or_after_train_end() -> None:
    train_end = pd.Timestamp("2026-04-01T00:00:00Z")
    original = select_for_state(_selection_rows(False), "low", train_end)
    mutated_future = select_for_state(_selection_rows(True), "low", train_end)

    assert original == mutated_future
    assert original is not None
    assert original["profile"] == "a"
    assert original["family"] == "normal_edge_reversion"


def test_state_selection_purges_label_settling_exactly_at_train_end() -> None:
    train_end = pd.Timestamp("2026-04-01T00:00:00Z")
    original_rows = _selection_rows(False)
    boundary = original_rows.iloc[0].copy()
    boundary["profile"] = "b"
    boundary["signal_time"] = train_end - pd.Timedelta(minutes=10)
    boundary["settle_time_h10_d0"] = train_end
    boundary["status_h10_d0"] = "lost"
    boundary["pnl_u_h10_d0"] = -AMOUNT_U
    original_rows = pd.concat([original_rows, boundary.to_frame().T], ignore_index=True)

    mutated_rows = original_rows.copy()
    mutated_rows.loc[mutated_rows.index[-1], "status_h10_d0"] = "won"
    mutated_rows.loc[mutated_rows.index[-1], "pnl_u_h10_d0"] = AMOUNT_U * PAYOUT_RATE

    assert select_for_state(original_rows, "low", train_end) == select_for_state(
        mutated_rows, "low", train_end
    )


def test_candidates_align_primary_and_diagnostic_entry_and_settlement_opens() -> None:
    minutes = _minutes(260)
    minutes["open"] = 100.0 + np.arange(len(minutes), dtype=float) * 0.1
    signal_position = 189  # 03:09 candle completes the 03:10 contract boundary.
    minutes.iloc[signal_position, minutes.columns.get_loc("close")] *= 0.85
    volatility = pd.DataFrame(
        {"vol_state": "low", "rv10m_bps": 5.0},
        index=minutes.index,
    )
    profile = StrategyProfile("alignment_test", "normal_edge_reversion", 30, 1.5)

    candidates = generate_candidates(minutes, volatility, profile)
    row = candidates.loc[
        candidates["signal_bar_time"].eq(minutes.index[signal_position])
    ].iloc[0]

    assert row["signal"] == "UP"
    assert row["signal_time"] == minutes.index[signal_position + 1]
    for horizon in HORIZONS_MIN:
        for delay in (0, 1):
            entry_position = signal_position + 1 + delay
            settle_position = entry_position + horizon
            assert minutes.index[entry_position] == row["signal_time"] + pd.Timedelta(
                minutes=delay
            )
            assert minutes.index[settle_position] == row["signal_time"] + pd.Timedelta(
                minutes=horizon + delay
            )
            expected_signed_bps = (
                minutes["open"].iloc[settle_position]
                / minutes["open"].iloc[entry_position]
                - 1.0
            ) * 10_000.0
            assert row[f"signed_bps_h{horizon}_d{delay}"] == pytest.approx(
                expected_signed_bps
            )
            assert row[f"entry_time_h{horizon}_d{delay}"] == minutes.index[
                entry_position
            ]
            assert row[f"settle_time_h{horizon}_d{delay}"] == minutes.index[
                settle_position
            ]


def test_fold_metrics_exclude_labels_settling_at_or_after_fold_end() -> None:
    fold_end = START + pd.Timedelta(hours=1)
    rows = pd.DataFrame(
        {
            "signal_time": [fold_end - pd.Timedelta(minutes=20), fold_end - pd.Timedelta(minutes=10)],
            "entry_time_h10_d0": [fold_end - pd.Timedelta(minutes=20), fold_end - pd.Timedelta(minutes=10)],
            "settle_time_h10_d0": [fold_end - pd.Timedelta(minutes=10), fold_end],
            "status_h10_d0": ["won", "won"],
            "pnl_u_h10_d0": [AMOUNT_U * PAYOUT_RATE, AMOUNT_U * PAYOUT_RATE],
        }
    )

    summary = metrics(rows, 10, 0, period_end=fold_end)
    assert summary["trades"] == 1
    assert summary["wins"] == 1
