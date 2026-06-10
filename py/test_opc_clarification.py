"""Clarification research on the 10-minute binary options OPC Normal Distribution strategy.

This script tests multiple statistical interpretations of the "Normal Distribution 7%":
1. Tail probability of 7% (Predicted Win Rate >= 93% or <= 7%, |Z| >= 1.476).
2. Volatility threshold of 0.07 * std (|Z| >= 0.07, Predicted Win Rate >= 52.8%).
3. Edge threshold of 7% (Predicted Win Rate >= 57%).
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

def run_backtest(df, lookback, target_wr, mode="trend"):
    """
    Runs a backtest with a specific target predicted win rate threshold.
    """
    df = df.copy()
    close_prices = df["close"].values
    horizon = 10
    
    # 10-minute returns
    log_returns = np.zeros(len(df))
    log_returns[horizon:] = np.log(close_prices[horizon:] / close_prices[:-horizon])
    
    actual_ups = np.zeros(len(df), dtype=bool)
    actual_ups[:-horizon] = close_prices[horizon:] > close_prices[:-horizon]
    actual_downs = np.zeros(len(df), dtype=bool)
    actual_downs[:-horizon] = close_prices[horizon:] < close_prices[:-horizon]
    
    ret_series = pd.Series(log_returns)
    rolling_mean = ret_series.rolling(lookback).mean().values.copy()
    rolling_std = ret_series.rolling(lookback).std().values.copy()
    rolling_std[rolling_std == 0] = np.nan
    
    z_scores = rolling_mean / rolling_std
    p_up = norm_cdf(z_scores)
    p_down = 1.0 - p_up
    
    if mode == "trend":
        call_signals = p_up >= target_wr
        put_signals = p_down >= target_wr
    else:
        call_signals = p_down >= target_wr
        put_signals = p_up >= target_wr
        
    valid_mask = np.ones(len(df), dtype=bool)
    valid_mask[:lookback] = False
    valid_mask[-horizon:] = False
    
    call_trades = call_signals & valid_mask
    put_trades = put_signals & valid_mask
    
    total_trades = np.sum(call_trades) + np.sum(put_trades)
    if total_trades == 0:
        return 0, 0.0, 0.0
        
    call_wins = actual_ups[call_trades]
    put_wins = actual_downs[put_trades]
    
    all_wins = np.concatenate([call_wins, put_wins])
    wins = np.sum(all_wins)
    losses = total_trades - wins
    wr = (wins / total_trades) * 100
    pnl = wins * STAKE * PAYOUT - losses * STAKE
    
    return int(total_trades), round(float(wr), 2), round(float(pnl), 2)

def main():
    df = load_data()
    if df is None:
        print("Data not found.")
        return
        
    print(f"Loaded {len(df)} rows. Running clarification backtests...\n")
    
    windows = [60, 120, 240, 480, 1440]
    
    print("==========================================================================")
    print("CASE 1: Extreme 7% Tail of Normal Distribution (Predicted Win Rate >= 93%)")
    print("Trade only when predicted probability is in the extreme 7% of tails.")
    print("==========================================================================")
    for w in windows:
        for mode in ["trend", "reversal"]:
            trades, wr, pnl = run_backtest(df, w, 0.93, mode=mode)
            print(f"Window: {w:4d}m | Mode: {mode:8s} | Trades: {trades:5d} | Actual WR: {wr:6.2f}% | PnL: ${pnl:+8.2f}")
            
    print("\n==========================================================================")
    print("CASE 2: Z-Score Threshold is 0.07 (Predicted Win Rate >= 52.8%)")
    print("Trade when expected return is at least 0.07 rolling standard deviations.")
    print("==========================================================================")
    for w in windows:
        for mode in ["trend", "reversal"]:
            trades, wr, pnl = run_backtest(df, w, 0.528, mode=mode)
            print(f"Window: {w:4d}m | Mode: {mode:8s} | Trades: {trades:5d} | Actual WR: {wr:6.2f}% | PnL: ${pnl:+8.2f}")

    print("\n==========================================================================")
    print("CASE 3: Predicted Win Rate >= 57% (7% Edge above 50%) - Recapped")
    print("==========================================================================")
    for w in windows:
        for mode in ["trend", "reversal"]:
            trades, wr, pnl = run_backtest(df, w, 0.57, mode=mode)
            print(f"Window: {w:4d}m | Mode: {mode:8s} | Trades: {trades:5d} | Actual WR: {wr:6.2f}% | PnL: ${pnl:+8.2f}")

if __name__ == "__main__":
    main()
