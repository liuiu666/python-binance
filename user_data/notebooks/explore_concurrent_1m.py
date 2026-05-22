import pandas as pd
import numpy as np
import talib

# Load 1m K-lines
df = pd.read_feather("user_data/data/binance/futures/BTC_USDT_USDT-1m-futures.feather")
df.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
for col in ['open', 'high', 'low', 'close', 'volume']:
    df[col] = pd.to_numeric(df[col], errors='coerce').astype(float)

# Forward returns (10 minutes = 10 bars of 1m)
df['forward_return_10b'] = (df['close'].shift(-10) - df['close']) / df['close']
df['target_call'] = (df['forward_return_10b'] > 0).astype(int)
df['target_put'] = (df['forward_return_10b'] < 0).astype(int)

# Indicators
df['atr'] = talib.ATR(df['high'].values, df['low'].values, df['close'].values, timeperiod=14)
df['adx'] = talib.ADX(df['high'].values, df['low'].values, df['close'].values, timeperiod=14)
df['ema50'] = talib.EMA(df['close'].values, timeperiod=50)
df['ema50_slope'] = (df['ema50'] - df['ema50'].shift(5)) / df['atr']

# Z-Score
mean20 = df['close'].rolling(window=20).mean()
std20 = df['close'].rolling(window=20).std()
df['zscore'] = (df['close'] - mean20) / std20
df['rsi'] = talib.RSI(df['close'].values, timeperiod=14)

# Volume anomaly
df['vol_median_20'] = df['volume'].rolling(window=20).median()
df['vol_ratio'] = df['volume'] / df['vol_median_20']

# Candle patterns
df['is_down_candle'] = (df['close'] < df['open']).astype(int)
df['consec_down_3'] = df['is_down_candle'].rolling(window=3).sum()
df['rsi_diff'] = df['rsi'] - df['rsi'].shift(1)

# Define optimized signals
df['raw_call'] = 0
df['raw_put'] = 0

# 1. Bearish Trend -> Call only (Z < -1.5 & RSI < 25) + filters
idx_bear = df['regime'] = (df['adx'] >= 20) & (df['ema50_slope'] <= -0.2)
df.loc[
    idx_bear & 
    (df['zscore'] < -1.5) & 
    (df['rsi'] < 25) &
    (df['vol_ratio'] >= 1.0) &
    (df['consec_down_3'] == 3) &
    (df['rsi_diff'] <= 0), 
    'raw_call'
] = 1

# 2. Bullish Trend -> Put only (Z > 2.2 & RSI > 75)
idx_bull = (df['adx'] >= 20) & (df['ema50_slope'] >= 0.2)
df.loc[idx_bull & (df['zscore'] > 2.2) & (df['rsi'] > 75), 'raw_put'] = 1

def run_concurrent_simulation(max_concurrent):
    active_trades = []  # list of expiry indices
    executed_trades = []
    
    raw_call_arr = df['raw_call'].values
    raw_put_arr = df['raw_put'].values
    target_call_arr = df['target_call'].values
    target_put_arr = df['target_put'].values
    
    for i in range(len(df)):
        # Remove expired trades
        active_trades = [expiry for expiry in active_trades if expiry > i]
        
        call_sig = raw_call_arr[i]
        put_sig = raw_put_arr[i]
        
        if len(active_trades) < max_concurrent:
            if call_sig == 1 and put_sig == 1:
                continue
            elif call_sig == 1:
                executed_trades.append({
                    'type': 'Call',
                    'is_win': target_call_arr[i]
                })
                active_trades.append(i + 10)  # expires in 10 bars
            elif put_sig == 1:
                executed_trades.append({
                    'type': 'Put',
                    'is_win': target_put_arr[i]
                })
                active_trades.append(i + 10)  # expires in 10 bars

    trades_df = pd.DataFrame(executed_trades)
    if len(trades_df) > 0:
        total = len(trades_df)
        wins = trades_df['is_win'].sum()
        wr = wins / total
        trades_per_day = total / 90.0
        payout = 0.80
        expectancy = wr * payout - (1 - wr) * 1.0
        return total, trades_per_day, wr, expectancy
    else:
        return 0, 0.0, 0.0, 0.0

print(f"{'Max Concurrent Trades':<25} | {'Total Trades':<12} | {'Daily Freq':<10} | {'Win Rate':<10} | {'Expectancy':<10}")
print("-" * 75)
for limit in [1, 2, 3, 5, 8, 10, 20, 100]:
    tot, freq, wr, exp = run_concurrent_simulation(limit)
    print(f"{limit:<25} | {tot:<12} | {freq:<10.2f} | {wr*100:<9.2f}% | {exp:<.4f} R")
