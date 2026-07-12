from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "py"))

import collect_auction_data as auction


SNAPSHOT = {
    "lastUpdateId": 100,
    "bids": [["100.0", "3.0"], ["99.0", "2.0"]],
    "asks": [["101.0", "4.0"], ["102.0", "1.0"]],
}


class LocalOrderBookTest(unittest.TestCase):
    def setUp(self):
        self.book = auction.LocalOrderBook(view_levels=2)
        self.book.reset(SNAPSHOT)

    def test_apply_tracks_added_removed_depth_and_summary(self):
        outcome, delta = self.book.apply({
            "U": 101,
            "u": 102,
            "pu": 1000,
            "b": [["100.0", "5.0"], ["99.0", "0"]],
            "a": [["101.0", "2.0"], ["103.0", "3.0"]],
        })

        self.assertEqual(outcome, "applied")
        self.assertTrue(delta["snapshot_bridge"])
        self.assertEqual(delta["bid_added_qty"], 2.0)
        self.assertEqual(delta["bid_removed_qty"], 2.0)
        self.assertEqual(delta["ask_added_qty"], 3.0)
        self.assertEqual(delta["ask_removed_qty"], 2.0)
        self.assertEqual(delta["bid_added_near_qty"], 2.0)
        self.assertEqual(delta["bid_removed_near_qty"], 0.0)
        self.assertEqual(delta["ask_added_near_qty"], 0.0)
        self.assertEqual(delta["ask_removed_near_qty"], 2.0)
        self.assertEqual(delta["bid_change_count"], 2)
        self.assertEqual(delta["ask_change_count"], 2)
        self.assertEqual(self.book.last_update_id, 102)
        summary = self.book.summary()
        self.assertEqual(summary["best_bid"], 100.0)
        self.assertEqual(summary["best_ask"], 101.0)
        self.assertAlmostEqual(summary["imbalance_n"], 0.25)

    def test_after_bridge_requires_exact_previous_update(self):
        applied, _delta = self.book.apply({"U": 101, "u": 102, "pu": 900, "b": [], "a": []})
        self.assertEqual(applied, "applied")

        gap, detail = self.book.apply({"U": 103, "u": 104, "pu": 999, "b": [], "a": []})
        self.assertEqual(gap, "gap")
        self.assertEqual(detail, {"expected": 102, "previous": 999})

    def test_apply_rejects_gap_and_stale_update(self):
        gap, detail = self.book.apply({"U": 103, "u": 105, "pu": 102, "b": [], "a": []})
        self.assertEqual(gap, "gap")
        self.assertEqual(detail["expected"], 101)

        stale, detail = self.book.apply({"U": 90, "u": 100, "pu": 99, "b": [], "a": []})
        self.assertEqual(stale, "stale")
        self.assertEqual(detail, {})


class AuctionEventParseTest(unittest.TestCase):
    def setUp(self):
        self.collector = auction.AuctionCollector()
        self.events = []
        self.collector._append = lambda stream, timestamp, payload: self.events.append((stream, timestamp, payload))

    def test_trade_assigns_aggressor_from_maker_flag(self):
        self.collector.handle_trade({"e": "trade", "T": 1000, "s": "BTCUSDT", "t": 5, "p": "100", "q": "0.2", "m": False})
        self.collector.handle_trade({"e": "trade", "T": 1001, "s": "BTCUSDT", "t": 6, "p": "99", "q": "0.3", "m": True})

        self.assertEqual(self.events[0][0], "trades")
        self.assertEqual(self.events[0][2]["aggressor"], "BUY")
        self.assertEqual(self.events[1][2]["aggressor"], "SELL")
        self.assertEqual(self.collector.status["trades"], 2)

    def test_force_order_extracts_order_payload(self):
        self.collector.handle_force_order({"e": "forceOrder", "E": 1000, "o": {
            "s": "BTCUSDT", "S": "SELL", "o": "LIMIT", "f": "IOC", "X": "FILLED",
            "p": "100", "ap": "99.5", "q": "3", "z": "2", "l": "1", "T": 1002,
        }})

        stream, timestamp, event = self.events[0]
        self.assertEqual((stream, timestamp), ("force_orders", 1002))
        self.assertEqual(event["side"], "SELL")
        self.assertEqual(event["filled_quantity"], "2")
        self.assertEqual(self.collector.status["force_orders"], 1)


if __name__ == "__main__":
    unittest.main()
