"""Live signal runner for the MeanReversion10mStrategy A3/A4 signal.

Polls Binance USDT-M perpetual futures 1m klines, computes ema120_dev /
vol_z40 / rv_z, fires DingTalk notifications when signal triggers, and
issues a settlement notification 10 minutes later.

Indicators are duplicated locally to avoid importing the freqtrade strategy
class (which carries heavy IStrategy machinery). The mathematical
definitions MUST stay in sync with
    user_data/strategies/MeanReversion10mStrategy.py

Run:
    .venv\\Scripts\\python -u user_data/notebooks/live_signal_runner.py

Changelog (2026-05-23):
  - FIX-1: True Range ATR (was plain H-L, now proper TR with gap handling)
  - FIX-2: vol_z40 / rv_z divide-by-zero → NaN / clip inf
  - FIX-3: Fetch only new klines (was: pull 100 each poll, now: startTime)
  - FIX-4: DingTalk retry queue with persistent JSON backup
  - FIX-5: Expiry settlement via nearest-bar interpolation (was: exact timestamp match)
  - FIX-6: Cleaner single-instance lock via psutil (was: wmic + taskkill)
  - FIX-7: Graceful SIGINT / atexit shutdown (was: raw break)
  - FIX-8: rv_z baseline uses explicit min_periods for stability
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

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
DINGTALK_QUEUE_FILE = "user_data/notebooks/dingtalk_queue.json"

# fapi USDT-M perpetual klines endpoint
FAPI_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
SYMBOL = "BTCUSDT"
INTERVAL = "1m"
KLINES_PER_REQ = 1500          # Binance fapi maximum per request

# Strategy parameters — A4: 多期 EMA 共振 + 信号分级（高质 / 普通）
EMA_SPANS = [30, 60, 120, 240]   # 4 个时间尺度
ATR_WIN = 120
VOL_Z_WIN = 40
RV_WIN = 60
RV_BASELINE_WIN = 60 * 24
EMA_DEV_QUANTILE_WIN = 60 * 24 * 14   # 14 days rolling rank window
# 高质量阈值：CALL q<=0.05 + 4 EMA 全合 / PUT q>=0.95 + >=2 EMA
HQ_CALL_QLO = 0.05; HQ_CALL_K = 4
HQ_PUT_QHI  = 0.95; HQ_PUT_K  = 2
# 普通阈值：CALL q<=0.10 + 4 EMA / PUT q>=0.90 + >=3 EMA
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

# DingTalk retry config
DINGTALK_RETRY_MAX = 5
DINGTALK_RETRY_DELAY = 30   # seconds between retries


# ============================== Single-instance (FIX-6: use psutil) ==============================

def enforce_single_instance():
    """Kill any previously-running instance cleanly, then register this PID."""
    current_pid = os.getpid()
    pid_path = Path(PID_FILE)
    if pid_path.exists():
        try:
            old_pid = int(pid_path.read_text(encoding="utf-8").strip())
            if old_pid != current_pid:
                _kill_stale_process(old_pid)
        except Exception:
            pass
    try:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(current_pid), encoding="utf-8")
    except Exception as e:
        print(f"[WARN] could not write PID file: {e}")


def _kill_stale_process(pid: int):
    """Kill process by PID if it's still running and looks like this script."""
    try:
        import psutil
        if psutil.pid_exists(pid):
            proc = psutil.Process(pid)
            cmdline = " ".join(proc.cmdline())
            if "live_signal_runner" in cmdline and "python" in cmdline.lower():
                print(f"[INFO] Stale runner detected (PID={pid}); killing gracefully.")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except psutil.TimeoutExpired:
                    proc.kill()
    except ImportError:
        # psutil not available; fall back to taskkill
        print(f"[WARN] psutil not available, using taskkill for PID={pid}")
        os.system(f"taskkill /F /PID {pid} 2>nul")
    except Exception as e:
        print(f"[WARN] Could not kill PID {pid}: {e}")


# ============================== Graceful shutdown (FIX-7) ==============================

_shutdown_requested = False


def _request_shutdown(signum, frame):
    global _shutdown_requested
    print("\n[INFO] Shutdown signal received, finishing current iteration...")
    _shutdown_requested = True


signal.signal(signal.SIGINT, _request_shutdown)
signal.signal(signal.SIGTERM, _request_shutdown)
atexit.register(lambda: print("[INFO] Runner exited."))


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


# ============================== DingTalk retry queue (FIX-4) ==============================

def _load_queue():
    if not os.path.exists(DINGTALK_QUEUE_FILE):
        return []
    try:
        with open(DINGTALK_QUEUE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def _save_queue(q):
    try:
        with open(DINGTALK_QUEUE_FILE, 'w', encoding='utf-8') as f:
            json.dump(q, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[WARN] queue save failed: {e}")


def _send_one(payload: dict) -> bool:
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.post(
            DINGTALK_WEBHOOK, json=payload,
            headers=headers,
            proxies={"http": None, "https": None},
            timeout=10,
        )
        if r.status_code == 200:
            return True
        print(f"[WARN] DingTalk HTTP {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        print(f"[WARN] DingTalk send failed: {e}")
        return False


def _drain_queue():
    """Try to send pending messages from the queue."""
    queue = _load_queue()
    if not queue:
        return
    remaining = []
    for item in queue:
        ok = _send_one(item["payload"])
        if ok:
            print(f"[INFO] Queued DingTalk delivered: {item['title']}")
        else:
            item["retries"] = item.get("retries", 0) + 1
            if item["retries"] < DINGTALK_RETRY_MAX:
                remaining.append(item)
            else:
                print(f"[WARN] DingTalk permanently failed after {DINGTALK_RETRY_MAX} retries: {item['title']}")
    _save_queue(remaining)


def send_dingtalk(title: str, content: str):
    """Enqueue a DingTalk message. Actual send happens immediately first,
    then on failure it goes into the retry queue."""
    payload = {
        "msgtype": "text",
        "text": {"content": f"[{KEYWORD} {title}]\n{content}"},
    }
    # Try immediately first
    if _send_one(payload):
        return
    # Failed → add to persistent retry queue
    queue = _load_queue()
    queue.append({"title": title, "payload": payload, "retries": 0})
    _save_queue(queue)
    print(f"[WARN] DingTalk enqueued for retry: {title} (queue size={len(queue)})")


# ============================== Klines (FIX-3: incremental fetch) ==============================

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
        time.sleep(0.25)
        if len(df) < KLINES_PER_REQ:
            break
    full = pd.concat(chunks, ignore_index=True).drop_duplicates(subset='date')
    full = full.sort_values('date').reset_index(drop=True)
    return full.tail(target_bars).reset_index(drop=True)


# ============================== Indicators (FIX-1 True Range, FIX-2 div-zero, FIX-8 min_periods) ==============================

def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    # FIX-1: True Range (proper ATR with gap handling)
    # TR = max(H - L, |H - prev_close|, |L - prev_close|)
    prev_close = out['close'].shift(1)
    tr1 = out['high'] - out['low']
    tr2 = (out['high'] - prev_close).abs()
    tr3 = (out['low'] - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    # Wilder's smoothing: EMA with alpha = 1/ATR_WIN (equivalent to traditional ATR)
    atr120 = true_range.ewm(alpha=1.0 / ATR_WIN, adjust=False).mean()
    out['atr'] = atr120

    # 4 个 EMA 偏离 + 14 天滚动分位（rank pct）
    min_q = 2 * 60 * 24   # 至少 2 天
    for s in EMA_SPANS:
        ema_s = out['close'].ewm(span=s, adjust=False).mean()
        # FIX-2: guard against zero/NaN atr → replace with NaN
        dev = (out['close'] - ema_s) / atr120.replace(0, np.nan)
        out[f'd{s}'] = dev
        out[f'q{s}'] = (
            dev.rolling(EMA_DEV_QUANTILE_WIN, min_periods=min_q)
            .rank(pct=True)
        )

    out['ema120_dev'] = out['d120']

    # FIX-2: vol_z40 — guard against zero std → NaN
    vmed40 = out['volume'].rolling(VOL_Z_WIN).median()
    vstd40 = out['volume'].rolling(VOL_Z_WIN).std()
    out['vol_z40'] = (out['volume'] - vmed40) / vstd40.replace(0, np.nan)

    # FIX-2: rv_z — guard against zero std → NaN, clip inf
    logret1 = np.log(out['close']).diff()
    rv60 = logret1.rolling(RV_WIN).std()
    rv_baseline_mean = rv60.rolling(RV_BASELINE_WIN, min_periods=RV_BASELINE_WIN).mean()
    rv_baseline_std  = rv60.rolling(RV_BASELINE_WIN, min_periods=RV_BASELINE_WIN).std()
    out['rv_z'] = ((rv60 - rv_baseline_mean) / rv_baseline_std.replace(0, np.nan))
    out['rv_z'] = out['rv_z'].replace([np.inf, -np.inf], np.nan)

    base = (
        (out['vol_z40'] > VOL_Z_THRESHOLD)
        & (out['rv_z'] > -RV_Z_BAND)
        & (out['rv_z'] < RV_Z_BAND)
        & out[f'q{EMA_SPANS[0]}'].notna()
    )

    # 计数：当前每个 EMA 是否落在低/高极端
    hq_lo_count   = sum((out[f'q{s}'] <= HQ_CALL_QLO).astype('Int64').fillna(0) for s in EMA_SPANS)
    norm_lo_count = sum((out[f'q{s}'] <= NORM_CALL_QLO).astype('Int64').fillna(0) for s in EMA_SPANS)
    hq_hi_count   = sum((out[f'q{s}'] >= HQ_PUT_QHI).astype('Int64').fillna(0)  for s in EMA_SPANS)
    norm_hi_count = sum((out[f'q{s}'] >= NORM_PUT_QHI).astype('Int64').fillna(0) for s in EMA_SPANS)
    out['hq_lo_count']   = hq_lo_count
    out['norm_lo_count'] = norm_lo_count
    out['hq_hi_count']   = hq_hi_count
    out['norm_hi_count'] = norm_hi_count

    out['hq_call']   = base & (hq_lo_count   >= HQ_CALL_K)
    out['hq_put']    = base & (hq_hi_count   >= HQ_PUT_K)
    out['norm_call'] = base & (norm_lo_count >= NORM_CALL_K) & ~out['hq_call']
    out['norm_put']  = base & (norm_hi_count >= NORM_PUT_K)  & ~out['hq_put']
    return out


# ============================== Settlement with interpolation (FIX-5) ==============================

def _settle_at_expiry(trade: dict, sig_df: pd.DataFrame) -> float:
    """Find settlement price for trade expiry, using nearest-bar interpolation
    if the exact timestamp is not in the cache (FIX-5)."""
    expiry = trade['expiry_time']
    dates = sig_df['date']

    # Exact match
    mask = dates == expiry
    if mask.any():
        return float(sig_df.loc[mask, 'close'].iloc[0])

    # Not found — check if expiry is too far in the future (trade still active)
    latest_bar_time = dates.max()
    if expiry > latest_bar_time:
        return float('nan')   # caller should treat as "still active"

    # Expiry is before latest bar but not in cache → use nearest bar
    diffs = (dates - expiry).abs()
    nearest_idx = diffs.idxmin()
    nearest_time = dates.loc[nearest_idx]
    nearest_price = float(sig_df.loc[nearest_idx, 'close'])
    print(f"[WARN] expiry {expiry} not in cache (nearest bar={nearest_time}, "
          f"delta={(nearest_time - expiry).total_seconds():.0f}s); "
          f"using nearest price {nearest_price:.2f}")
    return nearest_price


# ============================== Main loop ==============================

def main():
    enforce_single_instance()

    # Drain any messages that failed in previous runs
    _drain_queue()

    print("=" * 60)
    print("BTC 10-min binary option signal runner (A4 多期共振, 分级)")
    print(f"DingTalk webhook: {DINGTALK_WEBHOOK[:60]}...")
    print(f"Keyword: {KEYWORD}")
    print(f"Proxy:   {PROXIES['https']}")
    print(f"Source:  {FAPI_KLINES_URL}")
    print("Fixes applied: TrueRange ATR / div-zero guard / incremental fetch / "
          "DingTalk retry queue / expiry interpolation")
    print("=" * 60)

    print(f"\nBootstrapping {WARMUP_BARS} bars of 1m history (~{WARMUP_BARS/60/24:.1f} days)...")
    cache = bootstrap_history(WARMUP_BARS)
    if len(cache) < WARMUP_BARS // 2:
        print(f"[FATAL] only {len(cache)} bars fetched, aborting")
        sys.exit(1)
    print(f"Cache populated: {len(cache):,} bars from {cache['date'].iloc[0]} to {cache['date'].iloc[-1]}")

    active_trades = load_active_trades()
    last_processed = None
    last_fetch_time = cache['date'].max()   # track last fetched bar for incremental poll

    while not _shutdown_requested:
        try:
            # FIX-3: only fetch klines newer than what we already have
            since_ms = int(last_fetch_time.timestamp() * 1000) + 60_000
            tail = fetch_klines(start_ms=since_ms, limit=KLINES_PER_REQ)
            if not tail.empty:
                # Dedupe and merge
                cache = (
                    pd.concat([cache, tail], ignore_index=True)
                    .drop_duplicates(subset='date', keep='last')
                    .sort_values('date')
                    .reset_index(drop=True)
                )
                last_fetch_time = cache['date'].max()
            # Trim to warmup window plus headroom
            if len(cache) > WARMUP_BARS + 1000:
                cache = cache.iloc[-(WARMUP_BARS + 1000):].reset_index(drop=True)

            sig_df = compute_signals(cache)
            if len(sig_df) < 2:
                time.sleep(15); continue
            closed = sig_df.iloc[-2]
            bar_time = closed['date']

            # ---------- 1. settle expired trades (FIX-5: interpolation) ----------
            still_active = []
            changed = False
            for trade in active_trades:
                settle_price = _settle_at_expiry(trade, sig_df)
                if pd.isna(settle_price):
                    # Still waiting for the expiry bar to arrive
                    still_active.append(trade)
                else:
                    notify_settlement(trade, settle_price)
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
                vz = float(closed['vol_z40'])
                rz = float(closed['rv_z'])

                dev_dict = {s: float(closed[f'd{s}']) for s in EMA_SPANS}
                q_dict = {
                    s: float(closed[f'q{s}']) if pd.notna(closed[f'q{s}']) else float('nan')
                    for s in EMA_SPANS
                }

                hist_tail = sig_df.tail(EMA_DEV_QUANTILE_WIN)

                def _need_pct(side: str, qcut: float, k_required: int) -> float:
                    margins = []
                    for s in EMA_SPANS:
                        d_now = dev_dict[s]
                        thresh = hist_tail[f'd{s}'].quantile(qcut)
                        if side == 'CALL':
                            margin = max(0.0, d_now - thresh)
                        else:
                            margin = max(0.0, thresh - d_now)
                        margins.append(margin)
                    margins.sort()
                    binding = margins[k_required - 1]
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
                rv_str  = "波动正常" if rv_ok else f"波动异常(rv_z={rz:+.2f})"

                def _state(pct_hq: float, pct_norm: float,
                            side_label: str, click_label: str) -> str:
                    base_pass = vol_ok and rv_ok
                    if pct_norm <= 0.001:
                        if pct_hq <= 0.001:
                            base = "【高质 + 普通 都已就位】"
                        else:
                            base = f"【普通已就位】(再朝{side_label}方向 {pct_hq:.2f}% 升级高质)"
                        if base_pass:
                            return f"{base} → 立即推送 {click_label}"
                        else:
                            problems = []
                            if not vol_ok: problems.append(f"等放量(还差{vol_gap:+.2f})")
                            if not rv_ok:  problems.append("波动异常")
                            return f"{base} → 但 {', '.join(problems)}，暂不下单"
                    else:
                        return (f"还远 — 价格再{side_label} {pct_norm:.2f}% 触发普通 / "
                                f"{pct_hq:.2f}% 触发高质")

                call_state = _state(pct_hq_call, pct_norm_call, "下跌", "看涨(CALL)")
                put_state  = _state(pct_hq_put,  pct_norm_put,  "上涨", "看跌(PUT)")

                dev_str = " ".join(
                    f"e{s}={dev_dict[s]:+.1f}/q{q_dict[s]:.2f}" for s in EMA_SPANS
                )

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

            # Try to flush DingTalk retry queue every loop iteration
            _drain_queue()
            time.sleep(15)

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[ERROR] loop iteration failed: {e}")
            time.sleep(15)

    print("\n[INFO] Saving state before exit...")
    save_active_trades(active_trades)
    _save_queue(_load_queue())   # preserve any pending DingTalk


if __name__ == '__main__':
    main()
