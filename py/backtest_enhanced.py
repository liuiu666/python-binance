"""
Enhanced Multi-Symbol Multi-Timeframe Backtester
=================================================
Symbols: BTC, ETH, SOL
Timeframes: 15min (3 bars), 30min (6 bars), 1hr (12 bars)
Data: K-line + funding rate + long/short ratio + taker buy/sell
Model: 3x XGBoost ensemble with agreement filter
"""
import pandas as pd, numpy as np, json, time, warnings, os
warnings.filterwarnings("ignore")
from xgboost import XGBClassifier

OUT = "E:/codex/data"

SYMBOLS = ["btcusdt"]  # solo BTC for fast baseline
HORIZONS = {"10min": 2, "15min": 3, "30min": 6, "1hr": 12}  # bars on 5m aggregation
LABELS = {"btcusdt": "BTC", "ethusdt": "ETH", "solusdt": "SOL"}

# ============ INDICATORS ============
def ema(a, p):
    r = np.empty(len(a)); r[0] = a[0]; k = 2/(p+1)
    for i in range(1, len(a)): r[i] = a[i]*k + r[i-1]*(1-k)
    return r

def sma(a, p):
    cs = np.cumsum(a); r = np.full(len(a), np.nan)
    r[p-1:] = (cs[p-1:] - np.concatenate([[0], cs[:-p]])) / p
    return r

def rsi(a, p):
    d = np.diff(a, prepend=a[0]); g = np.where(d>0,d,0); lo = np.where(d<0,-d,0)
    ag = np.empty(len(a)); al = np.empty(len(a))
    ag[0] = g[:p+1].mean(); al[0] = lo[:p+1].mean()
    for i in range(1, len(a)): ag[i]=(ag[i-1]*(p-1)+g[i])/p; al[i]=(al[i-1]*(p-1)+lo[i])/p
    return 100 - 100/(1+np.where(al>0, ag/al, 100))

# ============ LOAD & MERGE ============
def load_symbol(sym):
    """Load klines + funding + ls ratio + taker for a symbol."""
    # Klines
    kline_path = os.path.join(OUT, f"{sym}_1m.csv")
    if not os.path.exists(kline_path):
        print(f"  {sym}: no kline data, skipping")
        return None
    df = pd.read_csv(kline_path, parse_dates=["open_time"])
    df = df.sort_values("open_time").reset_index(drop=True)

    # Aggregate to 5min
    df["period"] = df["open_time"].dt.floor("5min")
    df5 = df.groupby("period").agg(
        open=("open","first"), high=("high","max"), low=("low","min"),
        close=("close","last"), volume=("volume","sum"),
    ).reset_index().rename(columns={"period":"time"})

    # Funding rate (merge by closest timestamp)
    fund_path = os.path.join(OUT, f"{sym}_funding.csv")
    if os.path.exists(fund_path):
        fund = pd.read_csv(fund_path)
        fund["fundingTime"] = pd.to_datetime(fund["fundingTime"], utc=True, format="ISO8601")
        fund = fund.sort_values("fundingTime")
        # Forward-fill funding rate to each 5m bar
        df5["time_utc"] = pd.to_datetime(df5["time"], utc=True)
        fund_idx = np.searchsorted(fund["fundingTime"].values, df5["time_utc"].values, side="right") - 1
        fund_idx = np.clip(fund_idx, 0, len(fund)-1)
        df5["funding_rate"] = fund["fundingRate"].values[fund_idx]
        df5["funding_rate"] = df5["funding_rate"].fillna(0)
        df5 = df5.drop(columns=["time_utc"])

    # Long/short ratio
    ls_path = os.path.join(OUT, f"{sym}_lsratio.csv")
    if os.path.exists(ls_path):
        ls = pd.read_csv(ls_path)
        ls["timestamp"] = pd.to_datetime(ls["timestamp"], utc=True, format="ISO8601")
        ls = ls.sort_values("timestamp")
        df5["time_utc"] = pd.to_datetime(df5["time"], utc=True)
        ls_idx = np.searchsorted(ls["timestamp"].values, df5["time_utc"].values, side="right") - 1
        ls_idx = np.clip(ls_idx, 0, len(ls)-1)
        df5["ls_ratio"] = ls["longShortRatio"].values[ls_idx]
        df5["ls_long"] = ls["longAccount"].values[ls_idx]
        df5["ls_short"] = ls["shortAccount"].values[ls_idx]
        for c in ["ls_ratio","ls_long","ls_short"]:
            df5[c] = df5[c].fillna(1.0 if c == "ls_ratio" else 0.5)
        df5 = df5.drop(columns=["time_utc"])

    # Taker buy/sell
    tk_path = os.path.join(OUT, f"{sym}_taker.csv")
    if os.path.exists(tk_path):
        tk = pd.read_csv(tk_path)
        tk["timestamp"] = pd.to_datetime(tk["timestamp"], utc=True, format="ISO8601")
        tk = tk.sort_values("timestamp")
        df5["time_utc"] = pd.to_datetime(df5["time"], utc=True)
        tk_idx = np.searchsorted(tk["timestamp"].values, df5["time_utc"].values, side="right") - 1
        tk_idx = np.clip(tk_idx, 0, len(tk)-1)
        df5["taker_ratio"] = tk["buySellRatio"].values[tk_idx]
        df5["taker_buy"] = tk["buyVol"].values[tk_idx]
        df5["taker_sell"] = tk["sellVol"].values[tk_idx]
        for c in ["taker_ratio","taker_buy","taker_sell"]:
            df5[c] = df5[c].fillna(1.0 if c == "taker_ratio" else 0)
        df5 = df5.drop(columns=["time_utc"])

    return df5

# ============ BUILD FEATURES ============
def build_features(df5, horizon=6):
    c = df5["close"].values.astype(np.float64)
    h = df5["high"].values.astype(np.float64)
    l = df5["low"].values.astype(np.float64)
    v = df5["volume"].values.astype(np.float64)
    o = df5["open"].values.astype(np.float64)
    n = len(df5)
    F = {}

    ret = np.diff(c, prepend=c[0]) / np.where(np.roll(c,1)>0, np.roll(c,1), 1)

    # Returns lags
    for p in range(1, 21):
        F[f"rl{p}"] = np.roll(ret, p)

    # EMAs + price ratios
    for p in [5,10,20,50,100,200]:
        e = ema(c, p)
        F[f"pre{p}"] = c/e - 1
        F[f"esl{p}"] = (e - np.roll(e,5)) / np.where(np.abs(np.roll(e,5))>0, np.abs(np.roll(e,5)), 1)

    # RSI
    for p in [5,14,21]:
        F[f"rsi{p}"] = rsi(c, p)

    # ROC
    for p in [2,5,10,20,40]:
        rc = np.roll(c, p)
        F[f"roc{p}"] = np.where(rc>0, (c-rc)/rc, 0)

    # MACD
    e12 = ema(c,12); e26 = ema(c,26); ml = e12-e26; ms = ema(ml,9); mh = ml-ms
    F["macd_h"] = mh
    F["macd_d"] = mh - np.roll(mh,1)
    F["macd_s5"] = np.array([mh[max(0,i-4):i+1].sum() for i in range(n)])

    # Bollinger
    bb_mid = sma(c,20); std20 = np.full(n, np.nan)
    for i in range(19,n): std20[i] = c[i-19:i+1].std()
    bbu = bb_mid + 2*std20; bbl = bb_mid - 2*std20
    F["bbp"] = np.where((bbu-bbl)>0, (c-bbl)/(bbu-bbl), 0.5)
    F["bbw"] = np.where(bb_mid>0, (bbu-bbl)/bb_mid, 0)

    # ATR
    tr = np.maximum(h-l, np.maximum(np.abs(h-np.roll(c,1)), np.abs(l-np.roll(c,1))))
    tr[0] = h[0]-l[0]
    atr = ema(tr,14)
    F["atrp"] = atr/c
    F["atr_exp"] = atr / np.where(ema(tr,50)>0, ema(tr,50), atr)

    # Volume
    vs = sma(v,20)
    F["vr"] = np.where(vs>0, v/vs, 1)

    # OBV
    obv = np.zeros(n)
    for i in range(1,n): obv[i] = obv[i-1] + (v[i] if c[i]>c[i-1] else (-v[i] if c[i]<c[i-1] else 0))
    oe = ema(obv,20)
    F["obv_sl"] = (obv-oe)/np.where(np.abs(oe)>0, np.abs(oe), 1)

    # Candle
    body = np.abs(c-o); full = np.maximum(h-l, 0.01)
    F["br"] = body/full
    F["bull"] = (c>o).astype(float)
    consec = np.zeros(n)
    for i in range(1,n):
        if (c[i]>o[i])==(c[i-1]>o[i-1]): consec[i] = consec[i-1]+1
    F["consec"] = consec

    # Momentum
    for p in [6,12,18,30]:
        rc = np.roll(c,p); F[f"mom_{p}"] = np.where(rc>0, (c-rc)/rc, 0)

    # High/low position
    for p in [10,20,50]:
        hp = np.array([h[max(0,i-p+1):i+1].max() for i in range(n)])
        lp = np.array([l[max(0,i-p+1):i+1].min() for i in range(n)])
        F[f"hlp{p}"] = np.where(hp!=lp, (c-lp)/(hp-lp), 0.5)
        F[f"rng{p}"] = (hp-lp)/c

    # Higher-timeframe context on the same 5m bars. These features let the
    # models distinguish short RSI extremes inside a larger trend from true
    # range-bound reversal setups.
    for name, p in [("1h", 12), ("4h", 48), ("24h", 288)]:
        prev = np.full(n, np.nan)
        prev[p:] = c[:-p]
        F[f"htf_ret_{name}"] = np.where(prev > 0, (c - prev) / prev, np.nan)

        hp = np.full(n, np.nan)
        lp = np.full(n, np.nan)
        for i in range(p - 1, n):
            hp[i] = h[i - p + 1:i + 1].max()
            lp[i] = l[i - p + 1:i + 1].min()
        F[f"htf_pos_{name}"] = np.where(hp != lp, (c - lp) / (hp - lp), np.nan)
        F[f"htf_rng_{name}"] = np.where(c > 0, (hp - lp) / c, np.nan)

    # Volatility regime
    for p in [10,20,50]:
        F[f"vreg{p}"] = np.full(n, np.nan)
        for i in range(p-1,n): F[f"vreg{p}"][i] = ret[i-p+1:i+1].std()

    # EMA stack
    ema_stack = np.zeros(n)
    e5=ema(c,5); e10_=ema(c,10); e20_=ema(c,20); e50_=ema(c,50)
    for i in range(1,n):
        if e5[i]>=e10_[i]>=e20_[i]>=e50_[i]: ema_stack[i]=1
        elif e5[i]<=e10_[i]<=e20_[i]<=e50_[i]: ema_stack[i]=-1
    F["ema_stack"] = ema_stack

    for p in [6,12,30]:
        rc = np.roll(c,p); F[f"trend{p}"] = np.where(rc>0, (c-rc)/rc, 0)

    # Time
    hours = pd.to_datetime(df5["time"]).dt.hour.values
    F["h_sin"] = np.sin(2*np.pi*hours/24)
    F["h_cos"] = np.cos(2*np.pi*hours/24)

    # ---- NEW: Market microstructure features ----
    # Funding rate features
    if "funding_rate" in df5.columns:
        fr = df5["funding_rate"].values.astype(np.float64)
        F["fund_rate"] = fr
        F["fund_rate_ema"] = ema(fr, 6)  # 30min average funding
        F["fund_zscore"] = (fr - sma(fr, 12)) / np.where(rolling_std(fr, 12) > 0, rolling_std(fr, 12), 1e-10)
        F["fund_signal"] = np.where(fr > 0.0005, -1, np.where(fr < -0.0005, 1, 0))  # extreme funding = contrarian

    # Long/short ratio features
    if "ls_ratio" in df5.columns:
        lsr = df5["ls_ratio"].values.astype(np.float64)
        F["ls_ratio"] = lsr
        F["ls_ratio_ema"] = ema(lsr, 6)
        F["ls_delta"] = lsr - np.roll(lsr, 6)
        F["ls_extreme"] = np.where(lsr > 1.3, -1, np.where(lsr < 0.7, 1, 0))  # extreme = contrarian
        F["ls_long"] = df5["ls_long"].values.astype(np.float64) if "ls_long" in df5.columns else np.full(n, 0.5)
        F["ls_short"] = df5["ls_short"].values.astype(np.float64) if "ls_short" in df5.columns else np.full(n, 0.5)

    # Taker buy/sell features
    if "taker_ratio" in df5.columns:
        tr_ = df5["taker_ratio"].values.astype(np.float64)
        F["taker_ratio"] = tr_
        F["taker_ratio_ema"] = ema(tr_, 6)
        F["taker_delta"] = tr_ - np.roll(tr_, 6)
        F["taker_signal"] = np.where(tr_ > 1.2, 1, np.where(tr_ < 0.8, -1, 0))  # heavy buying = bullish

    # TARGET
    future = np.roll(c, -horizon)
    F["target"] = np.where(future > c, 1, np.where(future < c, -1, 0))

    fdf = pd.DataFrame(F, index=df5.index)
    fdf["time"] = df5["time"].values
    return fdf.dropna().reset_index(drop=True)

def rolling_std(arr, p):
    r = np.full(len(arr), np.nan)
    for i in range(p-1, len(arr)): r[i] = arr[i-p+1:i+1].std()
    return r

def fcols(df):
    return [c for c in df.columns if c not in ["time","target"]]

# ============ WALK-FORWARD BACKTEST ============
def walk_forward(df, threshold, horizon, label="", rsi_filter=False):
    cols = fcols(df)
    df = df[df["target"]!=0].iloc[-8000:].reset_index(drop=True)
    if rsi_filter and "rsi14" in df.columns:
        df = df[(df["rsi14"] < 30) | (df["rsi14"] > 70)].reset_index(drop=True)
    dfc = df.copy()
    dfc["label"] = (dfc["target"]==1).astype(int)
    X = dfc[cols].values; y = dfc["label"].values

    train_size=4000; test_size=500; step=500
    results=[]; i=train_size
    while i+test_size <= len(X):
        Xtr,Xte = X[i-train_size:i], X[i:i+test_size]
        ytr,yte = y[i-train_size:i], y[i:i+test_size]
        models = [
            XGBClassifier(n_estimators=200,max_depth=4,learning_rate=0.05,subsample=0.7,
                colsample_bytree=0.6,reg_alpha=1.0,reg_lambda=2.0,min_child_weight=30,
                tree_method="hist",eval_metric="logloss",use_label_encoder=False,verbosity=0,random_state=42),
            XGBClassifier(n_estimators=250,max_depth=3,learning_rate=0.03,subsample=0.8,
                colsample_bytree=0.7,reg_alpha=0.5,reg_lambda=1.5,min_child_weight=25,
                tree_method="hist",eval_metric="logloss",use_label_encoder=False,verbosity=0,random_state=123),
            XGBClassifier(n_estimators=150,max_depth=5,learning_rate=0.1,subsample=0.6,
                colsample_bytree=0.5,reg_alpha=2.0,reg_lambda=3.0,min_child_weight=40,
                tree_method="hist",eval_metric="logloss",use_label_encoder=False,verbosity=0,random_state=7),
        ]
        probs = [m.fit(Xtr,ytr).predict_proba(Xte)[:,1] for m in models]
        avg_p = np.mean(probs, axis=0)
        votes = [(p>=0.5).astype(int) for p in probs]
        agree = (votes[0]==votes[1]) & (votes[1]==votes[2])
        hc = ((avg_p>=threshold)|(avg_p<=(1-threshold))) & agree
        if hc.sum() > 0:
            pf = avg_p[hc]; yf = yte[hc]
            preds = (pf>=0.5).astype(int)
            if len(yf) >= 1:
                results.extend([int(p==y) for p,y in zip(preds,yf)])
        i += step
        print(f"  {label} Window {i//500}: {len(results)} trades", end="\r")

    if not results:
        return None
    total = len(results)
    won = sum(results)
    wr = won/total*100
    pnl = won*100*0.85 - (total-won)*100
    ml=0;cl=0
    for r in results:
        if r==1: cl=0
        else: cl+=1; ml=max(ml,cl)
    print(f"  {label}: WR={wr:.1f}% | n={total} | PnL={pnl:.0f} | MaxLoss={ml}")
    return {"label":label,"wr":round(float(wr),2),"trades":total,"pnl":round(float(pnl),2),"max_loss":ml,"th":threshold}

# ============ MAIN ============
if __name__ == "__main__":
    t0 = time.time()
    print("="*60)
    print("Enhanced Multi-Symbol Multi-Timeframe Backtest")
    print("="*60)

    all_results = []

    for sym in SYMBOLS:
        print(f"\n{'='*60}")
        print(f"Loading {LABELS.get(sym, sym)}...")
        df5 = load_symbol(sym)
        if df5 is None:
            continue
        print(f"  {len(df5)} 5m candles, columns: {[c for c in df5.columns if c not in ['time','open','high','low','close','volume']]}")

        for tf_name, horizon in HORIZONS.items():
            print(f"\n--- {LABELS.get(sym,sym)} {tf_name} (h={horizon}) ---")
            fdf = build_features(df5, horizon)
            cols = fcols(fdf)
            print(f"  {len(fdf)} rows, {len(cols)} features ({time.time()-t0:.0f}s)")

            for th in [0.55, 0.60, 0.65, 0.70, 0.73, 0.75, 0.78, 0.80]:
                label = f"{LABELS.get(sym,sym)}_{tf_name}_th{int(th*100)}"
                r = walk_forward(fdf, th, horizon, label)
                if r and r["trades"] >= 5:
                    all_results.append(r)
                r2 = walk_forward(fdf, th, horizon, label+"_rsi", rsi_filter=True)
                if r2 and r2["trades"] >= 5:
                    all_results.append(r2)

    # Summary
    print(f"\n{'='*60}")
    print("RESULTS SUMMARY")
    print(f"{'='*60}")

    # Sort by win rate, filter profitable
    profitable = [r for r in all_results if r["pnl"] > 0]
    profitable.sort(key=lambda x: x["wr"], reverse=True)

    print(f"\nTotal configurations tested: {len(all_results)}")
    print(f"Profitable configurations: {len(profitable)}")
    print(f"\nTop 10 by win rate (profitable only):")
    print(f"{'Label':<30} {'WR%':>6} {'Trades':>7} {'PnL':>8} {'MaxLoss':>8}")
    print("-"*65)
    for r in profitable[:10]:
        print(f"{r['label']:<30} {r['wr']:>6.1f} {r['trades']:>7} {r['pnl']:>8.0f} {r['max_loss']:>8}")

    # Compute total potential if we combine top strategies
    print(f"\nCombined strategy potential (top from each symbol):")
    by_sym = {}
    for r in profitable:
        sym = r["label"].split("_")[0]
        if sym not in by_sym or r["wr"] > by_sym[sym]["wr"]:
            by_sym[sym] = r
    for sym, r in by_sym.items():
        print(f"  {sym}: {r['label']} WR={r['wr']}% n={r['trades']} PnL={r['pnl']}")

    total_trades = sum(r["trades"] for r in by_sym.values())
    total_pnl = sum(r["pnl"] for r in by_sym.values())
    avg_wr = np.mean([r["wr"] for r in by_sym.values()])
    print(f"\n  Combined: avg WR={avg_wr:.1f}% | total trades={total_trades} | total PnL={total_pnl:.0f}")
    print(f"  Daily avg: ~{total_trades/90:.1f} trades/day")

    with open(os.path.join(OUT, "enhanced_results.json"), "w") as f:
        json.dump({"top10": profitable[:10], "by_symbol": by_sym, "all": all_results}, f, indent=2, default=str)
    print(f"\nResults saved to {OUT}/enhanced_results.json")
    print(f"Total time: {time.time()-t0:.0f}s")
