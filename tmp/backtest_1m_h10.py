"""1-minute data backtest for 10-min binary option (hor=10).
Uses btcusdt_1m.csv (139K rows, ~90 days) to properly evaluate 10-min horizon.
"""
import pandas as pd
import numpy as np
from scipy.stats import norm as scipy_norm
import os

CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "btcusdt_1m.csv")
PAYOUT = 0.80
BREAKEVEN_WR = 1.0 / (1.0 + PAYOUT)

def main():
    df = pd.read_csv(CSV)
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open_time","close"]).sort_values("open_time").reset_index(drop=True)
    
    total_min = (df["open_time"].iloc[-1] - df["open_time"].iloc[0]).total_seconds() / 60
    print(f"1m data: {len(df)} rows, {total_min/60:.1f} hours, {total_min/1440:.1f} days")
    print(f"Range: {df['open_time'].iloc[0]} -> {df['open_time'].iloc[-1]}")
    print(f"Price: {df['close'].min():.1f} ~ {df['close'].max():.1f}")
    print(f"Payout: {PAYOUT*100:.0f}%, Breakeven WR: {BREAKEVEN_WR*100:.2f}%")
    
    close = df["close"].values
    lr = np.log(close[1:] / close[:-1])
    lr = lr[np.isfinite(lr)]
    
    # Parameters to test
    # bar_size in minutes (1, 2, 5)
    # We aggregate 1m data to N-minute bars
    bar_sizes = [1, 2, 5]
    tail_pcts = [0.15, 0.20, 0.25, 0.30, 0.35, 0.40]
    window_mins = [10, 30, 60, 120]
    horizon_min = 10  # Fixed for 10-min option
    cooldown_mins = [0, 10, 30]
    
    # Vectorized POC Normal backtest
    def run_poc(bar_lr, bars_close, bar_min, w_bars, horizon_bars, tp, cd_bars):
        """Vectorized: compute p_up for all bars, then evaluate signals."""
        n = len(bar_lr)
        poc_thresh = 1.0 - tp
        
        # Compute rolling mean/std using cumsum trick
        cumsum = np.cumsum(bar_lr)
        cumsum2 = np.cumsum(bar_lr ** 2)
        
        # For each i, w = bar_lr[i-w_bars:i], mu = mean(w), var = var(w)
        valid_start = w_bars
        valid_end = n - horizon_bars
        if valid_start >= valid_end:
            return None
        
        indices = np.arange(valid_start, valid_end)
        
        # sum of window: sum(bar_lr[i-w_bars:i])
        s = cumsum[indices - 1] - np.where(indices - w_bars - 1 >= 0, cumsum[indices - w_bars - 1], 0.0)
        s2 = cumsum2[indices - 1] - np.where(indices - w_bars - 1 >= 0, cumsum2[indices - w_bars - 1], 0.0)
        
        mu = s / w_bars
        var = (s2 / w_bars) - mu ** 2
        sigma = np.sqrt(var * w_bars / (w_bars - 1))  # ddof=1
        
        # Filter valid sigma
        valid = sigma > 1e-10
        
        z = np.where(valid, (horizon_bars * mu) / (np.sqrt(horizon_bars) * sigma), 0.0)
        p_up = scipy_norm.cdf(z)
        
        # Determine signals
        sig_down = valid & (p_up >= poc_thresh)
        sig_up = valid & (p_up <= tp)
        has_signal = sig_down | sig_up
        
        # Future direction
        future_close = bars_close[indices + horizon_bars]
        current_close = bars_close[indices]
        actual_up = future_close > current_close
        actual_down = future_close < current_close
        
        # Evaluate trades with cooldown
        wins = 0
        losses = 0
        flats = 0
        pnl = 0.0
        cum_pnl = 0.0
        max_dd = 0.0
        last_sig = -999999
        trade_count = 0
        
        sig_indices = np.where(has_signal)[0]
        for idx in sig_indices:
            actual_idx = indices[idx]  # actual bar index
            if actual_idx - last_sig < cd_bars:
                continue
            
            is_up = sig_up[idx]
            is_down = sig_down[idx]
            
            if actual_up[idx]:
                correct = is_up
            elif actual_down[idx]:
                correct = is_down
            else:
                flats += 1
                continue
            
            trade_count += 1
            if correct:
                wins += 1
                pnl += PAYOUT
                cum_pnl += PAYOUT
            else:
                losses += 1
                pnl -= 1.0
                cum_pnl -= 1.0
            
            if cum_pnl < max_dd:
                max_dd = cum_pnl
            last_sig = actual_idx
        
        return {"wins": wins, "losses": losses, "flats": flats, 
                "pnl": pnl, "max_dd": max_dd, "trades": trade_count}
    
    results = []
    
    print(f"\n{'='*110}")
    print(f"10-minute option backtest (horizon=10min fixed)")
    print(f"{'='*110}")
    header = (f"{'bar':>4} | {'tail':>5} | {'win':>4} | {'cd':>4} | "
              f"{'trades':>6} | {'wins':>5} | {'loss':>5} | "
              f"{'WR':>6} | {'PNL':>9} | {'MaxDD':>9} | {'OK':>3}")
    print(header)
    print("-" * len(header))
    
    for bar_min in bar_sizes:
        if bar_min == 1:
            bars_close = close
        else:
            indices = list(range(0, len(close), bar_min))
            bars_close = close[indices]
        
        bar_lr = np.log(bars_close[1:] / bars_close[:-1])
        bar_lr = bar_lr[np.isfinite(bar_lr)]
        
        horizon_bars = max(1, int(horizon_min / bar_min))
        
        for w_min in window_mins:
            w_bars = max(2, int(w_min / bar_min))
            if w_bars + horizon_bars >= len(bar_lr) - 1:
                continue
            for tp in tail_pcts:
                for cd_min in cooldown_mins:
                    cd_bars = max(0, int(cd_min / bar_min))
                    
                    res = run_poc(bar_lr, bars_close, bar_min, w_bars, horizon_bars, tp, cd_bars)
                    if res is None or res["trades"] == 0:
                        continue
                    
                    decided = res["wins"] + res["losses"]
                    wr = res["wins"] / decided * 100 if decided > 0 else 0
                    profitable = wr > BREAKEVEN_WR * 100 if decided >= 10 else False
                    
                    print(f"{bar_min:>4} | {tp:>5.2f} | {w_min:>4} | {cd_min:>4} | "
                          f"{res['trades']:>6} | {res['wins']:>5} | {res['losses']:>5} | "
                          f"{wr:>5.1f}% | {res['pnl']:>+9.2f} | {res['max_dd']:>+9.2f} | {'Y' if profitable else 'N':>3}")
                    
                    results.append({
                        "bar": bar_min, "tail": tp, "win": w_min, "cd": cd_min,
                        "trades": res["trades"], "wins": res["wins"], "losses": res["losses"],
                        "wr": wr, "pnl": res["pnl"], "max_dd": res["max_dd"], "profitable": profitable,
                    })
    
    # === Summary ===
    print(f"\n{'='*110}")
    print(f"Top 30 by PNL (trades >= 50)")
    print(f"{'='*110}")
    ranked = sorted([r for r in results if r["trades"] >= 50], key=lambda x: -x["pnl"])
    print(f"{'#':>3} | {'bar':>4} | {'tail':>5} | {'win':>4} | {'cd':>4} | "
          f"{'trades':>6} | {'WR':>6} | {'PNL':>9} | {'MaxDD':>9}")
    print("-" * 80)
    for i, r in enumerate(ranked[:30]):
        print(f"{i+1:>3} | {r['bar']:>4} | {r['tail']:>5.2f} | {r['win']:>4} | {r['cd']:>4} | "
              f"{r['trades']:>6} | {r['wr']:>5.1f}% | {r['pnl']:>+9.2f} | {r['max_dd']:>+9.2f}")
    
    # === Profitable configs ===
    print(f"\n{'='*110}")
    print(f"Profitable configs (WR > {BREAKEVEN_WR*100:.1f}%, trades >= 100)")
    print(f"{'='*110}")
    profitable = sorted([r for r in results if r["wr"] > BREAKEVEN_WR * 100 and r["trades"] >= 100],
                        key=lambda x: -x["pnl"])
    for i, r in enumerate(profitable[:20]):
        tpd = r["trades"] / (total_min / 1440)  # trades per day
        print(f"  {i+1:>2}. bar={r['bar']}m tail={r['tail']:.2f} win={r['win']}min cd={r['cd']}min "
              f"-> {r['trades']}trades WR={r['wr']:.1f}% PNL={r['pnl']:+.2f} DD={r['max_dd']:+.2f} "
              f"({tpd:.1f}/day)")
    
    if not profitable:
        print("  (None)")
    
    print(f"\nDone! Tested {len(results)} configs on {total_min/1440:.1f} days of data.")

if __name__ == "__main__":
    main()
