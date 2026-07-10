import os
import shutil
import socket
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.abspath(__file__))
PY_DIR = os.path.join(ROOT, "py")
if PY_DIR not in sys.path:
    sys.path.insert(0, PY_DIR)

from signal_lock import acquire_singleton_lock, process_is_alive
import signal_paths
from signal_runtime_cache import empty_orderbook_features
from liquidity_v2_core import LiquidityV2Rules, evaluate_candidate


class SignalLockTests(unittest.TestCase):
    def test_process_is_alive(self):
        self.assertTrue(process_is_alive(os.getpid()))
        self.assertFalse(process_is_alive(-1))

    def test_second_instance_is_rejected(self):
        temp_dir = tempfile.mkdtemp(prefix="signal-lock-test-")
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        lock_file = os.path.join(temp_dir, "signal.lock")
        lock_dir = os.path.join(temp_dir, "signal.lockdir")
        handle = sock = None
        try:
            handle, sock = acquire_singleton_lock(
                temp_dir,
                lock_file,
                lock_dir,
                port,
                label="unit-test",
            )
            with self.assertRaises(SystemExit):
                acquire_singleton_lock(
                    temp_dir,
                    lock_file,
                    lock_dir,
                    port,
                    label="unit-test",
                )
        finally:
            if sock is not None:
                sock.close()
            if handle is not None:
                handle.close()
            shutil.rmtree(temp_dir, ignore_errors=True)


class SignalPathTests(unittest.TestCase):
    def test_runtime_files_share_data_directory(self):
        names = {
            signal_paths.SIGNAL_FILE: "live_signals.json",
            signal_paths.CONFIG_FILE: "prod_config.json",
            signal_paths.SIGNAL_AUDIT_FILE: "signal_audit.jsonl",
            signal_paths.SIGNAL_STATE_FILE: "signal_state.json",
            signal_paths.SECOND_TRADES_FILE: "btcusdt_1s_trades.csv",
            signal_paths.ORDERBOOK_FILE: "btcusdt_orderbook_1s.csv",
        }
        for path, filename in names.items():
            self.assertEqual(os.path.dirname(path), signal_paths.OUT)
            self.assertEqual(os.path.basename(path), filename)


class SignalRuntimeCacheTests(unittest.TestCase):
    def test_empty_orderbook_features_keep_expected_schema(self):
        import pandas as pd

        index = pd.date_range("2026-07-10T00:00:00Z", periods=3, freq="s")
        features = empty_orderbook_features(index, "test-source")
        self.assertEqual(list(features.index), list(index))
        self.assertEqual(
            list(features.columns),
            ["ob_available", "ob_imb20", "ob_micro_bps", "ob_spread_bps", "orderbook_source"],
        )
        self.assertFalse(features["ob_available"].any())
        self.assertEqual(set(features["orderbook_source"]), {"test-source"})


class LiquidityV2CoreTests(unittest.TestCase):
    @staticmethod
    def reclaim_up_row(**updates):
        row = {
            "z": -0.5,
            "flow_60": 0.0,
            "imbalance_20": 0.2,
            "micro_bps": 0.01,
            "ask_qty_20": 1.0,
            "bid_qty_20": 2.0,
            "bid20_chg_30": 0.0,
            "ask20_chg_30": 0.0,
            "z_max_retest": 0.0,
            "z_min_retest": -1.3,
            "ret_300s_bps": 0.0,
            "ret_600s_bps": 0.0,
            "bid20_chg_60": 0.0,
            "sigma_expand": 1.0,
            "center_slope_bps": 0.0,
            "inside1_ratio": 0.6,
            "ret_1800s_bps": 0.0,
            "pos_1800s": 0.5,
            "pos_600s": 0.5,
        }
        row.update(updates)
        return row

    def test_clean_reclaim_up_is_accepted(self):
        decision = evaluate_candidate(self.reclaim_up_row(), LiquidityV2Rules(trend_space_enabled=True))
        self.assertEqual(decision["status"], "accepted")
        self.assertEqual(decision["signal"], "UP")
        self.assertEqual(decision["reason"], "lower_fake_break_reclaim")

    def test_negative_flow_vetoes_reclaim_up(self):
        decision = evaluate_candidate(
            self.reclaim_up_row(flow_60=-0.1),
            LiquidityV2Rules(trend_space_enabled=True),
        )
        self.assertEqual(decision["status"], "veto")
        self.assertEqual(decision["reason"], "liq_v2_skip_up_negative_flow")

    def test_extreme_bidwall_trap_is_skipped_before_flip(self):
        decision = evaluate_candidate(
            self.reclaim_up_row(ret_300s_bps=-6.0, ret_600s_bps=-25.0, bid20_chg_60=2.5),
            LiquidityV2Rules(trend_space_enabled=True),
        )
        self.assertEqual(decision["status"], "veto")
        self.assertEqual(decision["reason"], "bidwall_trap_extreme_drop_skip")
        self.assertEqual(decision["blocked_signal"], "DOWN")


if __name__ == "__main__":
    unittest.main()
