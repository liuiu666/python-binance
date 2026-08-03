from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_full_history_stationarity_router_v32 import (  # noqa: E402
    CELLS,
    EXECUTION_SPECS,
    MAIN_EXECUTIONS,
    NEIGHBOR_EXECUTIONS,
    PROFILES,
    TRAINING_WINDOWS_MONTHS,
    _base_profile_eligible,
    common_period_frame,
    outcome_metrics,
    validate_fold_training,
)


def test_common_period_purges_cross_month_outcome() -> None:
    frame = pd.DataFrame(
        [
            {
                "entry_time_h20_d1": "2024-02-01T00:01:00Z",
                "settle_time_h20_d1": "2024-02-01T00:21:00Z",
            },
            {
                "entry_time_h20_d1": "2024-02-29T23:41:00Z",
                "settle_time_h20_d1": "2024-03-01T00:01:00Z",
            },
        ]
    )
    result = common_period_frame(
        frame,
        pd.Timestamp("2024-02-01T00:00:00Z"),
        pd.Timestamp("2024-03-01T00:00:00Z"),
    )
    assert len(result) == 1


def test_outcome_metrics_include_zero_trade_calendar_months() -> None:
    frame = pd.DataFrame(
        {
            "signal_time": pd.to_datetime(
                ["2024-01-10T00:00:00Z", "2024-03-10T00:00:00Z"], utc=True
            ),
            "status_h10_d0": ["won", "lost"],
            "pnl_u_h10_d0": [4.0, -5.0],
        }
    )
    result = outcome_metrics(
        frame,
        "h10_d0",
        calendar_months=["2024-01", "2024-02", "2024-03"],
    )
    assert result["trades"] == 2
    assert result["pnlU"] == -1.0
    assert result["calendarMonths"] == 3
    assert result["activeMonths"] == 2
    assert result["positiveMonthPct"] == pytest.approx(100.0 / 3.0, abs=1e-4)


def _passing_row() -> dict[str, float | int]:
    return {
        "trades": 200,
        "pnlU": 100.0,
        "winRatePct": 65.0,
        "wilson95LowerPct": 58.0,
        "positiveMonthPct": 75.0,
        "worstMonthPnlU": -10.0,
        "monthsWithAtLeast20Trades": 4,
    }


def test_training_gate_requires_main_and_neighbor_horizons() -> None:
    summary = {key: _passing_row() for key in EXECUTION_SPECS}
    assert _base_profile_eligible(summary)

    failed_main = {key: dict(value) for key, value in summary.items()}
    failed_main[MAIN_EXECUTIONS[0]]["wilson95LowerPct"] = 55.0
    assert not _base_profile_eligible(failed_main)

    failed_neighbor = {key: dict(value) for key, value in summary.items()}
    failed_neighbor[NEIGHBOR_EXECUTIONS[0]]["pnlU"] = -1.0
    assert not _base_profile_eligible(failed_neighbor)


def test_fold_training_is_exact_contiguous_prior_window() -> None:
    validate_fold_training(
        "2021-01", [f"2020-{month:02d}" for month in range(1, 13)], 12
    )
    with pytest.raises(ValueError, match="non-causal"):
        validate_fold_training("2021-01", ["2020-02"] * 12, 12)


def test_v32_reuses_frozen_v19_profiles_and_cells() -> None:
    assert TRAINING_WINDOWS_MONTHS == (3, 6, 12)
    assert len(PROFILES) == 8
    assert len(CELLS) == 6
    assert {profile.lookback_min for profile in PROFILES} == {30, 60, 120}
    assert {profile.threshold for profile in PROFILES} == {1.5, 2.0}
