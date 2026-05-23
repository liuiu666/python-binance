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

# Strategy parameters — A4: 多期 EMA 共振 + 信号分级（高质 / 普通）
EMA_SPANS = [30, 60, 120, 240]   # 4 个时间尺度
ATR_WIN = 120
VOL_Z_WIN = 40
RV_WIN = 60
RV_BASELINE_WIN = 60 * 24
EMA_DEV_QUANTILE_WIN = 60 * 24 * 14   # 14 days rolling rank window
# 高质量阈值：CALL q≤0.05 + 4 EMA 全合 / PUT q≥0.95 + ≥2 EMA
HQ_CALL_QLO = 0.05; HQ_CALL_K = 4
HQ_PUT_QHI  = 0.95; HQ_PUT_K  = 2
# 普通阈值：CALL q≤0.10 + 4 EMA / PUT q≥0.90 + ≥3 EMA
NORM_CALL_QLO = 0.10; NORM_CALL_K = 4
NORM_PUT_QHI  = 0.90; NORM_PUT_K  = 3
VOL_Z_THRESHOLD = 1.0
RV_Z_BAND = 1.0
EXPIRY_MINUTES = 10

# History needed at startup (max of all rolling/EWMA spans + headroom)
WARMUP_BARS = max(EMA_DEV_QUANTILE_WIN, RV_BASELINE_WIN + RV_WIN, max(EMA_SPANS) * 5) + 200

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


def notify_entry(direction: str, tier: str, dev_dict: dict, q_dict: dict,
                 vol_z: float, rv_z: float,
                 entry_price: float, entry_time: pd.Timestamp):
    cn_entry = entry_time + pd.Timedelta(hours=8)
    cn_expiry = cn_entry + pd.Timedelta(minutes=EXPIRY_MINUTES)
    dir_label = "看涨 (Call)" if direction == 'CALL' else "看跌 (Put)"
    tier_label = "[高质]" if tier == 'HQ' else "[普通]"
    suggested = "建议加大仓位（如 10U）" if tier == 'HQ' else "建议常规仓位（如 5U）"
    if tier == 'HQ':
        ref = ("参考: 高质 1y 回测 WR≈59% / Wilson LB≈57.3%" if direction == 'CALL'
               else "参考: 高质 1y 回测 WR≈58.3% / Wilson LB≈57.1%")
    else:
        ref = ("参考: 普通 1y 回测 WR≈58.0% / Wilson LB≈56.8%" if direction == 'CALL'
               else "参考: 普通 1y 回测 WR≈57.3% / Wilson LB≈56.3%")
    dev_str = "  ".join(f"ema{s}={dev_dict[s]:+.2f}(q={q_dict[s]:.3f})" for s in EMA_SPANS)
    content = (
        f"类型: 下单通知 (A4 多期共振) {tier_label}\n"
        f"方向: {dir_label}\n"
        f"下单价格: {entry_price:.2f} USDT\n"
        f"4 期偏离: {dev_str}\n"
        f"vol_z40 : {vol_z:+.2f}    (>1: 异常放量, 已通过)\n"
        f"rv_z    : {rv_z:+.2f}     (在 -1~+1 内, 已通过)\n"
        f"下单时间: {cn_entry.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)\n"
        f"预计结算: {cn_expiry.strftime('%Y-%m-%d %H:%M:%S')} (北京时间)\n"
        f"仓位建议: {suggested}\n"
        f"{ref}"
    )
    print("\n" + "=" * 60)
    print(f"[SIGNAL] A4 {tier} {direction} 触发")
    print(content)
    print("=" * 60)
    send_dingtalk(f"二元期权-下单 (A4 {tier})", content)


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
    out['atr'] = atr120

    # 4 个 EMA 偏离 + 14 天滚动分位（rank pct）
    min_q = 2 * 60 * 24   # 至少 2 天
    for s in EMA_SPANS:
        ema_s = out['close'].ewm(span=s, adjust=False).mean()
        out[f'd{s}'] = (out['close'] - ema_s) / atr120
        out[f'q{s}'] = (out[f'd{s}']
                        .rolling(EMA_DEV_QUANTILE_WIN, min_periods=min_q)
                        .rank(pct=True))

    # 兼容 console 输出：把 ema120_dev 暴露成 'ema120_dev'
    out['ema120_dev'] = out['d120']

    vmed40 = out['volume'].rolling(VOL_Z_WIN).median()
    vstd40 = out['volume'].rolling(VOL_Z_WIN).std()
    out['vol_z40'] = (out['volume'] - vmed40) / vstd40

    logret1 = np.log(out['close']).diff()
    rv60 = logret1.rolling(RV_WIN).std()
    out['rv_z'] = ((rv60 - rv60.rolling(RV_BASELINE_WIN).mean())
                   / rv60.rolling(RV_BASELINE_WIN).std())

    base = ((out['vol_z40'] > VOL_Z_THRESHOLD)
            & (out['rv_z'] > -RV_Z_BAND)
            & (out['rv_z'] < RV_Z_BAND)
            & out[f'q{EMA_SPANS[0]}'].notna())

    # 计数：当前每个 EMA 是否落在低/高极端
    hq_lo_count = sum((out[f'q{s}'] <= HQ_CALL_QLO).astype('Int64').fillna(0)
                       for s in EMA_SPANS)
    norm_lo_count = sum((out[f'q{s}'] <= NORM_CALL_QLO).astype('Int64').fillna(0)
                         for s in EMA_SPANS)
    hq_hi_count = sum((out[f'q{s}'] >= HQ_PUT_QHI).astype('Int64').fillna(0)
                       for s in EMA_SPANS)
    norm_hi_count = sum((out[f'q{s}'] >= NORM_PUT_QHI).astype('Int64').fillna(0)
                         for s in EMA_SPANS)
    out['hq_lo_count'] = hq_lo_count
    out['norm_lo_count'] = norm_lo_count
    out['hq_hi_count'] = hq_hi_count
    out['norm_hi_count'] = norm_hi_count

    out['hq_call'] = base & (hq_lo_count >= HQ_CALL_K)
    out['hq_put'] = base & (hq_hi_count >= HQ_PUT_K)
    # 普通信号：满足 NORM 阈值，但未达 HQ
    out['norm_call'] = base & (norm_lo_count >= NORM_CALL_K) & ~out['hq_call']
    out['norm_put'] = base & (norm_hi_count >= NORM_PUT_K) & ~out['hq_put']
    return out


# ============================== Main loop ==============================

def main():
    enforce_single_instance()
    print("=" * 60)
    print("BTC 10-min binary option signal runner (A4 多期共振, 分级)")
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
                price = float(closed['close'])
                atr_val = float(closed['atr']) if pd.notna(closed['atr']) else 0.0
                vz = float(closed['vol_z40']); rz = float(closed['rv_z'])
                # 收集 4 期偏离值与分位
                dev_dict = {s: float(closed[f'd{s}']) for s in EMA_SPANS}
                q_dict = {s: float(closed[f'q{s}']) if pd.notna(closed[f'q{s}']) else float('nan')
                          for s in EMA_SPANS}

                # 计算"距触发还要再跌/涨多少 %"：用最近 14 天的 d{s} 分布找阈值
                hist_tail = sig_df.tail(EMA_DEV_QUANTILE_WIN)

                def _need_pct(side: str, qcut: float, k_required: int) -> float:
                    """返回价格还需朝该方向移动的百分比（>0 表示还没触发）。
                    side='CALL' → 要价格下跌到使至少 k_required 个 EMA 的 d ≤ q-quantile
                    side='PUT'  → 要价格上涨到使至少 k_required 个 EMA 的 d ≥ q-quantile
                    取第 k_required 个最容易达到的 EMA 作为绑定约束。"""
                    margins = []
                    for s in EMA_SPANS:
                        d_now = dev_dict[s]
                        thresh = hist_tail[f'd{s}'].quantile(qcut)
                        if side == 'CALL':
                            # need d_now ≤ thresh（thresh 是低值），margin = d_now - thresh
                            margin = max(0.0, d_now - thresh)
                        else:
                            # need d_now ≥ thresh（thresh 是高值），margin = thresh - d_now
                            margin = max(0.0, thresh - d_now)
                        margins.append(margin)
                    margins.sort()
                    binding = margins[k_required - 1]   # 第 k 易达成的 EMA 决定门槛
                    if atr_val <= 0 or price <= 0:
                        return float('nan')
                    return binding * atr_val / price * 100.0

                pct_hq_call   = _need_pct('CALL', HQ_CALL_QLO,   HQ_CALL_K)
                pct_norm_call = _need_pct('CALL', NORM_CALL_QLO, NORM_CALL_K)
                pct_hq_put    = _need_pct('PUT',  HQ_PUT_QHI,    HQ_PUT_K)
                pct_norm_put  = _need_pct('PUT',  NORM_PUT_QHI,  NORM_PUT_K)

                vol_gap = VOL_Z_THRESHOLD - vz
                vol_ok = vz > VOL_Z_THRESHOLD
                rv_ok = abs(rz) < RV_Z_BAND
                vol_str = "放量已就位" if vol_ok else f"待放量(成交量z还差{vol_gap:+.2f})"
                rv_str = "波动正常" if rv_ok else f"波动异常(rv_z={rz:+.2f})"

                def _state(pct_hq: float, pct_norm: float, side_label: str, click_label: str) -> str:
                    """生成一边（做多/做空）的人话状态。"""
                    base_pass = vol_ok and rv_ok
                    if pct_norm <= 0.001:   # 普通条件已满足
                        if pct_hq <= 0.001:
                            base = f"【高质 + 普通 都已就位】"
                        else:
                            base = f"【普通已就位】(再朝{side_label}方向 {pct_hq:.2f}% 升级高质)"
                        if base_pass:
                            return f"{base} → 立即推送 {click_label}"
                        else:
                            problems = []
                            if not vol_ok: problems.append(f"等放量(还差{vol_gap:.2f})")
                            if not rv_ok: problems.append("波动异常")
                            return f"{base} → 但 {', '.join(problems)}，暂不下单"
                    else:
                        return (f"还远 — 价格再{side_label} {pct_norm:.2f}% 触发普通 / "
                                f"{pct_hq:.2f}% 触发高质")

                call_state = _state(pct_hq_call, pct_norm_call, "下跌", "看涨(CALL)")
                put_state  = _state(pct_hq_put,  pct_norm_put,  "上涨", "看跌(PUT)")

                # 简短偏离 (调试用，可删)
                dev_str = " ".join(f"e{s}={dev_dict[s]:+.1f}/q{q_dict[s]:.2f}" for s in EMA_SPANS)

                print(
                    f"\n[{datetime.now().strftime('%H:%M:%S')}] {cn} 北京  BTC={price:.1f}  "
                    f"过滤: {vol_str} | {rv_str}\n"
                    f"  ↑做多 CALL: {call_state}\n"
                    f"  ↓做空 PUT : {put_state}\n"
                    f"  (调试: {dev_str})"
                )

                # tier-aware signal dispatch
                tier = direction = None
                if bool(closed['hq_call']):
                    tier, direction = 'HQ', 'CALL'
                elif bool(closed['hq_put']):
                    tier, direction = 'HQ', 'PUT'
                elif bool(closed['norm_call']):
                    tier, direction = 'NORM', 'CALL'
                elif bool(closed['norm_put']):
                    tier, direction = 'NORM', 'PUT'

                if direction:
                    entry_price = price
                    notify_entry(direction, tier, dev_dict, q_dict, vz, rz,
                                 entry_price, bar_time)
                    expiry_time = bar_time + pd.Timedelta(minutes=EXPIRY_MINUTES)
                    active_trades.append({
                        'entry_time': bar_time,
                        'entry_price': entry_price,
                        'direction': direction,
                        'tier': tier,
                        'expiry_time': expiry_time,
                        'd30': dev_dict[30], 'd60': dev_dict[60],
                        'd120': dev_dict[120], 'd240': dev_dict[240],
                        'q30': q_dict[30], 'q60': q_dict[60],
                        'q120': q_dict[120], 'q240': q_dict[240],
                        'vol_z40': vz, 'rv_z': rz,
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
