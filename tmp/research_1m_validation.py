"""
1分钟数据长期验证 — POC Normal策略等效参数
==========================================
将秒级策略参数转换为分钟级:
  W=600s → W=10 (10分钟窗口)
  H_p_up=300s → H=5 (5分钟投影)
  H_SETTLE=600s → 10 bars (10分钟到期)
  CD=600s → 10 bars
  tail=0.20 (不变)

数据: 139,275行 ≈ 96天 1分钟K线
"""
import math, numpy as np, pandas as pd, json
from collections import defaultdict

PAYOUT = 0.80
BE = 1.0 / (1.0 + PAYOUT) * 100
H_SETTLE_BARS = 10  # 10分钟到期
CD_BARS = 10  # 10分钟冷却

df = pd.read_csv("e:/python-binance/data/btcusdt_1m.csv")
df["ts"] = pd.to_datetime(df["open_time"], utc=True)
df = df.sort_values("ts").reset_index(drop=True)
close = df["close"].values.astype(float)
N = len(close)
t_start = df["ts"].iloc[0]
t_end = df["ts"].iloc[-1]
days = (t_end - t_start).total_seconds() / 86400
print(f"1分钟数据: {N}行 ({days:.0f}天)")
print(f"时间范围: {t_start} ~ {t_end}")
print(f"BE={BE:.2f}%")

lr = np.log(close[1:] / close[:-1])
lr = np.where(np.isfinite(lr), lr, 0.0)
cs_lr  = np.concatenate([[0.0], np.cumsum(lr)])
cs_lr2 = np.concatenate([[0.0], np.cumsum(lr ** 2)])
ncdf = np.vectorize(lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0))))
max_eval = N - H_SETTLE_BARS - CD_BARS

def get_signals_1m(W, H_p_up, tail, cd=CD_BARS):
    indices = np.arange(W, max_eval)
    s  = cs_lr[indices] - cs_lr[indices - W]
    s2 = cs_lr2[indices] - cs_lr2[indices - W]
    mu = s / W
    var = np.maximum((s2 / W) - mu**2, 0.0) * W / (W - 1)
    sigma = np.sqrt(var)
    z = np.sqrt(H_p_up) * mu / np.maximum(sigma, 1e-10)
    p_up = ncdf(z)

    all_sigs = []
    for i in range(len(indices)):
        d = 0
        if p_up[i] <= tail:
            d = 1
        elif p_up[i] >= 1 - tail:
            d = -1
        if d != 0:
            all_sigs.append((indices[i], d, p_up[i], z[i]))

    filtered = []
    last_bar = -99999
    for gidx, d, p, zv in all_sigs:
        if gidx - last_bar >= cd:
            last_bar = gidx
            filtered.append((gidx, d, p, zv))
    return filtered

def settle_1m(gidx, d):
    s_idx = gidx + H_SETTLE_BARS
    if s_idx >= N:
        return None
    went_up = close[s_idx] > close[gidx]
    return (went_up and d == 1) or (not went_up and d == -1)

# ============================================================
# Part 1: 等效参数验证
# ============================================================
print(f"\n{'='*100}")
print(f"Part 1: 等效参数验证 (1分钟级别)")
print(f"{'='*100}")

# 秒级最优 → 分钟级等效
EQUIV_PARAMS = [
    # (W_1m, H_1m, tail, label, equiv_1s)
    (10, 5, 0.20, "W=10 H=5 t=0.20", "≡ 1s: W=600 H=300 t=0.20"),
    (10, 10, 0.10, "W=10 H=10 t=0.10", "≡ 1s: W=600 H=600 t=0.10"),
    (10, 2, 0.30, "W=10 H=2 t=0.30", "≡ 1s: W=600 H=120 t=0.30"),
    (10, 15, 0.05, "W=10 H=15 t=0.05", "≡ 1s: W=600 H=900 t=0.05"),
    (2, 15, 0.05, "W=2 H=15 t=0.05", "≡ 1s: W=120 H=900 t=0.05"),
    (2, 2, 0.25, "W=2 H=2 t=0.25", "≡ 1s: W=120 H=120 t=0.25"),
]

print(f"\n  {'参数':>20} | {'N':>5} {'WR':>6} {'PNL':>8} | {'信号/天':>7} | {'等效1s参数':>25}")
print("  " + "-" * 80)

for W, H, t, label, equiv in EQUIV_PARAMS:
    sigs = get_signals_1m(W, H, t)
    results = [(g, d, settle_1m(g, d)) for g, d, p, z in sigs]
    results = [(g, d, w) for g, d, w in results if w is not None]
    n = len(results)
    if n < 10:
        print(f"  {label:>20} | {n:>5}  样本不足")
        continue
    wins = sum(1 for _, _, w in results if w)
    wr = wins / n * 100
    pnl = wins * PAYOUT - (n - wins) * 1.0
    sig_per_day = n / days
    bar = "█" * int(wr / 5)
    print(f"  {label:>20} | {n:>5} {wr:>5.1f}% {pnl:>+7.1f} | {sig_per_day:>6.1f} | {equiv:>25} {bar}")

# ============================================================
# Part 2: 大规模网格搜索
# ============================================================
print(f"\n{'='*100}")
print(f"Part 2: 1分钟级网格搜索")
print(f"{'='*100}")

W_GRID = [5, 8, 10, 12, 15, 20, 30]
H_GRID = [2, 3, 5, 8, 10, 15, 20, 30]
T_GRID = [0.05, 0.10, 0.15, 0.20, 0.25]

# 3折WF
splits = [(0, N//3), (N//3, 2*N//3), (2*N//3, N)]

scored = []
for W in W_GRID:
    for H in H_GRID:
        for t in T_GRID:
            sigs = get_signals_1m(W, H, t)
            fold_results = []
            for lo, hi in splits:
                results = []
                for gidx, d, p, z in sigs:
                    if lo <= gidx < hi:
                        r = settle_1m(gidx, d)
                        if r is not None:
                            results.append(r)
                fold_results.append(results)

            all_res = [r for fold in fold_results for r in fold]
            n = len(all_res)
            if n < 50:
                continue
            wr = sum(all_res) / n * 100
            pnl = sum(all_res) * PAYOUT - (n - sum(all_res)) * 1.0

            fold_wrs = []
            for fr in fold_results:
                if len(fr) >= 10:
                    fold_wrs.append(sum(fr)/len(fr)*100)
            if len(fold_wrs) < 3:
                continue
            min_fold_wr = min(fold_wrs)

            score = (wr - BE) * math.sqrt(n) * max(0, min_fold_wr / BE)
            scored.append({
                "W": W, "H": H, "t": t, "N": n, "WR": wr, "PNL": pnl,
                "minFold": min_fold_wr, "folds": fold_wrs, "score": score,
                "sig_per_day": n / days,
            })

scored.sort(key=lambda x: x["score"], reverse=True)

print(f"\n  Top 20 (综合评分, {days:.0f}天数据):")
print(f"  {'#':>3} {'W':>3} {'H':>3} {'t':>5} | {'N':>5} {'WR':>6} {'PNL':>8} | {'minFold':>7} {'F1':>6} {'F2':>6} {'F3':>6} | {'评分':>6} {'信号/天':>7}")
print("  " + "-" * 90)
for i, s in enumerate(scored[:20]):
    stars = "★★★" if s["minFold"] > BE else ("★★" if s["WR"] > BE else "★")
    bar = "█" * int(s["WR"] / 5)
    print(f"  {i+1:>3} {s['W']:>3} {s['H']:>3} {s['t']:>5.2f} | {s['N']:>5} {s['WR']:>5.1f}% {s['PNL']:>+7.1f} | {s['minFold']:>6.1f}% {s['folds'][0]:>5.1f}% {s['folds'][1]:>5.1f}% {s['folds'][2]:>5.1f}% | {s['score']:>5.1f} {s['sig_per_day']:>6.1f}/d {stars} {bar}")

# ============================================================
# Part 3: 等效参数的月度Walk-Forward
# ============================================================
print(f"\n{'='*100}")
print(f"Part 3: 月度Walk-Forward — 等效最优参数")
print(f"{'='*100}")

# 按月分割
df["month"] = df["ts"].dt.to_period("M")
months = df["month"].unique()
print(f"  月份: {len(months)}个 ({months[0]} ~ {months[-1]})")

for W, H, t in [(10, 5, 0.20), (10, 10, 0.10), (2, 15, 0.05)]:
    sigs = get_signals_1m(W, H, t)
    results = []
    for gidx, d, p, z in sigs:
        r = settle_1m(gidx, d)
        if r is not None:
            month = df["month"].iloc[gidx]
            results.append((gidx, d, r, month))

    n = len(results)
    wins = sum(1 for _, _, w, _ in results if w)
    wr = wins / n * 100
    pnl = wins * PAYOUT - (n - wins) * 1.0

    print(f"\n  W={W} H={H} t={t:.2f}: {n}信号 WR={wr:.1f}% PNL={pnl:+.1f}")
    print(f"  {'月份':>10} | {'N':>5} {'WR':>6} {'PNL':>7}")
    print("  " + "-" * 35)

    monthly = defaultdict(list)
    for g, d, w, m in results:
        monthly[str(m)].append(w)

    losing_months = 0
    for m in sorted(monthly.keys()):
        wins_list = monthly[m]
        mn = len(wins_list)
        mw = sum(wins_list)
        mwr = mw / mn * 100 if mn > 0 else 0
        mpnl = mw * PAYOUT - (mn - mw) * 1.0
        if mwr < BE:
            losing_months += 1
        bar = "█" * int(mwr / 5)
        flag = " ✗" if mwr < BE else " ★" if mwr > 70 else ""
        print(f"  {m:>10} | {mn:>5} {mwr:>5.1f}% {mpnl:>+6.1f}{flag} {bar}")

    print(f"  → 亏损月数: {losing_months}/{len(monthly)} ({losing_months/len(monthly)*100:.0f}%)")

# ============================================================
# Part 4: Bootstrap CI — 1分钟级
# ============================================================
print(f"\n{'='*100}")
print(f"Part 4: Bootstrap CI — 1分钟级等效参数")
print(f"{'='*100}")

np.random.seed(42)
N_BOOT = 5000

for W, H, t in [(10, 5, 0.20), (10, 10, 0.10), (2, 15, 0.05)]:
    sigs = get_signals_1m(W, H, t)
    wins_arr = np.array([1 if settle_1m(g, d) else 0 for g, d, _, _ in sigs
                         if settle_1m(g, d) is not None])
    n = len(wins_arr)
    wr = wins_arr.mean() * 100
    pnl = wins_arr.sum() * PAYOUT - (n - wins_arr.sum()) * 1.0

    boot_wrs = np.zeros(N_BOOT)
    for b in range(N_BOOT):
        sample = np.random.choice(wins_arr, size=n, replace=True)
        boot_wrs[b] = sample.mean() * 100

    ci_lo = np.percentile(boot_wrs, 2.5)
    ci_hi = np.percentile(boot_wrs, 97.5)
    prob_above_be = (boot_wrs > BE).mean() * 100

    print(f"\n  W={W} H={H} t={t:.2f}: {n}信号 WR={wr:.1f}% PNL={pnl:+.1f} ({n/days:.1f}信号/天)")
    print(f"    Bootstrap 95% CI: [{ci_lo:.1f}%, {ci_hi:.1f}%]")
    print(f"    P(WR > {BE:.1f}%): {prob_above_be:.1f}%")
    print(f"    P(WR > 60%):      {(boot_wrs > 60).mean()*100:.1f}%")
    print(f"    P(WR > 65%):      {(boot_wrs > 65).mean()*100:.1f}%")

# ============================================================
# Part 5: 秒级 vs 分钟级对比总结
# ============================================================
print(f"\n{'='*100}")
print(f"Part 5: 秒级(5h) vs 分钟级(96天) 对比总结")
print(f"{'='*100}")

print(f"""
  ┌──────────────────────────────────────────────────────────────────┐
  │              秒级数据 vs 分钟级数据 — POC Normal对比            │
  ├───────────────────┬────────────────────┬─────────────────────────┤
  │ 指标              │ 秒级(5h,17567行)   │ 分钟级(96天,139275行)  │
  ├───────────────────┼────────────────────┼─────────────────────────┤
  │ W=600/10 H=300/5  │                    │                         │
  │   WR              │ 75.0%              │ (见上方结果)            │
  │   信号数          │ 20                 │                         │
  │   P(WR>BE)        │ 96.0%              │                         │
  ├───────────────────┼────────────────────┼─────────────────────────┤
  │ W=120/2 H=900/15  │                    │                         │
  │   WR              │ 71.4%              │ (见上方结果)            │
  │   信号数          │ 28                 │                         │
  │   P(WR>BE)        │ 99.3%              │                         │
  ├───────────────────┼────────────────────┼─────────────────────────┤
  │ 数据量            │ 5小时              │ 96天                    │
  │ 统计可信度        │ 低(CI宽)           │ 高(CI窄)               │
  │ 过拟合风险        │ 高                 │ 低                      │
  └───────────────────┴────────────────────┴─────────────────────────┘
""")

# 保存结果
results_1m = {
    "data": {"rows": N, "days": days},
    "top_params": [{"W": s["W"], "H": s["H"], "t": s["t"], "N": s["N"],
                    "WR": s["WR"], "PNL": s["PNL"], "score": s["score"],
                    "minFold": s["minFold"], "sig_per_day": s["sig_per_day"]}
                   for s in scored[:10]],
}
with open("e:/python-binance/tmp/research_1m_validation.json", "w") as f:
    json.dump(results_1m, f, indent=2, default=str)
print(f"  ✓ 1分钟级结果已保存至 research_1m_validation.json")
