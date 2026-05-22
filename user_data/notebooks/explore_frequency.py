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

# Secondary features
df['vol_median_20'] = df['volume'].rolling(window=20).median()
df['vol_ratio'] = df['volume'] / df['vol_median_20']

df['is_down_candle'] = (df['close'] < df['open']).astype(int)
df['consec_down_3'] = df['is_down_candle'].rolling(window=3).sum()
df['rsi_diff'] = df['rsi'] - df['rsi'].shift(1)

def run_simulation(z_bear, rsi_bear, z_bull, rsi_bull, use_filters):
    raw_call = np.zeros(len(df))
    raw_put = np.zeros(len(df))
    
    # Bullish Trend -> Put only
    idx_bull = (df['regime'] == 'Bullish Trend').values
    raw_put[idx_bull & (df['zscore'] > z_bull).values & (df['rsi'] > rsi_bull).values] = 1
    
    # Bearish Trend -> Call only
    idx_bear = (df['regime'] == 'Bearish Trend').values
    if use_filters:
        raw_call[
            idx_bear & 
            (df['zscore'] < -z_bear).values & 
            (df['rsi'] < rsi_bear).values &
            (df['vol_ratio'] >= 1.0).values &
            (df['consec_down_3'] == 3).values &
            (df['rsi_diff'] <= 0).values
        ] = 1
    else:
        raw_call[idx_bear & (df['zscore'] < -z_bear).values & (df['rsi'] < rsi_bear).values] = 1

    # Simulation loop
    wins = 0
    total = 0
    cooldown = 0
    
    target_call_arr = df['target_call'].values
    target_put_arr = df['target_put'].values
    
    for i in range(len(df)):
        if cooldown > 0:
            cooldown -= 1
            continue
        
        c_sig = raw_call[i]
        p_sig = raw_put[i]
        
        if c_sig == 1 and p_sig == 1:
            continue
        elif c_sig == 1:
            total += 1
            wins += target_call_arr[i]
            cooldown = 10
        elif p_sig == 1:
            total += 1
            wins += target_put_arr[i]
            cooldown = 10
            
    wr = wins / total if total > 0 else 0
    trades_per_day = total / 90.0
    # Binary Payout is 80% (0.80 R)
    expectancy = wr * 0.80 - (1 - wr) * 1.0 if total > 0 else 0
    return total, trades_per_day, wr, expectancy

# Test different levels of parameter looseness
scenarios = [
    # (z_bear, rsi_bear, z_bull, rsi_bull, use_filters, label)
    (1.5, 25, 2.2, 75, True, "1. Final Opt Strategy (Strict + Filters)"),
    (1.5, 25, 2.2, 75, False, "2. Base Regime Strategy (No Filters)"),
    (1.2, 30, 1.8, 70, False, "3. Medium Loose (Lower Z & RSI)"),
    (1.0, 35, 1.5, 65, False, "4. High Loose (Early reversion entries)"),
    (0.8, 40, 1.2, 60, False, "5. Ultra Loose (Trade minor deviations)"),
    (0.0, 100, 0.0, 0, False, "6. Max Trades (Always entry in regime)"),
]

print("Max 10m Cycles per Day: 144\n")
print(f"{'Scenario Name':<45} | {'Trades':<8} | {'Daily Freq':<10} | {'Win Rate':<10} | {'Expectancy':<10}")
print("-" * 95)

for z_bear, rsi_bear, z_bull, rsi_bull, use_filters, label in scenarios:
    tot, t_day, wr, exp = run_simulation(z_bear, rsi_bear, z_bull, rsi_bull, use_filters)
    print(f"{label:<45} | {tot:<8} | {t_day:<10.2f} | {wr*100:<9.2f}% | {exp:<.4f} R")
