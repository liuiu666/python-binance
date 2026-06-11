"""Sweeping short rolling lookback windows (15m to 50m) to unlock higher trade counts under extreme POC tails.
"""

import os
import numpy as np
import pandas as pd
from scipy.stats import norm

# Setup relative paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")

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

def run_backtest(df, lookback, tail_pct, mode="reversal", horizon=10):
    df = df.copy()
    close_prices = df["close"].values
    
    # 1-minute independent log returns
    log_returns_1m = np.zeros(len(df))
    log_returns_1m[1:] = np.log(close_prices[1:] / close_prices[:-1])
    
    actual_ups = np.zeros(len(df), dtype=bool)
    actual_ups[:-horizon] = close_prices[horizon:] > close_prices[:-horizon]
    actual_downs = np.zeros(len(df), dtype=bool)
    actual_downs[:-horizon] = close_prices[horizon:] < close_prices[:-horizon]
    
    ret_series = pd.Series(log_returns_1m)
    mu_1m = ret_series.rolling(lookback).mean().values.copy()
    sigma_1m = ret_series.rolling(lookback).std().values.copy()
    sigma_1m[sigma_1m == 0] = np.nan
    
    # Square-Root-of-Time Scale modeling
    z_scores = np.sqrt(horizon) * (mu_1m / sigma_1m)
    p_up = norm_cdf(z_scores)
    p_down = 1.0 - p_up
    
    target_p = 1.0 - tail_pct
    
    if mode == "trend":
        raw_call_signals = p_up >= target_p
        raw_put_signals = p_down >= target_p
    else:  # reversal
        raw_call_signals = p_down >= target_p
        raw_put_signals = p_up >= target_p
        
    valid_mask = np.ones(len(df), dtype=bool)
    valid_mask[:lookback] = False
    valid_mask[-horizon:] = False
    
    call_trades = raw_call_signals & valid_mask
    put_trades = raw_put_signals & valid_mask
    
    total_trades = np.sum(call_trades) + np.sum(put_trades)
    if total_trades == 0:
        return 0, 0.0, 0.0, 0
        
    call_wins = actual_ups[call_trades]
    put_wins = actual_downs[put_trades]
    
    all_wins = np.concatenate([call_wins, put_wins])
    wins = np.sum(all_wins)
    losses = total_trades - wins
    wr = (wins / total_trades) * 100
    pnl = wins * STAKE * PAYOUT - losses * STAKE
    
    # Calculate consecutive loss streak
    trade_times = np.concatenate([df["open_time"].values[call_trades], df["open_time"].values[put_trades]])
    sorted_idx = np.argsort(trade_times)
    chronological_wins = all_wins[sorted_idx]
    
    best = cur = 0
    for ok in chronological_wins:
        if ok:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
            
    return int(total_trades), round(float(wr), 2), round(float(pnl), 2), int(best)

def main():
    df = load_data()
    if df is None:
        print("Data not found.")
        return
        
    print(f"Loaded {len(df)} rows. Sweeping short windows (15m to 50m)...\n")
    
    windows = [15, 20, 30, 40, 50]
    tail_pcts = [0.05, 0.07, 0.10, 0.12, 0.15]
    
    for w in windows:
        print("="*100)
        print(f" SHORT ROLLING WINDOW: {w} minutes (lookback) ")
        print("="*100)
        print(f"{'Tail %':8s} | {'Thres WR':8s} | {'Mode':8s} | {'Trades':6s} | {'Tr/Day':6s} | {'Actual WR':9s} | {'PnL':9s} | {'Max Loss Streak':15s}")
        print("-"*100)
        for pct in tail_pcts:
            for mode in ["trend", "reversal"]:
                trades, wr, pnl, max_loss = run_backtest(df, w, pct, mode=mode)
                tr_per_day = round(trades / 93.0, 2)
                pct_str = f"{pct*100:.0f}%"
                thres_str = f"{100 - pct*100:.0f}%"
                
                hl_prefix = "★ " if (mode == "reversal" and pnl > 0 and trades >= 1000) else "  "
                print(f"{hl_prefix}{pct_str:6s} | {thres_str:8s} | {mode:8s} | {trades:6d} | {tr_per_day:6.2f} | {wr:8.2f}% | ${pnl:+8.2f} | {max_loss:15d}")
        print()

if __name__ == "__main__":
    main()
