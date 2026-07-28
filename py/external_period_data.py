"""Availability-time rules for exchange period statistics."""

from __future__ import annotations

import pandas as pd


DEFAULT_PERIOD = pd.Timedelta(minutes=5)


def add_period_available_time(
    frame: pd.DataFrame,
    timestamp_col: str = "timestamp",
    *,
    period: pd.Timedelta = DEFAULT_PERIOD,
    output_col: str = "available_time",
) -> pd.DataFrame:
    """Return a copy where bucket data becomes usable only after it closes."""
    out = frame.copy()
    start = pd.to_datetime(out[timestamp_col], utc=True, format="mixed", errors="coerce")
    out[output_col] = start + period
    return out
