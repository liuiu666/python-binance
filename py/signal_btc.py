"""BTC dual-strategy signal service.

Outputs independent BTC_10min and BTC_30min signals for the tablet executor.
"""
import json
import msvcrt
import os
import pickle
import atexit
import shutil
import socket
import sys
import time
import warnings

OUT = "E:/codex/data"
SIGNAL_FILE = os.path.join(OUT, "live_signals.json")
CONFIG_FILE = os.path.join(OUT, "prod_config.json")
SIGNAL_AUDIT_FILE = os.path.join(OUT, "signal_audit.jsonl")
LOCK_FILE = os.path.join(OUT, "signal_btc.lock")
LOCK_DIR = os.path.join(OUT, "signal_btc.lockdir")
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
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
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
import requests
from lightgbm import LGBMClassifier
from xgboost import XGBClassifier

warnings.filterwarnings("ignore")

sys.path.insert(0, "E:/codex/py")
from backtest_enhanced import build_features, load_symbol


def append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


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
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
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
        hlp20_val = float(last.iloc[0].get("hlp20", 0.5) or 0.5)
        hlp50_val = float(last.iloc[0].get("hlp50", 0.5) or 0.5)
        trend12_val = float(last.iloc[0].get("trend12", 0) or 0)
        trend30_val = float(last.iloc[0].get("trend30", 0) or 0)
        pre50_val = float(last.iloc[0].get("pre50", 0) or 0)
        ema_stack_val = float(last.iloc[0].get("ema_stack", 0) or 0)
        atrp = float(X[0, self.feat_cols.index("atrp")]) if "atrp" in self.feat_cols else None
        vol_rank = None
        vol_ok = True
        if self.vol_min_rank is not None and "atrp" in fdf.columns:
            recent = fdf["atrp"].dropna().iloc[-8000:]
            if len(recent) > 1 and atrp is not None:
                vol_rank = float((recent <= atrp).mean())
                vol_ok = vol_rank >= self.vol_min_rank

        candle_time = pd.to_datetime(df5["time"].iloc[-1], utc=True)
        candle_close_time = candle_time + pd.Timedelta(minutes=5)
        session_ok = candle_time.hour not in self.skip_hours_utc
        trend_val = trend_score(last.iloc[0])

        sig = None
        conf = None
        if agree and high_conf and rsi_extreme and vol_ok and session_ok:
            if self.agree_mode == "majority":
                sig = "UP" if majority_up else "DOWN"
            else:
                sig = "UP" if avg >= 0.5 else "DOWN"
            conf = round(abs(avg - 0.5) * 2 * 100, 1)
        countertrend_guard_ok = True
        regime_filter_ok = True
        regime_filter_reasons = []
        trend6_val = float(last.iloc[0].get("trend6", 0) or 0)
        strength_val = round(abs(avg - 0.5) * 2 * 100, 1)
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
            "trend6": round(trend6_val, 6),
            "trend12": round(trend12_val, 6),
            "trend30": round(trend30_val, 6),
            "pre50": round(pre50_val, 6),
            "ema_stack": round(ema_stack_val, 3),
            "bbp": round(bbp_val, 4),
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
            "skip_hours_utc": self.skip_hours_utc,
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


def fetch_live_klines():
    last_err = None
    for base in BASE_URLS:
        try:
            r = requests.get(
                f"{base}/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "1m", "limit": 500},
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


def merge_live(df5, live):
    last_hist = pd.to_datetime(df5["time"]).max()
    live["time_dt"] = pd.to_datetime(live["time"], utc=True)
    new = live[live["time_dt"] > last_hist]
    if len(new) > 0:
        for c in ["funding_rate", "ls_ratio", "ls_long", "ls_short", "taker_ratio", "taker_buy", "taker_sell"]:
            if c in df5.columns:
                new[c] = df5[c].iloc[-1]
        new = new.drop(columns=["time_dt"])
        df5 = pd.concat([df5, new], ignore_index=True)
    return df5


def status_text(r):
    if r["signal"]:
        return f"*** {r['signal']} {r['confidence']}% ***"
    parts = []
    if not r["agree"]:
        parts.append("model split")
    if not r["high_conf"]:
        parts.append("low conf")
    if not r["rsi_extreme"]:
        parts.append(f"RSI={r['rsi_value']}")
    if not r.get("vol_ok", True):
        parts.append(f"vol={r.get('vol_rank')}")
    if r.get("countertrend_guard_ok") is False:
        parts.append("countertrend hot")
    if r.get("regime_filter_ok") is False:
        parts.append("regime " + ",".join(r.get("regime_filter_reasons") or []))
    return " | ".join(parts) if parts else "waiting"


configs = load_config()
strategies = [Strategy(k, v) for k, v in configs.items() if v.get("enabled", True)]
shadow_strategies = []
for shadow in SHADOW_CANDIDATES:
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
for shadow in RULE_SHADOW_CANDIDATES:
    base_cfg = dict(configs[shadow["base"]])
    rule_shadow_strategies.append((shadow, RuleShadowStrategy(shadow, base_cfg)))
last_audit_keys = load_audit_keys(SIGNAL_AUDIT_FILE)

print("[Signal] Loading BTC history...")
df5 = load_symbol("btcusdt")
print(f"[Signal] {len(df5)} 5m candles")
print("\n[Signal] Starting BTC dual-strategy loop (every 15s)...")

while True:
    try:
        live = fetch_live_klines()
        df5 = merge_live(df5, live)
        signals = {}
        for strategy in strategies:
            r = strategy.predict(df5)
            if r:
                signals[strategy.id] = r
                print(
                    f"  {r['time']} {strategy.id} avg={r['avg_prob']:.3f} "
                    f"RSI={r['rsi_value']:.0f} {status_text(r)}"
                )
        if signals:
            with open(SIGNAL_FILE, "w", encoding="utf-8") as f:
                json.dump(signals, f, ensure_ascii=False)
            for strategy_id, r in signals.items():
                key = f"signal_snapshot|{strategy_id}|{r.get('time')}"
                if key not in last_audit_keys:
                    append_jsonl(SIGNAL_AUDIT_FILE, {
                        "event": "signal_snapshot",
                        "serverTime": int(time.time() * 1000),
                        **r,
                    })
                    last_audit_keys.add(key)
        for shadow_meta, shadow_strategy in shadow_strategies:
            r = shadow_strategy.predict(df5)
            if not r:
                continue
            r["shadow"] = True
            r["shadow_base_strategy"] = shadow_meta["base"]
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
                    f"  {r['time']} {r['strategy_id']} shadow avg={r['avg_prob']:.3f} "
                    f"RSI={r['rsi_value']:.0f} {status_text(r)}"
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
        if os.environ.get("SIGNAL_ONCE") == "1":
            break
    except Exception as e:
        print(f"Error: {e}")
        if os.environ.get("SIGNAL_ONCE") == "1":
            raise
    time.sleep(15)
