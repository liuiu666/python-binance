from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pandas.testing as pdt


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_adaptive_structure_router_v33 import (  # noqa: E402
    SHOCK_QUANTILE,
    STRUCTURE_QUANTILE,
    TRAINING_WINDOWS_MONTHS,
    build_adaptive_structure_features,
    causal_trailing_quantile,
    causal_trailing_zscore,
)


def _minutes(rows: int = 900) -> pd.DataFrame:
    rng = np.random.default_rng(20260730)
    returns = rng.normal(0.0, 0.0005, rows)
    close = 100.0 * np.exp(np.cumsum(returns))
    index = pd.date_range("2024-01-01", periods=rows, freq="1min", tz="UTC")
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.0001,
            "low": close * 0.9999,
            "close": close,
            "volume": 1.0,
        },
        index=index,
    )


def test_causal_normalizers_do_not_change_past_when_future_changes() -> None:
    source = pd.Series(np.linspace(0.0, 10.0, 200))
    altered = source.copy()
    altered.iloc[150:] = 10_000.0
    original_z = causal_trailing_zscore(source, history_min=40, min_periods=20)
    altered_z = causal_trailing_zscore(altered, history_min=40, min_periods=20)
    original_q = causal_trailing_quantile(
        source, 0.8, history_min=40, min_periods=20
    )
    altered_q = causal_trailing_quantile(
        altered, 0.8, history_min=40, min_periods=20
    )
    pdt.assert_series_equal(original_z.iloc[:150], altered_z.iloc[:150])
    pdt.assert_series_equal(original_q.iloc[:150], altered_q.iloc[:150])


def test_adaptive_structure_is_causal_and_uses_only_registered_states() -> None:
    minutes = _minutes()
    original = build_adaptive_structure_features(
        minutes, history_min=240, min_periods=120
    )
    altered_minutes = minutes.copy()
    altered_minutes.loc[altered_minutes.index[750]:, "close"] *= 2.0
    altered_minutes.loc[altered_minutes.index[750]:, "open"] *= 2.0
    altered = build_adaptive_structure_features(
        altered_minutes, history_min=240, min_periods=120
    )
    pdt.assert_series_equal(
        original["structure_state"].iloc[:750],
        altered["structure_state"].iloc[:750],
    )
    assert set(original["structure_state"].unique()) <= {
        "unknown",
        "mixed",
        "shock",
        "revertible",
        "trend",
    }
    assert original["adaptive_mr_score"].notna().any()
    assert original["adaptive_trend_score"].notna().any()


def test_v33_frozen_design_constants() -> None:
    assert TRAINING_WINDOWS_MONTHS == (3, 6, 12)
    assert STRUCTURE_QUANTILE == 0.80
    assert SHOCK_QUANTILE == 0.95
