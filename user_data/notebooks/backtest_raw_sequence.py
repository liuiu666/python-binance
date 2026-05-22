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

def prepare_1m_data():
    df = df_1m.copy()
    df = pd.merge(df, df_1m_lookup, on='date', how='left')
    df = df.dropna(subset=['close_10m_later']).reset_index(drop=True)
    df['forward_return_10m'] = (df['close_10m_later'] - df['close']) / df['close']
    df['target_call'] = (df['forward_return_10m'] > 0).astype(int)
    df['target_put'] = (df['forward_return_10m'] < 0).astype(int)
    
    df['dir'] = (df['close'] > df['open']).astype(int)
    df['vol_high'] = (df['volume'] > df['volume'].rolling(30).median()).astype(int)
    df['large_range'] = ((df['high'] - df['low']) > (df['high'] - df['low']).rolling(30).mean()).astype(int)
    
    seq_len = 6
    df['seq_str'] = ""
    for shift in reversed(range(seq_len)):
        df['seq_str'] = df['seq_str'] + df['dir'].shift(shift).fillna(0).astype(int).astype(str)
        
    df['combo_str'] = df['seq_str'] + "_" + df['vol_high'].astype(str) + "_" + df['large_range'].astype(str)
    df = df.iloc[seq_len:].copy()
    
    call_combos = ['001000_1_1', '010011_0_1', '000000_1_1', '001111_0_1']
    put_combos = ['000010_0_1', '011110_0_1', '011101_0_1', '001111_1_0']
    
    df['raw_call'] = df['combo_str'].isin(call_combos).astype(int)
    df['raw_put'] = df['combo_str'].isin(put_combos).astype(int)
    return df[['date', 'raw_call', 'raw_put']]

def prepare_2m_data():
    df = df_1m.set_index('date').resample('2min').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna().reset_index()
    
    df['date'] = df['date'] + pd.Timedelta(minutes=2)
    
    df = pd.merge(df, df_1m_lookup, on='date', how='left')
    df = df.dropna(subset=['close_10m_later']).reset_index(drop=True)
    df['forward_return_10m'] = (df['close_10m_later'] - df['close']) / df['close']
    df['target_call'] = (df['forward_return_10m'] > 0).astype(int)
    df['target_put'] = (df['forward_return_10m'] < 0).astype(int)
    
    df['dir'] = (df['close'] > df['open']).astype(int)
    df['vol_high'] = (df['volume'] > df['volume'].rolling(15).median()).astype(int)
    df['large_range'] = ((df['high'] - df['low']) > (df['high'] - df['low']).rolling(15).mean()).astype(int)
    
    seq_len = 5
    df['seq_str'] = ""
    for shift in reversed(range(seq_len)):
        df['seq_str'] = df['seq_str'] + df['dir'].shift(shift).fillna(0).astype(int).astype(str)
        
    df['combo_str'] = df['seq_str'] + "_" + df['vol_high'].astype(str) + "_" + df['large_range'].astype(str)
    df = df.iloc[seq_len:].copy()
    
    call_combos = ['00010_0_1', '00000_1_1', '10000_1_1', '00000_1_0', '01100_1_1']
    put_combos = ['10011_1_0', '11011_1_1']
    
    df['raw_call'] = df['combo_str'].isin(call_combos).astype(int)
    df['raw_put'] = df['combo_str'].isin(put_combos).astype(int)
    return df[['date', 'raw_call', 'raw_put']]

print("Preparing datasets...")
df_1m_sig = prepare_1m_data()
df_2m_sig = prepare_2m_data()

# Merge onto 1m timeline
df_timeline = df_1m[['date', 'close']].copy()
df_1m_sig = df_1m_sig.rename(columns={'raw_call': 'call_1m', 'raw_put': 'put_1m'})
df_timeline = pd.merge(df_timeline, df_1m_sig, on='date', how='left')

df_2m_sig = df_2m_sig.rename(columns={'raw_call': 'call_2m', 'raw_put': 'put_2m'})
df_timeline = pd.merge(df_timeline, df_2m_sig, on='date', how='left')

df_timeline = df_timeline.fillna(0)

# True Target (from 1m timeline)
df_timeline['forward_return_10m'] = (df_timeline['close'].shift(-10) - df_timeline['close']) / df_timeline['close']
df_timeline['true_target_call'] = (df_timeline['forward_return_10m'] > 0).astype(int)
df_timeline['true_target_put'] = (df_timeline['forward_return_10m'] < 0).astype(int)
df_timeline = df_timeline.dropna(subset=['forward_return_10m']).reset_index(drop=True)

def run_detailed_backtest(max_concurrent, starting_capital=10000, trade_size=100, payout=0.80):
    active_trades = [] # list of expiry indices
    executed_trades = []
    
    c1 = df_timeline['call_1m'].values
    p1 = df_timeline['put_1m'].values
    c2 = df_timeline['call_2m'].values
    p2 = df_timeline['put_2m'].values
    
    t_call = df_timeline['true_target_call'].values
    t_put = df_timeline['true_target_put'].values
    date_arr = df_timeline['date'].values
    
    for i in range(len(df_timeline)):
        # Remove expired trades
        active_trades = [expiry for expiry in active_trades if expiry > i]
        
        # Combined signals
        call_sig = c1[i] or c2[i]
        put_sig = p1[i] or p2[i]
        
        if len(active_trades) < max_concurrent:
            if call_sig == 1 and put_sig == 1:
                continue
            elif call_sig == 1:
                executed_trades.append({
                    'date': date_arr[i],
                    'type': 'Call',
                    'is_win': t_call[i]
                })
                active_trades.append(i + 10)
            elif put_sig == 1:
                executed_trades.append({
                    'date': date_arr[i],
                    'type': 'Put',
                    'is_win': t_put[i]
                })
                active_trades.append(i + 10)

    trades_df = pd.DataFrame(executed_trades)
    if len(trades_df) == 0:
        return None
        
    # Calculate equity curve
    # Flat size: Win yields trade_size * payout, Loss yields -trade_size
    trades_df['pnl'] = np.where(trades_df['is_win'] == 1, trade_size * payout, -trade_size)
    trades_df['equity'] = starting_capital + trades_df['pnl'].cumsum()
    
    # Calculate drawdowns
    trades_df['peak'] = trades_df['equity'].cummax()
    trades_df['drawdown_pct'] = (trades_df['peak'] - trades_df['equity']) / trades_df['peak']
    max_dd_pct = trades_df['drawdown_pct'].max()
    max_dd_val = (trades_df['peak'] - trades_df['equity']).max()
    
    # Calculate consecutive wins/losses
    trades_df['loss'] = (trades_df['is_win'] == 0).astype(int)
    # Consecutive loss count using groupby
    trades_df['consec_loss'] = trades_df['loss'].groupby((trades_df['loss'] != trades_df['loss'].shift()).cumsum()).cumsum()
    max_consec_losses = trades_df['consec_loss'].max()
    
    total = len(trades_df)
    wins = trades_df['is_win'].sum()
    wr = wins / total
    freq = total / 90.0
    final_equity = trades_df['equity'].iloc[-1]
    net_profit = final_equity - starting_capital
    roi = (net_profit / starting_capital) * 100
    
    return {
        'total_trades': total,
        'daily_freq': freq,
        'win_rate': wr,
        'final_equity': final_equity,
        'net_profit': net_profit,
        'roi_pct': roi,
        'max_drawdown_pct': max_dd_pct,
        'max_drawdown_val': max_dd_val,
        'max_consec_losses': max_consec_losses
    }

print("\n" + "="*80)
print(f"{'Max Concurrent':<15} | {'Total Trades':<12} | {'Daily Freq':<10} | {'Win Rate':<10} | {'Net Profit':<12} | {'Max DD %':<10} | {'Max Consec Loss':<15}")
print("="*80)
for limit in [1, 2, 3, 5, 10]:
    res = run_detailed_backtest(limit)
    if res:
        print(f"{limit:<15} | {res['total_trades']:<12} | {res['daily_freq']:<10.2f} | {res['win_rate']*100:<9.2f}% | ${res['net_profit']:<11.2f} | {res['max_drawdown_pct']*100:<9.2f}% | {res['max_consec_losses']:<15}")
print("="*80)
