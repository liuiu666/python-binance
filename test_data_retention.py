import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "py"))

from cleanup_data_retention import cleanup


class DataRetentionTest(unittest.TestCase):
    def test_cleanup_keeps_current_files_and_trims_logs(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            second = data / "second" / "BTCUSDT" / "futures"
            orderbook = data / "orderbook" / "BTCUSDT" / "futures"
            auction = data / "auction" / "BTCUSDT" / "futures" / "depth_updates" / "date=2026-07-15"
            second.mkdir(parents=True)
            orderbook.mkdir(parents=True)
            auction.mkdir(parents=True)
            (second / "2026-07-01.csv").write_text("old", encoding="utf-8")
            (second / "2026-07-15.csv").write_text("new", encoding="utf-8")
            (orderbook / "2026-07-01.csv").write_text("old", encoding="utf-8")
            (orderbook / "2026-07-15.csv").write_text("new", encoding="utf-8")
            (auction / "events.jsonl.gz").write_bytes(b"raw")
            audit = data / "signal_audit.jsonl"
            audit.write_text("".join(json.dumps({"n": value}) + "\n" for value in range(10)), encoding="utf-8")
            predictions = data / "orderbook_predictions.jsonl"
            predictions.write_text("".join(json.dumps({"n": value}) + "\n" for value in range(10)), encoding="utf-8")
            database = sqlite3.connect(data / "codex.db")
            database.executescript(
                "CREATE TABLE trade_audits(eventId TEXT, serverTime INTEGER);"
                "CREATE TABLE price_ticks(time INTEGER, price REAL);"
            )
            database.close()

            report = cleanup(
                data,
                market_days=7,
                auction_days=3,
                audit_lines=3,
                prediction_lines=4,
                database_days=7,
                remove_auction=True,
                today=date(2026, 7, 15),
            )

            self.assertFalse((second / "2026-07-01.csv").exists())
            self.assertTrue((second / "2026-07-15.csv").exists())
            self.assertFalse((orderbook / "2026-07-01.csv").exists())
            self.assertTrue((orderbook / "2026-07-15.csv").exists())
            self.assertFalse((data / "auction" / "BTCUSDT" / "futures").exists())
            self.assertEqual(len(audit.read_text(encoding="utf-8").splitlines()), 3)
            self.assertEqual(len(predictions.read_text(encoding="utf-8").splitlines()), 4)
            self.assertGreater(report["freedBytes"], 0)


if __name__ == "__main__":
    unittest.main()
