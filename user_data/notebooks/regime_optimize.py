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
df['target_put'] = (df['forward_return_2b'] < 0).astype(int)

# Indicators
df['atr'] = talib.ATR(df['high'].values, df['low'].values, df['close'].values, timeperiod=14)
df['bb_upper'], df['bb_middle'], df['bb_lower'] = talib.BBANDS(df['close'].values, timeperiod=20, nbdevup=2.0, nbdevdn=2.0)
df['adx'] = talib.ADX(df['high'].values, df['low'].values, df['close'].values, timeperiod=14)
df['ema50'] = talib.EMA(df['close'].values, timeperiod=50)
df['ema50_slope'] = (df['ema50'] - df['ema50'].shift(5)) / df['atr']

# Z-Score
mean20 = df['close'].rolling(window=20).mean()
std20 = df['close'].rolling(window=20).std()
df['zscore'] = (df['close'] - mean20) / std20
df['rsi'] = talib.RSI(df['close'].values, timeperiod=14)

# Regime definition function
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

print("Searching for optimal thresholds...")

# Wide search space to capture both extreme mean reversion and mild trend-pullbacks
call_candidates = [
    (-1.0, 45), (-1.2, 40), (-1.4, 35), (-1.6, 32), (-1.8, 30), (-2.0, 25), (-2.2, 20), (-2.4, 15)
]
put_candidates = [
    (1.0, 55), (1.2, 60), (1.4, 65), (1.6, 68), (1.8, 70), (2.0, 75), (2.2, 80), (2.4, 85)
]

for reg in ['Quiet Ranging', 'Bullish Trend', 'Bearish Trend']:
    print(f"\n=================== REGIME: {reg} ===================")
    idx_reg = df['regime'] == reg
    group = df[idx_reg]
    
    print("--- Optimize Call (Long) ---")
    best_wr = 0
    best_params = None
    best_trades = 0
    # Find parameter that maximizes win rate, subject to minimum trade count of 100
    for z_t, r_t in call_candidates:
        subset = group[(group['zscore'] < z_t) & (group['rsi'] < r_t)]
        if len(subset) >= 100:  # Increased min trade count for better statistical validity
            wr = subset['target_call'].mean()
            # We want to select parameters that yield a win rate > 56%
            if wr > best_wr:
                best_wr = wr
                best_params = (z_t, r_t)
                best_trades = len(subset)
    if best_params:
        print(f"  Best Call: Z < {best_params[0]} & RSI < {best_params[1]} | Win Rate: {best_wr*100:.2f}% | Trades: {best_trades}")
    else:
        print("  No Call configuration satisfied min trade count (100)")

    print("--- Optimize Put (Short) ---")
    best_wr = 0
    best_params = None
    best_trades = 0
    for z_t, r_t in put_candidates:
        subset = group[(group['zscore'] > z_t) & (group['rsi'] > r_t)]
        if len(subset) >= 100:
            wr = subset['target_put'].mean()
            if wr > best_wr:
                best_wr = wr
                best_params = (z_t, r_t)
                best_trades = len(subset)
    if best_params:
        print(f"  Best Put:  Z > {best_params[0]} & RSI > {best_params[1]} | Win Rate: {best_wr*100:.2f}% | Trades: {best_trades}")
    else:
        print("  No Put configuration satisfied min trade count (100)")
