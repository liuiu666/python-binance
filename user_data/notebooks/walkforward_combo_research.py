import math
import itertools
from collections import defaultdict
import pandas as pd
import numpy as np

FEATHER_PATH = "user_data/data/binance/futures/BTC_USDT_USDT-1m-futures.feather"
TF_MINUTES   = 2
EXPIRY_BARS  = 5
SEQ_LEN      = 6
PAYOUT_WIN   = 4.0
PAYOUT_LOSS  = -5.0

LIVE_CALL = {"000100_1_1","000000_1_1","011010_0_1","110100_1_1",
             "010110_0_1","011000_1_1","000011_1_1"}
LIVE_PUT  = {"111011_1_1","101001_0_1","100101_0_1","110101_1_0",
             "111001_1_0","110001_1_1"}

def wilson_lb(k, n, z=1.96):
    if n == 0: return 0.0
    p = k / n
    denom = 1 + z*z/n
    centre = p + z*z/(2*n)
    margin = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n))
    return (centre - margin) / denom

def combo_id_to_str(cid):
    r = cid & 1
    v = (cid >> 1) & 1
    seq = (cid >> 2) & ((1 << SEQ_LEN) - 1)
    bits = [(seq >> i) & 1 for i in reversed(range(SEQ_LEN))]
    return "".join(str(b) for b in bits) + f"_{v}_{r}"

def load_2m():
    df = pd.read_feather(FEATHER_PATH)
    df.columns = ['date','open','high','low','close','volume']
    df['date'] = pd.to_datetime(df['date'])
    for c in ['open','high','low','close','volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce').astype(float)
    df = df.set_index('date').resample(f'{TF_MINUTES}min').agg({
        'open':'first','high':'max','low':'min','close':'last','volume':'sum'
    }).dropna().reset_index()
    return df

def build_features(df2, rolling_win, vol_mult, range_mult):
    out = df2.copy()
    out['dir'] = (out['close'] > out['open']).astype(np.int8)
    vol_med = out['volume'].rolling(rolling_win).median()
    out['vol_high'] = (out['volume'] > vol_med * vol_mult).astype(np.int8)
    rng = out['high'] - out['low']
    rng_mean = rng.rolling(rolling_win).mean()
    out['large_range'] = (rng > rng_mean * range_mult).astype(np.int8)

    dir_arr = out['dir'].to_numpy()
    seq = np.zeros(len(out), dtype=np.int32)
    for i in range(SEQ_LEN):
        if i == 0:
            shifted = dir_arr.copy()
        else:
            shifted = np.zeros_like(dir_arr)
            shifted[i:] = dir_arr[:-i]
        seq |= (shifted.astype(np.int32) << i)
    combo = (seq.astype(np.int64) << 2) \
            | (out['vol_high'].to_numpy().astype(np.int64) << 1) \
            | out['large_range'].to_numpy().astype(np.int64)
    out['combo_id'] = combo
    out['exit_close'] = out['close'].shift(-EXPIRY_BARS)
    out['fwd_ret'] = (out['exit_close'] - out['close']) / out['close']
    out['target_call'] = (out['fwd_ret'] > 0).astype(np.int8)
    out['target_put']  = (out['fwd_ret'] < 0).astype(np.int8)
    valid = out['vol_high'].notna() & out['fwd_ret'].notna()
    return out.loc[valid].reset_index(drop=True)

def discover_wilson(train, wilson_thr, min_trades):
    g = train.groupby('combo_id').agg(
        n=('target_call','size'),
        cw=('target_call','sum'),
        pw=('target_put','sum'),
    )
    g = g[g['n'] >= min_trades]
    if len(g) == 0:
        return set(), set()
    call_lb = np.array([wilson_lb(int(w), int(n)) for w,n in zip(g['cw'], g['n'])])
    put_lb  = np.array([wilson_lb(int(w), int(n)) for w,n in zip(g['pw'], g['n'])])
    call_ids = set(g.index[call_lb >= wilson_thr].tolist())
    put_ids  = set(g.index[put_lb  >= wilson_thr].tolist())
    return call_ids, put_ids

def simulate_no_overlap(df, call_ids, put_ids):
    combo = df['combo_id'].to_numpy()
    tc = df['target_call'].to_numpy()
    tp = df['target_put'].to_numpy()
    n = len(df); cd = 0
    types, results, combos = [], [], []
    for i in range(n):
        if cd > 0:
            cd -= 1; continue
        cid = int(combo[i])
        in_c = cid in call_ids; in_p = cid in put_ids
        if in_c and in_p: continue
        if in_c:
            types.append(1); results.append(int(tc[i])); combos.append(cid); cd = EXPIRY_BARS
        elif in_p:
            types.append(-1); results.append(int(tp[i])); combos.append(cid); cd = EXPIRY_BARS
    return types, results, combos

def summarize(results):
    if not results:
        return dict(n=0, wins=0, wr=0.0, pnl=0.0, mdd=0.0, max_consec_loss=0)
    arr = np.array(results); wins = int(arr.sum()); n_tr = len(arr)
    pnl = np.where(arr == 1, PAYOUT_WIN, PAYOUT_LOSS)
    eq = np.cumsum(pnl)
    peak = np.maximum.accumulate(eq); mdd = float((peak - eq).max())
    consec = max_c = 0
    for w in arr:
        if w == 0: consec += 1; max_c = max(max_c, consec)
        else: consec = 0
    return dict(n=n_tr, wins=wins, wr=wins/n_tr, pnl=float(eq[-1]),
                mdd=mdd, max_consec_loss=max_c)

def expanding_walkforward(feat, warmup_bars, refit_bars, wilson_thr, min_trades):
    n = len(feat)
    fold_idx = 0
    fold_call_hits = defaultdict(int)
    fold_put_hits  = defaultdict(int)
    fold_count = 0
    all_types, all_results, all_combos = [], [], []
    fold_summaries = []
    start = warmup_bars
    while start + refit_bars <= n:
        train = feat.iloc[:start]
        test  = feat.iloc[start:start+refit_bars]
        c_ids, p_ids = discover_wilson(train, wilson_thr, min_trades)
        for cid in c_ids: fold_call_hits[cid] += 1
        for cid in p_ids: fold_put_hits[cid]  += 1
        fold_count += 1
        types, results, combos = simulate_no_overlap(test, c_ids, p_ids)
        s = summarize(results)
        s['fold'] = fold_idx; s['n_call_combos'] = len(c_ids); s['n_put_combos'] = len(p_ids)
        s['start_date'] = test['date'].iloc[0] if len(test) else None
        fold_summaries.append(s)
        all_types += types; all_results += results
        all_combos += combos
        start += refit_bars
        fold_idx += 1
    agg = summarize(all_results)
    return agg, fold_summaries, fold_count, fold_call_hits, fold_put_hits, all_combos, all_types, all_results

def per_combo_oos(all_combos, all_dirs, all_results):
    rows = defaultdict(lambda: [0,0])
    for cid, d, r in zip(all_combos, all_dirs, all_results):
        rows[(cid, d)][0] += 1
        rows[(cid, d)][1] += r
    out = []
    for (cid, d), (n, w) in rows.items():
        wr = w / n if n else 0.0
        pnl = w*PAYOUT_WIN + (n-w)*PAYOUT_LOSS
        out.append(dict(cid=cid, dir=d, n=n, w=w, l=n-w, wr=wr, pnl=pnl))
    return out

def main():
    print("Loading 2m bars ...")
    df2 = load_2m()
    days_total = (df2['date'].iloc[-1] - df2['date'].iloc[0]).total_seconds() / 86400
    print(f"  bars={len(df2):,}  days={days_total:.1f}")

    BARS_PER_DAY = (60 // TF_MINUTES) * 24
    WARMUP_DAYS  = 30
    REFIT_DAYS   = 14
    warmup_bars  = WARMUP_DAYS * BARS_PER_DAY
    refit_bars   = REFIT_DAYS  * BARS_PER_DAY

    print("\n=== Param sweep (expanding walk-forward, Wilson LB filter) ===")
    sweep = []
    for rw, vm, rm, wlb, mt in itertools.product(
            [15, 20, 30],
            [1.2, 1.4, 1.6],
            [1.2, 1.4, 1.6],
            [0.50, 0.52, 0.54],
            [50, 80]):
        feat = build_features(df2, rw, vm, rm)
        if len(feat) < warmup_bars + refit_bars:
            continue
        agg, folds, nf, ch, ph, *_ = expanding_walkforward(
            feat, warmup_bars, refit_bars, wlb, mt)
        sweep.append(dict(rw=rw, vm=vm, rm=rm, wlb=wlb, mt=mt,
                          n=agg['n'], wr=agg['wr'], pnl=agg['pnl'],
                          mdd=agg['mdd'], folds=nf,
                          avg_call=np.mean([f['n_call_combos'] for f in folds]) if folds else 0,
                          avg_put =np.mean([f['n_put_combos']  for f in folds]) if folds else 0))
    sweep.sort(key=lambda r: r['pnl'], reverse=True)
    print(f"{'rw':>3}{'vm':>5}{'rm':>5}{'wlb':>6}{'mt':>4}{'folds':>6}"
          f"{'OOS_n':>7}{'WR':>8}{'PnL(U)':>10}{'MDD':>8}{'avgC':>6}{'avgP':>6}")
    for r in sweep[:20]:
        print(f"{r['rw']:>3}{r['vm']:>5.1f}{r['rm']:>5.1f}{r['wlb']:>6.2f}"
              f"{r['mt']:>4}{r['folds']:>6}{r['n']:>7}{r['wr']*100:>7.2f}%"
              f"{r['pnl']:>9.2f}U{r['mdd']:>7.2f}U"
              f"{r['avg_call']:>6.1f}{r['avg_put']:>6.1f}")

    if not sweep:
        print("Sweep produced no valid results.")
        return

    best = sweep[0]
    print(f"\n=== Best config: rw={best['rw']} vm={best['vm']} rm={best['rm']} "
          f"wlb={best['wlb']} mt={best['mt']} ===")

    feat = build_features(df2, best['rw'], best['vm'], best['rm'])
    agg, folds, nf, call_hits, put_hits, all_combos, all_dirs, all_results = \
        expanding_walkforward(feat, warmup_bars, refit_bars, best['wlb'], best['mt'])

    print(f"Folds: {nf}  Aggregated OOS:")
    print(f"  trades={agg['n']}  WR={agg['wr']*100:.2f}%  "
          f"PnL(5U,1.8x)={agg['pnl']:+.2f}U  MDD={agg['mdd']:.2f}U  "
          f"MaxConsecLoss={agg['max_consec_loss']}")

    print(f"\nPer-fold OOS:")
    print(f"{'fold':>4}{'date':>22}{'nC':>4}{'nP':>4}{'n':>5}{'WR':>8}{'PnL':>9}")
    for f in folds:
        d = f['start_date']
        ds = pd.Timestamp(d).strftime('%Y-%m-%d %H:%M') if d is not None else '-'
        print(f"{f['fold']:>4}{ds:>22}{f['n_call_combos']:>4}{f['n_put_combos']:>4}"
              f"{f['n']:>5}{f['wr']*100:>7.2f}%{f['pnl']:>8.2f}U")

    print(f"\n=== Per-combo OOS (best config) ===")
    rows = per_combo_oos(all_combos, all_dirs, all_results)
    for r in rows:
        cid = r['cid']
        d = 'CALL' if r['dir'] == 1 else 'PUT'
        hits = call_hits[cid] if r['dir']==1 else put_hits[cid]
        r['hits'] = hits; r['dir_str'] = d
    rows.sort(key=lambda r: r['pnl'], reverse=True)
    print(f"{'Dir':<5}{'Combo':<14}{'folds':>6}{'N':>5}{'W':>5}{'L':>5}"
          f"{'WR':>8}{'PnL':>9}  {'inLive'}")
    STABLE_OOS_WR = 0.55
    MIN_FOLD_HITS = max(2, nf // 4)
    proposed_call, proposed_put = [], []
    for r in rows:
        s = combo_id_to_str(r['cid'])
        in_live = s in (LIVE_CALL if r['dir']==1 else LIVE_PUT)
        print(f"{r['dir_str']:<5}{s:<14}{r['hits']:>6}{r['n']:>5}{r['w']:>5}{r['l']:>5}"
              f"{r['wr']*100:>7.2f}%{r['pnl']:>8.2f}U  {'Y' if in_live else '-'}")
        if r['hits'] >= MIN_FOLD_HITS and r['wr'] >= STABLE_OOS_WR and r['n'] >= 30:
            (proposed_call if r['dir']==1 else proposed_put).append(s)

    print(f"\n=== Proposed STABLE combos (hits>={MIN_FOLD_HITS}, OOS_WR>={STABLE_OOS_WR}, N>=30) ===")
    print("CALL:", proposed_call)
    print("PUT :", proposed_put)
    print("\nLive CALL diff: dropped =", sorted(LIVE_CALL - set(proposed_call)),
          " new =", sorted(set(proposed_call) - LIVE_CALL))
    print("Live PUT  diff: dropped =", sorted(LIVE_PUT - set(proposed_put)),
          " new =", sorted(set(proposed_put) - LIVE_PUT))

if __name__ == '__main__':
    main()
