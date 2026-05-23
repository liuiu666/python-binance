"""Dynamic position sizing on top of A3 walk-forward.

Tests several stake schemes on the same A3 trade stream:
  S0  Flat 5U   (current baseline)
  S1  Kelly       stake_pct = (WR*b - (1-WR))/b   (b=0.8 for 1.8x payout)
  S2  Half Kelly
  S3  Quarter Kelly
  S4  Linear ramp: stake = base * clip(2*(rolling_WR - 0.55), 0, 2)
  S5  Step rule: <55% WR -> 0.5x, 55-60% -> 1x, >60% -> 1.5x

All sizing uses a rolling LOOKBACK trade WR (e.g., last 100 trades) and
compounds capital geometrically. Compares: bankroll growth, MDD, Calmar.
"""
import math
import numpy as np
import pandas as pd
import warnings
from collections import defaultdict

warnings.filterwarnings("ignore")

FEATHER_1M = "user_data/data/binance/futures/BTC_USDT_USDT-1m-futures.feather"
EXPIRY = 10
PAYOUT_MULT = 0.8  # 1.8x payout: win +0.8*stake, lose -1*stake
WARMUP_DAYS = 30
REFIT_DAYS = 14
BARS_PER_DAY = 60 * 24

BASE_STAKE = 5.0       # absolute U
START_BANK = 1000.0    # starting USD
LOOKBACK = 100         # rolling trade count for WR estimate
MAX_STAKE_PCT = 0.10   # cap at 10% of bankroll per trade


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

    out['exit_close'] = out['close'].shift(-EXPIRY)
    out['fwd_ret'] = (out['exit_close'] - out['close']) / out['close']
    out['target_call'] = (out['fwd_ret'] > 0).astype(np.int8)
    out['target_put'] = (out['fwd_ret'] < 0).astype(np.int8)
    return out


def gen_baseline_trades(feat):
    """Walk-forward A3 to get a clean OOS trade stream."""
    warmup = WARMUP_DAYS * BARS_PER_DAY
    refit = REFIT_DAYS * BARS_PER_DAY
    trades = []  # list of (date, side, win)
    start = warmup
    n = len(feat)
    while start + refit <= n:
        train = feat.iloc[:start]
        test = feat.iloc[start:start + refit]
        s = train['ema120_dev'].dropna()
        q10, q90 = np.quantile(s, [0.10, 0.90])
        ev = test['ema120_dev'].to_numpy()
        vz = test['vol_z40'].fillna(0).to_numpy()
        rz = test['rv_z'].fillna(0).to_numpy()
        flt = (vz > 1.0) & (rz > -1.0) & (rz < 1.0)
        sc = (ev <= q10) & flt
        sp = (ev >= q90) & flt
        tc = test['target_call'].to_numpy()
        tp = test['target_put'].to_numpy()
        cd = 0
        for i in range(len(test)):
            if cd > 0: cd -= 1; continue
            if sc[i] and sp[i]: continue
            if sc[i]:
                trades.append((test['date'].iloc[i], 1, int(tc[i]))); cd = EXPIRY
            elif sp[i]:
                trades.append((test['date'].iloc[i], -1, int(tp[i]))); cd = EXPIRY
        start += refit
    return trades


# ------------ Sizing schemes ------------

def flat_stake(*_):
    return BASE_STAKE

def kelly_stake(bank, rolling_wr, fraction=1.0):
    b = PAYOUT_MULT
    if rolling_wr is None:
        return BASE_STAKE
    k = (rolling_wr * b - (1 - rolling_wr)) / b
    k = max(0, min(MAX_STAKE_PCT, k * fraction))
    stake = bank * k
    return min(stake, bank)  # never bet more than bankroll

def linear_ramp(bank, rolling_wr):
    if rolling_wr is None:
        return BASE_STAKE
    mult = np.clip(2 * (rolling_wr - 0.55), 0.0, 2.0)  # WR 55% -> 0x, 60% -> 1x, 65% -> 2x
    return BASE_STAKE * mult

def step_rule(bank, rolling_wr):
    if rolling_wr is None:
        return BASE_STAKE
    if rolling_wr < 0.55:
        return BASE_STAKE * 0.5
    if rolling_wr < 0.60:
        return BASE_STAKE * 1.0
    return BASE_STAKE * 1.5


def simulate(trades, sizing_fn, name):
    bank = START_BANK
    eq_curve = [START_BANK]
    win_hist = []  # rolling window of wins (0/1)
    n_trades = 0
    n_wins = 0
    skipped = 0
    for (_d, _side, win) in trades:
        if len(win_hist) >= LOOKBACK:
            rwr = sum(win_hist[-LOOKBACK:]) / LOOKBACK
        else:
            rwr = None  # use base stake until lookback warms up
        stake = sizing_fn(bank, rwr)
        if stake <= 0:
            skipped += 1
            win_hist.append(win)
            eq_curve.append(bank)
            continue
        if win == 1:
            bank += stake * PAYOUT_MULT
            n_wins += 1
        else:
            bank -= stake
        n_trades += 1
        win_hist.append(win)
        eq_curve.append(bank)
        if bank <= 0:
            break
    eq = np.array(eq_curve)
    peak = np.maximum.accumulate(eq)
    mdd_abs = float((peak - eq).max())
    mdd_pct = float(((peak - eq) / peak).max() * 100)
    wr = n_wins / n_trades if n_trades else 0.0
    return dict(
        name=name, n=n_trades, skipped=skipped, wins=n_wins, wr=wr,
        final=bank, pnl=bank - START_BANK,
        ret_pct=(bank / START_BANK - 1) * 100,
        mdd_abs=mdd_abs, mdd_pct=mdd_pct,
        calmar=(bank - START_BANK) / mdd_abs if mdd_abs > 0 else float('inf'),
    )


def main():
    print("Loading + features ...")
    feat = build_features(load_1m()).dropna(subset=['ema120_dev', 'fwd_ret']).reset_index(drop=True)
    print(f"  bars={len(feat):,}")

    print("\nGenerating A3 walk-forward trade stream ...")
    trades = gen_baseline_trades(feat)
    print(f"  trades={len(trades):,}")

    schemes = [
        ('S0  flat 5U', flat_stake),
        ('S1  Kelly (full)', lambda b, w: kelly_stake(b, w, 1.0)),
        ('S2  Half Kelly', lambda b, w: kelly_stake(b, w, 0.5)),
        ('S3  Quarter Kelly', lambda b, w: kelly_stake(b, w, 0.25)),
        ('S4  Linear ramp (WR 55-65%)', linear_ramp),
        ('S5  Step rule (<55/55-60/>60)', step_rule),
    ]
    print(f"\n{'scheme':<32}{'n':>6}{'skip':>6}{'WR':>8}"
          f"{'final $':>12}{'PnL $':>10}{'ret%':>8}{'MDD $':>10}{'MDD%':>8}{'Calmar':>8}")
    print("-" * 110)
    for name, fn in schemes:
        r = simulate(trades, fn, name)
        print(f"{r['name']:<32}{r['n']:>6}{r['skipped']:>6}{r['wr']*100:>7.2f}%"
              f"{r['final']:>11.2f}{r['pnl']:>+10.2f}{r['ret_pct']:>+7.2f}%"
              f"{r['mdd_abs']:>9.2f}{r['mdd_pct']:>7.2f}%{r['calmar']:>8.2f}")


if __name__ == '__main__':
    main()
