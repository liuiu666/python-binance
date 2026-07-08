"""BTC dual-strategy signal service.

Outputs the production BTC strategy signals for the tablet executor.
"""
import json
import math
import os
import pickle
import atexit
import shutil
import socket
import sys
import time
import warnings
try:
    import msvcrt
    fcntl = None
except ImportError:
    msvcrt = None
    import fcntl

APP_DIR = os.environ.get("APP_DIR", "E:/codex")
OUT = os.environ.get("DATA_DIR", os.path.join(APP_DIR, "data"))
SIGNAL_FILE = os.path.join(OUT, "live_signals.json")
CONFIG_FILE = os.path.join(OUT, "prod_config.json")
SIGNAL_AUDIT_FILE = os.path.join(OUT, "signal_audit.jsonl")
LOCK_FILE = os.path.join(OUT, "signal_btc.lock")
LOCK_DIR = os.path.join(OUT, "signal_btc.lockdir")
HISTORY_1M_FILE = os.path.join(OUT, "btcusdt_1m.csv")
TAKER_FILE = os.path.join(OUT, "btcusdt_taker.csv")
LS_RATIO_FILE = os.path.join(OUT, "btcusdt_lsratio.csv")
FUNDING_FILE = os.path.join(OUT, "btcusdt_funding.csv")
SECOND_TRADES_FILE = os.path.join(OUT, "btcusdt_1s_trades.csv")
ORDERBOOK_FILE = os.path.join(OUT, "btcusdt_orderbook_1s.csv")

# ── 秒级 bars 内存缓存 ──────────────────────────────────────────────────────
# 所有 SecondNormalStrategy 实例共享同一份 bars，避免每次 predict 全量读盘。
# 最多每 SECOND_BARS_CACHE_TTL 秒刷新一次（增量追加新行）。
SECOND_BARS_DEFAULT_TAIL_SEC = int(os.environ.get("SECOND_SIGNAL_TAIL_SEC", str(6 * 60 * 60)))
_SECOND_BARS_REQUIRED_SEC = SECOND_BARS_DEFAULT_TAIL_SEC
_SECOND_BARS_CACHE: dict = {
    "bars": None,          # pd.DataFrame | None
    "file_size": 0,        # 上次读取时的文件字节数
    "last_check": 0.0,     # 上次检查时间 (time.time())
    "tail_sec": 0,
}
SECOND_BARS_CACHE_TTL = 1.0   # 秒，最短刷新间隔
_SECOND_BARS_CYCLE_CACHE = {"active": False, "bars": None}
_MINUTE_FEATURE_CACHE = {"signature": None, "features": None}
_ORDERBOOK_FEATURE_CACHE = {"key": None, "features": None}
_NORMAL_V11_CONTEXT_CACHE = {"key": None, "context": None}
ORDERBOOK_FEATURE_TAIL_ROWS = int(os.environ.get("ORDERBOOK_FEATURE_TAIL_ROWS", "18000"))
ORDERBOOK_FEATURE_TAIL_CHUNK = int(os.environ.get("ORDERBOOK_FEATURE_TAIL_CHUNK", str(12 * 1024 * 1024)))
SHADOW_CANDIDATES = [
    {
        "id": "SHADOW_10m_strict_th58_rsi30_70_all3",
        "base": "BTC_10min",
        "threshold": 0.58,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "all3",
        "note": "Strict walk-forward 10m candidate; shadow only until live sample confirms.",
    },
    {
        "id": "SHADOW_10m_guard_th68_rsi30_70_all3",
        "base": "BTC_10min",
        "threshold": 0.68,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "all3",
        "note": "High-strength 10m guard candidate aligned with live shadow safety review.",
    },
    {
        "id": "SHADOW_10m_more_trades_th60_rsi35_65_vol_hi_majority",
        "base": "BTC_10min",
        "threshold": 0.60,
        "rsi_lo": 35,
        "rsi_hi": 65,
        "vol_min_rank": 0.60,
        "agree_mode": "majority",
        "note": "Walk-forward alternative with more trades, not production.",
    },
    {
        "id": "SHADOW_10m_recent_scan_th65_rsi35_65_all3",
        "base": "BTC_10min",
        "threshold": 0.65,
        "rsi_lo": 35,
        "rsi_hi": 65,
        "vol_min_rank": None,
        "agree_mode": "all3",
        "note": "Recent-history high-WR candidate; strict validation was weak, shadow only.",
    },
    {
        "id": "SHADOW_10m_ctcool_t630_str30",
        "base": "BTC_10min",
        "threshold": 0.55,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "majority",
        "countertrend_max_abs_trend6": 0.0030,
        "countertrend_max_strength": 30,
        "note": "10m counter-trend cooling guard; shadow only until live sample confirms.",
    },
    {
        "id": "SHADOW_10m_bbp_cap105_th55_rsi30_70_majority",
        "base": "BTC_10min",
        "threshold": 0.55,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "majority",
        "bbp_cap": 1.05,
        "note": "10m BBP regime cap; shadow only until live sample confirms.",
    },
    {
        "id": "SHADOW_10m_bbp_cap120_th55_rsi30_70_majority",
        "base": "BTC_10min",
        "threshold": 0.55,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "majority",
        "bbp_cap": 1.20,
        "note": "10m high-retention BBP regime cap; shadow only until live sample confirms.",
    },
    {
        "id": "SHADOW_10m_bbp120_rsi76_th55_rsi30_70_majority",
        "base": "BTC_10min",
        "threshold": 0.55,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "majority",
        "bbp_cap": 1.20,
        "rsi_extreme_cap": 76,
        "note": "10m balanced BBP+RSI overextension guard; shadow only until live sample confirms.",
    },
    {
        "id": "SHADOW_10m_bbp105_rsi74_th55_rsi30_70_majority",
        "base": "BTC_10min",
        "threshold": 0.55,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "majority",
        "bbp_cap": 1.05,
        "rsi_extreme_cap": 74,
        "note": "10m WR-first BBP+RSI overextension guard; shadow only until live sample confirms.",
    },
    {
        "id": "SHADOW_10m_rsi_cap74_th55_rsi30_70_majority",
        "base": "BTC_10min",
        "threshold": 0.55,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "majority",
        "rsi_extreme_cap": 74,
        "note": "10m RSI stretch cap; shadow only until live sample confirms.",
    },
    {
        "id": "SHADOW_10m_skip_hour12_th55_rsi30_70_majority",
        "base": "BTC_10min",
        "threshold": 0.55,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "majority",
        "extra_skip_hours_utc": [12],
        "note": "10m extra UTC hour-12 filter; shadow only until live sample confirms.",
    },
    {
        "id": "SHADOW_10m_skip_hours1_8_th55_rsi30_70_majority",
        "base": "BTC_10min",
        "threshold": 0.55,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "majority",
        "extra_skip_hours_utc": [1, 8],
        "note": "10m live-drift UTC hour-1/8 filter; shadow only until live sample confirms.",
    },
    {
        "id": "SHADOW_10m_conf_lt40_th55_rsi30_70_majority",
        "base": "BTC_10min",
        "threshold": 0.55,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "majority",
        "confidence_max": 40,
        "note": "10m high-strength confidence cap; shadow only until live sample confirms.",
    },
    {
        "id": "SHADOW_10m_bbp105_conf_lt40_th55_rsi30_70_majority",
        "base": "BTC_10min",
        "threshold": 0.55,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "majority",
        "bbp_cap": 1.05,
        "confidence_max": 40,
        "note": "10m BBP plus confidence drift guard; shadow only until live sample confirms.",
    },
    {
        "id": "SHADOW_10m_bbp105_rsi78_conf_lt40_th55_rsi30_70_majority",
        "base": "BTC_10min",
        "threshold": 0.55,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "majority",
        "bbp_cap": 1.05,
        "rsi_extreme_cap": 78,
        "confidence_max": 40,
        "note": "10m WR-first BBP+RSI+confidence guard; shadow only until live sample confirms.",
    },
    {
        "id": "SHADOW_10m_bbp105_rsi78_conf_lt50_th55_rsi30_70_majority",
        "base": "BTC_10min",
        "threshold": 0.55,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "majority",
        "bbp_cap": 1.05,
        "rsi_extreme_cap": 78,
        "confidence_max": 50,
        "note": "10m balanced BBP+RSI+confidence guard; shadow only until live sample confirms.",
    },
    {
        "id": "SHADOW_10m_bbp120_rsi74_conf_lt50_th55_rsi30_70_majority",
        "base": "BTC_10min",
        "threshold": 0.55,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "majority",
        "bbp_cap": 1.20,
        "rsi_extreme_cap": 74,
        "confidence_max": 50,
        "note": "10m moderate-retention BBP+RSI+confidence guard; shadow only until live sample confirms.",
    },
    {
        "id": "SHADOW_30m_stable_th58_rsi30_70_all3",
        "base": "BTC_30min",
        "threshold": 0.58,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "all3",
        "note": "Strict walk-forward 30m stable candidate; shadow only until live sample confirms.",
    },
    {
        "id": "SHADOW_30m_guard_th68_rsi30_70_all3",
        "base": "BTC_30min",
        "threshold": 0.68,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "all3",
        "note": "High-strength 30m guard candidate aligned with live shadow safety review.",
    },
    {
        "id": "SHADOW_30m_ctcool_t625_str30",
        "base": "BTC_30min",
        "threshold": 0.58,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "majority",
        "countertrend_max_abs_trend6": 0.0025,
        "countertrend_max_strength": 30,
        "note": "30m counter-trend cooling guard; shadow only until live sample confirms.",
    },
    {
        "id": "SHADOW_30m_conf_lt40_th55_rsi30_70_majority",
        "base": "BTC_30min",
        "threshold": 0.55,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "majority",
        "confidence_max": 40,
        "note": "30m confidence cap with highest offline WR in focused scan; shadow only.",
    },
    {
        "id": "SHADOW_30m_conf_lt50_th55_rsi30_70_majority",
        "base": "BTC_30min",
        "threshold": 0.55,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "majority",
        "confidence_max": 50,
        "note": "30m balanced confidence cap with high retention; shadow only.",
    },
    {
        "id": "SHADOW_30m_skip_hour12_th55_rsi30_70_majority",
        "base": "BTC_30min",
        "threshold": 0.55,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "majority",
        "extra_skip_hours_utc": [12],
        "note": "30m extra UTC hour-12 filter; shadow only.",
    },
    {
        "id": "SHADOW_30m_skip_hour6_th55_rsi30_70_majority",
        "base": "BTC_30min",
        "threshold": 0.55,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "majority",
        "extra_skip_hours_utc": [6],
        "note": "30m extra UTC hour-6 filter; shadow only.",
    },
    {
        "id": "SHADOW_30m_bbp105_rsi80_th55_rsi30_70_majority",
        "base": "BTC_30min",
        "threshold": 0.55,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "vol_min_rank": None,
        "agree_mode": "majority",
        "bbp_cap": 1.05,
        "rsi_extreme_cap": 80,
        "note": "30m BBP+RSI overextension guard; shadow only because max loss did not improve offline.",
    },
]
STATEFUL_SHADOW_CANDIDATES = [
    {
        "id": "STATEFUL_10m_bbp_1.20_rsi_cap_74_confidence_lt_50_one_open_position",
        "base": "BTC_10min",
        "source_shadow": "SHADOW_10m_bbp120_rsi74_conf_lt50_th55_rsi30_70_majority",
        "policy": "one_open_position",
        "note": "10m stateful overlay: BBP+RSI+confidence guard plus one open shadow position at a time.",
    },
]
META_GATE_SHADOW_CANDIDATES = [
    {
        "id": "SHADOW_META_30m_signal_quality_th65",
        "base": "BTC_30min",
        "model_id": "BTC_30min_signal_quality",
        "threshold": 0.65,
        "note": "30m second-stage signal-quality gate; meta-OOS +1.47pp with 57% retention, shadow only.",
    },
]
TWO_MINUTE_LIVE_CANDIDATES = [
    {
        "id": "BTC_10min",
        "base": "BTC_10min",
        "model_id": "BTC_2m_10min_primary_lowvol_up_gate",
        "live": True,
        "note": "LIVE 10m signal: 2m aggregated research model with regime thresholds and low-volatility UP strength gate.",
    },
]
TWO_MINUTE_SHADOW_CANDIDATES = []
RULE_SHADOW_CANDIDATES = [
    {
        "id": "SHADOW_RULE_10m_rsi_reversal_30_70",
        "base": "BTC_10min",
        "kind": "rsi_reversal",
        "rsi_lo": 30,
        "rsi_hi": 70,
        "trend_gate": "none",
        "note": "Rule-only RSI mean reversion; live shadow only.",
    },
    {
        "id": "SHADOW_RULE_10m_rsi_reversal_no_strong_trend",
        "base": "BTC_10min",
        "kind": "rsi_reversal",
        "rsi_lo": 30,
        "rsi_hi": 70,
        "trend_gate": "no_strong_trend_score3",
        "note": "Rule-only RSI mean reversion, skipped in strong trend; live shadow only.",
    },
    {
        "id": "SHADOW_RULE_10m_pullback_follow",
        "base": "BTC_10min",
        "kind": "pullback_follow",
        "score_min": 3,
        "note": "Rule-only trend pullback follow; live shadow only.",
    },
    {
        "id": "SHADOW_RULE_10m_hybrid_regime",
        "base": "BTC_10min",
        "kind": "hybrid_regime",
        "rsi_lo": 30,
        "rsi_hi": 70,
        "score_min": 3,
        "note": "Trend-follow in strong trend, RSI reversal in range; live shadow only.",
    },
    {
        "id": "SHADOW_RULE_30m_rsi_reversal_30_70",
        "base": "BTC_30min",
        "kind": "rsi_reversal",
        "rsi_lo": 30,
        "rsi_hi": 70,
        "trend_gate": "none",
        "note": "Rule-only RSI mean reversion; live shadow only.",
    },
    {
        "id": "SHADOW_RULE_30m_rsi_reversal_no_strong_trend",
        "base": "BTC_30min",
        "kind": "rsi_reversal",
        "rsi_lo": 30,
        "rsi_hi": 70,
        "trend_gate": "no_strong_trend_score3",
        "note": "Rule-only RSI mean reversion, skipped in strong trend; live shadow only.",
    },
    {
        "id": "SHADOW_RULE_30m_pullback_follow",
        "base": "BTC_30min",
        "kind": "pullback_follow",
        "score_min": 3,
        "note": "Rule-only trend pullback follow; live shadow only.",
    },
    {
        "id": "SHADOW_RULE_30m_hybrid_regime",
        "base": "BTC_30min",
        "kind": "hybrid_regime",
        "rsi_lo": 30,
        "rsi_hi": 70,
        "score_min": 3,
        "note": "Trend-follow in strong trend, RSI reversal in range; live shadow only.",
    },
]
BASE_URLS = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
]
LOCK_PORT = 39871


def acquire_singleton_lock():
    os.makedirs(OUT, exist_ok=True)
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
            print(f"[Signal] Another signal_btc.py instance is active pid={old_pid}; exiting.")
            sys.exit(0)
        except Exception:
            shutil.rmtree(LOCK_DIR, ignore_errors=True)
            try:
                os.mkdir(LOCK_DIR)
                with open(pid_path, "w", encoding="utf-8") as fpid:
                    fpid.write(str(os.getpid()))
                atexit.register(lambda: shutil.rmtree(LOCK_DIR, ignore_errors=True))
            except FileExistsError:
                print("[Signal] Another signal_btc.py instance acquired the directory lock; exiting.")
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
        print(f"[Signal] Another signal_btc.py instance holds {LOCK_FILE}; exiting.")
        sys.exit(0)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return f, s
    except OSError:
        print(f"[Signal] Another signal_btc.py instance is already running on lock port {LOCK_PORT}; exiting.")
        sys.exit(0)


LOCK_HANDLE, LOCK_SOCKET = acquire_singleton_lock()

import pandas as pd
import numpy as np
import requests
from lightgbm import LGBMClassifier
from xgboost import XGBClassifier

warnings.filterwarnings("ignore")

HISTORY_1M_MAX_AGE = pd.Timedelta(minutes=15)
EXTERNAL_RATIO_MAX_AGE = pd.Timedelta(minutes=30)
FUNDING_MAX_AGE = pd.Timedelta(hours=12)
LIVE_1M_MAX_AGE = pd.Timedelta(minutes=3)
MAX_HISTORY_LIVE_GAP = pd.Timedelta(minutes=2)
MAX_5M_LIVE_MERGE_GAP = pd.Timedelta(minutes=7)
ENABLE_SIGNAL_SHADOWS = os.environ.get("ENABLE_SIGNAL_SHADOWS", "0") == "1"
ENABLE_LEGACY_TWO_MINUTE_LIVE = os.environ.get("ENABLE_LEGACY_TWO_MINUTE_LIVE", "0") == "1"

sys.path.insert(0, os.path.join(APP_DIR, "py"))
from backtest_enhanced import build_features, load_symbol
from second_backtest.data import load_recent_second_bars, load_second_bars
from second_backtest.dynamic_zone import (
    compact_zone_context,
    dynamic_zone_allows,
    dynamic_zone_context_from_bars,
    dynamic_zone_signal_hint,
    is_dynamic_zone_filter_enabled,
)
from second_backtest.incident_filter import apply_incident_filter_to_live_signals, incident_config_from_dict
import research_normal_state_v1 as normal_state_v1
import research_normal_state_v6 as normal_state_v6
try:
    from research_2m_10min_binary import (
        SYMBOL as RESEARCH_2M_SYMBOL,
        aggregate_bars as aggregate_2m_bars,
        build_features as build_2m_features,
        load_1m as load_2m_1m,
        merge_external as merge_2m_external,
    )
    from research_regime_strategy_2m import classify_regime as classify_2m_regime
except ModuleNotFoundError as exc:
    RESEARCH_2M_SYMBOL = "btcusdt"
    aggregate_2m_bars = build_2m_features = load_2m_1m = merge_2m_external = None
    classify_2m_regime = None
    if ENABLE_LEGACY_TWO_MINUTE_LIVE:
        raise
    print(f"[Signal] Optional legacy 2m modules unavailable: {exc}")


def append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(json_safe(obj), ensure_ascii=False) + "\n")


def json_safe(value):
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "item"):
        try:
            return json_safe(value.item())
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(json_safe(obj), f, ensure_ascii=False)
    os.replace(tmp, path)


def file_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def csv_header(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.readline().strip().split(",")
    except OSError:
        return []


def csv_tail_rows(path, limit=120, chunk_size=65536):
    if not os.path.exists(path):
        return []
    header = csv_header(path)
    if not header:
        return []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - chunk_size))
            text = f.read().decode("utf-8", errors="ignore")
        lines = [line for line in text.splitlines() if line.strip()]
        data_lines = [
            line for line in lines
            if line != ",".join(header) and not line.startswith(header[0] + ",")
        ][-limit:]
        rows = []
        for line in data_lines:
            cells = line.split(",")
            if len(cells) < len(header):
                continue
            rows.append({col: cells[idx] for idx, col in enumerate(header)})
        return rows
    except OSError:
        return []


def csv_tail_time(path, time_col):
    rows = csv_tail_rows(path, limit=5, chunk_size=8192)
    for row in reversed(rows):
        if time_col not in row:
            continue
        ts = pd.to_datetime(row[time_col], utc=True, format="ISO8601", errors="coerce")
        if pd.notna(ts):
            return ts
    return None


def age_status(now, name, path, time_col, max_age):
    ts = csv_tail_time(path, time_col)
    age = None if ts is None else now - ts
    reasons = []
    if not os.path.exists(path):
        reasons.append(f"{name}_missing")
    elif ts is None:
        reasons.append(f"{name}_unparseable")
    elif age > max_age:
        reasons.append(f"{name}_stale")
    return {
        "last_time": None if ts is None else str(ts),
        "age_seconds": None if age is None else round(age.total_seconds(), 3),
        "max_age_seconds": round(max_age.total_seconds(), 3),
        "reasons": reasons,
    }


def file_signature(path):
    try:
        st = os.stat(path)
        return (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return None


def _int_cfg(cfg, key, default):
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        return int(default)


def _estimate_second_tail_sec(config_map):
    required = int(SECOND_BARS_DEFAULT_TAIL_SEC)
    for cfg in (config_map or {}).values():
        if not cfg.get("enabled", True):
            continue
        model_type = str(cfg.get("model_type") or "")
        if model_type == "normal_state_v11":
            lookback = _int_cfg(cfg, "normal_state_lookback_sec", cfg.get("second_lookback_sec", 180 * 60))
            horizon = _int_cfg(cfg, "normal_state_horizon_sec", cfg.get("second_horizon_sec", 600))
            min_gap = _int_cfg(cfg, "normal_state_min_gap_sec", cfg.get("second_min_gap_sec", 600))
            confirm = _int_cfg(cfg, "normal_state_confirm_delay_sec", 5)
            hold = _int_cfg(cfg, "normal_state_signal_hold_sec", 55)
            scan_extra = _int_cfg(
                cfg,
                "normal_state_scan_extra_sec",
                max(3600, min_gap + 1800, confirm + hold + 900),
            )
            required = max(required, lookback + scan_extra + confirm + horizon + 120)
        elif model_type in ("second_normal", "second_normal_vw_confirm", "second_normal_router_v21", "second_normal_lowvol_v22"):
            lookback = _int_cfg(cfg, "second_lookback_sec", 1800)
            if model_type in ("second_normal_router_v21", "second_normal_lowvol_v22"):
                lookback = max(
                    _int_cfg(cfg, "second_router_route_lookback_sec", 4200),
                    _int_cfg(cfg, "second_lookback_sec", 4200),
                )
            horizon = _int_cfg(cfg, "second_horizon_sec", 600)
            hold = _int_cfg(cfg, "second_signal_hold_sec", 60)
            required = max(required, lookback + horizon + hold + 3600)
        elif model_type == "second_chip":
            lookback = _int_cfg(cfg, "second_chip_lookback_sec", cfg.get("second_lookback_sec", 3600))
            horizon = _int_cfg(cfg, "second_chip_horizon_sec", cfg.get("second_horizon_sec", 600))
            hold = _int_cfg(cfg, "second_chip_signal_hold_sec", cfg.get("second_signal_hold_sec", 60))
            required = max(required, lookback + horizon + hold + 600)
        elif model_type in ("second_range_breakout_confirm", "second_value_area_smart", "second_trend_pullback_down", "second_normal_liquidity_orderbook_v1"):
            lookback = max(
                _int_cfg(cfg, "second_lookback_sec", 3600),
                _int_cfg(cfg, "value_area_sec", 3600),
                _int_cfg(cfg, "trend_lookback_sec", 3600),
                _int_cfg(cfg, "second_liq_normal_window_sec", 600),
                _int_cfg(cfg, "second_liq_center_slope_sec", 300),
                _int_cfg(cfg, "second_liq_retest_sec", 120),
            )
            horizon = _int_cfg(cfg, "second_horizon_sec", cfg.get("second_liq_horizon_sec", 600))
            required = max(required, lookback + horizon + 1800)
    return max(3600, int(required))


def update_second_tail_requirement(config_map):
    global _SECOND_BARS_REQUIRED_SEC
    next_required = _estimate_second_tail_sec(config_map)
    if int(_SECOND_BARS_REQUIRED_SEC) != int(next_required):
        _SECOND_BARS_REQUIRED_SEC = int(next_required)
        _SECOND_BARS_CACHE["bars"] = None
        _SECOND_BARS_CACHE["file_size"] = 0
        _SECOND_BARS_CACHE["tail_sec"] = 0
        print(f"[Signal] second bars live tail set to {_SECOND_BARS_REQUIRED_SEC}s")


def begin_second_bars_cycle():
    _SECOND_BARS_CYCLE_CACHE["active"] = True
    _SECOND_BARS_CYCLE_CACHE["bars"] = None


def end_second_bars_cycle():
    _SECOND_BARS_CYCLE_CACHE["active"] = False
    _SECOND_BARS_CYCLE_CACHE["bars"] = None


def remember_second_bars_for_cycle(bars):
    if _SECOND_BARS_CYCLE_CACHE.get("active") and bars is not None:
        _SECOND_BARS_CYCLE_CACHE["bars"] = bars
    return bars


def load_second_bars_cached_for_cycle():
    import time as _time
    cache = _SECOND_BARS_CACHE
    required_tail_sec = int(_SECOND_BARS_REQUIRED_SEC)
    cycle_bars = _SECOND_BARS_CYCLE_CACHE.get("bars") if _SECOND_BARS_CYCLE_CACHE.get("active") else None
    if cycle_bars is not None:
        return cycle_bars

    now = _time.time()
    if now - cache["last_check"] < SECOND_BARS_CACHE_TTL:
        return remember_second_bars_for_cycle(cache["bars"])

    cache["last_check"] = now
    try:
        cur_size = os.path.getsize(SECOND_TRADES_FILE) if os.path.exists(SECOND_TRADES_FILE) else 0
    except OSError:
        cur_size = 0

    if (
        cache["bars"] is not None
        and cur_size == cache["file_size"]
        and cur_size > 0
        and int(cache.get("tail_sec") or 0) == required_tail_sec
    ):
        return remember_second_bars_for_cycle(cache["bars"])

    try:
        sec = load_recent_second_bars(
            SECOND_TRADES_FILE,
            include_shards=True,
            tail_sec=required_tail_sec,
        )
    except Exception as exc:
        print(f"[Signal] second data load failed: {exc}")
        return remember_second_bars_for_cycle(cache["bars"])
    if sec.empty:
        return remember_second_bars_for_cycle(cache["bars"])

    cache["bars"] = sec.reset_index().rename(columns={"index": "time"})
    cache["file_size"] = cur_size
    cache["tail_sec"] = required_tail_sec
    return remember_second_bars_for_cycle(cache["bars"])


def load_minute_features_cached(second_index):
    if not os.path.exists(HISTORY_1M_FILE):
        raise FileNotFoundError("btcusdt_1m.csv not found")
    sig = file_signature(HISTORY_1M_FILE)
    cache = _MINUTE_FEATURE_CACHE
    if cache.get("signature") != sig or cache.get("features") is None:
        df = pd.read_csv(HISTORY_1M_FILE)
        ts = normal_state_v1.parse_time_series(df)
        close_col = "close" if "close" in df.columns else "price"
        vol_col = "volume" if "volume" in df.columns else None
        minute = pd.DataFrame(
            {
                "m_close": pd.to_numeric(df[close_col], errors="coerce").to_numpy(float),
                "m_volume": pd.to_numeric(df[vol_col], errors="coerce").fillna(0.0).to_numpy(float)
                if vol_col else np.zeros(len(df), dtype=float),
            },
            index=ts.to_numpy(),
        ).dropna(subset=["m_close"])
        minute = minute[~minute.index.duplicated(keep="last")].sort_index()
        close = minute["m_close"]
        ret = np.log(close / close.shift(1))
        ma60 = close.rolling(60, min_periods=40).mean()
        sd60 = close.rolling(60, min_periods=40).std(ddof=1)
        z60 = (close - ma60) / sd60.replace(0, np.nan)
        width_bps = 4.0 * sd60 / close * 10000.0
        width_median = width_bps.rolling(360, min_periods=120).median()
        vol_median = minute["m_volume"].rolling(180, min_periods=60).median()
        x = z60.shift(1)
        y = z60
        mean_x = x.rolling(120, min_periods=60).mean()
        mean_y = y.rolling(120, min_periods=60).mean()
        cov_xy = (x * y).rolling(120, min_periods=60).mean() - mean_x * mean_y
        var_x = (x * x).rolling(120, min_periods=60).mean() - mean_x * mean_x
        phi = cov_xy / var_x.replace(0, np.nan)
        half_life = pd.Series(np.nan, index=minute.index, dtype=float)
        mask = (phi > 0.0) & (phi < 0.999)
        half_life.loc[mask] = -math.log(2.0) / np.log(phi.loc[mask])
        features = pd.DataFrame(index=minute.index)
        features["m_z60"] = z60
        features["m_width_bps"] = width_bps
        features["m_width_ratio"] = width_bps / width_median.replace(0, np.nan)
        features["m_slope15_bps"] = (close / close.shift(15) - 1.0) * 10000.0
        features["m_slope60_bps"] = (close / close.shift(60) - 1.0) * 10000.0
        features["m_sigma10_bps"] = ret.rolling(60, min_periods=40).std(ddof=1) * math.sqrt(10) * 10000.0
        features["m_cover2_120"] = (z60.abs() <= 2.0).astype(float).rolling(120, min_periods=80).mean()
        features["m_bandwalk10"] = (z60.abs() >= 1.5).astype(float).rolling(10, min_periods=5).sum()
        features["m_vol_ratio"] = minute["m_volume"] / vol_median.replace(0, np.nan)
        features["m_half_life_min"] = half_life
        features["minute_source"] = HISTORY_1M_FILE
        cache["signature"] = sig
        cache["features"] = features
    return cache["features"].reindex(second_index, method="ffill")


def empty_orderbook_features(second_index, source=""):
    out = pd.DataFrame(index=second_index)
    out["ob_available"] = False
    out["ob_imb20"] = np.nan
    out["ob_micro_bps"] = np.nan
    out["ob_spread_bps"] = np.nan
    out["orderbook_source"] = source
    return out


def _numeric_array(df, col):
    if col not in df.columns:
        return np.full(len(df), np.nan, dtype=float)
    return pd.to_numeric(df[col], errors="coerce").to_numpy(float)


def load_orderbook_features_cached(second_index):
    if len(second_index) == 0:
        return empty_orderbook_features(second_index)
    sig = file_signature(ORDERBOOK_FILE)
    key = (
        sig,
        len(second_index),
        int(second_index[0].value),
        int(second_index[-1].value),
    )
    cache = _ORDERBOOK_FEATURE_CACHE
    if cache.get("key") == key and cache.get("features") is not None:
        return cache["features"].copy()
    if sig is None:
        features = empty_orderbook_features(second_index)
        cache["key"] = key
        cache["features"] = features
        return features.copy()
    try:
        limit = max(ORDERBOOK_FEATURE_TAIL_ROWS, len(second_index) + 60)
        rows = csv_tail_rows(ORDERBOOK_FILE, limit=limit, chunk_size=ORDERBOOK_FEATURE_TAIL_CHUNK)
        if not rows:
            features = empty_orderbook_features(second_index, ORDERBOOK_FILE)
        else:
            df = pd.DataFrame(rows)
            ts = normal_state_v1.parse_time_series(df).dt.floor("s")
            valid_ts = ts.notna()
            df = df.loc[valid_ts].reset_index(drop=True)
            ts = ts.loc[valid_ts].reset_index(drop=True)
            if df.empty:
                features = empty_orderbook_features(second_index, ORDERBOOK_FILE)
            else:
                ob = pd.DataFrame(
                    {
                        "ob_imb20": _numeric_array(df, "imbalance_20"),
                        "ob_micro_bps": _numeric_array(df, "microprice_edge_bps"),
                        "ob_spread_bps": _numeric_array(df, "spread_bps"),
                        "ob_available": np.ones(len(df), dtype=bool),
                    },
                    index=ts.to_numpy(),
                ).dropna(how="all")
                ob = ob[~ob.index.duplicated(keep="last")].sort_index()
                aligned = ob.reindex(second_index, method="ffill", limit=5)
                features = empty_orderbook_features(second_index, ORDERBOOK_FILE)
                for col in ("ob_imb20", "ob_micro_bps", "ob_spread_bps"):
                    features[col] = aligned[col]
                features["ob_available"] = aligned["ob_available"].fillna(False).astype(bool)
    except Exception as exc:
        print(f"[Signal] V11 orderbook features unavailable: {exc}")
        features = empty_orderbook_features(second_index, ORDERBOOK_FILE)
    cache["key"] = key
    cache["features"] = features
    return features.copy()


def build_live_data_health(live_1m):
    now = pd.Timestamp.now(tz="UTC")
    checks = {
        "history_1m": age_status(now, "history_1m", HISTORY_1M_FILE, "open_time", HISTORY_1M_MAX_AGE),
        "taker": age_status(now, "taker", TAKER_FILE, "timestamp", EXTERNAL_RATIO_MAX_AGE),
        "lsratio": age_status(now, "lsratio", LS_RATIO_FILE, "timestamp", EXTERNAL_RATIO_MAX_AGE),
        "funding": age_status(now, "funding", FUNDING_FILE, "fundingTime", FUNDING_MAX_AGE),
    }
    reasons = []
    for item in checks.values():
        reasons.extend(item["reasons"])

    live_info = {"first_time": None, "last_time": None, "age_seconds": None}
    if live_1m is None or len(live_1m) == 0:
        reasons.append("live_1m_missing")
    else:
        live_times = pd.to_datetime(live_1m["open_time"], utc=True).sort_values().reset_index(drop=True)
        live_first = live_times.iloc[0]
        live_last = live_times.iloc[-1]
        live_age = now - live_last
        live_info = {
            "first_time": str(live_first),
            "last_time": str(live_last),
            "age_seconds": round(live_age.total_seconds(), 3),
        }
        if live_age > LIVE_1M_MAX_AGE:
            reasons.append("live_1m_stale")
        gaps = live_times.diff().dropna()
        if len(gaps) and gaps.max() > MAX_HISTORY_LIVE_GAP:
            reasons.append("live_1m_recent_gap")
            live_info["max_gap_seconds"] = round(gaps.max().total_seconds(), 3)
        hist_last = csv_tail_time(HISTORY_1M_FILE, "open_time")
        if hist_last is not None and hist_last + MAX_HISTORY_LIVE_GAP < live_first:
            reasons.append("history_live_gap")
            live_info["history_live_gap_seconds"] = round((live_first - hist_last).total_seconds(), 3)

    unique_reasons = sorted(set(reasons))
    return {
        "blocked": bool(unique_reasons),
        "reasons": unique_reasons,
        "checks": checks,
        "live_1m": live_info,
    }


def signal_data_health_reasons(sig, health):
    reasons = list(health.get("reasons", []))
    if not reasons:
        return []
    model_type = str(sig.get("model_type") or "").lower()
    second_only = model_type.startswith("second_")
    taker_filter = str(sig.get("taker_filter") or "none").lower()
    needs_taker = taker_filter not in ("", "none", "off", "false")
    blocking = []
    for reason in reasons:
        if reason.startswith("taker_"):
            if needs_taker:
                blocking.append(reason)
            continue
        if reason.startswith("lsratio_") or reason.startswith("funding_"):
            continue
        if second_only and reason in ("history_1m_stale", "live_1m_stale", "history_live_gap", "live_1m_missing"):
            continue
        blocking.append(reason)
    return sorted(set(blocking))


def apply_signal_data_health(signals, health):
    if not health.get("blocked"):
        for sig in signals.values():
            if isinstance(sig, dict) and not sig.get("shadow"):
                sig["data_health_blocked"] = False
        return signals
    out = {}
    for strategy_id, sig in signals.items():
        if not isinstance(sig, dict) or sig.get("shadow"):
            out[strategy_id] = sig
            continue
        blocking_reasons = signal_data_health_reasons(sig, health)
        if not blocking_reasons:
            next_sig = dict(sig)
            next_sig["data_health_blocked"] = False
            next_sig["data_health_warning_reasons"] = health.get("reasons", [])
            out[strategy_id] = next_sig
            continue
        blocked = dict(sig)
        blocked["signal"] = None
        blocked["confidence"] = None
        blocked["data_health_blocked"] = True
        blocked["data_health_block_reasons"] = blocking_reasons
        blocked["data_health"] = health
        blocked["blocked_signal"] = sig.get("signal")
        blocked["blocked_confidence"] = sig.get("confidence")
        out[strategy_id] = blocked
    return out


def load_audit_keys(path, limit=20000):
    if not os.path.exists(path):
        return set()
    keys = set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        for line in lines:
            try:
                row = json.loads(line)
            except Exception:
                continue
            event = row.get("event")
            if (
                event in ("signal_snapshot", "shadow_candidate")
                and row.get("strategy_id")
                and row.get("time")
                and row.get("actionable_time")
            ):
                keys.add(f"{event}|{row['strategy_id']}|{row['time']}")
    except Exception:
        return set()
    return keys


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
        raw = json.load(f)
    return raw.get("strategies", raw)


def model_label_for(strategy_id, cfg):
    return cfg.get("model_label") or f"BTC_{int(cfg.get('interval_min', cfg['horizon'] * 5))}min"


def trend_score(row):
    score = 0
    eps = 0.00005
    for col in ["trend6", "trend12", "trend30", "pre50"]:
        v = float(row.get(col, 0) or 0)
        if v > eps:
            score += 1
        elif v < -eps:
            score -= 1
    stack = float(row.get("ema_stack", 0) or 0)
    if stack > 0:
        score += 1
    elif stack < 0:
        score -= 1
    return int(score)


def trend_label(score):
    if score >= 3:
        return "strong_uptrend"
    if score <= -3:
        return "strong_downtrend"
    if score > 0:
        return "mild_uptrend"
    if score < 0:
        return "mild_downtrend"
    return "neutral"


def htf_score(row):
    score = 0
    thresholds = {
        "htf_ret_1h": 0.0010,
        "htf_ret_4h": 0.0025,
        "htf_ret_24h": 0.0060,
    }
    for col, eps in thresholds.items():
        v = float(row.get(col, 0) or 0)
        if v > eps:
            score += 1
        elif v < -eps:
            score -= 1
    for col in ["htf_pos_4h", "htf_pos_24h"]:
        v = float(row.get(col, 0.5) or 0.5)
        if v >= 0.65:
            score += 1
        elif v <= 0.35:
            score -= 1
    return int(score)


def htf_label(score):
    if score >= 3:
        return "strong_up"
    if score <= -3:
        return "strong_down"
    if score > 0:
        return "mild_up"
    if score < 0:
        return "mild_down"
    return "range"


def direction_sign(signal):
    return 1 if signal == "UP" else -1


def directional_alignment(signal, score):
    return int(score or 0) * direction_sign(signal)


def market_confirmation(signal, trend_val, htf_val, taker_ratio, atr_exp):
    """Score whether current market structure supports the proposed direction."""
    short_align = directional_alignment(signal, trend_val)
    htf_align = directional_alignment(signal, htf_val)
    score = 0
    reasons = []

    if short_align >= 3:
        score += 2
        reasons.append("short_trend_strong_align")
    elif short_align > 0:
        score += 1
        reasons.append("short_trend_align")
    elif short_align <= -3:
        score -= 2
        reasons.append("short_trend_strong_counter")
    elif short_align < 0:
        score -= 1
        reasons.append("short_trend_counter")

    if htf_align >= 3:
        score += 2
        reasons.append("htf_strong_align")
    elif htf_align > 0:
        score += 1
        reasons.append("htf_align")
    elif htf_align <= -3:
        score -= 2
        reasons.append("htf_strong_counter")
    elif htf_align < 0:
        score -= 1
        reasons.append("htf_counter")

    taker_align = 0
    if taker_ratio >= 1.05:
        taker_align = 1
    elif taker_ratio <= 0.95:
        taker_align = -1
    if taker_align:
        if taker_align == direction_sign(signal):
            score += 1
            reasons.append("taker_align")
        else:
            score -= 1
            reasons.append("taker_counter")

    if 0.65 <= float(atr_exp or 0) <= 2.25:
        score += 1
        reasons.append("volatility_normal")
    elif float(atr_exp or 0) > 2.8:
        score -= 1
        reasons.append("volatility_hot")

    return {
        "score": int(score),
        "reasons": reasons,
        "short_align": int(short_align),
        "htf_align": int(htf_align),
        "taker_align": int(taker_align),
    }



class POCNormalStrategy:
    """Normal-tail reversal strategy with optional 2m aggregation and taker flow gate."""
    def __init__(self, strategy_id, cfg):
        self.id = strategy_id
        self.window = int(cfg.get("norm_window", 60))
        self.tail_pct = float(cfg.get("norm_tail_pct", 0.15))
        self.poc_threshold = 1.0 - self.tail_pct
        self.use_rsi = cfg.get("norm_use_rsi", True)
        self.rsi_lo = float(cfg.get("rsi_lo", 30))
        self.rsi_hi = float(cfg.get("rsi_hi", 70))
        self.horizon = int(cfg.get("horizon", 10))
        self.interval_min = int(cfg.get("interval_min", 10))
        self.source_minutes = max(1, int(cfg.get("norm_source_minutes", cfg.get("norm_bar_min", 1))))
        self.min_gap_minutes = int(cfg.get("norm_min_gap_minutes", self.interval_min))
        self.mode = cfg.get("norm_mode", "reversal")
        self.taker_filter = str(cfg.get("norm_taker_filter", "none")).lower()
        self.taker_align_up = float(cfg.get("norm_taker_align_up", 1.05))
        self.taker_align_down = float(cfg.get("norm_taker_align_down", 0.95))
        self.taker_counter_up = float(cfg.get("norm_taker_counter_up", 0.85))
        self.taker_counter_down = float(cfg.get("norm_taker_counter_down", 1.15))
        self.taker_max_age_minutes = int(cfg.get("norm_taker_max_age_minutes", 30))
        self.skip_hours_utc = sorted({int(h) for h in cfg.get("skip_hours_utc", [])})
    def _load_price_bars(self):
        import pandas as pd

        if not os.path.exists(HISTORY_1M_FILE):
            return None
        df1m = pd.read_csv(HISTORY_1M_FILE)
        if "open_time" not in df1m.columns or "close" not in df1m.columns:
            return None
        df1m["open_time"] = pd.to_datetime(df1m["open_time"], utc=True, errors="coerce")
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df1m.columns:
                df1m[col] = pd.to_numeric(df1m[col], errors="coerce")
        df1m = df1m.dropna(subset=["open_time", "close"]).drop_duplicates("open_time").sort_values("open_time")
        if self.source_minutes <= 1:
            out = df1m[["open_time", "close"]].rename(columns={"open_time": "time"}).reset_index(drop=True)
        else:
            df1m["period"] = df1m["open_time"].dt.floor(f"{self.source_minutes}min")
            agg = {"close": ("close", "last")}
            if "open" in df1m.columns:
                agg["open"] = ("open", "first")
            if "high" in df1m.columns:
                agg["high"] = ("high", "max")
            if "low" in df1m.columns:
                agg["low"] = ("low", "min")
            if "volume" in df1m.columns:
                agg["volume"] = ("volume", "sum")
            out = df1m.groupby("period").agg(**agg).reset_index().rename(columns={"period": "time"})
            latest_1m_open = df1m["open_time"].max()
            out["close_time"] = out["time"] + pd.Timedelta(minutes=self.source_minutes)
            out = out[out["close_time"] <= latest_1m_open].drop(columns=["close_time"]).reset_index(drop=True)
        return out.dropna(subset=["time", "close"]).reset_index(drop=True)

    def _latest_taker_ratio(self, signal_time):
        import pandas as pd

        if self.taker_filter in ("", "none", "off", "false"):
            return None, True, "disabled"
        if not os.path.exists(TAKER_FILE):
            return None, False, "taker_missing"
        try:
            taker = pd.read_csv(TAKER_FILE)
            if "timestamp" not in taker.columns or "buySellRatio" not in taker.columns:
                return None, False, "taker_columns_missing"
            taker["timestamp"] = pd.to_datetime(taker["timestamp"], utc=True, errors="coerce")
            taker["buySellRatio"] = pd.to_numeric(taker["buySellRatio"], errors="coerce")
            taker = taker.dropna(subset=["timestamp", "buySellRatio"]).sort_values("timestamp")
            if taker.empty:
                return None, False, "taker_empty"
            signal_ts = pd.to_datetime(signal_time, utc=True)
            rows = taker[taker["timestamp"] <= signal_ts]
            if rows.empty:
                return None, False, "taker_no_prior_row"
            row = rows.iloc[-1]
            age_min = (signal_ts - row["timestamp"]).total_seconds() / 60
            if age_min > self.taker_max_age_minutes:
                return float(row["buySellRatio"]), False, "taker_stale"
            return float(row["buySellRatio"]), True, "ok"
        except Exception:
            return None, False, "taker_read_error"

    def _taker_flow_bias(self, ratio):
        if ratio is None or not np.isfinite(ratio):
            return "unknown"
        if ratio >= self.taker_align_up:
            return "bullish"
        if ratio <= self.taker_align_down:
            return "bearish"
        return "neutral"

    def _taker_allows(self, signal, ratio):
        if self.taker_filter in ("", "none", "off", "false"):
            return True, "disabled"
        if ratio is None or not np.isfinite(ratio):
            return False, "taker_missing_ratio"
        if self.taker_filter == "align":
            if signal == "UP":
                return ratio >= self.taker_align_up, "taker_align_up" if ratio >= self.taker_align_up else "taker_not_aligned"
            if signal == "DOWN":
                return ratio <= self.taker_align_down, "taker_align_down" if ratio <= self.taker_align_down else "taker_not_aligned"
        if self.taker_filter == "not_counter":
            if signal == "UP":
                return ratio >= self.taker_counter_up, "taker_not_counter" if ratio >= self.taker_counter_up else "taker_counter"
            if signal == "DOWN":
                return ratio <= self.taker_counter_down, "taker_not_counter" if ratio <= self.taker_counter_down else "taker_counter"
        return False, f"unknown_taker_filter_{self.taker_filter}"

    def predict(self, df5=None):
        import numpy as np
        from scipy.stats import norm as scipy_norm
        import datetime

        try:
            bars = self._load_price_bars()
            if bars is None:
                return None
            close = np.asarray(bars["close"].astype(float).values, dtype=float)
        except Exception:
            return None

        window_bars = max(2, int(round(self.window / self.source_minutes)))
        horizon_bars = max(1, int(round(self.horizon / self.source_minutes)))
        if len(close) < window_bars + 1:
            return None

        now_hour = datetime.datetime.utcnow().hour
        if now_hour in self.skip_hours_utc:
            return {"strategy_id": self.id, "signal": None, "confidence": 0,
                    "avg_prob": 0.5, "rsi_value": None, "high_conf": False,
                    "agree": True, "vol_ok": True, "session_gate_ok": True,
                    "rsi_extreme": True, "z_score": 0, "p_up": 0.5,
                    "reason": "skip_hour", "model_type": "poc_normal",
                    "time": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}

        recent = close[-(window_bars + 1):]
        lr = np.log(recent[1:] / recent[:-1])
        lr = lr[np.isfinite(lr)]
        if len(lr) < 20:
            return None

        mu = np.mean(lr)
        sigma = np.std(lr, ddof=1)
        if sigma < 1e-10:
            return None

        H = horizon_bars
        z = (H * mu) / (np.sqrt(H) * sigma)
        p_up = scipy_norm.cdf(z)
        conf = abs(p_up - 0.5) * 200

        signal = None
        if self.mode == "reversal":
            if p_up >= self.poc_threshold:
                signal = "DOWN"
            elif p_up <= self.tail_pct:
                signal = "UP"
        else:
            if p_up >= self.poc_threshold:
                signal = "UP"
            elif p_up <= self.tail_pct:
                signal = "DOWN"

        # RSI filter
        rsi_value = None
        rsi_ok = True
        if self.use_rsi and len(close) >= 30:
            try:
                rsi_arr = self._compute_rsi(close[-30:], 14)
                rsi_value = float(rsi_arr[-1])
            except Exception:
                pass

        signal_time = bars["time"].iloc[-1]
        taker_ratio, taker_data_ok, taker_reason = self._latest_taker_ratio(signal_time)
        taker_flow_bias = self._taker_flow_bias(taker_ratio)

        if not signal:
            return {"strategy_id": self.id, "signal": None,
                    "confidence": round(min(conf, 95), 1),
                    "avg_prob": round(float(p_up), 4),
                    "rsi_value": round(rsi_value, 1) if rsi_value else None,
                    "high_conf": False, "agree": True, "vol_ok": True,
                    "session_gate_ok": True, "rsi_extreme": True,
                    "z_score": round(float(z), 4), "p_up": round(float(p_up), 4),
                    "mode": self.mode,
                    "source_minutes": self.source_minutes,
                    "window_minutes": self.window,
                    "window_bars": window_bars,
                    "horizon_minutes": self.horizon,
                    "horizon_bars": horizon_bars,
                    "min_gap_minutes": self.min_gap_minutes,
                    "tail_pct": self.tail_pct,
                    "taker_filter": self.taker_filter,
                    "taker_ratio": None if taker_ratio is None else round(float(taker_ratio), 6),
                    "taker_data_ok": bool(taker_data_ok),
                    "taker_reason": taker_reason,
                    "taker_flow_bias": taker_flow_bias,
                    "reason": "no_edge", "model_type": "poc_normal",
                    "time": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}

        # RSI filter
        rsi_value = None
        rsi_ok = True
        if self.use_rsi and len(close) >= 30:
            try:
                rsi_arr = self._compute_rsi(close[-30:], 14)
                rsi_value = float(rsi_arr[-1])
                if self.mode == "reversal":
                    if signal == "UP" and rsi_value > self.rsi_lo:
                        rsi_ok = False
                    if signal == "DOWN" and rsi_value < self.rsi_hi:
                        rsi_ok = False
                else:
                    if signal == "UP" and rsi_value < self.rsi_lo:
                        rsi_ok = False
                    if signal == "DOWN" and rsi_value > self.rsi_hi:
                        rsi_ok = False
            except Exception:
                pass

        if not rsi_ok:
            return {"strategy_id": self.id, "signal": None, "confidence": 0,
                    "avg_prob": round(float(p_up), 4), "rsi_value": rsi_value,
                    "high_conf": False, "agree": True, "vol_ok": True,
                    "session_gate_ok": True, "rsi_extreme": False,
                    "z_score": round(float(z), 4), "p_up": round(float(p_up), 4),
                    "reason": "rsi_filter", "model_type": "poc_normal",
                    "time": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}

        taker_ok, taker_filter_reason = self._taker_allows(signal, taker_ratio)
        if not taker_data_ok or not taker_ok:
            return {"strategy_id": self.id, "signal": None, "confidence": 0,
                    "avg_prob": round(float(p_up), 4), "rsi_value": rsi_value,
                    "high_conf": False, "agree": True, "vol_ok": True,
                    "session_gate_ok": True, "rsi_extreme": True,
                    "z_score": round(float(z), 4), "p_up": round(float(p_up), 4),
                    "reason": taker_reason if not taker_data_ok else taker_filter_reason,
                    "blocked_signal": signal,
                    "blocked_confidence": round(min(conf, 95), 1),
                    "model_type": "poc_normal",
                    "mode": self.mode,
                    "source_minutes": self.source_minutes,
                    "window_minutes": self.window,
                    "window_bars": window_bars,
                    "horizon_minutes": self.horizon,
                    "horizon_bars": horizon_bars,
                    "min_gap_minutes": self.min_gap_minutes,
                    "taker_filter": self.taker_filter,
                    "taker_ratio": None if taker_ratio is None else round(float(taker_ratio), 6),
                    "taker_data_ok": bool(taker_data_ok),
                    "taker_reason": taker_reason,
                    "taker_flow_bias": taker_flow_bias,
                    "taker_filter_ok": False,
                    "time": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}

        return {
            "strategy_id": self.id,
            "signal": signal,
            "confidence": round(min(conf, 95), 1),
            "avg_prob": round(float(p_up), 4),
            "rsi_value": round(rsi_value, 1) if rsi_value else None,
            "high_conf": conf >= 30,
            "agree": True,
            "vol_ok": True,
            "session_gate_ok": True,
            "rsi_extreme": True,
            "z_score": round(float(z), 4),
            "p_up": round(float(p_up), 4),
            "mu_bar": round(float(mu), 8),
            "sigma_bar": round(float(sigma), 8),
            "mode": self.mode,
            "source_minutes": self.source_minutes,
            "window_minutes": self.window,
            "window_bars": window_bars,
            "horizon_minutes": self.horizon,
            "horizon_bars": horizon_bars,
            "min_gap_minutes": self.min_gap_minutes,
            "tail_pct": self.tail_pct,
            "taker_filter": self.taker_filter,
            "taker_ratio": None if taker_ratio is None else round(float(taker_ratio), 6),
            "taker_data_ok": bool(taker_data_ok),
            "taker_reason": taker_reason,
            "taker_flow_bias": taker_flow_bias,
            "taker_filter_ok": True,
            "bypass_entry_timing": True,
            "time": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model_type": "poc_normal",
        }

    def _compute_rsi(self, prices, period=14):
        import numpy as np
        if len(prices) < period + 1:
            return np.array([50.0] * len(prices))
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])
        rsi = np.zeros(len(prices))
        rsi[:period] = 50.0
        for i in range(period, len(prices)):
            avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
            if avg_loss < 1e-10:
                rsi[i] = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi[i] = 100.0 - 100.0 / (1.0 + rs)
        return rsi


class SecondNormalStrategy:
    """Second-level normal-tail reversal for 10m binary options."""
    def __init__(self, strategy_id, cfg):
        self.id = strategy_id
        self.lookback_sec = int(cfg.get("second_lookback_sec", 1800))
        self.horizon_sec = int(cfg.get("second_horizon_sec", 600))
        self.min_gap_sec = int(cfg.get("second_min_gap_sec", self.horizon_sec))
        self.tail_pct = float(cfg.get("second_tail_pct", 0.2))
        self.poc_threshold = 1.0 - self.tail_pct
        self.filter = str(cfg.get("second_filter", "none")).lower()
        self.zone_filter = str(cfg.get("second_zone_filter", "none")).lower()
        self.sigma_min_bps = float(cfg.get("second_sigma_min_bps", 0.0))
        self.sigma_max_bps = float(cfg.get("second_sigma_max_bps", 9999.0))
        self.interval_min = max(1, int(round(self.horizon_sec / 60)))

    def _load_seconds(self):
        return load_second_bars_cached_for_cycle()
        """加载秒级 bars，使用模块级缓存，最多 SECOND_BARS_CACHE_TTL 秒刷新一次。

        策略：
          1. 若距上次检查不足 TTL，直接返回缓存。
          2. 检查主文件大小；若与缓存时相同，也跳过重读。
          3. 若文件变大，只读取新增行（tail read），追加到缓存并丢弃过老的行。
          4. 首次或缓存为空时全量读取一次。
        """
        import time as _time
        cache = _SECOND_BARS_CACHE
        now = _time.time()

        # 未到刷新间隔，直接返回缓存
        if now - cache["last_check"] < SECOND_BARS_CACHE_TTL:
            return cache["bars"]

        cache["last_check"] = now
        try:
            cur_size = os.path.getsize(SECOND_TRADES_FILE) if os.path.exists(SECOND_TRADES_FILE) else 0
        except OSError:
            cur_size = 0

        # 文件未变化且缓存有效，跳过 IO
        if cache["bars"] is not None and cur_size == cache["file_size"] and cur_size > 0:
            return cache["bars"]

        # 全量或增量读取
        try:
            sec = load_second_bars(SECOND_TRADES_FILE, include_shards=True)
        except Exception as exc:
            print(f"[Signal] second data load failed: {exc}")
            return cache["bars"]   # 读失败时沿用旧缓存
        if sec.empty:
            return cache["bars"]

        cache["bars"] = sec.reset_index().rename(columns={"index": "time"})
        cache["file_size"] = cur_size
        return cache["bars"]

    def _filter_allows(self, signal, bars):
        import numpy as np

        if self.filter in ("", "none", "off", "false"):
            return True, "disabled", None, None
        latest_vol = float(bars["volume"].tail(60).sum())
        volume_series = bars["volume"].rolling(60).sum().dropna()
        vol_rank = None
        if len(volume_series) >= 30:
            vol_rank = float((volume_series <= latest_vol).mean())
        buy = float(bars["buy_qty"].tail(60).sum())
        sell = float(bars["sell_qty"].tail(60).sum())
        flow_ratio = buy / max(sell, 1e-12)
        if self.filter == "vol_high":
            ok = vol_rank is not None and vol_rank >= 0.6
            return ok, "vol_high" if ok else "vol_not_high", vol_rank, flow_ratio
        if self.filter == "vol_not_high":
            ok = vol_rank is None or vol_rank <= 0.8
            return ok, "vol_not_high" if ok else "vol_too_high", vol_rank, flow_ratio
        if self.filter in ("flow_align", "flow_strong_align", "flow_align_vol_not_high"):
            up_min = 1.2 if self.filter == "flow_strong_align" else 1.05
            down_max = 0.8 if self.filter == "flow_strong_align" else 0.95
            flow_ok = flow_ratio >= up_min if signal == "UP" else flow_ratio <= down_max
            vol_ok = True
            if self.filter == "flow_align_vol_not_high":
                vol_ok = vol_rank is None or vol_rank <= 0.8
            ok = bool(flow_ok and vol_ok)
            return ok, "flow_align" if ok else "flow_not_aligned", vol_rank, flow_ratio
        if self.filter == "flow_reversal":
            ok = flow_ratio <= 0.95 if signal == "UP" else flow_ratio >= 1.05
            return bool(ok), "flow_reversal" if ok else "flow_not_reversal", vol_rank, flow_ratio
        return False, f"unknown_second_filter_{self.filter}", vol_rank, flow_ratio

    def _zone_filter_allows(self, signal, bars):
        if not is_dynamic_zone_filter_enabled(self.zone_filter):
            return True, "zone_filter_disabled", {}
        context = dynamic_zone_context_from_bars(bars)
        ok, reason = dynamic_zone_allows(self.zone_filter, signal, context)
        extra = {
            "zone_filter": self.zone_filter,
            "zone_filter_reason": reason,
            "zone_signal_hint": dynamic_zone_signal_hint(self.zone_filter, signal, context),
            **compact_zone_context(context),
        }
        return ok, reason, extra

    def predict(self, df5=None):
        import numpy as np
        from scipy.stats import norm as scipy_norm

        bars = self._load_seconds()
        if bars is None or len(bars) < self.lookback_sec + 2:
            return None
        recent = bars.tail(self.lookback_sec + 1).copy()
        close = np.asarray(recent["close"].astype(float).values, dtype=float)
        lr = np.log(close[1:] / close[:-1])
        lr = lr[np.isfinite(lr)]
        if len(lr) < 60:
            return None
        mu = float(np.mean(lr))
        sigma = float(np.std(lr, ddof=1))
        if sigma < 1e-12:
            return None
        sigma_10m_bps = math.sqrt(self.horizon_sec) * sigma * 10000.0
        z = (self.horizon_sec * mu) / (math.sqrt(self.horizon_sec) * sigma)
        p_up = float(scipy_norm.cdf(z))
        conf = abs(p_up - 0.5) * 200
        signal = None
        if p_up >= self.poc_threshold:
            signal = "DOWN"
        elif p_up <= self.tail_pct:
            signal = "UP"
        signal_time = bars["time"].iloc[-1]
        base = {
            "strategy_id": self.id,
            "confidence": round(min(conf, 95), 1),
            "avg_prob": round(p_up, 4),
            "rsi_value": None,
            "high_conf": bool(signal),
            "agree": True,
            "vol_ok": True,
            "session_gate_ok": True,
            "rsi_extreme": True,
            "z_score": round(float(z), 4),
            "p_up": round(p_up, 4),
            "mu_sec": round(mu, 10),
            "sigma_sec": round(sigma, 10),
            "sigma_10m_bps": round(float(sigma_10m_bps), 4),
            "second_sigma_min_bps": self.sigma_min_bps,
            "second_sigma_max_bps": self.sigma_max_bps,
            "lookback_sec": self.lookback_sec,
            "horizon_sec": self.horizon_sec,
            "interval_min": self.interval_min,
            "duration": self.interval_min,
            "min_gap_sec": self.min_gap_sec,
            "tail_pct": self.tail_pct,
            "second_filter": self.filter,
            "second_zone_filter": self.zone_filter,
            "time": signal_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model_type": "second_normal",
            "next_signal_estimate": "V1每5秒扫描；价格假突破1.2σ后回到正态区间，且订单薄有被动支撑/压力时触发10分钟实盘信号。",
            "bypass_entry_timing": True,
        }
        zone_context = dynamic_zone_context_from_bars(bars)
        base.update({
            "zone_filter": self.zone_filter,
            "zone_signal_hint": dynamic_zone_signal_hint(self.zone_filter, signal, zone_context),
            **compact_zone_context(zone_context),
        })
        if not (self.sigma_min_bps <= sigma_10m_bps <= self.sigma_max_bps):
            return {**base, "signal": None, "reason": "sigma_out_of_range"}
        if not signal:
            return {**base, "signal": None, "reason": "no_edge"}
        ok, reason, vol_rank, flow_ratio = self._filter_allows(signal, bars.tail(max(self.lookback_sec, 1800)))
        if vol_rank is not None:
            base["second_vol_rank_60s"] = round(float(vol_rank), 4)
        if flow_ratio is not None and np.isfinite(flow_ratio):
            base["second_flow_ratio_60s"] = round(float(flow_ratio), 6)
        if not ok:
            return {**base, "signal": None, "reason": reason, "blocked_signal": signal, "blocked_confidence": round(min(conf, 95), 1)}
        zone_ok, zone_reason, zone_extra = self._zone_filter_allows(signal, bars)
        base.update(zone_extra)
        if not zone_ok:
            return {**base, "signal": None, "reason": zone_reason, "blocked_signal": signal, "blocked_confidence": round(min(conf, 95), 1)}
        return {**base, "signal": signal, "reason": "second_tail_reversal"}


class SecondNormalRouterV21Strategy(SecondNormalStrategy):
    """Live V21 second-normal router used by the local loss-density research."""

    BRANCHES = [
        {
            "role": "low",
            "name": "LOW_L4200_T25_S4_18_DYN",
            "lookback_sec": 4200,
            "tail_pct": 0.25,
            "sigma_min_bps": 4.0,
            "sigma_max_bps": 18.0,
            "second_filter": "none",
            "zone_filter": "dynamic_v3",
        },
        {
            "role": "mid",
            "name": "MID_L4200_T25_S10_25_DYN",
            "lookback_sec": 4200,
            "tail_pct": 0.25,
            "sigma_min_bps": 10.0,
            "sigma_max_bps": 25.0,
            "second_filter": "none",
            "zone_filter": "dynamic_v3",
        },
        {
            "role": "high",
            "name": "HIGH_L2700_T25_S14_35_FLOW_DYN",
            "lookback_sec": 2700,
            "tail_pct": 0.25,
            "sigma_min_bps": 14.0,
            "sigma_max_bps": 35.0,
            "second_filter": "flow_reversal",
            "zone_filter": "dynamic_v3",
        },
    ]

    def __init__(self, strategy_id, cfg):
        super().__init__(strategy_id, cfg)
        self.horizon_sec = int(cfg.get("second_router_horizon_sec", cfg.get("second_horizon_sec", 600)))
        self.min_gap_sec = int(cfg.get("second_router_min_gap_sec", cfg.get("second_min_gap_sec", 600)))
        self.route_lookback_sec = int(cfg.get("second_router_route_lookback_sec", 4200))
        self.r10_window_sec = int(cfg.get("second_router_r10_window_sec", 600))
        self.r10_cap_bps = float(cfg.get("second_router_r10_cap_bps", 42.0))
        self.down_r10_cap_bps = float(cfg.get("second_router_down_r10_cap_bps", 35.0))
        self.mid_route_sigma_cap_bps = float(cfg.get("second_router_mid_route_sigma_cap_bps", 20.0))
        self.min_observed_pct = float(cfg.get("second_router_min_observed_pct", 88.0))
        self.veto_low_up = self._cfg_bool(cfg.get("second_router_veto_low_up", True), True)
        self.interval_min = max(1, int(round(self.horizon_sec / 60)))
        self.model_label = str(cfg.get("model_label", "BTC_10min_NORMAL_STATE_V21_LOSS_DENSITY_3OF6_8H"))
        self.loss_density_window = int(cfg.get("normal_state_loss_density_window", 6) or 6)
        self.loss_density_losses = int(cfg.get("normal_state_loss_density_losses", 3) or 3)
        self.loss_density_cooldown_sec = int(cfg.get("normal_state_loss_density_cooldown_sec", 28800) or 28800)

    @staticmethod
    def _normal_cdf(x):
        return 0.5 * (1.0 + math.erf(float(x) / math.sqrt(2.0)))

    @staticmethod
    def _cfg_bool(value, default=False):
        if value is None:
            return bool(default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() not in ("0", "false", "no", "off", "disabled")

    @staticmethod
    def _role_order(route_sigma):
        if route_sigma < 9.0:
            return ["low", "mid", "high"]
        if route_sigma >= 16.0:
            return ["high", "mid", "low"]
        if route_sigma < 22.0:
            return ["mid", "high", "low"]
        return ["high", "mid", "low"]

    @staticmethod
    def _observed_pct(bars, seconds):
        if "observed" not in bars:
            return 100.0
        recent = bars["observed"].tail(max(1, int(seconds))).astype(float)
        if len(recent) == 0:
            return 0.0
        return float(recent.mean() * 100.0)

    @staticmethod
    def _range_bps(close):
        if len(close) == 0:
            return float("nan")
        price = float(close[-1])
        if price <= 0:
            return float("nan")
        return (float(np.nanmax(close)) - float(np.nanmin(close))) / price * 10000.0

    def _route_sigma_bps(self, bars):
        close = np.asarray(bars["close"].astype(float).tail(self.route_lookback_sec + 1).values, dtype=float)
        if len(close) < 120:
            return float("nan")
        lr = np.diff(np.log(close))
        lr = lr[np.isfinite(lr)]
        if len(lr) < 60:
            return float("nan")
        sigma = float(np.std(lr, ddof=1))
        return math.sqrt(self.horizon_sec) * sigma * 10000.0

    def _branch_signal(self, bars, branch):
        probe = self._branch_diagnostic(bars, branch)
        if not probe.get("has_tail_signal"):
            return None
        return probe

    def _branch_diagnostic(self, bars, branch):
        lookback = int(branch["lookback_sec"])
        tail_pct = float(branch["tail_pct"])
        info = {
            "role": branch["role"],
            "branch": branch["name"],
            "lookback_sec": lookback,
            "tail_pct": tail_pct,
            "up_trigger_pct": round(tail_pct * 100.0, 2),
            "down_trigger_pct": round((1.0 - tail_pct) * 100.0, 2),
            "sigma_min_bps": float(branch["sigma_min_bps"]),
            "sigma_max_bps": float(branch["sigma_max_bps"]),
            "second_filter": str(branch["second_filter"]),
            "zone_filter": str(branch["zone_filter"]),
            "signal": None,
            "has_tail_signal": False,
        }
        if len(bars) < lookback + 1:
            return {
                **info,
                "status": "insufficient_data",
                "detail": f"需要{lookback}s秒级数据，当前不足，继续等待采集。",
            }
        recent = bars.tail(lookback + 1).copy()
        close = np.asarray(recent["close"].astype(float).values, dtype=float)
        lr = np.diff(np.log(close))
        lr = lr[np.isfinite(lr)]
        if len(lr) < 60:
            return {
                **info,
                "status": "insufficient_data",
                "detail": "有效秒级收益率不足60个，继续等待采集。",
            }
        mu = float(np.mean(lr))
        sigma = float(np.std(lr, ddof=1))
        if sigma < 1e-12:
            return {
                **info,
                "status": "flat_sigma",
                "detail": "当前波动过低，sigma接近0，暂不判断方向。",
            }
        sigma_10m_bps = math.sqrt(self.horizon_sec) * sigma * 10000.0
        info.update({
            "sigma_10m_bps": round(float(sigma_10m_bps), 4),
        })
        if not (float(branch["sigma_min_bps"]) <= sigma_10m_bps <= float(branch["sigma_max_bps"])):
            return {
                **info,
                "status": "sigma_out_of_range",
                "detail": (
                    f"10分钟sigma={sigma_10m_bps:.2f}bp，不在本档"
                    f"{float(branch['sigma_min_bps']):.0f}-{float(branch['sigma_max_bps']):.0f}bp范围。"
                ),
            }
        z = (self.horizon_sec * mu) / (math.sqrt(self.horizon_sec) * sigma)
        p_up = self._normal_cdf(z)
        up_gap = max(0.0, p_up - tail_pct) * 100.0
        down_gap = max(0.0, (1.0 - tail_pct) - p_up) * 100.0
        nearest_signal = "UP" if up_gap <= down_gap else "DOWN"
        edge_gap = min(up_gap, down_gap)
        info.update({
            "p_up": round(float(p_up), 6),
            "p_up_pct": round(float(p_up) * 100.0, 2),
            "z_score": round(float(z), 4),
            "nearest_signal": nearest_signal,
            "edge_gap_pct": round(float(edge_gap), 2),
        })
        signal = None
        if p_up >= 1.0 - tail_pct:
            signal = "DOWN"
        elif p_up <= tail_pct:
            signal = "UP"
        if not signal:
            return {
                **info,
                "status": "waiting_tail",
                "detail": (
                    f"p_up={p_up * 100.0:.1f}%，需<= {tail_pct * 100.0:.0f}%做多"
                    f" 或>= {(1.0 - tail_pct) * 100.0:.0f}%做空，"
                    f"最近还差约{edge_gap:.1f}个百分点。"
                ),
            }
        old_filter = self.filter
        old_zone = self.zone_filter
        self.filter = str(branch["second_filter"]).lower()
        self.zone_filter = str(branch["zone_filter"]).lower()
        try:
            ok, reason, vol_rank, flow_ratio = self._filter_allows(signal, bars.tail(max(lookback, 1800)))
            if not ok:
                return {
                    **info,
                    "signal": signal,
                    "has_tail_signal": True,
                    "blocked": True,
                    "status": "blocked_filter",
                    "reason": reason,
                    "detail": f"已经进入{signal}尾部，但被秒级资金流过滤拦截：{reason}。",
                }
            zone_ok, zone_reason, zone_extra = self._zone_filter_allows(signal, bars)
            if not zone_ok:
                return {
                    **info,
                    "signal": signal,
                    "has_tail_signal": True,
                    "blocked": True,
                    "status": "blocked_zone",
                    "reason": zone_reason,
                    "detail": f"已经进入{signal}尾部，但动态区间过滤未通过：{zone_reason}。",
                    **zone_extra,
                }
        finally:
            self.filter = old_filter
            self.zone_filter = old_zone
        return {
            **info,
            "status": "ready",
            "detail": f"{branch['role']}档进入{signal}尾部，过滤已通过，可作为候选信号。",
            "signal": signal,
            "has_tail_signal": True,
            "vol_rank_60s": None if vol_rank is None or not np.isfinite(vol_rank) else float(vol_rank),
            "flow_ratio_60s": None if flow_ratio is None or not np.isfinite(flow_ratio) else float(flow_ratio),
            **zone_extra,
        }

    @staticmethod
    def _nearest_branch_text(branch_diagnostics):
        waiting = [
            item for item in branch_diagnostics
            if item.get("status") == "waiting_tail" and item.get("edge_gap_pct") is not None
        ]
        if not waiting:
            return "等待三档路由任一分支满足波动范围和尾部概率。"
        nearest = min(waiting, key=lambda item: float(item.get("edge_gap_pct") or 999.0))
        role = nearest.get("role", "--")
        side = "做多" if nearest.get("nearest_signal") == "UP" else "做空"
        return (
            f"最近的是{role}档，倾向{side}，距离25%尾部还差约"
            f"{float(nearest.get('edge_gap_pct')):.1f}个百分点。"
        )

    @staticmethod
    def _cooldown_text(seconds):
        seconds = max(60, int(seconds or 0))
        if seconds % 3600 == 0:
            return f"{seconds // 3600}小时"
        if seconds % 60 == 0:
            return f"{seconds // 60}分钟"
        return f"{seconds}秒"

    def _loss_density_summary_text(self):
        return (
            f"服务端按实盘/影子已结算结果执行 "
            f"{self.loss_density_losses}/{self.loss_density_window}亏损密度冷却"
            f"{self._cooldown_text(self.loss_density_cooldown_sec)}"
        )

    def predict(self, df5=None):
        bars = self._load_seconds()
        required = max(self.route_lookback_sec + 1, max(int(b["lookback_sec"]) for b in self.BRANCHES) + 1)
        if bars is None or len(bars) < required:
            return None
        bars = bars.copy()
        bars["time"] = pd.to_datetime(bars["time"], utc=True, errors="coerce")
        bars = bars.dropna(subset=["time"]).sort_values("time")
        now_time = bars["time"].iloc[-1]
        close_tail = np.asarray(bars["close"].astype(float).tail(self.r10_window_sec).values, dtype=float)
        route_sigma = self._route_sigma_bps(bars)
        r10 = self._range_bps(close_tail)
        observed600 = self._observed_pct(bars, 600)
        price = float(bars["close"].iloc[-1])
        next_check_at = pd.Timestamp.now(tz="UTC") + pd.Timedelta(seconds=5)
        base = {
            "strategy_id": self.id,
            "model_type": "second_normal_router_v21",
            "model_label": self.model_label,
            "interval_min": self.interval_min,
            "duration": self.interval_min,
            "horizon_sec": self.horizon_sec,
            "min_gap_sec": self.min_gap_sec,
            "route_sigma_bps": None if not np.isfinite(route_sigma) else round(float(route_sigma), 4),
            "r10_bps": None if not np.isfinite(r10) else round(float(r10), 4),
            "observed600_pct": round(float(observed600), 4),
            "min_observed_pct": self.min_observed_pct,
            "time": now_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "price": round(price, 4),
            "entry": round(price, 4),
            "scan_interval_sec": 5,
            "next_check_time": next_check_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "next_check_time_shanghai": next_check_at.tz_convert("Asia/Shanghai").strftime("%Y/%m/%d %H:%M:%S"),
            "next_signal_estimate": "正式实盘口径约4.21单/天；策略每5秒滚动扫描，实际要等任一分支进入25%尾部，不按整点固定触发。",
            "bypass_entry_timing": True,
            "rsi_value": None,
            "agree": True,
            "vol_ok": True,
            "session_gate_ok": True,
            "rsi_extreme": True,
            "condition_summary": {
                "entry": "三档秒级正态路由：low/mid/high 动态选择，预测10分钟二元期权方向",
                "risk": "r10<=42bp，DOWN r10<=35bp，mid routeSigma<20，数据覆盖>=88%，low+UP否决",
                "loss_density": self._loss_density_summary_text(),
            },
        }
        if not np.isfinite(route_sigma) or not np.isfinite(r10):
            return {
                **base,
                "signal": None,
                "reason": "router_metrics_unavailable",
                "signal_detail": "路由指标暂不可用，等待秒级数据刷新。",
            }
        if observed600 < self.min_observed_pct:
            return {
                **base,
                "signal": None,
                "reason": "observed600_low",
                "signal_detail": f"秒级覆盖率{observed600:.1f}%，低于{self.min_observed_pct:.0f}%，暂不下单。",
            }
        if r10 > self.r10_cap_bps:
            return {
                **base,
                "signal": None,
                "reason": "r10_cap",
                "signal_detail": f"10分钟价格范围{r10:.1f}bp，超过{self.r10_cap_bps:.0f}bp，波动过大暂不下单。",
            }

        branch_results = []
        branch_diagnostics = []
        by_role = {}
        for branch in self.BRANCHES:
            result = self._branch_diagnostic(bars, branch)
            branch_diagnostics.append(result)
            if result and result.get("has_tail_signal"):
                branch_results.append(result)
                by_role.setdefault(branch["role"], result)
        rejects = []
        for role in self._role_order(route_sigma):
            item = by_role.get(role)
            if not item:
                continue
            if item.get("blocked"):
                rejects.append(f"{role}:{item.get('reason')}")
                continue
            if self.veto_low_up and role == "low" and item["signal"] == "UP":
                rejects.append("low_up_veto")
                item["blocked"] = True
                item["status"] = "blocked_low_up_veto"
                item["reason"] = "low_up_veto"
                item["detail"] = "V21 low档 UP 历史表现偏弱，当前跳过实盘，只保留诊断。"
                continue
            if role == "mid" and route_sigma >= self.mid_route_sigma_cap_bps:
                rejects.append("mid_sigma_cap")
                continue
            if item["signal"] == "DOWN" and r10 > self.down_r10_cap_bps:
                rejects.append("down_r10_cap")
                continue
            obs_lookback = self._observed_pct(bars, item["lookback_sec"])
            if obs_lookback < self.min_observed_pct:
                rejects.append("lookback_observed_low")
                continue
            confidence = abs(float(item["p_up"]) - 0.5) * 200.0
            return {
                **base,
                **item,
                "signal": item["signal"],
                "raw_signal": item["signal"],
                "confidence": round(min(confidence, 95.0), 1),
                "high_conf": True,
                "avg_prob": round(float(item["p_up"]), 4),
                "p_up": round(float(item["p_up"]), 6),
                "z_score": round(float(item["z_score"]), 4),
                "sigma_10m_bps": round(float(item["sigma_10m_bps"]), 4),
                "observed_lookback_pct": round(float(obs_lookback), 4),
                "reason": "v21_second_normal_router",
                "signal_detail": "V21秒级正态路由通过：动态选择波动分支，等待服务端亏损密度风控后执行。",
                "router_diagnostics": branch_diagnostics,
                }
        waiting_detail = "等待极端尾部：" + self._nearest_branch_text(branch_diagnostics)
        if "low_up_veto" in rejects:
            waiting_detail = "low档UP已被V21.1否决：历史回测表现偏弱，当前不下实单，等待其它分支信号。"
        return {
            **base,
            "signal": None,
            "reason": "no_router_branch",
            "router_rejects": rejects[-8:],
            "router_candidate_roles": [row.get("role") for row in branch_results],
            "router_diagnostics": branch_diagnostics,
            "signal_detail": waiting_detail,
        }


class SecondNormalLowVolV22Strategy(SecondNormalRouterV21Strategy):
    """Shadow handler for low-volatility V21 candidates.

    V21 remains the real strategy. V22 only observes the low-volatility state
    and records shadow signals after a short confirmation move.
    """

    def __init__(self, strategy_id, cfg):
        super().__init__(strategy_id, cfg)
        self.veto_low_up = self._cfg_bool(cfg.get("second_router_veto_low_up", False), False)
        self.low_vol_route_sigma_max_bps = float(cfg.get("second_lowvol_route_sigma_max_bps", 10.0))
        self.low_vol_confirm_sec = max(1, int(cfg.get("second_lowvol_confirm_sec", 15)))
        self.low_vol_reversion_bps = float(cfg.get("second_lowvol_reversion_bps", 0.5))
        self.low_vol_breakout_bps = float(cfg.get("second_lowvol_breakout_bps", 1.5))
        self.trade_enabled = bool(cfg.get("trade_enabled", False))
        self.model_label = str(cfg.get("model_label", "BTC_10min_V22_LOWVOL_CONFIRM_SHADOW"))

    def _decorate(self, row):
        out = dict(row or {})
        summary = dict(out.get("condition_summary") or {})
        summary.update({
            "entry": (
                f"V22低波动影子：routeSigma<{self.low_vol_route_sigma_max_bps:.1f}bp，"
                f"先出现V21尾部候选，再看{self.low_vol_confirm_sec}s确认方向"
            ),
            "risk": (
                f"回归确认>={self.low_vol_reversion_bps:.1f}bp按V21方向；"
                f"突破确认>={self.low_vol_breakout_bps:.1f}bp反向记录；"
                f"{'允许实盘' if self.trade_enabled else '默认影子观察'}"
            ),
            "loss_density": "V22不参与亏损密度冷却；是否实盘由页面策略开关决定"
        })
        out.update({
            "model_type": "second_normal_lowvol_v22",
            "model_label": self.model_label,
            "low_vol_handler": True,
            "shadow_only": not self.trade_enabled,
            "trade_enabled": self.trade_enabled,
            "low_vol_route_sigma_max_bps": round(float(self.low_vol_route_sigma_max_bps), 4),
            "low_vol_confirm_sec": int(self.low_vol_confirm_sec),
            "low_vol_reversion_bps": round(float(self.low_vol_reversion_bps), 4),
            "low_vol_breakout_bps": round(float(self.low_vol_breakout_bps), 4),
            "scan_interval_sec": 5,
            "next_signal_estimate": (
                "只在低波动状态观察：先等V21尾部候选，随后15秒内确认回归或突破；"
                "不是固定时间出单。"
            ),
            "condition_summary": summary,
        })
        return out

    @staticmethod
    def _opposite_signal(signal):
        return "DOWN" if signal == "UP" else "UP"

    def _confirmation_move(self):
        bars = self._load_seconds()
        if bars is None or len(bars) <= self.low_vol_confirm_sec:
            return None
        bars = bars.copy()
        bars["time"] = pd.to_datetime(bars["time"], utc=True, errors="coerce")
        bars = bars.dropna(subset=["time"]).sort_values("time")
        close = np.asarray(bars["close"].astype(float).values, dtype=float)
        if len(close) <= self.low_vol_confirm_sec:
            return None
        now_price = float(close[-1])
        prev_price = float(close[-1 - self.low_vol_confirm_sec])
        if not np.isfinite(now_price) or not np.isfinite(prev_price) or prev_price <= 0:
            return None
        return {
            "confirm_price_now": round(now_price, 4),
            "confirm_price_prev": round(prev_price, 4),
            "confirm_move_bps": float((now_price / prev_price - 1.0) * 10000.0),
        }

    def predict(self, df5=None):
        base = super().predict(df5)
        if base is None:
            return None
        out = self._decorate(base)
        try:
            route_sigma = float(out.get("route_sigma_bps"))
        except (TypeError, ValueError):
            route_sigma = float("nan")
        if not np.isfinite(route_sigma):
            out.update({
                "signal": None,
                "reason": "v22_route_sigma_unavailable",
                "signal_detail": "V22等待routeSigma可用后再判断低波动确认。"
            })
            return out
        if route_sigma >= self.low_vol_route_sigma_max_bps:
            out.update({
                "signal": None,
                "blocked_signal": out.get("signal"),
                "blocked_confidence": out.get("confidence"),
                "reason": "v22_low_vol_not_active",
                "signal_detail": (
                    f"当前routeSigma={route_sigma:.2f}bp，不属于V22低波动区间"
                    f"(<{self.low_vol_route_sigma_max_bps:.1f}bp)。"
                ),
            })
            return out
        candidate_signal = out.get("signal")
        if not candidate_signal:
            out.update({
                "reason": out.get("reason") or "v22_wait_tail_candidate",
                "signal_detail": "V22已进入低波动观察区，等待V21尾部候选后再确认回归/突破。"
            })
            return out

        move = self._confirmation_move()
        if move is None:
            out.update({
                "signal": None,
                "blocked_signal": candidate_signal,
                "blocked_confidence": out.get("confidence"),
                "reason": "v22_wait_confirm_data",
                "signal_detail": "V22低波动候选已出现，但确认窗口数据不足，继续等待。"
            })
            return out

        side = 1.0 if candidate_signal == "UP" else -1.0
        reversion_bps = side * float(move["confirm_move_bps"])
        breakout_bps = -reversion_bps
        out.update({
            **move,
            "low_vol_candidate_signal": candidate_signal,
            "low_vol_reversion_move_bps": round(float(reversion_bps), 4),
            "low_vol_breakout_move_bps": round(float(breakout_bps), 4),
        })
        if reversion_bps >= self.low_vol_reversion_bps:
            mode_text = "输出实盘信号" if self.trade_enabled else "记录影子单"
            out.update({
                "signal": candidate_signal,
                "raw_signal": candidate_signal,
                "low_vol_confirm_mode": "reversion",
                "reason": "v22_low_vol_reversion_confirm",
                "signal_detail": (
                    f"低波动回归确认：{self.low_vol_confirm_sec}s朝V21方向移动"
                    f"{reversion_bps:.2f}bp，{mode_text} {candidate_signal}。"
                ),
            })
            return out
        if breakout_bps >= self.low_vol_breakout_bps:
            signal = self._opposite_signal(candidate_signal)
            mode_text = "输出实盘信号" if self.trade_enabled else "记录影子单"
            try:
                confidence = float(out.get("confidence") or 0)
            except (TypeError, ValueError):
                confidence = 0.0
            out.update({
                "signal": signal,
                "raw_signal": candidate_signal,
                "blocked_signal": candidate_signal,
                "confidence": round(min(max(confidence, 60.0) + 3.0, 95.0), 1),
                "low_vol_confirm_mode": "breakout",
                "reason": "v22_low_vol_breakout_confirm",
                "signal_detail": (
                    f"低波动突破确认：{self.low_vol_confirm_sec}s反V21方向移动"
                    f"{breakout_bps:.2f}bp，{mode_text} {signal}。"
                ),
            })
            return out
        out.update({
            "signal": None,
            "blocked_signal": candidate_signal,
            "blocked_confidence": out.get("confidence"),
            "reason": "v22_low_vol_wait_confirm",
            "signal_detail": (
                f"低波动候选已出现，但确认不足：回归{reversion_bps:.2f}bp/"
                f"需{self.low_vol_reversion_bps:.1f}bp，突破{breakout_bps:.2f}bp/"
                f"需{self.low_vol_breakout_bps:.1f}bp。"
            ),
        })
        return out


class SecondNormalVwConfirmStrategy(SecondNormalStrategy):
    """Second-level normal reversal confirmed by volume-weighted returns plus ETA."""

    def __init__(self, strategy_id, cfg):
        super().__init__(strategy_id, cfg)
        self.eta_target_bps = float(cfg.get("eta_target_bps", 2.0))
        self.eta_max_wait_sec = int(cfg.get("eta_max_wait_sec", 45))

    @staticmethod
    def _normal_cdf(x):
        return 0.5 * (1.0 + math.erf(float(x) / math.sqrt(2.0)))

    def _forecast_eta(self, close, buy_qty, sell_qty, signal):
        import numpy as np

        speed_window = 30
        accel_window = 10
        min_speed_bps = 0.005
        idx = len(close) - 1
        if idx < speed_window + accel_window + 2:
            return False, {"eta_ok": False, "eta_reason": "warmup"}
        side = 1.0 if signal == "DOWN" else -1.0
        ret_bps = side * 10000.0 * np.diff(np.log(close), prepend=np.nan)
        recent = ret_bps[idx - speed_window + 1 : idx + 1]
        prev = ret_bps[idx - speed_window - accel_window + 1 : idx - accel_window + 1]
        if len(recent) < speed_window or len(prev) < speed_window:
            return False, {"eta_ok": False, "eta_reason": "warmup"}
        weights = np.linspace(1.0, 2.0, len(recent))
        pos_recent = np.clip(recent, 0.0, None)
        weighted_speed = float(np.average(pos_recent, weights=weights))
        net_move = float(np.nansum(recent))
        path = float(np.nansum(np.abs(recent)))
        efficiency = max(0.0, min(1.0, net_move / path)) if path > 1e-12 else 0.0
        volume = buy_qty[idx - speed_window + 1 : idx + 1] + sell_qty[idx - speed_window + 1 : idx + 1]
        flow = side * (buy_qty[idx - speed_window + 1 : idx + 1] - sell_qty[idx - speed_window + 1 : idx + 1])
        flow_eff = float(np.nansum(flow) / max(np.nansum(volume), 1e-12))
        flow_multiplier = max(0.25, min(1.5, 1.0 + flow_eff))
        v_now = float(np.nanmean(np.clip(recent[-accel_window:], 0.0, None)))
        v_prev = float(np.nanmean(np.clip(prev[-accel_window:], 0.0, None)))
        accel = (v_now - v_prev) / max(float(accel_window), 1.0)
        raw_speed = weighted_speed * max(efficiency, 0.15) * flow_multiplier
        if raw_speed < min_speed_bps:
            return False, {
                "eta_ok": False,
                "eta_reason": "no_momentum",
                "eta_sec": 1_000_000_000.0,
                "eta_speed_bps_sec": raw_speed,
                "eta_efficiency": efficiency,
                "eta_flow_eff": flow_eff,
            }
        eta_linear = self.eta_target_bps / raw_speed
        eta_accel = eta_linear
        if abs(accel) > 1e-9:
            disc = raw_speed * raw_speed + 2.0 * accel * self.eta_target_bps
            if disc > 0:
                root = (-raw_speed + math.sqrt(disc)) / accel
                if root > 0 and np.isfinite(root):
                    eta_accel = float(root)
        eta = max(1.0, 0.65 * eta_linear + 0.35 * eta_accel)
        ok = eta <= float(self.eta_max_wait_sec)
        return ok, {
            "eta_ok": bool(ok),
            "eta_reason": "predicted_reachable" if ok else "eta_too_slow",
            "eta_sec": round(float(eta), 4),
            "eta_speed_bps_sec": round(float(raw_speed), 8),
            "eta_efficiency": round(float(efficiency), 6),
            "eta_flow_eff": round(float(flow_eff), 6),
            "eta_accel_bps_sec2": round(float(accel), 8),
        }

    def predict(self, df5=None):
        import numpy as np

        bars = self._load_seconds()
        warmup = max(self.lookback_sec + 1, self.lookback_sec + 45)
        if bars is None or len(bars) < warmup:
            return None
        recent = bars.tail(self.lookback_sec + 1).copy()
        close = np.asarray(recent["close"].astype(float).values, dtype=float)
        volume = np.asarray(recent["volume"].astype(float).values, dtype=float)
        buy_qty = np.asarray(bars["buy_qty"].astype(float).values, dtype=float)
        sell_qty = np.asarray(bars["sell_qty"].astype(float).values, dtype=float)
        full_close = np.asarray(bars["close"].astype(float).values, dtype=float)
        lr = np.log(close[1:] / close[:-1])
        lr = lr[np.isfinite(lr)]
        if len(lr) < 60:
            return None
        mu = float(np.mean(lr))
        sigma = float(np.std(lr, ddof=1))
        if sigma < 1e-12:
            return None
        sigma_10m_bps = math.sqrt(self.horizon_sec) * sigma * 10000.0
        z = (self.horizon_sec * mu) / (math.sqrt(self.horizon_sec) * sigma)
        p_up = self._normal_cdf(z)
        threshold_hi = 1.0 - self.tail_pct
        signal = "DOWN" if p_up >= threshold_hi else "UP" if p_up <= self.tail_pct else None

        vw_signal = None
        vw_p_up = None
        vw_z = None
        if len(volume) > 1:
            weights = np.nan_to_num(volume[1:], nan=0.0)
            x = np.nan_to_num(np.log(close[1:] / close[:-1]), nan=0.0)
            sw = float(np.sum(weights))
            if sw > 1e-12:
                vw_mu = float(np.sum(weights * x) / sw)
                vw_sigma = math.sqrt(max(float(np.sum(weights * x * x) / sw - vw_mu * vw_mu), 0.0))
                if vw_sigma > 1e-12:
                    vw_z = (self.horizon_sec * vw_mu) / (math.sqrt(self.horizon_sec) * vw_sigma)
                    vw_p_up = self._normal_cdf(vw_z)
                    vw_signal = "DOWN" if vw_p_up >= threshold_hi else "UP" if vw_p_up <= self.tail_pct else None

        conf = abs(p_up - 0.5) * 200
        signal_time = bars["time"].iloc[-1]
        last_price = float(full_close[-1])
        base = {
            "strategy_id": self.id,
            "confidence": round(min(conf, 95), 1),
            "avg_prob": round(p_up, 4),
            "rsi_value": None,
            "high_conf": bool(signal),
            "agree": True,
            "vol_ok": True,
            "session_gate_ok": True,
            "rsi_extreme": True,
            "z_score": round(float(z), 4),
            "p_up": round(float(p_up), 4),
            "vw_p_up": None if vw_p_up is None else round(float(vw_p_up), 4),
            "vw_z_score": None if vw_z is None else round(float(vw_z), 4),
            "mu_sec": round(mu, 10),
            "sigma_sec": round(sigma, 10),
            "lookback_sec": self.lookback_sec,
            "horizon_sec": self.horizon_sec,
            "interval_min": self.interval_min,
            "duration": self.interval_min,
            "min_gap_sec": self.min_gap_sec,
            "tail_pct": self.tail_pct,
            "second_zone_filter": self.zone_filter,
            "eta_target_bps": self.eta_target_bps,
            "eta_max_wait_sec": self.eta_max_wait_sec,
            "time": signal_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model_type": "second_normal_vw_confirm",
            "bypass_entry_timing": False,
        }
        zone_context = dynamic_zone_context_from_bars(bars)
        base.update({
            "zone_filter": self.zone_filter,
            "zone_signal_hint": dynamic_zone_signal_hint(self.zone_filter, signal, zone_context),
            **compact_zone_context(zone_context),
        })
        if not signal:
            return {**base, "signal": None, "reason": "no_edge"}
        if vw_signal != signal:
            return {
                **base,
                "signal": None,
                "reason": "vw_confirm_not_aligned",
                "blocked_signal": signal,
                "blocked_confidence": round(min(conf, 95), 1),
            }
        zone_ok, zone_reason, zone_extra = self._zone_filter_allows(signal, bars)
        base.update(zone_extra)
        if not zone_ok:
            return {
                **base,
                "signal": None,
                "reason": zone_reason,
                "blocked_signal": signal,
                "blocked_confidence": round(min(conf, 95), 1),
            }
        eta_ok, eta_extra = self._forecast_eta(full_close, buy_qty, sell_qty, signal)
        base.update(eta_extra)
        if not eta_ok:
            return {
                **base,
                "signal": None,
                "reason": eta_extra.get("eta_reason", "eta_blocked"),
                "blocked_signal": signal,
                "blocked_confidence": round(min(conf, 95), 1),
            }
        target = last_price * math.exp((self.eta_target_bps if signal == "DOWN" else -self.eta_target_bps) / 10000.0)
        return {
            **base,
            "signal": signal,
            "reason": "second_normal_vw_confirm_eta",
            "price": round(last_price, 4),
            "eta_entry_target_price": round(float(target), 4),
            "eta_entry_reference_price": round(last_price, 4),
        }


class NormalStateV11Strategy(SecondNormalStrategy):
    """Live V11 normal-state false-break reversion strategy.

    This mirrors the V11 backtest trigger, but it never requires future
    settlement data. It only emits a signal after the configured confirmation
    delay has already elapsed.
    """

    def __init__(self, strategy_id, cfg):
        super().__init__(strategy_id, cfg)
        self.lookback_sec = int(cfg.get("normal_state_lookback_sec", cfg.get("second_lookback_sec", 180 * 60)))
        self.horizon_sec = int(cfg.get("normal_state_horizon_sec", cfg.get("second_horizon_sec", 600)))
        self.min_gap_sec = int(cfg.get("normal_state_min_gap_sec", cfg.get("second_min_gap_sec", 600)))
        self.confirm_delay_sec = int(cfg.get("normal_state_confirm_delay_sec", 5))
        self.max_adverse_bps = float(cfg.get("normal_state_max_adverse_bps", 5.0))
        self.signal_hold_sec = int(cfg.get("normal_state_signal_hold_sec", 55))
        self.bandwalk_max = float(cfg.get("normal_state_bandwalk_max", 6.0))
        self.min_consensus_votes = int(cfg.get("normal_state_min_consensus_votes", 2))
        self.scan_extra_sec = int(cfg.get(
            "normal_state_scan_extra_sec",
            max(3600, self.min_gap_sec + 1800, self.confirm_delay_sec + self.signal_hold_sec + 900),
        ))
        self.state_gate = str(cfg.get("normal_state_state_gate", "edge_persistence_lt6"))
        self.confirmation_veto = str(
            cfg.get("normal_state_confirmation_veto", cfg.get("normalStateConfirmationVeto", "none"))
        ).lower()
        self.interval_min = max(1, int(round(self.horizon_sec / 60)))

    def _orderbook_features(self, second_index):
        return load_orderbook_features_cached(second_index)

    @staticmethod
    def _observed_ratio(observed, start_idx, end_idx):
        if start_idx < 0 or end_idx <= start_idx or end_idx > len(observed):
            return 0.0
        return float(observed.iloc[start_idx:end_idx].mean())

    def _state_gate_allows(self, row):
        bandwalk = normal_state_v6.finite(row.get("m_bandwalk10"))
        sigma10 = normal_state_v6.finite(row.get("sigma10_bps"))
        half_life = normal_state_v6.finite(row.get("m_half_life_min"))
        if self.state_gate in ("", "edge_persistence_lt6"):
            ok = np.isfinite(bandwalk) and bandwalk < self.bandwalk_max
            return ok, "bandwalk_lt6" if ok else "persistent_edge"
        if self.state_gate == "v15_bw35_or_early_sigma18":
            mild_bandwalk = np.isfinite(bandwalk) and 3.0 <= bandwalk < self.bandwalk_max
            early_high_vol = np.isfinite(bandwalk) and bandwalk < 3.0 and np.isfinite(sigma10) and sigma10 > 18.0
            ok = bool(mild_bandwalk or early_high_vol)
            if mild_bandwalk:
                return True, "mild_bandwalk_3_5"
            if early_high_vol:
                return True, "early_high_vol_sigma_gt18"
            return False, "v15_state_reject"
        if self.state_gate == "avoid_slow_persistent_edge":
            bad = np.isfinite(bandwalk) and np.isfinite(half_life) and bandwalk >= self.bandwalk_max and half_life > 8.0
            return not bad, "pass" if not bad else "slow_persistent_edge"
        if self.state_gate == "avoid_lowvol_slow_edge":
            bad = (
                np.isfinite(sigma10)
                and np.isfinite(bandwalk)
                and np.isfinite(half_life)
                and sigma10 < 18.0
                and bandwalk >= 5.0
                and half_life > 8.0
            )
            return not bad, "pass" if not bad else "lowvol_slow_edge"
        ok = np.isfinite(bandwalk) and bandwalk < self.bandwalk_max
        return ok, "fallback_bandwalk_lt_max" if ok else "fallback_persistent_edge"

    @staticmethod
    def _side_value(row, key):
        side = normal_state_v6.finite(row.get("breakout_side"))
        value = normal_state_v6.finite(row.get(key))
        if not np.isfinite(side) or not np.isfinite(value):
            return float("nan")
        return float(side * value)

    def _confirmation_veto_reason(self, row):
        name = self.confirmation_veto
        if name in ("", "none", "off", "false"):
            return None

        adverse = normal_state_v6.finite(row.get("confirm_adverse_bps"))
        confirm_weak = np.isfinite(adverse) and -1.4 < adverse < 1.0
        ob_available = bool(row.get("ob_available"))
        side_imb = self._side_value(row, "ob_imb20")
        side_micro = self._side_value(row, "ob_micro_bps")
        ob_weak = ob_available and (
            (np.isfinite(side_imb) and side_imb > -0.35)
            or (np.isfinite(side_micro) and side_micro > -0.0035)
        )

        width = normal_state_v6.finite(row.get("m_width_ratio"))
        sigma10 = normal_state_v6.finite(row.get("sigma10_bps"))
        bandwalk = normal_state_v6.finite(row.get("m_bandwalk10"))
        price_weak = (
            (np.isfinite(width) and np.isfinite(sigma10) and width > 2.2 and sigma10 < 18.0)
            or (np.isfinite(bandwalk) and np.isfinite(sigma10) and bandwalk <= 3.0 and sigma10 < 15.0)
        )

        if name == "ob_confirm_weak" and ob_weak and confirm_weak:
            return "ob_confirm_weak"
        if name == "ob_weak" and ob_weak:
            return "ob_weak"
        if name == "price_confirm_weak" and price_weak and confirm_weak:
            return "price_confirm_weak"
        if name == "ob_or_price_weak" and ((ob_weak and confirm_weak) or (price_weak and confirm_weak)):
            return "ob_or_price_weak"
        if name not in ("ob_confirm_weak", "ob_weak", "price_confirm_weak", "ob_or_price_weak"):
            raise ValueError(f"unknown confirmation veto: {self.confirmation_veto}")
        return None

    def _condition_summary(self):
        state_text = {
            "edge_persistence_lt6": "Bandwalk < 6",
            "v15_bw35_or_early_sigma18": "Bandwalk 3-5，或早期突破且10分钟波动 > 18bp",
            "avoid_slow_persistent_edge": "避开慢速持续贴边",
            "avoid_lowvol_slow_edge": "避开低波动慢速贴边",
        }.get(self.state_gate, self.state_gate)
        veto_text = {
            "none": "无额外V19过滤",
            "ob_confirm_weak": "V19：订单薄反向不足且5秒确认偏弱时跳过",
            "ob_weak": "订单薄反向不足时跳过",
            "price_confirm_weak": "价格结构偏弱且5秒确认偏弱时跳过",
            "ob_or_price_weak": "订单薄或价格结构偏弱且5秒确认偏弱时跳过",
        }.get(self.confirmation_veto, self.confirmation_veto)
        return {
            "state": state_text,
            "veto": veto_text,
            "entry": f"价格先突破±1.96σ，再回到正态区间，等待{self.confirm_delay_sec}秒确认",
            "expiry": f"{self.interval_min}分钟二元期权，到期比较入场价",
            "gap": f"同策略两次入场至少间隔{self.min_gap_sec}秒",
        }

    def _next_scan_payload(self, now_time, *, candidates=None, confirmed=None):
        candidates = candidates or []
        confirmed = confirmed or []
        next_check = pd.Timestamp.now(tz="UTC") + pd.Timedelta(seconds=5)
        last_candidate = candidates[-1] if candidates else None
        if confirmed:
            estimate = "已有有效信号，等待信号有效期内执行或到期结算。"
        elif last_candidate:
            signal_idx = int(last_candidate.get("idx", 0))
            due_idx = signal_idx + self.confirm_delay_sec
            estimate = "刚出现过候选假突破，正在等待5秒确认、V19过滤和间隔锁。"
            if due_idx < len(getattr(self, "_last_index_for_hint", [])):
                due_time = self._last_index_for_hint[due_idx]
                estimate = f"最近候选确认点约 {due_time.tz_convert('Asia/Shanghai').strftime('%H:%M:%S')}，未通过则继续等下一次假突破。"
        else:
            estimate = "当前没有假突破回归候选；价格需要先离开10分钟正态区间，再回到区间并通过确认。"
        return {
            "next_check_time": next_check.isoformat(),
            "next_check_time_shanghai": next_check.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
            "next_signal_estimate": estimate,
            "condition_summary": self._condition_summary(),
            "candidate_count": len(candidates),
        }

    def _candidate_rows(self, bars_indexed, features):
        sec = normal_state_v1.build_second_context(bars_indexed, self.lookback_sec)
        close = bars_indexed["close"].to_numpy(float)
        z_arr = sec["z"].to_numpy(float)
        sigma10_arr = sec["sigma10_bps"].to_numpy(float)
        flow60_arr = sec["flow60"].to_numpy(float)
        obs600_arr = sec["obs600"].to_numpy(float)
        m_cover_arr = features["m_cover2_120"].to_numpy(float)
        m_width_arr = features["m_width_ratio"].to_numpy(float)
        m_slope_arr = features["m_slope60_bps"].to_numpy(float)
        m_bandwalk_arr = features["m_bandwalk10"].to_numpy(float)
        m_half_life_arr = features["m_half_life_min"].to_numpy(float) if "m_half_life_min" in features else np.full(len(features), np.nan)
        ob_available_arr = features["ob_available"].to_numpy(bool)
        ob_imb_arr = features["ob_imb20"].to_numpy(float)
        ob_micro_arr = features["ob_micro_bps"].to_numpy(float)

        rows = []
        state = None
        start = max(self.lookback_sec, 3600)
        end = len(bars_indexed) - self.confirm_delay_sec
        for i in range(start, max(start, end)):
            z = z_arr[i]
            if not np.isfinite(z):
                continue
            if obs600_arr[i] < 0.98:
                state = None
                continue
            if state is None:
                if z >= 1.96:
                    state = {"side": 1.0, "start": i, "peak": abs(float(z))}
                elif z <= -1.96:
                    state = {"side": -1.0, "start": i, "peak": abs(float(z))}
                continue
            breakout_side = float(state["side"])
            state["peak"] = max(float(state["peak"]), abs(float(z)))
            outside_sec = i - int(state["start"])
            if outside_sec > 900 or breakout_side * z < 0:
                state = None
                continue
            reentered = (breakout_side > 0 and z <= 1.96) or (breakout_side < 0 and z >= -1.96)
            if not reentered:
                continue
            sigma10 = sigma10_arr[i]
            flow60 = flow60_arr[i]
            m_cover = m_cover_arr[i]
            m_width = m_width_arr[i]
            m_slope = m_slope_arr[i]
            m_bandwalk = m_bandwalk_arr[i]
            m_half_life = m_half_life_arr[i]
            if not all(np.isfinite(x) for x in (sigma10, flow60, m_cover, m_width, m_slope, m_bandwalk)):
                state = None
                continue
            if breakout_side * flow60 > 0.12:
                state = None
                continue
            signal = "DOWN" if breakout_side > 0 else "UP"
            rows.append(
                {
                    "idx": int(i),
                    "time": bars_indexed.index[i].isoformat(),
                    "mode": "reversion",
                    "signal": signal,
                    "breakout_side": breakout_side,
                    "entry": round(float(close[i]), 2),
                    "z": round(float(z), 4),
                    "peak_abs_z": round(float(state["peak"]), 4),
                    "outside_sec": int(outside_sec),
                    "sigma10_bps": round(float(sigma10), 4),
                    "flow60": round(float(flow60), 5),
                    "m_cover2_120": round(float(m_cover), 5),
                    "m_width_ratio": round(float(m_width), 5),
                    "m_slope60_bps": round(float(m_slope), 4),
                    "m_bandwalk10": round(float(m_bandwalk), 2),
                    "m_half_life_min": None if not np.isfinite(float(m_half_life)) else round(float(m_half_life), 4),
                    "ob_available": bool(ob_available_arr[i]),
                    "ob_imb20": None if not np.isfinite(float(ob_imb_arr[i])) else round(float(ob_imb_arr[i]), 5),
                    "ob_micro_bps": None if not np.isfinite(float(ob_micro_arr[i])) else round(float(ob_micro_arr[i]), 5),
                }
            )
            state = None
        return rows

    def _confirmed_rows(self, bars_indexed, candidates):
        close = bars_indexed["close"].to_numpy(float)
        observed = bars_indexed["observed"].astype(float)
        spec = next((s for s in normal_state_v6.rule_specs() if s.name == "V6_CONSENSUS_2OF5_UPPER"), None)
        out = []
        last_entry_idx = -10**9
        for row in sorted(candidates, key=lambda r: int(r["idx"])):
            annotated = normal_state_v6.annotate_base_quality(row, "upper_only")
            if not annotated or spec is None:
                continue
            ok, rule_detail = normal_state_v6.rule_allows(annotated, spec)
            if not ok:
                continue
            votes_n, votes = normal_state_v6.consensus_votes(annotated)
            state_ok, state_reason = self._state_gate_allows(annotated)
            if not state_ok:
                continue
            if votes_n < self.min_consensus_votes:
                continue
            signal_idx = int(annotated["idx"])
            entry_idx = signal_idx + self.confirm_delay_sec
            if entry_idx >= len(close):
                continue
            if self._observed_ratio(observed, max(0, entry_idx - 600), entry_idx) < 0.98:
                continue
            signal = str(annotated["signal"])
            signal_entry = float(annotated["entry"])
            delayed_entry = float(close[entry_idx])
            adverse_bps = (delayed_entry / signal_entry - 1.0) * 10000.0
            if signal == "UP":
                adverse_bps = -adverse_bps
            if adverse_bps > self.max_adverse_bps:
                continue
            item = dict(annotated)
            item.update(
                {
                    "idx": int(entry_idx),
                    "signal_time": annotated["time"],
                    "time": bars_indexed.index[entry_idx].isoformat(),
                    "entry": round(delayed_entry, 2),
                    "signal_entry": round(signal_entry, 2),
                    "confirm_delay_sec": int(self.confirm_delay_sec),
                    "confirm_adverse_bps": round(float(adverse_bps), 4),
                    "confirm_max_adverse_bps": float(self.max_adverse_bps),
                    "consensus_votes": int(votes_n),
                    "consensus_vote_names": ",".join(votes),
                    "rule_filter_detail": rule_detail,
                    "state_gate": self.state_gate,
                    "state_gate_reason": state_reason,
                    "confirmation_veto": self.confirmation_veto,
                }
            )
            veto_reason = self._confirmation_veto_reason(item)
            if veto_reason is not None:
                continue
            if entry_idx - last_entry_idx < self.min_gap_sec:
                continue
            out.append(item)
            last_entry_idx = entry_idx
        return out

    def predict(self, df5=None):
        bars = self._load_seconds()
        min_rows = self.lookback_sec + 3600 + self.confirm_delay_sec + 5
        if bars is None or len(bars) < min_rows:
            return None
        indexed = bars.copy()
        indexed["time"] = pd.to_datetime(indexed["time"], utc=True, errors="coerce")
        indexed = indexed.dropna(subset=["time"]).set_index("time").sort_index()
        indexed = indexed.tail(max(min_rows, self.lookback_sec + self.scan_extra_sec)).copy()
        now_time = indexed.index[-1]
        self._last_index_for_hint = indexed.index
        base = {
            "strategy_id": self.id,
            "model_type": "normal_state_v11",
            "model_label": "BTC_10min_NORMAL_STATE_V11_BANDWALK_2OF5_D5A5",
            "interval_min": self.interval_min,
            "duration": self.interval_min,
            "lookback_sec": self.lookback_sec,
            "horizon_sec": self.horizon_sec,
            "min_gap_sec": self.min_gap_sec,
            "confirm_delay_sec": self.confirm_delay_sec,
            "max_adverse_bps": self.max_adverse_bps,
            "bandwalk_max": self.bandwalk_max,
            "min_consensus_votes": self.min_consensus_votes,
            "scan_extra_sec": self.scan_extra_sec,
            "state_gate": self.state_gate,
            "confirmation_veto": self.confirmation_veto,
            "condition_summary": self._condition_summary(),
            "time": now_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bypass_entry_timing": True,
            "rsi_value": None,
            "rsi_extreme": True,
            "agree": True,
            "high_conf": False,
            "vol_ok": True,
            "session_gate_ok": True,
        }
        try:
            context_key = (
                int(len(indexed)),
                int(indexed.index[-1].value),
                int(self.lookback_sec),
                int(self.confirm_delay_sec),
                int(self.scan_extra_sec),
            )
            cached = _NORMAL_V11_CONTEXT_CACHE.get("context") if _NORMAL_V11_CONTEXT_CACHE.get("key") == context_key else None
            if cached is not None:
                candidates = cached["candidates"]
            else:
                minute = load_minute_features_cached(indexed.index)
                orderbook = self._orderbook_features(indexed.index)
                features = pd.concat(
                    [
                        minute.drop(columns=["minute_source"], errors="ignore"),
                        orderbook.drop(columns=["orderbook_source"], errors="ignore"),
                    ],
                    axis=1,
                )
                candidates = self._candidate_rows(indexed, features)
                _NORMAL_V11_CONTEXT_CACHE["key"] = context_key
                _NORMAL_V11_CONTEXT_CACHE["context"] = {"candidates": candidates}
            confirmed = self._confirmed_rows(indexed, candidates)
        except Exception as exc:
            print(f"[Signal] V11 predict failed: {exc}")
            return {**base, "signal": None, "reason": "normal_state_v11_error", "error": str(exc)[:300]}
        if not confirmed:
            hint = self._next_scan_payload(now_time, candidates=candidates, confirmed=[])
            return {
                **base,
                **hint,
                "signal": None,
                "reason": "no_confirmed_false_break",
                "signal_detail": "暂无有效信号：还没有同时满足假突破回归、5秒确认、状态过滤和V19过滤。",
            }
        latest = confirmed[-1]
        age_sec = (now_time - pd.Timestamp(latest["time"])).total_seconds()
        hint = self._next_scan_payload(now_time, candidates=candidates, confirmed=confirmed)
        payload = {
            **base,
            **hint,
            "time": pd.Timestamp(latest["time"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "actionable_time": pd.Timestamp(latest["time"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "signal_time": latest.get("signal_time"),
            "price": float(latest["entry"]),
            "entry": float(latest["entry"]),
            "signal_entry": float(latest["signal_entry"]),
            "confidence": min(95.0, 60.0 + float(latest.get("consensus_votes") or 0) * 7.5),
            "avg_prob": None,
            "high_conf": True,
            "raw_signal": latest.get("signal"),
            "blocked_signal": latest.get("signal"),
            "z_score": latest.get("z"),
            "peak_abs_z": latest.get("peak_abs_z"),
            "outside_sec": latest.get("outside_sec"),
            "sigma10_bps": latest.get("sigma10_bps"),
            "flow60": latest.get("flow60"),
            "m_cover2_120": latest.get("m_cover2_120"),
            "m_width_ratio": latest.get("m_width_ratio"),
            "m_slope60_bps": latest.get("m_slope60_bps"),
            "m_bandwalk10": latest.get("m_bandwalk10"),
            "m_half_life_min": latest.get("m_half_life_min"),
            "ob_available": latest.get("ob_available"),
            "ob_imb20": latest.get("ob_imb20"),
            "ob_micro_bps": latest.get("ob_micro_bps"),
            "consensus_votes": latest.get("consensus_votes"),
            "consensus_vote_names": latest.get("consensus_vote_names"),
            "rule_filter_detail": latest.get("rule_filter_detail"),
            "state_gate": latest.get("state_gate"),
            "state_gate_reason": latest.get("state_gate_reason"),
            "confirmation_veto": latest.get("confirmation_veto"),
            "confirm_adverse_bps": latest.get("confirm_adverse_bps"),
            "signal_age_sec": round(float(age_sec), 3),
            "signal_detail": "有效信号：假突破已回到正态区间，且5秒确认和过滤条件通过。",
        }
        if age_sec > self.signal_hold_sec:
            return {
                **payload,
                "signal": None,
                "high_conf": False,
                "reason": "confirmed_signal_expired",
            }
        return {**payload, "signal": latest.get("signal"), "reason": "normal_state_v11_confirmed_false_break"}


class SecondChipStrategy(SecondNormalStrategy):
    """Second-level POC chip-zone reversal for 10m binary options."""

    def __init__(self, strategy_id, cfg):
        super().__init__(strategy_id, cfg)
        self.lookback_sec = int(cfg.get("second_chip_lookback_sec", cfg.get("second_lookback_sec", 3600)))
        self.horizon_sec = int(cfg.get("second_chip_horizon_sec", cfg.get("second_horizon_sec", 600)))
        self.min_gap_sec = int(cfg.get("second_chip_min_gap_sec", cfg.get("second_min_gap_sec", self.horizon_sec)))
        self.target_share = float(cfg.get("second_chip_target_share", 0.2))
        self.bin_mode = str(cfg.get("second_chip_bin_mode", "fixed")).lower()
        self.bin_pct = float(cfg.get("second_chip_bin_pct", 0.0003))
        self.bin_size = float(cfg.get("second_chip_bin_size", 20))
        self.break_pct = float(cfg.get("second_chip_break_pct", 0.0023))
        self.direction_filter = str(cfg.get("second_chip_direction_filter", "breakout_up_only")).lower()
        self.chip_filter = str(cfg.get("second_chip_filter", "none")).lower()
        self.signal_hold_sec = int(cfg.get("second_chip_signal_hold_sec", cfg.get("second_signal_hold_sec", 60)))
        self.interval_min = max(1, int(round(self.horizon_sec / 60)))

    def _dynamic_bin_size(self, price):
        if self.bin_mode == "percent":
            return max(1.0, float(price) * self.bin_pct)
        return max(1.0, self.bin_size)

    def _state_at(self, close, volume, price, bin_size):
        zone = self._chip_zone(close, volume, bin_size)
        if not zone:
            return None
        upper = zone["high"] * (1.0 + self.break_pct)
        lower = zone["low"] * (1.0 - self.break_pct)
        state = "inside"
        if price > upper:
            state = "above"
        elif price < lower:
            state = "below"
        return zone, state, upper, lower

    def _chip_zone(self, close, volume, bin_size):
        import numpy as np

        bins = np.rint(close / bin_size).astype(int)
        offset = int(bins.min())
        size = int(bins.max() - offset + 1)
        counts = np.zeros(size, dtype=float)
        vols = np.zeros(size, dtype=float)
        for b0, vol in zip(bins, volume):
            b = b0 - offset
            counts[b] += 1
            vols[b] += vol
        total = counts.sum()
        if total <= 0:
            return None
        total_vol = vols.sum()
        poc = int(np.argmax(counts))
        lo = hi = poc
        zone_count = counts[poc]
        while zone_count / max(total, 1e-12) < self.target_share:
            left = counts[lo - 1] if lo > 0 else -1
            right = counts[hi + 1] if hi + 1 < size else -1
            if left < 0 and right < 0:
                break
            if right > left:
                hi += 1
                zone_count += counts[hi]
            else:
                lo -= 1
                zone_count += counts[lo]
        sl = slice(lo, hi + 1)
        return {
            "low": (lo + offset) * bin_size,
            "high": (hi + offset) * bin_size,
            "poc": (poc + offset) * bin_size,
            "share": counts[sl].sum() / max(total, 1e-12),
            "volume_share": vols[sl].sum() / max(total_vol, 1e-12) if total_vol > 0 else 0.0,
            "width_bins": hi - lo + 1,
        }

    def _direction_allowed(self, breakout):
        if self.direction_filter in ("", "none", "all"):
            return True
        if self.direction_filter == "breakout_up_only":
            return breakout == "UP"
        if self.direction_filter == "breakout_down_only":
            return breakout == "DOWN"
        return True

    def _chip_filter_allows(self, signal, zone, flow300):
        if self.chip_filter in ("", "none", "off", "false"):
            return True, "disabled"
        if self.chip_filter == "width_lte_3":
            return int(zone.get("width_bins", 999999)) <= 3, "width_lte_3"
        if self.chip_filter == "width_lte_5":
            return int(zone.get("width_bins", 999999)) <= 5, "width_lte_5"
        if self.chip_filter == "flow_reversal":
            if flow300 is None or not np.isfinite(flow300):
                return False, "flow_missing"
            ok = (signal == "UP" and flow300 < 0) or (signal == "DOWN" and flow300 > 0)
            return ok, "flow_reversal"
        return False, f"unknown_chip_filter_{self.chip_filter}"

    def _position(self, price, state, upper, lower):
        if state == "above":
            return price / max(upper, 1e-12) - 1.0, "above_upper_trigger"
        if state == "below":
            return lower / max(price, 1e-12) - 1.0, "below_lower_trigger"
        upper_gap = upper / max(price, 1e-12) - 1.0
        lower_gap = price / max(lower, 1e-12) - 1.0
        return min(max(0.0, upper_gap), max(0.0, lower_gap)), "inside_nearest_trigger_gap"

    def _transition_signal(self, state, prev_state, price, zone):
        if state == "above" and prev_state != "above":
            return "DOWN", "UP", price / max(zone["high"], 1e-12) - 1.0
        if state == "below" and prev_state != "below":
            return "UP", "DOWN", zone["low"] / max(price, 1e-12) - 1.0
        return None, None, 0.0

    def predict(self, df5=None):
        import numpy as np

        bars = self._load_seconds()
        if bars is None or len(bars) < self.lookback_sec + 2:
            return None
        scan_sec = max(1, min(int(self.signal_hold_sec), max(1, len(bars) - self.lookback_sec - 1)))
        recent = bars.tail(self.lookback_sec + scan_sec + 1).copy()
        close = np.asarray(recent["close"].astype(float).values, dtype=float)
        volume = np.asarray(recent["volume"].astype(float).values, dtype=float)
        buy_qty = np.asarray(recent["buy_qty"].astype(float).values, dtype=float)
        sell_qty = np.asarray(recent["sell_qty"].astype(float).values, dtype=float)
        price = float(close[-1])
        bin_size = self._dynamic_bin_size(price)
        current = self._state_at(close[-self.lookback_sec:], volume[-self.lookback_sec:], price, bin_size)
        prev = self._state_at(close[-self.lookback_sec - 1:-1], volume[-self.lookback_sec - 1:-1], float(close[-2]), bin_size)
        if not current:
            return None
        zone, state, upper, lower = current
        prev_state = prev[1] if prev else "unknown"
        signal = None
        breakout = None
        distance_pct = 0.0
        signal_time = bars["time"].iloc[-1]
        signal_zone = zone
        signal_state = state
        signal_prev_state = prev_state
        signal_price = price
        signal_upper = upper
        signal_lower = lower
        transition_index = None
        signal_flow300 = None
        for i in range(len(close) - 1, self.lookback_sec - 1, -1):
            scan_price = float(close[i])
            scan_bin_size = self._dynamic_bin_size(scan_price)
            scan_current = self._state_at(close[i - self.lookback_sec + 1:i + 1], volume[i - self.lookback_sec + 1:i + 1], scan_price, scan_bin_size)
            scan_prev = self._state_at(close[i - self.lookback_sec:i], volume[i - self.lookback_sec:i], float(close[i - 1]), scan_bin_size)
            if not scan_current:
                continue
            scan_zone, scan_state, scan_upper, scan_lower = scan_current
            scan_prev_state = scan_prev[1] if scan_prev else "unknown"
            scan_signal, scan_breakout, scan_distance = self._transition_signal(scan_state, scan_prev_state, scan_price, scan_zone)
            flow_start = max(0, i - 299)
            scan_flow300 = float(np.sum(buy_qty[flow_start:i + 1] - sell_qty[flow_start:i + 1]))
            if scan_signal and self._direction_allowed(scan_breakout):
                signal = scan_signal
                breakout = scan_breakout
                distance_pct = scan_distance
                signal_flow300 = scan_flow300
                signal_time = recent["time"].iloc[i]
                signal_zone = scan_zone
                signal_state = scan_state
                signal_prev_state = scan_prev_state
                signal_price = scan_price
                signal_upper = scan_upper
                signal_lower = scan_lower
                transition_index = i
                break
            if scan_signal and not self._direction_allowed(scan_breakout):
                signal_time = recent["time"].iloc[i]
                signal_zone = scan_zone
                signal_state = scan_state
                signal_prev_state = scan_prev_state
                signal_price = scan_price
                signal_upper = scan_upper
                signal_lower = scan_lower
                transition_index = i
                breakout = scan_breakout
                distance_pct = scan_distance
                signal_flow300 = scan_flow300
                break

        has_transition = transition_index is not None
        display_zone = signal_zone if has_transition else zone
        display_state = signal_state if has_transition else state
        display_prev_state = signal_prev_state if has_transition else prev_state
        display_lower = signal_lower if has_transition else lower
        display_upper = signal_upper if has_transition else upper
        display_price = signal_price if has_transition else price
        if has_transition:
            position_pct, position_label = self._position(display_price, display_state, display_upper, display_lower)
        else:
            position_pct, position_label = self._position(price, state, upper, lower)
        distance_conf = max(0.0, min(95.0, (distance_pct / max(self.break_pct, 1e-12)) * 50.0))
        base = {
            "strategy_id": self.id,
            "confidence": round(distance_conf, 1),
            "avg_prob": None,
            "rsi_value": None,
            "high_conf": bool(signal),
            "agree": True,
            "vol_ok": True,
            "session_gate_ok": True,
            "rsi_extreme": True,
            "lookback_sec": self.lookback_sec,
            "horizon_sec": self.horizon_sec,
            "interval_min": self.interval_min,
            "duration": self.interval_min,
            "min_gap_sec": self.min_gap_sec,
            "chip_target_share": round(self.target_share, 6),
            "chip_bin_mode": self.bin_mode,
            "chip_bin_pct": round(self.bin_pct, 8),
            "chip_bin_size": round(float(bin_size), 4),
            "chip_break_pct": round(self.break_pct, 8),
            "chip_direction_filter": self.direction_filter,
            "chip_filter": self.chip_filter,
            "chip_flow300": round(float(signal_flow300), 6) if signal_flow300 is not None and np.isfinite(signal_flow300) else None,
            "chip_signal_hold_sec": self.signal_hold_sec,
            "chip_state": display_state,
            "chip_prev_state": display_prev_state,
            "chip_current_state": state,
            "chip_current_prev_state": prev_state,
            "chip_poc": round(float(display_zone["poc"]), 2),
            "chip_zone_low": round(float(display_zone["low"]), 2),
            "chip_zone_high": round(float(display_zone["high"]), 2),
            "chip_lower_trigger": round(float(display_lower), 2),
            "chip_upper_trigger": round(float(display_upper), 2),
            "chip_zone_share": round(float(display_zone["share"]), 4),
            "chip_zone_volume_share": round(float(display_zone["volume_share"]), 4),
            "chip_zone_width_bins": int(display_zone["width_bins"]),
            "chip_current_poc": round(float(zone["poc"]), 2),
            "chip_current_zone_low": round(float(zone["low"]), 2),
            "chip_current_zone_high": round(float(zone["high"]), 2),
            "chip_current_lower_trigger": round(float(lower), 2),
            "chip_current_upper_trigger": round(float(upper), 2),
            "chip_distance_pct": round(float(distance_pct), 6),
            "chip_position_pct": round(float(position_pct), 6),
            "chip_position_label": position_label,
            "time": signal_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model_type": "second_chip",
            "bypass_entry_timing": True,
        }
        if transition_index is not None:
            base.update({
                "chip_signal_price": round(float(signal_price), 2),
                "chip_signal_state": signal_state,
                "chip_signal_prev_state": signal_prev_state,
                "chip_signal_poc": round(float(signal_zone["poc"]), 2),
                "chip_signal_zone_low": round(float(signal_zone["low"]), 2),
                "chip_signal_zone_high": round(float(signal_zone["high"]), 2),
                "chip_signal_lower_trigger": round(float(signal_lower), 2),
                "chip_signal_upper_trigger": round(float(signal_upper), 2),
                "chip_signal_age_sec": int(max(0, len(close) - 1 - transition_index)),
            })
        if not signal:
            reason = "inside_chip_zone" if state == "inside" else "already_outside_chip_zone"
            if transition_index is not None:
                return {**base, "signal": None, "reason": "direction_filter", "blocked_signal": "DOWN" if breakout == "UP" else "UP", "chip_breakout": breakout}
            return {**base, "signal": None, "reason": reason}
        filter_ok, filter_reason = self._chip_filter_allows(signal, signal_zone, signal_flow300)
        if not filter_ok:
            return {**base, "signal": None, "reason": "chip_filter", "blocked_signal": signal, "chip_breakout": breakout, "chip_filter_reason": filter_reason}
        return {**base, "signal": signal, "reason": "second_chip_reversal", "chip_breakout": breakout}


class SecondRangeBreakoutConfirmStrategy(SecondNormalStrategy):
    """Second-level range breakout continuation with a causal confirmation window."""

    def __init__(self, strategy_id, cfg):
        super().__init__(strategy_id, cfg)
        self.lookback_sec = int(cfg.get("second_range_lookback_sec", cfg.get("second_lookback_sec", 1800)))
        self.horizon_sec = int(cfg.get("second_range_horizon_sec", cfg.get("second_horizon_sec", 600)))
        self.min_gap_sec = int(cfg.get("second_range_signal_gap_sec", cfg.get("second_min_gap_sec", self.horizon_sec)))
        self.z_entry = float(cfg.get("second_range_z_entry", 2.2))
        self.confirm_sec = int(cfg.get("second_range_confirm_sec", 60))
        self.hold_z = float(cfg.get("second_range_hold_z", 1.0))
        self.min_hold_ratio = float(cfg.get("second_range_min_hold_ratio", 0.75))
        self.pre_slope_sec = int(cfg.get("second_range_pre_slope_sec", 300))
        self.confirm_slope_sec = int(cfg.get("second_range_confirm_slope_sec", self.confirm_sec))
        self.min_pre_slope_bps = float(cfg.get("second_range_min_pre_slope_bps", 8.0))
        self.min_confirm_slope_bps = float(cfg.get("second_range_min_confirm_slope_bps", 4.0))
        self.min_flow_imbalance = float(cfg.get("second_range_min_flow_imbalance", 0.12))
        self.min_confirm_flow_imbalance = float(cfg.get("second_range_min_confirm_flow_imbalance", 0.08))
        self.min_volume_ratio = float(cfg.get("second_range_min_volume_ratio", 0.45))
        self.min_volatility_ratio = float(cfg.get("second_range_min_volatility_ratio", 0.55))
        self.max_age_beyond_sec = int(cfg.get("second_range_max_age_beyond_sec", 180))
        self.interval_min = max(1, int(round(self.horizon_sec / 60)))

    def predict(self, df5=None):
        import numpy as np
        import pandas as pd

        bars = self._load_seconds()
        warmup = self.lookback_sec + self.confirm_sec + max(self.pre_slope_sec, self.confirm_slope_sec) + 5
        if bars is None or len(bars) < warmup:
            return None
        close = np.asarray(bars["close"].astype(float).values, dtype=float)
        volume = np.asarray(bars["volume"].astype(float).values, dtype=float)
        buy_qty = np.asarray(bars["buy_qty"].astype(float).values, dtype=float)
        sell_qty = np.asarray(bars["sell_qty"].astype(float).values, dtype=float)
        idx = len(close) - 1
        break_idx = idx - self.confirm_sec
        pre = max(30, self.pre_slope_sec)
        conf = max(10, self.confirm_slope_sec)
        if break_idx - max(self.lookback_sec, pre) < 0:
            return None

        look = close[break_idx - self.lookback_sec : break_idx]
        mu = float(np.nanmean(look))
        sigma = float(np.nanstd(look, ddof=1))
        signal_time = bars["time"].iloc[-1]
        base = {
            "strategy_id": self.id,
            "confidence": 0,
            "avg_prob": None,
            "rsi_value": None,
            "high_conf": False,
            "agree": True,
            "vol_ok": True,
            "session_gate_ok": True,
            "rsi_extreme": True,
            "lookback_sec": self.lookback_sec,
            "horizon_sec": self.horizon_sec,
            "interval_min": self.interval_min,
            "duration": self.interval_min,
            "min_gap_sec": self.min_gap_sec,
            "z_entry": self.z_entry,
            "confirm_sec": self.confirm_sec,
            "time": signal_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model_type": "second_range_breakout_confirm",
            "bypass_entry_timing": True,
        }
        if not np.isfinite(mu) or not np.isfinite(sigma) or sigma <= 1e-12:
            return {**base, "signal": None, "reason": "range_sigma_invalid"}

        break_z = (close[break_idx] - mu) / sigma
        if not np.isfinite(break_z) or abs(break_z) < self.z_entry:
            return {**base, "signal": None, "reason": "inside_range", "range_z": round(float(break_z), 4)}
        side = 1.0 if break_z > 0 else -1.0
        signal = "UP" if side > 0 else "DOWN"

        age = 0
        for j in range(break_idx, max(-1, break_idx - self.max_age_beyond_sec), -1):
            zj = (close[j] - mu) / sigma
            if abs(zj) >= self.z_entry and np.sign(zj) == np.sign(break_z):
                age += 1
            else:
                break
        if self.max_age_beyond_sec > 0 and age > self.max_age_beyond_sec:
            return {**base, "signal": None, "reason": "breakout_too_old", "blocked_signal": signal, "beyond_age_sec": age}

        pre_slope_bps = (close[break_idx] / close[break_idx - pre] - 1.0) * 10000.0
        aligned_pre_slope = side * pre_slope_bps
        buy_pre = float(np.nansum(buy_qty[break_idx - pre + 1 : break_idx + 1]))
        sell_pre = float(np.nansum(sell_qty[break_idx - pre + 1 : break_idx + 1]))
        flow = (buy_pre - sell_pre) / max(buy_pre + sell_pre, 1e-12)
        aligned_flow = side * flow
        if aligned_pre_slope < self.min_pre_slope_bps:
            return {**base, "signal": None, "reason": "pre_slope_not_aligned", "blocked_signal": signal, "aligned_pre_slope_bps": round(float(aligned_pre_slope), 4)}
        if aligned_flow < self.min_flow_imbalance:
            return {**base, "signal": None, "reason": "pre_flow_not_aligned", "blocked_signal": signal, "aligned_flow_imbalance": round(float(aligned_flow), 4)}

        vol_pre = float(np.nansum(volume[break_idx - pre + 1 : break_idx + 1]))
        vol_ref = float(pd.Series(volume[max(0, break_idx - self.lookback_sec) : break_idx + 1]).mean() * pre)
        vol_ratio = vol_pre / max(vol_ref, 1e-12)
        ret = np.abs(np.diff(np.log(close[max(0, break_idx - self.lookback_sec) : break_idx + 1]), prepend=np.nan))
        abs_ref = float(np.nanmean(ret))
        abs_pre = float(np.nanmean(np.abs(np.diff(np.log(close[break_idx - pre : break_idx + 1]), prepend=np.nan))))
        volatility_ratio = abs_pre / max(abs_ref, 1e-12)
        if not np.isfinite(vol_ratio) or vol_ratio < self.min_volume_ratio:
            return {**base, "signal": None, "reason": "volume_ratio_low", "blocked_signal": signal, "pre_volume_ratio": round(float(vol_ratio), 4)}
        if not np.isfinite(volatility_ratio) or volatility_ratio < self.min_volatility_ratio:
            return {**base, "signal": None, "reason": "volatility_ratio_low", "blocked_signal": signal, "pre_volatility_ratio": round(float(volatility_ratio), 4)}

        path_z = side * ((close[break_idx + 1 : idx + 1] - mu) / sigma)
        hold_ratio = float(np.mean(path_z >= self.hold_z)) if len(path_z) else 0.0
        if hold_ratio < self.min_hold_ratio:
            return {**base, "signal": None, "reason": "confirm_hold_failed", "blocked_signal": signal, "hold_ratio": round(hold_ratio, 4)}

        conf_start = max(break_idx, idx - conf)
        confirm_slope_bps = (close[idx] / close[conf_start] - 1.0) * 10000.0
        aligned_confirm_slope = side * confirm_slope_bps
        buy_conf = float(np.nansum(buy_qty[conf_start + 1 : idx + 1]))
        sell_conf = float(np.nansum(sell_qty[conf_start + 1 : idx + 1]))
        confirm_flow = (buy_conf - sell_conf) / max(buy_conf + sell_conf, 1e-12)
        aligned_confirm_flow = side * confirm_flow
        if aligned_confirm_slope < self.min_confirm_slope_bps:
            return {**base, "signal": None, "reason": "confirm_slope_not_aligned", "blocked_signal": signal, "aligned_confirm_slope_bps": round(float(aligned_confirm_slope), 4)}
        if aligned_confirm_flow < self.min_confirm_flow_imbalance:
            return {**base, "signal": None, "reason": "confirm_flow_not_aligned", "blocked_signal": signal, "aligned_confirm_flow_imbalance": round(float(aligned_confirm_flow), 4)}

        confidence = min(95.0, 45.0 + abs(float(break_z)) * 7.0 + aligned_confirm_slope)
        return {
            **base,
            "signal": signal,
            "reason": "second_range_breakout_confirm",
            "confidence": round(float(confidence), 1),
            "high_conf": True,
            "price": round(float(close[idx]), 4),
            "break_time": bars["time"].iloc[break_idx].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "break_z": round(float(break_z), 4),
            "confirm_z": round(float((close[idx] - mu) / sigma), 4),
            "hold_ratio": round(float(hold_ratio), 4),
            "pre_slope_bps": round(float(pre_slope_bps), 4),
            "confirm_slope_bps": round(float(confirm_slope_bps), 4),
            "pre_flow_imbalance": round(float(flow), 4),
            "confirm_flow_imbalance": round(float(confirm_flow), 4),
            "pre_volume_ratio": round(float(vol_ratio), 4),
            "pre_volatility_ratio": round(float(volatility_ratio), 4),
            "range_mean": round(float(mu), 4),
            "range_sigma_bps": round(float(sigma / close[break_idx] * 10000.0), 4),
        }


class SecondValueAreaSmartStrategy(SecondNormalStrategy):
    """Failed value-area breakout fade with order-book and loss-pause guards."""

    def __init__(self, strategy_id, cfg):
        super().__init__(strategy_id, cfg)
        self.lookback_sec = int(cfg.get("second_va_lookback_sec", cfg.get("second_lookback_sec", 4200)))
        self.horizon_sec = int(cfg.get("second_va_horizon_sec", cfg.get("second_horizon_sec", 600)))
        self.min_gap_sec = int(cfg.get("second_va_signal_gap_sec", cfg.get("second_min_gap_sec", 600)))
        self.tail_pct = float(cfg.get("second_va_tail_pct", 0.20))
        self.sigma_min_bps = float(cfg.get("second_va_sigma_min_bps", 8.0))
        self.sigma_max_bps = float(cfg.get("second_va_sigma_max_bps", 80.0))
        self.value_area_sec = int(cfg.get("second_va_value_area_sec", 3600))
        self.bin_size = float(cfg.get("second_va_bin_size", 10.0))
        self.value_pct = float(cfg.get("second_va_value_pct", 0.70))
        self.normal_window_sec = int(cfg.get("second_va_normal_window_sec", 600))
        self.normal_coverage = float(cfg.get("second_va_normal_coverage", 0.70))
        self.mode = str(cfg.get("second_va_mode", "failed_break_fade")).lower()
        self.min_edge_bps = float(cfg.get("second_va_min_edge_bps", 1.0))
        self.min_flow = float(cfg.get("second_va_min_flow", 0.05))
        self.min_trend_bps = float(cfg.get("second_va_min_trend_bps", 1.0))
        self.min_volume_ratio = float(cfg.get("second_va_min_volume_ratio", 1.15))
        self.min_ob_imbalance = float(cfg.get("second_va_min_ob_imbalance", 0.05))
        self.min_micro_bps = float(cfg.get("second_va_min_micro_bps", 0.001))
        self.max_against_ob_imbalance = float(cfg.get("second_va_max_against_ob_imbalance", 0.25))
        self.max_against_flow = float(cfg.get("second_va_max_against_flow", 0.35))
        self.retest_sec = int(cfg.get("second_va_retest_sec", 180))
        self.retest_bps = float(cfg.get("second_va_retest_bps", 4.0))
        self.break_hold_sec = int(cfg.get("second_va_break_hold_sec", 30))
        self.reclaim_bps = float(cfg.get("second_va_reclaim_bps", 0.8))
        self.absorption_max_progress_bps = float(cfg.get("second_va_absorption_max_progress_bps", 1.5))
        self.loss_pause_after = int(cfg.get("second_va_loss_pause_after", 2))
        self.loss_pause_sec = int(cfg.get("second_va_loss_pause_sec", 1800))
        self.interval_min = max(1, int(round(self.horizon_sec / 60)))
        self.pending_signals = []
        self.loss_streak = 0
        self.pause_until = None
        self.last_emit_time = None

    @staticmethod
    def _normal_cdf(x):
        return 0.5 * (1.0 + math.erf(float(x) / math.sqrt(2.0)))

    @staticmethod
    def _value_area(close, volume, seconds, bin_size, value_pct):
        if len(close) < max(300, int(seconds) // 4):
            return None
        p = np.asarray(close[-int(seconds):], dtype=float)
        v = np.asarray(volume[-int(seconds):], dtype=float)
        mask = np.isfinite(p) & (p > 0) & np.isfinite(v) & (v >= 0)
        p = p[mask]
        v = v[mask]
        if len(p) < max(300, int(seconds) // 4):
            return None
        if float(np.sum(v)) <= 1e-12:
            v = np.ones(len(p), dtype=float)
        bins = np.round(p / float(bin_size)) * float(bin_size)
        hist = {}
        for price_bin, qty in zip(bins, v):
            key = float(price_bin)
            hist[key] = hist.get(key, 0.0) + float(qty)
        items = sorted(hist.items())
        prices = np.array([x[0] for x in items], dtype=float)
        vols = np.array([x[1] for x in items], dtype=float)
        total = float(np.sum(vols))
        if total <= 1e-12:
            return None
        poc_i = int(np.argmax(vols))
        chosen = {poc_i}
        covered = float(vols[poc_i])
        lo = hi = poc_i
        while covered / total < float(value_pct) and (lo > 0 or hi < len(vols) - 1):
            left_vol = vols[lo - 1] if lo > 0 else -1.0
            right_vol = vols[hi + 1] if hi < len(vols) - 1 else -1.0
            if right_vol >= left_vol:
                hi += 1
                chosen.add(hi)
                covered += float(vols[hi])
            else:
                lo -= 1
                chosen.add(lo)
                covered += float(vols[lo])
        val = float(prices[min(chosen)])
        vah = float(prices[max(chosen)])
        poc = float(prices[poc_i])
        now = float(close[-1])
        width_bps = (vah - val) / now * 10000.0 if now > 0 else float("nan")
        pos = (now - val) / max(vah - val, 1e-12)
        return {
            "val": val,
            "vah": vah,
            "poc": poc,
            "pos": float(pos),
            "width_bps": float(width_bps),
            "outside_up_bps": max(0.0, (now / vah - 1.0) * 10000.0),
            "outside_down_bps": max(0.0, (val / now - 1.0) * 10000.0),
            "inside": bool(val <= now <= vah),
        }

    @staticmethod
    def _flow_imbalance(buy_qty, sell_qty, seconds):
        buy = float(np.nansum(np.asarray(buy_qty[-int(seconds):], dtype=float)))
        sell = float(np.nansum(np.asarray(sell_qty[-int(seconds):], dtype=float)))
        total = buy + sell
        return 0.0 if total <= 1e-12 else (buy - sell) / total

    @staticmethod
    def _normal_price_zone(close, seconds, coverage):
        if len(close) < max(120, int(seconds) // 3):
            return None
        p = np.asarray(close[-int(seconds):], dtype=float)
        p = p[np.isfinite(p) & (p > 0)]
        if len(p) < max(120, int(seconds) // 3):
            return None
        mean = float(np.mean(p))
        sigma = float(np.std(p, ddof=1)) if len(p) > 1 else float("nan")
        if not np.isfinite(sigma) or sigma <= 1e-12:
            return None
        z = 1.036433389 if abs(float(coverage) - 0.70) < 1e-9 else 1.036433389
        low = mean - z * sigma
        high = mean + z * sigma
        now = float(close[-1])
        width = max(high - low, 1e-12)
        return {
            "normal_mean": mean,
            "normal_sigma": sigma,
            "normal_low": float(low),
            "normal_high": float(high),
            "normal_pos": float((now - low) / width),
            "normal_width_bps": float(width / now * 10000.0) if now > 0 else float("nan"),
            "normal_inside": bool(low <= now <= high),
        }

    @staticmethod
    def _ret_bps(close, seconds):
        if len(close) <= int(seconds):
            return float("nan")
        base = float(close[-1 - int(seconds)])
        now = float(close[-1])
        return (now / max(base, 1e-12) - 1.0) * 10000.0

    @staticmethod
    def _volume_ratio(volume, seconds, lookback_sec):
        v = np.asarray(volume, dtype=float)
        if len(v) < int(lookback_sec) or len(v) < int(seconds):
            return float("nan")
        current = float(np.nansum(v[-int(seconds):]))
        sums = []
        start = max(0, len(v) - int(lookback_sec))
        for end in range(start + int(seconds), len(v) + 1):
            sums.append(float(np.nansum(v[end - int(seconds):end])))
        if len(sums) < 5:
            return float("nan")
        baseline = float(np.nanmedian(sums))
        return current / max(baseline, 1e-12)

    def _latest_orderbook(self, signal_time):
        rows = csv_tail_rows(ORDERBOOK_FILE, limit=12, chunk_size=32768)
        best = None
        for row in reversed(rows):
            ts = pd.to_datetime(row.get("timestamp"), utc=True, errors="coerce")
            if pd.isna(ts):
                continue
            age = abs((pd.Timestamp(signal_time).tz_convert("UTC") - ts).total_seconds())
            if age <= 5:
                best = (row, ts, age)
                break
        if best is None:
            return None
        row, ts, age = best
        try:
            return {
                "timestamp": ts,
                "age_sec": float(age),
                "imbalance_20": float(row.get("imbalance_20")),
                "microprice_edge_bps": float(row.get("microprice_edge_bps")),
                "spread_bps": float(row.get("spread_bps") or 0.0),
            }
        except (TypeError, ValueError):
            return None

    def _settle_pending(self, bars):
        if not self.pending_signals:
            return
        close = np.asarray(bars["close"].astype(float).values, dtype=float)
        times = pd.to_datetime(bars["time"], utc=True)
        last_time = times.iloc[-1]
        remaining = []
        for row in self.pending_signals:
            settle_time = row["time"] + pd.Timedelta(seconds=self.horizon_sec)
            if settle_time > last_time:
                remaining.append(row)
                continue
            idx = int(np.searchsorted(times.values, np.datetime64(settle_time.to_datetime64()), side="left"))
            if idx >= len(close):
                remaining.append(row)
                continue
            won = close[idx] > row["entry"] if row["signal"] == "UP" else close[idx] < row["entry"]
            if won:
                self.loss_streak = 0
            else:
                self.loss_streak += 1
                if self.loss_pause_after > 0 and self.loss_streak >= self.loss_pause_after:
                    self.pause_until = settle_time + pd.Timedelta(seconds=self.loss_pause_sec)
        self.pending_signals = remaining

    def predict(self, df5=None):
        bars = self._load_seconds()
        warmup = max(self.lookback_sec, self.value_area_sec, self.retest_sec, 300) + 2
        if bars is None or len(bars) < warmup:
            return None
        self._settle_pending(bars)
        recent = bars.tail(max(self.lookback_sec + 1, self.value_area_sec + 1, self.retest_sec + 1)).copy()
        close = np.asarray(recent["close"].astype(float).values, dtype=float)
        volume = np.asarray(recent["volume"].astype(float).values, dtype=float)
        buy_qty = np.asarray(recent["buy_qty"].astype(float).values, dtype=float)
        sell_qty = np.asarray(recent["sell_qty"].astype(float).values, dtype=float)
        signal_time = pd.Timestamp(recent["time"].iloc[-1]).tz_convert("UTC")
        price = float(close[-1])

        lr = np.diff(np.log(close[-self.lookback_sec:]), prepend=np.nan)
        lr = lr[np.isfinite(lr)]
        mu = float(np.nanmean(lr)) if len(lr) else float("nan")
        sigma = float(np.nanstd(lr, ddof=1)) if len(lr) > 1 else float("nan")
        sigma_10m_bps = math.sqrt(self.horizon_sec) * sigma * 10000.0 if np.isfinite(sigma) else float("nan")
        z = self.horizon_sec * mu / (math.sqrt(self.horizon_sec) * sigma) if np.isfinite(sigma) and sigma > 1e-12 else float("nan")
        p_up = self._normal_cdf(z) if np.isfinite(z) else None
        confidence = 0.0 if p_up is None else min(95.0, abs(float(p_up) - 0.5) * 200.0)
        base = {
            "strategy_id": self.id,
            "confidence": round(float(confidence), 1),
            "avg_prob": None if p_up is None else round(float(p_up), 4),
            "rsi_value": None,
            "high_conf": False,
            "agree": True,
            "vol_ok": True,
            "session_gate_ok": True,
            "rsi_extreme": True,
            "price": round(price, 4),
            "p_up": None if p_up is None else round(float(p_up), 4),
            "z_score": None if not np.isfinite(z) else round(float(z), 4),
            "sigma_10m_bps": None if not np.isfinite(sigma_10m_bps) else round(float(sigma_10m_bps), 4),
            "lookback_sec": self.lookback_sec,
            "horizon_sec": self.horizon_sec,
            "interval_min": self.interval_min,
            "duration": self.interval_min,
            "min_gap_sec": self.min_gap_sec,
            "value_area_sec": self.value_area_sec,
            "tail_pct": self.tail_pct,
            "model_type": "second_value_area_smart",
            "time": signal_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bypass_entry_timing": True,
            "loss_streak": int(self.loss_streak),
            "pause_until": None if self.pause_until is None else self.pause_until.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if self.pause_until is not None and signal_time < self.pause_until:
            return {**base, "signal": None, "reason": "loss_pause_active"}
        if self.last_emit_time is not None and (signal_time - self.last_emit_time).total_seconds() < self.min_gap_sec:
            return {**base, "signal": None, "reason": "strategy_gap"}
        if not np.isfinite(sigma_10m_bps) or not (self.sigma_min_bps <= sigma_10m_bps <= self.sigma_max_bps):
            return {**base, "signal": None, "reason": "sigma_out_of_range"}

        area = self._value_area(close, volume, self.value_area_sec, self.bin_size, self.value_pct)
        if area is None:
            return {**base, "signal": None, "reason": "value_area_warmup"}
        normal = self._normal_price_zone(close, self.normal_window_sec, self.normal_coverage)
        if normal is None:
            return {**base, "signal": None, "reason": "normal70_warmup"}
        flow = self._flow_imbalance(buy_qty, sell_qty, 300)
        trend_30s = self._ret_bps(close, 30)
        trend_60s = self._ret_bps(close, 60)
        trend_180s = self._ret_bps(close, 180)
        vol_ratio = self._volume_ratio(volume, 60, max(self.normal_window_sec, 600))
        ob = self._latest_orderbook(signal_time)
        area_extra = {
            "val": round(float(area["val"]), 4),
            "vah": round(float(area["vah"]), 4),
            "poc": round(float(area["poc"]), 4),
            "va_pos": round(float(area["pos"]), 6),
            "va_width_bps": round(float(area["width_bps"]), 4),
            "outside_up_bps": round(float(area["outside_up_bps"]), 4),
            "outside_down_bps": round(float(area["outside_down_bps"]), 4),
            "normal_low": round(float(normal["normal_low"]), 4),
            "normal_high": round(float(normal["normal_high"]), 4),
            "normal_mean": round(float(normal["normal_mean"]), 4),
            "normal_pos": round(float(normal["normal_pos"]), 6),
            "normal_width_bps": round(float(normal["normal_width_bps"]), 4),
            "normal_inside": bool(normal["normal_inside"]),
            "flow_5m": round(float(flow), 6),
            "trend_30s_bps": None if not np.isfinite(trend_30s) else round(float(trend_30s), 4),
            "trend_60s_bps": None if not np.isfinite(trend_60s) else round(float(trend_60s), 4),
            "trend_180s_bps": None if not np.isfinite(trend_180s) else round(float(trend_180s), 4),
            "volume_ratio_60s": None if not np.isfinite(vol_ratio) else round(float(vol_ratio), 4),
            "ob_available": bool(ob is not None),
        }
        base.update(area_extra)
        if ob is None:
            return {**base, "signal": None, "reason": "orderbook_missing_or_stale"}
        ob_imb = float(ob["imbalance_20"])
        ob_micro = float(ob["microprice_edge_bps"])
        base.update({
            "ob_imbalance_20": round(ob_imb, 6),
            "ob_micro_bps": round(ob_micro, 6),
            "ob_age_sec": round(float(ob["age_sec"]), 3),
        })

        ob_up = ob_imb >= self.min_ob_imbalance and ob_micro >= self.min_micro_bps
        ob_down = ob_imb <= -self.min_ob_imbalance and ob_micro <= -self.min_micro_bps
        flow_up = flow >= self.min_flow
        flow_down = flow <= -self.min_flow
        confirm_up = flow_up and ob_up
        confirm_down = flow_down and ob_down

        prev = close[-self.retest_sec:]
        prev_high = float(np.nanmax(prev))
        prev_low = float(np.nanmin(prev))
        broke_up_recent = prev_high >= area["vah"] * (1.0 + self.min_edge_bps / 10000.0)
        broke_down_recent = prev_low <= area["val"] * (1.0 - self.min_edge_bps / 10000.0)
        normal_broke_up_recent = prev_high >= normal["normal_high"] * (1.0 + self.min_edge_bps / 10000.0)
        normal_broke_down_recent = prev_low <= normal["normal_low"] * (1.0 - self.min_edge_bps / 10000.0)
        hold_window = close[-max(1, self.break_hold_sec):]
        hold_up = len(hold_window) >= max(5, self.break_hold_sec // 2) and bool(
            np.all(hold_window > normal["normal_high"] * (1.0 + self.reclaim_bps / 10000.0))
        )
        hold_down = len(hold_window) >= max(5, self.break_hold_sec // 2) and bool(
            np.all(hold_window < normal["normal_low"] * (1.0 - self.reclaim_bps / 10000.0))
        )
        strong_volume = bool(np.isfinite(vol_ratio) and vol_ratio >= self.min_volume_ratio)
        true_break_up = (
            price > normal["normal_high"] * (1.0 + self.min_edge_bps / 10000.0)
            and (hold_up or (trend_60s >= self.min_trend_bps and strong_volume and confirm_up))
        )
        true_break_down = (
            price < normal["normal_low"] * (1.0 - self.min_edge_bps / 10000.0)
            and (hold_down or (trend_60s <= -self.min_trend_bps and strong_volume and confirm_down))
        )
        reclaimed_from_up = normal_broke_up_recent and price <= normal["normal_high"] * (1.0 - self.reclaim_bps / 10000.0)
        reclaimed_from_down = normal_broke_down_recent and price >= normal["normal_low"] * (1.0 + self.reclaim_bps / 10000.0)
        absorption_up = (flow_up or ob_up) and np.isfinite(trend_30s) and trend_30s <= self.absorption_max_progress_bps
        absorption_down = (flow_down or ob_down) and np.isfinite(trend_30s) and trend_30s >= -self.absorption_max_progress_bps
        base.update({
            "normal_broke_up_recent": bool(normal_broke_up_recent),
            "normal_broke_down_recent": bool(normal_broke_down_recent),
            "true_break_up": bool(true_break_up),
            "true_break_down": bool(true_break_down),
            "reclaimed_from_up": bool(reclaimed_from_up),
            "reclaimed_from_down": bool(reclaimed_from_down),
            "absorption_up": bool(absorption_up),
            "absorption_down": bool(absorption_down),
        })
        signal = None
        reason = None
        if self.mode == "failed_break_fade":
            if broke_up_recent and area["inside"] and price < area["vah"] and not confirm_up:
                signal, reason = "DOWN", "vah_failed_break_fade"
            elif broke_down_recent and area["inside"] and price > area["val"] and not confirm_down:
                signal, reason = "UP", "val_failed_break_fade"
        elif self.mode in {"normal70_liquidity_v2", "normal70_liq_v2"}:
            if true_break_up or true_break_down:
                return {**base, "signal": None, "reason": "true_breakout_guard"}
            if reclaimed_from_up and not confirm_up:
                signal, reason = "DOWN", "normal70_up_fake_break_revert"
            elif reclaimed_from_down and not confirm_down:
                signal, reason = "UP", "normal70_down_fake_break_revert"
            elif normal["normal_inside"] and normal["normal_pos"] >= 0.90 and not confirm_up and trend_30s <= self.min_trend_bps:
                signal, reason = "DOWN", "normal70_upper_reversion"
            elif normal["normal_inside"] and normal["normal_pos"] <= 0.10 and not confirm_down and trend_30s >= -self.min_trend_bps:
                signal, reason = "UP", "normal70_lower_reversion"
            if signal == "DOWN" and confirm_up and not absorption_up:
                signal, reason = None, None
            elif signal == "UP" and confirm_down and not absorption_down:
                signal, reason = None, None
        if not signal:
            return {
                **base,
                "signal": None,
                "reason": "waiting_failed_break",
                "prev_high": round(prev_high, 4),
                "prev_low": round(prev_low, 4),
            }
        if signal == "UP" and ob_imb < -self.max_against_ob_imbalance:
            return {**base, "signal": None, "reason": "against_orderbook", "blocked_signal": signal}
        if signal == "DOWN" and ob_imb > self.max_against_ob_imbalance:
            return {**base, "signal": None, "reason": "against_orderbook", "blocked_signal": signal}
        if signal == "UP" and flow < -self.max_against_flow:
            return {**base, "signal": None, "reason": "against_flow", "blocked_signal": signal}
        if signal == "DOWN" and flow > self.max_against_flow:
            return {**base, "signal": None, "reason": "against_flow", "blocked_signal": signal}

        self.last_emit_time = signal_time
        self.pending_signals.append({"time": signal_time, "signal": signal, "entry": price})
        confidence = min(95.0, 55.0 + abs(flow) * 20.0 + min(20.0, abs(ob_imb) * 20.0))
        return {
            **base,
            "signal": signal,
            "reason": reason,
            "confidence": round(float(confidence), 1),
            "high_conf": True,
            "prev_high": round(prev_high, 4),
            "prev_low": round(prev_low, 4),
            "min_edge_bps": self.min_edge_bps,
            "max_against_ob_imbalance": self.max_against_ob_imbalance,
        }


class SecondNormalLiquidityOrderbookV1Strategy(SecondNormalStrategy):
    """Rolling normal reclaim strategy confirmed by passive order-book liquidity."""

    def __init__(self, strategy_id, cfg):
        super().__init__(strategy_id, cfg)
        self.normal_window_sec = int(cfg.get("second_liq_normal_window_sec", 600))
        self.horizon_sec = int(cfg.get("second_liq_horizon_sec", cfg.get("second_horizon_sec", 600)))
        self.min_gap_sec = int(cfg.get("second_liq_signal_gap_sec", cfg.get("second_min_gap_sec", self.horizon_sec)))
        self.z_entry = float(cfg.get("second_liq_z_entry", 1.2))
        self.z_reclaim = float(cfg.get("second_liq_z_reclaim", 0.85))
        self.retest_sec = int(cfg.get("second_liq_retest_sec", 120))
        self.inside_min = float(cfg.get("second_liq_inside_min", 0.55))
        self.observed_min_pct = float(cfg.get("second_liq_observed_min_pct", 88.0))
        self.center_slope_sec = int(cfg.get("second_liq_center_slope_sec", 300))
        self.center_slope_max_bps = float(cfg.get("second_liq_center_slope_max_bps", 8.0))
        self.sigma_min_bps = float(cfg.get("second_liq_sigma_min_bps", 5.8))
        self.sigma_max_bps = float(cfg.get("second_liq_sigma_max_bps", 55.0))
        self.sigma_expand_max = float(cfg.get("second_liq_sigma_expand_max", 1.9))
        self.orderbook_max_age_sec = int(cfg.get("second_liq_orderbook_max_age_sec", 3))
        self.ob_imbalance_min = float(cfg.get("second_liq_ob_imbalance_min", 0.08))
        self.micro_min_bps = float(cfg.get("second_liq_micro_min_bps", 0.001))
        self.wall_ratio_min = float(cfg.get("second_liq_wall_ratio_min", 1.0))
        self.flow_guard = float(cfg.get("second_liq_flow_guard", 0.12))
        self.true_break_flow = float(cfg.get("second_liq_true_break_flow", 0.28))
        self.true_break_imbalance = float(cfg.get("second_liq_true_break_imbalance", 0.28))
        self.bidwall_trap_enabled = bool(cfg.get("second_liq_bidwall_trap_enabled", True))
        self.bidwall_trap_ret300_max_bps = float(cfg.get("second_liq_bidwall_trap_ret300_max_bps", -5.0))
        self.bidwall_trap_bid20_chg60_min = float(cfg.get("second_liq_bidwall_trap_bid20_chg60_min", 2.0))
        self.quality_v2_enabled = bool(cfg.get("second_liq_quality_v2_enabled", True))
        self.quality_v2_down_bid20_chg60_min = float(cfg.get("second_liq_quality_v2_down_bid20_chg60_min", -0.7))
        self.quality_v2_up_flow60_min = float(cfg.get("second_liq_quality_v2_up_flow60_min", -0.063))
        self.mode = str(cfg.get("second_liq_mode", "reclaim")).lower()
        self.trade_enabled = bool(cfg.get("trade_enabled", False))
        self.interval_min = max(1, int(round(self.horizon_sec / 60)))
        self.model_label = str(cfg.get("model_label", "Orderbook V2 quality"))
        self.last_emit_time = None

    @staticmethod
    def _rolling_sum(series, window, min_periods=None):
        return series.rolling(int(window), min_periods=min_periods or max(30, int(window) // 3)).sum()

    @staticmethod
    def _safe_float(row, key, default=float("nan")):
        try:
            return float(row.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    def _load_orderbook(self, second_index):
        if len(second_index) == 0:
            return None
        try:
            limit = max(
                len(second_index) + self.orderbook_max_age_sec + 30,
                self.normal_window_sec + self.center_slope_sec + self.retest_sec + 120,
                1200,
            )
            rows = csv_tail_rows(ORDERBOOK_FILE, limit=limit, chunk_size=ORDERBOOK_FEATURE_TAIL_CHUNK)
        except Exception as exc:
            print(f"[Signal] liquidity orderbook load failed: {exc}")
            return None
        if not rows:
            return None
        df = pd.DataFrame(rows)
        if "timestamp" not in df.columns:
            return None
        ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce").dt.floor("s")
        valid = ts.notna()
        df = df.loc[valid].reset_index(drop=True)
        ts = ts.loc[valid].reset_index(drop=True)
        if df.empty:
            return None
        cols = [
            "mid",
            "spread_bps",
            "bid_qty_20",
            "ask_qty_20",
            "imbalance_5",
            "imbalance_20",
            "microprice_edge_bps",
            "bid_wall_qty",
            "ask_wall_qty",
        ]
        ob = pd.DataFrame(index=ts.to_numpy())
        for col in cols:
            ob[col] = pd.to_numeric(df[col], errors="coerce").to_numpy(float) if col in df.columns else np.nan
        ob["ob_ts_ms"] = (ts.astype("int64") // 1_000_000).to_numpy()
        ob = ob[~ob.index.duplicated(keep="last")].sort_index()
        aligned = ob.reindex(second_index, method="ffill", limit=max(1, int(self.orderbook_max_age_sec)))
        latest_ts_ms = aligned["ob_ts_ms"]
        target_ms = pd.Series(second_index.astype("int64") // 1_000_000, index=second_index)
        aligned["ob_age_sec"] = (target_ms - latest_ts_ms) / 1000.0
        aligned["ob_available"] = (
            aligned["mid"].notna()
            & aligned["ob_age_sec"].notna()
            & (aligned["ob_age_sec"] <= float(self.orderbook_max_age_sec))
        )
        return aligned

    def _build_features(self, data):
        close = data["close"].astype(float)
        volume = data["volume"].astype(float).clip(lower=0.0)
        observed = data["observed"].astype(bool).astype(float) if "observed" in data else pd.Series(1.0, index=data.index)
        window = int(self.normal_window_sec)

        sw = self._rolling_sum(volume, window)
        sx = self._rolling_sum(close * volume, window)
        sx2 = self._rolling_sum(close * close * volume, window)
        mean = close.rolling(window, min_periods=max(120, window // 3)).mean()
        std = close.rolling(window, min_periods=max(120, window // 3)).std(ddof=1)
        vwap = sx / sw.replace(0, np.nan)
        var = sx2 / sw.replace(0, np.nan) - vwap * vwap
        vw_sigma = np.sqrt(var.clip(lower=0.0))
        center = vwap.fillna(mean)
        sigma = vw_sigma.where(vw_sigma > 1e-9, std)
        z = (close - center) / sigma.replace(0, np.nan)

        inside1 = z.abs().le(1.0).astype(float)
        sigma_bps = sigma / close * 10000.0
        sigma_median = sigma_bps.rolling(max(window, 900), min_periods=max(120, window // 3)).median()
        buy_60 = data["buy_qty"].astype(float).rolling(60, min_periods=10).sum()
        sell_60 = data["sell_qty"].astype(float).rolling(60, min_periods=10).sum()
        flow_60 = (buy_60 - sell_60) / (buy_60 + sell_60).replace(0, np.nan)
        bid20 = data["bid_qty_20"].astype(float)
        ask20 = data["ask_qty_20"].astype(float)
        bid_wall = data["bid_wall_qty"].astype(float)
        ask_wall = data["ask_wall_qty"].astype(float)

        out = pd.DataFrame(index=data.index)
        out["close"] = close
        out["center"] = center
        out["sigma"] = sigma
        out["z"] = z
        out["normal_low"] = center - sigma
        out["normal_high"] = center + sigma
        out["inside1_ratio"] = inside1.rolling(window, min_periods=max(120, window // 3)).mean()
        out["observed_pct"] = observed.rolling(min(600, window), min_periods=120).mean() * 100.0
        out["center_slope_bps"] = (center / center.shift(self.center_slope_sec) - 1.0) * 10000.0
        out["sigma_bps"] = sigma_bps
        out["sigma_expand"] = sigma_bps / sigma_median.replace(0, np.nan)
        out["flow_60"] = flow_60
        out["slope_30_bps"] = (close / close.shift(30) - 1.0) * 10000.0
        out["slope_90_bps"] = (close / close.shift(90) - 1.0) * 10000.0
        out["ret_300s_bps"] = np.log(close / close.shift(300)) * 10000.0
        out["imbalance_5"] = data["imbalance_5"].astype(float)
        out["imbalance_20"] = data["imbalance_20"].astype(float)
        out["micro_bps"] = data["microprice_edge_bps"].astype(float)
        out["spread_bps"] = data["spread_bps"].astype(float)
        out["bid_qty_20"] = bid20
        out["ask_qty_20"] = ask20
        out["bid20_chg_30"] = bid20 / bid20.shift(30).replace(0, np.nan) - 1.0
        out["bid20_chg_60"] = bid20 / bid20.shift(60).replace(0, np.nan) - 1.0
        out["ask20_chg_30"] = ask20 / ask20.shift(30).replace(0, np.nan) - 1.0
        out["wall_balance"] = (bid_wall - ask_wall) / (bid_wall + ask_wall).replace(0, np.nan)
        out["z_max_retest"] = z.rolling(self.retest_sec, min_periods=10).max()
        out["z_min_retest"] = z.rolling(self.retest_sec, min_periods=10).min()
        out["ob_available"] = data["ob_available"].astype(bool)
        out["ob_age_sec"] = data["ob_age_sec"].astype(float)
        return out

    def _normal_ready(self, row):
        checks = [
            row.get("z"),
            row.get("inside1_ratio"),
            row.get("observed_pct"),
            row.get("center_slope_bps"),
            row.get("sigma_bps"),
            row.get("sigma_expand"),
        ]
        if any(not np.isfinite(float(x)) for x in checks):
            return False
        return (
            float(row["inside1_ratio"]) >= self.inside_min
            and float(row["observed_pct"]) >= self.observed_min_pct
            and abs(float(row["center_slope_bps"])) <= self.center_slope_max_bps
            and self.sigma_min_bps <= float(row["sigma_bps"]) <= self.sigma_max_bps
            and float(row["sigma_expand"]) <= self.sigma_expand_max
        )

    def _passive_resistance(self, row):
        ask = self._safe_float(row, "ask_qty_20")
        bid = self._safe_float(row, "bid_qty_20")
        return (
            np.isfinite(ask)
            and np.isfinite(bid)
            and float(row["imbalance_20"]) <= -self.ob_imbalance_min
            and float(row["micro_bps"]) <= -self.micro_min_bps
            and ask >= max(1e-9, bid * self.wall_ratio_min)
            and self._safe_float(row, "ask20_chg_30", 0.0) > -0.55
        )

    def _passive_support(self, row):
        ask = self._safe_float(row, "ask_qty_20")
        bid = self._safe_float(row, "bid_qty_20")
        return (
            np.isfinite(ask)
            and np.isfinite(bid)
            and float(row["imbalance_20"]) >= self.ob_imbalance_min
            and float(row["micro_bps"]) >= self.micro_min_bps
            and bid >= max(1e-9, ask * self.wall_ratio_min)
            and self._safe_float(row, "bid20_chg_30", 0.0) > -0.55
        )

    def _true_break_up(self, row):
        return (
            float(row["flow_60"]) >= self.true_break_flow
            or float(row["imbalance_20"]) >= self.true_break_imbalance
            or float(row["micro_bps"]) >= self.micro_min_bps * 4.0
        )

    def _true_break_down(self, row):
        return (
            float(row["flow_60"]) <= -self.true_break_flow
            or float(row["imbalance_20"]) <= -self.true_break_imbalance
            or float(row["micro_bps"]) <= -self.micro_min_bps * 4.0
        )

    def _signal_from_row(self, row):
        z = float(row["z"])
        flow = float(row["flow_60"])
        resistance = self._passive_resistance(row)
        support = self._passive_support(row)
        true_up = self._true_break_up(row)
        true_down = self._true_break_down(row)

        edge_down = z >= self.z_entry and resistance and flow <= self.flow_guard and not true_up
        edge_up = z <= -self.z_entry and support and flow >= -self.flow_guard and not true_down
        reclaim_down = (
            float(row["z_max_retest"]) >= self.z_entry
            and 0.0 <= z <= self.z_reclaim
            and resistance
            and flow <= self.flow_guard
            and not true_up
        )
        reclaim_up = (
            float(row["z_min_retest"]) <= -self.z_entry
            and -self.z_reclaim <= z <= 0.0
            and support
            and flow >= -self.flow_guard
            and not true_down
        )
        if self.mode in ("edge", "hybrid") and edge_down:
            return "DOWN", "upper_passive_resistance_fade"
        if self.mode in ("edge", "hybrid") and edge_up:
            return "UP", "lower_passive_support_fade"
        if self.mode in ("reclaim", "hybrid") and reclaim_down:
            return "DOWN", "upper_fake_break_reclaim"
        if self.mode in ("reclaim", "hybrid") and reclaim_up:
            return "UP", "lower_fake_break_reclaim"
        return None, None

    def _is_bidwall_trap(self, signal, reason, row):
        if not self.bidwall_trap_enabled:
            return False
        if signal != "UP" or reason != "lower_fake_break_reclaim":
            return False
        ret300 = self._safe_float(row, "ret_300s_bps")
        bid20_chg60 = self._safe_float(row, "bid20_chg_60")
        return (
            np.isfinite(ret300)
            and np.isfinite(bid20_chg60)
            and ret300 <= self.bidwall_trap_ret300_max_bps
            and bid20_chg60 > self.bidwall_trap_bid20_chg60_min
        )

    def _quality_v2_veto(self, signal, row):
        if not self.quality_v2_enabled or not signal:
            return None
        if signal == "DOWN":
            bid20_chg60 = self._safe_float(row, "bid20_chg_60")
            if np.isfinite(bid20_chg60) and bid20_chg60 <= self.quality_v2_down_bid20_chg60_min:
                return (
                    "liq_v2_skip_down_bid_fade",
                    f"V2 skip DOWN: bid20_60s_chg={bid20_chg60:.3f} <= {self.quality_v2_down_bid20_chg60_min:g}",
                )
        if signal == "UP":
            flow60 = self._safe_float(row, "flow_60")
            if np.isfinite(flow60) and flow60 <= self.quality_v2_up_flow60_min:
                return (
                    "liq_v2_skip_up_negative_flow",
                    f"V2 skip UP: flow_60={flow60:.3f} <= {self.quality_v2_up_flow60_min:g}",
                )
        return None

    def predict(self, df5=None):
        bars = self._load_seconds()
        warmup = max(self.normal_window_sec, self.center_slope_sec, self.retest_sec, 900) + 10
        if bars is None or len(bars) < warmup:
            return None
        recent = bars.tail(warmup + 120).copy()
        recent["time"] = pd.to_datetime(recent["time"], utc=True, errors="coerce").dt.floor("s")
        recent = recent.dropna(subset=["time"]).drop_duplicates("time", keep="last").sort_values("time")
        if len(recent) < warmup:
            return None
        indexed = recent.set_index("time").sort_index()
        orderbook = self._load_orderbook(indexed.index)
        signal_time = indexed.index[-1]
        price = float(indexed["close"].iloc[-1])
        next_check_at = pd.Timestamp.now(tz="UTC") + pd.Timedelta(seconds=5)
        base = {
            "strategy_id": self.id,
            "model_type": "second_normal_liquidity_orderbook_v1",
            "model_label": self.model_label,
            "interval_min": self.interval_min,
            "duration": self.interval_min,
            "horizon_sec": self.horizon_sec,
            "min_gap_sec": self.min_gap_sec,
            "normal_window_sec": self.normal_window_sec,
            "retest_sec": self.retest_sec,
            "z_entry": self.z_entry,
            "z_reclaim": self.z_reclaim,
            "liquidity_mode": self.mode,
            "time": signal_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "price": round(price, 4),
            "entry": round(price, 4),
            "scan_interval_sec": 5,
            "next_check_time": next_check_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "next_check_time_shanghai": next_check_at.tz_convert("Asia/Shanghai").strftime("%Y/%m/%d %H:%M:%S"),
            "next_signal_estimate": "影子策略每5秒扫描；只在价格假突破1.2σ后回到正态区间，并且订单薄仍有被动支撑/压力时记录10分钟影子单。",
            "bypass_entry_timing": True,
            "shadow_only": not self.trade_enabled,
            "trade_enabled": self.trade_enabled,
            "rsi_value": None,
            "agree": True,
            "vol_ok": True,
            "session_gate_ok": True,
            "rsi_extreme": True,
            "condition_summary": {
                "entry": "600秒滚动正态区间 + 假突破回归 + 订单薄被动支撑/压力确认",
                "risk": "订单薄延迟<=3秒，秒级覆盖>=88%，inside>=55%，sigma=5-55bp，扩张<=1.9",
                "loss_density": "V2 uses orderbook quality vetoes; live trading is controlled by the strategy switches.",
            },
        }
        base["next_signal_estimate"] = (
            "V2每5秒扫描；价格假突破1.2σ后回到正态区间，"
            "且订单薄有被动支撑/压力时触发10分钟实盘信号。"
        )
        base["condition_summary"] = {
            "entry": "600秒滚动正态区间 + 假突破回归 + 订单薄被动支撑/压力确认",
            "risk": "订单薄延迟<=3秒，秒级覆盖>=88%，inside>=55%，sigma=5-55bp，扩张<=1.9",
            "bidwall_trap": "下沿回收做多时，如果5分钟仍下跌且60秒买盘深度突然放大>2倍，改判为DOWN",
            "live": "当前版本可实盘；是否下单仍受全局自动交易、实盘开关和执行失败保护控制",
        }
        if orderbook is None:
            return {
                **base,
                "signal": None,
                "reason": "liq_orderbook_missing",
                "signal_detail": "订单薄文件暂不可用，V2不降级交易。",
            }

        data = indexed.join(orderbook, how="left")
        latest_ob_available = bool(data["ob_available"].iloc[-1]) if "ob_available" in data else False
        if not latest_ob_available:
            return {
                **base,
                "signal": None,
                "reason": "liq_orderbook_missing_or_stale",
                "ob_age_sec": None if "ob_age_sec" not in data else round(float(data["ob_age_sec"].iloc[-1]), 3) if np.isfinite(data["ob_age_sec"].iloc[-1]) else None,
                "signal_detail": f"订单薄缺失或超过{self.orderbook_max_age_sec}秒，V2跳过。",
            }

        try:
            features = self._build_features(data)
            row = features.iloc[-1]
        except Exception as exc:
            return {
                **base,
                "signal": None,
                "reason": "liq_feature_error",
                "signal_detail": f"V2特征计算异常：{str(exc)[:160]}",
            }

        feature_extra = {
            "z_score": None if not np.isfinite(row.get("z")) else round(float(row["z"]), 4),
            "normal_low": None if not np.isfinite(row.get("normal_low")) else round(float(row["normal_low"]), 4),
            "normal_high": None if not np.isfinite(row.get("normal_high")) else round(float(row["normal_high"]), 4),
            "normal_center": None if not np.isfinite(row.get("center")) else round(float(row["center"]), 4),
            "inside1_ratio": None if not np.isfinite(row.get("inside1_ratio")) else round(float(row["inside1_ratio"]), 4),
            "observed_pct": None if not np.isfinite(row.get("observed_pct")) else round(float(row["observed_pct"]), 4),
            "center_slope_bps": None if not np.isfinite(row.get("center_slope_bps")) else round(float(row["center_slope_bps"]), 4),
            "sigma_bps": None if not np.isfinite(row.get("sigma_bps")) else round(float(row["sigma_bps"]), 4),
            "sigma_expand": None if not np.isfinite(row.get("sigma_expand")) else round(float(row["sigma_expand"]), 4),
            "flow_60": None if not np.isfinite(row.get("flow_60")) else round(float(row["flow_60"]), 6),
            "imbalance_20": None if not np.isfinite(row.get("imbalance_20")) else round(float(row["imbalance_20"]), 6),
            "micro_bps": None if not np.isfinite(row.get("micro_bps")) else round(float(row["micro_bps"]), 6),
            "bid_qty_20": None if not np.isfinite(row.get("bid_qty_20")) else round(float(row["bid_qty_20"]), 6),
            "ask_qty_20": None if not np.isfinite(row.get("ask_qty_20")) else round(float(row["ask_qty_20"]), 6),
            "ret_300s_bps": None if not np.isfinite(row.get("ret_300s_bps")) else round(float(row["ret_300s_bps"]), 4),
            "bid20_chg_60": None if not np.isfinite(row.get("bid20_chg_60")) else round(float(row["bid20_chg_60"]), 6),
            "ob_age_sec": None if not np.isfinite(row.get("ob_age_sec")) else round(float(row["ob_age_sec"]), 3),
            "z_max_retest": None if not np.isfinite(row.get("z_max_retest")) else round(float(row["z_max_retest"]), 4),
            "z_min_retest": None if not np.isfinite(row.get("z_min_retest")) else round(float(row["z_min_retest"]), 4),
        }
        if not self._normal_ready(row):
            return {
                **base,
                **feature_extra,
                "signal": None,
                "reason": "liq_normal_not_ready",
                "signal_detail": (
                    "等待重新进入短周期正态震荡：需要覆盖、inside比例、中心斜率、sigma范围和sigma扩张同时达标。"
                ),
            }

        if self.last_emit_time is not None and (signal_time - self.last_emit_time).total_seconds() < self.min_gap_sec:
            return {
                **base,
                **feature_extra,
                "signal": None,
                "reason": "liq_strategy_gap",
                "signal_detail": "V2上一个信号后处于10分钟同策略间隔，避免同一区域重复记录。",
            }

        signal, reason = self._signal_from_row(row)
        raw_signal = signal
        raw_reason = reason
        bidwall_trap = self._is_bidwall_trap(signal, reason, row)
        if bidwall_trap:
            signal = "DOWN"
            reason = "lower_reclaim_bidwall_trap_flip_down"
        if not signal:
            return {
                **base,
                **feature_extra,
                "signal": None,
                "reason": "liq_wait_reclaim",
                "signal_detail": "正态状态已满足，等待1.2σ假突破后回归，并等待订单薄被动支撑/压力确认。",
            }

        quality_v2_veto = self._quality_v2_veto(signal, row)
        if quality_v2_veto:
            veto_reason, veto_detail = quality_v2_veto
            self.last_emit_time = signal_time
            return {
                **base,
                **feature_extra,
                "signal": None,
                "raw_signal": raw_signal,
                "raw_reason": raw_reason,
                "bidwall_trap": bool(bidwall_trap),
                "bidwall_trap_rule": (
                    f"ret_300s_bps<={self.bidwall_trap_ret300_max_bps:g} "
                    f"and bid20_chg_60>{self.bidwall_trap_bid20_chg60_min:g}"
                ) if bidwall_trap else None,
                "quality_v2_veto": True,
                "quality_v2_rule": veto_detail,
                "reason": veto_reason,
                "blocked_signal": signal,
                "signal_detail": veto_detail,
            }

        self.last_emit_time = signal_time
        ob_strength = abs(float(row["imbalance_20"])) + min(1.0, abs(float(row["micro_bps"])) * 500.0)
        confidence = min(95.0, 58.0 + ob_strength * 12.0 + max(0.0, float(row["inside1_ratio"]) - self.inside_min) * 20.0)
        trap_detail = None
        if bidwall_trap:
            trap_detail = (
                "raw UP lower_fake_break_reclaim -> DOWN; "
                f"ret300={float(row['ret_300s_bps']):.2f}bp, "
                f"bid20_60s_chg={float(row['bid20_chg_60']):.2f}"
            )
        return {
            **base,
            **feature_extra,
            "signal": signal,
            "raw_signal": raw_signal,
            "raw_reason": raw_reason,
            "bidwall_trap": bool(bidwall_trap),
            "bidwall_trap_rule": (
                f"ret_300s_bps<={self.bidwall_trap_ret300_max_bps:g} "
                f"and bid20_chg_60>{self.bidwall_trap_bid20_chg60_min:g}"
            ) if bidwall_trap else None,
            "bidwall_trap_detail": trap_detail,
            "quality_v2_veto": False,
            "quality_v2_rule": None,
            "reason": reason,
            "confidence": round(float(confidence), 1),
            "high_conf": True,
            "signal_detail": (
                f"V2信号：{reason}，当前z={float(row['z']):.2f}，"
                f"订单薄imb20={float(row['imbalance_20']):.3f}，flow60={float(row['flow_60']):.3f}。"
            ),
        }


class SecondTrendPullbackDownStrategy(SecondNormalStrategy):
    """Trade downtrend acceptance by selling the short pullback."""

    def __init__(self, strategy_id, cfg):
        super().__init__(strategy_id, cfg)
        self.regime_lookback_sec = int(cfg.get("second_trend_regime_lookback_sec", 7200))
        self.regime_alt_lookback_sec = int(cfg.get("second_trend_regime_alt_lookback_sec", 5400))
        self.regime_drop_pct = float(cfg.get("second_trend_regime_drop_pct", 0.004))
        self.regime_alt_drop_pct = float(cfg.get("second_trend_regime_alt_drop_pct", 0.003))
        self.max_pos_pct = float(cfg.get("second_trend_max_pos_pct", 0.6))
        self.max_entry_pos_pct = float(cfg.get("second_trend_max_entry_pos_pct", 0.4))
        self.max_recent_ret_pct = float(cfg.get("second_trend_max_recent_ret_pct", 0.001))
        self.pullback_sec = int(cfg.get("second_trend_pullback_sec", 300))
        self.pullback_pct = float(cfg.get("second_trend_pullback_pct", 0.001))
        self.horizon_sec = int(cfg.get("second_trend_horizon_sec", cfg.get("second_horizon_sec", 600)))
        self.min_gap_sec = int(cfg.get("second_trend_min_gap_sec", cfg.get("second_trend_signal_gap_sec", 600)))
        self.suppress_reversal = bool(cfg.get("second_trend_suppress_reversal", True))
        self.interval_min = max(1, int(round(self.horizon_sec / 60)))

    def predict(self, df5=None):
        bars = self._load_seconds()
        required = max(self.regime_lookback_sec, self.regime_alt_lookback_sec, self.pullback_sec, 1800) + 2
        if bars is None or len(bars) < required:
            return None
        recent = bars.tail(required).copy()
        close = recent["close"].astype(float).values
        price = float(close[-1])
        regime_base = float(close[-1 - self.regime_lookback_sec])
        alt_regime_base = float(close[-1 - self.regime_alt_lookback_sec])
        recent_base = float(close[-1 - 1800])
        pullback_base = float(close[-1 - self.pullback_sec])
        regime_ret = price / max(regime_base, 1e-12) - 1.0
        alt_regime_ret = price / max(alt_regime_base, 1e-12) - 1.0
        recent_ret = price / max(recent_base, 1e-12) - 1.0
        pullback_ret = price / max(pullback_base, 1e-12) - 1.0
        regime_window = close[-self.regime_lookback_sec:]
        roll_min = float(np.min(regime_window))
        roll_max = float(np.max(regime_window))
        roll_mean = float(np.mean(regime_window))
        pos = (price - roll_min) / max(roll_max - roll_min, 1e-12)
        mean_gap = price / max(roll_mean, 1e-12) - 1.0
        regime_active = (
            (regime_ret <= -self.regime_drop_pct or alt_regime_ret <= -self.regime_alt_drop_pct)
            and pos < self.max_pos_pct
            and recent_ret <= self.max_recent_ret_pct
            and mean_gap <= 0
        )
        pullback_active = pullback_ret >= self.pullback_pct and pos < self.max_entry_pos_pct
        signal = "DOWN" if regime_active and pullback_active else None
        confidence = min(95.0, max(0.0, abs(regime_ret) / max(self.regime_drop_pct, 1e-12) * 35.0))
        if pullback_active:
            confidence = min(95.0, confidence + min(35.0, pullback_ret / max(self.pullback_pct, 1e-12) * 20.0))
        signal_time = bars["time"].iloc[-1]
        base = {
            "strategy_id": self.id,
            "confidence": round(float(confidence), 1),
            "avg_prob": None,
            "rsi_value": None,
            "high_conf": bool(signal),
            "agree": True,
            "vol_ok": True,
            "session_gate_ok": True,
            "rsi_extreme": True,
            "regime_lookback_sec": self.regime_lookback_sec,
            "regime_alt_lookback_sec": self.regime_alt_lookback_sec,
            "regime_drop_pct": round(self.regime_drop_pct, 6),
            "regime_alt_drop_pct": round(self.regime_alt_drop_pct, 6),
            "regime_ret": round(float(regime_ret), 6),
            "alt_regime_ret": round(float(alt_regime_ret), 6),
            "recent_ret": round(float(recent_ret), 6),
            "pos_regime": round(float(pos), 6),
            "mean_gap_regime": round(float(mean_gap), 6),
            "max_pos_pct": round(self.max_pos_pct, 6),
            "max_entry_pos_pct": round(self.max_entry_pos_pct, 6),
            "max_recent_ret_pct": round(self.max_recent_ret_pct, 6),
            "pullback_sec": self.pullback_sec,
            "pullback_pct": round(self.pullback_pct, 6),
            "pullback_ret": round(float(pullback_ret), 6),
            "trend_regime_active": bool(regime_active),
            "trend_pullback_active": bool(pullback_active),
            "suppress_reversal_in_regime": bool(self.suppress_reversal),
            "horizon_sec": self.horizon_sec,
            "interval_min": self.interval_min,
            "duration": self.interval_min,
            "min_gap_sec": self.min_gap_sec,
            "time": signal_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model_type": "second_trend_pullback_down",
            "bypass_entry_timing": True,
        }
        if not regime_active:
            return {**base, "signal": None, "reason": "no_downtrend_acceptance"}
        if not pullback_active:
            return {**base, "signal": None, "reason": "waiting_pullback"}
        return {**base, "signal": signal, "reason": "trend_down_pullback_continuation"}


class Strategy:
    def __init__(self, strategy_id, cfg):
        self.id = strategy_id
        self.cfg = cfg
        self.horizon = int(cfg["horizon"])
        self.interval_min = int(cfg.get("interval_min", self.horizon * 5))
        self.threshold = float(cfg["threshold"])
        self.rsi_lo = float(cfg.get("rsi_lo", 30))
        self.rsi_hi = float(cfg.get("rsi_hi", 70))
        self.vol_min_rank = cfg.get("vol_min_rank")
        self.vol_min_rank = None if self.vol_min_rank is None else float(self.vol_min_rank)
        self.agree_mode = cfg.get("agree_mode", "all3")
        self.skip_hours_utc = sorted({int(h) for h in cfg.get("skip_hours_utc", [])})
        self.session_filter_mode = cfg.get("session_filter_mode", "hard")
        self.session_confidence_bump = float(cfg.get("session_confidence_bump", 8))
        self.session_min_market_score = int(cfg.get("session_min_market_score", 2))
        self.session_block_strong_countertrend = bool(cfg.get("session_block_strong_countertrend", True))
        self.fixed_amount = cfg.get("fixed_amount")
        self.model_label = model_label_for(strategy_id, cfg)
        self.countertrend_max_abs_trend6 = cfg.get("countertrend_max_abs_trend6")
        self.countertrend_max_abs_trend6 = (
            None if self.countertrend_max_abs_trend6 is None else float(self.countertrend_max_abs_trend6)
        )
        self.countertrend_max_strength = cfg.get("countertrend_max_strength")
        self.countertrend_max_strength = (
            None if self.countertrend_max_strength is None else float(self.countertrend_max_strength)
        )
        self.bbp_cap = cfg.get("bbp_cap")
        self.bbp_cap = None if self.bbp_cap is None else float(self.bbp_cap)
        self.rsi_extreme_cap = cfg.get("rsi_extreme_cap")
        self.rsi_extreme_cap = None if self.rsi_extreme_cap is None else float(self.rsi_extreme_cap)
        self.confidence_max = cfg.get("confidence_max")
        self.confidence_max = None if self.confidence_max is None else float(self.confidence_max)
        self.xgb_models = []
        for i in range(2):
            m = XGBClassifier()
            m.load_model(os.path.join(OUT, f"prod_{self.model_label}_m{i + 1}.json"))
            self.xgb_models.append(m)
        with open(os.path.join(OUT, f"prod_{self.model_label}_lgb.pkl"), "rb") as f:
            self.lgb_model = pickle.load(f)
        with open(os.path.join(OUT, f"prod_{self.model_label}_cols.json"), "r", encoding="utf-8") as f:
            self.feat_cols = json.load(f)
        print(
            f"[Signal] {self.id} -> {self.model_label} | horizon={self.horizon} "
            f"| th={self.threshold} | RSI<{self.rsi_lo}/{self.rsi_hi}> "
            f"| vol_min_rank={self.vol_min_rank if self.vol_min_rank is not None else 'none'} "
            f"| agree={self.agree_mode} "
            f"| ctcool_t6={self.countertrend_max_abs_trend6 if self.countertrend_max_abs_trend6 is not None else 'none'} "
            f"| ctcool_strength={self.countertrend_max_strength if self.countertrend_max_strength is not None else 'none'} "
            f"| bbp_cap={self.bbp_cap if self.bbp_cap is not None else 'none'} "
            f"| rsi_cap={self.rsi_extreme_cap if self.rsi_extreme_cap is not None else 'none'} "
            f"| conf_max={self.confidence_max if self.confidence_max is not None else 'none'} "
            f"| skip_hours_utc={self.skip_hours_utc or 'none'} "
            f"| session_mode={self.session_filter_mode} "
            f"| amount={self.fixed_amount or 'config'}"
        )

    def predict(self, df5):
        fdf = build_features(df5, self.horizon)
        if len(fdf) < 10:
            return None
        last = fdf.iloc[[-1]]
        missing = [c for c in self.feat_cols if c not in last.columns]
        if missing:
            raise RuntimeError(f"{self.id} missing features: {missing[:5]}")
        X = last[self.feat_cols].values
        probs = [float(m.predict_proba(X)[0, 1]) for m in self.xgb_models]
        probs.append(float(self.lgb_model.predict_proba(X)[0, 1]))
        avg = sum(probs) / len(probs)
        dirs = [p >= 0.5 for p in probs]
        agree_all = dirs[0] == dirs[1] == dirs[2]
        up_votes = sum(1 for d in dirs if d)
        majority_up = up_votes >= 2
        agree = agree_all if self.agree_mode == "all3" else True
        high_conf = avg >= self.threshold or avg <= (1 - self.threshold)
        rsi_val = float(X[0, self.feat_cols.index("rsi14")])
        rsi_extreme = rsi_val < self.rsi_lo or rsi_val > self.rsi_hi
        bbp_val = float(last.iloc[0].get("bbp", 0.5) or 0.5)
        bbw_val = float(last.iloc[0].get("bbw", 0) or 0)
        hlp20_val = float(last.iloc[0].get("hlp20", 0.5) or 0.5)
        hlp50_val = float(last.iloc[0].get("hlp50", 0.5) or 0.5)
        trend12_val = float(last.iloc[0].get("trend12", 0) or 0)
        trend30_val = float(last.iloc[0].get("trend30", 0) or 0)
        pre50_val = float(last.iloc[0].get("pre50", 0) or 0)
        ema_stack_val = float(last.iloc[0].get("ema_stack", 0) or 0)
        atrp = float(X[0, self.feat_cols.index("atrp")]) if "atrp" in self.feat_cols else None
        atr_exp_val = float(last.iloc[0].get("atr_exp", 0) or 0)
        vr_val = float(last.iloc[0].get("vr", 1) or 1)
        vol_rank = None
        vol_ok = True
        if self.vol_min_rank is not None and "atrp" in fdf.columns:
            recent = fdf["atrp"].dropna().iloc[-8000:]
            if len(recent) > 1 and atrp is not None:
                vol_rank = float((recent <= atrp).mean())
                vol_ok = vol_rank >= self.vol_min_rank

        candle_time = pd.to_datetime(df5["time"].iloc[-1], utc=True)
        candle_close_time = candle_time + pd.Timedelta(minutes=5)
        session_risk = candle_time.hour in self.skip_hours_utc
        session_hard_block = session_risk and self.session_filter_mode == "hard"
        session_ok = not session_hard_block
        trend_val = trend_score(last.iloc[0])
        htf_val = htf_score(last.iloc[0])
        taker_ratio_val = float(last.iloc[0].get("taker_ratio", 1) or 1)

        sig = None
        conf = None
        strength_val = round(abs(avg - 0.5) * 2 * 100, 1)
        base_strength_min = round(abs(self.threshold - 0.5) * 2 * 100, 1)
        session_gate_ok = True
        session_gate_reasons = []
        market_confirm = {
            "score": 0,
            "reasons": [],
            "short_align": 0,
            "htf_align": 0,
            "taker_align": 0,
        }
        if agree and high_conf and rsi_extreme and vol_ok and session_ok:
            if self.agree_mode == "majority":
                sig = "UP" if majority_up else "DOWN"
            else:
                sig = "UP" if avg >= 0.5 else "DOWN"
            conf = strength_val
            market_confirm = market_confirmation(sig, trend_val, htf_val, taker_ratio_val, atr_exp_val)
            if session_risk and self.session_filter_mode == "soft":
                if strength_val < base_strength_min + self.session_confidence_bump:
                    session_gate_ok = False
                    session_gate_reasons.append("session_strength_bump")
                if market_confirm["score"] < self.session_min_market_score:
                    session_gate_ok = False
                    session_gate_reasons.append("market_confirm_score")
                if (
                    self.session_block_strong_countertrend
                    and market_confirm["short_align"] <= -3
                    and market_confirm["htf_align"] <= 0
                ):
                    session_gate_ok = False
                    session_gate_reasons.append("strong_countertrend_in_risk_session")
            if session_risk and self.session_filter_mode == "hard":
                session_gate_ok = False
                session_gate_reasons.append("session_hard_block")
            if not session_gate_ok:
                sig = None
                conf = None
        countertrend_guard_ok = True
        regime_filter_ok = True
        regime_filter_reasons = []
        trend6_val = float(last.iloc[0].get("trend6", 0) or 0)
        if sig:
            countertrend = (sig == "UP" and trend_val <= -3) or (sig == "DOWN" and trend_val >= 3)
            if countertrend:
                if (
                    self.countertrend_max_abs_trend6 is not None
                    and abs(trend6_val) > self.countertrend_max_abs_trend6
                ):
                    countertrend_guard_ok = False
                if (
                    self.countertrend_max_strength is not None
                    and strength_val > self.countertrend_max_strength
                ):
                    countertrend_guard_ok = False
            if not countertrend_guard_ok:
                sig = None
                conf = None
            if sig and self.bbp_cap is not None:
                if (sig == "DOWN" and bbp_val > self.bbp_cap) or (sig == "UP" and bbp_val < 1 - self.bbp_cap):
                    regime_filter_ok = False
                    regime_filter_reasons.append("bbp_cap")
            if sig and self.rsi_extreme_cap is not None:
                if (sig == "DOWN" and rsi_val > self.rsi_extreme_cap) or (sig == "UP" and rsi_val < 100 - self.rsi_extreme_cap):
                    regime_filter_ok = False
                    regime_filter_reasons.append("rsi_extreme_cap")
            if sig and self.confidence_max is not None and strength_val >= self.confidence_max:
                regime_filter_ok = False
                regime_filter_reasons.append("confidence_max")
            if sig and not regime_filter_ok:
                sig = None
                conf = None

        result = {
            "strategy_id": self.id,
            "probs": [round(p, 4) for p in probs],
            "avg_prob": round(avg, 4),
            "agree": agree,
            "agree_mode": self.agree_mode,
            "agree_all": agree_all,
            "high_conf": high_conf,
            "rsi_extreme": rsi_extreme,
            "rsi_value": round(rsi_val, 1),
            "trend_score": trend_val,
            "trend_label": trend_label(trend_val),
            "htf_score": htf_val,
            "htf_label": htf_label(htf_val),
            "htf_ret_1h": round(float(last.iloc[0].get("htf_ret_1h", 0) or 0), 6),
            "htf_ret_4h": round(float(last.iloc[0].get("htf_ret_4h", 0) or 0), 6),
            "htf_ret_24h": round(float(last.iloc[0].get("htf_ret_24h", 0) or 0), 6),
            "htf_pos_1h": round(float(last.iloc[0].get("htf_pos_1h", 0.5) or 0.5), 4),
            "htf_pos_4h": round(float(last.iloc[0].get("htf_pos_4h", 0.5) or 0.5), 4),
            "htf_pos_24h": round(float(last.iloc[0].get("htf_pos_24h", 0.5) or 0.5), 4),
            "htf_rng_1h": round(float(last.iloc[0].get("htf_rng_1h", 0) or 0), 6),
            "htf_rng_4h": round(float(last.iloc[0].get("htf_rng_4h", 0) or 0), 6),
            "htf_rng_24h": round(float(last.iloc[0].get("htf_rng_24h", 0) or 0), 6),
            "trend6": round(trend6_val, 6),
            "trend12": round(trend12_val, 6),
            "trend30": round(trend30_val, 6),
            "pre50": round(pre50_val, 6),
            "ema_stack": round(ema_stack_val, 3),
            "bbp": round(bbp_val, 4),
            "bbw": round(bbw_val, 6),
            "atrp": None if atrp is None else round(float(atrp), 8),
            "atr_exp": round(atr_exp_val, 6),
            "vr": round(vr_val, 6),
            "taker_ratio": round(taker_ratio_val, 6),
            "ls_ratio": round(float(last.iloc[0].get("ls_ratio", 1) or 1), 6),
            "fund_rate": round(float(last.iloc[0].get("fund_rate", last.iloc[0].get("funding_rate", 0)) or 0), 8),
            "hlp20": round(hlp20_val, 4),
            "hlp50": round(hlp50_val, 4),
            "countertrend_guard_ok": countertrend_guard_ok,
            "countertrend_max_abs_trend6": self.countertrend_max_abs_trend6,
            "countertrend_max_strength": self.countertrend_max_strength,
            "regime_filter_ok": regime_filter_ok,
            "regime_filter_reasons": regime_filter_reasons,
            "bbp_cap": self.bbp_cap,
            "rsi_extreme_cap": self.rsi_extreme_cap,
            "confidence_max": self.confidence_max,
            "vol_ok": vol_ok,
            "vol_rank": None if vol_rank is None else round(vol_rank, 3),
            "vol_min_rank": self.vol_min_rank,
            "session_ok": session_ok,
            "session_risk": session_risk,
            "session_filter_mode": self.session_filter_mode,
            "session_gate_ok": session_gate_ok,
            "session_gate_reasons": session_gate_reasons,
            "session_confidence_bump": self.session_confidence_bump,
            "session_min_market_score": self.session_min_market_score,
            "skip_hours_utc": self.skip_hours_utc,
            "market_confirm_score": market_confirm["score"],
            "market_confirm_reasons": market_confirm["reasons"],
            "short_align": market_confirm["short_align"],
            "htf_align": market_confirm["htf_align"],
            "taker_align": market_confirm["taker_align"],
            "signal": sig,
            "confidence": conf,
            "interval_min": self.interval_min,
            "duration": str(self.interval_min),
            "price": round(float(df5["close"].iloc[-1]), 2),
            "time": str(candle_time),
            "candle_close_time": str(candle_close_time),
            "actionable_time": str(candle_close_time),
            "symbol": "BTCUSDT",
            "label": self.id,
            "model_label": self.model_label,
            "threshold": self.threshold,
        }
        if self.fixed_amount is not None:
            result["amount"] = str(self.fixed_amount)
            result["fixed_amount"] = True
        return result


def regime_group_2m(regime):
    return "transition" if str(regime).startswith("transition") else str(regime)


def enrich_live_2m_features(fdf, bars2):
    bars = bars2[["time", "open", "high", "low", "close", "volume"]].copy()
    bars["time"] = pd.to_datetime(bars["time"], utc=True)
    out = fdf.copy()
    out["time"] = pd.to_datetime(out["time"], utc=True)
    out = out.merge(bars, on="time", how="left")
    return out.dropna(subset=["close"]).reset_index(drop=True)


class TwoMinuteRegimeShadow:
    def __init__(self, meta):
        self.meta = meta
        self.id = meta["id"]
        self.base = meta["base"]
        self.model_id = meta["model_id"]
        self.live = bool(meta.get("live", False))
        self.model = None
        self.feat_cols = []
        self.policy = {}
        self.df1 = None
        self.cached_period = None
        self.cached_result = None
        prefix = os.path.join(OUT, f"prod_{self.model_id}")
        try:
            with open(f"{prefix}_hgb.pkl", "rb") as f:
                self.model = pickle.load(f)
            with open(f"{prefix}_cols.json", "r", encoding="utf-8") as f:
                self.feat_cols = json.load(f)
            with open(f"{prefix}_policy.json", "r", encoding="utf-8") as f:
                self.policy = json.load(f)
            self.df1 = load_2m_1m(RESEARCH_2M_SYMBOL)
            self.df1_mtime = file_mtime(HISTORY_1M_FILE)
            print(
                f"[Signal] {self.id} -> 2m {'LIVE' if self.live else 'shadow'} | model={self.model_id} "
                f"| features={len(self.feat_cols)} | policy={self.policy.get('name')}"
            )
        except Exception as e:
            print(f"[Signal] {self.id} disabled: {e}")

    def _reload_base_1m_if_changed(self):
        mtime = file_mtime(HISTORY_1M_FILE)
        if mtime is None or mtime == self.df1_mtime:
            return
        self.df1 = load_2m_1m(RESEARCH_2M_SYMBOL)
        self.df1_mtime = mtime
        self.cached_period = None
        self.cached_result = None
        print(f"[Signal] Reloaded 2m base 1m history after data update: {len(self.df1)} rows")

    def _merge_live_1m(self, live1m):
        if self.df1 is None:
            return
        self._reload_base_1m_if_changed()
        if live1m is None or len(live1m) == 0:
            return
        hist_last = pd.to_datetime(self.df1["open_time"].max(), utc=True)
        live_first = pd.to_datetime(live1m["open_time"].min(), utc=True)
        if hist_last + MAX_HISTORY_LIVE_GAP < live_first:
            print(
                f"[Signal] 2m live merge blocked: history/live gap "
                f"{(live_first - hist_last).total_seconds():.0f}s ({hist_last} -> {live_first})"
            )
            return
        merged = pd.concat([self.df1, live1m], ignore_index=True)
        merged["open_time"] = pd.to_datetime(merged["open_time"], utc=True)
        merged = merged.drop_duplicates("open_time", keep="last").sort_values("open_time").reset_index(drop=True)
        self.df1 = merged

    def _last_closed_2m_period(self):
        latest_1m = pd.to_datetime(self.df1["open_time"].max(), utc=True)
        return (latest_1m - pd.Timedelta(minutes=2)).floor("2min")

    def _build_live_frame(self):
        two = aggregate_2m_bars(self.df1)
        latest_1m = pd.to_datetime(self.df1["open_time"].max(), utc=True)
        two["time"] = pd.to_datetime(two["time"], utc=True)
        two["close_time"] = two["time"] + pd.Timedelta(minutes=2)
        two = two[two["close_time"] <= latest_1m].drop(columns=["close_time"]).reset_index(drop=True)
        two = merge_2m_external(two)
        fdf = build_2m_features(two, keep_unlabeled=True)
        frame = classify_2m_regime(enrich_live_2m_features(fdf, two))
        frame["regime_group"] = frame["regime"].map(regime_group_2m)
        return frame

    def _threshold_for_group(self, group):
        thresholds = self.policy.get("regime_thresholds") or {}
        return float(thresholds.get(group, thresholds.get("uncertain", 0.65)))

    def predict(self, live1m):
        if self.model is None or not self.feat_cols or self.df1 is None:
            return None
        self._merge_live_1m(live1m)
        period = self._last_closed_2m_period()
        if self.cached_period is not None and period == self.cached_period:
            return dict(self.cached_result) if self.cached_result else None

        frame = self._build_live_frame()
        if len(frame) < 10:
            return None
        last = frame.iloc[[-1]].copy()
        missing = [c for c in self.feat_cols if c not in last.columns]
        if missing:
            raise RuntimeError(f"{self.id} missing 2m features: {missing[:5]}")
        row = last.iloc[0]
        X = last[self.feat_cols].replace([np.inf, -np.inf], np.nan).fillna(0).to_numpy(dtype=np.float32)
        prob = float(self.model.predict_proba(X)[0, 1])
        group = regime_group_2m(row.get("regime_group", "uncertain"))
        threshold = self._threshold_for_group(group)

        direction = None
        margin = 0.0
        if prob >= threshold:
            direction = 1
            margin = prob - threshold
        elif prob <= 1 - threshold:
            direction = 0
            margin = (1 - prob) - threshold

        raw_signal = "UP" if direction == 1 else ("DOWN" if direction == 0 else None)
        sig = raw_signal
        filter_reasons = []
        taker_ratio_val = float(row.get("taker_ratio", 1) or 1)
        if sig and self.policy.get("block_flow_opposes", False):
            if (sig == "UP" and taker_ratio_val < 0.85) or (sig == "DOWN" and taker_ratio_val > 1.15):
                sig = None
                filter_reasons.append("flow_opposes")

        gate = self.policy.get("gate") or {}
        if sig and gate.get("kind") == "raise_margin":
            atr_rank = float(row.get("atr_rank", 0.5) or 0.5)
            bbw_rank = float(row.get("bbw_rank", 0.5) or 0.5)
            directions = set(gate.get("directions") or [])
            direction_ok = (sig == "UP" and "UP" in directions) or (sig == "DOWN" and "DOWN" in directions)
            lowvol = atr_rank <= float(gate.get("atr_max", 1.0)) and bbw_rank <= float(gate.get("bbw_max", 1.0))
            if direction_ok and lowvol and margin < float(gate.get("min_margin", 0)):
                sig = None
                filter_reasons.append("lowvol_strength_gate")

        candle_time = pd.to_datetime(row["time"], utc=True)
        candle_close_time = candle_time + pd.Timedelta(minutes=2)
        strength_val = round(abs(prob - 0.5) * 200, 1)
        trend_val = int(float(row.get("trend_score", 0) or 0))
        htf_val = int(float(row.get("htf_score", 0) or 0))
        rsi_val = float(row.get("rsi14", 50) or 50)
        result = {
            "strategy_id": self.id,
            "engine": "two_minute_regime_model",
            "shadow": not self.live,
            "shadow_type": "two_minute_regime_model",
            "shadow_base_strategy": self.base,
            "shadow_model_id": self.model_id,
            "live_model": self.live,
            "policy_name": self.policy.get("name"),
            "probs": [round(prob, 4)],
            "avg_prob": round(prob, 4),
            "policy_threshold": round(threshold, 4),
            "policy_margin": round(float(margin), 4),
            "agree": True,
            "agree_mode": "single_hgb_2m",
            "agree_all": True,
            "high_conf": raw_signal is not None,
            "rsi_extreme": True,
            "rsi_value": round(rsi_val, 1),
            "regime": str(row.get("regime", "unknown")),
            "regime_group": group,
            "trend_score": trend_val,
            "trend_label": trend_label(trend_val),
            "htf_score": htf_val,
            "htf_label": htf_label(htf_val),
            "bbp": round(float(row.get("bbp", 0.5) or 0.5), 4),
            "bbw": round(float(row.get("bbw", 0) or 0), 6),
            "bbw_rank": round(float(row.get("bbw_rank", 0.5) or 0.5), 4),
            "atrp": round(float(row.get("atrp", 0) or 0), 8),
            "atr_rank": round(float(row.get("atr_rank", 0.5) or 0.5), 4),
            "atr_exp": round(float(row.get("atr_exp", 0) or 0), 6),
            "vr": round(float(row.get("vr", 1) or 1), 6),
            "vr_rank": round(float(row.get("vr_rank", 0.5) or 0.5), 4),
            "taker_ratio": round(taker_ratio_val, 6),
            "ls_ratio": round(float(row.get("ls_ratio", 1) or 1), 6),
            "fund_rate": round(float(row.get("funding_rate", 0) or 0), 8),
            "regime_filter_ok": len(filter_reasons) == 0,
            "regime_filter_reasons": filter_reasons,
            "signal": sig,
            "raw_signal": raw_signal,
            "confidence": strength_val if sig else None,
            "bypass_min_confidence_filter": False,
            "bypass_entry_timing": self.live,
            "interval_min": int(self.policy.get("interval_min", 10)),
            "duration": str(int(self.policy.get("interval_min", 10))),
            "price": round(float(row.get("close", 0) or 0), 2),
            "time": str(candle_time),
            "candle_close_time": str(candle_close_time),
            "actionable_time": str(candle_close_time),
            "symbol": "BTCUSDT",
            "label": self.id,
            "model_label": self.model_id,
            "threshold": threshold,
            "amount": str(self.policy.get("fixed_amount", 5)),
            "fixed_amount": True,
        }
        self.cached_period = period
        self.cached_result = dict(result)
        return result


class RuleShadowStrategy:
    def __init__(self, meta, cfg):
        self.meta = meta
        self.id = meta["id"]
        self.base = meta["base"]
        self.kind = meta["kind"]
        self.horizon = int(cfg["horizon"])
        self.interval_min = int(cfg.get("interval_min", self.horizon * 5))
        self.skip_hours_utc = sorted({int(h) for h in cfg.get("skip_hours_utc", [])})
        self.rsi_lo = float(meta.get("rsi_lo", cfg.get("rsi_lo", 30)))
        self.rsi_hi = float(meta.get("rsi_hi", cfg.get("rsi_hi", 70)))
        self.score_min = int(meta.get("score_min", 3))
        self.trend_gate = meta.get("trend_gate", "none")
        print(
            f"[Signal] {self.id} -> rule {self.kind} | base={self.base} "
            f"| horizon={self.horizon} | RSI<{self.rsi_lo}/{self.rsi_hi}> "
            f"| score_min={self.score_min} | trend_gate={self.trend_gate} "
            f"| skip_hours_utc={self.skip_hours_utc or 'none'}"
        )

    def _rsi_reversal(self, rsi_val, score):
        sig = None
        if rsi_val < self.rsi_lo:
            sig = "UP"
        elif rsi_val > self.rsi_hi:
            sig = "DOWN"
        if not sig:
            return None
        if self.trend_gate == "no_strong_trend_score3" and abs(score) >= 3:
            return None
        if self.trend_gate == "skip_opposite_score3":
            if sig == "UP" and score <= -3:
                return None
            if sig == "DOWN" and score >= 3:
                return None
        return sig

    def _pullback_follow(self, row, rsi_val, score):
        bbp = float(row.get("bbp", 0.5) or 0.5)
        if score >= self.score_min and rsi_val <= 60 and bbp <= 0.65:
            return "UP"
        if score <= -self.score_min and rsi_val >= 40 and bbp >= 0.35:
            return "DOWN"
        return None

    def _hybrid_regime(self, row, rsi_val, score):
        if score >= self.score_min:
            return "UP"
        if score <= -self.score_min:
            return "DOWN"
        return self._rsi_reversal(rsi_val, score)

    def predict(self, df5):
        fdf = build_features(df5, self.horizon)
        if len(fdf) < 10:
            return None
        row = fdf.iloc[-1]
        candle_time = pd.to_datetime(df5["time"].iloc[-1], utc=True)
        candle_close_time = candle_time + pd.Timedelta(minutes=5)
        session_ok = candle_time.hour not in self.skip_hours_utc
        rsi_val = float(row.get("rsi14"))
        score = trend_score(row)
        htf_val = htf_score(row)
        sig = None
        if self.kind == "rsi_reversal":
            sig = self._rsi_reversal(rsi_val, score)
        elif self.kind == "pullback_follow":
            sig = self._pullback_follow(row, rsi_val, score)
        elif self.kind == "hybrid_regime":
            sig = self._hybrid_regime(row, rsi_val, score)
        else:
            raise RuntimeError(f"unknown rule shadow kind: {self.kind}")
        if not session_ok:
            sig = None

        rsi_extreme = rsi_val < self.rsi_lo or rsi_val > self.rsi_hi
        confidence = None
        if sig:
            if self.kind == "rsi_reversal":
                confidence = round(min(100.0, max(0.0, abs(rsi_val - 50) * 2)), 1)
            else:
                confidence = round(min(100.0, max(0.0, abs(score) / 5 * 100)), 1)

        return {
            "strategy_id": self.id,
            "shadow_rule": True,
            "shadow_type": "rule",
            "shadow_base_strategy": self.base,
            "rule_kind": self.kind,
            "avg_prob": None,
            "probs": [],
            "agree": True,
            "agree_mode": "rule",
            "agree_all": True,
            "high_conf": bool(sig),
            "rsi_extreme": rsi_extreme,
            "rsi_value": round(rsi_val, 1),
            "trend_score": score,
            "trend_label": trend_label(score),
            "htf_score": htf_val,
            "htf_label": htf_label(htf_val),
            "htf_ret_1h": round(float(row.get("htf_ret_1h", 0) or 0), 6),
            "htf_ret_4h": round(float(row.get("htf_ret_4h", 0) or 0), 6),
            "htf_ret_24h": round(float(row.get("htf_ret_24h", 0) or 0), 6),
            "htf_pos_1h": round(float(row.get("htf_pos_1h", 0.5) or 0.5), 4),
            "htf_pos_4h": round(float(row.get("htf_pos_4h", 0.5) or 0.5), 4),
            "htf_pos_24h": round(float(row.get("htf_pos_24h", 0.5) or 0.5), 4),
            "bbp": round(float(row.get("bbp", 0.5) or 0.5), 4),
            "session_ok": session_ok,
            "skip_hours_utc": self.skip_hours_utc,
            "signal": sig,
            "confidence": confidence,
            "interval_min": self.interval_min,
            "duration": str(self.interval_min),
            "price": round(float(df5["close"].iloc[-1]), 2),
            "time": str(candle_time),
            "candle_close_time": str(candle_close_time),
            "actionable_time": str(candle_close_time),
            "symbol": "BTCUSDT",
            "label": self.id,
            "model_label": "rule",
            "threshold": None,
            "amount": "5",
            "fixed_amount": True,
        }


class StatefulShadowOverlay:
    def __init__(self, meta):
        self.meta = meta
        self.id = meta["id"]
        self.base = meta["base"]
        self.source_shadow = meta["source_shadow"]
        self.policy = meta.get("policy", "one_open_position")
        self.active_until = None
        print(
            f"[Signal] {self.id} -> stateful overlay | base={self.base} "
            f"| source={self.source_shadow} | policy={self.policy}"
        )

    def predict(self, source_result):
        if not source_result:
            return None
        out = dict(source_result)
        out.update({
            "strategy_id": self.id,
            "label": self.id,
            "shadow": True,
            "shadow_type": "stateful_overlay",
            "shadow_base_strategy": self.base,
            "stateful_source_strategy": self.source_shadow,
            "stateful_policy": self.policy,
            "stateful_filter_ok": True,
            "stateful_filter_reasons": [],
        })
        sig = out.get("signal")
        if not sig:
            return out
        entry_time = pd.to_datetime(out.get("actionable_time") or out.get("candle_close_time") or out.get("time"), utc=True)
        duration = pd.Timedelta(minutes=int(float(out.get("duration") or out.get("interval_min") or 0)))
        if self.policy == "one_open_position" and self.active_until is not None and entry_time < self.active_until:
            out["signal"] = None
            out["confidence"] = None
            out["stateful_filter_ok"] = False
            out["stateful_filter_reasons"] = ["one_open_position"]
            return out
        self.active_until = entry_time + duration
        return out


def trend_direction_value(score):
    score = int(score or 0)
    if score >= 3:
        return 1
    if score <= -3:
        return 0
    return -1


class MetaGateShadow:
    def __init__(self, meta):
        self.meta = meta
        self.id = meta["id"]
        self.base = meta["base"]
        self.model_id = meta["model_id"]
        self.threshold = float(meta["threshold"])
        prefix = os.path.join(OUT, f"meta_gate_{self.model_id}")
        self.model = None
        self.feat_cols = []
        try:
            with open(f"{prefix}_lgb.pkl", "rb") as f:
                self.model = pickle.load(f)
            with open(f"{prefix}_cols.json", "r", encoding="utf-8") as f:
                self.feat_cols = json.load(f)
            print(
                f"[Signal] {self.id} -> meta gate | base={self.base} "
                f"| model={self.model_id} | th={self.threshold} | features={len(self.feat_cols)}"
            )
        except Exception as e:
            print(f"[Signal] {self.id} disabled; meta gate model load failed: {e}")

    def _features(self, base_result):
        direction = base_result.get("signal")
        direction_num = 1 if direction == "UP" else 0
        trend_score_val = int(base_result.get("trend_score") or 0)
        htf_score_val = int(base_result.get("htf_score") or 0)
        short_trend_dir = trend_direction_value(trend_score_val)
        htf_trend_dir = trend_direction_value(htf_score_val)
        short_counter = short_trend_dir >= 0 and direction_num != short_trend_dir
        htf_counter = htf_trend_dir >= 0 and direction_num != htf_trend_dir
        both_counter = short_counter and htf_counter and short_trend_dir == htf_trend_dir
        hour = pd.to_datetime(base_result.get("time"), utc=True).hour
        row = {
            "avg": float(base_result.get("avg_prob") or 0.5),
            "strength": float(base_result.get("confidence") or 0),
            "rsi14": float(base_result.get("rsi_value") or 50),
            "bbp": float(base_result.get("bbp") or 0.5),
            "bbw": float(base_result.get("bbw") or 0),
            "atrp": float(base_result.get("atrp") or 0),
            "atr_exp": float(base_result.get("atr_exp") or 0),
            "vr": float(base_result.get("vr") or 1),
            "trend6": float(base_result.get("trend6") or 0),
            "trend12": float(base_result.get("trend12") or 0),
            "trend30": float(base_result.get("trend30") or 0),
            "pre50": float(base_result.get("pre50") or 0),
            "ema_stack": float(base_result.get("ema_stack") or 0),
            "trend_score": trend_score_val,
            "htf_score": htf_score_val,
            "htf_ret_1h": float(base_result.get("htf_ret_1h") or 0),
            "htf_ret_4h": float(base_result.get("htf_ret_4h") or 0),
            "htf_ret_24h": float(base_result.get("htf_ret_24h") or 0),
            "htf_pos_1h": float(base_result.get("htf_pos_1h") or 0.5),
            "htf_pos_4h": float(base_result.get("htf_pos_4h") or 0.5),
            "htf_pos_24h": float(base_result.get("htf_pos_24h") or 0.5),
            "htf_rng_1h": float(base_result.get("htf_rng_1h") or 0),
            "htf_rng_4h": float(base_result.get("htf_rng_4h") or 0),
            "htf_rng_24h": float(base_result.get("htf_rng_24h") or 0),
            "taker_ratio": float(base_result.get("taker_ratio") or 1),
            "ls_ratio": float(base_result.get("ls_ratio") or 1),
            "fund_rate": float(base_result.get("fund_rate") or 0),
            "short_align": trend_score_val if direction_num == 1 else -trend_score_val,
            "htf_align": htf_score_val if direction_num == 1 else -htf_score_val,
            "is_down": 1 if direction == "DOWN" else 0,
            "short_countertrend": 1 if short_counter else 0,
            "htf_countertrend": 1 if htf_counter else 0,
            "both_countertrend": 1 if both_counter else 0,
            "hour_sin": math.sin(2 * math.pi * hour / 24),
            "hour_cos": math.cos(2 * math.pi * hour / 24),
        }
        return [[float(row.get(c, 0) or 0) for c in self.feat_cols]], {
            "short_countertrend": short_counter,
            "htf_countertrend": htf_counter,
            "both_countertrend": both_counter,
        }

    def predict(self, base_result):
        if not base_result:
            return None
        out = dict(base_result)
        out.update({
            "strategy_id": self.id,
            "label": self.id,
            "shadow": True,
            "shadow_type": "meta_gate",
            "shadow_base_strategy": self.base,
            "meta_gate_model": self.model_id,
            "meta_threshold": self.threshold,
            "meta_gate_ok": False,
            "meta_gate_reasons": [],
            "fixed_amount": True,
            "amount": "5",
        })
        if self.model is None:
            out["signal"] = None
            out["confidence"] = None
            out["meta_gate_reasons"] = ["model_missing"]
            return out
        if not base_result.get("signal"):
            out["signal"] = None
            out["confidence"] = None
            out["meta_gate_reasons"] = ["base_no_signal"]
            return out
        X, flags = self._features(base_result)
        meta_prob = float(self.model.predict_proba(X)[0, 1])
        gate_ok = meta_prob >= self.threshold
        out.update({
            "meta_prob": round(meta_prob, 4),
            "meta_gate_ok": gate_ok,
            "meta_short_countertrend": flags["short_countertrend"],
            "meta_htf_countertrend": flags["htf_countertrend"],
            "meta_both_countertrend": flags["both_countertrend"],
        })
        if not gate_ok:
            out["signal"] = None
            out["confidence"] = None
            out["meta_gate_reasons"] = ["meta_prob_below_threshold"]
        return out


def fetch_live_1m_raw(limit=1000):
    last_err = None
    for base in BASE_URLS:
        try:
            r = requests.get(
                f"{base}/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "1m", "limit": int(limit)},
                timeout=10,
            )
            r.raise_for_status()
            break
        except Exception as e:
            last_err = e
            r = None
    if r is None:
        raise last_err
    df = pd.DataFrame(r.json(), columns=["ot", "o", "h", "l", "c", "v", "ct", "qv", "tr", "t1", "t2", "t3"])
    for c in ["o", "h", "l", "c", "v"]:
        df[c] = df[c].astype(float)
    df["ot"] = pd.to_datetime(df["ot"], unit="ms", utc=True)
    df = df[["ot", "o", "h", "l", "c", "v"]].rename(
        columns={"ot": "open_time", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    )
    return df


def aggregate_live_5m(df):
    df = df.copy()
    df["p"] = df["open_time"].dt.floor("5min")
    latest_1m_open = df["open_time"].max()
    live = df.groupby("p").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).reset_index().rename(columns={"p": "time"})
    # Drop the still-forming 5m candle. The model may only act after a 5m
    # candle closes; using the moving candle would repaint live signals.
    live["close_time"] = live["time"] + pd.Timedelta(minutes=5)
    live = live[live["close_time"] <= latest_1m_open].drop(columns=["close_time"])
    return live


def fetch_live_klines():
    return aggregate_live_5m(fetch_live_1m_raw(500))


def merge_live(df5, live):
    last_hist = pd.to_datetime(df5["time"]).max()
    live["time_dt"] = pd.to_datetime(live["time"], utc=True)
    new = live[live["time_dt"] > last_hist]
    if len(new) > 0:
        first_new = pd.to_datetime(new["time_dt"].min(), utc=True)
        if last_hist + MAX_5M_LIVE_MERGE_GAP < first_new:
            print(
                f"[Signal] 5m live merge blocked: history/live gap "
                f"{(first_new - last_hist).total_seconds():.0f}s ({last_hist} -> {first_new})"
            )
            return df5
        for c in ["funding_rate", "ls_ratio", "ls_long", "ls_short", "taker_ratio", "taker_buy", "taker_sell"]:
            if c in df5.columns:
                new[c] = df5[c].iloc[-1]
        new = new.drop(columns=["time_dt"])
        df5 = pd.concat([df5, new], ignore_index=True)
    return df5


def status_text(r):
    if r.get("signal"):
        return f"*** {r['signal']} {r['confidence']}% ***"
    parts = []
    if not r.get("agree", True):
        parts.append("model split")
    if not r.get("high_conf", False):
        parts.append("low conf")
    if not r.get("rsi_extreme", True):
        parts.append(f"RSI={r['rsi_value']}")
    if not r.get("vol_ok", True):
        parts.append(f"vol={r.get('vol_rank')}")
    if r.get("session_filter_mode") == "soft" and r.get("session_risk"):
        if r.get("session_gate_ok") is False:
            parts.append("session risk " + ",".join(r.get("session_gate_reasons") or []))
        else:
            parts.append(f"session risk soft score={r.get('market_confirm_score')}")
    elif r.get("session_ok") is False:
        parts.append("session hard block")
    if r.get("countertrend_guard_ok") is False:
        parts.append("countertrend hot")
    if r.get("regime_filter_ok") is False:
        parts.append("regime " + ",".join(r.get("regime_filter_reasons") or []))
    if r.get("stateful_filter_ok") is False:
        parts.append("stateful " + ",".join(r.get("stateful_filter_reasons") or []))
    return " | ".join(parts) if parts else "waiting"


def fmt_num(value, default=0.0):
    try:
        if value is None:
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _make_strategy(sid, cfg):
    if cfg.get("model_type") == "poc_normal":
        return POCNormalStrategy(sid, cfg)
    if cfg.get("model_type") == "second_normal":
        return SecondNormalStrategy(sid, cfg)
    if cfg.get("model_type") == "second_normal_router_v21":
        return SecondNormalRouterV21Strategy(sid, cfg)
    if cfg.get("model_type") == "second_normal_lowvol_v22":
        return SecondNormalLowVolV22Strategy(sid, cfg)
    if cfg.get("model_type") == "second_normal_liquidity_orderbook_v1":
        return SecondNormalLiquidityOrderbookV1Strategy(sid, cfg)
    if cfg.get("model_type") == "second_normal_vw_confirm":
        return SecondNormalVwConfirmStrategy(sid, cfg)
    if cfg.get("model_type") == "normal_state_v11":
        return NormalStateV11Strategy(sid, cfg)
    if cfg.get("model_type") == "second_chip":
        return SecondChipStrategy(sid, cfg)
    if cfg.get("model_type") == "second_range_breakout_confirm":
        return SecondRangeBreakoutConfirmStrategy(sid, cfg)
    if cfg.get("model_type") == "second_value_area_smart":
        return SecondValueAreaSmartStrategy(sid, cfg)
    if cfg.get("model_type") == "second_trend_pullback_down":
        return SecondTrendPullbackDownStrategy(sid, cfg)
    return Strategy(sid, cfg)

def build_strategies(config_map):
    live_two_minute_ids = {item["id"] for item in TWO_MINUTE_LIVE_CANDIDATES}
    return [
        _make_strategy(k, v)
        for k, v in config_map.items()
        if v.get("enabled", True) and (v.get("model_type") in ("poc_normal", "second_normal", "second_normal_router_v21", "second_normal_lowvol_v22", "second_normal_liquidity_orderbook_v1", "second_normal_vw_confirm", "normal_state_v11", "second_chip", "second_range_breakout_confirm", "second_value_area_smart", "second_trend_pullback_down") or k not in live_two_minute_ids)
    ]


def apply_trend_mode_switch(signals):
    trend_blockers = [
        row for row in signals.values()
        if row.get("model_type") == "second_trend_pullback_down"
        and row.get("trend_regime_active")
        and row.get("suppress_reversal_in_regime")
    ]
    if not trend_blockers:
        return signals
    blocker = trend_blockers[0]
    out = {}
    for strategy_id, row in signals.items():
        if row.get("model_type") in ("second_normal", "second_normal_router_v21", "second_normal_lowvol_v22", "second_normal_liquidity_orderbook_v1", "second_normal_vw_confirm", "normal_state_v11", "second_chip", "second_range_breakout_confirm") and row.get("signal"):
            blocked = dict(row)
            blocked["blocked_signal"] = blocked.get("signal")
            blocked["blocked_confidence"] = blocked.get("confidence")
            blocked["signal"] = None
            blocked["high_conf"] = False
            blocked["reason"] = "trend_down_mode_suppressed_reversal"
            blocked["trend_mode_strategy_id"] = blocker.get("strategy_id")
            blocked["trend_regime_ret"] = blocker.get("regime_ret")
            out[strategy_id] = blocked
        else:
            out[strategy_id] = row
    return out


def apply_incident_mode_filter(signals, config_map):
    needs_filter = any(
        row.get("signal")
        and incident_config_from_dict(config_map.get(strategy_id, {})).enabled
        for strategy_id, row in (signals or {}).items()
    )
    if not needs_filter:
        return signals
    try:
        bars = load_second_bars_cached_for_cycle()
    except Exception as exc:
        print(f"[Signal] incident filter skipped, second data load failed: {exc}")
        return signals
    if bars is None or len(bars) == 0:
        return signals
    if "time" in bars.columns:
        bars = bars.copy()
        bars["time"] = pd.to_datetime(bars["time"], utc=True, errors="coerce")
        bars = bars.dropna(subset=["time"]).set_index("time").sort_index()
    return apply_incident_filter_to_live_signals(bars, signals, config_map)

configs = load_config()
update_second_tail_requirement(configs)
configs_mtime = file_mtime(CONFIG_FILE)
strategies = build_strategies(configs)
live_two_minute_strategies = (
    [(item, TwoMinuteRegimeShadow(item)) for item in TWO_MINUTE_LIVE_CANDIDATES]
    if ENABLE_LEGACY_TWO_MINUTE_LIVE else []
)
shadow_strategies = []
if ENABLE_SIGNAL_SHADOWS:
    for shadow in SHADOW_CANDIDATES:
        if shadow["base"] not in configs or not configs.get(shadow["base"], {}).get("enabled", True):
            continue
        base_cfg = dict(configs[shadow["base"]])
        base_cfg.update({
            "threshold": shadow["threshold"],
            "rsi_lo": shadow["rsi_lo"],
            "rsi_hi": shadow["rsi_hi"],
            "agree_mode": shadow["agree_mode"],
            "vol_min_rank": shadow["vol_min_rank"],
            "fixed_amount": 5,
            "countertrend_max_abs_trend6": shadow.get("countertrend_max_abs_trend6"),
            "countertrend_max_strength": shadow.get("countertrend_max_strength"),
            "bbp_cap": shadow.get("bbp_cap"),
            "rsi_extreme_cap": shadow.get("rsi_extreme_cap"),
            "confidence_max": shadow.get("confidence_max"),
            "skip_hours_utc": sorted(set(base_cfg.get("skip_hours_utc", [])) | set(shadow.get("extra_skip_hours_utc", []))),
            "enabled": True,
        })
        shadow_strategies.append((shadow, Strategy(shadow["id"], base_cfg)))
rule_shadow_strategies = []
if ENABLE_SIGNAL_SHADOWS:
    for shadow in RULE_SHADOW_CANDIDATES:
        if shadow["base"] not in configs or not configs.get(shadow["base"], {}).get("enabled", True):
            continue
        base_cfg = dict(configs[shadow["base"]])
        rule_shadow_strategies.append((shadow, RuleShadowStrategy(shadow, base_cfg)))
stateful_shadow_overlays = (
    [
        (shadow, StatefulShadowOverlay(shadow))
        for shadow in STATEFUL_SHADOW_CANDIDATES
        if shadow["base"] in configs and configs.get(shadow["base"], {}).get("enabled", True)
    ]
    if ENABLE_SIGNAL_SHADOWS else []
)
meta_gate_shadows = (
    [
        (shadow, MetaGateShadow(shadow))
        for shadow in META_GATE_SHADOW_CANDIDATES
        if shadow["base"] in configs and configs.get(shadow["base"], {}).get("enabled", True)
    ]
    if ENABLE_SIGNAL_SHADOWS else []
)
two_minute_shadow_strategies = (
    [(shadow, TwoMinuteRegimeShadow(shadow)) for shadow in TWO_MINUTE_SHADOW_CANDIDATES]
    if ENABLE_SIGNAL_SHADOWS else []
)
if not ENABLE_SIGNAL_SHADOWS:
    print("[Signal] Shadow strategies disabled for lower CPU usage. Set ENABLE_SIGNAL_SHADOWS=1 to collect shadow samples.")
if not ENABLE_LEGACY_TWO_MINUTE_LIVE:
    print("[Signal] Legacy two-minute live candidates disabled. Set ENABLE_LEGACY_TWO_MINUTE_LIVE=1 only for research.")
last_audit_keys = load_audit_keys(SIGNAL_AUDIT_FILE)

print("[Signal] Loading BTC history...")
df5 = load_symbol("btcusdt")
df5_history_mtime = file_mtime(HISTORY_1M_FILE)
print(f"[Signal] {len(df5)} 5m candles")
print("\n[Signal] Starting BTC dual-strategy loop (every 5s)...")
last_data_health_key = None

while True:
    try:
        current_config_mtime = file_mtime(CONFIG_FILE)
        if current_config_mtime is not None and current_config_mtime != configs_mtime:
            configs = load_config()
            update_second_tail_requirement(configs)
            configs_mtime = current_config_mtime
            strategies = build_strategies(configs)
            print(
                "[Signal] Reloaded strategy config: "
                + ", ".join(strategy.id for strategy in strategies)
            )
        current_history_mtime = file_mtime(HISTORY_1M_FILE)
        if current_history_mtime is not None and current_history_mtime != df5_history_mtime:
            df5 = load_symbol("btcusdt")
            df5_history_mtime = current_history_mtime
            print(f"[Signal] Reloaded 5m history after data update: {len(df5)} candles")
        live_1m = fetch_live_1m_raw(1000)
        data_health = build_live_data_health(live_1m)
        data_health_key = "blocked:" + ",".join(data_health["reasons"]) if data_health["blocked"] else "ok"
        if data_health_key != last_data_health_key:
            print(f"[Signal] Data health {data_health_key}")
            last_data_health_key = data_health_key
        live = aggregate_live_5m(live_1m)
        df5 = merge_live(df5, live)
        begin_second_bars_cycle()
        signals = {}
        for strategy in strategies:
            r = strategy.predict(df5)
            if r:
                signals[strategy.id] = r
                print(
                    f"  {r.get('time','?')} {strategy.id} avg={fmt_num(r.get('avg_prob')):.3f}"
                    f" RSI={fmt_num(r.get('rsi_value')):.0f} {status_text(r)}"
                )
        signals = apply_trend_mode_switch(signals)
        signals = apply_incident_mode_filter(signals, configs)
        for _, strategy in live_two_minute_strategies:
            if configs.get(strategy.id, {}).get("model_type") == "poc_normal": continue
            r = strategy.predict(live_1m)
            if r:
                signals[strategy.id] = r
                print(
                    f"  {r['time']} {strategy.id} 2m-live p={r['avg_prob']:.3f} "
                    f"regime={r.get('regime_group')} {status_text(r)}"
                )
        if signals:
            signals = apply_signal_data_health(signals, data_health)
            snapshot_ms = int(time.time() * 1000)
            signals_with_meta = {
                **signals,
                "_snapshot_time_ms": snapshot_ms,
                "_snapshot_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(snapshot_ms / 1000)),
                "_snapshot_strategy_count": len(signals),
            }
            write_json_atomic(SIGNAL_FILE, signals_with_meta)
            for strategy_id, r in signals.items():
                key = f"signal_snapshot|{strategy_id}|{r.get('time')}"
                if key not in last_audit_keys:
                    append_jsonl(SIGNAL_AUDIT_FILE, {
                        "event": "signal_snapshot",
                        "serverTime": int(time.time() * 1000),
                        **r,
                    })
                    last_audit_keys.add(key)
        shadow_results = {}
        for shadow_meta, shadow_strategy in shadow_strategies:
            r = shadow_strategy.predict(df5)
            if not r:
                continue
            r["shadow"] = True
            r["shadow_base_strategy"] = shadow_meta["base"]
            r["shadow_note"] = shadow_meta["note"]
            shadow_results[r.get("strategy_id")] = r
            key = f"shadow_candidate|{r.get('strategy_id')}|{r.get('time')}"
            if key not in last_audit_keys:
                append_jsonl(SIGNAL_AUDIT_FILE, {
                    "event": "shadow_candidate",
                    "serverTime": int(time.time() * 1000),
                    **r,
                })
                last_audit_keys.add(key)
            if r.get("signal"):
                print(
                    f"  {r['time']} {r['strategy_id']} shadow avg={r['avg_prob']:.3f} "
                    f"RSI={r['rsi_value']:.0f} {status_text(r)}"
                )
            if len(last_audit_keys) > 5000:
                last_audit_keys = set(list(last_audit_keys)[-2000:])
        for shadow_meta, overlay in stateful_shadow_overlays:
            r = overlay.predict(shadow_results.get(shadow_meta["source_shadow"]))
            if not r:
                continue
            r["shadow_note"] = shadow_meta["note"]
            key = f"shadow_candidate|{r.get('strategy_id')}|{r.get('time')}"
            if key not in last_audit_keys:
                append_jsonl(SIGNAL_AUDIT_FILE, {
                    "event": "shadow_candidate",
                    "serverTime": int(time.time() * 1000),
                    **r,
                })
                last_audit_keys.add(key)
                source_had_signal = bool(shadow_results.get(shadow_meta["source_shadow"], {}).get("signal"))
                if source_had_signal:
                    print(
                        f"  {r['time']} {r['strategy_id']} stateful-shadow "
                        f"RSI={r['rsi_value']:.0f} {status_text(r)}"
                    )
            if len(last_audit_keys) > 5000:
                last_audit_keys = set(list(last_audit_keys)[-2000:])
        for shadow_meta, meta_gate in meta_gate_shadows:
            r = meta_gate.predict(signals.get(shadow_meta["base"]))
            if not r:
                continue
            r["shadow_note"] = shadow_meta["note"]
            key = f"shadow_candidate|{r.get('strategy_id')}|{r.get('time')}"
            if key not in last_audit_keys:
                append_jsonl(SIGNAL_AUDIT_FILE, {
                    "event": "shadow_candidate",
                    "serverTime": int(time.time() * 1000),
                    **r,
                })
                last_audit_keys.add(key)
            if r.get("signal"):
                print(
                    f"  {r['time']} {r['strategy_id']} meta-shadow "
                    f"p={r.get('meta_prob')} RSI={r['rsi_value']:.0f} {status_text(r)}"
                )
            if len(last_audit_keys) > 5000:
                last_audit_keys = set(list(last_audit_keys)[-2000:])
        for shadow_meta, shadow_strategy in two_minute_shadow_strategies:
            r = shadow_strategy.predict(live_1m)
            if not r:
                continue
            r["shadow_note"] = shadow_meta["note"]
            key = f"shadow_candidate|{r.get('strategy_id')}|{r.get('time')}"
            if key not in last_audit_keys:
                append_jsonl(SIGNAL_AUDIT_FILE, {
                    "event": "shadow_candidate",
                    "serverTime": int(time.time() * 1000),
                    **r,
                })
                last_audit_keys.add(key)
            if r.get("signal"):
                print(
                    f"  {r['time']} {r['strategy_id']} 2m-shadow "
                    f"p={r.get('avg_prob')} {status_text(r)}"
                )
            if len(last_audit_keys) > 5000:
                last_audit_keys = set(list(last_audit_keys)[-2000:])
        for shadow_meta, shadow_strategy in rule_shadow_strategies:
            r = shadow_strategy.predict(df5)
            if not r:
                continue
            r["shadow"] = True
            r["shadow_note"] = shadow_meta["note"]
            key = f"shadow_candidate|{r.get('strategy_id')}|{r.get('time')}"
            if key not in last_audit_keys:
                append_jsonl(SIGNAL_AUDIT_FILE, {
                    "event": "shadow_candidate",
                    "serverTime": int(time.time() * 1000),
                    **r,
                })
                last_audit_keys.add(key)
            if r.get("signal"):
                print(
                    f"  {r['time']} {r['strategy_id']} rule-shadow "
                    f"RSI={r['rsi_value']:.0f} trend={r['trend_score']} {status_text(r)}"
                )
            if len(last_audit_keys) > 5000:
                last_audit_keys = set(list(last_audit_keys)[-2000:])
        end_second_bars_cycle()
        if os.environ.get("SIGNAL_ONCE") == "1":
            break
    except Exception as e:
        end_second_bars_cycle()
        import traceback; traceback.print_exc()
        if os.environ.get("SIGNAL_ONCE") == "1":
            raise
    time.sleep(5)
