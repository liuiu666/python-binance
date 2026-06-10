"""10-Minute Binary Options Backtesting and Research Framework.

This script implements the POC Normal Distribution model combined with 
volatility filtering and RSI double-confirmation filters for BTC 10-minute binary options.
It supports both Trend-Following and Mean-Reverting (Reversal) trading modes.
"""

import os
import json
import time
import numpy as np
import pandas as pd
from scipy.stats import norm

# Setup paths relative to workspace
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_FILE = os.path.join(OUTPUT_DIR, "poc_research_report.json")

PAYOUT = 0.85
STAKE = 5.0
BREAKEVEN_WR = 100 / (1 + PAYOUT)  # ~54.05%

def load_data(file_name="btcusdt_1m.csv"):
    """Loads 1-minute historical candlestick data."""
    path = os.path.join(DATA_DIR, file_name)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Historical data file not found at: {path}")
    print(f"Loading data from {path}...")
    df = pd.read_csv(path, parse_dates=["open_time"])
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open_time", "close"]).drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    return df

def norm_cdf(x):
    """Standard Normal Cumulative Distribution Function."""
    return norm.cdf(x)

def calculate_rsi(prices, period=14):
    """Vectorized calculation of Relative Strength Index (RSI 14)."""
    deltas = np.diff(prices)
    seed = deltas[:period+1]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    rs = up / (down if down != 0 else 1e-10)
    rsi = np.zeros_like(prices)
    rsi[:period+1] = 100. - 100. / (1. + rs)
    
    for i in range(period+1, len(prices)):
        delta = deltas[i-1]
        upval = delta if delta > 0 else 0.0
        downval = -delta if delta < 0 else 0.0
        up = (up * (period - 1) + upval) / period
        down = (down * (period - 1) + downval) / period
        rs = up / (down if down != 0 else 1e-10)
        rsi[i] = 100. - 100. / (1. + rs)
    return rsi

def calculate_max_loss_streak(wins):
    """Calculates the maximum consecutive loss streak."""
    best = cur = 0
    for ok in wins:
        if ok:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best

def run_backtest(df, lookback_window, edge_threshold, rsi14, actual_ups, actual_downs, mode="reversal", vol_pct=0.0, use_rsi=False, horizon=10):
    """
    Evaluates mathematically rigorous POC normal distribution strategy performance.
    """
    close_prices = df["close"].values
    n = len(df)
    
    # 1. Calculate independent 1-minute log returns: ln(S_t / S_{t-1})
    log_returns_1m = np.zeros(n)
    log_returns_1m[1:] = np.log(close_prices[1:] / close_prices[:-1])
    
    # 2. Rolling mean and std of 1-minute returns (i.i.d. assumption)
    ret_series = pd.Series(log_returns_1m)
    mu_1m = ret_series.rolling(lookback_window).mean().values.copy()
    sigma_1m = ret_series.rolling(lookback_window).std().values.copy()
    sigma_1m[sigma_1m == 0] = np.nan
    
    # 3. Mathematically correct Scaling to H-minutes:
    # Expected H-min return = H * mu_1m
    # Standard deviation of H-min return = sqrt(H) * sigma_1m
    # Z-score = (H * mu_1m) / (sqrt(H) * sigma_1m) = sqrt(H) * (mu_1m / sigma_1m)
    z_scores = np.sqrt(horizon) * (mu_1m / sigma_1m)
    p_up = norm_cdf(z_scores)
    p_down = 1.0 - p_up
    
    target_p = 0.5 + edge_threshold
    
    if mode == "trend":
        call_signals = p_up >= target_p
        put_signals = p_down >= target_p
    else:  # reversal
        call_signals = p_down >= target_p
        put_signals = p_up >= target_p
        
    # Volatility Filter
    if vol_pct > 0:
        valid_stds = sigma_1m[lookback_window:-horizon]
        valid_stds = valid_stds[~np.isnan(valid_stds)]
        if len(valid_stds) > 0:
            vol_cutoff = np.percentile(valid_stds, vol_pct * 100)
            vol_mask = sigma_1m >= vol_cutoff
            call_signals &= vol_mask
            put_signals &= vol_mask
            
    # RSI Double-Confirmation Filter
    if use_rsi:
        if mode == "trend":
            call_signals &= rsi14 >= 55
            put_signals &= rsi14 <= 45
        else:  # reversal
            call_signals &= rsi14 <= 40
            put_signals &= rsi14 >= 60
            
    valid_mask = np.ones(len(df), dtype=bool)
    valid_mask[:lookback_window] = False
    valid_mask[-horizon:] = False
    
    call_trades = call_signals & valid_mask
    put_trades = put_signals & valid_mask
    
    total_trades = np.sum(call_trades) + np.sum(put_trades)
    if total_trades == 0:
        return {
            "window": lookback_window, "edge": edge_threshold, "mode": mode, "vol_pct": vol_pct, "use_rsi": use_rsi,
            "trades": 0, "calls": 0, "puts": 0, "wins": 0, "losses": 0, "wr": 0.0, "edge_over_breakeven": round(-BREAKEVEN_WR, 2), "pnl": 0.0, "max_loss_streak": 0, "trades_per_day": 0.0
        }
        
    call_wins = actual_ups[call_trades]
    put_wins = actual_downs[put_trades]
    
    all_wins = np.concatenate([call_wins, put_wins])
    num_wins = np.sum(all_wins)
    num_losses = total_trades - num_wins
    win_rate = (num_wins / total_trades) * 100
    pnl = num_wins * STAKE * PAYOUT - num_losses * STAKE
    
    trade_times = np.concatenate([df["open_time"].values[call_trades], df["open_time"].values[put_trades]])
    sorted_idx = np.argsort(trade_times)
    chronological_wins = all_wins[sorted_idx]
    max_loss = calculate_max_loss_streak(chronological_wins)
    
    return {
        "window": int(lookback_window),
        "edge": float(edge_threshold),
        "mode": mode,
        "vol_pct": float(vol_pct),
        "use_rsi": bool(use_rsi),
        "trades": int(total_trades),
        "calls": int(np.sum(call_trades)),
        "puts": int(np.sum(put_trades)),
        "wins": int(num_wins),
        "losses": int(num_losses),
        "wr": round(float(win_rate), 2),
        "edge_over_breakeven": round(float(win_rate - BREAKEVEN_WR), 2),
        "pnl": round(float(pnl), 2),
        "max_loss_streak": int(max_loss),
        "trades_per_day": round(float(total_trades / (len(df) / 1440)), 2)
    }

def main():
    t0 = time.time()
    try:
        df = load_data("btcusdt_1m.csv")
    except Exception as e:
        print(f"Error loading btcusdt_1m.csv: {e}")
        # Try fall back to other available files
        try:
            df = load_data("btc_1m_90d.csv")
        except Exception as ex:
            print(f"Fatal: No historical datasets found: {ex}")
            return
            
    print(f"Loaded {len(df)} rows spanning {df['open_time'].iloc[0]} to {df['open_time'].iloc[-1]}\n")
    
    close_prices = df["close"].values
    horizon = 10
    
    # Pre-calculated arrays for speed
    rsi14 = calculate_rsi(close_prices, 14)
    actual_ups = np.zeros(len(df), dtype=bool)
    actual_ups[:-horizon] = close_prices[horizon:] > close_prices[:-horizon]
    actual_downs = np.zeros(len(df), dtype=bool)
    actual_downs[:-horizon] = close_prices[horizon:] < close_prices[:-horizon]
    
    # Run structured comparative tests
    windows = [60, 120, 240]
    edges = [0.03, 0.05, 0.07, 0.09, 0.11, 0.43] # 0.43 edge = 93% threshold (7% extreme tail)
    modes = ["trend", "reversal"]
    vols = [0.0, 0.3, 0.5]
    rsis = [False, True]
    
    print("Running strategic grid backtest...")
    all_results = []
    for w in windows:
        for edge in edges:
            for mode in modes:
                for vol in vols:
                    for rsi in rsis:
                        # Skip logically redundant combinations to save time
                        if rsi and mode == "trend" and edge > 0.15:
                            continue # RSI trend filters not meaningful at extremely high mathematical thresholds
                        res = run_backtest(df, w, edge, rsi14, actual_ups, actual_downs, mode=mode, vol_pct=vol, use_rsi=rsi)
                        all_results.append(res)
                        
    # Filter active runs
    active_runs = [r for r in all_results if r["trades"] > 0]
    
    # Sort by PnL
    active_runs.sort(key=lambda x: x["pnl"], reverse=True)
    
    # 1. Show Original 7% Edge (57% WR) comparison
    print("\n" + "="*80)
    print(" 1. 经典 7% 胜率偏差 (57% 预测胜率阈值) 表现比对")
    print("="*80)
    print(f"{'Mode':8s} | {'Window':6s} | {'Trades':6s} | {'Actual WR':9s} | {'Breakeven Edge':14s} | {'PnL':9s}")
    print("-"*80)
    for mode in ["trend", "reversal"]:
        for w in [60, 120, 240]:
            match = [r for r in all_results if r["edge"] == 0.07 and r["window"] == w and r["mode"] == mode and r["vol_pct"] == 0.0 and not r["use_rsi"]][0]
            print(f"{match['mode']:8s} | {match['window']:4d}m | {match['trades']:6d} | {match['wr']:8.2f}% | {match['edge_over_breakeven']:+13.2f}% | ${match['pnl']:+8.2f}")
            
    # 2. Show 7% Extreme Tail Probability (93% WR) comparison
    print("\n" + "="*80)
    print(" 2. 置信区间 7% 尾部极值概率 (93% 预测胜率阈值) 表现比对")
    print("="*80)
    print(f"{'Mode':8s} | {'Window':6s} | {'Trades':6s} | {'Actual WR':9s} | {'Breakeven Edge':14s} | {'PnL':9s}")
    print("-"*80)
    for mode in ["trend", "reversal"]:
        for w in [60, 120]:
            matches = [r for r in all_results if r["edge"] == 0.43 and r["window"] == w and r["mode"] == mode and r["vol_pct"] == 0.0 and not r["use_rsi"]]
            if matches:
                match = matches[0]
                print(f"{match['mode']:8s} | {match['window']:4d}m | {match['trades']:6d} | {match['wr']:8.2f}% | {match['edge_over_breakeven']:+13.2f}% | ${match['pnl']:+8.2f}")
                
    # 3. Show Top 5 Optimized High Frequency & High Win Rate configurations
    print("\n" + "="*80)
    print(" 3. 终极高频 + 高胜率优化配置（Top 5 排行）")
    print("="*80)
    print(f"{'Rank':4s} | {'Window':6s} | {'Mode':8s} | {'VolPct':6s} | {'POC Th':6s} | {'RSI':5s} | {'Trades':6s} | {'Tr/Day':6s} | {'Win Rate':8s} | {'PnL':8s}")
    print("-"*80)
    for i, r in enumerate(active_runs[:5]):
        print(f"#{i+1:<3d} | {r['window']:4d}m | {r['mode']:8s} | {r['vol_pct']:5.1f}  | {r['edge']+0.5:5.2f} | {str(r['use_rsi']):5s} | {r['trades']:6d} | {r['trades_per_day']:6.1f} | {r['wr']:7.2f}% | ${r['pnl']:+7.2f}")
    print("="*80)
    
    # Save Report
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "runtime_sec": round(time.time() - t0, 2),
        "total_configs_tested": len(all_results),
        "breakeven_wr": round(BREAKEVEN_WR, 2),
        "top_5_optimized": active_runs[:5],
        "all_results": all_results
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\nSaved full report database to: {REPORT_FILE}")

if __name__ == "__main__":
    main()
