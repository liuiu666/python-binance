from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from v14_validation import (
    DEFAULT_DELAYS_SEC,
    DEFAULT_EXECUTION_BASE_LAG_SEC,
    apply_family_cooldown,
    deduplicate_candidates,
    metrics_by_block,
    normalize_candidates,
    normalize_futures_ticks,
    resolve_candidate_trades,
    run_validation,
    summarize_metrics,
    wilson_lower_bound,
)


START = pd.Timestamp("2026-01-01T00:00:00Z")


def _ticks(seconds: list[int], prices: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "time": [START + pd.Timedelta(seconds=second) for second in seconds],
            "price": prices,
            "market": "futures",
        }
    )


def test_exact_candidate_duplicates_are_removed_once():
    candidates = pd.DataFrame(
        [
            {
                "time": START,
                "signal": "UP",
                "family": "V14",
                "branch": "mean_reversal",
                "source": "snapshot_a",
            },
            {
                "time": START,
                "signal": "UP",
                "family": "V14",
                "branch": "mean_reversal",
                "source": "overlapping_snapshot_b",
            },
            {
                "time": START,
                "signal": "UP",
                "family": "V14",
                "branch": "orderflow",
                "source": "snapshot_a",
            },
        ]
    )

    deduped = deduplicate_candidates(normalize_candidates(candidates))

    assert len(deduped) == 2
    duplicate_counts = dict(zip(deduped["branch"], deduped["duplicate_count"]))
    assert duplicate_counts == {"mean_reversal": 2, "orderflow": 1}
    assert deduped.loc[deduped["branch"].eq("mean_reversal"), "source"].item() == "snapshot_a"


def test_family_cooldown_spans_aliases_and_branches_but_keeps_exact_boundary():
    candidates = pd.DataFrame(
        [
            {
                "time": START,
                "signal": "UP",
                "family": "V14",
                "branch": "normal",
                "strategy_id": "V14_ALIAS_A",
            },
            {
                "time": START + pd.Timedelta(seconds=300),
                "signal": "DOWN",
                "family": "V14",
                "branch": "orderflow",
                "strategy_id": "V14_ALIAS_B",
            },
            {
                "time": START + pd.Timedelta(seconds=600),
                "signal": "DOWN",
                "family": "V14",
                "branch": "third_branch",
                "strategy_id": "V14_ALIAS_C",
            },
            {
                "time": START + pd.Timedelta(seconds=100),
                "signal": "UP",
                "family": "INDEPENDENT_FAMILY",
                "branch": "normal",
                "strategy_id": "OTHER",
            },
        ]
    )

    cooled = apply_family_cooldown(
        deduplicate_candidates(normalize_candidates(candidates)),
        cooldown_sec=600,
    )

    v14 = cooled[cooled["family"].eq("V14")]
    assert list(v14["strategy_id"]) == ["V14_ALIAS_A", "V14_ALIAS_C"]
    assert list(v14["time"]) == [START, START + pd.Timedelta(seconds=600)]
    assert "OTHER" in set(cooled["strategy_id"])


def test_default_delays_resolve_four_unique_trades():
    candidates = pd.DataFrame(
        [{"time": START, "signal": "UP", "family": "V14", "branch": "normal"}]
    )
    ticks = _ticks(
        [0, 1, 6, 11, 16, 601, 606, 611, 616],
        [1.0, 100.0, 101.0, 102.0, 103.0, 110.0, 111.0, 112.0, 113.0],
    )

    trades, report = run_validation(candidates, ticks)

    assert DEFAULT_DELAYS_SEC == (0, 5, 10, 15)
    assert DEFAULT_EXECUTION_BASE_LAG_SEC == 1
    assert list(trades["delay_sec"]) == [0, 5, 10, 15]
    assert list(trades["entry_target_time"]) == [
        START + pd.Timedelta(seconds=1),
        START + pd.Timedelta(seconds=6),
        START + pd.Timedelta(seconds=11),
        START + pd.Timedelta(seconds=16),
    ]
    assert report["method"]["entryDelaysSec"] == [0, 5, 10, 15]
    assert report["method"]["executionBaseLagSec"] == 1
    assert trades["trade_key"].is_unique
    assert len(trades) == 4


def test_entry_and_settlement_use_first_tick_after_each_target():
    candidates = normalize_candidates(
        pd.DataFrame(
            [{"time": START, "signal": "UP", "family": "V14", "branch": "normal"}]
        )
    )
    ticks = normalize_futures_ticks(
        _ticks(
            [-1, 0, 2, 600, 603],
            [99.0, 50.0, 100.0, 80.0, 110.0],
        )
    )

    trades = resolve_candidate_trades(candidates, ticks, delays_sec=(0,), horizon_sec=600)
    trade = trades.iloc[0]

    assert trade["entry_target_time"] == START + pd.Timedelta(seconds=1)
    assert trade["entry_time"] == START + pd.Timedelta(seconds=2)
    assert trade["entry_lag_sec"] == 1.0
    assert trade["settle_target_time"] == START + pd.Timedelta(seconds=602)
    assert trade["settle_time"] == START + pd.Timedelta(seconds=603)
    assert trade["settle_lag_sec"] == 1.0
    assert trade["settle_price"] == 110.0
    assert trade["status"] == "won"


def test_spot_label_is_rejected_from_futures_validation():
    spot_ticks = pd.DataFrame(
        {"time": [START], "price": [100.0], "market": ["spot"]}
    )

    with pytest.raises(ValueError, match="non-futures"):
        normalize_futures_ticks(spot_ticks)


def test_metrics_cover_pnl_drawdown_streak_thin_margin_and_wilson():
    statuses = ["won", "lost", "lost", "won", "tie", "missing_entry"]
    trades = pd.DataFrame(
        {
            "signal_time": [START + pd.Timedelta(seconds=i) for i in range(6)],
            "entry_time": [START + pd.Timedelta(seconds=i) for i in range(5)] + [pd.NaT],
            "status": statuses,
            "pnl_u": [4.0, -5.0, -5.0, 4.0, 0.0, float("nan")],
            "signed_bps": [2.0, -1.0, -4.0, 10.0, 0.0, float("nan")],
        }
    )

    metrics = summarize_metrics(trades, thin_margin_bps=3.0)

    assert metrics["requested"] == 6
    assert metrics["settled"] == 5
    assert (metrics["wins"], metrics["losses"], metrics["ties"], metrics["missing"]) == (2, 2, 1, 1)
    assert metrics["winRatePct"] == 50.0
    assert metrics["pnlU"] == -2.0
    assert metrics["maxDrawdownU"] == 10.0
    assert metrics["maxLossStreak"] == 2
    assert metrics["medianSignedBps"] == 0.0
    assert metrics["thinMarginCount"] == 3
    assert metrics["thinMarginPct"] == 60.0
    assert wilson_lower_bound(2, 4) == pytest.approx(0.150039, abs=1e-6)
    assert metrics["wilson95LowerPct"] == pytest.approx(15.0039, abs=1e-4)


def test_metrics_group_by_explicit_daily_and_fixed_size_blocks():
    trades = pd.DataFrame(
        {
            "trade_key": [f"trade_{index}" for index in range(4)],
            "candidate_key": [f"candidate_{index}" for index in range(4)],
            "signal_time": [
                START,
                START + pd.Timedelta(hours=1),
                START + pd.Timedelta(days=1),
                START + pd.Timedelta(days=1, hours=1),
            ],
            "entry_time": [
                START,
                START + pd.Timedelta(hours=1),
                START + pd.Timedelta(days=1),
                START + pd.Timedelta(days=1, hours=1),
            ],
            "delay_sec": [0, 0, 0, 0],
            "status": ["won", "lost", "won", "lost"],
            "pnl_u": [4.0, -5.0, 4.0, -5.0],
            "signed_bps": [5.0, -5.0, 5.0, -5.0],
            "block": ["walk_forward_a", "walk_forward_a", "walk_forward_b", "walk_forward_b"],
        }
    )

    explicit = metrics_by_block(trades, block_col="block")
    daily = metrics_by_block(
        trades.drop(columns="block"),
        block_col=None,
        block_freq="1D",
        block_timezone="UTC",
    )
    fixed = metrics_by_block(
        trades.drop(columns="block"),
        block_col=None,
        block_size=2,
    )

    assert set(explicit["0"]) == {"walk_forward_a", "walk_forward_b"}
    assert all(metrics["requested"] == 2 for metrics in explicit["0"].values())
    assert len(daily["0"]) == 2
    assert all(metrics["requested"] == 2 for metrics in daily["0"].values())
    assert set(fixed["0"]) == {"block_0000", "block_0001"}
    assert all(metrics["requested"] == 2 for metrics in fixed["0"].values())
