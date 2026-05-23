"""Show the frequency vs WR trade-off curve.

For each parameter combination, run a 24-fold walk-forward A3 simulation
and report total trades, WR, Wilson LB, PnL@5U.
"""
import math
import numpy as np
import pandas as pd
import warnings

warnings.filterwarnings("ignore")

FEATHER_1M = "user_data/data/binance/futures/BTC_USDT_USDT-1m-futures.feather"
EXPIRY = 10
PAYOUT_WIN, PAYOUT_LOSS = 4.0, -5.0
WARMUP_DAYS = 30
REFIT_DAYS = 14
BARS_PER_DAY = 60 * 24


def wilson_lb(k, n, z=1.96):
    if n == 0: return 0.0
    p = k / n
    return ((p + z*z/(2*n) - z*math.sqrt(p*(1-p)/n + z*z/(4*n*n)))
            / (1 + z*z/n))


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
    out['rv_z'] = (rv60 - rv60.rolling(60*24).mean()) / rv60.rolling(60*24).std()
    out['exit_close'] = out['close'].shift(-EXPIRY)
    out['fwd_ret'] = (out['exit_close'] - out['close']) / out['close']
    out['target_call'] = (out['fwd_ret'] > 0).astype(np.int8)
    out['target_put'] = (out['fwd_ret'] < 0).astype(np.int8)
    return out


def run(feat, quantile_low, vol_z_min, rv_z_band, name):
    warmup = WARMUP_DAYS * BARS_PER_DAY
    refit = REFIT_DAYS * BARS_PER_DAY
    all_wins = []
    start = warmup
    while start + refit <= len(feat):
        train = feat.iloc[:start]
        test = feat.iloc[start:start+refit]
        s = train['ema120_dev'].dropna()
        q_lo, q_hi = np.quantile(s, [quantile_low, 1-quantile_low])
        ev = test['ema120_dev'].to_numpy()
        vz = test['vol_z40'].fillna(0).to_numpy()
        rz = test['rv_z'].fillna(0).to_numpy()
        flt = vz > vol_z_min
        if rv_z_band is not None:
            flt = flt & (rz > -rv_z_band) & (rz < rv_z_band)
        sc = (ev <= q_lo) & flt
        sp = (ev >= q_hi) & flt
        tc = test['target_call'].to_numpy()
        tp = test['target_put'].to_numpy()
        cd = 0
        for i in range(len(test)):
            if cd > 0: cd -= 1; continue
            if sc[i] and sp[i]: continue
            if sc[i]: all_wins.append(int(tc[i])); cd = EXPIRY
            elif sp[i]: all_wins.append(int(tp[i])); cd = EXPIRY
        start += refit
    n = len(all_wins)
    if n == 0:
        return (name, 0, 0, 0, 0, 0, 0)
    wins = sum(all_wins)
    wr = wins / n
    wlb = wilson_lb(wins, n)
    pnl = wins*PAYOUT_WIN + (n-wins)*PAYOUT_LOSS
    arr = np.array(all_wins)
    eq = np.cumsum(np.where(arr==1, PAYOUT_WIN, PAYOUT_LOSS))
    mdd = float((np.maximum.accumulate(eq) - eq).max())
    days = (feat['date'].iloc[-1] - feat['date'].iloc[WARMUP_DAYS*BARS_PER_DAY]).total_seconds() / 86400
    per_day = n / days
    return (name, n, per_day, wr, wlb, pnl, mdd)


def main():
    print("Loading + features ...")
    df = pd.read_feather(FEATHER_1M)
    df.columns = ['date','open','high','low','close','volume']
    df['date'] = pd.to_datetime(df['date'])
    for c in ['open','high','low','close','volume']:
        df[c] = pd.to_numeric(df[c]).astype(float)
    feat = build_features(df).dropna(subset=['ema120_dev','fwd_ret']).reset_index(drop=True)
    print(f"  {len(feat):,} bars")

    BE = 5/9  # 1.8x breakeven = 55.56%
    print(f"\n{'profile':<42}{'n':>7}{'笔/天':>8}{'WR':>8}{'WLB':>8}{'PnL':>9}{'MDD':>8}{'>BE?'}")
    print("-" * 100)

    configs = [
        # (quantile_low, vol_z_min, rv_z_band, label)
        (0.10, 1.0,  1.0, "A3 当前 (q10/90, vol_z>1, rv_z bd 1)"),
        (0.10, 1.0,  None, "无 rv_z 限制"),
        (0.10, 0.0,  1.0, "vol_z>0 (放宽量能门槛)"),
        (0.10, -10,  None, "全无过滤 (仅 ema 分位)"),
        (0.05, 1.0,  1.0, "更极端 ema_dev (q5/95)"),
        (0.15, 1.0,  1.0, "稍放宽 ema_dev (q15/85)"),
        (0.20, 1.0,  1.0, "更放宽 ema_dev (q20/80)"),
        (0.25, 1.0,  1.0, "最宽 ema_dev (q25/75)"),
        (0.30, 1.0,  1.0, "ema_dev q30/70"),
        (0.50, 1.0,  1.0, "无 ema_dev 限制 (q50)"),
        (0.20, -10,  None, "q20/80 + 无任何过滤"),
        (0.50, -10,  None, "无任何信号 (基准对照)"),
    ]
    rows = []
    for ql, vmin, rvb, lbl in configs:
        rows.append(run(feat, ql, vmin, rvb, lbl))
    for (lbl, n, pd_, wr, wlb, pnl, mdd) in rows:
        marker = "  ***" if wlb >= BE else ("   *" if wr >= BE else "")
        print(f"{lbl:<42}{n:>7}{pd_:>8.2f}{wr*100:>7.2f}%{wlb*100:>7.2f}%"
              f"{pnl:>+8.0f}U{mdd:>7.0f}U {marker}")

    print("\nBreakeven WR for 1.8x payout: 55.56%")
    print("Wilson LB *** = 95% confidence above breakeven")


if __name__ == '__main__':
    main()
