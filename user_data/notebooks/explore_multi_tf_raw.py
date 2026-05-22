import pandas as pd
import numpy as np

# Load 1m K-lines
df_1m = pd.read_feather("user_data/data/binance/futures/BTC_USDT_USDT-1m-futures.feather")
df_1m.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
df_1m['date'] = pd.to_datetime(df_1m['date'])
for col in ['open', 'high', 'low', 'close', 'volume']:
    df_1m[col] = pd.to_numeric(df_1m[col], errors='coerce').astype(float)

# We find the close price 10 minutes later by matching timestamps.
df_1m_lookup = df_1m[['date', 'close']].rename(columns={'close': 'close_10m_later'})
df_1m_lookup['date'] = df_1m_lookup['date'] - pd.Timedelta(minutes=10)

def prepare_tf_data(tf_minutes, seq_len, call_combos, put_combos):
    # Resample
    df = df_1m.set_index('date').resample(f'{tf_minutes}min').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna().reset_index()
    
    # Crucial: Shift the timestamp to the END of the bar (when it closes and we enter the trade)
    df['date'] = df['date'] + pd.Timedelta(minutes=tf_minutes)
    
    # Merge target (price 10 minutes after the END of the bar)
    df = pd.merge(df, df_1m_lookup, on='date', how='left')
    df = df.dropna(subset=['close_10m_later']).reset_index(drop=True)
    df['forward_return_10m'] = (df['close_10m_later'] - df['close']) / df['close']
    df['target_call'] = (df['forward_return_10m'] > 0).astype(int)
    df['target_put'] = (df['forward_return_10m'] < 0).astype(int)
    
    # Features
    df['dir'] = (df['close'] > df['open']).astype(int)
    vol_median = df['volume'].rolling(window=15).median()
    df['vol_high'] = (df['volume'] > vol_median).astype(int)
    full_range = df['high'] - df['low']
    range_mean = full_range.rolling(window=15).mean()
    df['large_range'] = (full_range > range_mean).astype(int)
    
    df['seq_str'] = ""
    for shift in reversed(range(seq_len)):
        df['seq_str'] = df['seq_str'] + df['dir'].shift(shift).fillna(0).astype(int).astype(str)
        
    df['combo_str'] = df['seq_str'] + "_" + df['vol_high'].astype(str) + "_" + df['large_range'].astype(str)
    df = df.iloc[seq_len:].copy()
    
    # Mark signals
    df['raw_call'] = df['combo_str'].isin(call_combos).astype(int)
    df['raw_put'] = df['combo_str'].isin(put_combos).astype(int)
    
    return df[['date', 'raw_call', 'raw_put', 'target_call', 'target_put', 'combo_str']]

# 1. Prepare 2m, 3m, 4m datasets
print("Preparing datasets...")
# We use the premium combos only!
# For 2m: 5 Call combos, 2 Put combos
df_2m_sig = prepare_tf_data(2, 5, ['00010_0_1', '00000_1_1', '10000_1_1', '00000_1_0', '01100_1_1'], ['10011_1_0', '11011_1_1'])
# For 3m: 3 Call combos
df_3m_sig = prepare_tf_data(3, 4, ['0000_1_1', '0100_0_1', '0000_0_1'], [])
# For 4m: 3 Call combos, 4 Put combos
df_4m_sig = prepare_tf_data(4, 4, ['0110_0_1', '1101_1_0', '1101_1_1', '1000_1_0'], ['1101_1_0', '1101_1_1', '1100_0_1', '1011_0_1'])

# 2. Align them to a 1-minute timeline to simulate joint execution
df_timeline = df_1m[['date', 'close']].copy()

# Merge 2m signals
df_2m_sig = df_2m_sig.rename(columns={'raw_call': 'call_2m', 'raw_put': 'put_2m', 'target_call': 'target_call_2m', 'target_put': 'target_put_2m'})
df_timeline = pd.merge(df_timeline, df_2m_sig, on='date', how='left')

# Merge 3m signals
df_3m_sig = df_3m_sig.rename(columns={'raw_call': 'call_3m', 'raw_put': 'put_3m', 'target_call': 'target_call_3m', 'target_put': 'target_put_3m'})
df_timeline = pd.merge(df_timeline, df_3m_sig, on='date', how='left')

# Merge 4m signals
df_4m_sig = df_4m_sig.rename(columns={'raw_call': 'call_4m', 'raw_put': 'put_4m', 'target_call': 'target_call_4m', 'target_put': 'target_put_4m'})
df_timeline = pd.merge(df_timeline, df_4m_sig, on='date', how='left')

df_timeline = df_timeline.fillna(0)

# Target: 10-minute return from the 1m timeline itself (the true target)
df_timeline['forward_return_10m'] = (df_timeline['close'].shift(-10) - df_timeline['close']) / df_timeline['close']
df_timeline['true_target_call'] = (df_timeline['forward_return_10m'] > 0).astype(int)
df_timeline['true_target_put'] = (df_timeline['forward_return_10m'] < 0).astype(int)
df_timeline = df_timeline.dropna(subset=['forward_return_10m']).reset_index(drop=True)

# Joint Simulation (10-minute cooldown = 10 bars of 1m)
cooldown = 0
wins = 0
total = 0
executed = []

c2 = df_timeline['call_2m'].values
p2 = df_timeline['put_2m'].values
c3 = df_timeline['call_3m'].values
p3 = df_timeline['put_3m'].values
c4 = df_timeline['call_4m'].values
p4 = df_timeline['put_4m'].values

t_call = df_timeline['true_target_call'].values
t_put = df_timeline['true_target_put'].values

for i in range(len(df_timeline)):
    if cooldown > 0:
        cooldown -= 1
        continue
    
    # Check signals: 2m takes highest priority, then 3m, then 4m
    call_sig = c2[i] or c3[i] or c4[i]
    put_sig = p2[i] or p3[i] or p4[i]
    
    if call_sig == 1 and put_sig == 1:
        continue
    elif call_sig == 1:
        total += 1
        wins += t_call[i]
        cooldown = 10  # 10 minutes lock
        executed.append('Call')
    elif put_sig == 1:
        total += 1
        wins += t_put[i]
        cooldown = 10  # 10 minutes lock
        executed.append('Put')

print("\n=== Multi-Timeframe Raw Sequence Joint Simulation ===")
print(f"Total Trades: {total} (over ~90 days)")
print(f"Daily Frequency: {total/90:.2f} trades/day")
print(f"Combined Win Rate: {wins/total*100:.2f}%" if total > 0 else "N/A")
print(f"Expectancy: {(wins/total)*0.80 - (1 - (wins/total))*1.0:.4f} R" if total > 0 else "N/A")
print(f"Net Profit ($100 per trade): ${wins*80 - (total-wins)*100:.0f}")
