import pandas as pd
import numpy as np

# Load 1m K-lines
df = pd.read_feather("user_data/data/binance/futures/BTC_USDT_USDT-1m-futures.feather")
df.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
df['date'] = pd.to_datetime(df['date'])
for col in ['open', 'high', 'low', 'close', 'volume']:
    df[col] = pd.to_numeric(df[col], errors='coerce').astype(float)

# Target (close price 10 minutes later)
df_1m_lookup = df[['date', 'close']].rename(columns={'close': 'close_10m_later'})
df_1m_lookup['date'] = df_1m_lookup['date'] - pd.Timedelta(minutes=10)
df = pd.merge(df, df_1m_lookup, on='date', how='left')
df = df.dropna(subset=['close_10m_later']).reset_index(drop=True)

df['forward_return_10m'] = (df['close_10m_later'] - df['close']) / df['close']
df['target_call'] = (df['forward_return_10m'] > 0).astype(int)
df['target_put'] = (df['forward_return_10m'] < 0).astype(int)

# Direction, Volume, and Range indicators
df['dir'] = (df['close'] > df['open']).astype(int)
vol_median = df['volume'].rolling(window=30).median()
df['vol_high'] = (df['volume'] > vol_median).astype(int)
full_range = df['high'] - df['low']
range_mean = full_range.rolling(window=30).mean()
df['large_range'] = (full_range > range_mean).astype(int)

# Sequence of length 6 (6 minutes of history)
seq_len = 6
df['seq_str'] = ""
for shift in reversed(range(seq_len)):
    df['seq_str'] = df['seq_str'] + df['dir'].shift(shift).fillna(0).astype(int).astype(str)

df['combo_str'] = df['seq_str'] + "_" + df['vol_high'].astype(str) + "_" + df['large_range'].astype(str)
df = df.iloc[seq_len:].copy()

# Print stats for all combos
combo_stats = []
for combo, grp in df.groupby('combo_str'):
    trades = len(grp)
    freq = trades / 90.0
    call_wr = grp['target_call'].mean()
    put_wr = grp['target_put'].mean()
    if trades >= 150: # Ensure statistical significance
        combo_stats.append((combo, trades, freq, call_wr, put_wr))

print("=== 1m K-Line Sequence-6 Combos (Top Call Patterns, WR >= 56%) ===")
call_combos = [x for x in combo_stats if x[3] >= 0.56]
call_combos.sort(key=lambda x: x[3], reverse=True)
print(f"{'Combo':<15} | {'Trades':<8} | {'Daily Freq':<10} | {'Call Win%':<10}")
print("-" * 50)
total_call_freq = 0
for r in call_combos[:15]:
    total_call_freq += r[2]
    print(f"{r[0]:<15} | {r[1]:<8} | {r[2]:<10.2f} | {r[3]*100:.2f}%")
print(f"Total Call Candidate Freq: {total_call_freq:.2f} trades/day")

print("\n=== 1m K-Line Sequence-6 Combos (Top Put Patterns, WR >= 56%) ===")
put_combos = [x for x in combo_stats if x[4] >= 0.56]
put_combos.sort(key=lambda x: x[4], reverse=True)
print(f"{'Combo':<15} | {'Trades':<8} | {'Daily Freq':<10} | {'Put Win%':<10}")
print("-" * 50)
total_put_freq = 0
for r in put_combos[:15]:
    total_put_freq += r[2]
    print(f"{r[0]:<15} | {r[1]:<8} | {r[2]:<10.2f} | {r[4]*100:.2f}%")
print(f"Total Put Candidate Freq: {total_put_freq:.2f} trades/day")
