"""Diagnose 2026-03 underperformance.

Re-runs the strategy and slices March 2026 by:
  * side (CALL vs PUT)
  * trend bucket
  * vol-regime bucket
  * daily bias
  * which intraday hours
to identify what regime broke the strategy that month.
"""
import math
import importlib.util
import numpy as np
import pandas as pd

FEATHER_1M = "user_data/data/binance/futures/BTC_USDT_USDT-1m-futures.feather"
EXPIRY = 10
PAYOUT_WIN, PAYOUT_LOSS = 4.0, -5.0


def import_strategy():
    spec = importlib.util.spec_from_file_location(
        "s", "user_data/strategies/MeanReversion10mStrategy.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m.MeanReversion10mStrategy


def main():
    strat = import_strategy().__new__(import_strategy())
    df = pd.read_feather(FEATHER_1M)
    df.columns = ['date','open','high','low','close','volume']
    df['date'] = pd.to_datetime(df['date'])
    for c in ['open','high','low','close','volume']:
        df[c] = pd.to_numeric(df[c]).astype(float)

    # Add regime indicators (same recipe as regime_conditional_research.py)
    rng = df['high'] - df['low']
    atr240 = rng.rolling(240).mean()
    ema240 = df['close'].ewm(span=240, adjust=False).mean()
    ema720 = df['close'].ewm(span=720, adjust=False).mean()
    df['trend_strength'] = (ema240 - ema720).abs() / atr240
    df['logret1'] = np.log(df['close']).diff()
    df['daily_ret'] = df['close'].pct_change(60*24)
    df['daily_bias'] = np.sign(df['daily_ret']).fillna(0).astype(int)
    df['hour'] = df['date'].dt.hour

    df = strat.populate_indicators(df, {'pair':'BTC/USDT:USDT'})
    df = strat.populate_entry_trend(df, {'pair':'BTC/USDT:USDT'})

    # Generate trades with 10-bar lockout
    n = len(df)
    el = df['enter_long'].fillna(0).to_numpy().astype(np.int8)
    es = df['enter_short'].fillna(0).to_numpy().astype(np.int8)
    close = df['close'].to_numpy()
    cd = 0; rows = []
    for i in range(n - EXPIRY):
        if cd > 0: cd -= 1; continue
        if el[i] and not es[i]:
            rows.append((df['date'].iloc[i], 1, close[i], close[i+EXPIRY],
                         int(close[i+EXPIRY] > close[i]),
                         df['hour'].iloc[i], df['trend_strength'].iloc[i],
                         df['daily_bias'].iloc[i], df['rv_z'].iloc[i]))
            cd = EXPIRY
        elif es[i] and not el[i]:
            rows.append((df['date'].iloc[i], -1, close[i], close[i+EXPIRY],
                         int(close[i+EXPIRY] < close[i]),
                         df['hour'].iloc[i], df['trend_strength'].iloc[i],
                         df['daily_bias'].iloc[i], df['rv_z'].iloc[i]))
            cd = EXPIRY
    t = pd.DataFrame(rows, columns=['date','side','entry','exit','win','hour','trend','bias','rv_z'])

    print(f"Total trades: {len(t)}")
    t['month'] = t['date'].dt.to_period('M').astype(str)

    # March 2026 isolation
    mar = t[t['month'] == '2026-03'].copy()
    mar_pnl = mar['win'].apply(lambda w: PAYOUT_WIN if w==1 else PAYOUT_LOSS)
    print(f"\n=== 2026-03 ({len(mar)} trades) ===")
    print(f"Overall: WR={mar['win'].mean()*100:.2f}% PnL={mar_pnl.sum():+.2f}U")

    # By side
    print("\nBy side:")
    for s in [1, -1]:
        sub = mar[mar['side']==s]
        if len(sub)==0: continue
        pnl = sum(PAYOUT_WIN if w==1 else PAYOUT_LOSS for w in sub['win'])
        print(f"  {'CALL' if s==1 else 'PUT '}: n={len(sub):>4} WR={sub['win'].mean()*100:5.2f}% PnL={pnl:+7.2f}U")

    # By daily_bias
    print("\nBy daily_bias x side:")
    for b in [-1, 0, 1]:
        for s in [1, -1]:
            sub = mar[(mar['bias']==b) & (mar['side']==s)]
            if len(sub) < 10: continue
            pnl = sum(PAYOUT_WIN if w==1 else PAYOUT_LOSS for w in sub['win'])
            print(f"  bias={b:>2} {'CALL' if s==1 else 'PUT '}: n={len(sub):>4} WR={sub['win'].mean()*100:5.2f}% PnL={pnl:+7.2f}U")

    # By trend tercile (computed within March)
    if mar['trend'].dropna().size > 30:
        q33, q67 = np.quantile(mar['trend'].dropna(), [1/3, 2/3])
        mar['tb'] = pd.cut(mar['trend'], [-np.inf, q33, q67, np.inf], labels=[0,1,2])
        print("\nBy trend tercile x side:")
        for tb in [0,1,2]:
            for s in [1, -1]:
                sub = mar[(mar['tb']==tb) & (mar['side']==s)]
                if len(sub) < 10: continue
                pnl = sum(PAYOUT_WIN if w==1 else PAYOUT_LOSS for w in sub['win'])
                print(f"  tr={tb} {'CALL' if s==1 else 'PUT '}: n={len(sub):>4} WR={sub['win'].mean()*100:5.2f}% PnL={pnl:+7.2f}U")

    # March vs neighboring months
    print("\nNeighboring month comparison:")
    print(f"{'month':>8}{'n':>5}{'WR_C':>7}{'WR_P':>7}{'PnL_C':>9}{'PnL_P':>9}{'BTC_chg':>10}")
    for m in ['2026-01','2026-02','2026-03','2026-04']:
        sub = t[t['month']==m]
        if len(sub)==0: continue
        c = sub[sub['side']==1]; p = sub[sub['side']==-1]
        c_pnl = sum(PAYOUT_WIN if w==1 else PAYOUT_LOSS for w in c['win'])
        p_pnl = sum(PAYOUT_WIN if w==1 else PAYOUT_LOSS for w in p['win'])
        # BTC change in that month
        first_close = df.loc[df['date'].dt.to_period('M').astype(str)==m, 'close'].iloc[0]
        last_close  = df.loc[df['date'].dt.to_period('M').astype(str)==m, 'close'].iloc[-1]
        chg = (last_close/first_close - 1) * 100
        print(f"{m:>8}{len(sub):>5}"
              f"{c['win'].mean()*100:>6.2f}%{p['win'].mean()*100:>6.2f}%"
              f"{c_pnl:>8.2f}U{p_pnl:>8.2f}U{chg:>9.2f}%")


if __name__ == '__main__':
    main()
