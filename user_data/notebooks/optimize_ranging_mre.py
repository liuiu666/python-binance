import pandas as pd
import numpy as np
import talib

# Load 1m K-lines
df = pd.read_feather("user_data/data/binance/futures/BTC_USDT_USDT-1m-futures.feather")
df.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
for col in ['open', 'high', 'low', 'close', 'volume']:
    df[col] = pd.to_numeric(df[col], errors='coerce').astype(float)

# Forward returns
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

# Volume median
df['vol_median_20'] = df['volume'].rolling(window=20).median()
df['vol_ratio'] = df['volume'] / df['vol_median_20']

# Ranging subset (ADX < 20 and absolute slope < 0.2)
ranging_df = df[(df['adx'] < 20) & (df['ema50_slope'].abs() < 0.2)].copy()
print(f"Ranging data rows: {len(ranging_df)} (out of {len(df)})")

# Search space for Ranging Call
print("\n=== Searching Ranging Call Patterns ===")
print(f"{'Z-Score limit':<15} | {'RSI limit':<10} | {'Vol Limit':<10} | {'Trades':<8} | {'Daily Freq':<10} | {'Win Rate':<10}")
print("-" * 75)

z_levels = [-0.8, -1.0, -1.2, -1.5, -1.8]
rsi_levels = [30, 35, 40, 45]
vol_levels = [0.8, 1.0, 1.2, 999.0]  # 999 means no volume filter

best_call_rules = []
for z in z_levels:
    for r in rsi_levels:
        for v in vol_levels:
            cond = (ranging_df['zscore'] < z) & (ranging_df['rsi'] < r)
            if v != 999.0:
                cond = cond & (ranging_df['vol_ratio'] <= v)
            
            subset = ranging_df[cond]
            trades = len(subset)
            freq = trades / 90.0
            if trades >= 200:
                wr = subset['target_call'].mean()
                if wr >= 0.56:
                    best_call_rules.append((z, r, v, trades, freq, wr))

# Sort by win rate
best_call_rules.sort(key=lambda x: x[5], reverse=True)
for r in best_call_rules[:15]:
    v_str = f"<= {r[2]}" if r[2] != 999.0 else "None"
    print(f"Z < {r[0]:<11.1f} | RSI < {r[1]:<6} | Vol {v_str:<7} | {r[3]:<8} | {r[4]:<10.2f} | {r[5]*100:.2f}%")


# Search space for Ranging Put
print("\n=== Searching Ranging Put Patterns ===")
print(f"{'Z-Score limit':<15} | {'RSI limit':<10} | {'Vol Limit':<10} | {'Trades':<8} | {'Daily Freq':<10} | {'Win Rate':<10}")
print("-" * 75)

z_levels_put = [0.8, 1.0, 1.2, 1.5, 1.8]
rsi_levels_put = [70, 65, 60, 55]

best_put_rules = []
for z in z_levels_put:
    for r in rsi_levels_put:
        for v in vol_levels:
            cond = (ranging_df['zscore'] > z) & (ranging_df['rsi'] > r)
            if v != 999.0:
                cond = cond & (ranging_df['vol_ratio'] <= v)
            
            subset = ranging_df[cond]
            trades = len(subset)
            freq = trades / 90.0
            if trades >= 200:
                wr = subset['target_put'].mean()
                if wr >= 0.56:
                    best_put_rules.append((z, r, v, trades, freq, wr))

# Sort by win rate
best_put_rules.sort(key=lambda x: x[5], reverse=True)
for r in best_put_rules[:15]:
    v_str = f"<= {r[2]}" if r[2] != 999.0 else "None"
    print(f"Z > {r[0]:<11.1f} | RSI > {r[1]:<6} | Vol {v_str:<7} | {r[3]:<8} | {r[4]:<10.2f} | {r[5]*100:.2f}%")
