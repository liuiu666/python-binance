"""Mean-reversion strategy research for 10-min binary option on BTC futures.

Three studies, all on 1-year fapi 1m -> 2m bars:
  A. Single-signal: ema60_dev extreme deciles, expanding walk-forward (14-day
     refit cadence, 30-day warmup). Thresholds re-learned each fold.
  B. Two-feature combos: ema60_dev extreme AND a second filter
     (vol_z20, pos_in_range60, vwap1h_dev, funding_z, hour bucket).
  C. Logistic-regression baseline: all numeric features -> P(call) with
     symmetric thresholds, expanding walk-forward.

Run:
    .venv\\Scripts\\python -u user_data/notebooks/mean_reversion_research.py
"""
import math
import warnings
from collections import defaultdict
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

FEATHER_1M = "user_data/data/binance/futures/BTC_USDT_USDT-1m-futures.feather"
FEATHER_FUNDING = "user_data/data/binance/futures/BTC_USDT_USDT-8h-funding_rate.feather"

TF_MIN = 2
EXPIRY_BARS = 5
PAYOUT_WIN = 4.0   # 1.8x payout, +4U on win for 5U stake
PAYOUT_LOSS = -5.0
STAKE = 5.0

WARMUP_DAYS = 30
REFIT_DAYS = 14
BARS_PER_DAY = (60 // TF_MIN) * 24


def wilson_lb(k, n, z=1.96):
    if n == 0:
        return 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom


def load_2m():
    df = pd.read_feather(FEATHER_1M)
    df.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
    df['date'] = pd.to_datetime(df['date'])
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce').astype(float)
    df = df.set_index('date').resample(f'{TF_MIN}min').agg({
        'open': 'first', 'high': 'max', 'low': 'min',
        'close': 'last', 'volume': 'sum'
    }).dropna().reset_index()
    return df


def load_funding():
    try:
        f = pd.read_feather(FEATHER_FUNDING)
        f.columns = [c.lower() for c in f.columns]
        if 'date' not in f.columns:
            for cand in ['timestamp', 'time']:
                if cand in f.columns:
                    f = f.rename(columns={cand: 'date'})
                    break
        f['date'] = pd.to_datetime(f['date'])
        rate_col = next((c for c in f.columns if 'rate' in c or c == 'open'), None)
        if rate_col is None:
            return None
        return f[['date', rate_col]].rename(columns={rate_col: 'funding'})
    except Exception:
        return None


def build_features(df, funding=None):
    out = df.copy()
    out['logret1'] = np.log(out['close']).diff()

    for w in (5, 15, 30):
        r = out['close'].pct_change(w)
        sd = r.rolling(120).std()
        out[f'ret_z{w}'] = (r - r.rolling(120).mean()) / sd

    out['rng'] = out['high'] - out['low']
    atr20 = out['rng'].rolling(20).mean()
    atr60 = out['rng'].rolling(60).mean()

    ema20 = out['close'].ewm(span=20, adjust=False).mean()
    ema60 = out['close'].ewm(span=60, adjust=False).mean()
    out['ema20_dev'] = (out['close'] - ema20) / atr20
    out['ema60_dev'] = (out['close'] - ema60) / atr60

    pv = (out['close'] * out['volume']).rolling(30).sum()
    vv = out['volume'].rolling(30).sum()
    vwap1h = pv / vv
    out['vwap1h_dev'] = (out['close'] - vwap1h) / atr20

    hh60 = out['high'].rolling(60).max()
    ll60 = out['low'].rolling(60).min()
    out['pos_in_range60'] = (out['close'] - ll60) / (hh60 - ll60)

    vmed20 = out['volume'].rolling(20).median()
    vstd20 = out['volume'].rolling(20).std()
    out['vol_z20'] = (out['volume'] - vmed20) / vstd20

    out['hour'] = out['date'].dt.hour
    out['hour_sin'] = np.sin(2 * np.pi * out['hour'] / 24.0)
    out['hour_cos'] = np.cos(2 * np.pi * out['hour'] / 24.0)

    if funding is not None and len(funding):
        fdf = funding.set_index('date').reindex(
            pd.date_range(funding['date'].min(), out['date'].max(), freq='1min')
        ).ffill().reset_index().rename(columns={'index': 'date'})
        win = 30 * 24 * 60
        fdf['funding_z'] = ((fdf['funding'] - fdf['funding'].rolling(win).mean())
                            / fdf['funding'].rolling(win).std())
        out = out.merge(fdf[['date', 'funding_z']], on='date', how='left')
    else:
        out['funding_z'] = np.nan

    out['exit_close'] = out['close'].shift(-EXPIRY_BARS)
    out['fwd_ret'] = (out['exit_close'] - out['close']) / out['close']
    out['target_call'] = (out['fwd_ret'] > 0).astype(np.int8)
    out['target_put'] = (out['fwd_ret'] < 0).astype(np.int8)

    return out


def simulate_signals(test_df, signals_call, signals_put):
    """Sequential 10-min lockout simulation. Inputs are bool arrays aligned to test_df."""
    n = len(test_df)
    tc = test_df['target_call'].to_numpy()
    tp = test_df['target_put'].to_numpy()
    sc = np.asarray(signals_call, dtype=bool)
    sp = np.asarray(signals_put, dtype=bool)
    cd = 0
    res = []  # 1 win, 0 loss, side stored
    sides = []
    for i in range(n):
        if cd > 0:
            cd -= 1
            continue
        if sc[i] and sp[i]:
            continue
        if sc[i]:
            res.append(int(tc[i])); sides.append(1); cd = EXPIRY_BARS
        elif sp[i]:
            res.append(int(tp[i])); sides.append(-1); cd = EXPIRY_BARS
    return res, sides


def summarize(results):
    if not results:
        return dict(n=0, wins=0, wr=0.0, pnl=0.0, mdd=0.0, max_consec_loss=0)
    arr = np.array(results)
    wins = int(arr.sum()); n = len(arr)
    pnl_per = np.where(arr == 1, PAYOUT_WIN, PAYOUT_LOSS)
    eq = np.cumsum(pnl_per)
    peak = np.maximum.accumulate(eq)
    mdd = float((peak - eq).max())
    consec = max_c = 0
    for w in arr:
        if w == 0: consec += 1; max_c = max(max_c, consec)
        else: consec = 0
    return dict(n=n, wins=wins, wr=wins/n, pnl=float(eq[-1]),
                mdd=mdd, max_consec_loss=max_c)


def fold_iter(feat, warmup_bars, refit_bars):
    n = len(feat)
    start = warmup_bars
    fi = 0
    while start + refit_bars <= n:
        yield fi, feat.iloc[:start], feat.iloc[start:start + refit_bars]
        start += refit_bars
        fi += 1


# ---------- A. Single-signal: ema60_dev ----------
def study_A_single_signal(feat):
    print("\n" + "=" * 70)
    print("STUDY A: Single-signal ema60_dev expanding walk-forward")
    print("=" * 70)
    warmup = WARMUP_DAYS * BARS_PER_DAY
    refit = REFIT_DAYS * BARS_PER_DAY
    all_results = []
    fold_log = []
    for fi, train, test in fold_iter(feat, warmup, refit):
        # Decile boundaries from train
        s = train['ema60_dev'].dropna()
        if s.empty:
            continue
        q05, q95 = np.quantile(s, [0.10, 0.90])  # use deciles 0 & 9 lower/upper bound
        # signal: extreme below -> CALL, extreme above -> PUT
        ev = test['ema60_dev'].to_numpy()
        sc = ev <= q05
        sp = ev >= q95
        res, sides = simulate_signals(test, sc, sp)
        s_summ = summarize(res)
        s_summ['fold'] = fi
        s_summ['q_lo'] = float(q05); s_summ['q_hi'] = float(q95)
        s_summ['date_start'] = test['date'].iloc[0]
        s_summ['n_call'] = int(sum(1 for x in sides if x == 1))
        s_summ['n_put'] = int(sum(1 for x in sides if x == -1))
        fold_log.append(s_summ)
        all_results += res
    agg = summarize(all_results)
    print(f"\n[Aggregated OOS] folds={len(fold_log)}  "
          f"trades={agg['n']}  wins={agg['wins']}  WR={agg['wr']*100:.2f}%  "
          f"PnL(5U,1.8x)={agg['pnl']:+.2f}U  MDD={agg['mdd']:.2f}U  "
          f"MaxConsecLoss={agg['max_consec_loss']}")
    print(f"\n{'fold':>4}{'date':>22}{'q_lo':>8}{'q_hi':>8}{'nC':>5}{'nP':>5}"
          f"{'n':>6}{'WR':>8}{'PnL':>9}")
    for f in fold_log:
        print(f"{f['fold']:>4}{f['date_start'].strftime('%Y-%m-%d'):>22}"
              f"{f['q_lo']:>8.2f}{f['q_hi']:>8.2f}{f['n_call']:>5}{f['n_put']:>5}"
              f"{f['n']:>6}{f['wr']*100:>7.2f}%{f['pnl']:>8.2f}U")
    return agg


# ---------- B. Two-feature combos ----------
def study_B_two_feature(feat):
    print("\n" + "=" * 70)
    print("STUDY B: ema60_dev extreme x second-filter combos")
    print("=" * 70)
    warmup = WARMUP_DAYS * BARS_PER_DAY
    refit = REFIT_DAYS * BARS_PER_DAY

    # Filter recipes: (name, func(test_df)->bool array of "passes filter")
    # Each filter returns True where the trade is ALLOWED.
    filters = {
        'baseline': lambda d: np.ones(len(d), dtype=bool),
        'vol_z20<=0': lambda d: d['vol_z20'].fillna(0).to_numpy() <= 0.0,
        'vol_z20>0': lambda d: d['vol_z20'].fillna(0).to_numpy() > 0.0,
        'vol_z20>1': lambda d: d['vol_z20'].fillna(0).to_numpy() > 1.0,
        'pos_in_range60_align': None,  # special: CALL only if pos<0.5, PUT only if pos>0.5
        'vwap1h_align': None,           # CALL only if vwap1h_dev<0, PUT only if >0
        'funding_z_align': None,        # CALL only if funding_z<0, PUT only if >0
        'hour_us_session': lambda d: ((d['hour'] >= 13) & (d['hour'] < 21)).to_numpy(),
        'hour_asia_session': lambda d: ((d['hour'] >= 0) & (d['hour'] < 8)).to_numpy(),
    }

    def call_filter_signals(name, test, base_call, base_put):
        if name == 'pos_in_range60_align':
            pos = test['pos_in_range60'].to_numpy()
            return base_call & (pos <= 0.3), base_put & (pos >= 0.7)
        if name == 'vwap1h_align':
            v = test['vwap1h_dev'].to_numpy()
            return base_call & (v <= 0), base_put & (v >= 0)
        if name == 'funding_z_align':
            f = test['funding_z'].fillna(0).to_numpy()
            return base_call & (f <= 0), base_put & (f >= 0)
        m = filters[name](test)
        return base_call & m, base_put & m

    summaries = {}
    folds_per_filter = defaultdict(list)
    for name in filters:
        all_res = []
        for fi, train, test in fold_iter(feat, warmup, refit):
            s = train['ema60_dev'].dropna()
            if s.empty: continue
            q05, q95 = np.quantile(s, [0.10, 0.90])
            ev = test['ema60_dev'].to_numpy()
            base_call = ev <= q05
            base_put = ev >= q95
            sc, sp = call_filter_signals(name, test, base_call, base_put)
            res, _ = simulate_signals(test, sc, sp)
            folds_per_filter[name].append(summarize(res))
            all_res += res
        summaries[name] = summarize(all_res)

    print(f"\n{'filter':<22}{'n':>7}{'WR':>8}{'WLB':>8}{'PnL':>10}{'MDD':>8}")
    rows = sorted(summaries.items(), key=lambda kv: -kv[1]['pnl'])
    for name, s in rows:
        wlb = wilson_lb(s['wins'], s['n']) if s['n'] > 0 else 0.0
        print(f"{name:<22}{s['n']:>7}{s['wr']*100:>7.2f}%{wlb*100:>7.2f}%"
              f"{s['pnl']:>9.2f}U{s['mdd']:>7.2f}U")
    return summaries


# ---------- C. Logistic regression baseline ----------
def study_C_logreg(feat):
    print("\n" + "=" * 70)
    print("STUDY C: Logistic regression expanding walk-forward")
    print("=" * 70)
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        print("sklearn not installed; skipping")
        return None

    feat_cols = ['ret_z5', 'ret_z15', 'ret_z30',
                 'ema20_dev', 'ema60_dev',
                 'vwap1h_dev', 'pos_in_range60',
                 'vol_z20', 'hour_sin', 'hour_cos']

    warmup = WARMUP_DAYS * BARS_PER_DAY
    refit = REFIT_DAYS * BARS_PER_DAY

    thresholds = [0.55, 0.56, 0.57, 0.58, 0.60]
    agg_per_thr = {t: [] for t in thresholds}

    for fi, train, test in fold_iter(feat, warmup, refit):
        tr = train[feat_cols + ['target_call']].dropna()
        te = test[feat_cols + ['target_call', 'target_put', 'fwd_ret']].dropna()
        if len(tr) < 1000 or len(te) < 100:
            continue
        scaler = StandardScaler().fit(tr[feat_cols].values)
        X_tr = scaler.transform(tr[feat_cols].values)
        y_tr = tr['target_call'].values
        clf = LogisticRegression(max_iter=2000, C=1.0).fit(X_tr, y_tr)
        X_te = scaler.transform(te[feat_cols].values)
        p_call = clf.predict_proba(X_te)[:, 1]

        for thr in thresholds:
            sc = p_call >= thr
            sp = p_call <= (1 - thr)
            te2 = te.reset_index(drop=True).copy()
            res, _ = simulate_signals(te2.assign(target_call=te2['target_call'],
                                                 target_put=te2['target_put']),
                                      sc, sp)
            agg_per_thr[thr] += res

    print(f"\n{'p_thr':>6}{'n':>7}{'WR':>8}{'WLB':>8}{'PnL':>10}{'MDD':>8}")
    for thr, allres in agg_per_thr.items():
        s = summarize(allres)
        wlb = wilson_lb(s['wins'], s['n']) if s['n'] > 0 else 0.0
        print(f"{thr:>6.2f}{s['n']:>7}{s['wr']*100:>7.2f}%{wlb*100:>7.2f}%"
              f"{s['pnl']:>9.2f}U{s['mdd']:>7.2f}U")


def main():
    print("Loading data ...")
    df2 = load_2m()
    funding = load_funding()
    feat = build_features(df2, funding)
    feat = feat.dropna(subset=['ema60_dev', 'fwd_ret']).reset_index(drop=True)
    days = (feat['date'].iloc[-1] - feat['date'].iloc[0]).total_seconds() / 86400
    print(f"  {len(feat):,} bars  /  {days:.1f} days")

    study_A_single_signal(feat)
    study_B_two_feature(feat)
    study_C_logreg(feat)


if __name__ == '__main__':
    main()
