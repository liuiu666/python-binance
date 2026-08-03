from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_v17_second_execution_validation import (  # noqa: E402
    _target_seconds,
    load_needed_second_rows,
)


START = pd.Timestamp("2026-07-01T00:00:00Z")


def test_target_seconds_cover_entry_and_lagged_settlement_windows() -> None:
    needed = _target_seconds([START])
    assert len(needed) == 37
    assert needed[0] == START
    assert needed[-1] == START + pd.Timedelta(seconds=620)
    assert START + pd.Timedelta(seconds=600) in needed
    assert START + pd.Timedelta(seconds=605) in needed


def test_needed_second_filter_forces_matching_datetime_units(tmp_path: Path) -> None:
    source = tmp_path / "2026-07-01.csv"
    pd.DataFrame(
        {
            "timestamp": [
                START.isoformat(),
                (START + pd.Timedelta(seconds=1)).isoformat(),
                (START + pd.Timedelta(seconds=2)).isoformat(),
            ],
            "market": ["futures"] * 3,
            "open": [100.0, 101.0, 102.0],
            "close": [100.5, 101.5, 102.5],
            "last_trade_time": [
                (START + pd.Timedelta(milliseconds=900)).isoformat(),
                (START + pd.Timedelta(seconds=1, milliseconds=900)).isoformat(),
                (START + pd.Timedelta(seconds=2, milliseconds=900)).isoformat(),
            ],
        }
    ).to_csv(source, index=False)
    needed = pd.DatetimeIndex([START, START + pd.Timedelta(seconds=2)])

    rows, audit = load_needed_second_rows([source], needed)
    assert list(rows["time"]) == [START, START + pd.Timedelta(seconds=2)]
    assert list(rows["close"]) == [100.5, 102.5]
    assert audit["uniqueNeededSecondsFound"] == 2

