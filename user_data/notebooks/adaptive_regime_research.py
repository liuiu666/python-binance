"""Adaptive A3 walk-forward with dynamic cell selection based on recent history.

Idea:
  Every fold (14 days), use the last LOOKBACK_DAYS of A3 signals from the
  TRAINING window (no future info) to compute WR per (daily_bias x side)
  cell. Only enable cells whose recent WR Wilson_LB >= MIN_WLB.
  Cells that have been losing are disabled until they recover.

Also tests:
  - A range-bound (low Bollinger Band Width) filter that disables trades
    when the market is squeezing.

Compares against fixed A3 baseline and reports H1/H2 stability so we know
whether the adaptation truly generalises.
"""
import math
import numpy as np
import pandas as pd
import warnings
from collections import defaultdict

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

FEATHER_1M = "user_data/data/binance/futures/BTC_USDT_USDT-1m-futures.feather"
EXPIRY = 10
PAYOUT_WIN, PAYOUT_LOSS = 4.0, -5.0
WARMUP_DAYS = 30
REFIT_DAYS = 14
BARS_PER_DAY = 60 * 24
LOOKBACK_DAYS = 90
MIN_WLB = 0.52
BREAKEVEN_WR = 5 / 9


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
        df[c] = pd.to_numeric(df[c]).astype(float)
    return df


def build_features(df):
    out = df.copy()
    out['logret1'] = np.log(out['close']).diff()
    rng = out['high'] - out['low']
    atr120 = rng.rolling(120).mean()
    ema120 = out['close'].ewm(span=120, adjust=False).mean()
    out['ema120_dev'] = (out['close'] - ema120) / atr120

    vmed40 = out['volume'].rolling(40).median()
    vstd40 = out['volume'].rolling(40).std()
    out['vol_z40'] = (out['volume'] - vmed40) / vstd40

    rv60 = out['logret1'].rolling(60).std()
    out['rv_z'] = (rv60 - rv60.rolling(60 * 24).mean()) / rv60.rolling(60 * 24).std()

    # Bollinger Band Width (60 bars), normalized by mid
    mid60 = out['close'].rolling(60).mean()
    std60 = out['close'].rolling(60).std()
    out['bbw60'] = (4 * std60) / mid60
    # BBW expansion: bbw60 / its 24h mean -- >1 means expanding, <1 squeezing
    out['bbw_ratio'] = out['bbw60'] / out['bbw60'].rolling(60 * 24).mean()

    # Daily bias (sign of 24h return)
    out['daily_bias'] = np.sign(out['close'].pct_change(60 * 24)).fillna(0).astype(int)

    out['exit_close'] = out['close'].shift(-EXPIRY)
    out['fwd_ret'] = (out['exit_close'] - out['close']) / out['close']
    out['target_call'] = (out['fwd_ret'] > 0).astype(np.int8)
    out['target_put'] = (out['fwd_ret'] < 0).astype(np.int8)
    return out


def base_signals(df, train_window):
    """A3 base signals; thresholds learned from `train_window` rolling 14d."""
    s = train_window['ema120_dev'].dropna()
    q10, q90 = np.quantile(s, [0.10, 0.90])
    ev = df['ema120_dev'].to_numpy()
    vz = df['vol_z40'].fillna(0).to_numpy()
    rz = df['rv_z'].fillna(0).to_numpy()
    flt = (vz > 1.0) & (rz > -1.0) & (rz < 1.0)
    return (ev <= q10) & flt, (ev >= q90) & flt


def simulate(df, sc, sp):
    """Return list of (idx, side, win, daily_bias, bbw_ratio)."""
    tc = df['target_call'].to_numpy()
    tp = df['target_put'].to_numpy()
    db = df['daily_bias'].to_numpy().astype(int)
    bbw = df['bbw_ratio'].to_numpy()
    cd = 0; rows = []
    for i in range(len(df)):
        if cd > 0: cd -= 1; continue
        if sc[i] and sp[i]: continue
        if sc[i]:
            rows.append((i, 1, int(tc[i]), int(db[i]), float(bbw[i]) if not np.isnan(bbw[i]) else 1.0)); cd = EXPIRY
        elif sp[i]:
            rows.append((i, -1, int(tp[i]), int(db[i]), float(bbw[i]) if not np.isnan(bbw[i]) else 1.0)); cd = EXPIRY
    return rows


def summarize(wins):
    if not wins:
        return dict(n=0, wr=0, pnl=0, mdd=0)
    arr = np.array(wins)
    wins_n = int(arr.sum()); n = len(arr)
    p = np.where(arr == 1, PAYOUT_WIN, PAYOUT_LOSS)
    eq = np.cumsum(p); peak = np.maximum.accumulate(eq)
    return dict(n=n, wins=wins_n, wr=wins_n / n,
                wlb=wilson_lb(wins_n, n),
                pnl=float(eq[-1]),
                mdd=float((peak - eq).max()))


def run_baseline(feat):
    warmup = WARMUP_DAYS * BARS_PER_DAY
    refit = REFIT_DAYS * BARS_PER_DAY
    all_trades = []
    start = warmup
    n = len(feat)
    while start + refit <= n:
        train = feat.iloc[:start]
        test = feat.iloc[start:start + refit]
        sc, sp = base_signals(test, train)
        rows = simulate(test, sc, sp)
        # absolute idx within feat
        for (i, side, win, db, bbw) in rows:
            all_trades.append((feat['date'].iloc[start + i], side, win, db, bbw))
        start += refit
    return all_trades


def run_adaptive_cells(feat):
    """Adaptive: per fold, look at LAST LOOKBACK_DAYS of base trades and only enable
    (daily_bias x side) cells whose Wilson_LB >= MIN_WLB."""
    warmup = WARMUP_DAYS * BARS_PER_DAY
    refit = REFIT_DAYS * BARS_PER_DAY
    lookback = LOOKBACK_DAYS * BARS_PER_DAY
    all_trades = []
    history = []  # all base trades seen so far (date, side, win, db)
    start = warmup
    n = len(feat)
    fold_log = []
    while start + refit <= n:
        train = feat.iloc[:start]
        test = feat.iloc[start:start + refit]

        # Decide enabled cells from recent history within [start-lookback, start)
        cutoff = feat['date'].iloc[max(0, start - lookback)]
        recent = [t for t in history if t[0] >= cutoff]
        cell_stats = defaultdict(list)
        for (_d, side, win, db, _b) in recent:
            cell_stats[(db, side)].append(win)
        enabled = set()
        for k, ws in cell_stats.items():
            if len(ws) >= 20 and wilson_lb(sum(ws), len(ws)) >= MIN_WLB:
                enabled.add(k)

        # Generate fold trades
        sc, sp = base_signals(test, train)
        rows = simulate(test, sc, sp)
        kept = []
        for (i, side, win, db, _b) in rows:
            d_abs = feat['date'].iloc[start + i]
            history.append((d_abs, side, win, db, 0.0))
            if (db, side) in enabled:
                kept.append((d_abs, side, win, db, 0.0))
        fold_log.append((feat['date'].iloc[start], len(enabled), len(rows), len(kept)))
        all_trades += kept
        start += refit
    return all_trades, fold_log


def run_bbw_filter(feat, mode='expansion'):
    """Run A3 but skip when BBW is squeezing (or expanding, depending on mode)."""
    warmup = WARMUP_DAYS * BARS_PER_DAY
    refit = REFIT_DAYS * BARS_PER_DAY
    all_trades = []
    start = warmup
    n = len(feat)
    while start + refit <= n:
        train = feat.iloc[:start]
        test = feat.iloc[start:start + refit]
        sc, sp = base_signals(test, train)
        bbw = test['bbw_ratio'].fillna(1.0).to_numpy()
        if mode == 'expansion':
            allow = bbw > 1.0  # only trade when BBW expanding
        elif mode == 'squeeze':
            allow = bbw <= 1.0  # only trade when BBW squeezing (control)
        else:
            allow = np.ones_like(bbw, dtype=bool)
        rows = simulate(test, sc & allow, sp & allow)
        for (i, side, win, db, _b) in rows:
            all_trades.append((feat['date'].iloc[start + i], side, win, db, 0.0))
        start += refit
    return all_trades


def run_combined(feat):
    """Both adaptive cell selection AND bbw expansion filter."""
    warmup = WARMUP_DAYS * BARS_PER_DAY
    refit = REFIT_DAYS * BARS_PER_DAY
    lookback = LOOKBACK_DAYS * BARS_PER_DAY
    all_trades = []
    history = []
    start = warmup
    n = len(feat)
    while start + refit <= n:
        train = feat.iloc[:start]
        test = feat.iloc[start:start + refit]
        cutoff = feat['date'].iloc[max(0, start - lookback)]
        recent = [t for t in history if t[0] >= cutoff]
        cell_stats = defaultdict(list)
        for (_d, side, win, db, _b) in recent:
            cell_stats[(db, side)].append(win)
        enabled = set()
        for k, ws in cell_stats.items():
            if len(ws) >= 20 and wilson_lb(sum(ws), len(ws)) >= MIN_WLB:
                enabled.add(k)
        sc, sp = base_signals(test, train)
        bbw = test['bbw_ratio'].fillna(1.0).to_numpy()
        allow = bbw > 1.0
        rows = simulate(test, sc & allow, sp & allow)
        for (i, side, win, db, _b) in rows:
            d_abs = feat['date'].iloc[start + i]
            history.append((d_abs, side, win, db, 0.0))
            if (db, side) in enabled:
                all_trades.append((d_abs, side, win, db, 0.0))
        start += refit
    return all_trades


def report_monthly(trades, name):
    if not trades:
        print(f"\n--- {name}: no trades")
        return
    df = pd.DataFrame(trades, columns=['date', 'side', 'win', 'db', 'bbw'])
    df['month'] = df['date'].dt.to_period('M').astype(str)
    print(f"\n--- {name} monthly ---")
    print(f"{'month':>8}{'n':>5}{'WR':>8}{'PnL':>8}")
    for m, sub in df.groupby('month'):
        s = summarize(sub['win'].tolist())
        print(f"{m:>8}{s['n']:>5}{s['wr']*100:>7.2f}%{s['pnl']:>7.2f}U")


def main():
    print("Loading 1m bars + features ...")
    df = load_1m()
    feat = build_features(df).dropna(subset=['ema120_dev', 'fwd_ret', 'bbw_ratio']).reset_index(drop=True)
    print(f"  {len(feat):,} bars")

    print("\n=== Baseline A3 (static) ===")
    base = run_baseline(feat)
    s = summarize([t[2] for t in base])
    print(f"  n={s['n']}  WR={s['wr']*100:.2f}%  WLB={s['wlb']*100:.2f}%  "
          f"PnL={s['pnl']:+.2f}U  MDD={s['mdd']:.2f}U  "
          f"Calmar={s['pnl']/s['mdd'] if s['mdd']>0 else float('inf'):.2f}")

    print(f"\n=== Adaptive cell selection (lookback={LOOKBACK_DAYS}d, WLB>={MIN_WLB}) ===")
    adp, fold_log = run_adaptive_cells(feat)
    s = summarize([t[2] for t in adp])
    print(f"  n={s['n']}  WR={s['wr']*100:.2f}%  WLB={s['wlb']*100:.2f}%  "
          f"PnL={s['pnl']:+.2f}U  MDD={s['mdd']:.2f}U  "
          f"Calmar={s['pnl']/s['mdd'] if s['mdd']>0 else float('inf'):.2f}")

    print(f"\n=== BBW expansion filter (only trade when 1h BBW > 24h avg) ===")
    bex = run_bbw_filter(feat, 'expansion')
    s = summarize([t[2] for t in bex])
    print(f"  n={s['n']}  WR={s['wr']*100:.2f}%  WLB={s['wlb']*100:.2f}%  "
          f"PnL={s['pnl']:+.2f}U  MDD={s['mdd']:.2f}U  "
          f"Calmar={s['pnl']/s['mdd'] if s['mdd']>0 else float('inf'):.2f}")

    print(f"\n=== BBW squeeze (control - should be WORSE) ===")
    bsq = run_bbw_filter(feat, 'squeeze')
    s = summarize([t[2] for t in bsq])
    print(f"  n={s['n']}  WR={s['wr']*100:.2f}%  WLB={s['wlb']*100:.2f}%  "
          f"PnL={s['pnl']:+.2f}U  MDD={s['mdd']:.2f}U")

    print(f"\n=== Combined: Adaptive cells + BBW expansion ===")
    comb = run_combined(feat)
    s = summarize([t[2] for t in comb])
    print(f"  n={s['n']}  WR={s['wr']*100:.2f}%  WLB={s['wlb']*100:.2f}%  "
          f"PnL={s['pnl']:+.2f}U  MDD={s['mdd']:.2f}U  "
          f"Calmar={s['pnl']/s['mdd'] if s['mdd']>0 else float('inf'):.2f}")

    # Monthly breakdown to see if March 2026 specifically improved
    report_monthly(base, "Baseline A3")
    report_monthly(adp, "Adaptive cells")
    report_monthly(bex, "BBW expansion")
    report_monthly(comb, "Combined")


if __name__ == '__main__':
    main()
