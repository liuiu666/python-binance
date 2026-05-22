"""Strict validation of regime-conditional mean-reversion + GBM comparison.

Three studies:
  V1. H1/H2 temporal split on the OOS 1-year stream:
      - Pick (trend x vol x daily_bias x side) cells with WLB>=BREAKEVEN on H1
      - Apply same cells to H2, report independent OOS WR/PnL/MDD.
  V2. 4-axis regime table on full year (trend x vol x daily_bias x side),
      printed for inspection.
  V3. HistGradientBoostingClassifier walk-forward baseline
      (same features as logreg) -- nonlinear comparison.

Run:
    .venv\\Scripts\\python -u user_data/notebooks/regime_validation.py
"""
import math
import warnings
from collections import defaultdict
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

FEATHER_1M = "user_data/data/binance/futures/BTC_USDT_USDT-1m-futures.feather"
EXPIRY_BARS = 10
PAYOUT_WIN = 4.0
PAYOUT_LOSS = -5.0
WARMUP_DAYS = 30
REFIT_DAYS = 14
BARS_PER_DAY = 60 * 24
BREAKEVEN_WR = 5.0 / 9.0


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

    atr40 = out['rng'].rolling(40).mean()
    atr120 = out['rng'].rolling(120).mean()
    atr240 = out['rng'].rolling(240).mean()

    ema120 = out['close'].ewm(span=120, adjust=False).mean()
    ema240 = out['close'].ewm(span=240, adjust=False).mean()
    ema720 = out['close'].ewm(span=720, adjust=False).mean()
    out['ema120_dev'] = (out['close'] - ema120) / atr120
    out['ema40_dev'] = (out['close'] - out['close'].ewm(span=40, adjust=False).mean()) / atr40
    out['trend_strength'] = (ema240 - ema720).abs() / atr240

    vmed40 = out['volume'].rolling(40).median()
    vstd40 = out['volume'].rolling(40).std()
    out['vol_z40'] = (out['volume'] - vmed40) / vstd40

    rv60 = out['logret1'].rolling(60).std()
    out['rv_z'] = (rv60 - rv60.rolling(60 * 24).mean()) / rv60.rolling(60 * 24).std()

    pv = (out['close'] * out['volume']).rolling(60).sum()
    vv = out['volume'].rolling(60).sum()
    vwap1h = pv / vv
    out['vwap1h_dev'] = (out['close'] - vwap1h) / atr40

    hh120 = out['high'].rolling(120).max()
    ll120 = out['low'].rolling(120).min()
    out['pos_in_range'] = (out['close'] - ll120) / (hh120 - ll120)

    for w in (10, 30, 60):
        r = out['close'].pct_change(w)
        sd = r.rolling(240).std()
        out[f'ret_z{w}'] = (r - r.rolling(240).mean()) / sd

    out['hour'] = out['date'].dt.hour
    out['hour_sin'] = np.sin(2 * np.pi * out['hour'] / 24.0)
    out['hour_cos'] = np.cos(2 * np.pi * out['hour'] / 24.0)

    out['daily_ret'] = out['close'].pct_change(60 * 24)
    out['daily_bias'] = np.sign(out['daily_ret']).fillna(0).astype(int)

    out['exit_close'] = out['close'].shift(-EXPIRY_BARS)
    out['fwd_ret'] = (out['exit_close'] - out['close']) / out['close']
    out['target_call'] = (out['fwd_ret'] > 0).astype(np.int8)
    out['target_put'] = (out['fwd_ret'] < 0).astype(np.int8)
    return out


def fold_iter(feat, warmup, refit):
    n = len(feat); start = warmup; fi = 0
    while start + refit <= n:
        yield fi, feat.iloc[:start], feat.iloc[start:start + refit]
        start += refit; fi += 1


def base_signals(train, test):
    s = train['ema120_dev'].dropna()
    q05, q95 = np.quantile(s, [0.10, 0.90])
    ev = test['ema120_dev'].to_numpy()
    vz = test['vol_z40'].fillna(0).to_numpy()
    rvz = test['rv_z'].fillna(0).to_numpy()
    base = (vz > 1.0) & (rvz > -1.0) & (rvz < 1.0)
    return (ev <= q05) & base, (ev >= q95) & base


def trend_buckets(train, test):
    s = train['trend_strength'].dropna()
    q33, q67 = np.quantile(s, [1 / 3, 2 / 3])
    ts = test['trend_strength'].to_numpy()
    return np.where(np.isnan(ts), -1, np.where(ts < q33, 0, np.where(ts < q67, 1, 2)))


def vol_buckets(test):
    rv = test['rv_z'].fillna(0).to_numpy()
    return np.where(rv < -0.5, 0, np.where(rv < 0.5, 1, 2))


def simulate_with_meta(test_df, sc, sp, meta_arrays):
    n = len(test_df)
    tc = test_df['target_call'].to_numpy()
    tp = test_df['target_put'].to_numpy()
    sc = np.asarray(sc, dtype=bool); sp = np.asarray(sp, dtype=bool)
    cd = 0; rows = []
    dt = test_df['date'].to_numpy()
    for i in range(n):
        if cd > 0: cd -= 1; continue
        if sc[i] and sp[i]: continue
        if sc[i]:
            rows.append((dt[i], int(tc[i]), 1) + tuple(m[i] for m in meta_arrays))
            cd = EXPIRY_BARS
        elif sp[i]:
            rows.append((dt[i], int(tp[i]), -1) + tuple(m[i] for m in meta_arrays))
            cd = EXPIRY_BARS
    return rows


def summarize(arr):
    if not arr:
        return dict(n=0, wins=0, wr=0.0, pnl=0.0, mdd=0.0)
    a = np.array(arr); wins = int(a.sum()); n = len(a)
    pnl = np.where(a == 1, PAYOUT_WIN, PAYOUT_LOSS)
    eq = np.cumsum(pnl); peak = np.maximum.accumulate(eq)
    mdd = float((peak - eq).max())
    return dict(n=n, wins=wins, wr=wins/n, pnl=float(eq[-1]), mdd=mdd)


# ----------------- main -----------------
def main():
    print("Loading 1m bars ...")
    df = load_1m()
    feat = build_features(df)
    feat = feat.dropna(subset=['ema120_dev', 'fwd_ret', 'trend_strength']).reset_index(drop=True)
    print(f"  {len(feat):,} bars / {(feat['date'].iloc[-1]-feat['date'].iloc[0]).total_seconds()/86400:.1f} days")

    warmup = WARMUP_DAYS * BARS_PER_DAY
    refit = REFIT_DAYS * BARS_PER_DAY

    # Collect every OOS trade with regime labels
    trades = []
    for fi, train, test in fold_iter(feat, warmup, refit):
        sc, sp = base_signals(train, test)
        tb = trend_buckets(train, test)
        vb = vol_buckets(test)
        db = test['daily_bias'].to_numpy().astype(int)
        rows = simulate_with_meta(test, sc, sp, [tb, vb, db])
        # row: (date, win, side, trend, vol, daily_bias)
        trades += rows
    print(f"\nA3 base trades: {len(trades)}")

    # ---- V1. H1/H2 temporal validation ----
    trades.sort(key=lambda r: r[0])
    mid = len(trades) // 2
    H1 = trades[:mid]; H2 = trades[mid:]
    h1_start = H1[0][0]; h2_start = H2[0][0]; end = H2[-1][0]
    print(f"\nH1: {h1_start} ... {H1[-1][0]}  ({len(H1)} trades)")
    print(f"H2: {h2_start} ... {end}  ({len(H2)} trades)")

    print("\n=== V1. 4-axis regime cells on H1, then validate on H2 ===")

    # cell key = (trend, vol, daily_bias, side)
    def cell_summaries(stream):
        agg = defaultdict(list)
        for (_d, w, side, tb_, vb_, db_) in stream:
            agg[(tb_, vb_, db_, side)].append(w)
        out = {}
        for k, v in agg.items():
            wins = sum(v); n = len(v)
            out[k] = dict(n=n, wins=wins, wr=wins/n, wlb=wilson_lb(wins, n),
                          pnl=wins*PAYOUT_WIN + (n-wins)*PAYOUT_LOSS)
        return out

    h1_cells = cell_summaries(H1)
    h2_cells = cell_summaries(H2)

    # Selection on H1 (WLB >= breakeven, N >= 50)
    selected = {k for k, s in h1_cells.items() if s['n'] >= 50 and s['wlb'] >= BREAKEVEN_WR}
    print(f"\nH1-selected cells (WLB>=55.56%, N>=50): {len(selected)}")
    print(f"{'tr':>3}{'vl':>3}{'db':>3}{'side':>5}"
          f"{'H1_n':>6}{'H1_WR':>7}{'H1_WLB':>8}{'H1_PnL':>9}"
          f"{'H2_n':>6}{'H2_WR':>7}{'H2_WLB':>8}{'H2_PnL':>9}")
    for k in sorted(selected):
        h1 = h1_cells[k]
        h2 = h2_cells.get(k, dict(n=0, wins=0, wr=0.0, wlb=0.0, pnl=0.0))
        tb_, vb_, db_, side = k
        sname = 'CALL' if side == 1 else 'PUT'
        print(f"{tb_:>3}{vb_:>3}{db_:>3}{sname:>5}"
              f"{h1['n']:>6}{h1['wr']*100:>6.2f}%{h1['wlb']*100:>7.2f}%{h1['pnl']:>8.2f}U"
              f"{h2['n']:>6}{h2['wr']*100:>6.2f}%{h2['wlb']*100:>7.2f}%{h2['pnl']:>8.2f}U")

    # Applied performance on H2
    kept_h2 = [w for (_d, w, s_, tb_, vb_, db_) in H2
               if (tb_, vb_, db_, s_) in selected]
    s = summarize(kept_h2)
    print(f"\nH2 applied: trades={s['n']}  WR={s['wr']*100:.2f}%  "
          f"WLB={wilson_lb(s['wins'], s['n'])*100:.2f}%  "
          f"PnL={s['pnl']:+.2f}U  MDD={s['mdd']:.2f}U")

    # Also report symmetric: select on H2, apply to H1
    selected_h2 = {k for k, s in h2_cells.items() if s['n'] >= 50 and s['wlb'] >= BREAKEVEN_WR}
    kept_h1 = [w for (_d, w, s_, tb_, vb_, db_) in H1
               if (tb_, vb_, db_, s_) in selected_h2]
    s = summarize(kept_h1)
    print(f"H2->H1 cross: cells={len(selected_h2)} trades={s['n']}  "
          f"WR={s['wr']*100:.2f}%  WLB={wilson_lb(s['wins'], s['n'])*100:.2f}%  "
          f"PnL={s['pnl']:+.2f}U  MDD={s['mdd']:.2f}U")

    # Cells stable in BOTH halves
    stable = {k for k in selected if k in selected_h2}
    print(f"\nCells passing breakeven in BOTH H1 and H2: {len(stable)}")
    if stable:
        all_w = [w for (_d, w, s_, tb_, vb_, db_) in trades
                 if (tb_, vb_, db_, s_) in stable]
        s = summarize(all_w)
        print(f"  full-year applied: trades={s['n']}  WR={s['wr']*100:.2f}%  "
              f"WLB={wilson_lb(s['wins'], s['n'])*100:.2f}%  "
              f"PnL={s['pnl']:+.2f}U  MDD={s['mdd']:.2f}U  "
              f"Calmar={s['pnl']/s['mdd'] if s['mdd']>0 else float('inf'):.2f}")
        print(f"  cells: {sorted(stable)}")

    # ---- V2. Full 4-axis table ----
    print("\n=== V2. Full 4-axis cell table (full year, N>=80) ===")
    full = cell_summaries(trades)
    rows = sorted(full.items(), key=lambda kv: -kv[1]['pnl'])
    print(f"{'tr':>3}{'vl':>3}{'db':>3}{'side':>5}"
          f"{'n':>6}{'WR':>8}{'WLB':>8}{'PnL':>9}")
    for k, s in rows:
        if s['n'] < 80: continue
        tb_, vb_, db_, side = k
        sname = 'CALL' if side == 1 else 'PUT'
        marker = ''
        if s['wlb'] >= BREAKEVEN_WR: marker = '  ***'
        elif s['wlb'] >= 0.54: marker = '   *'
        print(f"{tb_:>3}{vb_:>3}{db_:>3}{sname:>5}"
              f"{s['n']:>6}{s['wr']*100:>7.2f}%{s['wlb']*100:>7.2f}%"
              f"{s['pnl']:>8.2f}U{marker}")

    # ---- V3. HistGradientBoosting baseline ----
    try:
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        print("\n[skip] sklearn HistGradientBoosting unavailable")
        return

    print("\n=== V3. HistGradientBoosting walk-forward baseline ===")
    cols = ['ret_z10', 'ret_z30', 'ret_z60',
            'ema40_dev', 'ema120_dev',
            'vwap1h_dev', 'pos_in_range',
            'vol_z40', 'rv_z', 'trend_strength',
            'hour_sin', 'hour_cos', 'daily_bias']

    thresholds = [0.55, 0.56, 0.57, 0.58, 0.60]

    def gbm_signals(train, test):
        tr = train[cols + ['target_call']].dropna()
        if len(tr) < 5000:
            return None
        clf = HistGradientBoostingClassifier(
            max_iter=200, max_depth=4, learning_rate=0.05,
            min_samples_leaf=200, random_state=42)
        clf.fit(tr[cols].values, tr['target_call'].values)
        te = test[cols].fillna(0.0).values
        return clf.predict_proba(te)[:, 1]

    agg_per_thr = {t: [] for t in thresholds}
    for fi, train, test in fold_iter(feat, warmup, refit):
        p = gbm_signals(train, test)
        if p is None: continue
        for thr in thresholds:
            sc = p >= thr
            sp = p <= (1 - thr)
            te2 = test.reset_index(drop=True)
            tc = te2['target_call'].to_numpy()
            tp = te2['target_put'].to_numpy()
            cd = 0
            for i in range(len(te2)):
                if cd > 0: cd -= 1; continue
                if sc[i] and sp[i]: continue
                if sc[i]:
                    agg_per_thr[thr].append(int(tc[i])); cd = EXPIRY_BARS
                elif sp[i]:
                    agg_per_thr[thr].append(int(tp[i])); cd = EXPIRY_BARS

    print(f"{'p_thr':>6}{'n':>7}{'WR':>8}{'WLB':>8}{'PnL':>10}{'MDD':>8}{'Calmar':>8}")
    for thr, all_w in agg_per_thr.items():
        s = summarize(all_w)
        wlb = wilson_lb(s['wins'], s['n']) if s['n'] > 0 else 0.0
        cal = s['pnl']/s['mdd'] if s['mdd'] > 0 else float('inf')
        print(f"{thr:>6.2f}{s['n']:>7}{s['wr']*100:>7.2f}%{wlb*100:>7.2f}%"
              f"{s['pnl']:>9.2f}U{s['mdd']:>7.2f}U{cal:>8.2f}")


if __name__ == '__main__':
    main()
