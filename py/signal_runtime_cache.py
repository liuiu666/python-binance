"""Shared second-bar, minute-feature, and order-book caches."""

import math
import os
import threading
import time

import numpy as np
import pandas as pd

import research_normal_state_v1 as normal_state_v1
from second_backtest.data import load_recent_second_bars
from signal_io import csv_tail_rows, file_signature
from signal_paths import HISTORY_1M_FILE, ORDERBOOK_FILE, SECOND_TRADES_FILE


SECOND_BARS_DEFAULT_TAIL_SEC = int(os.environ.get("SECOND_SIGNAL_TAIL_SEC", str(6 * 60 * 60)))
SECOND_BARS_CACHE_TTL = 1.0
SECOND_BARS_INCREMENTAL_TAIL_SEC = max(60, int(os.environ.get("SECOND_SIGNAL_INCREMENTAL_TAIL_SEC", "180")))
SECOND_BARS_INCREMENTAL_WARMUP_SEC = max(60, int(os.environ.get("SECOND_SIGNAL_INCREMENTAL_WARMUP_SEC", "120")))
SECOND_BARS_INCREMENTAL_INITIAL_BYTES = max(4096, int(os.environ.get("SECOND_SIGNAL_INCREMENTAL_INITIAL_BYTES", str(512 * 1024))))
ORDERBOOK_FEATURE_TAIL_ROWS = int(os.environ.get("ORDERBOOK_FEATURE_TAIL_ROWS", "18000"))
ORDERBOOK_FEATURE_TAIL_CHUNK = int(os.environ.get("ORDERBOOK_FEATURE_TAIL_CHUNK", str(12 * 1024 * 1024)))

_SECOND_BARS_REQUIRED_SEC = SECOND_BARS_DEFAULT_TAIL_SEC
_SECOND_BARS_CACHE = {"bars": None, "file_size": 0, "last_check": 0.0, "tail_sec": 0}
_SECOND_BARS_CYCLE_CACHE = {"active": False, "bars": None}
_MINUTE_FEATURE_CACHE = {"signature": None, "features": None}
_ORDERBOOK_FEATURE_CACHE = {"key": None, "features": None}
_ORDERBOOK_ROWS_CACHE = {"signature": None, "rows": None, "limit": 0}
_ORDERBOOK_ROWS_CYCLE_CACHE = {"active": False, "rows": None, "limit": 0}


class StaleWhileRefreshCache:
    """Keep the last good value while a single daemon thread refreshes it."""

    def __init__(self, fetcher, refresh_sec=5.0, retry_sec=2.0, clock=None):
        self.fetcher = fetcher
        self.refresh_sec = max(0.1, float(refresh_sec))
        self.retry_sec = max(0.1, float(retry_sec))
        self.clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._value = None
        self._last_success = 0.0
        self._last_attempt = 0.0
        self._last_error = None
        self._thread = None

    def _store_success(self, value):
        with self._lock:
            self._value = value
            self._last_success = float(self.clock())
            self._last_attempt = self._last_success
            self._last_error = None

    def prime(self):
        value = self.fetcher()
        self._store_success(value)
        return value

    def _refresh_worker(self):
        try:
            value = self.fetcher()
        except Exception as exc:
            with self._lock:
                self._last_attempt = float(self.clock())
                self._last_error = str(exc)
        else:
            self._store_success(value)
        finally:
            with self._lock:
                self._thread = None

    def get(self):
        now = float(self.clock())
        thread = None
        with self._lock:
            value = self._value
            reference = self._last_success if self._last_error is None else self._last_attempt
            interval = self.refresh_sec if self._last_error is None else self.retry_sec
            due = reference <= 0.0 or now - reference >= interval
            if due and self._thread is None:
                thread = threading.Thread(target=self._refresh_worker, name="live-1m-refresh", daemon=True)
                self._thread = thread
        if thread is not None:
            thread.start()
        return value

    def wait(self, timeout=None):
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.get_without_refresh()

    def get_without_refresh(self):
        with self._lock:
            return self._value

    def status(self):
        with self._lock:
            return {
                "has_value": self._value is not None,
                "refreshing": self._thread is not None,
                "last_success": self._last_success,
                "last_attempt": self._last_attempt,
                "last_error": self._last_error,
                "refresh_sec": self.refresh_sec,
                "retry_sec": self.retry_sec,
            }


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
        elif model_type in ("second_range_breakout_confirm", "second_value_area_smart", "second_trend_pullback_down", "second_normal_liquidity_orderbook_v1", "second_normal_trend_orderbook_latch_v2", "second_branch_vote_startup_v1", "second_multi_normal_hf_stable_v1"):
            lookback = max(
                _int_cfg(cfg, "second_lookback_sec", 3600),
                _int_cfg(cfg, "value_area_sec", 3600),
                _int_cfg(cfg, "trend_lookback_sec", 3600),
                _int_cfg(cfg, "second_liq_normal_window_sec", 600),
                _int_cfg(cfg, "branch_vote_normal_window_sec", 600) + 5400,
                _int_cfg(cfg, "multi_normal_window_sec", 600) + 5400,
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
    _ORDERBOOK_ROWS_CYCLE_CACHE["active"] = True
    _ORDERBOOK_ROWS_CYCLE_CACHE["rows"] = None
    _ORDERBOOK_ROWS_CYCLE_CACHE["limit"] = 0


def end_second_bars_cycle():
    _SECOND_BARS_CYCLE_CACHE["active"] = False
    _SECOND_BARS_CYCLE_CACHE["bars"] = None
    _ORDERBOOK_ROWS_CYCLE_CACHE["active"] = False
    _ORDERBOOK_ROWS_CYCLE_CACHE["rows"] = None
    _ORDERBOOK_ROWS_CYCLE_CACHE["limit"] = 0


def remember_second_bars_for_cycle(bars):
    if _SECOND_BARS_CYCLE_CACHE.get("active") and bars is not None:
        _SECOND_BARS_CYCLE_CACHE["bars"] = bars
    return bars


def _merge_second_bar_tail(existing, latest, tail_sec):
    """Replace the recent overlap while retaining the configured live window."""

    if existing is None or existing.empty:
        return latest
    if latest is None or latest.empty:
        return existing
    merged = pd.concat([existing, latest], ignore_index=True)
    merged["time"] = pd.to_datetime(merged["time"], utc=True, errors="coerce")
    merged = merged.dropna(subset=["time"]).sort_values("time").drop_duplicates("time", keep="last")
    cutoff = merged["time"].iloc[-1] - pd.Timedelta(seconds=max(1, int(tail_sec)))
    return merged.loc[merged["time"] >= cutoff].reset_index(drop=True)


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

    incremental = (
        cache["bars"] is not None
        and not cache["bars"].empty
        and cur_size > int(cache.get("file_size") or 0)
        and int(cache.get("tail_sec") or 0) == required_tail_sec
    )
    try:
        if incremental:
            recent = load_recent_second_bars(
                SECOND_TRADES_FILE,
                include_shards=True,
                tail_sec=min(required_tail_sec, SECOND_BARS_INCREMENTAL_TAIL_SEC),
                source_warmup_sec=SECOND_BARS_INCREMENTAL_WARMUP_SEC,
                read_initial_bytes=SECOND_BARS_INCREMENTAL_INITIAL_BYTES,
            ).reset_index().rename(columns={"index": "time"})
            sec = _merge_second_bar_tail(cache["bars"], recent, required_tail_sec)
        else:
            sec = load_recent_second_bars(
                SECOND_TRADES_FILE,
                include_shards=True,
                tail_sec=required_tail_sec,
            ).reset_index().rename(columns={"index": "time"})
    except Exception as exc:
        print(f"[Signal] second data load failed: {exc}")
        return remember_second_bars_for_cycle(cache["bars"])
    if sec.empty:
        return remember_second_bars_for_cycle(cache["bars"])

    cache["bars"] = sec
    cache["file_size"] = cur_size
    cache["tail_sec"] = required_tail_sec
    return remember_second_bars_for_cycle(cache["bars"])


def load_orderbook_rows_cached_for_cycle(limit):
    requested = max(1, int(limit))
    cycle = _ORDERBOOK_ROWS_CYCLE_CACHE
    if cycle.get("active") and cycle.get("rows") is not None and int(cycle.get("limit") or 0) >= requested:
        return cycle["rows"][-requested:]

    signature = file_signature(ORDERBOOK_FILE)
    cache = _ORDERBOOK_ROWS_CACHE
    fetch_limit = max(requested, ORDERBOOK_FEATURE_TAIL_ROWS)
    if (
        cache.get("rows") is None
        or cache.get("signature") != signature
        or int(cache.get("limit") or 0) < requested
    ):
        cache["rows"] = csv_tail_rows(
            ORDERBOOK_FILE,
            limit=fetch_limit,
            chunk_size=ORDERBOOK_FEATURE_TAIL_CHUNK,
        )
        cache["signature"] = signature
        cache["limit"] = fetch_limit

    rows = cache.get("rows") or []
    if cycle.get("active"):
        cycle["rows"] = rows
        cycle["limit"] = int(cache.get("limit") or 0)
    return rows[-requested:]


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
    key = (sig, len(second_index), int(second_index[0].value), int(second_index[-1].value))
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
        rows = load_orderbook_rows_cached_for_cycle(limit)
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
