import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = os.path.dirname(os.path.abspath(__file__))
PY_DIR = os.path.join(ROOT, "py")
if PY_DIR not in sys.path:
    sys.path.insert(0, PY_DIR)

from signal_lock import acquire_singleton_lock, process_is_alive
from signal_state import load_audit_keys
from signal_io import json_safe
import signal_paths
from signal_runtime_cache import StaleWhileRefreshCache, empty_orderbook_features
from liquidity_v2_core import LiquidityV2Rules, evaluate_candidate, normal_ready, trend_space_veto_code
from normal_trend_latch_core import NormalTrendLatchEngine, passive_book_valid
from multi_normal_hf_stable_core import MultiNormalHFStableConfig, evaluate_snapshot as evaluate_multi_normal
from multiscale_phase_gate_core import evaluate_latest as evaluate_multiscale_phase_latest
from backtest_io import load_scan_times
import collect_second_data as second_data_collector


class MultiscalePhaseGateTests(unittest.TestCase):
    def test_non_finite_values_are_serialized_as_null(self):
        self.assertIsNone(json_safe(float("nan")))
        self.assertIsNone(json_safe(float("inf")))

    def test_nan_signal_is_never_emitted(self):
        import numpy as np
        import pandas as pd

        decision = evaluate_multiscale_phase_latest(pd.DataFrame([{
            "detected_time": pd.Timestamp("2026-07-12T00:00:59Z"),
            "signal": np.nan,
            "phase": "startup_or_middle",
            "reason": "phase_gate_startup_middle_skip",
        }]))
        self.assertIsNone(decision["signal"])
        self.assertEqual(decision["phase"], "startup_or_middle")


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
    def test_audit_key_loader_reads_only_the_tail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "signal_audit.jsonl"
            rows = [
                {"event": "signal_snapshot", "strategy_id": "old", "time": "2026-07-10T00:00:00Z", "actionable_time": "2026-07-10T00:00:00Z"},
                {"event": "signal_snapshot", "strategy_id": "keep", "time": "2026-07-10T00:00:01Z", "actionable_time": "2026-07-10T00:00:01Z"},
                {"event": "ignored", "strategy_id": "ignored", "time": "2026-07-10T00:00:02Z", "actionable_time": "2026-07-10T00:00:02Z"},
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            keys = load_audit_keys(str(path), limit=2)

        self.assertEqual(keys, {"signal_snapshot|keep|2026-07-10T00:00:01Z"})

    def test_stale_while_refresh_returns_last_good_value(self):
        now = [100.0]
        calls = []

        def fetch():
            calls.append(len(calls) + 1)
            return {"version": calls[-1]}

        cache = StaleWhileRefreshCache(fetch, refresh_sec=5.0, retry_sec=2.0, clock=lambda: now[0])
        self.assertEqual(cache.prime(), {"version": 1})
        now[0] = 104.0
        self.assertEqual(cache.get(), {"version": 1})
        self.assertEqual(calls, [1])

        now[0] = 106.0
        self.assertEqual(cache.get(), {"version": 1})
        self.assertEqual(cache.wait(1.0), {"version": 2})
        self.assertEqual(calls, [1, 2])
        self.assertIsNone(cache.status()["last_error"])

    def test_stale_while_refresh_keeps_value_after_fetch_error(self):
        now = [200.0]
        calls = [0]

        def fetch():
            calls[0] += 1
            if calls[0] > 1:
                raise RuntimeError("network down")
            return "healthy"

        cache = StaleWhileRefreshCache(fetch, refresh_sec=5.0, retry_sec=2.0, clock=lambda: now[0])
        cache.prime()
        now[0] = 206.0
        self.assertEqual(cache.get(), "healthy")
        self.assertEqual(cache.wait(1.0), "healthy")
        self.assertEqual(cache.status()["last_error"], "network down")

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

    def test_orderbook_rows_are_read_once_per_signal_cycle(self):
        rows = [{"timestamp": str(i)} for i in range(300)]
        import signal_runtime_cache as runtime_cache

        runtime_cache._ORDERBOOK_ROWS_CACHE.update({"signature": None, "rows": None, "limit": 0})
        runtime_cache.end_second_bars_cycle()
        with patch.object(runtime_cache, "file_signature", return_value=(1, 2)), patch.object(
            runtime_cache,
            "csv_tail_rows",
            return_value=rows,
        ) as read_tail:
            runtime_cache.begin_second_bars_cycle()
            first = runtime_cache.load_orderbook_rows_cached_for_cycle(100)
            second = runtime_cache.load_orderbook_rows_cached_for_cycle(200)
            runtime_cache.end_second_bars_cycle()

        self.assertEqual(first, rows[-100:])
        self.assertEqual(second, rows[-200:])
        read_tail.assert_called_once()

    def test_incremental_second_tail_replaces_overlap_without_duplicates(self):
        import pandas as pd
        import signal_runtime_cache as runtime_cache

        initial = pd.DataFrame({
            "time": pd.date_range("2026-07-10T00:00:00Z", periods=3, freq="s"),
            "close": [100.0, 101.0, 102.0],
        })
        latest = pd.DataFrame({
            "time": pd.date_range("2026-07-10T00:00:02Z", periods=2, freq="s"),
            "close": [102.5, 103.0],
        })
        merged = runtime_cache._merge_second_bar_tail(initial, latest, tail_sec=10)

        self.assertEqual(list(merged["time"]), list(pd.date_range("2026-07-10T00:00:00Z", periods=4, freq="s")))
        self.assertEqual(float(merged.iloc[2]["close"]), 102.5)
        self.assertEqual(float(merged.iloc[-1]["close"]), 103.0)


class SecondDataMaintenanceTests(unittest.TestCase):
    def test_gap_repair_scheduler_does_not_block_websocket_caller(self):
        started = threading.Event()
        release = threading.Event()
        state = {
            "last_gap_repair": 0.0,
            "gap_repair_worker": None,
            "status": {},
            "total_rows": 10,
        }

        def slow_repair(*_args, **_kwargs):
            started.set()
            release.wait(1.0)
            return {
                "enabled": True,
                "actualAdded": 1,
                "syntheticAdded": 1,
                "missingBefore": 2,
            }

        with patch.object(second_data_collector, "repair_recent_gaps", side_effect=slow_repair):
            before = time.perf_counter()
            self.assertTrue(second_data_collector.schedule_gap_repair(state, force=True))
            elapsed = time.perf_counter() - before
            self.assertLess(elapsed, 0.1)
            self.assertTrue(started.wait(0.5))
            self.assertFalse(second_data_collector.schedule_gap_repair(state, force=True))
            release.set()
            state["gap_repair_worker"].join(1.0)

        with patch.object(second_data_collector, "write_status"):
            self.assertEqual(second_data_collector.drain_gap_repair_results(state), 1)
        self.assertEqual(state["total_rows"], 12)
        self.assertEqual(state["status"]["rows"], 12)
        self.assertEqual(state["status"]["gap_repair"]["missingBefore"], 2)

    def test_websocket_flush_does_not_wait_for_maintenance_file_lock(self):
        locked = threading.Event()
        release = threading.Event()

        def hold_lock():
            with second_data_collector.CSV_WRITE_LOCK:
                locked.set()
                release.wait(1.0)

        worker = threading.Thread(target=hold_lock)
        worker.start()
        self.assertTrue(locked.wait(0.5))
        before = time.perf_counter()
        self.assertEqual(
            second_data_collector.flush_state({"nonblocking_writes": True}),
            0,
        )
        self.assertLess(time.perf_counter() - before, 0.1)
        release.set()
        worker.join(1.0)

    def test_websocket_rows_remain_pending_when_maintenance_lock_is_busy(self):
        locked = threading.Event()
        release = threading.Event()
        now_ms = int(time.time() * 1000)
        state = {
            "status": {},
            "pending": second_data_collector.pd.DataFrame(),
            "ws_rows": [{"a": 123, "p": "64000", "q": "0.01", "T": now_ms, "m": False}],
            "last_ws_flush": 0.0,
            "last_flush": 0.0,
            "total_rows": 0,
            "nonblocking_writes": True,
        }

        def hold_lock():
            with second_data_collector.CSV_WRITE_LOCK:
                locked.set()
                release.wait(1.0)

        worker = threading.Thread(target=hold_lock)
        worker.start()
        self.assertTrue(locked.wait(0.5))
        try:
            self.assertEqual(second_data_collector.flush_ws_rows(state), 0)
            self.assertEqual(len(state["ws_rows"]), 0)
            self.assertEqual(len(state["pending"]), 1)
            self.assertEqual(int(state["pending"].iloc[0]["last_agg_trade_id"]), 123)
        finally:
            release.set()
            worker.join(1.0)

        observed = []

        def capture_flush(current, **_kwargs):
            observed.append(len(current["pending"]))
            return 1

        with patch.object(second_data_collector, "_flush_state_unlocked", side_effect=capture_flush):
            self.assertEqual(second_data_collector.flush_state(state), 1)
        self.assertEqual(observed, [1])

    def test_archive_scheduler_does_not_block_websocket_caller(self):
        started = threading.Event()
        release = threading.Event()
        state = {
            "last_archive_check": 0.0,
            "archive_worker": None,
            "status": {},
            "total_rows": 10,
        }

        def slow_archive():
            started.set()
            release.wait(1.0)
            return 0

        with patch.object(second_data_collector, "archive_old_main_rows", side_effect=slow_archive):
            before = time.perf_counter()
            self.assertTrue(second_data_collector.schedule_archive(state, force=True))
            self.assertLess(time.perf_counter() - before, 0.1)
            self.assertTrue(started.wait(0.5))
            self.assertFalse(second_data_collector.schedule_archive(state, force=True))
            release.set()
            state["archive_worker"].join(1.0)

        with patch.object(second_data_collector, "write_status"):
            self.assertEqual(second_data_collector.drain_archive_results(state), 1)
        self.assertIsNone(state["archive_worker"])


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

    def test_trend_slope_limit_is_applied_once_during_normal_readiness(self):
        rules = LiquidityV2Rules(trend_space_enabled=True)
        row = self.reclaim_up_row(
            observed_pct=100.0,
            sigma_bps=10.0,
            center_slope_bps=6.1,
        )
        self.assertFalse(normal_ready(row, rules))
        self.assertIsNone(trend_space_veto_code("UP", "lower_fake_break_reclaim", row, rules))

class LiquidityV2BacktestTests(unittest.TestCase):
    def test_load_scan_times_accepts_audit_rows_and_deduplicates(self):
        with tempfile.TemporaryDirectory(prefix="scan-times-test-") as temp_dir:
            path = Path(temp_dir) / "scan_times.json"
            path.write_text(
                json.dumps([
                    {"time": "2026-07-10T00:00:01Z"},
                    "2026-07-10T00:00:02Z",
                    {"time": "2026-07-10T00:00:01Z"},
                    {"time": "invalid"},
                ]),
                encoding="utf-8",
            )
            scan_times = load_scan_times(path)

        self.assertEqual(len(scan_times), 2)


class NormalTrendLatchCoreTests(unittest.TestCase):
    @staticmethod
    def execution_row(imbalance=-0.2, micro=-0.01, bid=2.0, ask=1.0):
        import pandas as pd

        return pd.Series({
            "sigma_bps": 15.0,
            "state": "transition",
            "trend_direction": 1.0,
            "data_quality_ready": True,
            "imbalance_20": imbalance,
            "micro_bps": micro,
            "bid_qty_20": bid,
            "ask_qty_20": ask,
            "bid20_chg_30": 0.0,
            "ask20_chg_30": 0.0,
        })

    def test_latched_normal_emits_without_execution_book_recheck(self):
        import pandas as pd

        engine = NormalTrendLatchEngine({"router_execution_phase": 0})
        created = pd.Timestamp("2026-07-09T00:00:00Z")
        engine.latched = {
            "kind": "normal",
            "signal": "UP",
            "reason": "test",
            "band": "mid",
            "created_time": created,
            "expires_time": created + pd.Timedelta(seconds=6),
        }
        emitted = engine.step(created, self.execution_row())
        self.assertEqual(emitted["event"], "emitted")
        self.assertEqual(emitted["signal"]["signal"], "UP")
        self.assertEqual(emitted["signal"]["delay_sec"], 0)
        self.assertIsNone(engine.latched)

    def test_runtime_state_round_trip_preserves_latch(self):
        import pandas as pd

        engine = NormalTrendLatchEngine({})
        created = pd.Timestamp("2026-07-09T00:00:00Z")
        engine.latched = {
            "kind": "normal", "signal": "DOWN", "reason": "test", "band": "low",
            "created_time": created, "expires_time": created + pd.Timedelta(seconds=6),
        }
        restored = NormalTrendLatchEngine({})
        restored.restore_state(engine.export_state())
        self.assertEqual(restored.latched["signal"], "DOWN")
        self.assertEqual(restored.latched["band"], "low")

    def test_passive_book_requires_directional_support(self):
        rules = LiquidityV2Rules()
        self.assertFalse(passive_book_valid(self.execution_row(), "UP", rules))
        self.assertTrue(
            passive_book_valid(
                self.execution_row(imbalance=0.2, micro=0.01, bid=2.0, ask=1.0),
                "UP",
                rules,
            )
        )


class MultiNormalHFStableCoreTests(unittest.TestCase):
    @staticmethod
    def row(**updates):
        row = {
            "trend": "trend_up",
            "volatility": "sigma_high",
            "range": "range_wide",
            "normal_quality": "normal_weak",
            "normal_pos": "upper_edge",
            "sprint": "up_sprint",
            "volume": "vol_normal",
            "z": 0.6,
            "sigma10_bps": 8.0,
            "range10_bps": 35.0,
            "ret10_bps": 18.0,
            "ret30_bps": 25.0,
            "ret60_bps": 30.0,
            "flow5": 0.2,
            "imb20": 0.05,
            "sigma_expand": 1.5,
        }
        row.update(updates)
        return row

    def test_high_volatility_uses_dynamic_z_and_fades_exhaustion(self):
        decision = evaluate_multi_normal(self.row(), MultiNormalHFStableConfig())
        self.assertEqual(decision["signal"], "DOWN")
        self.assertEqual(decision["module"], "mature_trend_exhaustion")
        self.assertEqual(decision["z_required"], 0.5)

    def test_orderbook_still_supporting_trend_blocks_signal(self):
        decision = evaluate_multi_normal(self.row(imb20=0.081), MultiNormalHFStableConfig())
        self.assertIsNone(decision["signal"])


if __name__ == "__main__":
    unittest.main()
