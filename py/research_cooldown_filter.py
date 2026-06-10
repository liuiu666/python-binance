"""Backtesting script to compare standard overlapping trading with a 10-minute cooldown (non-overlapping) filter.
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

def run_backtest_with_cooldown(df, lookback, tail_pct, mode="reversal", cooldown_minutes=10, horizon=10):
    df = df.copy()
    close_prices = df["close"].values
    n = len(df)
    
    # 10-minute returns
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
    
    # Standard raw signals
    if mode == "trend":
        raw_call_signals = p_up >= target_p
        raw_put_signals = p_down >= target_p
    else:  # reversal
        raw_call_signals = p_down >= target_p
        raw_put_signals = p_up >= target_p
        
    valid_mask = np.ones(n, dtype=bool)
    valid_mask[:lookback] = False
    valid_mask[-horizon:] = False
    
    # Standard overlapping execution
    overlapping_calls = raw_call_signals & valid_mask
    overlapping_puts = raw_put_signals & valid_mask
    
    # ----------------------------------------------------
    # Cooldown (Non-overlapping) Execution simulation
    # ----------------------------------------------------
    cooldown_calls = np.zeros(n, dtype=bool)
    cooldown_puts = np.zeros(n, dtype=bool)
    
    last_trade_minute = -9999
    
    for i in range(n):
        if not valid_mask[i]:
            continue
            
        # Check if we are in cooldown
        if i - last_trade_minute < cooldown_minutes:
            continue
            
        # Check if we have a signal
        if raw_call_signals[i]:
            cooldown_calls[i] = True
            last_trade_minute = i
        elif raw_put_signals[i]:
            cooldown_puts[i] = True
            last_trade_minute = i
            
    # ----------------------------------------------------
    # Evaluate Overlapping Strategy
    # ----------------------------------------------------
    ol_trades_num = np.sum(overlapping_calls) + np.sum(overlapping_puts)
    if ol_trades_num > 0:
        ol_call_wins = actual_ups[overlapping_calls]
        ol_put_wins = actual_downs[overlapping_puts]
        ol_wins = np.concatenate([ol_call_wins, ol_put_wins])
        ol_wins_num = np.sum(ol_wins)
        ol_wr = (ol_wins_num / ol_trades_num) * 100
        ol_pnl = ol_wins_num * STAKE * PAYOUT - (ol_trades_num - ol_wins_num) * STAKE
        
        # Max Loss Streak for Overlapping
        trade_times = np.concatenate([df["open_time"].values[overlapping_calls], df["open_time"].values[overlapping_puts]])
        sorted_idx = np.argsort(trade_times)
        ol_chron_wins = ol_wins[sorted_idx]
        ol_streak = 0; cur = 0
        for ok in ol_chron_wins:
            if ok: cur = 0
            else: cur += 1; ol_streak = max(ol_streak, cur)
    else:
        ol_wr, ol_pnl, ol_streak = 0.0, 0.0, 0
        
    # ----------------------------------------------------
    # Evaluate Cooldown Strategy
    # ----------------------------------------------------
    cd_trades_num = np.sum(cooldown_calls) + np.sum(cooldown_puts)
    if cd_trades_num > 0:
        cd_call_wins = actual_ups[cooldown_calls]
        cd_put_wins = actual_downs[cooldown_puts]
        cd_wins = np.concatenate([cd_call_wins, cd_put_wins])
        cd_wins_num = np.sum(cd_wins)
        cd_wr = (cd_wins_num / cd_trades_num) * 100
        cd_pnl = cd_wins_num * STAKE * PAYOUT - (cd_trades_num - cd_wins_num) * STAKE
        
        # Max Loss Streak for Cooldown
        trade_times = np.concatenate([df["open_time"].values[cooldown_calls], df["open_time"].values[cooldown_puts]])
        sorted_idx = np.argsort(trade_times)
        cd_chron_wins = cd_wins[sorted_idx]
        cd_streak = 0; cur = 0
        for ok in cd_chron_wins:
            if ok: cur = 0
            else: cur += 1; cd_streak = max(cd_streak, cur)
    else:
        cd_wr, cd_pnl, cd_streak = 0.0, 0.0, 0
        
    return {
        "ol_trades": ol_trades_num, "ol_wr": round(ol_wr, 2), "ol_pnl": round(ol_pnl, 2), "ol_streak": ol_streak,
        "cd_trades": cd_trades_num, "cd_wr": round(cd_wr, 2), "cd_pnl": round(cd_pnl, 2), "cd_streak": cd_streak
    }

def main():
    df = load_data()
    if df is None:
        print("Data not found.")
        return
        
    print(f"Loaded {len(df)} rows. Comparing Overlapping vs 10m Cooldown (Non-overlapping)...\n")
    
    # We will test Window 50m and Window 60m since they are the sweet spots
    windows = [50, 60]
    tail_pcts = [0.05, 0.07, 0.10, 0.12, 0.15, 0.20]
    
    for w in windows:
        print("="*110)
        print(f" ROLLING WINDOW: {w}m (reversal mode)")
        print("="*110)
        print(f"{'Tail %':6s} | {'Thres WR':8s} | {'[Raw] Trades':12s} | {'[Raw] WR':8s} | {'[Raw] PnL':9s} | {'[Raw] Streak':12s} | {'[CD] Trades':11s} | {'[CD] WR':7s} | {'[CD] PnL':8s} | {'[CD] Streak':10s}")
        print("-"*110)
        for pct in tail_pcts:
            res = run_backtest_with_cooldown(df, w, pct, mode="reversal", cooldown_minutes=10)
            pct_str = f"{pct*100:.0f}%"
            thres_str = f"{100 - pct*100:.0f}%"
            print(f"  {pct_str:4s} | {thres_str:8s} | {res['ol_trades']:12d} | {res['ol_wr']:7.2f}% | ${res['ol_pnl']:+8.2f} | {res['ol_streak']:12d} | {res['cd_trades']:11d} | {res['cd_wr']:6.2f}% | ${res['cd_pnl']:+7.2f} | {res['cd_streak']:10d}")
        print()

if __name__ == "__main__":
    main()
