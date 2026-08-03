from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_frozen_second_execution_v28 as subject  # noqa: E402
from v14_validation import normalize_candidates, normalize_futures_ticks  # noqa: E402


def _candidate(time: str = "2020-01-01T00:00:00Z") -> pd.DataFrame:
    raw = pd.DataFrame(
        {
            "time": [time],
            "signal": ["DOWN"],
            "family": ["frozen"],
            "branch": ["mid|revertible"],
        }
    )
    return normalize_candidates(raw)


def test_fixed_boundary_keeps_original_settlement_for_all_delays() -> None:
    candidates = _candidate()
    ticks = normalize_futures_ticks(
        pd.DataFrame(
            {
                "time": pd.to_datetime(
                    [
                        "2020-01-01T00:00:00Z",
                        "2020-01-01T00:00:05Z",
                        "2020-01-01T00:00:10Z",
                        "2020-01-01T00:10:00Z",
                    ],
                    utc=True,
                ),
                "price": [100.0, 101.0, 102.0, 99.0],
            }
        ),
        time_col="time",
        price_col="price",
        require_futures=False,
    )
    trades = subject.resolve_fixed_boundary_trades(candidates, ticks)
    assert trades["delay_sec"].tolist() == [0, 5, 10]
    assert trades["entry_price"].tolist() == [100.0, 101.0, 102.0]
    assert trades["settle_time"].nunique() == 1
    assert trades["settle_time"].iloc[0] == pd.Timestamp("2020-01-01T00:10:00Z")
    assert set(trades["status"]) == {"won"}


def test_common_keys_require_both_modes_and_every_delay() -> None:
    rows = []
    for mode in ("entry_plus_600s", "fixed_boundary"):
        for delay in (0, 5, 10):
            rows.append(
                {
                    "candidate_key": "complete",
                    "settlement_mode": mode,
                    "delay_sec": delay,
                    "status": "won",
                }
            )
    rows.pop()
    rows.extend(
        {
            "candidate_key": "other",
            "settlement_mode": mode,
            "delay_sec": delay,
            "status": "won",
        }
        for mode in ("entry_plus_600s", "fixed_boundary")
        for delay in (0, 5, 10)
    )
    assert subject._common_candidate_keys(pd.DataFrame(rows)) == {"other"}


def test_diagnostics_pass_only_when_each_slice_is_robust() -> None:
    rows = []
    for month in pd.period_range("2020-01", "2023-12", freq="M"):
        base = month.start_time.tz_localize("UTC")
        for offset in range(4):
            signal_time = base + pd.Timedelta(days=offset)
            for mode in ("entry_plus_600s", "fixed_boundary"):
                for delay in (0, 5, 10):
                    rows.append(
                        {
                            "candidate_key": f"{signal_time.isoformat()}|{offset}",
                            "settlement_mode": mode,
                            "delay_sec": delay,
                            "status": "won",
                            "pnl_u": 4.0,
                            "signed_bps": 2.0,
                            "signal_time": signal_time,
                            "entry_time": signal_time + pd.Timedelta(seconds=delay),
                        }
                    )
    result = subject._slice_diagnostics(pd.DataFrame(rows))
    assert len(result) == 6
    assert all(value["passed"] for value in result.values())

