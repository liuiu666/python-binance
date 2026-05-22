import pandas as pd
import numpy as np
import talib

# Load the feather file
df = pd.read_feather("user_data/data/binance/futures/BTC_USDT_USDT-5m-futures.feather")
df.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
print("Loaded K-lines:", len(df))

# Convert types to float explicitly and verify
for col in ['open', 'high', 'low', 'close', 'volume']:
    df[col] = pd.to_numeric(df[col], errors='coerce').astype(float)

# Target variable: 10-minute forward return (after 2 bars of 5m)
df['forward_return_2b'] = (df['close'].shift(-2) - df['close']) / df['close']
df['target_call'] = (df['forward_return_2b'] > 0).astype(int)
df['target_put'] = (df['forward_return_2b'] < 0).astype(int)

# Features:
# 1. Volatility Regime: BB Width or normalized ATR
df['atr'] = talib.ATR(df['high'].values, df['low'].values, df['close'].values, timeperiod=14)
df['bb_upper'], df['bb_middle'], df['bb_lower'] = talib.BBANDS(df['close'].values, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / df['bb_middle']
df['bb_width_ma'] = df['bb_width'].rolling(window=100).mean()
df['volatility_regime'] = np.where(df['bb_width'] > df['bb_width_ma'], 'High Vol', 'Low Vol')

# 2. Trend Regime: ADX and EMA slope
df['adx'] = talib.ADX(df['high'].values, df['low'].values, df['close'].values, timeperiod=14)
df['ema50'] = talib.EMA(df['close'].values, timeperiod=50)
df['ema200'] = talib.EMA(df['close'].values, timeperiod=200)
# EMA slope over 5 bars normalized by ATR
df['ema50_slope'] = (df['ema50'] - df['ema50'].shift(5)) / df['atr']
df['trend_strength'] = np.where(df['adx'] > 25, 'Trending', 'Ranging')
df['trend_direction'] = np.where(df['ema50_slope'] > 0.2, 'Bullish', np.where(df['ema50_slope'] < -0.2, 'Bearish', 'Flat'))


# Combine trend and volatility to define market states
# E.g., Ranging + High Vol, Trending + High Vol, etc.
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

df['rsi'] = talib.RSI(df['close'].values, timeperiod=14)
# Z-Score
mean20 = df['close'].rolling(window=20).mean()
std20 = df['close'].rolling(window=20).std()
df['zscore'] = (df['close'] - mean20) / std20

print("\n=== Market Regime Distribution ===")
print(df['regime'].value_counts(normalize=True))

# ── Research Mean Reversion performance under different regimes ──
# Signal: Z-Score < -2.0 (Call candidates) or Z-Score > 2.0 (Put candidates)
z_threshold = 2.0

for regime_name, group in df.groupby('regime'):
    print(f"\n--- Regime: {regime_name} ---")
    
    # Call signals
    calls = group[group['zscore'] < -z_threshold]
    call_wr = calls['target_call'].mean()
    print(f"  Call Mean Reversion (Z < -{z_threshold}): {len(calls)} trades | Win Rate: {call_wr*100:.2f}%")
    
    # Put signals
    puts = group[group['zscore'] > z_threshold]
    put_wr = puts['target_put'].mean()
    print(f"  Put Mean Reversion  (Z >  {z_threshold}): {len(puts)} trades | Win Rate: {put_wr*100:.2f}%")

# Let's look at Trend Following breakout signals under different regimes
# Breakout Call: Close crosses BB upper band (indicating strong momentum)
# Breakout Put: Close crosses BB lower band
df['breakout_call_sig'] = (df['close'] > df['bb_upper']).astype(int)
df['breakout_put_sig'] = (df['close'] < df['bb_lower']).astype(int)

print("\n=== Trend Breakout Performance by Regime ===")
for regime_name, group in df.groupby('regime'):
    print(f"\n--- Regime: {regime_name} ---")
    
    calls = group[group['breakout_call_sig'] == 1]
    call_wr = calls['target_call'].mean()
    print(f"  Call Breakout: {len(calls)} trades | Win Rate: {call_wr*100:.2f}%")
    
    puts = group[group['breakout_put_sig'] == 1]
    put_wr = puts['target_put'].mean()
    print(f"  Put Breakout:  {len(puts)} trades | Win Rate: {put_wr*100:.2f}%")
