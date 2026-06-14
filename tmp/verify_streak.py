"""Quick fix: correct streak calculation."""
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

w_bars = 120; horizon_bars = 10; tp = 0.35; poc_thresh = 0.65
n = len(lr)
indices = np.arange(w_bars, n - horizon_bars)
cumsum = np.cumsum(lr); cumsum2 = np.cumsum(lr ** 2)
s = cumsum[indices - 1] - cumsum[indices - w_bars - 1]
s2 = cumsum2[indices - 1] - cumsum2[indices - w_bars - 1]
mu = s / w_bars; var = (s2 / w_bars) - mu ** 2
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

# Run simulation
results = []  # list of (time, correct, cum_pnl)
sig_indices = np.where(has_signal)[0]
cum_pnl = 0.0; peak = 0.0; max_peak_dd = 0.0
peak_time = None; trough_time = None; best_peak_time = None; worst_trough_time = None

for idx in sig_indices:
    actual_idx = indices[idx]
    is_up = sig_up[idx]; is_down = sig_down[idx]
    if actual_up[idx]:
        correct = bool(is_up)
    elif actual_down[idx]:
        correct = bool(is_down)
    else:
        continue
    cum_pnl += 0.80 if correct else -1.0
    if cum_pnl > peak:
        peak = cum_pnl
        best_peak_time = df["open_time"].iloc[actual_idx]
    dd = peak - cum_pnl
    if dd > max_peak_dd:
        max_peak_dd = dd
        peak_time = best_peak_time
        worst_trough_time = df["open_time"].iloc[actual_idx]
    results.append((correct, cum_pnl))

# Correct streak calculation
corrects = [r[0] for r in results]
max_win = 0; max_loss = 0; cur_win = 0; cur_loss = 0
for c in corrects:
    if c:
        cur_win += 1
        cur_loss = 0
        max_win = max(max_win, cur_win)
    else:
        cur_loss += 1
        cur_win = 0
        max_loss = max(max_loss, cur_loss)

print(f"=== Champion: 1m/tail=0.35/win=120/cd=0 ===")
print(f"Total trades: {len(results)}")
print(f"Win rate: {sum(corrects)/len(corrects)*100:.1f}%")
print(f"Final PNL: {cum_pnl:+.2f}")
print(f"Peak PNL: {peak:+.2f}")
print(f"")
print(f"Longest WIN streak:  {max_win} consecutive")
print(f"Longest LOSS streak: {max_loss} consecutive")
print(f"")
print(f"=== REAL Risk Metrics ===")
print(f"Max peak-to-trough drawdown: {max_peak_dd:.2f}U")
print(f"  Peak at:   {peak_time} (cum=+{peak:.2f})")
print(f"  Trough at: {worst_trough_time} (cum=+{peak - max_peak_dd:.2f})")
print(f"  Duration:  {(worst_trough_time - peak_time).days} days")
print(f"")
print(f"MaxDD as % of peak: {max_peak_dd/peak*100:.1f}%")
print(f"Daily profit: {cum_pnl/96.7:+.2f}U/day")
print(f"MaxDD / daily profit = {max_peak_dd/(cum_pnl/96.7):.0f} days of profit wiped out")
