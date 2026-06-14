"""Verify the champion config's drawdown claim."""
import pandas as pd, numpy as np
from scipy.stats import norm as scipy_norm

df = pd.read_csv("e:/python-binance/data/btcusdt_1m.csv")
df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
for c in ["open","high","low","close","volume"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")
df = df.dropna(subset=["open_time","close"]).sort_values("open_time").reset_index(drop=True)

close = df["close"].values
lr = np.log(close[1:] / close[:-1])
lr = lr[np.isfinite(lr)]

# Champion config: bar=1m, tail=0.35, win=120min, cd=0
w_bars = 120
horizon_bars = 10
tp = 0.35
poc_thresh = 0.65

n = len(lr)
indices = np.arange(w_bars, n - horizon_bars)

# Vectorized p_up
cumsum = np.cumsum(lr)
cumsum2 = np.cumsum(lr ** 2)
s = cumsum[indices - 1] - cumsum[indices - w_bars - 1]
s2 = cumsum2[indices - 1] - cumsum2[indices - w_bars - 1]
mu = s / w_bars
var = (s2 / w_bars) - mu ** 2
sigma = np.sqrt(np.maximum(var * w_bars / (w_bars - 1), 0))
valid = sigma > 1e-10
z = np.where(valid, (horizon_bars * mu) / (np.sqrt(horizon_bars) * sigma), 0)
p_up = scipy_norm.cdf(z)

sig_down = valid & (p_up >= poc_thresh)
sig_up = valid & (p_up <= tp)
has_signal = sig_down | sig_up

future_close = close[indices + horizon_bars]
current_close = close[indices]
actual_up = future_close > current_close
actual_down = future_close < current_close

# Simulate trades and track cumulative PNL
sig_indices = np.where(has_signal)[0]
cum_pnl = 0.0
min_pnl = 0.0
max_pnl = 0.0
trades = []

for idx in sig_indices:
    actual_idx = indices[idx]
    is_up = sig_up[idx]
    is_down = sig_down[idx]
    
    if actual_up[idx]:
        correct = bool(is_up)
    elif actual_down[idx]:
        correct = bool(is_down)
    else:
        continue
    
    if correct:
        cum_pnl += 0.80
    else:
        cum_pnl -= 1.0
    
    if cum_pnl < min_pnl:
        min_pnl = cum_pnl
    if cum_pnl > max_pnl:
        max_pnl = cum_pnl
    
    trades.append({
        "bar": actual_idx,
        "time": df["open_time"].iloc[actual_idx],
        "correct": correct,
        "cum_pnl": cum_pnl,
    })

print(f"Champion: 1m/tail=0.35/win=120/cd=0")
print(f"Total trades: {len(trades)}")
print(f"Final PNL: {cum_pnl:+.2f}")
print(f"Min cumulative PNL: {min_pnl:+.2f}")
print(f"Max cumulative PNL: {max_pnl:+.2f}")

# First N trades analysis
print(f"\n=== First 20 trades ===")
for i, t in enumerate(trades[:20]):
    w = "WIN" if t["correct"] else "LOSS"
    print(f"  #{i+1:>3} {t['time']}  {w:>4}  cum={t['cum_pnl']:+.2f}")

# Find worst drawdown period
print(f"\n=== Worst drawdown periods ===")
cum_pnls = [t["cum_pnl"] for t in trades]
# Find the lowest point
min_idx = cum_pnls.index(min(cum_pnls))
print(f"Worst point: trade #{min_idx+1}, cum_pnl={cum_pnls[min_idx]:+.2f}")
print(f"  Time: {trades[min_idx]['time']}")
if min_idx > 0:
    print(f"  Previous 5 trades:")
    for i in range(max(0, min_idx-5), min_idx+1):
        t = trades[i]
        w = "WIN" if t["correct"] else "LOSS"
        print(f"    #{i+1} {t['time']}  {w:>4}  cum={t['cum_pnl']:+.2f}")

# Check: did PNL ever go negative?
negative_trades = [t for t in trades if t["cum_pnl"] < 0]
print(f"\n=== PNL < 0 occurrences ===")
print(f"Times PNL went negative: {len(negative_trades)}")
if negative_trades:
    print(f"First time: trade #{trades.index(negative_trades[0])+1}, cum={negative_trades[0]['cum_pnl']:+.2f}")
    print(f"Worst: cum={min(t['cum_pnl'] for t in negative_trades):+.2f}")
else:
    print("PNL NEVER went negative! MaxDD=0 is CORRECT.")

# Win/loss streak analysis
streaks = []
current = 0
for t in trades:
    if t["correct"]:
        if current > 0:
            current += 1
        else:
            current = 1
    else:
        if current < 0:
            current -= 1
        else:
            current = -1
    streaks.append(current)

max_win_streak = max(streaks)
max_loss_streak = min(streaks)
print(f"\n=== Streaks ===")
print(f"Longest win streak: {max_win_streak}")
print(f"Longest loss streak: {max_loss_streak}")

# Find and display the 42-loss streak
print(f"\n=== 42-loss streak detail ===")
current_streak = 0
worst_streak_end = -1
for i, s in enumerate(streaks):
    if s < current_streak:
        current_streak = s
        worst_streak_end = i
    else:
        current_streak = 0 if trades[i]["correct"] else -1

# Walk backwards to find streak start
streak_start = worst_streak_end
while streak_start > 0 and not trades[streak_start-1]["correct"]:
    streak_start -= 1

print(f"Loss streak from trade #{streak_start+1} to #{worst_streak_end+1} ({worst_streak_end - streak_start + 1} trades)")
print(f"PNL before streak: {trades[streak_start-1]['cum_pnl']:+.2f}" if streak_start > 0 else "PNL before streak: 0.00")
print(f"PNL after streak:  {trades[worst_streak_end]['cum_pnl']:+.2f}")
print(f"PNL dropped by:    {trades[worst_streak_end]['cum_pnl'] - (trades[streak_start-1]['cum_pnl'] if streak_start > 0 else 0):+.2f}")
print()
print(f"  {'#':>5} {'Time':>25} {'Result':>6} {'cum_pnl':>10} {'entry':>12} {'exit':>12} {'actual_dir':>10}")
for i in range(streak_start, min(streak_start + 50, len(trades))):
    t = trades[i]
    w = "WIN" if t["correct"] else "LOSS"
    bar = t["bar"]
    entry = close[bar]
    exit_p = close[min(bar + 10, len(close)-1)]
    direction = "UP" if exit_p > entry else ("DOWN" if exit_p < entry else "FLAT")
    print(f"  {i+1:>5} {str(t['time']):>25} {w:>6} {t['cum_pnl']:>+10.2f} {entry:>12.1f} {exit_p:>12.1f} {direction:>10}")

# Peak-to-trough drawdown (the REAL drawdown)
print(f"\n=== Peak-to-trough drawdown ===")
peak = 0.0
max_peak_dd = 0.0
peak_dd_start = 0
peak_dd_end = 0
current_peak = 0.0
current_peak_idx = 0
for i, t in enumerate(trades):
    if t["cum_pnl"] > current_peak:
        current_peak = t["cum_pnl"]
        current_peak_idx = i
    dd = current_peak - t["cum_pnl"]
    if dd > max_peak_dd:
        max_peak_dd = dd
        peak_dd_start = current_peak_idx
        peak_dd_end = i

print(f"Max peak-to-trough drawdown: {max_peak_dd:.2f}")
print(f"  From peak trade #{peak_dd_start+1} (cum={trades[peak_dd_start]['cum_pnl']:+.2f})")
print(f"  To trough trade #{peak_dd_end+1} (cum={trades[peak_dd_end]['cum_pnl']:+.2f})")
if peak_dd_start < len(trades) and peak_dd_end < len(trades):
    print(f"  Time span: {trades[peak_dd_start]['time']} -> {trades[peak_dd_end]['time']}")
