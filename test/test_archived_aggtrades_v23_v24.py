from __future__ import annotations

import csv
import io
import sys
import zipfile
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_all_archived_aggtrades_v24 as v24  # noqa: E402
import research_archived_aggtrades_v23 as v23  # noqa: E402
from research_directional_candidate_v22 import (  # noqa: E402
    CELL,
    PROFILE,
    SIGNAL,
    Z_THRESHOLD,
)


def _candidate(
    signal_time: str | pd.Timestamp,
    row_id: str,
    **overrides: object,
) -> dict[str, object]:
    row: dict[str, object] = {
        "signal_time": pd.Timestamp(signal_time),
        "cell": CELL,
        "profile": PROFILE,
        "signal": SIGNAL,
        "z": Z_THRESHOLD,
        "row_id": row_id,
        "sparse_value": row_id,
    }
    row.update(overrides)
    return row


def _selection_candidates() -> pd.DataFrame:
    return pd.DataFrame(
        [
            _candidate("2024-01-20T00:00:00Z", "jan_later"),
            _candidate(
                "2024-01-10T00:00:00Z",
                "jan_first",
                sparse_value=pd.NA,
            ),
            _candidate("2023-12-31T23:59:59Z", "before_archive_period"),
            _candidate("2024-02-10T00:00:00Z", "wrong_cell", cell="low|revertible"),
            _candidate("2024-02-11T00:00:00Z", "wrong_profile", profile="other"),
            _candidate("2024-02-12T00:00:00Z", "wrong_signal", signal="UP"),
            _candidate("2024-02-13T00:00:00Z", "below_z", z=Z_THRESHOLD - 0.01),
            _candidate("2024-02-14T00:00:00Z", "feb"),
            _candidate("2025-12-31T23:59:59Z", "last_2025"),
            _candidate("2026-01-01T00:00:00Z", "after_archive_period"),
        ]
    )


def test_monthly_selection_keeps_the_earliest_whole_row_in_2024_2025() -> None:
    selected = v23.select_first_signal_per_month(_selection_candidates())

    assert selected["row_id"].tolist() == ["jan_first", "feb", "last_2025"]
    assert pd.isna(selected.loc[0, "sparse_value"])
    assert selected["signal_time"].is_monotonic_increasing


def test_all_signal_selection_keeps_every_matching_2024_2025_row() -> None:
    selected = v24.select_all_reverse_signals(_selection_candidates())

    assert selected["row_id"].tolist() == [
        "jan_first",
        "jan_later",
        "feb",
        "last_2025",
    ]
    assert selected["signal_time"].is_monotonic_increasing


def _milliseconds(timestamp: pd.Timestamp) -> int:
    return int(timestamp.timestamp() * 1000)


def test_needed_ranges_cover_each_entry_and_actual_entry_settlement_lag() -> None:
    signal_time = pd.Timestamp("2025-03-04T12:00:00Z")
    ranges = v23._needed_ranges(pd.DataFrame({"signal_time": [signal_time]}))

    assert set(ranges) == {"2025-03-04"}
    expected: list[tuple[int, int]] = []
    for delay in v23.DELAYS_SEC:
        entry = signal_time + pd.Timedelta(seconds=delay)
        settlement = entry + pd.Timedelta(seconds=v23.HORIZON_SEC)
        expected.extend(
            [
                (
                    _milliseconds(entry),
                    _milliseconds(entry + pd.Timedelta(seconds=v23.MAX_TICK_LAG_SEC)),
                ),
                (
                    _milliseconds(settlement),
                    _milliseconds(
                        settlement
                        + pd.Timedelta(seconds=2 * v23.MAX_TICK_LAG_SEC)
                    ),
                ),
            ]
        )
    assert ranges["2025-03-04"] == expected


def _write_aggtrade_zip(
    path: Path,
    rows: list[list[object]],
    *,
    include_header: bool,
) -> None:
    columns = [
        "agg_trade_id",
        "price",
        "quantity",
        "first_trade_id",
        "last_trade_id",
        "transact_time",
        "is_buyer_maker",
    ]
    text = io.StringIO(newline="")
    writer = csv.writer(text, lineterminator="\n")
    if include_header:
        writer.writerow(columns)
    writer.writerows(rows)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(path.with_suffix(".csv").name, text.getvalue())


@pytest.mark.parametrize("include_header", [True, False])
def test_archive_parser_supports_headered_and_headerless_binance_csv(
    tmp_path: Path,
    include_header: bool,
) -> None:
    base = _milliseconds(pd.Timestamp("2025-03-04T12:00:00Z"))
    path = tmp_path / f"ticks_{include_header}.zip"
    _write_aggtrade_zip(
        path,
        [
            [10, "100.0", "1", 100, 100, base + 999, False],
            [11, "101.0", "1", 101, 101, base + 1000, False],
            [12, "102.0", "1", 102, 102, base + 3000, True],
            [13, "103.0", "1", 103, 103, base + 3001, True],
        ],
        include_header=include_header,
    )

    rows = v23._read_archive_ranges(
        {"date": "2025-03-04", "path": str(path)},
        [(base + 1000, base + 3000)],
    )

    assert rows.columns.tolist() == [
        "agg_trade_id",
        "price",
        "time_ms",
        "archive_date",
    ]
    assert rows["agg_trade_id"].astype(int).tolist() == [11, 12]
    assert rows["price"].astype(float).tolist() == [101.0, 102.0]
    assert rows["time_ms"].astype(int).tolist() == [base + 1000, base + 3000]
    assert rows["archive_date"].tolist() == ["2025-03-04", "2025-03-04"]


def test_filtered_ticks_are_chronological_and_keep_latest_duplicate_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _milliseconds(pd.Timestamp("2025-03-04T12:00:00Z"))
    parts = {
        "2025-03-04": pd.DataFrame(
            {
                "agg_trade_id": [2, 1, 4],
                "price": [102.0, 103.0, -1.0],
                "time_ms": [base + 2000, base + 3000, base + 1000],
                "archive_date": ["2025-03-04"] * 3,
            }
        ),
        "2025-03-05": pd.DataFrame(
            {
                "agg_trade_id": [1, 3, None],
                "price": [99.0, 104.0, 105.0],
                "time_ms": [base + 1000, base + 2000, base + 4000],
                "archive_date": ["2025-03-05"] * 3,
            }
        ),
    }

    def fake_read(
        archive: dict[str, object],
        ranges: list[tuple[int, int]],
    ) -> pd.DataFrame:
        assert ranges == [(base, base + 5000)]
        return parts[str(archive["date"])].copy()

    monkeypatch.setattr(v23, "_read_archive_ranges", fake_read)
    ticks = v23.load_filtered_ticks(
        [
            {"date": "2025-03-04", "path": "unused-a"},
            {"date": "2025-03-05", "path": "unused-b"},
        ],
        {
            "2025-03-04": [(base, base + 5000)],
            "2025-03-05": [(base, base + 5000)],
        },
    )

    assert ticks["agg_trade_id"].astype(int).tolist() == [2, 3, 1]
    assert ticks["price"].tolist() == [102.0, 104.0, 103.0]
    assert ticks["time"].tolist() == [
        pd.Timestamp("2025-03-04T12:00:02Z"),
        pd.Timestamp("2025-03-04T12:00:02Z"),
        pd.Timestamp("2025-03-04T12:00:03Z"),
    ]
    assert ticks["agg_trade_id"].is_unique


def _fifty_candidates_and_ticks(
    *,
    omit_last_ten_second_settlement: bool,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    start = pd.Timestamp("2024-06-01T00:00:00Z")
    candidates: list[dict[str, object]] = []
    ticks: list[dict[str, object]] = []
    for index in range(50):
        signal_time = start + pd.Timedelta(seconds=1200 * index)
        candidates.append(_candidate(signal_time, f"signal_{index:02d}"))
        entry_price = 200.0 + index
        for delay in v23.DELAYS_SEC:
            ticks.append({"time": signal_time + pd.Timedelta(seconds=delay), "price": entry_price})
            if (
                omit_last_ten_second_settlement
                and index == 49
                and delay == 10
            ):
                continue
            ticks.append(
                {
                    "time": signal_time
                    + pd.Timedelta(seconds=delay + v23.HORIZON_SEC),
                    "price": entry_price - 1.0,
                }
            )
    return pd.DataFrame(candidates), pd.DataFrame(ticks)


@pytest.mark.parametrize(
    ("omit_last_ten_second_settlement", "expected_common", "expected_ten_settled"),
    [(False, 50, 50), (True, 49, 49)],
)
def test_v24_reports_common_0_5_10_second_settlement_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    omit_last_ten_second_settlement: bool,
    expected_common: int,
    expected_ten_settled: int,
) -> None:
    candidates, ticks = _fifty_candidates_and_ticks(
        omit_last_ten_second_settlement=omit_last_ten_second_settlement
    )
    requested_dates: list[str] = []

    def fake_download(dates: list[str], workers: int) -> list[dict[str, object]]:
        assert workers == 2
        requested_dates.extend(dates)
        return [{"date": date, "path": "unused"} for date in dates]

    monkeypatch.setattr(v24, "load_candidates", lambda _: candidates.copy())
    monkeypatch.setattr(v24, "download_archives", fake_download)
    monkeypatch.setattr(
        v24,
        "load_filtered_ticks",
        lambda archives, ranges: ticks.copy(),
    )
    monkeypatch.setattr(v24, "OUT_JSON", tmp_path / "report.json")
    monkeypatch.setattr(v24, "OUT_TICKS", tmp_path / "ticks.csv")
    monkeypatch.setattr(v24, "OUT_TRADES", tmp_path / "trades.csv")

    report = v24.run("unused-candidates.csv", workers=2)

    assert requested_dates == ["2024-06-01"]
    assert report["sampling"]["selectedSignals"] == 50
    assert report["results"]["commonCoverageSignals"] == expected_common
    assert set(report["results"]["byDelay"]) == {"0", "5", "10"}
    assert report["results"]["byDelay"]["0"]["settled"] == 50
    assert report["results"]["byDelay"]["5"]["settled"] == 50
    assert report["results"]["byDelay"]["10"]["settled"] == expected_ten_settled
    assert {
        delay: metrics["settled"]
        for delay, metrics in report["results"]["commonCoverageByDelay"].items()
    } == {"0": expected_common, "5": expected_common, "10": expected_common}
    assert report["results"]["promotionStyleGatePassed"] is (
        expected_common == 50
    )
    assert (tmp_path / "report.json").exists()
    assert (tmp_path / "ticks.csv").exists()
    assert (tmp_path / "trades.csv").exists()
