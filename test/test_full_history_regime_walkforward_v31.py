from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_full_history_regime_walkforward_v31 import (  # noqa: E402
    ALL_ACTIONS,
    FAMILIES,
    LOOKBACKS_MIN,
    NO_TRADE,
    STATES,
    THRESHOLDS,
    TRAINING_WINDOWS_MONTHS,
    _aggregate_monthly_metrics,
    combine_minute_frames,
    validate_causal_selections,
)


def _frame(start: str, rows: int) -> pd.DataFrame:
    index = pd.date_range(start, periods=rows, freq="1min", tz="UTC")
    values = 100.0 + np.arange(rows, dtype=float)
    return pd.DataFrame(
        {
            "open": values,
            "high": values + 1.0,
            "low": values - 1.0,
            "close": values,
            "volume": 1.0,
        },
        index=index,
    )


def _selection(month: str, training: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "month": month,
                "train_months": ",".join(training),
                "vol_state": state,
                "family": NO_TRADE,
            }
            for state in STATES
        ]
    )


def test_combined_history_requires_exact_one_minute_seam() -> None:
    early = _frame("2020-01-01", 3)
    late = _frame("2020-01-01 00:03", 2)
    combined = combine_minute_frames([late, early])
    assert len(combined) == 5
    assert combined.index[0] == early.index[0]
    assert combined.index[-1] == late.index[-1]

    gap = _frame("2020-01-01 00:04", 2)
    with pytest.raises(ValueError, match="one-minute seam"):
        combine_minute_frames([early, gap])


def test_causality_audit_accepts_only_contiguous_prior_months() -> None:
    prior = [f"2020-{month:02d}" for month in range(1, 13)]
    valid = _selection("2021-01", prior)
    result = validate_causal_selections(valid, 12)
    assert result["validatedCells"] == len(STATES)
    assert result["futureMonthsObserved"] == 0

    leaked = valid.copy()
    leaked.loc[:, "train_months"] = ",".join(prior[:-1] + ["2021-01"])
    with pytest.raises(ValueError, match="non-causal"):
        validate_causal_selections(leaked, 12)


def test_monthly_aggregation_preserves_zero_months_and_wilson_counts() -> None:
    frame = pd.DataFrame(
        [
            {"month": "2020-01", "trades": 4, "wins": 3, "losses": 1, "ties": 0, "pnlU": 7.0},
            {"month": "2020-03", "trades": 2, "wins": 1, "losses": 1, "ties": 0, "pnlU": -1.0},
        ]
    )
    result = _aggregate_monthly_metrics(
        frame, ["2020-01", "2020-02", "2020-03"]
    )
    assert result["trades"] == 6
    assert result["wins"] == 4
    assert result["pnlU"] == 6.0
    assert result["calendarMonths"] == 3
    assert result["activeMonths"] == 2
    assert result["positiveMonthPct"] == pytest.approx(100.0 / 3.0, abs=1e-4)


def test_v31_reuses_full_v25_grid_and_adds_twelve_month_walkforward() -> None:
    assert LOOKBACKS_MIN == (10, 20, 30, 60, 120)
    assert THRESHOLDS == (1.5, 2.0, 2.5)
    assert TRAINING_WINDOWS_MONTHS == (3, 6, 12)
    assert set(ALL_ACTIONS) == set(FAMILIES) | {NO_TRADE}
