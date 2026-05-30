"""Optimization analysis: identify bottlenecks for WR and trade count."""
import warnings, sys, os
warnings.filterwarnings('ignore')
sys.path.insert(0, os.getcwd())
import math, numpy as np, pandas as pd

FEATHER = "user_data/data/binance/futures/BTC_USDT_USDT-1m-futures.feather"
EMA_SPANS = [30, 60, 120, 240]
ATR_WIN = 120; VOL_Z_WIN = 40; RV_WIN = 60
RV_BASELINE_WIN = 60 * 24; EMA_DEV_QUANTILE_WIN = 60 * 24 * 14
HQ_CALL_QLO = 0.05; HQ_CALL_K = 4; HQ_PUT_QHI = 0.95; HQ_PUT_K = 2
NORM_CALL_QLO = 0.10; NORM_CALL_K = 4; NORM_PUT_QHI = 0.90; NORM_PUT_K = 3
VOL_Z_THRESHOLD = 1.0; RV_Z_BAND = 1.0
EXPIRY_BARS = 10; PAYOUT_WIN = 4.0; PAYOUT_LOSS = -5.0

print("Loading data ...")
df = pd.read_feather(FEATHER)
df.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
df['date'] = pd.to_datetime(df['date'])
for c in ['open', 'high', 'low', 'close', 'volume']:
    df[c] = pd.to_numeric(df[c], errors='coerce').astype(float)

print("Computing indicators ...")
prev_close = df['close'].shift(1)
tr1 = df['high'] - df['low']
tr2 = (df['high'] - prev_close).abs()
tr3 = (df['low'] - prev_close).abs()
true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
atr120 = true_range.ewm(alpha=1.0 / ATR_WIN, adjust=False).mean()
df['atr'] = atr120

min_q = 2 * 60 * 24
for s in EMA_SPANS:
    ema_s = df['close'].ewm(span=s, adjust=False).mean()
    dev = (df['close'] - ema_s) / atr120.replace(0, np.nan)
    df[f'd{s}'] = dev
    df[f'q{s}'] = dev.rolling(EMA_DEV_QUANTILE_WIN, min_periods=min_q).rank(pct=True)

vmed40 = df['volume'].rolling(VOL_Z_WIN).median()
vstd40 = df['volume'].rolling(VOL_Z_WIN).std()
df['vol_z40'] = (df['volume'] - vmed40) / vstd40.replace(0, np.nan)

logret1 = np.log(df['close']).diff()
rv60 = logret1.rolling(RV_WIN).std()
rv_mean = rv60.rolling(RV_BASELINE_WIN, min_periods=RV_BASELINE_WIN).mean()
rv_std  = rv60.rolling(RV_BASELINE_WIN, min_periods=RV_BASELINE_WIN).std()
df['rv_z'] = ((rv60 - rv_mean) / rv_std.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)

base = ((df['vol_z40'] > VOL_Z_THRESHOLD) & (df['rv_z'] > -RV_Z_BAND)
        & (df['rv_z'] < RV_Z_BAND) & df['q30'].notna())

hq_lo   = sum((df[f'q{s}'] <= HQ_CALL_QLO).astype('Int64').fillna(0) for s in EMA_SPANS)
norm_lo = sum((df[f'q{s}'] <= NORM_CALL_QLO).astype('Int64').fillna(0) for s in EMA_SPANS)
hq_hi   = sum((df[f'q{s}'] >= HQ_PUT_QHI).astype('Int64').fillna(0) for s in EMA_SPANS)
norm_hi = sum((df[f'q{s}'] >= NORM_PUT_QHI).astype('Int64').fillna(0) for s in EMA_SPANS)

mask_hq_call   = base & (hq_lo   >= HQ_CALL_K)
mask_hq_put    = base & (hq_hi   >= HQ_PUT_K)
mask_norm_call = base & (norm_lo >= NORM_CALL_K) & ~mask_hq_call
mask_norm_put  = base & (norm_hi >= NORM_PUT_K)  & ~mask_hq_put

df['enter_long']  = 0; df['enter_short'] = 0; df['tier'] = ''
df.loc[mask_hq_call,   'enter_long']  = 1
df.loc[mask_hq_put,    'enter_short'] = 1
df.loc[mask_norm_call, 'enter_long']  = 1
df.loc[mask_norm_put,  'enter_short'] = 1
df.loc[mask_hq_call,   'tier'] = 'HQ'
df.loc[mask_hq_put,    'tier'] = 'HQ'
df.loc[mask_norm_call, 'tier'] = 'NORM'
df.loc[mask_norm_put,  'tier'] = 'NORM'

# Simulate with 10-bar lockout
def simulate(df, expiry=EXPIRY_BARS):
    el = df['enter_long'].fillna(0).to_numpy().astype(np.int8)
    es = df['enter_short'].fillna(0).to_numpy().astype(np.int8)
    cl = df['close'].to_numpy(); n = len(df); cd = 0; rows = []
    for i in range(n - expiry):
        if cd > 0: cd -= 1; continue
        if el[i]==1 and es[i]==0:
            entry=cl[i]; exit_p=cl[i+expiry]; win=int(exit_p>entry)
            rows.append((i, df['date'].iloc[i], 1, df['tier'].iloc[i], entry, exit_p, win)); cd=expiry
        elif es[i]==1 and el[i]==0:
            entry=cl[i]; exit_p=cl[i+expiry]; win=int(exit_p<entry)
            rows.append((i, df['date'].iloc[i], -1, df['tier'].iloc[i], entry, exit_p, win)); cd=expiry
    return pd.DataFrame(rows, columns=['idx','date','side','tier','entry','exit','win'])

def wilson(k, n, z=1.96):
    if n==0: return 0.0
    p=k/n; denom=1+z*z/n; centre=p+z*z/(2*n); margin=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))
    return (centre-margin)/denom

def stats(td, label=""):
    n=len(td); w=int(td['win'].sum())
    pnl=np.where(td['win'].to_numpy()==1,PAYOUT_WIN,PAYOUT_LOSS)
    eq=np.cumsum(pnl); peak=np.maximum.accumulate(eq); mdd=float((peak-eq).max())
    return dict(n=n, w=w, wr=w/n, wlb=wilson(w,n), pnl=float(eq[-1]), mdd=mdd, label=label)

trades = simulate(df)
print(f"\nBaseline A4: n={len(trades)}  WR={stats(trades)['wr']*100:.2f}%  WLB={stats(trades)['wlb']*100:.2f}%  PnL={stats(trades)['pnl']:+.0f}U")

# =========================================================================
# 1. THRESHOLD SENSITIVITY ANALYSIS
# =========================================================================
print("\n" + "="*60)
print("1. QUANTILE THRESHOLD SENSITIVITY (HQ CALL only)")
print("="*60)
print(f"{'QLO':>6} {'K':>4} {'n':>5} {'WR%':>7} {'WLB%':>7} {'PnL':>8} {'MDD':>7}")
print("-"*50)
for q_lo in [0.03, 0.04, 0.05, 0.06, 0.08, 0.10]:
    for k in [3, 4]:
        hq_test  = base & (hq_lo   >= k)
        norm_test = base & (norm_lo >= k) & ~hq_test
        test_long = hq_test | norm_test
        df_t = df.copy()
        df_t['enter_long'] = 0; df_t['enter_short'] = 0
        df_t.loc[test_long, 'enter_long'] = 1
        # simulate only longs
        el_t = df_t['enter_long'].fillna(0).to_numpy().astype(np.int8)
        cl_t = df_t['close'].to_numpy(); n=len(df_t); cd=0; rows=[]
        for i in range(n-EXPIRY_BARS):
            if cd>0: cd-=1; continue
            if el_t[i]==1:
                entry=cl_t[i]; exit_p=cl_t[i+EXPIRY_BARS]; win=int(exit_p>entry)
                rows.append((df_t['date'].iloc[i], entry, exit_p, win)); cd=EXPIRY_BARS
        t_sub = pd.DataFrame(rows, columns=['date','entry','exit','win'])
        if len(t_sub) < 30: continue
        s = stats(t_sub)
        ok = "OK" if s['wlb']>0.5556 else "---"
        print(f"  {q_lo:.2f}  {k}   {s['n']:>5} {s['wr']*100:>6.2f}% {s['wlb']*100:>6.2f}% {s['pnl']:>+7.0f}U {ok}")

# =========================================================================
# 2. VOLUME FILTER IMPACT
# =========================================================================
print("\n" + "="*60)
print("2. VOL_Z THRESHOLD SWEEP")
print("="*60)
print(f"{'VOL_Z':>7} {'n':>5} {'WR%':>7} {'WLB%':>7} {'PnL':>8} {'结论':>6}")
print("-"*50)
for vz_th in [0.5, 0.8, 1.0, 1.2, 1.5, 2.0]:
    base_v = ((df['vol_z40'] > vz_th) & (df['rv_z'] > -RV_Z_BAND)
              & (df['rv_z'] < RV_Z_BAND) & df['q30'].notna())
    sig_v = base_v & (hq_lo >= HQ_CALL_K)
    el_v = sig_v.fillna(0).to_numpy().astype(np.int8)
    cl_v = df['close'].to_numpy(); n=len(df); cd=0; rows=[]
    for i in range(n-EXPIRY_BARS):
        if cd>0: cd-=1; continue
        if el_v[i]==1:
            entry=cl_v[i]; exit_p=cl_v[i+EXPIRY_BARS]; win=int(exit_p>entry)
            rows.append((df['date'].iloc[i], entry, exit_p, win)); cd=EXPIRY_BARS
    t_v = pd.DataFrame(rows, columns=['date','entry','exit','win'])
    if len(t_v)<20: continue
    s = stats(t_v)
    ok = "OK" if s['wlb']>0.5556 else "FAIL"
    print(f"  {vz_th:.1f}     {s['n']:>5} {s['wr']*100:>6.2f}% {s['wlb']*100:>6.2f}% {s['pnl']:>+7.0f}U  {ok}")

# =========================================================================
# 3. RV_Z BAND WIDTH
# =========================================================================
print("\n" + "="*60)
print("3. RV_Z BAND SWEEP (call signals only)")
print("="*60)
print(f"{'RV_Z':>6} {'n':>5} {'WR%':>7} {'WLB%':>7} {'PnL':>8}")
for band in [0.5, 0.75, 1.0, 1.25, 1.5, 2.0]:
    base_r = ((df['vol_z40'] > VOL_Z_THRESHOLD)
              & (df['rv_z'] > -band) & (df['rv_z'] < band) & df['q30'].notna())
    sig_r = base_r & (hq_lo >= HQ_CALL_K)
    el_r = sig_r.fillna(0).to_numpy().astype(np.int8)
    cl_r = df['close'].to_numpy(); n=len(df); cd=0; rows=[]
    for i in range(n-EXPIRY_BARS):
        if cd>0: cd-=1; continue
        if el_r[i]==1:
            entry=cl_r[i]; exit_p=cl_r[i+EXPIRY_BARS]; win=int(exit_p>entry)
            rows.append((df['date'].iloc[i], entry, exit_p, win)); cd=EXPIRY_BARS
    t_r = pd.DataFrame(rows, columns=['date','entry','exit','win'])
    if len(t_r)<20: continue
    s = stats(t_r)
    print(f"  {band:.2f}  {s['n']:>5} {s['wr']*100:>6.2f}% {s['wlb']*100:>6.2f}% {s['pnl']:>+7.0f}U")

# =========================================================================
# 4. EXPIRY SWEEP (how 5min vs 10min vs 15min affects WR)
# =========================================================================
print("\n" + "="*60)
print("4. EXPIRY LENGTH SWEEP (CALL signals only)")
print("="*60)
print(f"{'Expiry':>7} {'n':>5} {'WR%':>7} {'WLB%':>7} {'PnL':>8}")
for expiry in [5, 8, 10, 12, 15, 20]:
    el_e = mask_hq_call.fillna(0).to_numpy().astype(np.int8)
    cl_e = df['close'].to_numpy(); n=len(df); cd=0; rows=[]
    for i in range(n-expiry):
        if cd>0: cd-=1; continue
        if el_e[i]==1:
            entry=cl_e[i]; exit_p=cl_e[i+expiry]; win=int(exit_p>entry)
            rows.append((df['date'].iloc[i], entry, exit_p, win)); cd=expiry
    t_e = pd.DataFrame(rows, columns=['date','entry','exit','win'])
    if len(t_e)<20: continue
    s = stats(t_e)
    print(f"  {expiry:>4}min  {s['n']:>5} {s['wr']*100:>6.2f}% {s['wlb']*100:>6.2f}% {s['pnl']:>+7.0f}U")

# =========================================================================
# 5. MARKET REGIME: what happens in high vs low RV regimes?
# =========================================================================
print("\n" + "="*60)
print("5. MARKET REGIME: RV_Z quintile analysis (CALL signals)")
print("="*60)
df_rv = df.copy()
df_rv['rv_bucket'] = pd.qcut(df_rv['rv_z'].clip(-10,10), 5, labels=['very_low','low','mid','high','very_high'], duplicates='drop')
for bucket in ['very_low','low','mid','high','very_high']:
    sub = df_rv[df_rv['rv_bucket']==bucket]
    sig = base & (hq_lo >= HQ_CALL_K) & (df_rv['rv_bucket']==bucket)
    el_b = sig.fillna(0).to_numpy().astype(np.int8)
    cl_b = df['close'].to_numpy(); n=len(df); cd=0; rows=[]
    for i in range(n-EXPIRY_BARS):
        if cd>0: cd-=1; continue
        if el_b[i]==1:
            entry=cl_b[i]; exit_p=cl_b[i+EXPIRY_BARS]; win=int(exit_p>entry)
            rows.append((df['date'].iloc[i], entry, exit_p, win)); cd=EXPIRY_BARS
    t_b = pd.DataFrame(rows, columns=['date','entry','exit','win'])
    if len(t_b)<10: continue
    s = stats(t_b)
    ok = "BEST" if s['wr']==max(s['wr'],0.001) else ""
    print(f"  rv_z={bucket:<10} n={len(t_b):>5}  WR={s['wr']*100:.2f}%  WLB={s['wlb']*100:.2f}%  PnL={s['pnl']:+.0f}U  {ok}")

# =========================================================================
# 6. SIDE ASYMMETRY: long vs short performance
# =========================================================================
print("\n" + "="*60)
print("6. LONG vs SHORT asymmetry (HQ signals)")
print("="*60)
for side, mask_side in [("LONG", mask_hq_call), ("SHORT", mask_hq_put)]:
    el_s = mask_side.fillna(0).to_numpy().astype(np.int8)
    cl_s = df['close'].to_numpy(); n=len(df); cd=0; rows=[]
    for i in range(n-EXPIRY_BARS):
        if cd>0: cd-=1; continue
        if el_s[i]==1:
            entry=cl_s[i]; exit_p=cl_s[i+EXPIRY_BARS]
            if side=="LONG": win=int(exit_p>entry)
            else: win=int(exit_p<entry)
            rows.append((df['date'].iloc[i], entry, exit_p, win)); cd=EXPIRY_BARS
    t_s = pd.DataFrame(rows, columns=['date','entry','exit','win'])
    if len(t_s)<20: continue
    s = stats(t_s)
    print(f"  {side}: n={s['n']:>5}  WR={s['wr']*100:.2f}%  WLB={s['wlb']*100:.2f}%  PnL={s['pnl']:+.0f}U  MDD={s['mdd']:.0f}U")

# =========================================================================
# 7. HOW MANY BARS BETWEEN SIGNALS? (average gap)
# =========================================================================
signal_idx = trades['idx'].to_numpy()
gaps = np.diff(signal_idx)
print("\n" + "="*60)
print("7. SIGNAL FREQUENCY ANALYSIS")
print("="*60)
print(f"  Mean bars between signals: {gaps.mean():.1f}")
print(f"  Median bars between signals: {np.median(gaps):.1f}")
print(f"  Signals/day (avg): {1440 / gaps.mean():.2f}")
print(f"  Min gap: {gaps.min()} bars  Max gap: {gaps.max()} bars")
# How many bars in the 367-day window have vol_z > threshold?
vol_ok_pct = (df['vol_z40'] > VOL_Z_THRESHOLD).mean() * 100
rv_ok_pct  = ((df['rv_z'] > -RV_Z_BAND) & (df['rv_z'] < RV_Z_BAND)).mean() * 100
q_ok_pct   = (df['q30'] <= NORM_CALL_QLO).mean() * 100
print(f"  Bars passing vol_z filter: {vol_ok_pct:.1f}%")
print(f"  Bars passing rv_z filter:  {rv_ok_pct:.1f}%")
print(f"  Bars passing q_lo<=0.10 filter: {q_ok_pct:.1f}%")
print(f"  All 3 pass (signal possible): {(vol_ok_pct*rv_ok_pct*q_ok_pct/10000):.1f}%")

print("\n[DONE]")
