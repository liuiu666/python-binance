"""Mathematically rigorous Backtest Audit and Stress-Test.
1. Fixes look-ahead bias in volatility percentile filter using ROLLING Quantiles (past 1440m).
2. Introduces a hard Slippage/Spread Buffer (0U to 5U) to stress-test the strategy under severe real-world execution decay.
"""

import os
import warnings
import numpy as np
import pandas as pd
from scipy.stats import norm

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
    for col in ["close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna().drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)

def norm_cdf(x):
    return norm.cdf(x)

def run_audited_backtest(df, lookback, tail_pct, vol_pct=0.0, slippage_buffer=0.0, horizon=10):
    """
    Backtests the model with:
    - Zero look-ahead bias (Rolling volatility quantile).
    - Hard slippage/spread buffer (forces CALL to win by > slippage_buffer, PUT to win by < -slippage_buffer).
    """
    df = df.copy()
    close_prices = df["close"].values
    n = len(df)
    
    # 1-minute independent log returns
    log_returns_1m = np.zeros(n)
    log_returns_1m[1:] = np.log(close_prices[1:] / close_prices[:-1])
    
    # ----------------------------------------------------
    # HARD SLIPPAGE BUFFER (极度保守的实盘安全垫)
    # ----------------------------------------------------
    # CALL wins only if future price is higher than entry by at least 'slippage_buffer'
    actual_ups = close_prices[horizon:] > (close_prices[:-horizon] + slippage_buffer)
    # PUT wins only if future price is lower than entry by at least 'slippage_buffer'
    actual_downs = close_prices[horizon:] < (close_prices[:-horizon] - slippage_buffer)
    
    # Pad back to length n
    actual_ups_padded = np.zeros(n, dtype=bool)
    actual_ups_padded[:-horizon] = actual_ups
    actual_downs_padded = np.zeros(n, dtype=bool)
    actual_downs_padded[:-horizon] = actual_downs
    
    ret_series = pd.Series(log_returns_1m)
    mu_1m = ret_series.rolling(lookback).mean().values.copy()
    sigma_1m = ret_series.rolling(lookback).std().values.copy()
    sigma_1m[sigma_1m == 0] = np.nan
    
    # Mathematically correct Scale modeling
    z_scores = np.sqrt(horizon) * (mu_1m / sigma_1m)
    p_up = norm_cdf(z_scores)
    p_down = 1.0 - p_up
    
    target_p = 1.0 - tail_pct
    
    # Reversal signals
    raw_call_signals = p_down >= target_p
    raw_put_signals = p_up >= target_p
    
    # ----------------------------------------------------
    # FIX: Rolling Volatility Percentile (No Look-ahead Bias)
    # ----------------------------------------------------
    if vol_pct > 0:
        vol_series = pd.Series(sigma_1m)
        # Calculate rolling 1-day (1440m) quantile
        rolling_vol_cutoff = vol_series.rolling(1440, min_periods=120).quantile(vol_pct).values
        vol_mask = sigma_1m >= rolling_vol_cutoff
        raw_call_signals &= vol_mask
        raw_put_signals &= vol_mask
        
    valid_mask = np.ones(n, dtype=bool)
    valid_mask[:max(lookback, 1440 if vol_pct > 0 else 0)] = False
    valid_mask[-horizon:] = False
    
    # Cooldown simulation
    cooldown_calls = np.zeros(n, dtype=bool)
    cooldown_puts = np.zeros(n, dtype=bool)
    
    last_trade_minute = -9999
    cooldown_minutes = 5  # Optimal CD from previous studies
    
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
        
    call_wins = actual_ups_padded[cooldown_calls]
    put_wins = actual_downs_padded[cooldown_puts]
    
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
        
    print(f"Loaded {len(df)} rows. Running Hard Audit and Slippage Stress-Tests...\n")
    
    # Auditing Option 1: 10-Minute Options, 60m Window, 15% Tail, Reversal Mode
    # Auditing Option 2: 10-Minute Options, Parallel [45m + 60m]
    slippage_levels = [0.0, 1.0, 2.0, 5.0, 10.0] # USD deviation
    
    print("="*115)
    print(" AUDIT: 10m Binary Options | Window: 60m | Tail: 15% (85% Thres) | Reversal Mode | CD: 5m ")
    print("="*115)
    print(f"{'Slippage Buffer':18s} | {'Rolling VolPct':14s} | {'Trades':6s} | {'Tr/Day':6s} | {'Actual WR':9s} | {'PnL':9s} | {'Max Loss Streak':15s}")
    print("-"*115)
    for slip in slippage_levels:
        for vol in [0.0, 0.5]:
            trades, wr, pnl, streak = run_audited_backtest(df, 60, 0.15, vol_pct=vol, slippage_buffer=slip, horizon=10)
            tr_per_day = round(trades / 93.0, 2)
            slip_str = f"{slip:.1f} USD"
            vol_str = f"{vol*100:.0f}% Rolling" if vol > 0 else "None"
            print(f"  {slip_str:16s} | {vol_str:14s} | {trades:6d} | {tr_per_day:6.2f} | {wr:8.2f}% | ${pnl:+8.2f} | {streak:15d}")
            
if __name__ == "__main__":
    main()
