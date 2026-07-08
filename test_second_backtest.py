from __future__ import annotations

import unittest
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "py"))

from second_backtest.execution import execute_signals
from second_backtest.metrics import max_loss_streak, summarize_trades
from second_backtest.dynamic_zone import dynamic_zone_allows
from second_backtest.strategies import (
    SecondNormalConfig,
    SecondNormalRouterV21Config,
    _router_v21_candidate_allowed,
    generate_normal_signals,
    prod_configs_to_second_configs,
)


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

    def test_dynamic_zone_filter_blocks_fake_rebound_and_breakout(self):
        fake_rebound = {
            "zone_position": 0.28,
            "trend_300s_bps": 12.0,
            "trend_1800s_bps": -65.0,
            "range_10m_bps": 34.0,
            "flow_5m": 0.08,
        }
        ok, reason = dynamic_zone_allows("dynamic_v1", "UP", fake_rebound)
        self.assertFalse(ok)
        self.assertEqual(reason, "zone_20_40_fake_rebound_block")

        breakout = {
            "zone_position": 0.93,
            "trend_300s_bps": 22.0,
            "trend_1800s_bps": 70.0,
            "range_10m_bps": 44.0,
            "flow_5m": 0.25,
        }
        ok, reason = dynamic_zone_allows("dynamic_v1", "DOWN", breakout)
        self.assertFalse(ok)
        self.assertEqual(reason, "zone_80_100_breakout_block")

    def test_dynamic_zone_filter_allows_confirmed_upper_reversion(self):
        context = {
            "zone_position": 0.68,
            "trend_300s_bps": -2.0,
            "trend_1800s_bps": 46.0,
            "range_10m_bps": 33.0,
            "flow_5m": -0.05,
        }
        ok, reason = dynamic_zone_allows("dynamic_v1", "DOWN", context)
        self.assertTrue(ok)
        self.assertEqual(reason, "zone_60_80_upper_reversion")

    def test_dynamic_v2_only_allows_strict_upper_reversion(self):
        ok, reason = dynamic_zone_allows("dynamic_v2", "DOWN", {
            "zone_position": 0.68,
            "trend_300s_bps": 4.0,
            "trend_1800s_bps": 50.0,
            "range_10m_bps": 42.0,
            "flow_5m": 0.05,
        })
        self.assertTrue(ok)
        self.assertEqual(reason, "zone_v2_60_80_upper_reversion")

        ok, reason = dynamic_zone_allows("dynamic_v2", "UP", {
            "zone_position": 0.12,
            "trend_300s_bps": 7.0,
            "trend_1800s_bps": -40.0,
            "range_10m_bps": 28.0,
            "flow_5m": 0.12,
        })
        self.assertFalse(ok)
        self.assertEqual(reason, "zone_v2_only_60_80")

        ok, reason = dynamic_zone_allows("dynamic_v2", "DOWN", {
            "zone_position": 0.89,
            "trend_300s_bps": -8.0,
            "trend_1800s_bps": 150.0,
            "range_10m_bps": 40.0,
            "flow_5m": -0.2,
        })
        self.assertFalse(ok)
        self.assertEqual(reason, "zone_v2_only_60_80")

        ok, reason = dynamic_zone_allows("dynamic_v2", "DOWN", {
            "zone_position": 0.79,
            "trend_300s_bps": -3.0,
            "trend_1800s_bps": 98.0,
            "trend_3600s_bps": 127.0,
            "range_10m_bps": 42.0,
            "flow_5m": 0.07,
        })
        self.assertFalse(ok)
        self.assertEqual(reason, "zone_v2_long_trend_too_strong")

    def test_dynamic_v3_keeps_core_normal_shapes(self):
        ok, reason = dynamic_zone_allows("dynamic_v3", "UP", {
            "zone_position": 0.12,
            "trend_300s_bps": -20.0,
            "trend_1800s_bps": -60.0,
            "range_10m_bps": 35.0,
            "flow_5m": -0.2,
        })
        self.assertTrue(ok)
        self.assertEqual(reason, "zone_v3_lower_tail_up")

        ok, reason = dynamic_zone_allows("dynamic_v3", "DOWN", {
            "zone_position": 0.55,
            "trend_300s_bps": 3.0,
            "trend_1800s_bps": 10.0,
            "range_10m_bps": 30.0,
            "flow_5m": 0.0,
        })
        self.assertTrue(ok)
        self.assertEqual(reason, "zone_v3_mid_upper_down")

        ok, reason = dynamic_zone_allows("dynamic_v3", "UP", {
            "zone_position": 0.31,
            "trend_300s_bps": 8.0,
            "trend_1800s_bps": -60.0,
            "range_10m_bps": 34.0,
            "flow_5m": 0.05,
        })
        self.assertFalse(ok)
        self.assertEqual(reason, "zone_v3_20_40_block")

        ok, reason = dynamic_zone_allows("dynamic_v3", "DOWN", {
            "zone_position": 0.91,
            "trend_300s_bps": 20.0,
            "trend_1800s_bps": 70.0,
            "range_10m_bps": 40.0,
            "flow_5m": 0.2,
        })
        self.assertFalse(ok)
        self.assertEqual(reason, "zone_v3_80_100_block")

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

    def test_prod_config_maps_router_v21_backtest_config(self):
        configs = prod_configs_to_second_configs({
            "BTC_10min_NORMAL_STATE_V21_LOSS_DENSITY_3OF6_8H": {
                "enabled": True,
                "model_type": "second_normal_router_v21",
                "second_horizon_sec": 600,
                "second_min_gap_sec": 600,
                "second_router_veto_low_up": True,
                "normal_state_loss_density_enabled": True,
            }
        })
        self.assertEqual(len(configs), 1)
        self.assertIsInstance(configs[0], SecondNormalRouterV21Config)
        self.assertTrue(configs[0].veto_low_up)
        self.assertTrue(configs[0].loss_density_enabled)

    def test_router_v21_low_up_veto_matches_live_rule(self):
        cfg = SecondNormalRouterV21Config(veto_low_up=True)
        row = {
            "role": "low",
            "signal": "UP",
            "observed600_pct": 100.0,
            "observed_lookback_pct": 100.0,
            "r10_bps": 20.0,
            "route_sigma_bps": 8.0,
        }
        ok, reason = _router_v21_candidate_allowed(row, cfg)
        self.assertFalse(ok)
        self.assertEqual(reason, "low_up_veto")


if __name__ == "__main__":
    unittest.main()
