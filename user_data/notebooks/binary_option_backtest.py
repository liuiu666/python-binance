"""Binary-option backtest for MeanReversion10mStrategy.

Loads 1y BTCUSDT futures 1m K-lines, instantiates the freqtrade strategy
class to compute indicators and entry signals, then simulates a 10-minute
binary-option market with 1.8x payout (+4U on win, -5U on loss for 5U stake)
and a 10-bar lockout. Reports trades / WR / Wilson LB / PnL / MDD / Calmar.

This is the *operational* PnL backtest -- the freqtrade `backtesting`
sub-command uses linear-futures + fees and is not appropriate for a binary
option payoff.

Changelog (2026-05-23):
  - FIX-11: preserve UTC timezone in monthly grouping (was: dt.to_period drops tz)
  - ATR now uses True Range + Wilder's smoothing ( matches strategy.py )

Run:
    .venv\\Scripts\\python -u user_data/notebooks/binary_option_backtest.py
"""
import math
import warnings
import importlib.util
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)

FEATHER_1M = "user_data/data/binance/futures/BTC_USDT_USDT-1m-futures.feather"
STRATEGY_PATH = "user_data/strategies/MeanReversion10mStrategy.py"
EXPIRY_BARS = 10
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


def import_strategy():
    spec = importlib.util.spec_from_file_location("strategy_module", STRATEGY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.MeanReversion10mStrategy


def load_1m():
    df = pd.read_feather(FEATHER_1M)
    df.columns = ['date', 'open', 'high', 'low', 'close', 'volume']
    df['date'] = pd.to_datetime(df['date'])
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce').astype(float)
    return df


def simulate(df, expiry=EXPIRY_BARS):
    """Sequential 10-min lockout, 1.8x binary payout."""
    enter_long = df['enter_long'].fillna(0).to_numpy().astype(np.int8)
    enter_short = df['enter_short'].fillna(0).to_numpy().astype(np.int8)
    close = df['close'].to_numpy()

    n = len(df)
    cd = 0
    rows = []  # (date, side, entry, exit_price, win)
    for i in range(n - expiry):
        if cd > 0:
            cd -= 1
            continue
        if enter_long[i] == 1 and enter_short[i] == 0:
            entry = close[i]; exit_p = close[i + expiry]
            win = int(exit_p > entry)
            rows.append((df['date'].iloc[i], 1, entry, exit_p, win))
            cd = expiry
        elif enter_short[i] == 1 and enter_long[i] == 0:
            entry = close[i]; exit_p = close[i + expiry]
            win = int(exit_p < entry)
            rows.append((df['date'].iloc[i], -1, entry, exit_p, win))
            cd = expiry
    return pd.DataFrame(rows, columns=['date', 'side', 'entry', 'exit', 'win'])


def summarize(trades_df):
    n = len(trades_df)
    if n == 0:
        return dict(n=0, wins=0, wr=0.0, wlb=0.0, pnl=0.0, mdd=0.0, max_consec_loss=0)
    wins = int(trades_df['win'].sum())
    pnl_per = np.where(trades_df['win'].to_numpy() == 1, PAYOUT_WIN, PAYOUT_LOSS)
    eq = np.cumsum(pnl_per)
    peak = np.maximum.accumulate(eq)
    mdd = float((peak - eq).max())
    consec = max_c = 0
    for w in trades_df['win'].to_numpy():
        if w == 0: consec += 1; max_c = max(max_c, consec)
        else: consec = 0
    return dict(n=n, wins=wins, wr=wins/n,
                wlb=wilson_lb(wins, n),
                pnl=float(eq[-1]),
                mdd=mdd, max_consec_loss=max_c)


def by_month(trades_df):
    if trades_df.empty:
        return pd.DataFrame()
    t = trades_df.copy()
    # FIX-11: keep UTC timezone when extracting month so boundary bars
    # (e.g. 2025-05-31 22:00 UTC = 2025-06-01 06:00 CST) are placed
    # in the correct calendar month.
    # FIX-10: keep UTC tz-aware when grouping by month so boundary bars
    # (e.g. 2025-05-31 22:00 UTC = 2025-06-01 06:00 CST) land in the correct month.
    t['month'] = t['date'].dt.tz_localize(None).dt.to_period('M')
    out = []
    for m, sub in t.groupby('month', sort=True):
        s = summarize(sub)
        out.append((str(m), s['n'], s['wr'], s['pnl'], s['mdd']))
    return pd.DataFrame(out, columns=['month', 'n', 'wr', 'pnl', 'mdd'])


def main():
    print("Importing strategy ...")
    StrategyCls = import_strategy()
    strategy = StrategyCls.__new__(StrategyCls)  # bypass __init__

    print("Loading 1m bars ...")
    df = load_1m()
    print(f"  bars={len(df):,} from {df['date'].iloc[0]} to {df['date'].iloc[-1]}")

    print("Computing indicators via strategy.populate_indicators ...")
    df = strategy.populate_indicators(df, {'pair': 'BTC/USDT:USDT'})
    df = strategy.populate_entry_trend(df, {'pair': 'BTC/USDT:USDT'})

    n_long = int(df['enter_long'].fillna(0).sum())
    n_short = int(df['enter_short'].fillna(0).sum())
    print(f"  raw signal candles: long={n_long}  short={n_short}")

    trades = simulate(df)
    s = summarize(trades)
    days = (df['date'].iloc[-1] - df['date'].iloc[0]).total_seconds() / 86400
    calmar = s['pnl'] / s['mdd'] if s['mdd'] > 0 else float('inf')

    print("\n" + "=" * 60)
    print(f"Binary-option backtest (1.8x payout, 5U stake, 10-min lockout)")
    print("=" * 60)
    print(f"  Span               : {days:.1f} days")
    print(f"  Trades             : {s['n']}  ({s['n']/days:.2f}/day)")
    print(f"  Long / Short       : {(trades['side']==1).sum()} / {(trades['side']==-1).sum()}")
    print(f"  Wins / Losses      : {s['wins']} / {s['n']-s['wins']}")
    print(f"  Win rate           : {s['wr']*100:.2f}%")
    print(f"  Wilson 95% lb      : {s['wlb']*100:.2f}%")
    print(f"  Total PnL          : {s['pnl']:+.2f} U  (= ${s['pnl']:.2f})")
    print(f"  Max drawdown       : {s['mdd']:.2f} U")
    print(f"  Max consec losses  : {s['max_consec_loss']}")
    print(f"  Calmar (PnL / MDD) : {calmar:.2f}")
    print(f"  Per-trade EV       : {s['pnl']/s['n']:+.4f} U  (vs 5U stake)")

    print("\nMonthly breakdown:")
    mb = by_month(trades)
    if not mb.empty:
        print(f"  {'month':<8}{'n':>6}{'WR':>8}{'PnL':>10}{'MDD':>9}")
        for _, r in mb.iterrows():
            print(f"  {r['month']:<8}{int(r['n']):>6}{r['wr']*100:>7.2f}%{r['pnl']:>9.2f}U{r['mdd']:>8.2f}U")


if __name__ == '__main__':
    main()
