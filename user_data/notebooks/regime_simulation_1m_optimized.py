import pandas as pd
import numpy as np
import talib

# Load the 1m feather file
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

# Secondary features for bearish trend call optimization
df['vol_median_20'] = df['volume'].rolling(window=20).median()
df['vol_ratio'] = df['volume'] / df['vol_median_20']

df['is_down_candle'] = (df['close'] < df['open']).astype(int)
df['consec_down_3'] = df['is_down_candle'].rolling(window=3).sum()
df['rsi_diff'] = df['rsi'] - df['rsi'].shift(1)

# Raw signals based on optimized 1m settings
df['raw_call'] = 0
df['raw_put'] = 0

# 1. Bearish Trend -> Call only (Z < -1.5 & RSI < 25) + filters
idx_bear = df['regime'] == 'Bearish Trend'
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
idx_bull = df['regime'] == 'Bullish Trend'
df.loc[idx_bull & (df['zscore'] > 2.2) & (df['rsi'] > 75), 'raw_put'] = 1

# Non-overlapping Simulation (Max 1 Open Trade, held for 10 bars)
executed_trades = []
cooldown = 0

for i in range(len(df)):
    if cooldown > 0:
        cooldown -= 1
        continue
    
    call_sig = df.loc[i, 'raw_call']
    put_sig = df.loc[i, 'raw_put']
    
    if call_sig == 1 and put_sig == 1:
        continue
    elif call_sig == 1:
        executed_trades.append({
            'regime': df.loc[i, 'regime'],
            'type': 'Call',
            'is_win': df.loc[i, 'target_call']
        })
        cooldown = 10  # Hold for 10 bars (10 mins)
    elif put_sig == 1:
        executed_trades.append({
            'regime': df.loc[i, 'regime'],
            'type': 'Put',
            'is_win': df.loc[i, 'target_put']
        })
        cooldown = 10  # Hold for 10 bars (10 mins)

trades_df = pd.DataFrame(executed_trades)

if len(trades_df) > 0:
    print("=== 1m Optimized Performance Breakdown (Non-Overlapping) ===")
    for (reg, ttype), grp in trades_df.groupby(['regime', 'type']):
        print(f"{reg} - {ttype}: {len(grp)} trades | Win Rate: {grp['is_win'].mean()*100:.2f}%")
        
    print("\n=== 1m Optimized Combined Performance (Non-Overlapping) ===")
    total = len(trades_df)
    wins = trades_df['is_win'].sum()
    wr = wins / total
    payout = 0.80
    expectancy = wr * payout - (1 - wr) * 1.0
    profit = wins * 80 - (total - wins) * 100
    print(f"Total Trades: {total} (over ~90 days)")
    print(f"Trades per Day: {total/90:.2f}")
    print(f"Win Rate: {wr*100:.2f}%")
    print(f"Expectancy: {expectancy:.4f} R")
    print(f"Net Profit ($100 per trade): ${profit:.0f}")
else:
    print("No trades executed")
