"""Feature lab for 10-min binary option direction prediction on BTC futures 1m.

Loads 1y of futures 1m, resamples to 2m, builds ~20 features, splits 50/50
train/test, reports per-feature decile forward 10-min CALL/PUT win-rates
with Wilson lower bounds, runs a logistic-regression baseline, and prints
a payout-sensitivity table.

Run:
    .venv\\Scripts\\python -u user_data/notebooks/feature_lab.py
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
EXPIRY_BARS = 5  # 10 min on 2m grid
DECILES = 10
TRAIN_FRAC = 0.5
PAYOUT_WIN = 4.0
PAYOUT_LOSS = -5.0


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
    except Exception as e:
        print(f"[warn] funding load failed: {e}")
        return None


def build_features(df, funding=None):
    out = df.copy()
    out['ret1'] = out['close'].pct_change()
    out['logret1'] = np.log(out['close']).diff()

    # Returns over various windows (in bars)
    for w in (5, 15, 30, 60):
        r = out['close'].pct_change(w)
        mu = r.rolling(120).mean()
        sd = r.rolling(120).std()
        out[f'ret_z{w}'] = (r - mu) / sd

    # Realized vol regimes
    out['rv30'] = out['logret1'].rolling(30).std()
    out['rv120'] = out['logret1'].rolling(120).std()
    out['rv_ratio'] = out['rv30'] / out['rv120']

    # ATR-ish range
    out['rng'] = out['high'] - out['low']
    out['rng_z20'] = ((out['rng'] - out['rng'].rolling(20).mean())
                      / out['rng'].rolling(20).std())

    # Bar internals
    body = (out['close'] - out['open']).abs()
    rng_safe = (out['high'] - out['low']).replace(0, np.nan)
    out['body_pct'] = body / rng_safe
    out['upper_wick'] = (out['high'] - out[['open', 'close']].max(axis=1)) / rng_safe
    out['lower_wick'] = (out[['open', 'close']].min(axis=1) - out['low']) / rng_safe

    # Volume z-scores
    vmed20 = out['volume'].rolling(20).median()
    vmed120 = out['volume'].rolling(120).median()
    vstd20 = out['volume'].rolling(20).std()
    out['vol_z20'] = (out['volume'] - vmed20) / vstd20
    out['vol_ratio120'] = out['volume'] / vmed120

    # EMA distance scaled by ATR
    ema20 = out['close'].ewm(span=20, adjust=False).mean()
    ema60 = out['close'].ewm(span=60, adjust=False).mean()
    atr20 = out['rng'].rolling(20).mean()
    atr60 = out['rng'].rolling(60).mean()
    out['ema20_dev'] = (out['close'] - ema20) / atr20
    out['ema60_dev'] = (out['close'] - ema60) / atr60

    # Rolling VWAP (1h = 30 bars on 2m) deviation
    pv = (out['close'] * out['volume']).rolling(30).sum()
    vv = out['volume'].rolling(30).sum()
    vwap1h = pv / vv
    out['vwap1h_dev'] = (out['close'] - vwap1h) / atr20

    # Position within last 60 bar range
    hh60 = out['high'].rolling(60).max()
    ll60 = out['low'].rolling(60).min()
    out['pos_in_range60'] = (out['close'] - ll60) / (hh60 - ll60)

    # Streak: signed run length of same direction
    direc = np.sign(out['close'] - out['open']).fillna(0).astype(int)
    streak = np.zeros(len(direc), dtype=int)
    s = 0
    for i, d in enumerate(direc.values):
        if d == 0 or (i > 0 and d != direc.values[i - 1]):
            s = d
        else:
            s = s + d if d != 0 else 0
        streak[i] = s
    out['streak'] = streak

    # Original 6-bar dir sequence (encoded as 0..63)
    d_arr = (out['close'] > out['open']).astype(int).to_numpy()
    seq = np.zeros(len(d_arr), dtype=np.int64)
    for i in range(6):
        sh = np.zeros_like(d_arr)
        if i == 0:
            sh = d_arr.copy()
        else:
            sh[i:] = d_arr[:-i]
        seq |= (sh.astype(np.int64) << i)
    out['dir_seq6'] = seq

    # Time of day
    hour = out['date'].dt.hour + out['date'].dt.minute / 60.0
    out['hour_sin'] = np.sin(2 * np.pi * hour / 24.0)
    out['hour_cos'] = np.cos(2 * np.pi * hour / 24.0)
    out['minute_of_hour'] = (out['date'].dt.minute).astype(int)

    # Funding-rate features
    if funding is not None and len(funding):
        fdf = funding.set_index('date').reindex(
            pd.date_range(funding['date'].min(), out['date'].max(), freq='1min')
        ).ffill().reset_index().rename(columns={'index': 'date'})
        fdf['funding_z'] = ((fdf['funding'] - fdf['funding'].rolling(30 * 24 * 60).mean())
                            / fdf['funding'].rolling(30 * 24 * 60).std())
        out = out.merge(fdf[['date', 'funding', 'funding_z']], on='date', how='left')
    else:
        out['funding'] = np.nan
        out['funding_z'] = np.nan

    # Forward target on 2m grid: close[i+5] - close[i]
    out['exit_close'] = out['close'].shift(-EXPIRY_BARS)
    out['fwd_ret'] = (out['exit_close'] - out['close']) / out['close']
    out['target_call'] = (out['fwd_ret'] > 0).astype(np.int8)
    out['target_put'] = (out['fwd_ret'] < 0).astype(np.int8)

    return out


FEATURE_COLS_NUM = [
    'ret_z5', 'ret_z15', 'ret_z30', 'ret_z60',
    'rv30', 'rv120', 'rv_ratio',
    'rng_z20',
    'body_pct', 'upper_wick', 'lower_wick',
    'vol_z20', 'vol_ratio120',
    'ema20_dev', 'ema60_dev',
    'vwap1h_dev', 'pos_in_range60',
    'streak',
    'hour_sin', 'hour_cos',
    'funding_z',
]


def decile_table(train, test, feat, n_deciles=DECILES):
    """Return decile table built on TRAIN; report TEST WR per decile."""
    s = train[feat].dropna()
    if s.nunique() < n_deciles:
        return None
    edges = np.unique(np.quantile(s, np.linspace(0, 1, n_deciles + 1)))
    if len(edges) < 3:
        return None
    rows = []
    for d in range(len(edges) - 1):
        lo, hi = edges[d], edges[d + 1]
        if d == len(edges) - 2:
            mask_test = (test[feat] >= lo) & (test[feat] <= hi)
        else:
            mask_test = (test[feat] >= lo) & (test[feat] < hi)
        sub = test.loc[mask_test]
        n = len(sub)
        if n < 30:
            rows.append((d, lo, hi, n, np.nan, np.nan, np.nan, np.nan))
            continue
        cw = int(sub['target_call'].sum())
        pw = int(sub['target_put'].sum())
        rows.append((d, lo, hi, n,
                     cw / n, wilson_lb(cw, n),
                     pw / n, wilson_lb(pw, n)))
    return rows


def report_feature(rows, name):
    edge = 0.0
    print(f"\n--- {name} ---")
    print(f"{'D':>3}{'lo':>10}{'hi':>10}{'N':>7}{'CALL_WR':>9}{'C_LB':>7}{'PUT_WR':>9}{'P_LB':>7}")
    for r in rows:
        d, lo, hi, n, cwr, clb, pwr, plb = r
        if n < 30 or np.isnan(cwr):
            print(f"{d:>3}{lo:>10.3f}{hi:>10.3f}{n:>7}      n/a    n/a      n/a    n/a")
            continue
        marker = ""
        if clb >= 0.55:
            marker = "  *CALL"
            edge = max(edge, clb - 0.5)
        if plb >= 0.55:
            marker = "  *PUT"
            edge = max(edge, plb - 0.5)
        print(f"{d:>3}{lo:>10.3f}{hi:>10.3f}{n:>7}{cwr*100:>8.2f}%{clb*100:>6.2f}%"
              f"{pwr*100:>8.2f}%{plb*100:>6.2f}%{marker}")
    return edge


def feature_summary(rows):
    """Best-bucket Wilson LB for CALL or PUT side."""
    best_call = best_put = 0.0
    for r in rows:
        d, lo, hi, n, cwr, clb, pwr, plb = r
        if np.isnan(cwr):
            continue
        best_call = max(best_call, clb)
        best_put = max(best_put, plb)
    return best_call, best_put


def logreg_baseline(train, test, feat_cols):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    sub_tr = train[feat_cols + ['target_call']].dropna()
    sub_te = test[feat_cols + ['target_call', 'fwd_ret']].dropna()
    if len(sub_tr) < 1000 or len(sub_te) < 1000:
        return None
    X_tr = sub_tr[feat_cols].values
    y_tr = sub_tr['target_call'].values
    X_te = sub_te[feat_cols].values
    y_te = sub_te['target_call'].values
    scaler = StandardScaler().fit(X_tr)
    X_tr_s = scaler.transform(X_tr)
    X_te_s = scaler.transform(X_te)
    clf = LogisticRegression(max_iter=2000, C=1.0).fit(X_tr_s, y_tr)
    p_call = clf.predict_proba(X_te_s)[:, 1]

    print("\n=== Logistic regression OOS by predicted CALL probability ===")
    print(f"{'p_thr':>6}{'side':>6}{'n':>8}{'WR':>8}{'WLB':>8}{'PnL(5U)':>10}")
    for thr in [0.55, 0.56, 0.57, 0.58, 0.60, 0.62]:
        # CALL side
        m = p_call >= thr
        n_ = int(m.sum())
        if n_ >= 30:
            w_ = int(y_te[m].sum())
            wr = w_ / n_; wlb = wilson_lb(w_, n_)
            pnl = w_ * PAYOUT_WIN + (n_ - w_) * PAYOUT_LOSS
            print(f"{thr:>6.2f}{'CALL':>6}{n_:>8}{wr*100:>7.2f}%{wlb*100:>7.2f}%{pnl:>9.2f}U")
        # PUT side: p_call <= 1-thr (i.e., predicted strongly down)
        m = p_call <= (1 - thr)
        n_ = int(m.sum())
        if n_ >= 30:
            w_ = int((y_te[m] == 0).sum())  # PUT wins when target_call==0 AND fwd_ret<0
            # need actual put wins (fwd_ret < 0 strictly)
            put_wins = int((sub_te.loc[m, 'fwd_ret'] < 0).sum())
            wr = put_wins / n_; wlb = wilson_lb(put_wins, n_)
            pnl = put_wins * PAYOUT_WIN + (n_ - put_wins) * PAYOUT_LOSS
            print(f"{thr:>6.2f}{'PUT':>6}{n_:>8}{wr*100:>7.2f}%{wlb*100:>7.2f}%{pnl:>9.2f}U")


def payout_sensitivity_table():
    print("\n=== Payout sensitivity (breakeven WR) ===")
    print(f"{'payout_x':>9}{'win_pl':>8}{'breakeven_WR':>14}")
    for x in [1.7, 1.75, 1.8, 1.85, 1.9, 1.95, 2.0]:
        win = (x - 1) * 5.0
        be = 5.0 / (5.0 + win) * 100
        print(f"{x:>9.2f}{win:>7.2f}U{be:>13.2f}%")


def dir_seq_baseline(train, test):
    """Per-combo OOS like walkforward, but using simple WR≥0.55 + N≥50 train-discovery."""
    g = train.groupby('dir_seq6').agg(n=('target_call', 'size'),
                                       cw=('target_call', 'sum'),
                                       pw=('target_put', 'sum'))
    g = g[g['n'] >= 80]
    if len(g) == 0:
        print("\n=== dir_seq6 baseline === no combos with N>=80 in train")
        return
    # Pick combos via Wilson LB >= 0.52 on train
    g['c_lb'] = [wilson_lb(int(w), int(n)) for w, n in zip(g['cw'], g['n'])]
    g['p_lb'] = [wilson_lb(int(w), int(n)) for w, n in zip(g['pw'], g['n'])]
    call_ids = set(g.index[g['c_lb'] >= 0.52].tolist())
    put_ids = set(g.index[g['p_lb'] >= 0.52].tolist())
    print(f"\n=== dir_seq6 baseline (train Wilson_LB>=0.52, N>=80) ===")
    print(f"  train picked: {len(call_ids)} CALL, {len(put_ids)} PUT")

    g_te = test.groupby('dir_seq6').agg(n=('target_call', 'size'),
                                         cw=('target_call', 'sum'),
                                         pw=('target_put', 'sum'))
    rows = []
    for cid in call_ids:
        if cid in g_te.index:
            n = int(g_te.loc[cid, 'n']); w = int(g_te.loc[cid, 'cw'])
            rows.append(('CALL', cid, n, w, w/n if n else 0, wilson_lb(w, n)))
    for cid in put_ids:
        if cid in g_te.index:
            n = int(g_te.loc[cid, 'n']); w = int(g_te.loc[cid, 'pw'])
            rows.append(('PUT', cid, n, w, w/n if n else 0, wilson_lb(w, n)))
    rows.sort(key=lambda r: -r[5])
    print(f"{'side':>5}{'cid':>5}{'N':>6}{'W':>6}{'WR':>9}{'WLB':>8}")
    for r in rows[:15]:
        print(f"{r[0]:>5}{r[1]:>5}{r[2]:>6}{r[3]:>6}{r[4]*100:>8.2f}%{r[5]*100:>7.2f}%")


def main():
    print("Loading data ...")
    df2 = load_2m()
    funding = load_funding()
    print(f"  2m bars: {len(df2):,}  funding rows: {0 if funding is None else len(funding)}")

    print("Building features ...")
    feat = build_features(df2, funding)
    feat = feat.dropna(subset=['fwd_ret']).reset_index(drop=True)
    n = len(feat)
    cut = int(n * TRAIN_FRAC)
    train = feat.iloc[:cut].copy()
    test = feat.iloc[cut:].copy()
    print(f"  rows train={len(train):,} test={len(test):,}")
    print(f"  base CALL WR train={train['target_call'].mean()*100:.2f}%  "
          f"test={test['target_call'].mean()*100:.2f}%")

    payout_sensitivity_table()

    print("\n=== Per-feature decile OOS WR ===")
    summary = []
    for fcol in FEATURE_COLS_NUM:
        if feat[fcol].isna().all():
            continue
        rows = decile_table(train, test, fcol)
        if rows is None:
            continue
        bc, bp = feature_summary(rows)
        summary.append((fcol, bc, bp))
        report_feature(rows, fcol)

    print("\n=== Feature ranking by best-decile Wilson lower bound (OOS) ===")
    summary.sort(key=lambda r: -max(r[1], r[2]))
    print(f"{'feature':<20}{'best_C_WLB':>12}{'best_P_WLB':>12}")
    for f, bc, bp in summary:
        marker = "  ***" if max(bc, bp) >= 0.55 else ""
        print(f"{f:<20}{bc*100:>11.2f}%{bp*100:>11.2f}%{marker}")

    dir_seq_baseline(train, test)
    logreg_baseline(train, test, [c for c in FEATURE_COLS_NUM if c != 'funding_z'])


if __name__ == '__main__':
    main()
