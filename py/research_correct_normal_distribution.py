"""Mathematically rigorous 10-minute and 30-minute binary options backtesting under standard Normal Distribution.
Uses 1-minute independent returns and scales mean and variance to H-minutes using the Square-Root-of-Time rule.
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

def run_rigorous_backtest(df, lookback, tail_pct, horizon, mode="reversal"):
    df = df.copy()
    close_prices = df["close"].values
    n = len(df)
    
    # 1. Calculate independent 1-minute log returns: ln(S_t / S_{t-1})
    log_returns_1m = np.zeros(n)
    log_returns_1m[1:] = np.log(close_prices[1:] / close_prices[:-1])
    
    # Outcomes of the H-minute options
    actual_ups = np.zeros(n, dtype=bool)
    actual_ups[:-horizon] = close_prices[horizon:] > close_prices[:-horizon]
    actual_downs = np.zeros(n, dtype=bool)
    actual_downs[:-horizon] = close_prices[horizon:] < close_prices[:-horizon]
    
    # 2. Rolling mean and std of 1-minute returns (i.i.d. assumption)
    ret_series = pd.Series(log_returns_1m)
    mu_1m = ret_series.rolling(lookback).mean().values.copy()
    sigma_1m = ret_series.rolling(lookback).std().values.copy()
    sigma_1m[sigma_1m == 0] = np.nan
    
    # 3. Mathematically correct Scaling to H-minutes:
    # Expected H-min return = H * mu_1m
    # Standard deviation of H-min return = sqrt(H) * sigma_1m
    # Z-score = (H * mu_1m) / (sqrt(H) * sigma_1m) = sqrt(H) * (mu_1m / sigma_1m)
    z_scores = np.sqrt(horizon) * (mu_1m / sigma_1m)
    
    # 4. Correct cumulative probability under Normal Distribution
    p_up = norm_cdf(z_scores)
    p_down = 1.0 - p_up
    
    target_p = 1.0 - tail_pct
    
    if mode == "trend":
        raw_call_signals = p_up >= target_p
        raw_put_signals = p_down >= target_p
    else:  # reversal
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
    
    # Calculate Max Loss Streak
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
        
    print(f"Loaded {len(df)} rows. Running mathematically rigorous Normal Distribution (Square-Root-of-Time scaled) backtests...\n")
    
    horizons = [10, 30]
    windows = [30, 45, 60, 90, 120, 180, 240]
    tail_pcts = [0.01, 0.02, 0.03, 0.05, 0.07, 0.10, 0.12, 0.15, 0.20]
    
    for h in horizons:
        print("="*120)
        print(f" MATHEMATICALLY CORRECT {h}m BINARY OPTIONS (Square-Root-of-Time, Cooldown = {h}m) ")
        print("="*120)
        print(f"{'Window':8s} | {'Tail %':8s} | {'Thres WR':8s} | {'Mode':8s} | {'Trades':6s} | {'Tr/Day':6s} | {'Actual WR':9s} | {'PnL':9s} | {'Max Loss Streak':15s}")
        print("-"*120)
        for w in windows:
            for pct in tail_pcts:
                for mode in ["trend", "reversal"]:
                    trades, wr, pnl, streak = run_rigorous_backtest(df, w, pct, h, mode=mode)
                    tr_per_day = round(trades / 93.0, 2)
                    pct_str = f"{pct*100:.0f}%"
                    thres_str = f"{100 - pct*100:.0f}%"
                    
                    # Highlight reversal row if profitable and has active trades
                    hl_prefix = "★ " if (mode == "reversal" and pnl > 0 and trades >= 50) else "  "
                    
                    if trades == 0:
                        continue
                        
                    print(f"{hl_prefix}{w:5d}m | {pct_str:6s} | {thres_str:8s} | {mode:8s} | {trades:6d} | {tr_per_day:6.2f} | {wr:8.2f}% | ${pnl:+8.2f} | {streak:15d}")
        print()

if __name__ == "__main__":
    main()
