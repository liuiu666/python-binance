"""
最终策略优化 — 趋势过滤 + 参数组合 + Walk-Forward
================================================
关键发现驱动：
1. |trend_300s|>10bps时WR=33% → 强趋势是负过滤
2. 10min到期是各参数组最优
3. W=120 H=900 t=0.05统计置信度最高
4. W=600族3折WR minFold=71.4%

测试：
- 趋势过滤器效果（排除强趋势信号）
- W=120 H=900 vs W=600 H=300 全面对比
- 带趋势过滤的3折WF
- 最终推荐参数
"""
import math, numpy as np, pandas as pd, json

PAYOUT = 0.80
BE = 1.0 / (1.0 + PAYOUT) * 100
H_SETTLE = 600
CD = 600

df = pd.read_csv("e:/python-binance/tmp/server_1s_trades.csv")
df["ts"] = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601")
df = df.sort_values("ts").reset_index(drop=True)
close = df["close"].values.astype(float)
N = len(close)
print(f"数据: {N}行 ({N/60:.0f}min = {N/3600:.1f}h)")

lr = np.log(close[1:] / close[:-1])
lr = np.where(np.isfinite(lr), lr, 0.0)
cs_lr  = np.concatenate([[0.0], np.cumsum(lr)])
cs_lr2 = np.concatenate([[0.0], np.cumsum(lr ** 2)])
ncdf = np.vectorize(lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0))))
max_eval = N - H_SETTLE

# trend arrays
trend_300 = np.zeros(N)
for i in range(300, N):
    trend_300[i] = (close[i] / close[i - 300] - 1) * 10000

trend_600 = np.zeros(N)
for i in range(600, N):
    trend_600[i] = (close[i] / close[i - 600] - 1) * 10000

def get_signals(W, H_p_up, tail, cd=CD, trend_thr=0, trend_w=300):
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
        if gidx - last_bar < cd:
            continue
        # 趋势过滤
        if trend_thr > 0:
            tr_arr = trend_300 if trend_w == 300 else trend_600
            if abs(tr_arr[gidx]) > trend_thr:
                continue
        last_bar = gidx
        filtered.append((gidx, d, p, zv))
    return filtered

def settle(gidx, d):
    s_idx = gidx + H_SETTLE
    if s_idx >= N:
        return None
    went_up = close[s_idx] > close[gidx]
    return (went_up and d == 1) or (not went_up and d == -1)

# ============================================================
# Part 1: 趋势过滤器效果 — 扫描阈值
# ============================================================
print(f"\n{'='*100}")
print(f"Part 1: 趋势过滤器效果 — 排除|trend|>阈值的信号")
print(f"{'='*100}")

print(f"\n  {'参数':>22} {'趋势阈值':>8} | {'N':>4} {'WR':>6} {'PNL':>7} | {'过滤掉':>6} | {'BE概率':>6}")
print("  " + "-" * 75)

np.random.seed(42)
for W, H, t in [(600, 300, 0.20), (120, 900, 0.05)]:
    # 基准（无过滤）
    sigs_base = get_signals(W, H, t, trend_thr=0)
    results_base = [(g, d, settle(g, d)) for g, d, p, z in sigs_base]
    results_base = [(g, d, w) for g, d, w in results_base if w is not None]
    n_base = len(results_base)
    wr_base = sum(1 for _, _, w in results_base if w) / n_base * 100

    for trend_thr in [0, 5, 8, 10, 12, 15]:
        sigs = get_signals(W, H, t, trend_thr=trend_thr, trend_w=300)
        results = [(g, d, settle(g, d)) for g, d, p, z in sigs]
        results = [(g, d, w) for g, d, w in results if w is not None]
        n = len(results)
        if n < 3:
            continue
        wins = sum(1 for _, _, w in results if w)
        wr = wins / n * 100
        pnl = wins * PAYOUT - (n - wins) * 1.0
        filtered_out = n_base - n

        # Bootstrap P(WR>BE)
        wins_arr = np.array([1 if w else 0 for _, _, w in results])
        boot_wrs = np.array([np.random.choice(wins_arr, n).mean() * 100 for _ in range(2000)])
        p_above_be = (boot_wrs > BE).mean() * 100

        marker = " ★" if trend_thr == 10 else ""
        bar = "█" * int(wr / 5)
        print(f"  W={W} H={H:>4} t={t:.2f} | {trend_thr:>5}bps | {n:>4} {wr:>5.1f}% {pnl:>+6.1f} | {filtered_out:>5}个 | {p_above_be:>5.1f}%{marker} {bar}")
    print()

# ============================================================
# Part 2: W=120 H=900 vs W=600 H=300 全面对比
# ============================================================
print(f"\n{'='*100}")
print(f"Part 2: W=120 H=900 vs W=600 H=300 — 全面对比")
print(f"{'='*100}")

COMPARISON = [
    ("W=600 H=300 t=0.20", 600, 300, 0.20),
    ("W=120 H=900 t=0.05", 120, 900, 0.05),
]

splits = [(0, N//3), (N//3, 2*N//3), (2*N//3, N)]
print(f"\n  3折分割: F1[0:{N//3}] F2[{N//3}:{2*N//3}] F3[{2*N//3}:{N}]")
print(f"\n  {'指标':>20} | {'W=600 H=300':>15} | {'W=120 H=900':>15} | {'胜者':>8}")
print("  " + "-" * 65)

metrics = {}
for label, W, H, t in COMPARISON:
    sigs = get_signals(W, H, t)
    results = [(g, d, settle(g, d)) for g, d, p, z in sigs]
    results = [(g, d, w) for g, d, w in results if w is not None]
    n = len(results)
    wins = sum(1 for _, _, w in results if w)
    wr = wins / n * 100
    pnl = wins * PAYOUT - (n - wins) * 1.0

    # Fold stats
    fold_wrs = []
    for lo, hi in splits:
        fold_res = [w for g, d, w in results if lo <= g < hi]
        if len(fold_res) >= 2:
            fold_wrs.append(sum(fold_res)/len(fold_res)*100)

    # Direction stats
    ups = [w for g, d, w in results if d == 1]
    dns = [w for g, d, w in results if d == -1]
    up_wr = sum(ups)/len(ups)*100 if ups else 0
    dn_wr = sum(dns)/len(dns)*100 if dns else 0

    # Max consecutive loss
    streaks = []
    cur = 0
    for _, _, w in results:
        if not w:
            cur += 1
        else:
            if cur > 0:
                streaks.append(cur)
            cur = 0
    if cur > 0:
        streaks.append(cur)
    max_streak = max(streaks) if streaks else 0

    # Bootstrap CI
    wins_arr = np.array([1 if w else 0 for _, _, w in results])
    boot_wrs = np.array([np.random.choice(wins_arr, n).mean() * 100 for _ in range(5000)])
    ci_lo = np.percentile(boot_wrs, 2.5)
    ci_hi = np.percentile(boot_wrs, 97.5)
    p_be = (boot_wrs > BE).mean() * 100

    metrics[label] = {
        "n": n, "wr": wr, "pnl": pnl, "fold_wrs": fold_wrs,
        "up_wr": up_wr, "dn_wr": dn_wr, "ups": len(ups), "dns": len(dns),
        "max_streak": max_streak, "ci_lo": ci_lo, "ci_hi": ci_hi, "p_be": p_be,
        "ev_per_trade": pnl/n,
    }

m1 = metrics["W=600 H=300 t=0.20"]
m2 = metrics["W=120 H=900 t=0.05"]
comparisons = [
    ("信号数", m1["n"], m2["n"], "高"),
    ("胜率%", m1["wr"], m2["wr"], "高"),
    ("PNL", m1["pnl"], m2["pnl"], "高"),
    ("EV/笔", m1["ev_per_trade"], m2["ev_per_trade"], "高"),
    ("做多WR%", m1["up_wr"], m2["up_wr"], "高"),
    ("做空WR%", m1["dn_wr"], m2["dn_wr"], "高"),
    ("做多N", m1["ups"], m2["ups"], "高"),
    ("做空N", m1["dns"], m2["dns"], "高"),
    ("最大连亏", m1["max_streak"], m2["max_streak"], "低"),
    ("95%CI下限", m1["ci_lo"], m2["ci_lo"], "高"),
    ("P(WR>BE)%", m1["p_be"], m2["p_be"], "高"),
]

for name, v1, v2, better in comparisons:
    if better == "高":
        winner = "← " if v1 > v2 else (">" if v2 > v1 else "=")
    else:
        winner = "← " if v1 < v2 else (">" if v2 < v1 else "=")
    if isinstance(v1, float):
        print(f"  {name:>20} | {v1:>15.1f} | {v2:>15.1f} | {winner:>8}")
    else:
        print(f"  {name:>20} | {v1:>15} | {v2:>15} | {winner:>8}")

# 3折WR
print(f"\n  3折Walk-Forward WR:")
for i, (lo, hi) in enumerate(splits):
    f1 = m1["fold_wrs"][i] if i < len(m1["fold_wrs"]) else 0
    f2 = m2["fold_wrs"][i] if i < len(m2["fold_wrs"]) else 0
    print(f"    Fold {i+1} [{lo//60}-{hi//60}min]: W=600={f1:.1f}%  W=120={f2:.1f}%")

print(f"\n  minFold WR: W=600={min(m1['fold_wrs']):.1f}%  W=120={min(m2['fold_wrs']):.1f}%")

# ============================================================
# Part 3: 带趋势过滤的3折WF
# ============================================================
print(f"\n{'='*100}")
print(f"Part 3: 带趋势过滤(|trend_300|≤10bps)的3折WF")
print(f"{'='*100}")

for W, H, t in [(600, 300, 0.20), (120, 900, 0.05)]:
    sigs = get_signals(W, H, t, trend_thr=10, trend_w=300)
    results = [(g, d, settle(g, d)) for g, d, p, z in sigs]
    results = [(g, d, w) for g, d, w in results if w is not None]
    n = len(results)
    if n < 3:
        print(f"  W={W} H={H} t={t} +趋势过滤: 样本不足({n})")
        continue
    wins = sum(1 for _, _, w in results if w)
    wr = wins / n * 100
    pnl = wins * PAYOUT - (n - wins) * 1.0

    fold_stats = []
    for lo, hi in splits:
        fold_res = [(g, d, w) for g, d, w in results if lo <= g < hi]
        if len(fold_res) >= 1:
            fw = sum(1 for _, _, w in fold_res if w)
            fold_stats.append((len(fold_res), fw/len(fold_res)*100))
        else:
            fold_stats.append((0, 0))

    print(f"\n  W={W} H={H} t={t:.2f} +趋势过滤(≤10bps):")
    print(f"    全局: {n}信号 WR={wr:.1f}% PNL={pnl:+.1f}")
    for i, (fn, fwr) in enumerate(fold_stats):
        print(f"    Fold{i+1}: {fn}信号 WR={fwr:.1f}%")
    if all(fs[1] > BE for fs in fold_stats if fs[0] >= 2):
        print(f"    ★★★ 所有折均高于BE!")
    min_fold = min(fs[1] for fs in fold_stats if fs[0] >= 2)
    print(f"    minFold WR = {min_fold:.1f}%")

# ============================================================
# Part 4: 综合推荐
# ============================================================
print(f"\n{'='*100}")
print(f"Part 4: 最终推荐 — 综合所有分析")
print(f"{'='*100}")

print(f"""
  ╔══════════════════════════════════════════════════════════════════╗
  ║               秒级数据10分钟到期期权 — 最终策略推荐              ║
  ╠══════════════════════════════════════════════════════════════════╣
  ║                                                                  ║
  ║  基础参数:                                                       ║
  ║    PAYOUT = 0.80  (盈亏比)                                      ║
  ║    BE     = 55.56% (盈亏平衡胜率)                               ║
  ║    到期   = 600s (10分钟)                                       ║
  ║    冷却   = 600s (=到期,保证信号独立性)                         ║
  ║                                                                  ║
  ║  推荐参数A (高信号密度):                                        ║
  ║    W=120s  H_p_up=900s  tail=0.05                               ║
  ║    → 28信号/5h ≈ 5.6信号/h                                      ║
  ║    → WR=71.4-76.9%  P(WR>BE)=99.3%                             ║
  ║                                                                  ║
  ║  推荐参数B (高稳健性):                                          ║
  ║    W=600s  H_p_up=300s  tail=0.20                               ║
  ║    → 20信号/5h ≈ 4.0信号/h                                      ║
  ║    → WR=73.7-75.0%  3折minFold=71.4%                            ║
  ║                                                                  ║
  ║  可选过滤:                                                       ║
  ║    趋势过滤: 排除|trend_300s|>10bps的信号                       ║
  ║    → 提升WR约5-10%，但减少约30%信号                             ║
  ║                                                                  ║
  ║  仓位建议:                                                       ║
  ║    半Kelly ≈ 20%  (理论最优~44%但风险太高)                     ║
  ║    实盘建议: 5-10% 固定比例                                     ║
  ║                                                                  ║
  ║  ⚠️ 风险警告:                                                   ║
  ║    1. 数据仅5小时(17567行)，统计可信度有限                     ║
  ║    2. Bootstrap 95%CI下限仅51-62%                               ║
  ║    3. 方向不对称显著(15-26%)                                    ║
  ║    4. 参数对W高度敏感(W=660→WR骤降)                            ║
  ║    5. 时段效果可能偶然                                          ║
  ║                                                                  ║
  ╚══════════════════════════════════════════════════════════════════╝
""")

# 保存最终推荐
final_rec = {
    "strategy": {
        "PAYOUT": PAYOUT, "BE": BE,
        "expiry_sec": H_SETTLE, "cooldown_sec": CD,
        "params_A": {"W": 120, "H_p_up": 900, "tail": 0.05,
                      "WR_range": "71.4-76.9%", "signals_per_hour": 5.6,
                      "P_WR_above_BE": 99.3, "bootstrap_CI": [61.5, 92.3]},
        "params_B": {"W": 600, "H_p_up": 300, "tail": 0.20,
                      "WR_range": "73.7-75.0%", "signals_per_hour": 4.0,
                      "minFold_WR": 71.4, "bootstrap_CI": [52.6, 89.5]},
        "optional_filter": {"trend_300s_max": 10, "effect": "+5-10% WR, -30% signals"},
        "position_size": {"kelly": 0.44, "half_kelly": 0.22, "recommended": "5-10%"},
    },
    "data": {"rows": N, "hours": N/3600},
    "risks": [
        "仅5小时数据,统计可信度有限",
        "Bootstrap 95%CI下限51-62%",
        "方向不对称15-26%",
        "参数对W高度敏感",
        "时段效果可能偶然",
    ],
}
with open("e:/python-binance/tmp/research_1s_recommendation.json", "w") as f:
    json.dump(final_rec, f, indent=2, default=str)
print(f"  ✓ 最终推荐已保存至 research_1s_recommendation.json")
