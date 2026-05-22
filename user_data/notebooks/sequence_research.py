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

def resample_and_analyze_sequences(timeframe_minutes, seq_len):
    print(f"\n==========================================")
    print(f"ANALYZING {timeframe_minutes}m TIMEFRAME WITH SEQUENCE LENGTH {seq_len}")
    print(f"==========================================")
    
    # 1. Resample
    df = df_1m.set_index('date').resample(f'{timeframe_minutes}min').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna().reset_index()
    
    # 2. Merge target (close price 10 minutes later)
    df = pd.merge(df, df_1m_lookup, on='date', how='left')
    df = df.dropna(subset=['close_10m_later']).reset_index(drop=True)
    
    df['forward_return_10m'] = (df['close_10m_later'] - df['close']) / df['close']
    df['target_call'] = (df['forward_return_10m'] > 0).astype(int)
    df['target_put'] = (df['forward_return_10m'] < 0).astype(int)
    
    # 3. Define raw price action properties
    # Let's define:
    # - Direction of each candle: 1 if Close > Open, 0 if Close <= Open
    df['dir'] = (df['close'] > df['open']).astype(int)
    
    # - Relative volume: 1 if volume > median of last 10 bars, else 0
    vol_median = df['volume'].rolling(window=10).median()
    df['vol_high'] = (df['volume'] > vol_median).astype(int)
    
    # - Size relative to average range: 1 if body is large, else 0
    full_range = df['high'] - df['low']
    range_mean = full_range.rolling(window=10).mean()
    df['large_range'] = (full_range > range_mean).astype(int)
    
    # Shift to construct sequence strings
    # For example, if seq_len = 4, we want a string like '0101' representing the last 4 bar directions.
    df['seq_str'] = ""
    for shift in reversed(range(seq_len)):
        df['seq_str'] = df['seq_str'] + df['dir'].shift(shift).fillna(0).astype(int).astype(str)
        
    # We drop the first seq_len rows because their sequence is incomplete
    df_clean = df.iloc[seq_len:].copy()
    
    # Group by sequence string and analyze
    print(f"\n--- Direction Sequences ---")
    print(f"{'Sequence':<12} | {'Trades':<8} | {'Daily Freq':<10} | {'Call Win%':<10} | {'Put Win%':<10}")
    print("-" * 60)
    
    seq_stats = []
    for seq, grp in df_clean.groupby('seq_str'):
        trades = len(grp)
        freq = trades / 90.0
        call_wr = grp['target_call'].mean()
        put_wr = grp['target_put'].mean()
        seq_stats.append((seq, trades, freq, call_wr, put_wr))
        
    # Print sorted by highest edge (max of call_wr or put_wr)
    seq_stats.sort(key=lambda x: max(x[3], x[4]), reverse=True)
    for seq, trades, freq, call_wr, put_wr in seq_stats:
        # Highlight if win rate is above 56%
        call_str = f"*{call_wr*100:.2f}%*" if call_wr > 0.56 else f"{call_wr*100:.2f}%"
        put_str = f"*{put_wr*100:.2f}%*" if put_wr > 0.56 else f"{put_wr*100:.2f}%"
        print(f"{seq:<12} | {trades:<8} | {freq:<10.2f} | {call_str:<10} | {put_str:<10}")

    # Now let's try combining Direction Sequence + Volume Sequence + Range Sequence
    # For example: direction sequence + last bar volume
    df_clean['combo_str'] = df_clean['seq_str'] + "_" + df_clean['vol_high'].astype(str) + "_" + df_clean['large_range'].astype(str)
    
    print(f"\n--- Direction + Vol + Range Combos (Top 10 Profitable) ---")
    print(f"{'Combo':<15} | {'Trades':<8} | {'Daily Freq':<10} | {'Call Win%':<10} | {'Put Win%':<10}")
    print("-" * 65)
    
    combo_stats = []
    for combo, grp in df_clean.groupby('combo_str'):
        trades = len(grp)
        freq = trades / 90.0
        call_wr = grp['target_call'].mean()
        put_wr = grp['target_put'].mean()
        if trades >= 100:  # Avoid small sample size noise
            combo_stats.append((combo, trades, freq, call_wr, put_wr))
            
    combo_stats.sort(key=lambda x: max(x[3], x[4]), reverse=True)
    for combo, trades, freq, call_wr, put_wr in combo_stats[:15]:
        call_str = f"*{call_wr*100:.2f}%*" if call_wr > 0.56 else f"{call_wr*100:.2f}%"
        put_str = f"*{put_wr*100:.2f}%*" if put_wr > 0.56 else f"{put_wr*100:.2f}%"
        print(f"{combo:<15} | {trades:<8} | {freq:<10.2f} | {call_str:<10} | {put_str:<10}")

# Run for 2m, 3m, 4m with sequence length 3 and 4
for tf in [2, 3, 4]:
    resample_and_analyze_sequences(tf, 4)
