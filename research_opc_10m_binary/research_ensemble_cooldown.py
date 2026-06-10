"""Advanced Multi-Window Probability Ensemble and Fractional Cooldown optimization.
The ultimate setup for simultaneously maximizing win rate and trading frequency.
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

def run_ensemble_backtest(df, tail_pct, cooldown_minutes=5, use_ensemble=True, single_lookback=60, horizon=10):
    df = df.copy()
    close_prices = df["close"].values
    n = len(df)
    
    # 1-minute independent log returns
    log_returns_1m = np.zeros(n)
    log_returns_1m[1:] = np.log(close_prices[1:] / close_prices[:-1])
    
    actual_ups = np.zeros(n, dtype=bool)
    actual_ups[:-horizon] = close_prices[horizon:] > close_prices[:-horizon]
    actual_downs = np.zeros(n, dtype=bool)
    actual_downs[:-horizon] = close_prices[horizon:] < close_prices[:-horizon]
    
    ret_series = pd.Series(log_returns_1m)
    
    if use_ensemble:
        # Multi-window ensemble: 30m, 60m, 120m
        p_up_list = []
        for l in [30, 60, 120]:
            mu = ret_series.rolling(l).mean().values.copy()
            sigma = ret_series.rolling(l).std().values.copy()
            sigma[sigma == 0] = np.nan
            z = np.sqrt(horizon) * (mu / sigma)
            p_up_list.append(norm_cdf(z))
        
        # Average probability across windows
        p_up = np.nanmean(p_up_list, axis=0)
        p_down = 1.0 - p_up
        max_lookback = 120
    else:
        # Single lookback window
        mu = ret_series.rolling(single_lookback).mean().values.copy()
        sigma = ret_series.rolling(single_lookback).std().values.copy()
        sigma[sigma == 0] = np.nan
        z = np.sqrt(horizon) * (mu / sigma)
        p_up = norm_cdf(z)
        p_down = 1.0 - p_up
        max_lookback = single_lookback
        
    target_p = 1.0 - tail_pct
    
    # Reversal signals
    raw_call_signals = p_down >= target_p
    raw_put_signals = p_up >= target_p
    
    valid_mask = np.ones(n, dtype=bool)
    valid_mask[:max_lookback] = False
    valid_mask[-horizon:] = False
    
    # Cooldown simulation
    cooldown_calls = np.zeros(n, dtype=bool)
    cooldown_puts = np.zeros(n, dtype=bool)
    
    last_trade_minute = -9999
    
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
        
    print(f"Loaded {len(df)} rows. Running Ensemble and Fractional Cooldown search for 10m options...\n")
    
    cooldown_settings = [2, 3, 5, 10]
    tail_pcts = [0.05, 0.07, 0.10, 0.12, 0.15]
    
    results = []
    
    # Test Ensemble model
    for cd in cooldown_settings:
        for pct in tail_pcts:
            trades, wr, pnl, streak = run_ensemble_backtest(df, pct, cooldown_minutes=cd, use_ensemble=True)
            results.append({
                "type": "Ensemble (30+60+120)",
                "cooldown": cd,
                "tail": pct,
                "trades": trades,
                "wr": wr,
                "pnl": pnl,
                "streak": streak
            })
            
    # Test Single Window model (60m) as benchmark
    for cd in cooldown_settings:
        for pct in tail_pcts:
            trades, wr, pnl, streak = run_ensemble_backtest(df, pct, cooldown_minutes=cd, use_ensemble=False, single_lookback=60)
            results.append({
                "type": "Single Window (60m)",
                "cooldown": cd,
                "tail": pct,
                "trades": trades,
                "wr": wr,
                "pnl": pnl,
                "streak": streak
            })
            
    # Print comparison table
    results.sort(key=lambda x: x["pnl"], reverse=True)
    
    print("="*120)
    print(" TOP 15 OPTIMIZED ENSEMBLE & FRACTIONAL COOLDOWN CONFIGURATIONS FOR 10-MINUTE OPTIONS ")
    print("="*120)
    print(f"{'Rank':4s} | {'Model Type':22s} | {'CD':5s} | {'Tail %':8s} | {'Thres WR':8s} | {'Trades':6s} | {'Tr/Day':6s} | {'Actual WR':9s} | {'PnL':9s} | {'Max Loss Streak':15s}")
    print("-"*120)
    for i, r in enumerate(results[:15]):
        tr_per_day = round(r["trades"] / 93.0, 2)
        pct_str = f"{r['tail']*100:.0f}%"
        thres_str = f"{100 - r['tail']*100:.0f}%"
        print(f"#{i+1:<3d} | {r['type']:22s} | {r['cooldown']:3d}m | {pct_str:6s} | {thres_str:8s} | {r['trades']:6d} | {tr_per_day:6.2f} | {r['wr']:7.2f}% | ${r['pnl']:+8.2f} | {r['streak']:15d}")

if __name__ == "__main__":
    main()
