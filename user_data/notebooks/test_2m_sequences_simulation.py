import pandas as pd
import numpy as np

# Load 1m K-lines
df_1m = pd.read_feather("user_data/data/binance/futures/BTC_USDT_USDT-1m-futures.feather")
df_1m.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
df_1m['date'] = pd.to_datetime(df_1m['date'])
for col in ['open', 'high', 'low', 'close', 'volume']:
    df_1m[col] = pd.to_numeric(df_1m[col], errors='coerce').astype(float)

# Resample to 2m
tf = 2
df = df_1m.set_index('date').resample(f'{tf}min').agg({
    'open': 'first',
    'high': 'max',
    'low': 'min',
    'close': 'last',
    'volume': 'sum'
}).dropna().reset_index()

# Target (close price 10 minutes later)
df_1m_lookup = df_1m[['date', 'close']].rename(columns={'close': 'close_10m_later'})
df_1m_lookup['date'] = df_1m_lookup['date'] - pd.Timedelta(minutes=10)
df = pd.merge(df, df_1m_lookup, on='date', how='left')
df = df.dropna(subset=['close_10m_later']).reset_index(drop=True)

df['forward_return_10m'] = (df['close_10m_later'] - df['close']) / df['close']
df['target_call'] = (df['forward_return_10m'] > 0).astype(int)
df['target_put'] = (df['forward_return_10m'] < 0).astype(int)

# Direction, Volume, and Range indicators
df['dir'] = (df['close'] > df['open']).astype(int)
vol_median = df['volume'].rolling(window=15).median()
df['vol_high'] = (df['volume'] > vol_median).astype(int)
full_range = df['high'] - df['low']
range_mean = full_range.rolling(window=15).mean()
df['large_range'] = (full_range > range_mean).astype(int)

# Sequence of length 5
seq_len = 5
df['seq_str'] = ""
for shift in reversed(range(seq_len)):
    df['seq_str'] = df['seq_str'] + df['dir'].shift(shift).fillna(0).astype(int).astype(str)

df['combo_str'] = df['seq_str'] + "_" + df['vol_high'].astype(str) + "_" + df['large_range'].astype(str)
df = df.iloc[seq_len:].copy()

# Selected high-win-rate combos
call_combos = ['00010_0_1', '00000_1_1', '10000_1_1', '00000_1_0', '01100_1_1']
put_combos = ['10011_1_0', '11011_1_1']

df['raw_call'] = df['combo_str'].isin(call_combos).astype(int)
df['raw_put'] = df['combo_str'].isin(put_combos).astype(int)

# Cooldown simulation
cooldown = 0
wins = 0
total = 0
executed_trades = []

raw_call_arr = df['raw_call'].values
raw_put_arr = df['raw_put'].values
target_call_arr = df['target_call'].values
target_put_arr = df['target_put'].values
combo_str_arr = df['combo_str'].values

for i in range(len(df)):
    if cooldown > 0:
        cooldown -= 1
        continue
    
    c = raw_call_arr[i]
    p = raw_put_arr[i]
    
    if c == 1 and p == 1:
        continue
    elif c == 1:
        total += 1
        wins += target_call_arr[i]
        executed_trades.append({
            'type': 'Call',
            'combo': combo_str_arr[i],
            'is_win': target_call_arr[i]
        })
        cooldown = 5  # 10 minutes = 5 bars of 2m
    elif p == 1:
        total += 1
        wins += target_put_arr[i]
        executed_trades.append({
            'type': 'Put',
            'combo': combo_str_arr[i],
            'is_win': target_put_arr[i]
        })
        cooldown = 5  # 10 minutes = 5 bars of 2m

trades_df = pd.DataFrame(executed_trades)
print(f"=== 2m Non-Overlapping Sequence-5 Combos Simulation ===")
print(f"Total Trades: {total} (over ~90 days)")
print(f"Daily Frequency: {total/90:.2f} trades/day")
print(f"Combined Win Rate: {wins/total*100:.2f}%" if total > 0 else "N/A")
print(f"Expectancy: {(wins/total)*0.80 - (1 - (wins/total))*1.0:.4f} R" if total > 0 else "N/A")

if len(trades_df) > 0:
    print("\n--- Breakdown by Combo ---")
    for combo, grp in trades_df.groupby('combo'):
        print(f"Combo {combo}: {len(grp)} trades | Win Rate: {grp['is_win'].mean()*100:.2f}%")
