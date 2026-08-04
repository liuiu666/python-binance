# -*- coding: utf-8 -*-
"""
LLM 方向预测常驻服务
====================
独立常驻进程, 每10分钟预测一次 BTC 未来10分钟方向(UP/DOWN)。

数据来源(优先读 data/ CSV, 缺失时直连币安补):
  - data/btcusdt_1m.csv (OHLCV)
  - data/btcusdt_taker.csv (taker buySellRatio/buyVol/sellVol)
  - data/current_price.json (最新价)

输出:
  - data/signal_audit.jsonl  (event="llm_prediction", 和现有信号审计统一格式)
  - data/llm_predictions.jsonl (独立完整记录, 含 reasoning)

不碰实盘, 纯预测记录。
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ---------- 路径 ----------
APP_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("DATA_DIR", APP_DIR / "data"))
CSV_1M = DATA_DIR / "btcusdt_1m.csv"
CSV_TAKER = DATA_DIR / "btcusdt_taker.csv"
CURRENT_PRICE = DATA_DIR / "current_price.json"
SIGNAL_AUDIT = DATA_DIR / "signal_audit.jsonl"
LLM_LOG = DATA_DIR / "llm_predictions.jsonl"

# ---------- 配置 ----------
HORIZON_MIN = 10
MAX_STALE_SEC = 300  # CSV超过5分钟算太旧, 直连币安补
KL_URL = "https://fapi.binance.com/fapi/v1/klines"


def load_glm_config():
    """从 prod_config 明文读取连接参数，避免源码与实盘配置出现两份不同来源。"""
    path = DATA_DIR / "prod_config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        cfg = raw.get("BTC_10min_LLM_GLM52", {})
    except (OSError, ValueError, TypeError):
        cfg = {}
    return {
        "url": str(cfg.get("llm_api_url") or "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions"),
        "key": str(cfg.get("llm_api_key") or ""),
        "model": str(cfg.get("llm_model") or "glm-5.2"),
        "interval_sec": max(5, int(cfg.get("llm_interval_sec") or 600)),
    }


_GLM_CONFIG = load_glm_config()
PREDICT_INTERVAL_SEC = _GLM_CONFIG["interval_sec"]
GLM_URL = _GLM_CONFIG["url"]
GLM_KEY = _GLM_CONFIG["key"]
GLM_MODEL = _GLM_CONFIG["model"]

# 数据采集服务器 (优先从服务器拉OHLC, 保证和现有策略数据源一致)
COLLECTOR_URL = os.environ.get("COLLECTOR_URL", "http://115.190.218.128:3000")


# ---------- 数据加载 ----------
def load_1m_csv(n=200):
    """读 data/btcusdt_1m.csv 最后 n 根"""
    if not CSV_1M.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(CSV_1M, parse_dates=["open_time"])
        df = df.rename(columns={"open_time": "t", "open": "o", "high": "h",
                                "low": "l", "close": "c", "volume": "v"})
        df = df.sort_values("t").tail(n).reset_index(drop=True)
        return df
    except Exception as e:
        print(f"[warn] 读 {CSV_1M} 失败: {e}")
        return pd.DataFrame()


def load_taker_csv(n=200):
    """读 data/btcusdt_taker.csv, 返回带 buyVol/sellVol 的 df"""
    if not CSV_TAKER.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(CSV_TAKER, parse_dates=["timestamp"])
        df = df.rename(columns={"timestamp": "t"})
        df = df.sort_values("t").tail(n).reset_index(drop=True)
        return df
    except Exception as e:
        print(f"[warn] 读 {CSV_TAKER} 失败: {e}")
        return pd.DataFrame()


def fetch_binance_klines(interval, limit=200):
    """直连币安拉K线 (CSV太旧时的兜底)"""
    r = requests.get(KL_URL, params={"symbol": "BTCUSDT", "interval": interval, "limit": limit}, timeout=15)
    r.raise_for_status()
    raw = r.json()
    rows = []
    for k in raw:
        rows.append({
            "t": pd.Timestamp(k[0], unit="ms", tz="UTC"),
            "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
            "c": float(k[4]), "v": float(k[5]),
            "tb": float(k[9]),  # takerBuyBase
        })
    return pd.DataFrame(rows)


def fetch_collector_klines(interval="1m", limit=200):
    """从采集服务器拉K线 (OHLC, 和现有策略数据源一致)"""
    try:
        r = requests.get("%s/api/candles" % COLLECTOR_URL,
                         params={"interval": interval, "limit": limit}, timeout=10)
        r.raise_for_status()
        data = r.json()
        candles = data.get("candles", data) if isinstance(data, dict) else data
        rows = []
        for c in candles:
            rows.append({
                "t": pd.Timestamp(c["timeMs"], unit="ms", tz="UTC"),
                "o": float(c["open"]), "h": float(c["high"]),
                "l": float(c["low"]), "c": float(c["close"]),
                "v": float(c.get("volume", 0)),
            })
        return pd.DataFrame(rows)
    except Exception as e:
        print("[warn] 服务器K线拉取失败: %s" % e)
        return pd.DataFrame()


def fetch_collector_price():
    """从采集服务器拉最新价"""
    try:
        r = requests.get("%s/api/price" % COLLECTOR_URL, timeout=5)
        r.raise_for_status()
        d = r.json()
        return float(d["price"]), d.get("time")
    except Exception:
        return None, None


def fetch_binance_taker(limit=200):
    """直连币安拉 taker (1m K线自带 takerBuyBase)"""
    df = fetch_binance_klines("1m", limit)
    if df.empty:
        return df
    df["ts"] = df["v"] - df["tb"]
    df["dl"] = df["tb"] - df["ts"]
    return df


def get_fresh_data(n=200):
    """
    1m K线(含OHLCV+taker)全部从币安拉(数据最全)。
    用采集服务器的 /api/price 做价格一致性校验。
    """
    now = datetime.now(timezone.utc)

    # 币安 1m K线 (含 takerBuyBase)
    df = fetch_binance_taker(n)
    if df.empty:
        print("[warn] 币安数据拉取失败")
        return pd.DataFrame(), "none"

    # 剔除未收盘
    last = df["t"].iloc[-1]
    if last.tzinfo is None:
        last = last.tz_localize("UTC")
    if (now - last).total_seconds() < 60:
        df = df.iloc[:-1]

    # 用服务器价格校验 (确认数据源一致)
    srv_px, _ = fetch_collector_price()
    binance_px = float(df["c"].iloc[-1])
    if srv_px:
        diff_bps = abs(srv_px - binance_px) / binance_px * 10000
        src = "binance(srv校验%.1fbp)" % diff_bps
    else:
        src = "binance"

    print("[data] %s | 1m=%d根 | 最新=%s | 币安$%.1f 服务器$%s" % (
        src, len(df), df["t"].iloc[-1], binance_px,
        "%.1f" % srv_px if srv_px else "?"))
    return df.tail(n).reset_index(drop=True), src


def get_htf_data(tf, limit=200):
    """拉高周期(5m/15m/1h)数据"""
    now = datetime.now(timezone.utc)
    df = fetch_binance_klines(tf, limit)
    if df.empty:
        return df
    psec = {"5m": 300, "15m": 900, "1h": 3600}[tf]
    last_t = df["t"].iloc[-1]
    if last_t.tzinfo is None:
        last_t = last_t.tz_localize("UTC")
    if (now - last_t).total_seconds() < psec:
        df = df.iloc[:-1]
    df["ts"] = df["v"] - df["tb"]
    df["dl"] = df["tb"] - df["ts"]
    return df


# ---------- 指标计算 ----------
def rsi(s, p):
    d = s.diff()
    g = d.where(d > 0, 0).rolling(p).mean()
    l = (-d.where(d < 0, 0)).rolling(p).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))

def ema(s, p):
    return s.ewm(span=p, adjust=False).mean()

def macd(s):
    m = ema(s, 12) - ema(s, 26)
    sig = m.ewm(span=9, adjust=False).mean()
    return m, sig, m - sig

def compute_indicators(df):
    """在 df 上算全部指标(原地修改)"""
    df = df.copy()
    df["rsi7"] = rsi(df["c"], 7)
    df["rsi14"] = rsi(df["c"], 14)
    m, s, h = macd(df["c"])
    df["macd_m"], df["macd_s"], df["macd_h"] = m, s, h
    df["e20"] = ema(df["c"], 20)
    df["e50"] = ema(df["c"], 50)
    df["e100"] = ema(df["c"], 100)
    df["ma20"] = df["c"].rolling(20).mean()
    df["bm"] = df["c"].rolling(20).mean()
    sd = df["c"].rolling(20).std()
    df["bu"] = df["bm"] + 2 * sd
    df["bl"] = df["bm"] - 2 * sd
    pc = df["c"].shift(1)
    tr = pd.concat([(df["h"] - df["l"]).abs(), (df["h"] - pc).abs(), (df["l"] - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    df["atr20"] = tr.rolling(20).mean()
    tp = (df["h"] + df["l"] + df["c"]) / 3
    df["vwap"] = (tp * df["v"]).cumsum() / df["v"].cumsum()
    df["obv"] = (np.sign(df["c"].diff()) * df["v"]).cumsum()
    if "ts" not in df.columns:
        df["ts"] = df["v"] - df.get("tb", df["v"] * 0.5)
    if "dl" not in df.columns:
        df["dl"] = df.get("tb", df["v"] * 0.5) - df["ts"]
    return df


# ---------- 提示词构建 ----------
def jf(a, f=".2f"):
    return ", ".join(format(v, f) for v in a)

def fmt_k(d, n=100):
    d = d.tail(n)
    lines = []
    for _, r in d.iterrows():
        tb = r.get("tb", 0)
        lines.append("{}  O:{:.1f} H:{:.1f} L:{:.1f} C:{:.1f} V:{:.1f} TB:{:.1f}".format(
            r["t"], r["o"], r["h"], r["l"], r["c"], r["v"], tb))
    return "\n".join(lines)

def mom_line(tf, k):
    return "RSI7 (%s): %.1f | last 10: %s\nRSI14 (%s): %.1f | last 10: %s\nMACD (%s): hist=%+.1f | last 10: %s" % (
        tf, k["rsi7"].iloc[-1], jf(k["rsi7"].tail(10).values, ".1f"),
        tf, k["rsi14"].iloc[-1], jf(k["rsi14"].tail(10).values, ".1f"),
        tf, k["macd_h"].iloc[-1], jf(k["macd_h"].tail(10).values, "+.1f"))

def of_line(tf, k, px):
    cvd_last = k["dl"].tail(20).values[-1] * px / 1e6
    cvd10 = k["dl"].tail(10).values * px / 1e6
    cvd_sum = k["dl"].tail(20).sum() * px / 1e6
    tb_sum = k["tb"].tail(20).sum()
    ts_sum = k["ts"].tail(20).sum()
    ratio = tb_sum / ts_sum if ts_sum > 0 else 0
    tb10 = k["tb"].tail(10).values
    ts10 = k["ts"].tail(10).values
    ratios = np.divide(
        tb10,
        ts10,
        out=np.zeros_like(ts10, dtype=float),
        where=ts10 > 0,
    )
    return "CVD (%s): %+.2fM | last 10: %sM | Cum20: %+.2fM\nTaker (%s): Buy+%.2fM Sell-%.2fM | Ratio %.2fx | last 10: %sx" % (
        tf, cvd_last, jf(cvd10, "+.2f"), cvd_sum,
        tf, tb_sum * px / 1e6, ts_sum * px / 1e6, ratio, jf(ratios, ".2f"))

def vol_line(tf, k):
    h5 = k["h"].tail(5).values
    l5 = k["l"].tail(5).values
    vr = (h5.max() - l5.min()) / l5.min() * 100
    return "ATR14 (%s): %.1f | 20avg: %.1f | 5bar: %.3f%%" % (tf, k["atr"].iloc[-1], k["atr20"].iloc[-1], vr)

def trend_block(name, k):
    px_k = float(k["c"].iloc[-1])
    arr = "多头" if k["e20"].iloc[-1] > k["e50"].iloc[-1] > k["e100"].iloc[-1] else (
        "空头" if k["e20"].iloc[-1] < k["e50"].iloc[-1] < k["e100"].iloc[-1] else "交叉")
    return "=== %s TREND ===\nPrice: $%s | EMA20/50/100: %.0f/%.0f/%.0f (%s)\nMA20: %.0f | BOLL: U%.0f M%.0f L%.0f\nVWAP: %.1f | OBV: %s" % (
        name, format(px_k, ",.1f"), k["e20"].iloc[-1], k["e50"].iloc[-1], k["e100"].iloc[-1], arr,
        k["ma20"].iloc[-1], k["bu"].iloc[-1], k["bm"].iloc[-1], k["bl"].iloc[-1],
        k["vwap"].iloc[-1], format(int(k["obv"].iloc[-1]), ","))

def build_prompt(k1, k5, k15, k1h, px):
    now = datetime.now(timezone.utc)
    mom = "\n".join(mom_line(tf, k) for tf, k in [("1m", k1), ("5m", k5), ("15m", k15), ("1h", k1h)])
    of = "\n".join(of_line(tf, k, px) for tf, k in [("1m", k1), ("5m", k5), ("15m", k15), ("1h", k1h)])
    vol = "\n".join(vol_line(tf, k) for tf, k in [("1m", k1), ("5m", k5), ("15m", k15), ("1h", k1h)])
    return """=== PREDICTION OBJECTIVE ===
Predict the price direction of BTCUSDT over the NEXT 10 MINUTES.

Definitions (measured at the close of the 10-minute window):
- UP:    future price > current price  (price goes up by ANY amount)
- DOWN:  future price <= current price (price goes down or stays flat)

This is a STRICT binary choice. You MUST choose either UP or DOWN. No FLAT.

=== SESSION CONTEXT ===
Current UTC time: {now}

=== MARKET DATA ===
BTC/USDT  |  Price: ${px}

=== K-LINE DATA (1m x100) ===
{k1str}

=== MOMENTUM (1m + 5m + 15m + 1h) ===
{mom}

=== ORDER FLOW (1m + 5m + 15m + 1h) ===
{of}

=== VOLATILITY (1m + 5m + 15m + 1h) ===
{vol}

{t5}

{t15}

{t1h}

=== OUTPUT FORMAT ===
Respond with ONLY a JSON object:
{{"direction":"UP或DOWN","confidence":0.0到1.0,"reason":"一句话理由"}}""".format(
        now=now.strftime("%Y-%m-%d %H:%M:%S"),
        px="{:,.1f}".format(px),
        k1str=fmt_k(k1, 100),
        mom=mom, of=of, vol=vol,
        t5=trend_block("5M", k5), t15=trend_block("15M", k15), t1h=trend_block("1H", k1h))


# ---------- 模型调用 ----------
def call_glm(prompt):
    payload = json.dumps({
        "model": GLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 8000,
        "temperature": 0.2,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(GLM_URL, data=payload, headers={
        "Authorization": "Bearer %s" % GLM_KEY, "Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=180)
    data = json.loads(resp.read())
    msg = data["choices"][0]["message"]
    return msg.get("content", ""), msg.get("reasoning_content", "")


def parse_response(content):
    """解析完整 JSON 对象；格式错误或缺少合法方向时拒绝记录预测。"""
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise ValueError("llm_response_not_json") from exc
    if not isinstance(parsed, dict) or parsed.get("direction") not in ("UP", "DOWN"):
        raise ValueError("llm_direction_missing")
    pred = parsed["direction"]
    conf = float(parsed.get("confidence", 0.5))
    if not math.isfinite(conf):
        raise ValueError("llm_confidence_invalid")
    conf = min(max(conf, 0.0), 1.0)
    reason = str(parsed.get("reason", ""))[:200]
    return pred, conf, reason


# ---------- 写审计 ----------
def append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


def settle_prediction(pred_time_str, pred_price, target_ts):
    """等目标时间到达后, 拉真实价格结算"""
    # 这个函数在主循环里被定时调用(不阻塞)
    pass  # 结算逻辑在主循环里单独实现


# ---------- 主循环 ----------
def run_once():
    """执行一次预测"""
    t0 = time.time()
    now = datetime.now(timezone.utc)
    print("\n" + "=" * 60)
    print("[预测] %s" % now.strftime("%Y-%m-%d %H:%M:%S UTC"))

    # 拉数据
    k1_raw, src = get_fresh_data(200)
    if k1_raw.empty or len(k1_raw) < 30:
        print("[skip] 数据不足 (%d行)" % len(k1_raw))
        return None

    k1 = compute_indicators(k1_raw)
    px = float(k1["c"].iloc[-1])
    print("[data] 来源=%s | 1m=%d根 | 最新=%s | 价格=$%.1f" % (
        src, len(k1), k1["t"].iloc[-1], px))

    # 高周期 (始终直连币安, 因为 data/ 没有5m/15m/1h CSV)
    try:
        k5 = compute_indicators(get_htf_data("5m"))
        k15 = compute_indicators(get_htf_data("15m"))
        k1h = compute_indicators(get_htf_data("1h"))
    except Exception as e:
        print("[error] 高周期数据拉取失败: %s" % e)
        return None

    # 构建提示词
    prompt = build_prompt(k1, k5, k15, k1h, px)
    print("[prompt] %d字符" % len(prompt))

    # 调模型
    try:
        content, reasoning = call_glm(prompt)
    except Exception as e:
        print("[error] 模型调用失败: %s" % e)
        return None

    pred, conf, reason = parse_response(content)
    t1 = time.time()
    print("[model] %s (conf %.2f) | 耗时%.1fs | 思考%d字" % (pred, conf, t1 - t0, len(reasoning)))
    print("[reason] %s" % reason[:120])

    # 计算目标结算时间
    pred_minute = k1["t"].iloc[-1]
    if hasattr(pred_minute, "to_pydatetime"):
        pred_minute = pred_minute.to_pydatetime()
    if pred_minute.tzinfo is None:
        pred_minute = pred_minute.replace(tzinfo=timezone.utc)
    target_time = pred_minute + timedelta(minutes=HORIZON_MIN)

    record = {
        "event": "llm_prediction",
        "serverTime": int(now.timestamp() * 1000),
        "time": now.isoformat(),
        "strategy_id": "BTC_LLM_GLM52",
        "signal": pred,
        "direction": pred,
        "confidence": conf,
        "reason": reason,
        "price": px,
        "price_at_prediction": px,
        "pred_minute": pred_minute.isoformat(),
        "prediction_target_time": target_time.isoformat(),
        "horizon_min": HORIZON_MIN,
        "data_source": src,
        "model": GLM_MODEL,
        "model_latency_sec": round(t1 - t0, 1),
        "prompt_chars": len(prompt),
        "reasoning": reasoning[:2000],  # 截断, 避免太大
        "content": content,
    }

    # 写 signal_audit.jsonl (和现有格式统一)
    append_jsonl(SIGNAL_AUDIT, record)
    # 写独立日志
    append_jsonl(LLM_LOG, record)
    print("[done] 已写 signal_audit.jsonl + llm_predictions.jsonl")
    return record


def settle_loop():
    """扫描 llm_predictions.jsonl, 对到期的预测结算"""
    if not LLM_LOG.exists():
        return
    now = datetime.now(timezone.utc)
    pending = []
    predictions = []
    settled_keys = set()
    with open(LLM_LOG, "r", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
                if r.get("event") == "llm_prediction_settled":
                    # 结算事件使用预测分钟作为稳定键，进程重启后仍能去重。
                    settled_keys.add((r.get("strategy_id"), r.get("pred_minute")))
                elif r.get("event") == "llm_prediction":
                    predictions.append(r)
            except Exception:
                continue

    pending_keys = set()
    for r in predictions:
        key = (r.get("strategy_id"), r.get("pred_minute"))
        if key in settled_keys or key in pending_keys:
            continue
        tgt = r.get("prediction_target_time")
        if not tgt:
            continue
        try:
            if datetime.fromisoformat(tgt) <= now:
                pending.append(r)
                pending_keys.add(key)
        except (TypeError, ValueError):
            continue

    if not pending:
        return

    print("[settle] %d 条到期预测待结算" % len(pending))
    for r in pending:
        try:
            tgt = datetime.fromisoformat(r["prediction_target_time"])
            tgt_ts = int(tgt.timestamp() * 1000)
            # 拉目标时间的1m收盘价
            resp = requests.get(KL_URL, params={"symbol": "BTCUSDT", "interval": "1m",
                                                "startTime": str(tgt_ts), "limit": 1}, timeout=10)
            kl = resp.json()
            if not kl:
                continue
            settle_price = float(kl[0][4])
            px0 = r["price_at_prediction"]
            chg = (settle_price - px0) / px0
            actual = "UP" if chg > 0 else "DOWN"
            correct = (r["direction"] == actual)

            settle_record = {
                "event": "llm_prediction_settled",
                "serverTime": int(now.timestamp() * 1000),
                "strategy_id": r["strategy_id"],
                "pred_minute": r["pred_minute"],
                "direction": r["direction"],
                "actual_direction": actual,
                "is_correct": correct,
                "price_at_prediction": px0,
                "price_at_target": settle_price,
                "change_pct": round(chg * 100, 3),
                "confidence": r.get("confidence", 0),
            }
            append_jsonl(SIGNAL_AUDIT, settle_record)
            append_jsonl(LLM_LOG, settle_record)
            print("[settle] %s pred=%s actual=%s %s (%+.3f%%)" % (
                r["pred_minute"], r["direction"], actual,
                "✅" if correct else "❌", chg * 100))
        except Exception as e:
            print("[settle] 结算失败: %s" % e)


def main():
    print("=" * 60)
    print("LLM 方向预测服务启动")
    print("间隔: %ds | 模型: %s | 输出: %s" % (PREDICT_INTERVAL_SEC, GLM_MODEL, SIGNAL_AUDIT))
    print("=" * 60)

    while True:
        try:
            # 先结算到期预测
            settle_loop()
            # 执行一次预测
            run_once()
        except Exception as e:
            print("[error] %s" % e)

        print("[sleep] 等待 %d 秒..." % PREDICT_INTERVAL_SEC)
        time.sleep(PREDICT_INTERVAL_SEC)


if __name__ == "__main__":
    main()
