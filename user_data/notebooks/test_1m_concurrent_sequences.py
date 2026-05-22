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

# Sequence of length 6
seq_len = 6
df['seq_str'] = ""
for shift in reversed(range(seq_len)):
    df['seq_str'] = df['seq_str'] + df['dir'].shift(shift).fillna(0).astype(int).astype(str)

df['combo_str'] = df['seq_str'] + "_" + df['vol_high'].astype(str) + "_" + df['large_range'].astype(str)
df = df.iloc[seq_len:].copy()

# Premium 1m combos (excluding the weak one)
call_combos = ['001000_1_1', '010011_0_1', '000000_1_1']
put_combos = ['000010_0_1', '011110_0_1', '011101_0_1', '001111_1_0']

df['raw_call'] = df['combo_str'].isin(call_combos).astype(int)
df['raw_put'] = df['combo_str'].isin(put_combos).astype(int)

def simulate_concurrent_1m(max_concurrent):
    active_trades = []  # list of expiry indices
    executed_trades = []
    
    raw_call_arr = df['raw_call'].values
    raw_put_arr = df['raw_put'].values
    target_call_arr = df['target_call'].values
    target_put_arr = df['target_put'].values
    
    for i in range(len(df)):
        # Remove expired trades
        active_trades = [expiry for expiry in active_trades if expiry > i]
        
        call_sig = raw_call_arr[i]
        put_sig = raw_put_arr[i]
        
        if len(active_trades) < max_concurrent:
            if call_sig == 1 and put_sig == 1:
                continue
            elif call_sig == 1:
                executed_trades.append({
                    'type': 'Call',
                    'is_win': target_call_arr[i]
                })
                active_trades.append(i + 10)  # expires in 10 bars
            elif put_sig == 1:
                executed_trades.append({
                    'type': 'Put',
                    'is_win': target_put_arr[i]
                })
                active_trades.append(i + 10)  # expires in 10 bars

    trades_df = pd.DataFrame(executed_trades)
    if len(trades_df) > 0:
        total = len(trades_df)
        wins = trades_df['is_win'].sum()
        wr = wins / total
        trades_per_day = total / 90.0
        payout = 0.80
        expectancy = wr * payout - (1 - wr) * 1.0
        return total, trades_per_day, wr, expectancy
    else:
        return 0, 0.0, 0.0, 0.0

print(f"{'Max Concurrent Trades':<25} | {'Total Trades':<12} | {'Daily Freq':<10} | {'Win Rate':<10} | {'Expectancy':<10}")
print("-" * 75)
for limit in [1, 2, 3, 4, 5]:
    tot, freq, wr, exp = simulate_concurrent_1m(limit)
    print(f"{limit:<25} | {tot:<12} | {freq:<10.2f} | {wr*100:<9.2f}% | {exp:<.4f} R")
