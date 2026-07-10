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


if __name__ == "__main__":
    unittest.main()
