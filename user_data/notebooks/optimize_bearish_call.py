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
df['target_call'] = (df['forward_return_2b'] > 0).astype(int)

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

# Extract Bearish Trend - Call candidates
# Base condition: Bearish Trend and Z-Score < -1.6 and RSI < 32
base_cond = (df['regime'] == 'Bearish Trend') & (df['zscore'] < -1.6) & (df['rsi'] < 32)
calls = df[base_cond].copy()
print(f"Base Bearish Trend - Call trades: {len(calls)} | Win Rate: {calls['target_call'].mean()*100:.2f}%")

# Let's engineer some secondary features to test
# 1. Volume Anomaly (Current Volume / 20-period median volume)
calls['vol_median_20'] = df['volume'].rolling(window=20).median()
calls['vol_ratio'] = calls['volume'] / calls['vol_median_20']

# 2. Consecutive Down Candles (Exhaustion)
# How many of the last 3 candles were down?
df['is_down_candle'] = (df['close'] < df['open']).astype(int)
df['consec_down_3'] = df['is_down_candle'].rolling(window=3).sum()
calls['consec_down_3'] = df.loc[calls.index, 'consec_down_3']

# 3. Candle Body vs Range Ratio (High vs Low body size)
# If the body is small compared to the range, it shows indecision/wick (potential reversal)
df['candle_body'] = (df['close'] - df['open']).abs()
df['candle_range'] = df['high'] - df['low']
df['body_range_ratio'] = df['candle_body'] / df['candle_range'].replace(0, 1e-6)
calls['body_range_ratio'] = df.loc[calls.index, 'body_range_ratio']

# 4. Lower Wick Ratio
# If there is a long lower wick, it shows strong buying pressure stepped in
df['lower_wick'] = np.minimum(df['open'], df['close']) - df['low']
df['lower_wick_ratio'] = df['lower_wick'] / df['candle_range'].replace(0, 1e-6)
calls['lower_wick_ratio'] = df.loc[calls.index, 'lower_wick_ratio']

# 5. RSI Slope / Reversal
# Is RSI starting to rise, or is it flat/falling?
df['rsi_diff'] = df['rsi'] - df['rsi'].shift(1)
calls['rsi_diff'] = df.loc[calls.index, 'rsi_diff']

# Analyze secondary filters
print("\n=== Testing Secondary Filters ===")

# Test 1: Volume Filter
print("\n--- Volume Filter ---")
for v_thresh in [0.5, 0.8, 1.0, 1.2, 1.5, 2.0]:
    sub = calls[calls['vol_ratio'] >= v_thresh]
    print(f"  Vol Ratio >= {v_thresh}: {len(sub)} trades | Win Rate: {sub['target_call'].mean()*100:.2f}%")
for v_thresh in [1.0, 1.2, 1.5]:
    sub = calls[calls['vol_ratio'] < v_thresh]
    print(f"  Vol Ratio < {v_thresh}: {len(sub)} trades | Win Rate: {sub['target_call'].mean()*100:.2f}%")

# Test 2: Consecutive Down Candles
print("\n--- Consecutive Down Candles Filter ---")
for c_count in [1, 2, 3]:
    sub = calls[calls['consec_down_3'] >= c_count]
    print(f"  Consec Down Candles (last 3) >= {c_count}: {len(sub)} trades | Win Rate: {sub['target_call'].mean()*100:.2f}%")

# Test 3: Lower Wick Ratio
print("\n--- Lower Wick Ratio Filter ---")
for w_thresh in [0.1, 0.2, 0.3, 0.4, 0.5]:
    sub = calls[calls['lower_wick_ratio'] >= w_thresh]
    print(f"  Lower Wick Ratio >= {w_thresh}: {len(sub)} trades | Win Rate: {sub['target_call'].mean()*100:.2f}%")

# Test 4: RSI Direction
print("\n--- RSI Direction Filter ---")
sub_rising = calls[calls['rsi_diff'] > 0]
print(f"  RSI Rising (rsi_diff > 0): {len(sub_rising)} trades | Win Rate: {sub_rising['target_call'].mean()*100:.2f}%")
sub_falling = calls[calls['rsi_diff'] <= 0]
print(f"  RSI Falling/Flat: {len(sub_falling)} trades | Win Rate: {sub_falling['target_call'].mean()*100:.2f}%")

# Test 5: Combinations
print("\n--- Combined Filters ---")
# High volume + lower wick
sub_comb1 = calls[(calls['vol_ratio'] >= 1.2) & (calls['lower_wick_ratio'] >= 0.2)]
print(f"  Vol Ratio >= 1.2 AND Lower Wick >= 0.2: {len(sub_comb1)} trades | Win Rate: {sub_comb1['target_call'].mean()*100:.2f}%")

# High volume + rising RSI
sub_comb2 = calls[(calls['vol_ratio'] >= 1.2) & (calls['rsi_diff'] > 0)]
print(f"  Vol Ratio >= 1.2 AND RSI Rising: {len(sub_comb2)} trades | Win Rate: {sub_comb2['target_call'].mean()*100:.2f}%")

# Lower wick + consec down
sub_comb3 = calls[(calls['lower_wick_ratio'] >= 0.3) & (calls['consec_down_3'] >= 2)]
print(f"  Lower Wick >= 0.3 AND Consec Down >= 2: {len(sub_comb3)} trades | Win Rate: {sub_comb3['target_call'].mean()*100:.2f}%")
