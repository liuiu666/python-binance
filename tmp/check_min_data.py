"""Check minimum data needed and first signal time for each config."""
import pandas as pd, numpy as np
from scipy.stats import norm as scipy_norm

df = pd.read_csv("e:/python-binance/tmp/server_1s_trades.csv")
df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
close = df["close"].values
lr = np.log(close[1:] / close[:-1])
lr = lr[np.isfinite(lr)]

configs = [
    ("5s/t0.30/w5/h5",   5, 0.30, 5, 5),
    ("5s/t0.15/w5/h5",   5, 0.15, 5, 5),
    ("10s/t0.20/w5/h5", 10, 0.20, 5, 5),
    ("10s/t0.15/w5/h5", 10, 0.15, 5, 5),
    ("5s/t0.30/w10/h5",  5, 0.30, 10, 5),
]

print(f"{'Config':<22} {'WinBars':>7} {'MinData':>8} {'1stSignal':>10} {'Total':>6}")
print("-" * 60)

for name, bar_sec, tail, win_min, hor_min in configs:
    window_bars = int(win_min * 60 / bar_sec)
    horizon_bars = int(hor_min * 60 / bar_sec)
    poc_thresh = 1.0 - tail
    min_bars_needed = window_bars + horizon_bars
    min_data_sec = min_bars_needed * bar_sec

    first_signal_bar = None
    total_signals = 0
    for i in range(window_bars, len(lr) - horizon_bars):
        w = lr[i - window_bars:i]
        if len(w) < max(10, window_bars // 2):
            continue
        mu = np.mean(w)
        sigma = np.std(w, ddof=1)
        if sigma < 1e-10:
            continue
        z = (horizon_bars * mu) / (np.sqrt(horizon_bars) * sigma)
        p_up = scipy_norm.cdf(z)
        if p_up >= poc_thresh or p_up <= tail:
            total_signals += 1
            if first_signal_bar is None:
                first_signal_bar = i

    if first_signal_bar is not None:
        first_sec = first_signal_bar * bar_sec
        first_str = f"{first_sec}s ({first_sec/60:.1f}min)"
    else:
        first_str = "N/A"

    print(f"{name:<22} {window_bars:>7} {min_data_sec/60:>7.1f}min {first_str:>10} {total_signals:>6}")

# Also check: how many unique signals in first 5/10/15 min
print(f"\n=== Signal count over time (5s/t0.30/w5/h5) ===")
bar_sec, tail, win_min, hor_min = 5, 0.30, 5, 5
window_bars = int(win_min * 60 / bar_sec)
horizon_bars = int(hor_min * 60 / bar_sec)
poc_thresh = 1.0 - tail

signal_times = []
for i in range(window_bars, len(lr) - horizon_bars):
    w = lr[i - window_bars:i]
    if len(w) < max(10, window_bars // 2):
        continue
    mu = np.mean(w)
    sigma = np.std(w, ddof=1)
    if sigma < 1e-10:
        continue
    z = (horizon_bars * mu) / (np.sqrt(horizon_bars) * sigma)
    p_up = scipy_norm.cdf(z)
    if p_up >= poc_thresh or p_up <= tail:
        signal_times.append(i * bar_sec)

for cutoff_min in [5, 10, 15, 30, 60, 120]:
    cutoff_sec = cutoff_min * 60
    count = sum(1 for t in signal_times if t <= cutoff_sec)
    print(f"  First {cutoff_min:>3} min: {count:>4} signals")
