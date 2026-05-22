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
df['is_up_candle'] = (df['close'] > df['open']).astype(int)
df['consec_down_3'] = df['is_down_candle'].rolling(window=3).sum()
df['consec_up_3'] = df['is_up_candle'].rolling(window=3).sum()
df['rsi_diff'] = df['rsi'] - df['rsi'].shift(1)

# MACD
df['macd'], df['macdsignal'], df['macdhist'] = talib.MACD(df['close'].values, fastperiod=12, slowperiod=26, signalperiod=9)

# Define candidates
candidates = {}

# --- CATEGORY 1: MEAN REVERSION PATTERNS ---
# Pattern 1: Quiet Ranging - Call
candidates['MR1_Quiet_Call'] = (df['adx'] < 20) & (df['zscore'] < -1.8) & (df['rsi'] < 30), 'target_call'
# Pattern 2: Quiet Ranging - Put
candidates['MR2_Quiet_Put'] = (df['adx'] < 20) & (df['zscore'] > 1.8) & (df['rsi'] > 70), 'target_put'
# Pattern 3: Bull Trend - Put Reversion
candidates['MR3_Bull_Put'] = (df['ema50_slope'] >= 0.2) & (df['zscore'] > 2.0) & (df['rsi'] > 72), 'target_put'
# Pattern 4: Bear Trend - Call Reversion
candidates['MR4_Bear_Call'] = (df['ema50_slope'] <= -0.2) & (df['zscore'] < -1.5) & (df['rsi'] < 28) & (df['vol_ratio'] >= 1.0) & (df['consec_down_3'] == 3), 'target_call'

# --- CATEGORY 2: TREND FOLLOWING PATTERNS ---
# Pattern 5: Bull Trend Continuation - Call (Pullback to EMA)
candidates['TF1_Bull_Pullback_Call'] = (df['ema50_slope'] >= 0.2) & (df['close'] > df['ema50']) & (df['zscore'].between(-1.0, 0.0)) & (df['rsi'].between(45, 60)) & (df['vol_ratio'] >= 1.0), 'target_call'
# Pattern 6: Bear Trend Continuation - Put (Pullback to EMA)
candidates['TF2_Bear_Pullback_Put'] = (df['ema50_slope'] <= -0.2) & (df['close'] < df['ema50']) & (df['zscore'].between(0.0, 1.0)) & (df['rsi'].between(40, 55)) & (df['vol_ratio'] >= 1.0), 'target_put'
# Pattern 7: MACD Golden Cross in Bull Trend - Call
candidates['TF3_Bull_MACD_Call'] = (df['ema50_slope'] >= 0.2) & (df['macdhist'] > 0) & (df['macdhist'].shift(1) <= 0) & (df['rsi'] < 60), 'target_call'
# Pattern 8: MACD Death Cross in Bear Trend - Put
candidates['TF4_Bear_MACD_Put'] = (df['ema50_slope'] <= -0.2) & (df['macdhist'] < 0) & (df['macdhist'].shift(1) >= 0) & (df['rsi'] > 40), 'target_put'

# --- CATEGORY 3: BREAKOUT PATTERNS ---
# Pattern 9: Volatility Breakout - Call
candidates['BO1_Upper_Breakout_Call'] = (df['adx'] >= 20) & (df['close'] > df['close'].shift(1)) & (df['vol_ratio'] >= 1.5) & (df['rsi'] > 65), 'target_call'
# Pattern 10: Volatility Breakout - Put
candidates['BO2_Lower_Breakout_Put'] = (df['adx'] >= 20) & (df['close'] < df['close'].shift(1)) & (df['vol_ratio'] >= 1.5) & (df['rsi'] < 35), 'target_put'

print(f"{'Pattern Code':<25} | {'Trades':<8} | {'Daily Freq':<10} | {'Win Rate':<10}")
print("-" * 65)

for name, (cond, target_col) in candidates.items():
    subset = df[cond]
    total_trades = len(subset)
    daily_freq = total_trades / 90.0
    if total_trades > 0:
        wr = subset[target_col].mean()
        print(f"{name:<25} | {total_trades:<8} | {daily_freq:<10.2f} | {wr*100:<9.2f}%")
    else:
        print(f"{name:<25} | 0        | 0.00       | N/A")
