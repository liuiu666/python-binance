from __future__ import annotations

import unittest
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "py"))

from second_backtest.execution import execute_signals
from second_backtest.metrics import max_loss_streak, summarize_trades
from second_backtest.strategies import SecondNormalConfig, generate_normal_signals


class SecondBacktestTest(unittest.TestCase):
    def test_per_strategy_lock_does_not_dedupe_other_strategy(self):
        base = pd.Timestamp("2026-01-01T00:00:00Z")
        signals = [
            {"strategy_id": "A", "time": base, "signal": "UP", "horizon_sec": 600, "won": True},
            {"strategy_id": "B", "time": base, "signal": "DOWN", "horizon_sec": 600, "won": True},
            {
                "strategy_id": "A",
                "time": base + pd.Timedelta(seconds=60),
                "signal": "DOWN",
                "horizon_sec": 600,
                "won": False,
            },
        ]
        accepted, rejected = execute_signals(signals, cooldown_sec=600)
        self.assertEqual([row["strategy_id"] for row in accepted], ["A", "B"])
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["skipReason"], "strategy_lock")

    def test_global_lock_is_optional_research_only(self):
        base = pd.Timestamp("2026-01-01T00:00:00Z")
        signals = [
            {"strategy_id": "A", "time": base, "signal": "UP", "horizon_sec": 600, "won": True},
            {"strategy_id": "B", "time": base + pd.Timedelta(seconds=1), "signal": "DOWN", "horizon_sec": 600, "won": True},
        ]
        accepted, rejected = execute_signals(signals, cooldown_sec=600, global_lock_sec=600)
        self.assertEqual([row["strategy_id"] for row in accepted], ["A"])
        self.assertEqual(rejected[0]["skipReason"], "global_lock")

    def test_metrics_profit_for_10m_binary(self):
        base = pd.Timestamp("2026-01-01T00:00:00Z")
        trades = [
            {"time": base, "won": True},
            {"time": base + pd.Timedelta(minutes=10), "won": False},
            {"time": base + pd.Timedelta(minutes=20), "won": True},
        ]
        summary = summarize_trades(trades, base, base + pd.Timedelta(hours=1), amount=5, payout_rate=0.8)
        self.assertEqual(summary["wins"], 2)
        self.assertEqual(summary["losses"], 1)
        self.assertEqual(summary["pnl"], 3.0)
        self.assertEqual(max_loss_streak([True, False, False, True]), 2)

    def test_normal_strategy_uses_current_and_past_prices_only(self):
        times = pd.date_range("2026-01-01T00:00:00Z", periods=1300, freq="s")
        close = [100.0 + i * 0.01 for i in range(1300)]
        bars = pd.DataFrame(
            {
                "close": close,
                "volume": 1.0,
                "buy_qty": 0.5,
                "sell_qty": 0.5,
                "observed": True,
            },
            index=times,
        )
        cfg = SecondNormalConfig(lookback_sec=120, horizon_sec=60, tail_pct=0.40)
        baseline = generate_normal_signals(bars, cfg)
        changed = bars.copy()
        changed.iloc[500:, changed.columns.get_loc("close")] += 1000.0
        mutated = generate_normal_signals(changed, cfg)
        before_mutation_baseline = [row for row in baseline if row["idx"] < 440]
        before_mutation_mutated = [row for row in mutated if row["idx"] < 440]
        self.assertEqual(before_mutation_baseline, before_mutation_mutated)


if __name__ == "__main__":
    unittest.main()
