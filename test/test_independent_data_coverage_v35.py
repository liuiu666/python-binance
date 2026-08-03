from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from audit_independent_data_coverage_v35 import (  # noqa: E402
    MIN_LONGITUDINAL_DAYS,
    aggtrade_archive_audit,
    csv_time_bounds,
    qualifies_longitudinal,
)


def test_csv_bounds_strip_nul_and_parse_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "sample.csv"
    path.write_text(
        "timestamp,value\n"
        "\x002024-01-01T00:00:00Z,1\n"
        "2024-01-03T00:00:00Z,2\n",
        encoding="utf-8",
    )
    result = csv_time_bounds(path, "timestamp")
    assert result["rows"] == 2
    assert result["start"] == pd.Timestamp("2024-01-01T00:00:00Z")
    assert result["end"] == pd.Timestamp("2024-01-03T00:00:00Z")
    assert result["durationDays"] == 2.0


def test_longitudinal_gate_rejects_short_or_target_selected() -> None:
    assert not qualifies_longitudinal(
        {
            "exists": True,
            "durationDays": MIN_LONGITUDINAL_DAYS - 1,
            "sampling": "continuous_or_regular",
            "targetSelected": False,
        }
    )
    assert not qualifies_longitudinal(
        {
            "exists": True,
            "durationDays": MIN_LONGITUDINAL_DAYS + 1,
            "sampling": "signal_targeted_days",
            "targetSelected": True,
        }
    )
    assert qualifies_longitudinal(
        {
            "exists": True,
            "durationDays": MIN_LONGITUDINAL_DAYS + 1,
            "sampling": "continuous_or_regular",
            "targetSelected": False,
        }
    )


def test_aggtrade_archive_audit_measures_calendar_coverage(tmp_path: Path) -> None:
    for date in ("2024-01-01", "2024-01-03"):
        (tmp_path / f"BTCUSDT-aggTrades-{date}.zip").write_bytes(b"x")
    result = aggtrade_archive_audit(tmp_path)
    assert result["uniqueDates"] == 2
    assert result["calendarSpanDays"] == 3
    assert result["calendarCoveragePct"] == 66.6667
    assert result["targetSelected"] is True
