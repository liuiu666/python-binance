from __future__ import annotations

import sys
import gzip
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "py"))

import build_auction_features as features


class AuctionFeaturesTest(unittest.TestCase):
    def test_reads_compressed_partition(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trades" / "date=2026-07-12" / "events.jsonl.gz"
            path.parent.mkdir(parents=True)
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                handle.write(json.dumps({"event_time_ms": 1_000, "aggressor": "BUY"}) + "\n")
            rows, invalid = features.read_partition(Path(tmp), "trades", "2026-07-12")

        self.assertEqual(invalid, 0)
        self.assertEqual(rows, [{"event_time_ms": 1_000, "aggressor": "BUY"}])

    def test_builds_causal_trade_depth_and_force_order_features(self):
        rows = features.build_features(
            trades=[
                {"event_time_ms": 1_000, "price": "100", "quantity": "2", "aggressor": "BUY"},
                {"event_time_ms": 1_500, "price": "101", "quantity": "1", "aggressor": "SELL"},
                {"event_time_ms": 2_100, "price": "102", "quantity": "3", "aggressor": "BUY"},
            ],
            depths=[
                {"event_time_ms": 1_600, "mid": 100.5, "spread_bps": 1, "bid_depth_n": 8, "ask_depth_n": 4,
                 "imbalance_n": 0.33, "bid_added_qty": 5, "bid_removed_qty": 1, "ask_added_qty": 2, "ask_removed_qty": 3,
                 "bid_added_near_qty": 3, "bid_removed_near_qty": 0.5, "ask_added_near_qty": 1, "ask_removed_near_qty": 2},
                {"event_time_ms": 2_500, "mid": 102.5, "spread_bps": 1, "bid_depth_n": 7, "ask_depth_n": 5,
                 "imbalance_n": 0.17, "bid_added_qty": 1, "bid_removed_qty": 2, "ask_added_qty": 4, "ask_removed_qty": 1,
                 "bid_added_near_qty": 0.5, "bid_removed_near_qty": 1, "ask_added_near_qty": 2, "ask_removed_near_qty": 0.5},
            ],
            force_orders=[
                {"event_time_ms": 2_700, "side": "SELL", "filled_quantity": "0.5"},
            ],
        )

        self.assertEqual(len(rows), 2)
        first, second = rows
        self.assertEqual(first["trade_count"], 2)
        self.assertEqual(first["signed_flow_qty"], 1.0)
        self.assertEqual(first["net_bid_liquidity"], 4.0)
        self.assertEqual(first["net_ask_liquidity"], -1.0)
        self.assertTrue(first["near_liquidity_available"])
        self.assertEqual(first["near_net_bid_liquidity"], 2.5)
        self.assertEqual(first["near_net_ask_liquidity"], -1.0)
        self.assertAlmostEqual(first["near_liquidity_pressure_ratio_10s"], 2.5 / 8 - (-1 / 4))
        self.assertTrue(first["depth_available"])
        self.assertEqual(second["force_signed_qty"], -0.5)
        self.assertAlmostEqual(second["flow_10s"], 405.0)
        self.assertAlmostEqual(second["ret_10s_bps"], (102.5 - 100.5) / 100.5 * 10000.0)
        self.assertEqual(second["depth_coverage_10s"], 1.0)

    def test_marks_missing_orderbook_as_unavailable(self):
        rows = features.build_features(
            trades=[{"event_time_ms": 1_000, "price": "100", "quantity": "1", "aggressor": "BUY"}],
            depths=[],
            force_orders=[],
        )

        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["depth_available"])
        self.assertIsNone(rows[0]["ret_10s_bps"])


if __name__ == "__main__":
    unittest.main()
