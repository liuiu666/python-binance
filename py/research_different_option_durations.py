"""Backtesting script to sweep across different binary option contract durations (5m, 15m, 30m) and parameters.
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

def run_backtest_with_cooldown(df, lookback, tail_pct, horizon, mode="reversal"):
    df = df.copy()
    close_prices = df["close"].values
    n = len(df)
    
    # Calculate log returns over the horizon period
    log_returns = np.zeros(n)
    log_returns[horizon:] = np.log(close_prices[horizon:] / close_prices[:-horizon])
    
    actual_ups = np.zeros(n, dtype=bool)
    actual_ups[:-horizon] = close_prices[horizon:] > close_prices[:-horizon]
    actual_downs = np.zeros(n, dtype=bool)
    actual_downs[:-horizon] = close_prices[horizon:] < close_prices[:-horizon]
    
    ret_series = pd.Series(log_returns)
    rolling_mean = ret_series.rolling(lookback).mean().values.copy()
    rolling_std = ret_series.rolling(lookback).std().values.copy()
    rolling_std[rolling_std == 0] = np.nan
    
    z_scores = rolling_mean / rolling_std
    p_up = norm_cdf(z_scores)
    p_down = 1.0 - p_up
    
    target_p = 1.0 - tail_pct
    
    # Reversal signals (consistently outperforms trend)
    raw_call_signals = p_down >= target_p
    raw_put_signals = p_up >= target_p
        
    valid_mask = np.ones(n, dtype=bool)
    valid_mask[:lookback] = False
    valid_mask[-horizon:] = False
    
    # Cooldown execution simulation (cooldown = horizon to prevent overlapping trades)
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
        
    print(f"Loaded {len(df)} rows. Sweeping different binary option durations (5m, 15m, 30m)...\n")
    
    horizons = [5, 15, 30]
    windows = [30, 45, 60, 90]
    tail_pcts = [0.05, 0.07, 0.10, 0.15]
    
    for h in horizons:
        print("="*110)
        print(f" BINARY OPTION CONTRACT DURATION: {h} minutes (cooldown = {h}m, reversal mode) ")
        print("="*110)
        print(f"{'Window':8s} | {'Tail %':8s} | {'Thres WR':8s} | {'Trades':6s} | {'Tr/Day':6s} | {'Actual WR':9s} | {'PnL':9s} | {'Max Loss Streak':15s}")
        print("-"*110)
        for w in windows:
            for pct in tail_pcts:
                trades, wr, pnl, streak = run_backtest_with_cooldown(df, w, pct, h)
                tr_per_day = round(trades / 93.0, 2)
                pct_str = f"{pct*100:.0f}%"
                thres_str = f"{100 - pct*100:.0f}%"
                
                # Highlight highly profitable ones (win rate > 57% and trades > 100)
                hl_prefix = "★ " if (wr >= 57.0 and trades >= 100 and pnl > 0) else "  "
                
                print(f"{hl_prefix}{w:5d}m | {pct_str:6s} | {thres_str:8s} | {trades:6d} | {tr_per_day:6.2f} | {wr:8.2f}% | ${pnl:+8.2f} | {streak:15d}")
        print()

if __name__ == "__main__":
    main()
