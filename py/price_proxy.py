"""Price proxy: fetches BTC price via Python requests, writes to file every 2s"""
import json
import os
import atexit
import shutil
import socket
import sys
import time
try:
    import msvcrt
    fcntl = None
except ImportError:
    msvcrt = None
    import fcntl

APP_DIR = os.environ.get("APP_DIR", "E:/codex")
OUT = os.environ.get("DATA_DIR", os.path.join(APP_DIR, "data"))
PRICE_FILE = os.path.join(OUT, "current_price.json")
LOCK_FILE = os.path.join(OUT, "price_proxy.lock")
LOCK_DIR = os.path.join(OUT, "price_proxy.lockdir")
LOCK_PORT = 39870
BASE_URLS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
]


def acquire_singleton_lock():
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    try:
        os.mkdir(LOCK_DIR)
        with open(os.path.join(LOCK_DIR, "pid"), "w", encoding="utf-8") as fpid:
            fpid.write(str(os.getpid()))
        atexit.register(lambda: shutil.rmtree(LOCK_DIR, ignore_errors=True))
    except FileExistsError:
        pid_path = os.path.join(LOCK_DIR, "pid")
        old_pid = None
        try:
            with open(pid_path, "r", encoding="utf-8") as fpid:
                old_pid = int((fpid.read() or "0").strip())
            os.kill(old_pid, 0)
            print(f"[PriceProxy] Another price_proxy.py instance is active pid={old_pid}; exiting.")
            sys.exit(0)
        except Exception:
            shutil.rmtree(LOCK_DIR, ignore_errors=True)
            try:
                os.mkdir(LOCK_DIR)
                with open(pid_path, "w", encoding="utf-8") as fpid:
                    fpid.write(str(os.getpid()))
                atexit.register(lambda: shutil.rmtree(LOCK_DIR, ignore_errors=True))
            except FileExistsError:
                print("[PriceProxy] Another price_proxy.py instance acquired the directory lock; exiting.")
                sys.exit(0)
    f = open(LOCK_FILE, "a+", encoding="utf-8")
    try:
        f.seek(0)
        if msvcrt is not None:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
    except OSError:
        print(f"[PriceProxy] Another price_proxy.py instance holds {LOCK_FILE}; exiting.")
        sys.exit(0)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return f, s
    except OSError:
        print(f"[PriceProxy] Another price_proxy.py instance is already running on lock port {LOCK_PORT}; exiting.")
        sys.exit(0)


LOCK_HANDLE, LOCK_SOCKET = acquire_singleton_lock()

import requests

print("[PriceProxy] Starting...")

while True:
    try:
        last_err = None
        for base in BASE_URLS:
            try:
                r = requests.get(f"{base}/api/v3/ticker/price?symbol=BTCUSDT", timeout=5)
                r.raise_for_status()
                break
            except Exception as e:
                last_err = e
                r = None
        if r is None:
            raise last_err
        data = r.json()
        price = float(data["price"])
        with open(PRICE_FILE, "w") as f:
            json.dump({"price": price, "time": int(time.time() * 1000)}, f)
        print(f"\r  ${price:,.2f}", end="", flush=True)
    except Exception as e:
        print(f"\n  Error: {e}", end="", flush=True)
    time.sleep(2)
