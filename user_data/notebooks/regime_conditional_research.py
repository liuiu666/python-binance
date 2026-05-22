"""Regime-conditional analysis of the mean-reversion signal.

Take the A3 baseline (ema120_dev extreme + vol_z40>1 + rv_z in (-1,+1)) and
slice it by:
  axis 1 -- TREND STRENGTH:  |EMA(240) - EMA(720)| / ATR(240)
            terciles built on training data each fold
  axis 2 -- VOL REGIME:      rv_z bucketed into (-inf, -0.5), [-0.5,0.5), [0.5, +inf)
  axis 3 -- DAILY BIAS:      sign of close - close[1440 bars ago]

For each regime cell, report aggregated OOS WR / WLB / PnL across 24 folds.
Final step: build a 'regime-aware' strategy that disables cells with WLB <
breakeven and runs full WF.

Run:
    .venv\\Scripts\\python -u user_data/notebooks/regime_conditional_research.py
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
BREAKEVEN_WR = 5.0 / 9.0  # 1.8x payout breakeven = 55.56%


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

    # Trend strength: long-EMA spread normalized
    out['trend_strength'] = (ema240 - ema720).abs() / atr240
    out['trend_dir'] = np.sign(ema240 - ema720)

    # Volume z
    vmed40 = out['volume'].rolling(40).median()
    vstd40 = out['volume'].rolling(40).std()
    out['vol_z40'] = (out['volume'] - vmed40) / vstd40

    # Realized vol regime
    rv60 = out['logret1'].rolling(60).std()
    rv60_mean = rv60.rolling(60 * 24).mean()
    rv60_std = rv60.rolling(60 * 24).std()
    out['rv_z'] = (rv60 - rv60_mean) / rv60_std

    # Daily bias: sign of 24h return
    out['daily_ret'] = out['close'].pct_change(60 * 24)
    out['daily_bias'] = np.sign(out['daily_ret']).fillna(0).astype(int)

    out['exit_close'] = out['close'].shift(-EXPIRY_BARS)
    out['fwd_ret'] = (out['exit_close'] - out['close']) / out['close']
    out['target_call'] = (out['fwd_ret'] > 0).astype(np.int8)
    out['target_put'] = (out['fwd_ret'] < 0).astype(np.int8)
    return out


def base_signals(train, test):
    """A3 baseline: ema120_dev decile 0/9 + vol_z40>1 + rv_z in (-1,+1)."""
    s = train['ema120_dev'].dropna()
    q05, q95 = np.quantile(s, [0.10, 0.90])
    ev = test['ema120_dev'].to_numpy()
    vz = test['vol_z40'].fillna(0).to_numpy()
    rvz = test['rv_z'].fillna(0).to_numpy()
    base_filter = (vz > 1.0) & (rvz > -1.0) & (rvz < 1.0)
    sc = (ev <= q05) & base_filter
    sp = (ev >= q95) & base_filter
    return sc, sp


def trend_buckets(train, test):
    """Tercile of trend_strength built on TRAIN."""
    s = train['trend_strength'].dropna()
    if len(s) < 100:
        return np.zeros(len(test), dtype=int)
    q33, q67 = np.quantile(s, [1 / 3, 2 / 3])
    ts = test['trend_strength'].to_numpy()
    bucket = np.where(np.isnan(ts), -1,
                      np.where(ts < q33, 0, np.where(ts < q67, 1, 2)))
    return bucket  # 0=range, 1=mid, 2=trend


def vol_buckets(test):
    rv = test['rv_z'].fillna(0).to_numpy()
    return np.where(rv < -0.5, 0, np.where(rv < 0.5, 1, 2))  # 0=low, 1=mid, 2=high


def fold_iter(feat, warmup, refit):
    n = len(feat); start = warmup; fi = 0
    while start + refit <= n:
        yield fi, feat.iloc[:start], feat.iloc[start:start + refit]
        start += refit; fi += 1


def simulate_with_meta(test_df, sc, sp, meta_arrays):
    """Returns list of (result, side, meta_tuple) respecting 10-bar lockout."""
    n = len(test_df)
    tc = test_df['target_call'].to_numpy()
    tp = test_df['target_put'].to_numpy()
    sc = np.asarray(sc, dtype=bool); sp = np.asarray(sp, dtype=bool)
    cd = 0; rows = []
    for i in range(n):
        if cd > 0: cd -= 1; continue
        if sc[i] and sp[i]: continue
        if sc[i]:
            meta = tuple(m[i] for m in meta_arrays)
            rows.append((int(tc[i]), 1, meta)); cd = EXPIRY_BARS
        elif sp[i]:
            meta = tuple(m[i] for m in meta_arrays)
            rows.append((int(tp[i]), -1, meta)); cd = EXPIRY_BARS
    return rows


def summarize(arr_results):
    if not arr_results:
        return dict(n=0, wins=0, wr=0.0, pnl=0.0, mdd=0.0, max_consec_loss=0)
    a = np.array(arr_results); wins = int(a.sum()); n = len(a)
    pnl_per = np.where(a == 1, PAYOUT_WIN, PAYOUT_LOSS)
    eq = np.cumsum(pnl_per); peak = np.maximum.accumulate(eq)
    mdd = float((peak - eq).max())
    consec = max_c = 0
    for w in a:
        if w == 0: consec += 1; max_c = max(max_c, consec)
        else: consec = 0
    return dict(n=n, wins=wins, wr=wins/n, pnl=float(eq[-1]),
                mdd=mdd, max_consec_loss=max_c)


def main():
    print("Loading 1m bars ...")
    df = load_1m()
    feat = build_features(df)
    feat = feat.dropna(subset=['ema120_dev', 'fwd_ret', 'trend_strength']).reset_index(drop=True)
    print(f"  {len(feat):,} bars / {(feat['date'].iloc[-1]-feat['date'].iloc[0]).total_seconds()/86400:.1f} days")

    warmup = WARMUP_DAYS * BARS_PER_DAY
    refit = REFIT_DAYS * BARS_PER_DAY

    # Aggregate trades labeled with regime cell across all folds
    all_trades = []  # list of (result, side, trend_b, vol_b, daily_b)
    for fi, train, test in fold_iter(feat, warmup, refit):
        sc, sp = base_signals(train, test)
        trend_b = trend_buckets(train, test)
        vol_b = vol_buckets(test)
        daily_b = test['daily_bias'].to_numpy().astype(int)
        rows = simulate_with_meta(test, sc, sp, [trend_b, vol_b, daily_b])
        all_trades += rows

    if not all_trades:
        print("no trades")
        return

    print(f"\nTotal A3 baseline trades over 1y OOS: {len(all_trades)}")
    base = summarize([r[0] for r in all_trades])
    print(f"  WR={base['wr']*100:.2f}%  WLB={wilson_lb(base['wins'],base['n'])*100:.2f}%  "
          f"PnL={base['pnl']:+.2f}U  MDD={base['mdd']:.2f}U")

    # Slice by single axes
    print("\n=== By TREND_STRENGTH bucket (0=range, 1=mid, 2=strong-trend) ===")
    print(f"{'bucket':>7}{'side':>6}{'n':>7}{'WR':>8}{'WLB':>8}{'PnL':>10}")
    for b in (0, 1, 2):
        for side, sname in [(1, 'CALL'), (-1, 'PUT')]:
            res = [r[0] for r in all_trades if r[2][0] == b and r[1] == side]
            if not res:
                continue
            s = summarize(res); wlb = wilson_lb(s['wins'], s['n'])
            print(f"{b:>7}{sname:>6}{s['n']:>7}{s['wr']*100:>7.2f}%{wlb*100:>7.2f}%{s['pnl']:>9.2f}U")

    print("\n=== By VOL_REGIME bucket (0=low, 1=mid, 2=high) ===")
    print(f"{'bucket':>7}{'side':>6}{'n':>7}{'WR':>8}{'WLB':>8}{'PnL':>10}")
    for b in (0, 1, 2):
        for side, sname in [(1, 'CALL'), (-1, 'PUT')]:
            res = [r[0] for r in all_trades if r[2][1] == b and r[1] == side]
            if not res: continue
            s = summarize(res); wlb = wilson_lb(s['wins'], s['n'])
            print(f"{b:>7}{sname:>6}{s['n']:>7}{s['wr']*100:>7.2f}%{wlb*100:>7.2f}%{s['pnl']:>9.2f}U")

    print("\n=== By DAILY_BIAS (sign of last 24h return) ===")
    print(f"{'bias':>5}{'side':>6}{'n':>7}{'WR':>8}{'WLB':>8}{'PnL':>10}")
    for b in (-1, 0, 1):
        for side, sname in [(1, 'CALL'), (-1, 'PUT')]:
            res = [r[0] for r in all_trades if r[2][2] == b and r[1] == side]
            if not res: continue
            s = summarize(res); wlb = wilson_lb(s['wins'], s['n'])
            print(f"{b:>5}{sname:>6}{s['n']:>7}{s['wr']*100:>7.2f}%{wlb*100:>7.2f}%{s['pnl']:>9.2f}U")

    print("\n=== Two-axis: TREND x SIDE  (rows=trend bucket, cols=side) ===")
    print(f"{'trend':>6}{'side':>6}{'n':>7}{'WR':>8}{'WLB':>8}{'PnL':>10}")
    for tb in (0, 1, 2):
        for side, sname in [(1, 'CALL'), (-1, 'PUT')]:
            res = [r[0] for r in all_trades if r[2][0] == tb and r[1] == side]
            if not res: continue
            s = summarize(res); wlb = wilson_lb(s['wins'], s['n'])
            print(f"{tb:>6}{sname:>6}{s['n']:>7}{s['wr']*100:>7.2f}%{wlb*100:>7.2f}%{s['pnl']:>9.2f}U")

    print("\n=== Three-axis: TREND x VOL x SIDE ===")
    print(f"{'tr':>3}{'vl':>3}{'side':>6}{'n':>6}{'WR':>8}{'WLB':>8}{'PnL':>9}")
    keep_cells = []
    for tb in (0, 1, 2):
        for vb in (0, 1, 2):
            for side, sname in [(1, 'CALL'), (-1, 'PUT')]:
                res = [r[0] for r in all_trades if r[2][0] == tb and r[2][1] == vb and r[1] == side]
                if len(res) < 100: continue
                s = summarize(res); wlb = wilson_lb(s['wins'], s['n'])
                marker = ''
                if wlb >= BREAKEVEN_WR:
                    marker = '  ***'
                    keep_cells.append((tb, vb, side))
                elif wlb >= 0.54:
                    marker = '   *'
                print(f"{tb:>3}{vb:>3}{sname:>6}{s['n']:>6}{s['wr']*100:>7.2f}%{wlb*100:>7.2f}%{s['pnl']:>8.2f}U{marker}")

    # Regime-aware aggregate: keep only cells with WLB >= breakeven
    print("\n=== Regime-aware (drop cells with WLB < breakeven) ===")
    if keep_cells:
        keep_set = set(keep_cells)
        kept = [r[0] for r in all_trades
                if (r[2][0], r[2][1], r[1]) in keep_set]
        s = summarize(kept); wlb = wilson_lb(s['wins'], s['n'])
        calmar = s['pnl'] / s['mdd'] if s['mdd'] > 0 else float('inf')
        print(f"  cells kept: {len(keep_cells)}")
        print(f"  trades={s['n']}  WR={s['wr']*100:.2f}%  WLB={wlb*100:.2f}%  "
              f"PnL={s['pnl']:+.2f}U  MDD={s['mdd']:.2f}U  Calmar={calmar:.2f}")
    else:
        print("  no cell passes breakeven")


if __name__ == '__main__':
    main()
