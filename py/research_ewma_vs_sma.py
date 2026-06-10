"""Backtesting script to compare Simple Moving Average (SMA) and Exponentially Weighted Moving Average (EWMA) parameter estimation.
Tests specifically for 10-minute and 30-minute binary options.
"""

import os
import numpy as np
import pandas as pd
from scipy.stats import norm

APP_DIR = "E:/codex"
DATA_DIR = os.path.join(APP_DIR, "data")
PAYOUT = 0.85
STAKE = 5.0
BREAKEVEN_WR = 100 / (1 + PAYOUT)  # ~54.05%

def load_data():
    path = os.path.join(DATA_DIR, "btcusdt_1m.csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, parse_dates=["open_time"])
    for col in ["close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna().drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)

def norm_cdf(x):
    return norm.cdf(x)

def run_backtest(df, lookback, tail_pct, horizon, estimation_method="sma"):
    df = df.copy()
    close_prices = df["close"].values
    n = len(df)
    
    # Log returns over horizon
    log_returns = np.zeros(n)
    log_returns[horizon:] = np.log(close_prices[horizon:] / close_prices[:-horizon])
    
    actual_ups = np.zeros(n, dtype=bool)
    actual_ups[:-horizon] = close_prices[horizon:] > close_prices[:-horizon]
    actual_downs = np.zeros(n, dtype=bool)
    actual_downs[:-horizon] = close_prices[horizon:] < close_prices[:-horizon]
    
    ret_series = pd.Series(log_returns)
    
    # ----------------------------------------------------
    # Core Mathematical Parameter: SMA vs EWMA
    # ----------------------------------------------------
    if estimation_method == "sma":
        rolling_mean = ret_series.rolling(lookback).mean().values.copy()
        rolling_std = ret_series.rolling(lookback).std().values.copy()
    else:  # ewma
        rolling_mean = ret_series.ewm(span=lookback, adjust=False).mean().values.copy()
        rolling_std = ret_series.ewm(span=lookback, adjust=False).std().values.copy()
        
    rolling_std[rolling_std == 0] = np.nan
    
    z_scores = rolling_mean / rolling_std
    p_up = norm_cdf(z_scores)
    p_down = 1.0 - p_up
    
    target_p = 1.0 - tail_pct
    
    # Reversal signals (consistently superior)
    raw_call_signals = p_down >= target_p
    raw_put_signals = p_up >= target_p
        
    valid_mask = np.ones(n, dtype=bool)
    valid_mask[:lookback] = False
    valid_mask[-horizon:] = False
    
    # Cooldown (non-overlapping) simulation
    cooldown_calls = np.zeros(n, dtype=bool)
    cooldown_puts = np.zeros(n, dtype=bool)
    
    last_trade_minute = -9999
    cooldown_minutes = horizon
    
    for i in range(n):
        if not valid_mask[i]:
            continue
            
        if i - last_trade_minute < cooldown_minutes:
            continue
            
        if raw_call_signals[i]:
            cooldown_calls[i] = True
            last_trade_minute = i
        elif raw_put_signals[i]:
            cooldown_puts[i] = True
            last_trade_minute = i
            
    total_trades = np.sum(cooldown_calls) + np.sum(cooldown_puts)
    if total_trades == 0:
        return 0, 0.0, 0.0, 0
        
    call_wins = actual_ups[cooldown_calls]
    put_wins = actual_downs[cooldown_puts]
    
    all_wins = np.concatenate([call_wins, put_wins])
    wins_num = np.sum(all_wins)
    wr = (wins_num / total_trades) * 100
    pnl = wins_num * STAKE * PAYOUT - (total_trades - wins_num) * STAKE
    
    # Max loss streak
    trade_times = np.concatenate([df["open_time"].values[cooldown_calls], df["open_time"].values[cooldown_puts]])
    sorted_idx = np.argsort(trade_times)
    chron_wins = all_wins[sorted_idx]
    
    best = cur = 0
    for ok in chron_wins:
        if ok: cur = 0
        else: cur += 1; best = max(best, cur)
        
    return int(total_trades), round(float(wr), 2), round(float(pnl), 2), int(best)

def main():
    df = load_data()
    if df is None:
        print("Data not found.")
        return
        
    print(f"Loaded {len(df)} rows. Comparing SMA vs EWMA parameter estimation for 10m and 30m contracts...\n")
    
    horizons = [10, 30]
    windows = [30, 45, 60, 90, 120]
    tail_pcts = [0.05, 0.07, 0.10, 0.12, 0.15, 0.20]
    methods = ["sma", "ewma"]
    
    for h in horizons:
        print("="*120)
        print(f" CONTRACT DURATION: {h}m binary options (reversal mode, CD = {h}m) ")
        print("="*120)
        print(f"{'Method':5s} | {'Window':6s} | {'Tail %':8s} | {'Thres':5s} | {'Trades':6s} | {'Tr/Day':6s} | {'Actual WR':9s} | {'PnL':9s} | {'Max Loss Streak':15s}")
        print("-"*120)
        for w in windows:
            for pct in tail_pcts:
                for method in methods:
                    trades, wr, pnl, streak = run_backtest(df, w, pct, h, estimation_method=method)
                    tr_per_day = round(trades / 93.0, 2)
                    pct_str = f"{pct*100:.0f}%"
                    thres_str = f"{100 - pct*100:.0f}%"
                    
                    # Highlight top configurations (WR >= 57% and trades >= 100)
                    hl_prefix = "★ " if (wr >= 57.0 and trades >= 100 and pnl > 0) else "  "
                    
                    # Skip showing configurations that had 0 trades to keep output clean and focused
                    if trades == 0:
                        continue
                        
                    print(f"{hl_prefix}{method:5s} | {w:4d}m | {pct_str:6s} | {thres_str:5s} | {trades:6d} | {tr_per_day:6.2f} | {wr:8.2f}% | ${pnl:+8.2f} | {streak:15d}")
        print()

if __name__ == "__main__":
    main()
