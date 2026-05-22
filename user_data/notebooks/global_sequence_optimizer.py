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

def evaluate_config(tf_minutes, seq_len, min_individual_wr=0.565):
    # 1. Resample
    df = df_1m.set_index('date').resample(f'{tf_minutes}min').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna().reset_index()
    
    # Shift timestamp to END of bar
    df['date'] = df['date'] + pd.Timedelta(minutes=tf_minutes)
    
    # Merge target
    df = pd.merge(df, df_1m_lookup, on='date', how='left')
    df = df.dropna(subset=['close_10m_later']).reset_index(drop=True)
    df['forward_return_10m'] = (df['close_10m_later'] - df['close']) / df['close']
    df['target_call'] = (df['forward_return_10m'] > 0).astype(int)
    df['target_put'] = (df['forward_return_10m'] < 0).astype(int)
    
    # Indicators
    df['dir'] = (df['close'] > df['open']).astype(int)
    vol_median = df['volume'].rolling(window=20).median()
    df['vol_high'] = (df['volume'] > vol_median).astype(int)
    full_range = df['high'] - df['low']
    range_mean = full_range.rolling(window=20).mean()
    df['large_range'] = (full_range > range_mean).astype(int)
    
    # Sequence construction
    df['seq_str'] = ""
    for shift in reversed(range(seq_len)):
        df['seq_str'] = df['seq_str'] + df['dir'].shift(shift).fillna(0).astype(int).astype(str)
        
    df['combo_str'] = df['seq_str'] + "_" + df['vol_high'].astype(str) + "_" + df['large_range'].astype(str)
    df = df.iloc[seq_len:].copy()
    
    # Group by combo to find those with high individual win rate
    call_combos = []
    put_combos = []
    
    for combo, grp in df.groupby('combo_str'):
        trades = len(grp)
        if trades >= 100:  # Ensure statistical significance
            c_wr = grp['target_call'].mean()
            p_wr = grp['target_put'].mean()
            if c_wr >= min_individual_wr:
                call_combos.append(combo)
            if p_wr >= min_individual_wr:
                put_combos.append(combo)
                
    if len(call_combos) == 0 and len(put_combos) == 0:
        return None
        
    # Mark raw signals
    df['raw_call'] = df['combo_str'].isin(call_combos).astype(int)
    df['raw_put'] = df['combo_str'].isin(put_combos).astype(int)
    
    # 2. Align to 1m timeline for exact 10-minute cooldown simulation
    # (Since 10m is the true lock time, we align to the 1m timeline to handle different timeframes correctly)
    df_timeline = df_1m[['date', 'close']].copy()
    
    # Merge signals
    df_tf_sig = df[['date', 'raw_call', 'raw_put']].rename(columns={'raw_call': 'call_sig', 'raw_put': 'put_sig'})
    df_timeline = pd.merge(df_timeline, df_tf_sig, on='date', how='left')
    df_timeline = df_timeline.fillna(0)
    
    # True targets
    df_timeline['forward_return_10m'] = (df_timeline['close'].shift(-10) - df_timeline['close']) / df_timeline['close']
    df_timeline['true_target_call'] = (df_timeline['forward_return_10m'] > 0).astype(int)
    df_timeline['true_target_put'] = (df_timeline['forward_return_10m'] < 0).astype(int)
    df_timeline = df_timeline.dropna(subset=['forward_return_10m']).reset_index(drop=True)
    
    # 10-minute cooldown simulation
    cooldown = 0
    wins = 0
    total = 0
    executed_trades = []
    
    c_arr = df_timeline['call_sig'].values
    p_arr = df_timeline['put_sig'].values
    t_call = df_timeline['true_target_call'].values
    t_put = df_timeline['true_target_put'].values
    
    for i in range(len(df_timeline)):
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
            executed_trades.append(t_call[i])
            cooldown = 10
        elif p == 1:
            total += 1
            wins += t_put[i]
            executed_trades.append(t_put[i])
            cooldown = 10
            
    if total < 50: # Ignore configs with too few trades
        return None
        
    wr = wins / total
    freq = total / 90.0
    
    # Calculate Drawdown
    starting_capital = 10000
    trade_size = 100
    payout = 0.80
    
    pnls = [trade_size * payout if x == 1 else -trade_size for x in executed_trades]
    equity = starting_capital + np.cumsum(pnls)
    peaks = np.maximum.accumulate(equity)
    drawdowns = (peaks - equity) / peaks
    max_dd = np.max(drawdowns)
    net_profit = equity[-1] - starting_capital
    expectancy = wr * payout - (1 - wr) * 1.0
    
    # Calculate max consecutive losses
    losses = [1 if x == 0 else 0 for x in executed_trades]
    consec_losses = []
    current_consec = 0
    for loss in losses:
        if loss == 1:
            current_consec += 1
        else:
            current_consec = 0
        consec_losses.append(current_consec)
    max_consec_losses = max(consec_losses) if len(consec_losses) > 0 else 0
    
    return {
        'tf': tf_minutes,
        'seq_len': seq_len,
        'trades': total,
        'daily_freq': freq,
        'win_rate': wr,
        'expectancy': expectancy,
        'net_profit': net_profit,
        'max_dd': max_dd,
        'max_consec_losses': max_consec_losses,
        'num_call_combos': len(call_combos),
        'num_put_combos': len(put_combos)
    }

results = []
for tf in [1, 2, 3, 5]:
    for seq in [3, 4, 5, 6]:
        for min_wr in [0.560, 0.565, 0.570, 0.575]:
            res = evaluate_config(tf, seq, min_wr)
            if res:
                res['min_wr_thresh'] = min_wr
                results.append(res)

# Print Top 15 configurations sorted by net profit
results.sort(key=lambda x: x['net_profit'], reverse=True)

print("\n" + "="*115)
print(f"{'TF':<5} | {'Seq':<4} | {'Thresh':<8} | {'Trades':<6} | {'Daily Freq':<10} | {'Win Rate':<10} | {'Net Profit':<12} | {'Max DD %':<10} | {'Max Consec':<10} | {'C_Combos':<8} | {'P_Combos':<8}")
print("="*115)
for r in results[:18]:
    print(f"{r['tf']:<3}m  | {r['seq_len']:<4} | {r['min_wr_thresh']:<8.3f} | {r['trades']:<6} | {r['daily_freq']:<10.2f} | {r['win_rate']*100:<9.2f}% | ${r['net_profit']:<11.2f} | {r['max_dd']*100:<9.2f}% | {r['max_consec_losses']:<10} | {r['num_call_combos']:<8} | {r['num_put_combos']:<8}")
print("="*115)
