import pandas as pd
import numpy as np
import talib

# Load the feather file
df = pd.read_feather("user_data/data/binance/futures/BTC_USDT_USDT-5m-futures.feather")
df.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
for col in ['open', 'high', 'low', 'close', 'volume']:
    df[col] = pd.to_numeric(df[col], errors='coerce').astype(float)

# Forward returns
df['forward_return_2b'] = (df['close'].shift(-2) - df['close']) / df['close']
df['target_put'] = (df['forward_return_2b'] < 0).astype(int)

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

# Regime definition
def get_regime(row):
    if row['adx'] < 20:
        return 'Quiet Ranging'
    elif row['adx'] >= 20 and abs(row['ema50_slope']) < 0.2:
        return 'Volatile Ranging'
    elif row['ema50_slope'] >= 0.2:
        return 'Bullish Trend'
    else:
        return 'Bearish Trend'

df['regime'] = df.apply(get_regime, axis=1)

# Extract Bullish Trend - Put candidates
# Base condition: Bullish Trend and Z-Score > 1.8 and RSI > 70
base_cond = (df['regime'] == 'Bullish Trend') & (df['zscore'] > 1.8) & (df['rsi'] > 70)
puts = df[base_cond].copy()
print(f"Base Bullish Trend - Put trades: {len(puts)} | Win Rate: {puts['target_put'].mean()*100:.2f}%")

# Engineer secondary features
puts['vol_median_20'] = df['volume'].rolling(window=20).median()
puts['vol_ratio'] = puts['volume'] / puts['vol_median_20']

df['is_up_candle'] = (df['close'] > df['open']).astype(int)
df['consec_up_3'] = df['is_up_candle'].rolling(window=3).sum()
puts['consec_up_3'] = df.loc[puts.index, 'consec_up_3']

df['rsi_diff'] = df['rsi'] - df['rsi'].shift(1)
puts['rsi_diff'] = df.loc[puts.index, 'rsi_diff']

# Analyze secondary filters
print("\n=== Testing Secondary Filters ===")

# Test 1: Volume Filter
print("\n--- Volume Filter ---")
for v_thresh in [0.5, 0.8, 1.0, 1.2, 1.5, 2.0]:
    sub = puts[puts['vol_ratio'] >= v_thresh]
    print(f"  Vol Ratio >= {v_thresh}: {len(sub)} trades | Win Rate: {sub['target_put'].mean()*100:.2f}%")
for v_thresh in [1.0, 1.2, 1.5]:
    sub = puts[puts['vol_ratio'] < v_thresh]
    print(f"  Vol Ratio < {v_thresh}: {len(sub)} trades | Win Rate: {sub['target_put'].mean()*100:.2f}%")

# Test 2: Consecutive Up Candles
print("\n--- Consecutive Up Candles Filter ---")
for c_count in [1, 2, 3]:
    sub = puts[puts['consec_up_3'] >= c_count]
    print(f"  Consec Up Candles (last 3) >= {c_count}: {len(sub)} trades | Win Rate: {sub['target_put'].mean()*100:.2f}%")

# Test 3: RSI Direction
print("\n--- RSI Direction Filter ---")
sub_falling = puts[puts['rsi_diff'] < 0]
print(f"  RSI Falling (rsi_diff < 0): {len(sub_falling)} trades | Win Rate: {sub_falling['target_put'].mean()*100:.2f}%")
sub_rising = puts[puts['rsi_diff'] >= 0]
print(f"  RSI Rising/Flat: {len(sub_rising)} trades | Win Rate: {sub_rising['target_put'].mean()*100:.2f}%")
