"""Optimization script to find high-frequency, high-win-rate 10-minute binary option strategies.

We search for combinations of:
- Lookback windows
- OPC probability thresholds
- Volatility filters (only trading in high-volatility regimes)
- RSI filters (RSI 14 oversold/overbought double confirmation)
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
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna().drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)

def norm_cdf(x):
    return norm.cdf(x)

def calculate_rsi(prices, period=14):
    deltas = np.diff(prices)
    seed = deltas[:period+1]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    rs = up / (down if down != 0 else 1e-10)
    rsi = np.zeros_like(prices)
    rsi[:period+1] = 100. - 100. / (1. + rs)
    
    # Vectorized calculation for the rest
    for i in range(period+1, len(prices)):
        delta = deltas[i-1]
        if delta > 0:
            upval = delta
            downval = 0.
        else:
            upval = 0.
            downval = -delta
        up = (up * (period - 1) + upval) / period
        down = (down * (period - 1) + downval) / period
        rs = up / (down if down != 0 else 1e-10)
        rsi[i] = 100. - 100. / (1. + rs)
    return rsi

def main():
    df = load_data()
    if df is None:
        print("Data not found.")
        return
        
    print(f"Loaded {len(df)} rows. Running advanced high-frequency high-win-rate search...\n")
    
    close_prices = df["close"].values
    horizon = 10
    
    # Pre-calculate RSI 14
    rsi14 = calculate_rsi(close_prices, 14)
    
    # Pre-calculate 10-minute return outcomes
    actual_ups = np.zeros(len(df), dtype=bool)
    actual_ups[:-horizon] = close_prices[horizon:] > close_prices[:-horizon]
    actual_downs = np.zeros(len(df), dtype=bool)
    actual_downs[:-horizon] = close_prices[horizon:] < close_prices[:-horizon]
    
    # 10-minute returns
    log_returns = np.zeros(len(df))
    log_returns[horizon:] = np.log(close_prices[horizon:] / close_prices[:-horizon])
    ret_series = pd.Series(log_returns)
    
    # Parameters to test
    lookbacks = [30, 45, 60, 90, 120, 180, 240]
    thresholds = [0.65, 0.70, 0.75, 0.80, 0.85, 0.90]
    vol_percentiles = [0.0, 0.3, 0.5]  # Only trade if rolling volatility is above X-th percentile
    use_rsi_filters = [False, True]
    
    results = []
    
    for l in lookbacks:
        # Calculate OPC probability
        rolling_mean = ret_series.rolling(l).mean().values.copy()
        rolling_std = ret_series.rolling(l).std().values.copy()
        rolling_std[rolling_std == 0] = np.nan
        z_scores = rolling_mean / rolling_std
        p_up = norm_cdf(z_scores)
        p_down = 1.0 - p_up
        
        # Calculate historical volatility percentile threshold
        valid_rolling_std = rolling_std[l:-horizon]
        valid_rolling_std = valid_rolling_std[~np.isnan(valid_rolling_std)]
        
        for vol_pct in vol_percentiles:
            if vol_pct > 0:
                vol_cutoff = np.percentile(valid_rolling_std, vol_pct * 100)
                vol_mask = rolling_std >= vol_cutoff
            else:
                vol_mask = np.ones(len(df), dtype=bool)
                
            for th in thresholds:
                for rsi_filt in use_rsi_filters:
                    for mode in ["trend", "reversal"]:
                        # Standard signals
                        if mode == "trend":
                            call_signals = p_up >= th
                            put_signals = p_down >= th
                        else:
                            call_signals = p_down >= th
                            put_signals = p_up >= th
                            
                        # Apply Volatility filter
                        call_signals &= vol_mask
                        put_signals &= vol_mask
                        
                        # Apply RSI filter
                        if rsi_filt:
                            if mode == "trend":
                                # Trend mode: buy CALL when RSI is relatively high (strong momentum), PUT when low
                                call_signals &= rsi14 >= 55
                                put_signals &= rsi14 <= 45
                            else:
                                # Reversal mode: buy CALL when oversold (RSI <= 40), PUT when overbought (RSI >= 60)
                                call_signals &= rsi14 <= 40
                                put_signals &= rsi14 >= 60
                                
                        # Master Valid Mask
                        valid_mask = np.ones(len(df), dtype=bool)
                        valid_mask[:l] = False
                        valid_mask[-horizon:] = False
                        
                        final_calls = call_signals & valid_mask
                        final_puts = put_signals & valid_mask
                        
                        total_trades = np.sum(final_calls) + np.sum(final_puts)
                        
                        # Filter out low trade counts (we want high-frequency: at least 5 trades/day on average)
                        # Over 93 days, 5 trades/day = ~460 trades.
                        if total_trades < 460:
                            continue
                            
                        call_wins = actual_ups[final_calls]
                        put_wins = actual_downs[final_puts]
                        
                        all_wins = np.concatenate([call_wins, put_wins])
                        wins = np.sum(all_wins)
                        wr = (wins / total_trades) * 100
                        pnl = wins * STAKE * PAYOUT - (total_trades - wins) * STAKE
                        
                        results.append({
                            "lookback": l,
                            "mode": mode,
                            "vol_pct": vol_pct,
                            "threshold": th,
                            "rsi_filter": rsi_filt,
                            "trades": total_trades,
                            "trades_per_day": round(total_trades / 93.0, 1),
                            "win_rate": round(wr, 2),
                            "pnl": round(pnl, 2)
                        })

    # Sort results by win rate descending
    results.sort(key=lambda x: x["win_rate"], reverse=True)
    
    print("==========================================================================================")
    print("TOP 15 HIGH-FREQUENCY (>5 trades/day), HIGH-WIN-RATE CONFIGURATIONS")
    print("==========================================================================================")
    print(f"{'Rank':4s} | {'Window':6s} | {'Mode':8s} | {'VolPct':6s} | {'OPC Th':6s} | {'RSI Filt':8s} | {'Trades':6s} | {'Tr/Day':6s} | {'Win Rate':8s} | {'PnL':8s}")
    print("-" * 105)
    for i, r in enumerate(results[:15]):
        print(f"#{i+1:<3d} | {r['lookback']:4d}m | {r['mode']:8s} | {r['vol_pct']:5.1f}  | {r['threshold']:5.2f} | {str(r['rsi_filter']):8s} | {r['trades']:6d} | {r['trades_per_day']:6.1f} | {r['win_rate']:7.2f}% | ${r['pnl']:+7.2f}")

if __name__ == "__main__":
    main()
