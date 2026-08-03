from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_long_history_walkforward_v20 import (  # noqa: E402
    month_folds,
    training_summary,
    training_window,
)


def test_month_folds_keep_partial_tail_explicit() -> None:
    index = pd.date_range(
        "2024-01-01T00:00:00Z", "2024-08-15T00:00:00Z", freq="1min"
    )
    folds = month_folds(index)
    assert folds[0][0] == "2024-04"
    assert folds[-1][0] == "2024-08_partial"
    assert folds[-1][2] == index[-1] + pd.Timedelta(minutes=1)


def test_training_modes_are_calendar_aligned() -> None:
    test_start = pd.Timestamp("2024-07-01T00:00:00Z")
    data_start = pd.Timestamp("2024-01-01T00:00:00Z")
    start, months = training_window("rolling_3m", test_start, data_start)
    assert start == pd.Timestamp("2024-04-01T00:00:00Z")
    assert months == ["2024-04", "2024-05", "2024-06"]
    start, months = training_window("expanding", test_start, data_start)
    assert start == data_start
    assert months == [
        "2024-01",
        "2024-02",
        "2024-03",
        "2024-04",
        "2024-05",
        "2024-06",
    ]


def test_training_summary_keeps_zero_trade_month_in_denominator() -> None:
    frame = pd.DataFrame(
        {
            "signal_time": [
                pd.Timestamp("2024-01-10T00:00:00Z"),
                pd.Timestamp("2024-03-10T00:00:00Z"),
            ],
            "status_h10_d0": ["won", "won"],
            "pnl_u_h10_d0": [4.0, 4.0],
        }
    )
    summary = training_summary(
        frame,
        "exact",
        ["2024-01", "2024-02", "2024-03"],
        seed_key="zero-month",
    )
    assert summary["monthlyTrades"] == {
        "2024-01": 1,
        "2024-02": 0,
        "2024-03": 1,
    }
    assert summary["positiveMonthPctFixedDenominator"] == 66.6667

