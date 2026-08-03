from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import pull_binance_futures_monthly_klines as subject  # noqa: E402


def _write_zip(path: Path, *, header: bool) -> None:
    lines = []
    if header:
        lines.append(",".join(subject.RAW_COLUMNS))
    lines.extend(
        [
            "1577836800000,7200,7210,7190,7205,1.2,1577836859999,8646,12,0.6,4323,0",
            "1577836860000,7205,7220,7200,7215,1.5,1577836919999,10822.5,15,0.8,5772,0",
        ]
    )
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("BTCUSDT-1m-2020-01.csv", "\n".join(lines) + "\n")


def test_month_keys_are_end_exclusive() -> None:
    start = pd.Timestamp("2020-01-01T00:00:00Z")
    end = pd.Timestamp("2020-04-01T00:00:00Z")
    assert subject.month_keys(start, end) == ["2020-01", "2020-02", "2020-03"]


@pytest.mark.parametrize("header", [False, True])
def test_read_archive_accepts_headerless_and_headered_csv(
    tmp_path: Path, header: bool
) -> None:
    path = tmp_path / f"sample-{header}.zip"
    _write_zip(path, header=header)
    frame = subject.read_archive(
        {"path": str(path), "month": "2020-01"}, "BTCUSDT"
    )
    assert len(frame) == 2
    assert frame["open_time"].tolist() == [
        pd.Timestamp("2020-01-01T00:00:00Z"),
        pd.Timestamp("2020-01-01T00:01:00Z"),
    ]
    assert frame["close"].tolist() == [7205.0, 7215.0]
    assert set(frame["market"]) == {"futures"}


def test_audit_detects_continuity_and_gap() -> None:
    start = pd.Timestamp("2020-01-01T00:00:00Z")
    end = pd.Timestamp("2020-01-01T00:03:00Z")
    complete = pd.DataFrame(
        {
            "open_time": pd.date_range(start, periods=3, freq="min"),
            "market": ["futures"] * 3,
            "symbol": ["BTCUSDT"] * 3,
        }
    )
    result = subject.audit(complete, start, end)
    assert result["rows"] == 3
    assert result["missingMinutes"] == 0
    broken = complete.drop(index=1)
    result = subject.audit(broken, start, end)
    assert result["missingMinutes"] == 1
    assert result["firstMissingMinutes"] == ["2020-01-01T00:01:00+00:00"]

