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

# Secondary features
df['vol_median_20'] = df['volume'].rolling(window=20).median()
df['vol_ratio'] = df['volume'] / df['vol_median_20']

df['is_down_candle'] = (df['close'] < df['open']).astype(int)
df['consec_down_3'] = df['is_down_candle'].rolling(window=3).sum()
df['rsi_diff'] = df['rsi'] - df['rsi'].shift(1)

# Raw signals
df['raw_call'] = 0
df['raw_put'] = 0

# Apply the optimized configuration with bearish call filters
# 1. Quiet Ranging
idx_range = df['regime'] == 'Quiet Ranging'
df.loc[idx_range & (df['zscore'] < -1.8) & (df['rsi'] < 30), 'raw_call'] = 1
df.loc[idx_range & (df['zscore'] > 1.8) & (df['rsi'] > 70), 'raw_put'] = 1

# 2. Bullish Trend -> Trade Put only
idx_bull = df['regime'] == 'Bullish Trend'
df.loc[idx_bull & (df['zscore'] > 1.8) & (df['rsi'] > 70), 'raw_put'] = 1

# 3. Bearish Trend -> Trade Call only WITH exhaustion and volume filters
idx_bear = df['regime'] == 'Bearish Trend'
df.loc[
    idx_bear & 
    (df['zscore'] < -1.6) & 
    (df['rsi'] < 32) & 
    (df['vol_ratio'] >= 1.0) &      # Volume must be above median
    (df['consec_down_3'] == 3) &    # 3 consecutive red candles
    (df['rsi_diff'] <= 0),          # No late confirmation
    'raw_call'
] = 1

# Non-overlapping Simulation
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
        cooldown = 2
    elif put_sig == 1:
        executed_trades.append({
            'regime': df.loc[i, 'regime'],
            'type': 'Put',
            'is_win': df.loc[i, 'target_put']
        })
        cooldown = 2

trades_df = pd.DataFrame(executed_trades)

if len(trades_df) > 0:
    print("=== Performance Breakdown (Non-Overlapping) ===")
    for (reg, ttype), grp in trades_df.groupby(['regime', 'type']):
        print(f"{reg} - {ttype}: {len(grp)} trades | Win Rate: {grp['is_win'].mean()*100:.2f}%")
        
    print("\n=== Combined Performance (Non-Overlapping) ===")
    total = len(trades_df)
    wins = trades_df['is_win'].sum()
    wr = wins / total
    payout = 0.80
    expectancy = wr * payout - (1 - wr) * 1.0
    profit = wins * 80 - (total - wins) * 100
    print(f"Total Trades: {total}")
    print(f"Win Rate: {wr*100:.2f}%")
    print(f"Expectancy: {expectancy:.4f} R")
    print(f"Net Profit ($100 per trade): ${profit:.0f}")
else:
    print("No trades executed")
