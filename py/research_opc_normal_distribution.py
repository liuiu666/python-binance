"""Research 10-minute binary options on BTC using the OPC Normal Distribution model.

This script implements a rolling normal distribution model to estimate the
probability of a price increase over the next 10 minutes, generating trading signals
when the estimated probability meets the specified edge/win-rate threshold.
"""

import os
import json
import time
import math
import numpy as np
import pandas as pd

# Handle imports for scipy stats or fallback
try:
    from scipy.stats import norm
    def norm_cdf(x):
        return norm.cdf(x)
except ImportError:
    try:
        from scipy.special import erf
        def norm_cdf(x):
            return 0.5 * (1 + erf(x / np.sqrt(2)))
    except ImportError:
        # Simple numpy approximation of standard normal CDF
        def norm_cdf(x):
            return 1.0 / (1.0 + np.exp(-1.5976 * x - 0.07056 * x**3))

APP_DIR = os.environ.get("APP_DIR", "E:/codex")
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(APP_DIR, "data"))
SYMBOL = "btcusdt"
PAYOUT = 0.85
STAKE = 5.0
BREAKEVEN_WR = 100 / (1 + PAYOUT)  # ~54.05%

def load_data(file_name):
    path = os.path.join(DATA_DIR, file_name)
    if not os.path.exists(path):
        print(f"File not found: {path}")
        return None
    print(f"Loading data from {path}...")
    df = pd.read_csv(path, parse_dates=["open_time"])
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open_time", "close"]).drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    return df

def calculate_max_loss_streak(wins):
    best = cur = 0
    for ok in wins:
        if ok:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best

def evaluate_strategy(df, lookback_window, edge_threshold, horizon=10, mode="trend"):
    """
    Backtests the OPC Normal Distribution model.
    lookback_window: Number of minutes for estimating rolling mean & std dev
    edge_threshold: The target edge above 50% (e.g., 0.07 means 57% win rate threshold)
    horizon: Binary option duration in minutes (10 minutes)
    mode: "trend" for standard trend-following, "reversal" for mean-reverting (reversed signals)
    """
    # 1. Calculate 10-minute log returns: ln(S_t / S_{t-10})
    df = df.copy()
    close_prices = df["close"].values
    log_returns = np.zeros(len(df))
    log_returns[horizon:] = np.log(close_prices[horizon:] / close_prices[:-horizon])
    
    # We want to predict the return from t to t+horizon: ln(S_{t+horizon} / S_t)
    actual_ups = np.zeros(len(df), dtype=bool)
    actual_ups[:-horizon] = close_prices[horizon:] > close_prices[:-horizon]
    actual_downs = np.zeros(len(df), dtype=bool)
    actual_downs[:-horizon] = close_prices[horizon:] < close_prices[:-horizon]
    
    # 2. Rolling mean and std of historical 10-minute returns
    ret_series = pd.Series(log_returns)
    rolling_mean = ret_series.rolling(lookback_window).mean().values.copy()
    rolling_std = ret_series.rolling(lookback_window).std().values.copy()
    
    # Avoid division by zero
    rolling_std[rolling_std == 0] = np.nan
    
    # 3. Compute predicted probability of price going up over next 10 minutes
    z_scores = rolling_mean / rolling_std
    p_up = norm_cdf(z_scores)
    p_down = 1.0 - p_up
    
    # 4. Generate trade signals based on mode
    target_p = 0.5 + edge_threshold
    
    if mode == "trend":
        # Standard: If model says UP, buy CALL; if model says DOWN, buy PUT
        call_signals = p_up >= target_p
        put_signals = p_down >= target_p
    else:
        # Reversal: If model says UP, buy PUT; if model says DOWN, buy CALL
        call_signals = p_down >= target_p
        put_signals = p_up >= target_p
    
    # Filter valid index range
    valid_mask = np.ones(len(df), dtype=bool)
    valid_mask[:lookback_window] = False
    valid_mask[-horizon:] = False
    
    # Apply valid mask
    call_trades = call_signals & valid_mask
    put_trades = put_signals & valid_mask
    
    # Calculate stats for valid p_up
    valid_p_up = p_up[valid_mask]
    valid_p_up = valid_p_up[~np.isnan(valid_p_up)]
    p_up_min = float(np.min(valid_p_up)) if len(valid_p_up) > 0 else 0.5
    p_up_max = float(np.max(valid_p_up)) if len(valid_p_up) > 0 else 0.5
    p_up_mean = float(np.mean(valid_p_up)) if len(valid_p_up) > 0 else 0.5
    p_up_std = float(np.std(valid_p_up)) if len(valid_p_up) > 0 else 0.0
    
    # 5. Evaluate trades
    num_calls = np.sum(call_trades)
    num_puts = np.sum(put_trades)
    total_trades = num_calls + num_puts
    
    if total_trades == 0:
        return {
            "window": lookback_window,
            "edge": edge_threshold,
            "mode": mode,
            "trades": 0,
            "calls": 0,
            "puts": 0,
            "wins": 0,
            "losses": 0,
            "wr": 0.0,
            "edge_over_breakeven": round(-BREAKEVEN_WR, 2),
            "pnl": 0.0,
            "max_loss_streak": 0,
            "p_up_min": p_up_min,
            "p_up_max": p_up_max,
            "p_up_mean": p_up_mean,
            "p_up_std": p_up_std,
            "trades_per_day": 0.0
        }
        
    call_wins = actual_ups[call_trades]
    put_wins = actual_downs[put_trades]
    
    # Combine results
    all_wins = np.concatenate([call_wins, put_wins])
    num_wins = np.sum(all_wins)
    num_losses = total_trades - num_wins
    win_rate = (num_wins / total_trades) * 100
    
    # PnL calculation
    pnl = num_wins * STAKE * PAYOUT - num_losses * STAKE
    
    # Max loss streak
    trade_times = np.concatenate([df["open_time"].values[call_trades], df["open_time"].values[put_trades]])
    trade_wins = np.concatenate([call_wins, put_wins])
    
    sorted_idx = np.argsort(trade_times)
    chronological_wins = trade_wins[sorted_idx]
    max_loss = calculate_max_loss_streak(chronological_wins)
    
    return {
        "window": int(lookback_window),
        "edge": float(edge_threshold),
        "mode": mode,
        "trades": int(total_trades),
        "calls": int(num_calls),
        "puts": int(num_puts),
        "wins": int(num_wins),
        "losses": int(num_losses),
        "wr": round(float(win_rate), 2),
        "edge_over_breakeven": round(float(win_rate - BREAKEVEN_WR), 2),
        "pnl": round(float(pnl), 2),
        "max_loss_streak": int(max_loss),
        "trades_per_day": round(float(total_trades / (len(df) / 1440)), 2),
        "p_up_min": p_up_min,
        "p_up_max": p_up_max,
        "p_up_mean": p_up_mean,
        "p_up_std": p_up_std
    }

def main():
    t0 = time.time()
    
    # Detect available CSV datasets in the data folder
    csv_files = ["btcusdt_1m.csv", "btc_1m_90d.csv"]
    valid_csvs = [f for f in csv_files if os.path.exists(os.path.join(DATA_DIR, f))]
    
    if not valid_csvs:
        print("No valid CSV files found in E:/codex/data/")
        return
        
    results = {}
    
    # We will test multiple lookback windows (to see memory/stability of the norm dist model)
    windows = [60, 120, 240, 480, 720, 1440, 2880, 5760]  # from 1 hour to 4 days
    
    # We will test multiple edge thresholds around the requested 7% (0.07)
    edges = [0.03, 0.05, 0.07, 0.09, 0.11]  # corresponding to win rate thresholds 53%, 55%, 57%, 59%, 61%
    
    for file_name in valid_csvs:
        df = load_data(file_name)
        if df is None:
            continue
            
        print(f"Loaded {len(df)} rows spanning {df['open_time'].iloc[0]} to {df['open_time'].iloc[-1]}")
        
        file_results = []
        for w in windows:
            # Run once to print stats of p_up
            temp_res = evaluate_strategy(df, w, 0.0, mode="trend")
            print(f"Window {w}m: p_up stats -> min: {temp_res.get('p_up_min'):.4f}, max: {temp_res.get('p_up_max'):.4f}, mean: {temp_res.get('p_up_mean'):.4f}, std: {temp_res.get('p_up_std'):.4f}")
            for edge in edges:
                for mode in ["trend", "reversal"]:
                    res = evaluate_strategy(df, w, edge, mode=mode)
                    file_results.append(res)
                
        # Sort by PnL, prioritizing active strategies (trades > 0)
        active_results = [r for r in file_results if r["trades"] > 0]
        active_results.sort(key=lambda x: x["pnl"], reverse=True)
        
        results[file_name] = file_results
        
        print(f"\n=== COMPARISON FOR {file_name} (7% Edge / 57% WR Threshold) ===")
        edge_7_results = [r for r in file_results if abs(r["edge"] - 0.07) < 0.001]
        
        print("\n--- Standard Trend-Following Mode ---")
        trend_7 = [r for r in edge_7_results if r["mode"] == "trend"]
        trend_7.sort(key=lambda x: x["window"])
        for r in trend_7:
            print(f"  Window: {r['window']:4d}m | Trades: {r['trades']:6d} (Calls: {r['calls']:5d}, Puts: {r['puts']:5d}) | Actual WR: {r['wr']:6.2f}% | Edge over Breakeven: {r['edge_over_breakeven']:+6.2f}% | PnL: ${r['pnl']:+8.2f} | Max Loss Streak: {r['max_loss_streak']:2d}")
            
        print("\n--- Reversed Mean-Reverting Mode ---")
        rev_7 = [r for r in edge_7_results if r["mode"] == "reversal"]
        rev_7.sort(key=lambda x: x["window"])
        for r in rev_7:
            print(f"  Window: {r['window']:4d}m | Trades: {r['trades']:6d} (Calls: {r['calls']:5d}, Puts: {r['puts']:5d}) | Actual WR: {r['wr']:6.2f}% | Edge over Breakeven: {r['edge_over_breakeven']:+6.2f}% | PnL: ${r['pnl']:+8.2f} | Max Loss Streak: {r['max_loss_streak']:2d}")
            
        print(f"\nTop 5 PROFITABLE configurations with active trades for {file_name}:")
        if active_results:
            for r in active_results[:5]:
                print(f"  Window: {r['window']:4d}m | Mode: {r['mode']:8s} | Edge: {r['edge']*100:2.0f}% | Trades: {r['trades']:6d} | Actual WR: {r['wr']:6.2f}% | PnL: ${r['pnl']:+8.2f} | Max Loss Streak: {r['max_loss_streak']:2d}")
        else:
            print("  No configurations generated any trades.")
        print("==================================================\n")
            
    # Save a report
    output_path = os.path.join(DATA_DIR, "research_opc_normal_report.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "runtime_sec": round(time.time() - t0, 2),
            "payout": PAYOUT,
            "stake": STAKE,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "results": results
        }, f, indent=2, ensure_ascii=False)
        
    print(f"\nSaved full report to: {output_path}")

if __name__ == "__main__":
    main()
