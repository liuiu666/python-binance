"""Mathematically rigorous Volume Weighted Average Price (VWAP) and Volume-filtered POC Rejection model.
Implements Auction Market Theory (AMT):
- Uses VWAP as the true Point of Control (POC).
- Evaluates standard normal deviation from VWAP using scaled 1-minute independent returns.
- Filters for "Low-Volume Rejection" (无量反转) to ensure maximum win rate.
"""

import os
import warnings
import numpy as np
import pandas as pd
from scipy.stats import norm

# Suppress warnings
warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", category=UserWarning)

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
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna().drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)

def norm_cdf(x):
    return norm.cdf(x)

def run_vwap_poc_backtest(df, lookback, tail_pct, use_volume_filter=True, cooldown_minutes=5, horizon=10):
    df = df.copy()
    close_prices = df["close"].values
    volumes = df["volume"].values
    n = len(df)
    
    # 1-minute independent log returns
    log_returns_1m = np.zeros(n)
    log_returns_1m[1:] = np.log(close_prices[1:] / close_prices[:-1])
    
    actual_ups = np.zeros(n, dtype=bool)
    actual_ups[:-horizon] = close_prices[horizon:] > close_prices[:-horizon]
    actual_downs = np.zeros(n, dtype=bool)
    actual_downs[:-horizon] = close_prices[horizon:] < close_prices[:-horizon]
    
    # Calculate rolling Volume Weighted Average Price (VWAP) as the true POC
    cum_pv = df["close"] * df["volume"]
    rolling_pv = cum_pv.rolling(lookback).sum().values.copy()
    rolling_v = df["volume"].rolling(lookback).sum().values.copy()
    rolling_v[rolling_v == 0] = np.nan
    vwap = rolling_pv / rolling_v
    
    # Rolling standard deviation of 1-minute returns
    ret_series = pd.Series(log_returns_1m)
    sigma_1m = ret_series.rolling(lookback).std().values.copy()
    sigma_1m[sigma_1m == 0] = np.nan
    
    # Distance of current price from the true POC (VWAP)
    # dev = ln(S_t / VWAP_t)
    dev = np.log(close_prices / vwap)
    
    # Scaled Z-score of deviation from value center
    # z = dev / (sqrt(H) * sigma_1m)
    z_scores = dev / (np.sqrt(horizon) * sigma_1m)
    
    p_up = norm_cdf(z_scores)
    p_down = 1.0 - p_up
    
    target_p = 1.0 - tail_pct
    
    # Raw reversal signals based on normal distribution extremes
    raw_call_signals = p_down >= target_p
    raw_put_signals = p_up >= target_p
    
    # ----------------------------------------------------
    # "无量反转" (Low-Volume Rejection) Filter
    # ----------------------------------------------------
    if use_volume_filter:
        rolling_mean_volume = df["volume"].rolling(lookback).mean().values
        # Only trade if current volume is lower than the rolling average (confirming market maker withdrawal)
        low_volume_mask = volumes <= rolling_mean_volume
        raw_call_signals &= low_volume_mask
        raw_put_signals &= low_volume_mask
        
    valid_mask = np.ones(n, dtype=bool)
    valid_mask[:lookback] = False
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
        
    print(f"Loaded {len(df)} rows. Testing VWAP-Volume POC Rejection model (Auction Market Theory)...\n")
    
    horizons = [10, 30]
    windows = [45, 60, 90, 120]
    tail_pcts = [0.03, 0.05, 0.07, 0.10, 0.12, 0.15, 0.20]
    cooldowns = [2, 3, 5, 10]
    
    for h in horizons:
        results = []
        for w in windows:
            for pct in tail_pcts:
                for cd in cooldowns:
                    for vol_filt in [False, True]:
                        trades, wr, pnl, streak = run_vwap_poc_backtest(df, w, pct, use_volume_filter=vol_filt, cooldown_minutes=cd, horizon=h)
                        if trades >= 50: # Only count statistically meaningful results
                            results.append({
                                "window": w, "tail": pct, "vol_filt": vol_filt, "cd": cd, "trades": trades, "wr": wr, "pnl": pnl, "streak": streak
                            })
                            
        # Sort by Win Rate to find the absolute safest high-win-rate configurations
        results.sort(key=lambda x: x["wr"], reverse=True)
        
        print("="*125)
        print(f" TOP HIGH-WIN-RATE VWAP-VOLUME POC REJECTION CONFIGURATIONS FOR {h}-MINUTE OPTIONS ")
        print("="*125)
        print(f"{'Rank':4s} | {'Window':6s} | {'Tail %':8s} | {'Thres WR':8s} | {'Vol Filt':8s} | {'CD':4s} | {'Trades':6s} | {'Tr/Day':6s} | {'Actual WR':9s} | {'PnL':9s} | {'Max Loss Streak':15s}")
        print("-"*125)
        for i, r in enumerate(results[:10]):
            tr_per_day = round(r["trades"] / 93.0, 2)
            pct_str = f"{r['tail']*100:.0f}%"
            thres_str = f"{100 - r['tail']*100:.0f}%"
            print(f"#{i+1:<3d} | {r['window']:4d}m | {pct_str:6s} | {thres_str:8s} | {str(r['vol_filt']):8s} | {r['cd']:2d}m | {r['trades']:6d} | {tr_per_day:6.2f} | {r['wr']:8.2f}% | ${r['pnl']:+8.2f} | {r['streak']:15d}")
        print()

if __name__ == "__main__":
    main()
