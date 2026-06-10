"""Summarizes the mathematically rigorous backtest results for 10m and 30m options.
"""

import sys
os_dir = "E:/codex/py"
sys.path.append(os_dir)

from research_correct_normal_distribution import load_data, run_rigorous_backtest

def main():
    df = load_data()
    if df is None:
        print("Data not found.")
        return
        
    horizons = [10, 30]
    windows = [30, 45, 60, 90, 120]
    tail_pcts = [0.03, 0.05, 0.07, 0.10, 0.15, 0.20]
    
    for h in horizons:
        results = []
        for w in windows:
            for pct in tail_pcts:
                for mode in ["trend", "reversal"]:
                    trades, wr, pnl, streak = run_rigorous_backtest(df, w, pct, h, mode=mode)
                    if trades > 0:
                        results.append({
                            "window": w, "tail": pct, "mode": mode, "trades": trades, "wr": wr, "pnl": pnl, "streak": streak
                        })
                        
        # Filter for profitable reversal configurations
        results = [r for r in results if r["pnl"] > 0 and r["mode"] == "reversal"]
        results.sort(key=lambda x: x["wr"], reverse=True)
        
        print("="*95)
        print(f" TOP PROFITABLE REVERSAL CONFIGURATIONS FOR {h}-MINUTE OPTIONS ")
        print("="*95)
        print(f"{'Rank':4s} | {'Window':6s} | {'Tail %':8s} | {'Thres WR':8s} | {'Trades':6s} | {'Tr/Day':6s} | {'Actual WR':9s} | {'PnL':9s} | {'Max Loss Streak':15s}")
        print("-"*95)
        for i, r in enumerate(results[:8]):
            tr_per_day = round(r["trades"] / 93.0, 2)
            pct_str = f"{r['tail']*100:.0f}%"
            thres_str = f"{100 - r['tail']*100:.0f}%"
            print(f"#{i+1:<3d} | {r['window']:4d}m | {pct_str:6s} | {thres_str:8s} | {r['trades']:6d} | {tr_per_day:6.2f} | {r['wr']:8.2f}% | ${r['pnl']:+8.2f} | {r['streak']:15d}")
        print()

if __name__ == "__main__":
    main()
