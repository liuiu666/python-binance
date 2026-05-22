import pandas as pd
import numpy as np

# Load 1m K-lines
df_1m = pd.read_feather("user_data/data/binance/futures/BTC_USDT_USDT-1m-futures.feather")
df_1m.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
df_1m['date'] = pd.to_datetime(df_1m['date'])
for col in ['open', 'high', 'low', 'close', 'volume']:
    df_1m[col] = pd.to_numeric(df_1m[col], errors='coerce').astype(float)

# Target lookup (10 minutes later)
df_lookup = df_1m[['date', 'close']].rename(columns={'close': 'close_10m_later'})
df_lookup['date'] = df_lookup['date'] - pd.Timedelta(minutes=10)

def search_winrate_limit(tf, seq_len, vol_mult, range_mult, min_indiv_wr):
    # 1. Resample to target timeframe
    df = df_1m.set_index('date').resample(f'{tf}min').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
    }).dropna().reset_index()
    
    # End timestamp shift
    df['date'] = df['date'] + pd.Timedelta(minutes=tf)
    
    # Merge targets
    df = pd.merge(df, df_lookup, on='date', how='left').dropna(subset=['close_10m_later']).reset_index(drop=True)
    df['forward_return_10m'] = (df['close_10m_later'] - df['close']) / df['close']
    df['target_call'] = (df['forward_return_10m'] > 0).astype(int)
    df['target_put'] = (df['forward_return_10m'] < 0).astype(int)
    
    # Compute features with multipliers
    df['dir'] = (df['close'] > df['open']).astype(int)
    
    vol_median = df['volume'].rolling(window=20).median()
    # High volume is defined using a multiplier
    df['vol_high'] = (df['volume'] > (vol_median * vol_mult)).astype(int)
    
    full_range = df['high'] - df['low']
    range_mean = full_range.rolling(window=20).mean()
    # Large range is defined using a multiplier
    df['large_range'] = (full_range > (range_mean * range_mult)).astype(int)
    
    # Sequence construction
    df['seq_str'] = ""
    for shift in range(seq_len):
        df['seq_str'] = df['seq_str'] + df['dir'].shift(seq_len - 1 - shift).fillna(0).astype(int).astype(str)
        
    df['combo_str'] = df['seq_str'] + "_" + df['vol_high'].astype(str) + "_" + df['large_range'].astype(str)
    df = df.iloc[seq_len:].copy()
    
    # Find premium combos
    call_combos = []
    put_combos = []
    
    for combo, grp in df.groupby('combo_str'):
        trades = len(grp)
        # Ensure we have at least 60 trades to be statistically valid
        if trades >= 60:
            c_wr = grp['target_call'].mean()
            p_wr = grp['target_put'].mean()
            if c_wr >= min_indiv_wr:
                call_combos.append(combo)
            if p_wr >= min_indiv_wr:
                put_combos.append(combo)
                
    if not call_combos and not put_combos:
        return None
        
    # Mark raw signals
    df['raw_call'] = df['combo_str'].isin(call_combos).astype(int)
    df['raw_put'] = df['combo_str'].isin(put_combos).astype(int)
    
    # Align to 1m timeline for 10-minute cooldown simulation
    df_timeline = df_1m[['date', 'close']].copy()
    df_tf_sig = df[['date', 'raw_call', 'raw_put']]
    df_timeline = pd.merge(df_timeline, df_tf_sig, on='date', how='left').fillna(0)
    
    # Target
    df_timeline['forward_return_10m'] = (df_timeline['close'].shift(-10) - df_timeline['close']) / df_timeline['close']
    df_timeline['true_target_call'] = (df_timeline['forward_return_10m'] > 0).astype(int)
    df_timeline['true_target_put'] = (df_timeline['forward_return_10m'] < 0).astype(int)
    df_timeline = df_timeline.dropna(subset=['forward_return_10m']).reset_index(drop=True)
    
    # Cooldown simulation
    cooldown, wins, total = 0, 0, 0
    executed_wins = []
    c_arr, p_arr = df_timeline['raw_call'].values, df_timeline['raw_put'].values
    t_call, t_put = df_timeline['true_target_call'].values, df_timeline['true_target_put'].values
    
    for i in range(len(df_timeline)):
        if cooldown > 0:
            cooldown -= 1
            continue
        c, p = c_arr[i], p_arr[i]
        if c == 1 and p == 1:
            continue
        elif c == 1:
            total += 1
            wins += t_call[i]
            cooldown = 10
            executed_wins.append(t_call[i])
        elif p == 1:
            total += 1
            wins += t_put[i]
            cooldown = 10
            executed_wins.append(t_put[i])
            
    if total < 400:  # Require at least 400 trades over 90 days to avoid extreme low-frequency overfitting
        return None
        
    wr = wins / total
    
    # Drawdown
    starting_capital = 10000
    trade_size = 100
    payout = 0.80
    pnls = [trade_size * payout if x == 1 else -trade_size for x in executed_wins]
    equity = starting_capital + np.cumsum(pnls)
    max_dd = np.max((np.maximum.accumulate(equity) - equity) / np.maximum.accumulate(equity))
    profit = equity[-1] - starting_capital
    
    return {
        'tf': tf, 'seq': seq_len, 'vol_m': vol_mult, 'range_m': range_mult,
        'indiv_wr': min_indiv_wr, 'trades': total, 'freq': total/90,
        'win_rate': wr, 'profit': profit, 'max_dd': max_dd
    }

results = []
# We focus on the best timeframes: 2m and 3m
for tf in [2, 3]:
    for seq in [5, 6]:
        for vol_m in [1.0, 1.2, 1.4]:
            for range_m in [1.0, 1.2, 1.4]:
                for indiv_wr in [0.570, 0.575, 0.580, 0.585]:
                    res = search_winrate_limit(tf, seq, vol_m, range_m, indiv_wr)
                    if res:
                        results.append(res)

results.sort(key=lambda x: x['win_rate'], reverse=True)

print("\n" + "="*110)
print(f"{'TF':<5} | {'Seq':<4} | {'Vol_M':<6} | {'Rng_M':<6} | {'Indiv_WR':<8} | {'Trades':<6} | {'Daily Freq':<10} | {'Win Rate':<10} | {'Profit':<10} | {'Max DD %':<10}")
print("="*110)
for r in results[:15]:
    print(f"{r['tf']:<3}m  | {r['seq']:<4} | {r['vol_m']:<6.1f} | {r['range_m']:<6.1f} | {r['indiv_wr']:<8.3f} | {r['trades']:<6} | {r['freq']:<10.2f} | {r['win_rate']*100:<9.2f}% | ${r['profit']:<8.2f} | {r['max_dd']*100:<9.2f}%")
print("="*110)
