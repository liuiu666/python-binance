"""Singleton locking helpers for the live signal process."""

import atexit
import os
import shutil
import socket
import sys

try:
    import msvcrt
    fcntl = None
except ImportError:
    msvcrt = None
    import fcntl


def process_is_alive(pid):
    if not pid or int(pid) <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
            return True
        except PermissionError:
            return True
        except ProcessLookupError:
            return False

    import ctypes

    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        process_query_limited_information,
        False,
        int(pid),
    )
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def acquire_singleton_lock(out_dir, lock_file, lock_dir, lock_port, label="signal_btc.py"):
    os.makedirs(out_dir, exist_ok=True)
    try:
        os.mkdir(lock_dir)
        with open(os.path.join(lock_dir, "pid"), "w", encoding="utf-8") as fpid:
            fpid.write(str(os.getpid()))
        atexit.register(lambda: shutil.rmtree(lock_dir, ignore_errors=True))
    except FileExistsError:
        pid_path = os.path.join(lock_dir, "pid")
        try:
            with open(pid_path, "r", encoding="utf-8") as fpid:
                old_pid = int((fpid.read() or "0").strip())
            if process_is_alive(old_pid):
                print(f"[Signal] Another {label} instance is active pid={old_pid}; exiting.")
                sys.exit(0)
            raise ProcessLookupError(old_pid)
        except Exception:
            shutil.rmtree(lock_dir, ignore_errors=True)
            try:
                os.mkdir(lock_dir)
                with open(pid_path, "w", encoding="utf-8") as fpid:
                    fpid.write(str(os.getpid()))
                atexit.register(lambda: shutil.rmtree(lock_dir, ignore_errors=True))
            except FileExistsError:
                print(f"[Signal] Another {label} instance acquired the directory lock; exiting.")
                sys.exit(0)

    handle = open(lock_file, "a+", encoding="utf-8")
    try:
        handle.seek(0)
        if msvcrt is not None:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
    except OSError:
        print(f"[Signal] Another {label} instance holds {lock_file}; exiting.")
        sys.exit(0)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", lock_port))
        sock.listen(1)
        return handle, sock
    except OSError:
        print(f"[Signal] Another {label} instance is already running on lock port {lock_port}; exiting.")
        sys.exit(0)
