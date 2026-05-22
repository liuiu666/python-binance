import pandas as pd
import numpy as np

# Load 1m K-lines
df_1m = pd.read_feather("user_data/data/binance/futures/BTC_USDT_USDT-1m-futures.feather")
df_1m.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
df_1m['date'] = pd.to_datetime(df_1m['date'])
for col in ['open', 'high', 'low', 'close', 'volume']:
    df_1m[col] = pd.to_numeric(df_1m[col], errors='coerce').astype(float)

# We want to analyze 2-minute blocks.
# Let's align 1m bars into 2-minute pairs:
# Bar 0 (Even index): first 1m bar of the 2-minute period
# Bar 1 (Odd index): second 1m bar of the 2-minute period
df_even = df_1m.iloc[0::2].reset_index(drop=True)
df_odd = df_1m.iloc[1::2].reset_index(drop=True)

# Ensure they match timestamps (odd should be exactly 1 minute after even)
# We crop to the minimum length
n = min(len(df_even), len(df_odd))
df_even = df_even.iloc[:n]
df_odd = df_odd.iloc[:n]

# Build the 2m dataframe
df_2m = pd.DataFrame()
df_2m['date'] = df_even['date']
df_2m['open'] = df_even['open']
df_2m['high'] = np.maximum(df_even['high'], df_odd['high'])
df_2m['low'] = np.minimum(df_even['low'], df_odd['low'])
df_2m['close'] = df_odd['close']
df_2m['volume'] = df_even['volume'] + df_odd['volume']

# Intra-bar features:
df_2m['body1'] = df_even['close'] - df_even['open']
df_2m['body2'] = df_odd['close'] - df_odd['open']
df_2m['vol1'] = df_even['volume']
df_2m['vol2'] = df_odd['volume']
df_2m['range1'] = df_even['high'] - df_even['low']
df_2m['range2'] = df_odd['high'] - df_odd['low']

# Expiry target: Close exactly 10 minutes after the END of this 2m bar (which is 10 minutes from the odd bar close)
df_1m_lookup = df_1m[['date', 'close']].rename(columns={'close': 'close_10m_later', 'date': 'lookup_date'})
# The end of the 2m bar is the timestamp of df_odd
df_2m['end_time'] = df_odd['date']
df_2m = pd.merge(df_2m, df_1m_lookup, left_on='end_time', right_on='lookup_date', how='left')
# Match the close price 10 minutes later
df_2m['lookup_date_10m'] = df_2m['end_time'] + pd.Timedelta(minutes=10)
df_2m = pd.merge(df_2m, df_1m_lookup.rename(columns={'close_10m_later': 'close_at_exit', 'lookup_date': 'lookup_date_10m'}), on='lookup_date_10m', how='left')

df_2m['forward_return_10m'] = (df_2m['close_at_exit'] - df_2m['close']) / df_2m['close']
df_2m['target_call'] = (df_2m['forward_return_10m'] > 0).astype(int)
df_2m['target_put'] = (df_2m['forward_return_10m'] < 0).astype(int)
df_2m = df_2m.dropna(subset=['forward_return_10m']).reset_index(drop=True)

print(f"Constructed 2m dataset: {len(df_2m)} rows")

# Let's define the "Absorption Pattern" (阻截模式):
# For Call: 
# - First 1m bar is a significant down bar: body1 < -0.001 * open
# - Second 1m bar is a small body/doji (selling stopped): abs(body2) < 0.2 * range2
# - Second 1m bar volume is significantly higher than the first: vol2 > 1.5 * vol1 (liquidity absorption at the bottom)
cond_abs_call = (df_2m['body1'] < -0.001 * df_2m['open']) & (df_2m['body2'].abs() < 0.3 * df_2m['range2']) & (df_2m['vol2'] > 1.2 * df_2m['vol1'])

# For Put:
# - First 1m bar is a significant up bar: body1 > 0.001 * open
# - Second 1m bar is a small body/doji (buying stopped): abs(body2) < 0.2 * range2
# - Second 1m bar volume is significantly higher than the first: vol2 > 1.2 * vol1 (liquidity absorption at the top)
cond_abs_put = (df_2m['body1'] > 0.001 * df_2m['open']) & (df_2m['body2'].abs() < 0.3 * df_2m['range2']) & (df_2m['vol2'] > 1.2 * df_2m['vol1'])

# Let's evaluate these absorption patterns!
sub_call = df_2m[cond_abs_call]
sub_put = df_2m[cond_abs_put]

print("\n=== Raw Absorption Pattern (No Cooldown) ===")
print(f"Call Absorption: {len(sub_call)} trades | Win Rate: {sub_call['target_call'].mean()*100:.2f}% | Freq/Day: {len(sub_call)/90:.2f}")
print(f"Put Absorption: {len(sub_put)} trades | Win Rate: {sub_put['target_put'].mean()*100:.2f}% | Freq/Day: {len(sub_put)/90:.2f}")

# Let's run a simulation with 10-minute cooldown
def simulate_cooldown_2m(cond_call, cond_put):
    cooldown = 0
    wins = 0
    total = 0
    
    c_arr = cond_call.astype(int).values
    p_arr = cond_put.astype(int).values
    t_call = df_2m['target_call'].values
    t_put = df_2m['target_put'].values
    
    for i in range(len(df_2m)):
        if cooldown > 0:
            cooldown -= 1
            continue
        
        c = c_arr[i]
        p = p_arr[i]
        
        if c == 1 and p == 1:
            continue
        elif c == 1:
            total += 1
            wins += t_call[i]
            cooldown = 5 # 10 minutes = 5 bars of 2m
        elif p == 1:
            total += 1
            wins += t_put[i]
            cooldown = 5
            
    wr = wins / total if total > 0 else 0
    freq = total / 90.0
    return total, freq, wr

tot, freq, wr = simulate_cooldown_2m(cond_abs_call, cond_abs_put)
print("\n=== Absorption Pattern (with 10-minute Cooldown) ===")
print(f"Total Trades: {tot} | Daily Freq: {freq:.2f} | Win Rate: {wr*100:.2f}%")
