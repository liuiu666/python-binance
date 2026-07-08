from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "py"))

import collect_second_data as collector


class CollectSecondDataTest(unittest.TestCase):
    def test_repair_rejects_invalid_ohlc_rows(self):
        header = ",".join(collector.CSV_COLUMNS)
        good = (
            "2026-07-06T18:07:03.000000Z,BTCUSDT,futures,63678.0,63679.0,63677.0,63678.6,"
            "1.0,63678.6,2,0.4,0.6,25471.44,38207.16,0.666666,"
            "2026-07-06T18:07:03.000000Z,2026-07-06T18:07:03.500000Z,10,11"
        )
        bad_zero = good.replace("63678.0,63679.0,63677.0", "0.0,63679.0,0.0")
        bad_range = good.replace("63678.0,63679.0,63677.0", "63678.0,63670.0,63677.0")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "btcusdt_1s_trades.csv"
            path.write_text("\n".join([header, bad_zero, bad_range, good]) + "\n", encoding="utf-8")

            result = collector.repair_csv_file(str(path))
            repaired = path.read_text(encoding="utf-8")

        self.assertTrue(result["rewritten"])
        self.assertEqual(result["invalidRows"], 2)
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["lastTs"], "2026-07-06T18:07:03+00:00")
        self.assertIn(good, repaired)
        self.assertNotIn("0.0,63679.0,0.0", repaired)

    def test_aggregate_skips_zero_price_and_duplicate_trade_ids(self):
        rows = [
            {"e": "trade", "t": 100, "p": "0", "q": "1", "T": 1_788_633_600_000, "m": False},
            {"e": "trade", "t": 101, "p": "100.0", "q": "1", "T": 1_788_633_600_100, "m": False},
            {"e": "trade", "t": 101, "p": "101.0", "q": "1", "T": 1_788_633_600_200, "m": False},
            {"e": "trade", "t": 102, "p": "102.0", "q": "2", "T": 1_788_633_600_300, "m": True},
        ]

        bars = collector.aggregate_trades(rows)

        self.assertEqual(len(bars), 1)
        self.assertEqual(float(bars.iloc[0]["open"]), 100.0)
        self.assertEqual(float(bars.iloc[0]["close"]), 102.0)
        self.assertEqual(float(bars.iloc[0]["volume"]), 3.0)
        self.assertEqual(int(bars.iloc[0]["trades"]), 2)


if __name__ == "__main__":
    unittest.main()
