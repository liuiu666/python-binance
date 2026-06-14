"""cd=10min fixed (max 1 trade per 10 min), sweep tail & window."""
import math
import pandas as pd

df = pd.read_csv("e:/python-binance/data/btcusdt_1m.csv")
df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
for c in ["open","high","low","close","volume"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")
df = df.dropna(subset=["open_time","close"]).sort_values("open_time").reset_index(drop=True)

close = df["close"].tolist()
days = (df["open_time"].iloc[-1] - df["open_time"].iloc[0]).total_seconds() / 86400
N = len(close)
PAYOUT = 0.80
H = 10  # horizon bars (10min)
cd = 10  # cooldown bars (10min)

def normal_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def run_bt(bar_min, tail, w, poc_thresh):
    # 聚合
    bars = []
    for i in range(0, N, bar_min):
        bars.append(close[min(i + bar_min - 1, N - 1)])
    n = len(bars)
    lr = [0.0] * n
    for i in range(1, n):
        lr[i] = math.log(bars[i] / bars[i-1]) if bars[i] > 0 and bars[i-1] > 0 else 0.0
    
    wins = losses = flats = 0
    pnl = cum_pnl = 0.0
    peak = 0.0; max_peak_dd = 0.0; min_pnl = 0.0
    last_sig = -999999
    max_win = max_loss = cur_win = cur_loss = 0
    
    for i in range(w, n - H):
        if i - last_sig < cd:
            continue
        
        s = sum(lr[i-w+1:i+1])
        s2 = sum(lr[k]**2 for k in range(i-w+1, i+1))
        mu = s / w
        var = max((s2/w) - mu*mu, 0.0)
        sigma = math.sqrt(var * w/(w-1)) if w > 1 else math.sqrt(var)
        if sigma < 1e-12:
            continue
        
        z = (H * mu) / (math.sqrt(H) * sigma)
        p_up = normal_cdf(z)
        
        if p_up <= tail:
            bet_up = True
        elif p_up >= poc_thresh:
            bet_up = False
        else:
            continue
        
        entry = bars[i]; exit_p = bars[i + H]
        if exit_p > entry:
            correct = bet_up
        elif exit_p < entry:
            correct = not bet_up
        else:
            flats += 1; continue
        
        last_sig = i
        if correct:
            wins += 1; cum_pnl += PAYOUT
            cur_win += 1; cur_loss = 0
        else:
            losses += 1; cum_pnl -= 1.0
            cur_loss += 1; cur_win = 0
        
        max_win = max(max_win, cur_win)
        max_loss = max(max_loss, cur_loss)
        min_pnl = min(min_pnl, cum_pnl)
        peak = max(peak, cum_pnl)
        max_peak_dd = max(max_peak_dd, peak - cum_pnl)
    
    total = wins + losses
    wr = wins/total*100 if total > 0 else 0
    dd_pct = max_peak_dd/peak*100 if peak > 0 else 999
    return {
        "trades": total, "wr": wr, "pnl": cum_pnl, "pnl_day": cum_pnl/days,
        "per_day": total/days, "max_dd": max_peak_dd, "dd_pct": dd_pct,
        "min_pnl": min_pnl, "max_win": max_win, "max_loss": max_loss,
        "peak": peak,
    }

tails = [0.15, 0.20, 0.25, 0.30, 0.35, 0.40]
windows = [30, 60, 90, 120, 180, 240]
bar_mins = [1, 2, 5]

print(f"Data: {days:.1f} days, {N} bars | PAYOUT=0.80 | horizon=10min | cooldown=10min")
print(f"\n{'='*125}")
print(f"{'bar':>3} | {'tail':>5} | {'win':>4} | {'trades':>6} | {'/day':>6} | {'WR':>6} | "
      f"{'PNL':>9} | {'PNL/d':>7} | {'PeakDD':>8} | {'DD%':>5} | {'MaxW':>4} | {'MaxL':>4} | {'MinP':>7}")
print("-" * 125)

all_results = []
for bar_min in bar_mins:
    H_bar = max(1, H // bar_min)
    cd_bar = max(1, cd // bar_min)
    for w_min in windows:
        w = max(1, w_min // bar_min)
        for tail in tails:
            poc = 1.0 - tail
            r = run_bt(bar_min, tail, w, poc)
            all_results.append((bar_min, tail, w_min, r))
            ok = "✓" if r["wr"] > 55.56 and r["pnl"] > 0 else " "
            print(f"{bar_min:>3} | {tail:>5.2f} | {w_min:>4} | "
                  f"{r['trades']:>6} | {r['per_day']:>6.1f} | {r['wr']:>5.1f}% | "
                  f"{r['pnl']:>+9.1f} | {r['pnl_day']:>+6.2f} | "
                  f"{r['max_dd']:>8.1f} | {r['dd_pct']:>5.1f} | "
                  f"{r['max_win']:>4} | {r['max_loss']:>4} | "
                  f"{r['min_pnl']:>+7.1f} {ok}")
    print("-" * 125)

# Top 5 by PNL
print(f"\n{'='*80}")
print(f"Top 10 by PNL (profitable & WR > 55.56%)")
print(f"{'='*80}")
profitable = [(b,t,w,r) for b,t,w,r in all_results if r["wr"] > 55.56 and r["pnl"] > 0]
profitable.sort(key=lambda x: x[3]["pnl"], reverse=True)
print(f"{'bar':>3} | {'tail':>5} | {'win':>4} | {'/day':>6} | {'WR':>6} | {'PNL':>9} | {'PNL/d':>7} | {'PeakDD':>8} | {'DD%':>5} | {'MaxL':>4}")
for b,t,w,r in profitable[:10]:
    print(f"{b:>3} | {t:>5.2f} | {w:>4} | {r['per_day']:>6.1f} | {r['wr']:>5.1f}% | "
          f"{r['pnl']:>+9.1f} | {r['pnl_day']:>+6.2f} | {r['max_dd']:>8.1f} | "
          f"{r['dd_pct']:>5.1f} | {r['max_loss']:>4}")

# Best risk-adjusted (PNL/MaxDD)
print(f"\n{'='*80}")
print(f"Top 10 risk-adjusted (PNL / PeakDD)")
print(f"{'='*80}")
risk_adj = [(b,t,w,r, r["pnl"]/r["max_dd"] if r["max_dd"]>0 else 0) for b,t,w,r in profitable if r["max_dd"] > 0]
risk_adj.sort(key=lambda x: x[4], reverse=True)
print(f"{'bar':>3} | {'tail':>5} | {'win':>4} | {'/day':>6} | {'WR':>6} | {'PNL':>9} | {'PeakDD':>8} | {'PNL/DD':>7} | {'MaxL':>4}")
for b,t,w,r, ratio in risk_adj[:10]:
    print(f"{b:>3} | {t:>5.2f} | {w:>4} | {r['per_day']:>6.1f} | {r['wr']:>5.1f}% | "
          f"{r['pnl']:>+9.1f} | {r['max_dd']:>8.1f} | {ratio:>7.2f} | {r['max_loss']:>4}")
