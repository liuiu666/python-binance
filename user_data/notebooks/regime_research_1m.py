import pandas as pd
import numpy as np
import talib

# Load the 1m feather file
df = pd.read_feather("user_data/data/binance/futures/BTC_USDT_USDT-1m-futures.feather")
df.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
print("Loaded 1m K-lines:", len(df))

for col in ['open', 'high', 'low', 'close', 'volume']:
    df[col] = pd.to_numeric(df[col], errors='coerce').astype(float)

# Target: 10-minute forward return (after 10 bars of 1m)
df['forward_return_10b'] = (df['close'].shift(-10) - df['close']) / df['close']
df['target_call'] = (df['forward_return_10b'] > 0).astype(int)
df['target_put'] = (df['forward_return_10b'] < 0).astype(int)

# Indicators - Fast/Micro versions (matching 5m settings)
df['atr'] = talib.ATR(df['high'].values, df['low'].values, df['close'].values, timeperiod=14)
df['bb_upper'], df['bb_middle'], df['bb_lower'] = talib.BBANDS(df['close'].values, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
df['adx'] = talib.ADX(df['high'].values, df['low'].values, df['close'].values, timeperiod=14)
df['ema50'] = talib.EMA(df['close'].values, timeperiod=50)
df['ema50_slope'] = (df['ema50'] - df['ema50'].shift(5)) / df['atr']

# Z-Score over 20 bars
mean20 = df['close'].rolling(window=20).mean()
std20 = df['close'].rolling(window=20).std()
df['zscore'] = (df['close'] - mean20) / std20
df['rsi'] = talib.RSI(df['close'].values, timeperiod=14)

# Regime definition
def get_regime(row):
    if pd.isna(row['adx']) or pd.isna(row['ema50_slope']):
        return 'Unknown'
    if row['adx'] < 20:
        return 'Quiet Ranging'
    elif row['adx'] >= 20 and abs(row['ema50_slope']) < 0.2:
        return 'Volatile Ranging'
    elif row['ema50_slope'] >= 0.2:
        return 'Bullish Trend'
    else:
        return 'Bearish Trend'

df['regime'] = df.apply(get_regime, axis=1)

print("\n=== Market Regime Distribution (1m Fast) ===")
print(df['regime'].value_counts(normalize=True))

# Search candidates
z_candidates = [1.5, 1.8, 2.0, 2.2]
rsi_lower_candidates = [25, 30, 35]
rsi_upper_candidates = [65, 70, 75]

for reg in ['Quiet Ranging', 'Bullish Trend', 'Bearish Trend']:
    print(f"\n=================== REGIME (1m Fast): {reg} ===================")
    idx_reg = df['regime'] == reg
    group = df[idx_reg]
    
    print("--- Optimize Call (Long) ---")
    best_wr = 0
    best_params = None
    best_trades = 0
    for z_t in z_candidates:
        for r_t in rsi_lower_candidates:
            subset = group[(group['zscore'] < -z_t) & (group['rsi'] < r_t)]
            if len(subset) >= 50:
                wr = subset['target_call'].mean()
                if wr > best_wr:
                    best_wr = wr
                    best_params = (-z_t, r_t)
                    best_trades = len(subset)
    if best_params:
        print(f"  Best Call: Z < {best_params[0]} & RSI < {best_params[1]} | Win Rate: {best_wr*100:.2f}% | Trades: {best_trades}")
    
    print("--- Optimize Put (Short) ---")
    best_wr = 0
    best_params = None
    best_trades = 0
    for z_t in z_candidates:
        for r_t in rsi_upper_candidates:
            subset = group[(group['zscore'] > z_t) & (group['rsi'] > r_t)]
            if len(subset) >= 50:
                wr = subset['target_put'].mean()
                if wr > best_wr:
                    best_wr = wr
                    best_params = (z_t, r_t)
                    best_trades = len(subset)
    if best_params:
        print(f"  Best Put:  Z > {best_params[0]} & RSI > {best_params[1]} | Win Rate: {best_wr*100:.2f}% | Trades: {best_trades}")
