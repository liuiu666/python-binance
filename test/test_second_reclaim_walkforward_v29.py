from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_second_reclaim_walkforward_v29 as subject  # noqa: E402


def test_needed_ranges_cover_confirmation_and_latest_settlement() -> None:
    signals = pd.DataFrame(
        {"signal_time": [pd.Timestamp("2020-01-01T23:59:55Z")]}
    )
    ranges = subject.needed_ranges(signals)
    assert set(ranges) == {"2020-01-01", "2020-01-02"}
    all_ranges = [item for values in ranges.values() for item in values]
    latest_end = max(end for _, end in all_ranges)
    expected = int(pd.Timestamp("2020-01-02T00:10:20Z").timestamp() * 1000)
    assert latest_end == expected


def test_build_reclaim_trades_is_causal_and_uses_post_confirmation_delays() -> None:
    start = pd.Timestamp("2020-01-01T00:00:00Z")
    times = pd.date_range(start, periods=701, freq="s")
    prices = 100.0 - np.arange(len(times)) * 0.001
    ticks = pd.DataFrame(
        {
            "time": times,
            "price": prices,
            "agg_trade_id": np.arange(len(times)),
        }
    )
    signals = pd.DataFrame(
        {
            "candidate_id": ["h1"],
            "signal_time": [start],
            "period": ["test_2023"],
        }
    )
    trades = subject.build_reclaim_trades(signals, ticks)
    primary = trades.loc[trades["rule"].eq(subject.PRIMARY_RULE)]
    assert primary["confirmed"].all()
    assert sorted(primary["total_delay_sec"].unique()) == [5, 10, 15]
    assert len(primary) == 6
    fixed = primary.loc[primary["settlement_mode"].eq("fixed_boundary")]
    assert fixed["settle_target_time"].nunique() == 1
    assert fixed["settle_target_time"].iloc[0] == start + pd.Timedelta(seconds=600)
    full = primary.loc[primary["settlement_mode"].eq("entry_plus_600s")]
    assert sorted(
        (full["settle_target_time"] - start).dt.total_seconds().astype(int).tolist()
    ) == [605, 610, 615]


def test_rejected_signal_is_not_counted_as_settled() -> None:
    rows = []
    for mode in ("entry_plus_600s", "fixed_boundary"):
        for delay in (0, 5, 10):
            rows.append(
                {
                    "candidate_id": "accepted",
                    "settlement_mode": mode,
                    "post_confirm_delay_sec": delay,
                    "status": "won",
                }
            )
            rows.append(
                {
                    "candidate_id": "rejected",
                    "settlement_mode": mode,
                    "post_confirm_delay_sec": delay,
                    "status": "rejected",
                }
            )
    common = subject.common_settled(pd.DataFrame(rows), full_grid=True)
    assert set(common["candidate_id"]) == {"accepted"}


def test_promotion_gate_rejects_one_negative_slice() -> None:
    positive_slice = {
        "winRatePct": 70.0,
        "wilson95LowerPct": 60.0,
        "pnlU": 20.0,
        "yearlyPnlU": {"2023": 4.0, "2024": 4.0, "2025": 4.0},
        "bootstrap": {"lower90EvU": 0.2},
        "maxDrawdownU": 10.0,
        "maxLossStreak": 2,
    }
    slices = {f"slice{i}": dict(positive_slice) for i in range(6)}
    test = {"slices": slices}
    reused = {"slices": slices}
    combined = {"slices": slices, "commonCoverageSignals": 70}
    assert subject.promotion_gate(test, reused, combined) is True
    reused_bad = {"slices": {**slices, "slice0": {**positive_slice, "pnlU": -1.0}}}
    assert subject.promotion_gate(test, reused_bad, combined) is False

