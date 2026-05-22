"""1m-grid mean-reversion research with ATR regime filter.

Loads 1y of fapi 1m, builds features at 1m resolution (matching horizons of
the prior 2m study), 10-min forward target = 10 bars, with 10-bar lockout.
Adds an ATR regime filter (realized-vol z-score) on top of the
ema_dev + vol_z combo found in study B.

Run:
    .venv\\Scripts\\python -u user_data/notebooks/mean_reversion_1m_research.py
"""
import math
import warnings
from collections import defaultdict
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

FEATHER_1M = "user_data/data/binance/futures/BTC_USDT_USDT-1m-futures.feather"
EXPIRY_BARS = 10        # 10 minutes on 1m grid
PAYOUT_WIN = 4.0
PAYOUT_LOSS = -5.0

WARMUP_DAYS = 30
REFIT_DAYS = 14
BARS_PER_DAY = 60 * 24


def wilson_lb(k, n, z=1.96):
    if n == 0:
        return 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom


def load_1m():
    df = pd.read_feather(FEATHER_1M)
    df.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
    df['date'] = pd.to_datetime(df['date'])
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce').astype(float)
    return df


def build_features(df):
    out = df.copy()
    out['logret1'] = np.log(out['close']).diff()
    out['rng'] = out['high'] - out['low']

    # Horizons matching prior 2m study (2x bars on 1m grid)
    atr40 = out['rng'].rolling(40).mean()
    atr120 = out['rng'].rolling(120).mean()

    ema40 = out['close'].ewm(span=40, adjust=False).mean()
    ema120 = out['close'].ewm(span=120, adjust=False).mean()
    out['ema40_dev'] = (out['close'] - ema40) / atr40
    out['ema120_dev'] = (out['close'] - ema120) / atr120

    # Volume z over 40-min window
    vmed40 = out['volume'].rolling(40).median()
    vstd40 = out['volume'].rolling(40).std()
    out['vol_z40'] = (out['volume'] - vmed40) / vstd40

    # Realized vol regime: 60-min rv vs 24h rolling baseline
    rv60 = out['logret1'].rolling(60).std()
    rv60_mean = rv60.rolling(60 * 24).mean()
    rv60_std = rv60.rolling(60 * 24).std()
    out['rv_z'] = (rv60 - rv60_mean) / rv60_std

    # 1h VWAP deviation
    pv = (out['close'] * out['volume']).rolling(60).sum()
    vv = out['volume'].rolling(60).sum()
    vwap1h = pv / vv
    out['vwap1h_dev'] = (out['close'] - vwap1h) / atr40

    # Position in 2-hour range
    hh120 = out['high'].rolling(120).max()
    ll120 = out['low'].rolling(120).min()
    out['pos_in_range'] = (out['close'] - ll120) / (hh120 - ll120)

    # Returns z-scores
    for w in (10, 30, 60):
        r = out['close'].pct_change(w)
        sd = r.rolling(240).std()
        out[f'ret_z{w}'] = (r - r.rolling(240).mean()) / sd

    out['hour'] = out['date'].dt.hour
    out['hour_sin'] = np.sin(2 * np.pi * out['hour'] / 24.0)
    out['hour_cos'] = np.cos(2 * np.pi * out['hour'] / 24.0)

    # 10-min forward target on 1m grid
    out['exit_close'] = out['close'].shift(-EXPIRY_BARS)
    out['fwd_ret'] = (out['exit_close'] - out['close']) / out['close']
    out['target_call'] = (out['fwd_ret'] > 0).astype(np.int8)
    out['target_put'] = (out['fwd_ret'] < 0).astype(np.int8)

    return out


def simulate(test_df, sc, sp):
    n = len(test_df)
    tc = test_df['target_call'].to_numpy()
    tp = test_df['target_put'].to_numpy()
    sc = np.asarray(sc, dtype=bool); sp = np.asarray(sp, dtype=bool)
    cd = 0
    res = []; sides = []
    for i in range(n):
        if cd > 0:
            cd -= 1; continue
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
    arr = np.array(results); wins = int(arr.sum()); n = len(arr)
    pnl_per = np.where(arr == 1, PAYOUT_WIN, PAYOUT_LOSS)
    eq = np.cumsum(pnl_per)
    peak = np.maximum.accumulate(eq); mdd = float((peak - eq).max())
    consec = max_c = 0
    for w in arr:
        if w == 0: consec += 1; max_c = max(max_c, consec)
        else: consec = 0
    return dict(n=n, wins=wins, wr=wins/n, pnl=float(eq[-1]),
                mdd=mdd, max_consec_loss=max_c)


def fold_iter(feat, warmup_bars, refit_bars):
    n = len(feat); start = warmup_bars; fi = 0
    while start + refit_bars <= n:
        yield fi, feat.iloc[:start], feat.iloc[start:start + refit_bars]
        start += refit_bars; fi += 1


def expanding_evaluate(feat, build_signals_fn, warmup, refit):
    """build_signals_fn(train, test) -> (sc_array, sp_array)"""
    all_res = []; fold_log = []
    for fi, train, test in fold_iter(feat, warmup, refit):
        sc, sp = build_signals_fn(train, test)
        res, _ = simulate(test, sc, sp)
        s = summarize(res); s['fold'] = fi
        s['date_start'] = test['date'].iloc[0]
        fold_log.append(s); all_res += res
    return summarize(all_res), fold_log


def main():
    print("Loading 1m bars ...")
    df = load_1m()
    feat = build_features(df)
    feat = feat.dropna(subset=['ema120_dev', 'fwd_ret']).reset_index(drop=True)
    days = (feat['date'].iloc[-1] - feat['date'].iloc[0]).total_seconds() / 86400
    print(f"  {len(feat):,} bars / {days:.1f} days")

    warmup = WARMUP_DAYS * BARS_PER_DAY
    refit = REFIT_DAYS * BARS_PER_DAY

    # Build factory functions for various combos
    def f_ema120_only(train, test):
        s = train['ema120_dev'].dropna()
        q05, q95 = np.quantile(s, [0.10, 0.90])
        ev = test['ema120_dev'].to_numpy()
        return ev <= q05, ev >= q95

    def f_ema120_volz(train, test):
        s = train['ema120_dev'].dropna()
        q05, q95 = np.quantile(s, [0.10, 0.90])
        ev = test['ema120_dev'].to_numpy()
        vz = test['vol_z40'].fillna(0).to_numpy()
        m = vz > 1.0
        return (ev <= q05) & m, (ev >= q95) & m

    def f_ema120_volz_atrlow(train, test):
        s = train['ema120_dev'].dropna()
        q05, q95 = np.quantile(s, [0.10, 0.90])
        ev = test['ema120_dev'].to_numpy()
        vz = test['vol_z40'].fillna(0).to_numpy()
        rvz = test['rv_z'].fillna(0).to_numpy()
        m = (vz > 1.0) & (rvz < 1.0)  # avoid chaotic regimes
        return (ev <= q05) & m, (ev >= q95) & m

    def f_ema120_volz_atrmid(train, test):
        s = train['ema120_dev'].dropna()
        q05, q95 = np.quantile(s, [0.10, 0.90])
        ev = test['ema120_dev'].to_numpy()
        vz = test['vol_z40'].fillna(0).to_numpy()
        rvz = test['rv_z'].fillna(0).to_numpy()
        m = (vz > 1.0) & (rvz > -1.0) & (rvz < 1.0)
        return (ev <= q05) & m, (ev >= q95) & m

    def f_ema120_volz_atrhi(train, test):
        s = train['ema120_dev'].dropna()
        q05, q95 = np.quantile(s, [0.10, 0.90])
        ev = test['ema120_dev'].to_numpy()
        vz = test['vol_z40'].fillna(0).to_numpy()
        rvz = test['rv_z'].fillna(0).to_numpy()
        m = (vz > 1.0) & (rvz > 0.0)
        return (ev <= q05) & m, (ev >= q95) & m

    def f_ema120_volz_pos(train, test):
        s = train['ema120_dev'].dropna()
        q05, q95 = np.quantile(s, [0.10, 0.90])
        ev = test['ema120_dev'].to_numpy()
        vz = test['vol_z40'].fillna(0).to_numpy()
        pos = test['pos_in_range'].fillna(0.5).to_numpy()
        sc = (ev <= q05) & (vz > 1.0) & (pos <= 0.3)
        sp = (ev >= q95) & (vz > 1.0) & (pos >= 0.7)
        return sc, sp

    def f_logreg(train, test):
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
        cols = ['ret_z10', 'ret_z30', 'ret_z60',
                'ema40_dev', 'ema120_dev',
                'vwap1h_dev', 'pos_in_range',
                'vol_z40', 'rv_z',
                'hour_sin', 'hour_cos']
        tr = train[cols + ['target_call']].dropna()
        if len(tr) < 5000:
            return np.zeros(len(test), dtype=bool), np.zeros(len(test), dtype=bool)
        scaler = StandardScaler().fit(tr[cols].values)
        X_tr = scaler.transform(tr[cols].values); y_tr = tr['target_call'].values
        clf = LogisticRegression(max_iter=2000, C=1.0).fit(X_tr, y_tr)
        # For test rows with NaN features, set to 0 after scaling -> probability stays near 0.5
        te = test[cols].fillna(0.0).values
        X_te = scaler.transform(te)
        p = clf.predict_proba(X_te)[:, 1]
        return p >= 0.55, p <= 0.45

    def f_logreg_strict(train, test):
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
        cols = ['ret_z10', 'ret_z30', 'ret_z60',
                'ema40_dev', 'ema120_dev',
                'vwap1h_dev', 'pos_in_range',
                'vol_z40', 'rv_z',
                'hour_sin', 'hour_cos']
        tr = train[cols + ['target_call']].dropna()
        if len(tr) < 5000:
            return np.zeros(len(test), dtype=bool), np.zeros(len(test), dtype=bool)
        scaler = StandardScaler().fit(tr[cols].values)
        clf = LogisticRegression(max_iter=2000, C=1.0).fit(
            scaler.transform(tr[cols].values), tr['target_call'].values)
        te = test[cols].fillna(0.0).values
        p = clf.predict_proba(scaler.transform(te))[:, 1]
        return p >= 0.57, p <= 0.43

    cases = [
        ('A0  ema120_dev only',      f_ema120_only),
        ('A1  +vol_z40>1',           f_ema120_volz),
        ('A2  +vol_z40>1, ATR<+1',   f_ema120_volz_atrlow),
        ('A3  +vol_z40>1, ATR mid',  f_ema120_volz_atrmid),
        ('A4  +vol_z40>1, ATR>0',    f_ema120_volz_atrhi),
        ('A5  +vol_z40>1, pos align',f_ema120_volz_pos),
        ('B0  logreg p>=0.55',       f_logreg),
        ('B1  logreg p>=0.57',       f_logreg_strict),
    ]

    print("\n" + "=" * 78)
    print(f"{'Strategy':<28}{'n':>7}{'WR':>8}{'WLB':>8}{'PnL':>10}{'MDD':>8}{'Calmar':>8}")
    print("=" * 78)
    for name, fn in cases:
        agg, _ = expanding_evaluate(feat, fn, warmup, refit)
        wlb = wilson_lb(agg['wins'], agg['n']) if agg['n'] > 0 else 0.0
        calmar = agg['pnl'] / agg['mdd'] if agg['mdd'] > 0 else float('inf')
        print(f"{name:<28}{agg['n']:>7}{agg['wr']*100:>7.2f}%{wlb*100:>7.2f}%"
              f"{agg['pnl']:>9.2f}U{agg['mdd']:>7.2f}U{calmar:>8.2f}")


if __name__ == '__main__':
    main()
