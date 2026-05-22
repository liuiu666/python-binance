import pandas as pd
import numpy as np

# Load 1m K-lines
df_1m = pd.read_feather("user_data/data/binance/futures/BTC_USDT_USDT-1m-futures.feather")
df_1m.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
df_1m['date'] = pd.to_datetime(df_1m['date'])
for col in ['open', 'high', 'low', 'close', 'volume']:
    df_1m[col] = pd.to_numeric(df_1m[col], errors='coerce').astype(float)

print(f"Loaded 1m data: {len(df_1m)} rows. Date range: {df_1m['date'].min()} to {df_1m['date'].max()}")

def resample_data(df, timeframe_minutes):
    """
    Resamples 1m data to custom timeframe.
    """
    df = df.set_index('date')
    resampled = df.resample(f'{timeframe_minutes}T').agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna().reset_index()
    return resampled

# We want 10-minute forward return for the targets.
# Since we enter at the end of bar t (start of bar t+1) and exit after 10 minutes,
# we can map the exit price by looking at the close price exactly 10 minutes later.
# For resampled data:
# - For 2m data: 10 minutes = 5 bars later. Target is (close.shift(-5) - close) / close.
# - For 3m data: 10 minutes is not an exact multiple. But we can align it with the exact timestamp!
# To do this accurately, we will merge the resampled bar's close price with the close price from the 1m data exactly 10 minutes later.
def compute_features_and_targets(df_tf, timeframe_minutes):
    df_tf = df_tf.copy()
    
    # ── 1. Target Construction: Exact 10-minute forward return ──
    # We find the close price 10 minutes later by matching timestamps.
    df_1m_lookup = df_1m[['date', 'close']].rename(columns={'close': 'close_10m_later'})
    df_1m_lookup['date'] = df_1m_lookup['date'] - pd.Timedelta(minutes=10) # shift timestamp back by 10m
    
    df_tf = pd.merge(df_tf, df_1m_lookup, on='date', how='left')
    df_tf['forward_return_10m'] = (df_tf['close_10m_later'] - df_tf['close']) / df_tf['close']
    df_tf['target_call'] = (df_tf['forward_return_10m'] > 0).astype(int)
    df_tf['target_put'] = (df_tf['forward_return_10m'] < 0).astype(int)
    df_tf = df_tf.dropna(subset=['forward_return_10m']).reset_index(drop=True)
    
    # ── 2. Innovative Raw Price & Volume Features (No Standard Indicators) ──
    
    # Feature A: Body-to-Range Ratio (实体占比)
    # Measures if the candle is a solid trend candle or a ranging candle
    body = (df_tf['close'] - df_tf['open']).abs()
    full_range = df_tf['high'] - df_tf['low']
    df_tf['body_ratio'] = body / full_range.replace(0, np.nan)
    df_tf['body_ratio'] = df_tf['body_ratio'].fillna(0)
    
    # Feature B: Shadow Rejection Ratio (下影线占比 for Call, 上影线占比 for Put)
    # High lower shadow means strong buyback (rejection of down movement)
    df_tf['lower_shadow_ratio'] = (df_tf[['open', 'close']].min(axis=1) - df_tf['low']) / full_range.replace(0, np.nan)
    df_tf['upper_shadow_ratio'] = (df_tf['high'] - df_tf[['open', 'close']].max(axis=1)) / full_range.replace(0, np.nan)
    df_tf['lower_shadow_ratio'] = df_tf['lower_shadow_ratio'].fillna(0)
    df_tf['upper_shadow_ratio'] = df_tf['upper_shadow_ratio'].fillna(0)
    
    # Feature C: Cumulative Return of Last N Bars (累积收益率 - 衡量超买超卖)
    # We test N=3 and N=5
    df_tf['ret_last_3'] = (df_tf['close'] - df_tf['close'].shift(3)) / df_tf['close'].shift(3)
    df_tf['ret_last_5'] = (df_tf['close'] - df_tf['close'].shift(5)) / df_tf['close'].shift(5)
    
    # Feature D: Return Acceleration (动量加速度)
    # Acceleration = current return minus previous return
    df_tf['ret_1'] = (df_tf['close'] - df_tf['close'].shift(1)) / df_tf['close'].shift(1)
    df_tf['ret_1_prev'] = df_tf['ret_1'].shift(1)
    df_tf['acceleration'] = df_tf['ret_1'] - df_tf['ret_1_prev']
    
    # Feature E: Volume Surge (成交量异动)
    # Ratio of current volume to median of last 10 bars
    vol_median = df_tf['volume'].rolling(window=10).median()
    df_tf['vol_ratio'] = df_tf['volume'] / vol_median.replace(0, np.nan)
    df_tf['vol_ratio'] = df_tf['vol_ratio'].fillna(1.0)
    
    # Feature F: Consecutive Down/Up candles (连续阴/阳线数量)
    df_tf['is_down'] = (df_tf['close'] < df_tf['open']).astype(int)
    df_tf['is_up'] = (df_tf['close'] > df_tf['open']).astype(int)
    df_tf['consec_down'] = df_tf['is_down'].groupby((df_tf['is_down'] != df_tf['is_down'].shift()).cumsum()).cumsum()
    df_tf['consec_up'] = df_tf['is_up'].groupby((df_tf['is_up'] != df_tf['is_up'].shift()).cumsum()).cumsum()
    
    # Set NaNs to 0
    df_tf = df_tf.fillna(0)
    return df_tf

def simulate_cooldown(df_tf, tf_minutes, call_cond, put_cond):
    """
    Simulates trading with a 10-minute cooldown (at most 1 trade per 10 minutes).
    In tf_minutes bars, the cooldown in bars is: ceil(10 / tf_minutes).
    """
    cooldown_bars = int(np.ceil(10.0 / tf_minutes))
    
    raw_call = call_cond.astype(int).values
    raw_put = put_cond.astype(int).values
    target_call = df_tf['target_call'].values
    target_put = df_tf['target_put'].values
    
    wins = 0
    total = 0
    cooldown = 0
    
    for i in range(len(df_tf)):
        if cooldown > 0:
            cooldown -= 1
            continue
            
        c_sig = raw_call[i]
        p_sig = raw_put[i]
        
        if c_sig == 1 and p_sig == 1:
            continue
        elif c_sig == 1:
            total += 1
            wins += target_call[i]
            cooldown = cooldown_bars
        elif p_sig == 1:
            total += 1
            wins += target_put[i]
            cooldown = cooldown_bars
            
    wr = wins / total if total > 0 else 0
    trades_per_day = total / 90.0
    expectancy = wr * 0.80 - (1 - wr) * 1.0 if total > 0 else 0
    return total, trades_per_day, wr, expectancy

# Evaluate for 2m, 3m, 4m
for tf in [2, 3, 4]:
    print(f"\n=================== TIMEFRAME: {tf}m ===================")
    df_tf = resample_data(df_1m, tf)
    df_tf = compute_features_and_targets(df_tf, tf)
    
    # Let's search for raw price-action patterns!
    # Pattern Space:
    # 1. Momentum Exhaustion (Reversion):
    #    - Call: ret_last_3 < -threshold_ret, and lower_shadow_ratio > shadow_threshold, volume surging
    #    - Put: ret_last_3 > threshold_ret, and upper_shadow_ratio > shadow_threshold, volume surging
    # 2. Acceleration Reversal (V-shape):
    #    - Call: ret_1_prev < -0.002, acceleration > 0.001 (quick bounce)
    #    - Put: ret_1_prev > 0.002, acceleration < -0.001
    
    # Let's run a search sweep over threshold parameters
    best_results = []
    
    ret_thresholds = [0.0015, 0.002, 0.003, 0.004]
    shadow_thresholds = [0.2, 0.3, 0.4]
    vol_thresholds = [1.0, 1.2, 1.5]
    
    for r_t in ret_thresholds:
        for s_t in shadow_thresholds:
            for v_t in vol_thresholds:
                # Rule definitions
                call_cond = (df_tf['ret_last_3'] < -r_t) & (df_tf['lower_shadow_ratio'] >= s_t) & (df_tf['vol_ratio'] >= v_t)
                put_cond = (df_tf['ret_last_3'] > r_t) & (df_tf['upper_shadow_ratio'] >= s_t) & (df_tf['vol_ratio'] >= v_t)
                
                tot, t_day, wr, exp = simulate_cooldown(df_tf, tf, call_cond, put_cond)
                if t_day >= 3.0: # We want at least 3 trades per day for the combo
                    best_results.append((r_t, s_t, v_t, tot, t_day, wr, exp, "Momentum Exhaustion"))
                    
    # Let's also test Acceleration Reversal Scenario
    acc_thresholds = [0.001, 0.0015, 0.002]
    prev_ret_thresholds = [0.0015, 0.002, 0.003]
    for p_r in prev_ret_thresholds:
        for acc in acc_thresholds:
            call_cond = (df_tf['ret_1_prev'] < -p_r) & (df_tf['acceleration'] > acc)
            put_cond = (df_tf['ret_1_prev'] > p_r) & (df_tf['acceleration'] < -acc)
            
            tot, t_day, wr, exp = simulate_cooldown(df_tf, tf, call_cond, put_cond)
            if t_day >= 3.0:
                best_results.append((p_r, acc, 0, tot, t_day, wr, exp, "Acceleration Reversal"))

    # Print the top 10 rules for this timeframe based on expectancy
    best_results.sort(key=lambda x: x[5], reverse=True) # Sort by win rate
    print(f"{'Rule Style':<23} | {'Param A':<8} | {'Param B':<8} | {'Param C':<8} | {'Trades':<6} | {'Daily Freq':<10} | {'Win Rate':<10} | {'Expectancy':<10}")
    print("-" * 100)
    for r in best_results[:12]:
        print(f"{r[7]:<23} | {r[0]:<8.4f} | {r[1]:<8.4f} | {r[2]:<8.4f} | {r[3]:<6} | {r[4]:<10.2f} | {r[5]*100:<9.2f}% | {r[6]:<.4f} R")
