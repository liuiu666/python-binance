"""Test champion with different cooldowns, reporting REAL peak-to-trough drawdown."""
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

days = (df["open_time"].iloc[-1] - df["open_time"].iloc[0]).total_seconds() / 86400
PAYOUT = 0.80

def run_bt(bar_lr, bars_close, bar_min, w_bars, horizon_bars, tp, cd_bars):
    poc_thresh = 1 - tp
    n = len(bar_lr)
    indices = np.arange(w_bars, n - horizon_bars)
    cumsum = np.cumsum(bar_lr); cumsum2 = np.cumsum(bar_lr ** 2)
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
    future_close = bars_close[indices + horizon_bars]
    current_close = bars_close[indices]
    actual_up = future_close > current_close
    actual_down = future_close < current_close
    sig_indices = np.where(has_signal)[0]
    
    wins = 0; losses = 0; flats = 0
    cum_pnl = 0.0; peak = 0.0; max_peak_dd = 0.0
    last_sig = -999999
    max_win = 0; max_loss = 0; cur_win = 0; cur_loss = 0
    min_pnl = 0.0  # from-zero drawdown
    
    for idx in sig_indices:
        actual_idx = indices[idx]
        if actual_idx - last_sig < cd_bars:
            continue
        is_up = sig_up[idx]; is_down = sig_down[idx]
        if actual_up[idx]:
            correct = bool(is_up)
        elif actual_down[idx]:
            correct = bool(is_down)
        else:
            flats += 1; continue
        
        if correct:
            wins += 1; cum_pnl += PAYOUT
            cur_win += 1; cur_loss = 0
            max_win = max(max_win, cur_win)
        else:
            losses += 1; cum_pnl -= 1.0
            cur_loss += 1; cur_win = 0
            max_loss = max(max_loss, cur_loss)
        
        if cum_pnl < min_pnl:
            min_pnl = cum_pnl
        if cum_pnl > peak:
            peak = cum_pnl
        dd = peak - cum_pnl
        if dd > max_peak_dd:
            max_peak_dd = dd
        last_sig = actual_idx
    
    trade_count = wins + losses
    wr = wins / trade_count * 100 if trade_count > 0 else 0
    return {
        "trades": trade_count, "wins": wins, "losses": losses, "flats": flats,
        "wr": wr, "pnl": cum_pnl, "peak": peak,
        "max_dd": max_peak_dd,  # peak-to-trough
        "min_pnl": min_pnl,     # from-zero
        "max_win": max_win, "max_loss": max_loss,
        "per_day": trade_count / days,
        "pnl_day": cum_pnl / days,
    }

# Test configs: vary cooldown on the best bar/tail/win combos
configs = [
    # (bar_min, tail, window_min, cooldown_min)
    # tail=0.35, win=120 - vary cooldown
    (1, 0.35, 120, 0), (1, 0.35, 120, 10), (1, 0.35, 120, 30),
    # tail=0.30, win=60 - vary cooldown
    (1, 0.30, 60, 0), (1, 0.30, 60, 10), (1, 0.30, 60, 30),
    # tail=0.25, win=60 - vary cooldown
    (1, 0.25, 60, 0), (1, 0.25, 60, 10), (1, 0.25, 60, 30),
    # tail=0.35, win=60 - vary cooldown
    (1, 0.35, 60, 0), (1, 0.35, 60, 10), (1, 0.35, 60, 30),
    # tail=0.30, win=120 - vary cooldown
    (1, 0.30, 120, 0), (1, 0.30, 120, 10), (1, 0.30, 120, 30),
    # tail=0.25, win=120 - vary cooldown
    (1, 0.25, 120, 0), (1, 0.25, 120, 10), (1, 0.25, 120, 30),
    # bar=2m configs
    (2, 0.35, 120, 0), (2, 0.35, 120, 10), (2, 0.35, 120, 30),
    (2, 0.30, 60, 0), (2, 0.30, 60, 10), (2, 0.30, 60, 30),
]

print(f"Data: {days:.1f} days, {len(close)} bars (1m)")
print(f"\n{'='*140}")
print(f"{'bar':>3} | {'tail':>5} | {'win':>4} | {'cd':>4} | {'trades':>6} | {'/day':>6} | {'WR':>6} | {'PNL':>9} | {'PNL/d':>7} | {'PeakDD':>8} | {'DD%':>5} | {'MaxW':>5} | {'MaxL':>5} | {'MinP':>7}")
print("-" * 140)

for bar_min, tail, win_min, cd_min in configs:
    if bar_min == 1:
        bar_close = close
        bar_lr = lr
    else:
        idx = list(range(0, len(close), bar_min))
        bar_close = close[idx]
        bar_lr = np.log(bar_close[1:] / bar_close[:-1])
        bar_lr = bar_lr[np.isfinite(bar_lr)]
    
    w_bars = max(1, win_min // bar_min)
    cd_bars = max(0, cd_min // bar_min)
    r = run_bt(bar_lr, bar_close, bar_min, w_bars, 10, tail, cd_bars)
    
    dd_pct = r["max_dd"] / r["peak"] * 100 if r["peak"] > 0 else 999
    ok = "✓" if r["wr"] > 55.56 else " "
    print(f"{bar_min:>3} | {tail:>5.2f} | {win_min:>4} | {cd_min:>4} | "
          f"{r['trades']:>6} | {r['per_day']:>6.1f} | {r['wr']:>5.1f}% | "
          f"{r['pnl']:>+9.1f} | {r['pnl_day']:>+6.2f} | "
          f"{r['max_dd']:>8.1f} | {dd_pct:>5.1f} | "
          f"{r['max_win']:>5} | {r['max_loss']:>5} | "
          f"{r['min_pnl']:>+7.1f} {ok}")

print(f"\n{'='*140}")
print(f"Legend: PeakDD = peak-to-trough drawdown | DD% = drawdown as % of peak | MaxW/MaxL = longest win/loss streak | MinP = lowest cumulative PNL ever")
