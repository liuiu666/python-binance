"""A4 多期 EMA 共振策略回测器 — 验证实盘信号机的真实 OOS 表现。

运行:
    .venv\Scripts\python -u user_data/notebooks/a4_backtest.py

背景: live_signal_runner.py 当前跑的是 A4 策略（4 EMA 共振 + 分级信号），
但该策略从未经过独立回测验证。本文 件用真实 OOS walk-forward 验证 A4。

与 binary_option_backtest.py 的区别:
  - binary_option_backtest.py: A3 单 EMA120 策略 (已验证)
  - a4_backtest.py:           A4 多期 EMA 共振策略 (本文件, 验证中)
"""
import math
import warnings
import importlib.util
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

FEATHER_1M = "user_data/data/binance/futures/BTC_USDT_USDT-1m-futures.feather"
EXPIRY_BARS = 10
PAYOUT_WIN = 4.0
PAYOUT_LOSS = -5.0

# ========================== A4 参数（同步 live_signal_runner.py）==========================
EMA_SPANS = [30, 60, 120, 240]
ATR_WIN = 120
VOL_Z_WIN = 40
RV_WIN = 60
RV_BASELINE_WIN = 60 * 24
EMA_DEV_QUANTILE_WIN = 60 * 24 * 14
HQ_CALL_QLO = 0.05; HQ_CALL_K = 4
HQ_PUT_QHI  = 0.95; HQ_PUT_K  = 2
NORM_CALL_QLO = 0.10; NORM_CALL_K = 4
NORM_PUT_QHI  = 0.90; NORM_PUT_K  = 3
VOL_Z_THRESHOLD = 1.0
RV_Z_BAND = 1.0


# ========================== 指标（复制 live_signal_runner.py 逻辑）==========================

def compute_a4_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    # True Range ATR (FIX-1)
    prev_close = out['close'].shift(1)
    tr1 = out['high'] - out['low']
    tr2 = (out['high'] - prev_close).abs()
    tr3 = (out['low'] - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr120 = true_range.ewm(alpha=1.0 / ATR_WIN, adjust=False).mean()
    out['atr'] = atr120

    # 4 EMA 偏离 + rank pct
    min_q = 2 * 60 * 24
    for s in EMA_SPANS:
        ema_s = out['close'].ewm(span=s, adjust=False).mean()
        dev = (out['close'] - ema_s) / atr120.replace(0, np.nan)
        out[f'd{s}'] = dev
        out[f'q{s}'] = (
            dev.rolling(EMA_DEV_QUANTILE_WIN, min_periods=min_q)
            .rank(pct=True)
        )

    # vol_z40 (FIX-2)
    vmed40 = out['volume'].rolling(VOL_Z_WIN).median()
    vstd40 = out['volume'].rolling(VOL_Z_WIN).std()
    out['vol_z40'] = (out['volume'] - vmed40) / vstd40.replace(0, np.nan)

    # rv_z (FIX-2)
    logret1 = np.log(out['close']).diff()
    rv60 = logret1.rolling(RV_WIN).std()
    rv_mean = rv60.rolling(RV_BASELINE_WIN, min_periods=RV_BASELINE_WIN).mean()
    rv_std  = rv60.rolling(RV_BASELINE_WIN, min_periods=RV_BASELINE_WIN).std()
    out['rv_z'] = ((rv60 - rv_mean) / rv_std.replace(0, np.nan)).replace(
        [np.inf, -np.inf], np.nan)

    base = (
        (out['vol_z40'] > VOL_Z_THRESHOLD)
        & (out['rv_z'] > -RV_Z_BAND)
        & (out['rv_z'] < RV_Z_BAND)
        & out[f'q{EMA_SPANS[0]}'].notna()
    )

    hq_lo_count   = sum((out[f'q{s}'] <= HQ_CALL_QLO).astype('Int64').fillna(0) for s in EMA_SPANS)
    norm_lo_count = sum((out[f'q{s}'] <= NORM_CALL_QLO).astype('Int64').fillna(0) for s in EMA_SPANS)
    hq_hi_count   = sum((out[f'q{s}'] >= HQ_PUT_QHI).astype('Int64').fillna(0)  for s in EMA_SPANS)
    norm_hi_count = sum((out[f'q{s}'] >= NORM_PUT_QHI).astype('Int64').fillna(0) for s in EMA_SPANS)

    out['enter_long']  = 0
    out['enter_short'] = 0
    out['signal_tier'] = ""

    # 方向和分级
    mask_hq_call  = base & (hq_lo_count   >= HQ_CALL_K)
    mask_hq_put   = base & (hq_hi_count   >= HQ_PUT_K)
    mask_norm_call = base & (norm_lo_count >= NORM_CALL_K) & ~mask_hq_call
    mask_norm_put  = base & (norm_hi_count >= NORM_PUT_K)  & ~mask_hq_put

    out.loc[mask_hq_call,   'enter_long']  = 1
    out.loc[mask_hq_put,    'enter_short'] = 1
    out.loc[mask_norm_call, 'enter_long']  = 1
    out.loc[mask_norm_put,  'enter_short'] = 1
    out.loc[mask_hq_call,   'signal_tier'] = "HQ"
    out.loc[mask_hq_put,    'signal_tier'] = "HQ"
    out.loc[mask_norm_call, 'signal_tier'] = "NORM"
    out.loc[mask_norm_put,  'signal_tier'] = "NORM"

    return out


# ========================== Walk-forward OOS 验证 ==========================

def wilson_lb(k, n, z=1.96):
    if n == 0:
        return 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = q = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom


def simulate(df, expiry=EXPIRY_BARS):
    """Sequential 10-min lockout, 1.8x binary payout."""
    enter_long  = df['enter_long'].fillna(0).to_numpy().astype(np.int8)
    enter_short = df['enter_short'].fillna(0).to_numpy().astype(np.int8)
    close = df['close'].to_numpy()

    n = len(df)
    cd = 0
    rows = []
    for i in range(n - expiry):
        if cd > 0:
            cd -= 1
            continue
        if enter_long[i] == 1 and enter_short[i] == 0:
            entry = close[i]; exit_p = close[i + expiry]
            win = int(exit_p > entry)
            rows.append((df['date'].iloc[i], 1, df['signal_tier'].iloc[i], entry, exit_p, win))
            cd = expiry
        elif enter_short[i] == 1 and enter_long[i] == 0:
            entry = close[i]; exit_p = close[i + expiry]
            win = int(exit_p < entry)
            rows.append((df['date'].iloc[i], -1, df['signal_tier'].iloc[i], entry, exit_p, win))
            cd = expiry
    return pd.DataFrame(rows, columns=['date', 'side', 'tier', 'entry', 'exit', 'win'])


def summarize(trades_df, label=""):
    n = len(trades_df)
    if n == 0:
        return dict(n=0, wins=0, wr=0.0, wlb=0.0, pnl=0.0, mdd=0.0, max_consec_loss=0, label=label)
    wins = int(trades_df['win'].sum())
    pnl_per = np.where(trades_df['win'].to_numpy() == 1, PAYOUT_WIN, PAYOUT_LOSS)
    eq = np.cumsum(pnl_per)
    peak = np.maximum.accumulate(eq)
    mdd = float((peak - eq).max())
    consec = max_c = 0
    for w in trades_df['win'].to_numpy():
        if w == 0:
            consec += 1; max_c = max(max_c, consec)
        else:
            consec = 0
    return dict(
        n=n, wins=wins, wr=wins / n,
        wlb=wilson_lb(wins, n),
        pnl=float(eq[-1]),
        mdd=mdd, max_consec_loss=max_c,
        label=label,
    )


def walkforward_validate(df, n_folds=8):
    """ Expanding-window walk-forward: train on first (i/n_folds),
        validate on the remaining (i+1)/n_folds. """
    total_len = len(df)
    results = []
    for fold in range(1, n_folds):
        train_end = int(total_len * fold / n_folds)
        val_start = train_end
        val_end   = int(total_len * (fold + 1) / n_folds) if fold < n_folds - 1 else total_len
        if val_end - val_start < 1000:
            continue
        val_df = df.iloc[val_start:val_end].reset_index(drop=True)
        trades = simulate(compute_a4_indicators(val_df))
        s = summarize(trades, label=f"Fold {fold} OOS [{val_df['date'].iloc[0].date()} ~ {val_df['date'].iloc[-1].date()}]")
        results.append(s)
        print(f"  Fold {fold}: n={s['n']:4d}  WR={s['wr']*100:.2f}%  "
              f"WilsonLB={s['wlb']*100:.2f}%  PnL={s['pnl']:+.0f}U  "
              f"MDD={s['mdd']:.0f}U  tier=HQ/{(trades['tier']=='HQ').sum()} NORM/{(trades['tier']=='NORM').sum()}")
    return results


def by_month(trades_df):
    if trades_df.empty:
        return pd.DataFrame()
    t = trades_df.copy()
    # FIX-10: keep UTC context by dropping tz-awareness before to_period
    t['month'] = t['date'].dt.tz_localize(None).dt.to_period('M')
    out = []
    for m, sub in t.groupby('month', sort=True):
        s = summarize(sub)
        out.append((str(m), s['n'], s['wr'], s['pnl'], s['mdd']))
    return pd.DataFrame(out, columns=['month', 'n', 'wr', 'pnl', 'mdd'])


def main():
    print("Loading 1m bars ...")
    df = pd.read_feather(FEATHER_1M)
    df.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
    df['date'] = pd.to_datetime(df['date'])   # keep naive, UTC implied
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce').astype(float)
    print(f"  bars={len(df):,}  {df['date'].iloc[0]}  →  {df['date'].iloc[-1]}")

    print("\nComputing A4 indicators ...")
    df = compute_a4_indicators(df)
    print(f"  HQ_CALL  signals: {(df['enter_long']==1).sum()}  (HQ={(df['signal_tier']=='HQ').sum()})")
    print(f"  HQ_PUT   signals: {(df['enter_short']==1).sum()}")

    trades = simulate(df)
    s = summarize(trades, label="Full period")

    days = (df['date'].iloc[-1] - df['date'].iloc[0]).total_seconds() / 86400
    calmar = s['pnl'] / s['mdd'] if s['mdd'] > 0 else float('inf')

    print("\n" + "=" * 60)
    print(f"A4 多期 EMA 共振回测 (1.8x payout, 5U stake, 10-min lockout)")
    print("=" * 60)
    print(f"  Span               : {days:.1f} days")
    print(f"  总交易             : {s['n']}  ({s['n']/days:.2f}/day)")
    print(f"  HQ / NORM          : {(trades['tier']=='HQ').sum()} / {(trades['tier']=='NORM').sum()}")
    print(f"  Long / Short       : {(trades['side']==1).sum()} / {(trades['side']==-1).sum()}")
    print(f"  胜 / 负            : {s['wins']} / {s['n']-s['wins']}")
    print(f"  胜率               : {s['wr']*100:.2f}%")
    print(f"  Wilson 95% LB     : {s['wlb']*100:.2f}%  "
          f"{'✅ > 55.56%' if s['wlb'] > 0.5556 else '❌ < 55.56%'}")
    print(f"  总 PnL             : {s['pnl']:+.2f} U")
    print(f"  最大回撤           : {s['mdd']:.2f} U")
    print(f"  最大连败           : {s['max_consec_loss']}")
    print(f"  Calmar             : {calmar:.2f}")
    print(f"  单笔 EV            : {s['pnl']/s['n']:+.4f} U")

    print("\n--- Walk-Forward OOS 验证 (8-fold expanding) ---")
    wf_results = walkforward_validate(df, n_folds=8)
    if wf_results:
        total_wf = sum(r['n'] for r in wf_results)
        total_win = sum(r['wins'] for r in wf_results)
        avg_wlb = np.mean([r['wlb'] for r in wf_results])
        print(f"\n  OOS 汇总: 笔数={total_wf}  WR={total_win/total_wf*100:.2f}%  "
              f"Avg WilsonLB={avg_wlb*100:.2f}%  "
              f"{'✅ Avg LB > 55.56%' if avg_wlb > 0.5556 else '❌ Avg LB < 55.56%'}")

    print("\n--- 月度分解 ---")
    mb = by_month(trades)
    if not mb.empty:
        print(f"  {'month':<8}{'n':>6}{'WR':>8}{'PnL':>10}{'MDD':>9}")
        for _, r in mb.iterrows():
            flag = "✅" if r['wr'] >= 0.5556 else "❌"
            print(f"  {r['month']:<8}{int(r['n']):>6}{r['wr']*100:>7.2f}%{r['pnl']:>9.2f}U{r['mdd']:>8.2f}U {flag}")

    print("\n--- A3 对比基准（来自 binary_option_backtest.py）---")
    print("  A3 (单 EMA120): WR=57.02%  WilsonLB=55.82%  PnL=+869U  MDD=204U")
    a3_wlb = 0.5582
    print(f"  A4 (4 EMA共振):  WilsonLB={s['wlb']*100:.2f}%  "
          f"{'✅ 超越 A3' if s['wlb'] > a3_wlb else '⚠️ 低于 A3'}")


if __name__ == '__main__':
    main()
