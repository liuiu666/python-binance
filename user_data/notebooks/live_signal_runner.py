"""Live signal runner for the MeanReversion10mStrategy A3 signal.

Polls Binance USDT-M perpetual futures 1m klines, computes ema120_dev /
vol_z40 / rv_z, fires DingTalk notifications when signal triggers, and
issues a settlement notification 10 minutes later.

Indicators are duplicated locally to avoid importing the freqtrade strategy
class (which carries heavy IStrategy machinery). The mathematical
definitions MUST stay in sync with
    user_data/strategies/MeanReversion10mStrategy.py

Run:
    .venv\\Scripts\\python -u user_data/notebooks/live_signal_runner.py
"""
import json
import os
import sys
import time
import subprocess
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests


# ============================== Config ==============================

DINGTALK_WEBHOOK = "https://oapi.dingtalk.com/robot/send?access_token=941107d04389a231acc78a135a4fffd73551c6b3fae825924b0348dab0c684df"
KEYWORD = "666"
PROXIES = {
    "http": "http://127.0.0.1:7897",
    "https": "http://127.0.0.1:7897",
}
PID_FILE = "user_data/notebooks/live_signal_runner.pid"
ACTIVE_TRADES_FILE = "user_data/notebooks/active_trades.json"

# fapi USDT-M perpetual klines endpoint -- matches data source used in backtest.
FAPI_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
SYMBOL = "BTCUSDT"
INTERVAL = "1m"
KLINES_PER_REQ = 1500          # Binance fapi maximum

# Strategy parameters (must mirror MeanReversion10mStrategy)
EMA_SPAN = 120
ATR_WIN = 120
VOL_Z_WIN = 40
RV_WIN = 60
RV_BASELINE_WIN = 60 * 24
EMA_DEV_QUANTILE_WIN = 60 * 24 * 14   # 14 days
EMA_DEV_LOW_Q = 0.10
EMA_DEV_HIGH_Q = 0.90
VOL_Z_THRESHOLD = 1.0
RV_Z_BAND = 1.0
EXPIRY_MINUTES = 10

# History needed at startup (max of all rolling/EWMA spans + headroom)
WARMUP_BARS = max(EMA_DEV_QUANTILE_WIN, RV_BASELINE_WIN + RV_WIN, EMA_SPAN * 5) + 200

# Binary-option payout for stats display
PAYOUT_WIN = 4.0
PAYOUT_LOSS = -5.0
STAKE = 5.0


# ============================== Single-instance ==============================

def enforce_single_instance():
    current_pid = os.getpid()
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, 'r', encoding='utf-8') as f:
                old_pid = int(f.read().strip())
            if old_pid != current_pid:
                cmd = f'wmic process where "ProcessId={old_pid}" get CommandLine, Name'
                out = subprocess.check_output(cmd, shell=True).decode('gbk', errors='ignore')
                if "python" in out.lower() and "live_signal_runner.py" in out.lower():
                    print(f"[INFO] Existing live_signal_runner detected (PID={old_pid}); terminating.")
                    subprocess.call(f"taskkill /F /PID {old_pid}", shell=True,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    time.sleep(1)
        except Exception as e:
            print(f"[WARN] single-instance check failed: {e}")
    try:
        os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
        with open(PID_FILE, 'w', encoding='utf-8') as f:
            f.write(str(current_pid))
    except Exception as e:
        print(f"[WARN] could not write PID file: {e}")


# ============================== Active trades I/O ==============================

def load_active_trades():
    if not os.path.exists(ACTIVE_TRADES_FILE):
        return []
    try:
        with open(ACTIVE_TRADES_FILE, 'r', encoding='utf-8') as f:
            trades = json.load(f)
        for t in trades:
            t['entry_time'] = pd.Timestamp(t['entry_time'])
            t['expiry_time'] = pd.Timestamp(t['expiry_time'])
        print(f"[INFO] loaded {len(trades)} pending trades from {ACTIVE_TRADES_FILE}")
        return trades
    except Exception as e:
        print(f"[WARN] active trades load failed: {e}")
        return []


def save_active_trades(trades):
    try:
        out = []
        for t in trades:
            st = t.copy()
            st['entry_time'] = t['entry_time'].isoformat()
            st['expiry_time'] = t['expiry_time'].isoformat()
            out.append(st)
        with open(ACTIVE_TRADES_FILE, 'w', encoding='utf-8') as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] active trades save failed: {e}")


# ============================== DingTalk ==============================

def send_dingtalk(title: str, content: str):
    payload = {"msgtype": "text",
               "text": {"content": f"[{KEYWORD} {title}]\n{content}"}}
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.post(DINGTALK_WEBHOOK, json=payload, headers=headers,
                          proxies={"http": None, "https": None}, timeout=5)
        if r.status_code != 200:
            print(f"[WARN] DingTalk HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[WARN] DingTalk send failed: {e}")


def notify_entry(direction: str, ema_dev: float, vol_z: float, rv_z: float,
                 entry_price: float, entry_time: pd.Timestamp):
    cn_entry = entry_time + pd.Timedelta(hours=8)
    cn_expiry = cn_entry + pd.Timedelta(minutes=EXPIRY_MINUTES)
    dir_label = "看涨 (Call)" if direction == 'CALL' else "看跌 (Put)"
    content = (
        f"类型: 下单通知 (A3 均值回归)\n"
        f"方向: {dir_label}\n"
        f"下单价格: {entry_price:.2f} USDT\n"
        f"ema120_dev: {ema_dev:+.2f}  (距 EMA120 / ATR120)\n"
        f"vol_z40   : {vol_z:+.2f}    (>1: 异常放量)\n"
        f"rv_z      : {rv_z:+.2f}     (在 -1~+1 内)\n"
        f"下单时间: {cn_entry.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)\n"
        f"预计结算: {cn_expiry.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)\n"
        f"参考: 1y OOS 胜率 57.97% / Wilson LB 56.66%"
    )
    print("\n" + "=" * 60)
    print(f"[SIGNAL] A3 {direction} 触发")
    print(content)
    print("=" * 60)
    send_dingtalk("二元期权-下单 (A3)", content)


def notify_settlement(trade: dict, settle_price: float):
    direction = trade['direction']
    entry_price = trade['entry_price']
    cn_entry = trade['entry_time'] + pd.Timedelta(hours=8)
    cn_expiry = trade['expiry_time'] + pd.Timedelta(hours=8)
    if direction == 'CALL':
        is_win = settle_price > entry_price
    else:
        is_win = settle_price < entry_price
    pnl = PAYOUT_WIN if is_win else PAYOUT_LOSS
    result_str = "赢 (Profit)" if is_win else "输 (Loss)"
    dir_label = "看涨 (Call)" if direction == 'CALL' else "看跌 (Put)"
    content = (
        f"类型: 结算通知 (A3)\n"
        f"方向: {dir_label}\n"
        f"下单价格: {entry_price:.2f} USDT\n"
        f"结算价格: {settle_price:.2f} USDT  (价差: {settle_price - entry_price:+.2f})\n"
        f"结算结果: {result_str}\n"
        f"假设 5U 投注, 1.8x 赔率: {pnl:+.2f} U\n"
        f"下单时间: {cn_entry.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"结算时间: {cn_expiry.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    print("\n" + "=" * 60)
    print(f"[SETTLE] {direction} {'WIN' if is_win else 'LOSS'}")
    print(content)
    print("=" * 60)
    send_dingtalk("二元期权-结算 (A3)", content)


# ============================== Klines ==============================

def fetch_klines(start_ms: int = None, end_ms: int = None, limit: int = KLINES_PER_REQ):
    params = {"symbol": SYMBOL, "interval": INTERVAL, "limit": limit}
    if start_ms is not None:
        params["startTime"] = start_ms
    if end_ms is not None:
        params["endTime"] = end_ms
    r = requests.get(FAPI_KLINES_URL, params=params, proxies=PROXIES, timeout=15)
    r.raise_for_status()
    raw = r.json()
    if not raw:
        return pd.DataFrame(columns=['date', 'open', 'high', 'low', 'close', 'volume'])
    df = pd.DataFrame(raw, columns=[
        'open_time', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'qav', 'num_trades', 'tbb', 'tbq', 'ignore'])
    df['date'] = pd.to_datetime(df['open_time'], unit='ms', utc=True)
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce').astype(float)
    return df[['date', 'open', 'high', 'low', 'close', 'volume']]


def bootstrap_history(target_bars: int) -> pd.DataFrame:
    """Pull `target_bars` of 1m history paginated backward from now."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    end_ms = now_ms
    chunks = []
    fetched = 0
    while fetched < target_bars:
        df = fetch_klines(end_ms=end_ms, limit=KLINES_PER_REQ)
        if df.empty:
            break
        chunks.append(df)
        fetched += len(df)
        end_ms = int(df['date'].iloc[0].timestamp() * 1000) - 1
        print(f"  bootstrapped {fetched}/{target_bars} bars (oldest={df['date'].iloc[0]})")
        time.sleep(0.25)  # courtesy delay
        if len(df) < KLINES_PER_REQ:
            break
    full = pd.concat(chunks, ignore_index=True).drop_duplicates(subset='date')
    full = full.sort_values('date').reset_index(drop=True)
    return full.tail(target_bars).reset_index(drop=True)


# ============================== Indicators (mirror strategy) ==============================

def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    rng = out['high'] - out['low']
    atr120 = rng.rolling(ATR_WIN).mean()

    ema120 = out['close'].ewm(span=EMA_SPAN, adjust=False).mean()
    out['ema120_dev'] = (out['close'] - ema120) / atr120

    vmed40 = out['volume'].rolling(VOL_Z_WIN).median()
    vstd40 = out['volume'].rolling(VOL_Z_WIN).std()
    out['vol_z40'] = (out['volume'] - vmed40) / vstd40

    logret1 = np.log(out['close']).diff()
    rv60 = logret1.rolling(RV_WIN).std()
    out['rv_z'] = ((rv60 - rv60.rolling(RV_BASELINE_WIN).mean())
                   / rv60.rolling(RV_BASELINE_WIN).std())

    ev = out['ema120_dev']
    out['ev_q_lo'] = ev.rolling(EMA_DEV_QUANTILE_WIN, min_periods=2 * 60 * 24)\
                       .quantile(EMA_DEV_LOW_Q)
    out['ev_q_hi'] = ev.rolling(EMA_DEV_QUANTILE_WIN, min_periods=2 * 60 * 24)\
                       .quantile(EMA_DEV_HIGH_Q)

    base = ((out['vol_z40'] > VOL_Z_THRESHOLD)
            & (out['rv_z'] > -RV_Z_BAND)
            & (out['rv_z'] < RV_Z_BAND)
            & out['ev_q_lo'].notna()
            & out['ev_q_hi'].notna())
    out['signal_call'] = base & (out['ema120_dev'] <= out['ev_q_lo'])
    out['signal_put'] = base & (out['ema120_dev'] >= out['ev_q_hi'])
    return out


# ============================== Main loop ==============================

def main():
    enforce_single_instance()
    print("=" * 60)
    print("BTC 10-min binary option signal runner (A3 mean-reversion)")
    print(f"DingTalk webhook: {DINGTALK_WEBHOOK[:60]}...")
    print(f"Keyword: {KEYWORD}")
    print(f"Proxy:   {PROXIES['https']}")
    print(f"Source:  {FAPI_KLINES_URL}")
    print("=" * 60)

    print(f"\nBootstrapping {WARMUP_BARS} bars of 1m history (~{WARMUP_BARS/60/24:.1f} days)...")
    cache = bootstrap_history(WARMUP_BARS)
    if len(cache) < WARMUP_BARS // 2:
        print(f"[FATAL] only {len(cache)} bars fetched, aborting")
        sys.exit(1)
    print(f"Cache populated: {len(cache):,} bars from {cache['date'].iloc[0]} to {cache['date'].iloc[-1]}")

    active_trades = load_active_trades()
    last_processed = None

    while True:
        try:
            # Pull last 100 candles to refresh tail
            tail = fetch_klines(limit=100)
            cache = (pd.concat([cache, tail], ignore_index=True)
                     .drop_duplicates(subset='date', keep='last')
                     .sort_values('date')
                     .reset_index(drop=True))
            # Trim to warmup window plus a little headroom
            if len(cache) > WARMUP_BARS + 1000:
                cache = cache.iloc[-(WARMUP_BARS + 1000):].reset_index(drop=True)

            sig_df = compute_signals(cache)
            # The last row is the currently-forming candle; the previous row
            # is the most recently *closed* 1m candle.
            if len(sig_df) < 2:
                time.sleep(15); continue
            closed = sig_df.iloc[-2]
            bar_time = closed['date']

            # ---------- 1. settle expired trades ----------
            still_active = []
            changed = False
            lookup = sig_df.set_index('date')
            for trade in active_trades:
                expiry = trade['expiry_time']
                if expiry in lookup.index:
                    settle_price = float(lookup.loc[expiry, 'close'])
                    notify_settlement(trade, settle_price)
                    changed = True
                elif expiry > sig_df['date'].max():
                    still_active.append(trade)
                else:
                    fallback = float(closed['close'])
                    print(f"[WARN] expiry {expiry} not in cache, settling at {fallback:.2f}")
                    notify_settlement(trade, fallback)
                    changed = True
            active_trades = still_active
            if changed:
                save_active_trades(active_trades)

            # ---------- 2. detect new entry on the freshly-closed bar ----------
            if last_processed is None:
                last_processed = bar_time
                cn = (bar_time + pd.Timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')
                print(f"[{datetime.now().strftime('%H:%M:%S')}] init -> last closed bar {cn} CN")
            elif bar_time > last_processed:
                last_processed = bar_time
                cn = (bar_time + pd.Timedelta(hours=8)).strftime('%Y-%m-%d %H:%M')
                ev = closed['ema120_dev']; vz = closed['vol_z40']; rz = closed['rv_z']
                qlo = closed['ev_q_lo']; qhi = closed['ev_q_hi']
                print(f"[{datetime.now().strftime('%H:%M:%S')}] bar {cn} CN  "
                      f"ema_dev={ev:+.2f} (q_lo={qlo:+.2f},q_hi={qhi:+.2f})  "
                      f"vol_z={vz:+.2f}  rv_z={rz:+.2f}")

                direction = None
                if bool(closed['signal_call']):
                    direction = 'CALL'
                elif bool(closed['signal_put']):
                    direction = 'PUT'

                if direction:
                    entry_price = float(closed['close'])
                    notify_entry(direction, float(ev), float(vz), float(rz),
                                 entry_price, bar_time)
                    expiry_time = bar_time + pd.Timedelta(minutes=EXPIRY_MINUTES)
                    active_trades.append({
                        'entry_time': bar_time,
                        'entry_price': entry_price,
                        'direction': direction,
                        'expiry_time': expiry_time,
                        'ema120_dev': float(ev),
                        'vol_z40': float(vz),
                        'rv_z': float(rz),
                    })
                    save_active_trades(active_trades)

            time.sleep(15)
        except KeyboardInterrupt:
            print("\nshutting down")
            break
        except Exception as e:
            print(f"[ERROR] loop iteration failed: {e}")
            time.sleep(15)


if __name__ == '__main__':
    main()
