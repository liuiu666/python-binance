"""
p_up置信度分析 + 参数等价性 + 3折Walk-Forward
================================================
"""
import math, numpy as np, pandas as pd

PAYOUT = 0.80
BE = 1.0 / (1.0 + PAYOUT) * 100
H_SETTLE = 600
CD = 600

df = pd.read_csv("e:/python-binance/tmp/server_1s_trades.csv")
df["ts"] = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601")
df = df.sort_values("ts").reset_index(drop=True)
close = df["close"].values.astype(float)
N = len(close)
print(f"数据: {N}行 ({N/60:.0f}min)")

lr = np.log(close[1:] / close[:-1])
lr = np.where(np.isfinite(lr), lr, 0.0)
cs_lr  = np.concatenate([[0.0], np.cumsum(lr)])
cs_lr2 = np.concatenate([[0.0], np.cumsum(lr ** 2)])
ncdf = np.vectorize(lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0))))
max_eval = N - H_SETTLE

trend_300 = np.zeros(N)
for i in range(300, N):
    trend_300[i] = (close[i] / close[i - 300] - 1) * 10000

def get_all_signals(W, H_p_up, tail, cd=CD):
    """返回所有信号(包括被CD过滤的)及其属性"""
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
    
    # CD过滤
    filtered = []
    last_bar = -99999
    for gidx, d, p, z in all_sigs:
        if gidx - last_bar >= cd:
            last_bar = gidx
            filtered.append((gidx, d, p, z))
    return filtered, all_sigs

def settle(gidx, d):
    s_idx = gidx + H_SETTLE
    if s_idx >= N:
        return None
    went_up = close[s_idx] > close[gidx]
    win = (went_up and d == 1) or (not went_up and d == -1)
    return win

# ============================================================
# Part 1: p_up置信度 vs 胜率
# ============================================================
print(f"\n{'='*100}")
print(f"Part 1: p_up置信度 vs 胜率")
print(f"{'='*100}")

# 用多个组合收集信号，按p_up极端程度分析
COMBOS = [
    (600, 120, 0.30), (600, 300, 0.20), (600, 600, 0.10),
    (600, 900, 0.05), (120, 900, 0.05), (120, 120, 0.25),
    (300, 900, 0.10), (300, 300, 0.20), (300, 60, 0.25),
]

all_trades = []
seen = set()
for W, H, t in COMBOS:
    sigs, _ = get_all_signals(W, H, t)
    for gidx, d, p, z in sigs:
        if gidx in seen:
            continue
        seen.add(gidx)
        r = settle(gidx, d)
        if r is not None:
            confidence = abs(p - 0.5) * 2  # 0~1, 越接近1越极端
            all_trades.append({
                "gidx": gidx, "dir": d, "p_up": p, "z": z,
                "conf": confidence, "win": r,
                "trend": trend_300[gidx], "W": W, "H": H, "tail": t,
            })

n_total = len(all_trades)
wr_all = sum(t["win"] for t in all_trades) / n_total * 100
print(f"\n  去重后总信号: {n_total}, 整体WR={wr_all:.1f}%")

# 按置信度分5档
confs = np.array([t["conf"] for t in all_trades])
sorted_idx = np.argsort(confs)

print(f"\n  {'档位':>8} | {'conf范围':>16} | {'N':>4} {'WR':>6} {'PNL':>7} | {'avg_p_up':>9} {'avg_z':>7}")
print("  " + "-" * 75)
for q in range(5):
    lo = q * n_total // 5
    hi = (q + 1) * n_total // 5
    seg = [all_trades[i] for i in sorted_idx[lo:hi]]
    seg_n = len(seg)
    if seg_n == 0:
        continue
    wins = sum(1 for t in seg if t["win"])
    wr = wins / seg_n * 100
    pnl = wins * PAYOUT - (seg_n - wins) * 1.0
    c_lo = confs[sorted_idx[lo]]
    c_hi = confs[sorted_idx[hi-1]]
    avg_p = np.mean([t["p_up"] for t in seg])
    avg_z = np.mean([abs(t["z"]) for t in seg])
    bar = "█" * int(wr / 4)
    print(f"  {'Q'+str(q+1):>8} | [{c_lo:.3f},{c_hi:.3f}] | {seg_n:>4} {wr:>5.1f}% {pnl:>+6.1f} | {avg_p:>8.3f} {avg_z:>6.2f} {bar}")

# 按p_up绝对值分档
print(f"\n  按p_up极端程度分档:")
print(f"  {'p_up范围':>16} | {'N':>4} {'WR':>6} {'PNL':>7} | {'含义':>10}")
print("  " + "-" * 55)
for p_lo, p_hi, label in [
    (0.0, 0.02, "p_up≤0.02"), (0.02, 0.05, "0.02-0.05"),
    (0.05, 0.10, "0.05-0.10"), (0.10, 0.15, "0.10-0.15"),
    (0.85, 0.90, "0.85-0.90"), (0.90, 0.95, "0.90-0.95"),
    (0.95, 0.98, "0.95-0.98"), (0.98, 1.01, "p_up≥0.98"),
]:
    seg = [t for t in all_trades if p_lo <= t["p_up"] < p_hi or p_lo <= (1-t["p_up"]) < p_hi]
    if len(seg) < 3:
        continue
    wins = sum(1 for t in seg if t["win"])
    wr = wins / len(seg) * 100
    pnl = wins * PAYOUT - (len(seg) - wins) * 1.0
    bar = "█" * int(wr / 4)
    print(f"  {label:>16} | {len(seg):>4} {wr:>5.1f}% {pnl:>+6.1f} | {'极端信号' if '≤0.02' in label or '≥0.98' in label else '中等信号':>10} {bar}")

# ============================================================
# Part 2: 参数等价性验证
# ============================================================
print(f"\n{'='*100}")
print(f"Part 2: 参数等价性 — 为什么不同(H,t)产生相同信号?")
print(f"{'='*100}")

# 检查信号重叠
W_fixed = 600
test_combos = [
    (120, 0.30), (300, 0.20), (600, 0.10),
    (900, 0.05), (1200, 0.05),
]

combo_sigs = {}
for H, t in test_combos:
    sigs, _ = get_all_signals(W_fixed, H, t)
    gidx_set = set(g for g, _, _, _ in sigs)
    combo_sigs[(H, t)] = (sigs, gidx_set)
    print(f"  W={W_fixed} H={H:>5} t={t:.2f}: {len(sigs)}信号")

print(f"\n  信号重叠矩阵 (Jaccard相似度):")
print(f"  {'':>16}", end="")
for H2, t2 in test_combos:
    print(f" {(H2,t2)}", end="")
print()
for H1, t1 in test_combos:
    _, s1 = combo_sigs[(H1, t1)]
    print(f"  H={H1:>4} t={t1:.2f}", end="")
    for H2, t2 in test_combos:
        _, s2 = combo_sigs[(H2, t2)]
        inter = len(s1 & s2)
        union = len(s1 | s2)
        jac = inter / union if union > 0 else 0
        print(f" {jac:>5.2f}({inter:>2})", end="")
    print()

# ============================================================
# Part 3: z-score直接作为信号 — 统一框架
# ============================================================
print(f"\n{'='*100}")
print(f"Part 3: z-score直接作为信号 — 统一框架")
print(f"{'='*100}")

# 不用tail，直接用z阈值
print(f"\n  W=600, 直接用|z|阈值:")
print(f"  {'|z|阈值':>8} | {'信号':>4} {'WR':>6} {'PNL':>7} | {'做多':>4} {'做空':>4} | {'前半WR':>7} {'后半WR':>7}")
print("  " + "-" * 65)

for z_thr in [1.0, 1.28, 1.5, 1.645, 1.8, 2.0, 2.326, 2.5, 3.0]:
    indices = np.arange(600, max_eval)
    s = cs_lr[indices] - cs_lr[indices - 600]
    s2 = cs_lr2[indices] - cs_lr2[indices - 600]
    mu = s / 600
    var = np.maximum((s2 / 600) - mu**2, 0.0) * 600 / 599
    sigma = np.sqrt(var)
    z = np.sqrt(300) * mu / np.maximum(sigma, 1e-10)  # 固定H=300计算z
    
    sig_dir = np.zeros(len(indices), dtype=np.int8)
    sig_dir[z >= z_thr] = -1  # z大→预期涨→做空
    sig_dir[z <= -z_thr] = 1  # z小→预期跌→做多
    
    filtered = []
    last_bar = -99999
    for i in range(len(indices)):
        if sig_dir[i] != 0 and indices[i] - last_bar >= CD:
            last_bar = indices[i]
            filtered.append((indices[i], sig_dir[i], z[i]))
    
    results = [(g, d, settle(g, d)) for g, d, _ in filtered]
    results = [(g, d, w) for g, d, w in results if w is not None]
    n = len(results)
    if n < 5:
        continue
    wins = sum(1 for _, _, w in results if w)
    wr = wins / n * 100
    pnl = wins * PAYOUT - (n - wins) * 1.0
    ups = sum(1 for _, d, _ in results if d == 1)
    dns = n - ups
    
    mid = N // 2
    f_wins = sum(1 for g, _, w in results if g < mid and w)
    f_n = sum(1 for g, _, _ in results if g < mid)
    s_wins = sum(1 for g, _, w in results if g >= mid and w)
    s_n = sum(1 for g, _, _ in results if g >= mid)
    f_wr = f_wins / f_n * 100 if f_n > 0 else 0
    s_wr = s_wins / s_n * 100 if s_n > 0 else 0
    
    bar = "█" * int(wr / 4)
    print(f"  {z_thr:>8.2f} | {n:>4} {wr:>5.1f}% {pnl:>+6.1f} | {ups:>4} {dns:>4} | {f_wr:>6.1f}% {s_wr:>6.1f}% {bar}")

# ============================================================
# Part 4: 3折Walk-Forward
# ============================================================
print(f"\n{'='*100}")
print(f"Part 4: 3折Walk-Forward验证")
print(f"{'='*100}")

splits = [(0, N//3), (N//3, 2*N//3), (2*N//3, N)]
print(f"  Fold 1: 0~{N//3} ({N//3/60:.0f}min)")
print(f"  Fold 2: {N//3}~{2*N//3} ({N//3/60:.0f}min)")
print(f"  Fold 3: {2*N//3}~{N} ({(N-2*N//3)/60:.0f}min)")

# 测试几个代表性组合
test_params = [
    (600, 300, 0.10), (600, 600, 0.10), (600, 900, 0.05),
    (120, 900, 0.05), (120, 120, 0.25), (600, 120, 0.30),
    (300, 900, 0.10), (300, 600, 0.10),
]

print(f"\n  {'W':>5} {'H':>7} {'t':>6} | {'全N':>4} {'全WR':>6} {'全PNL':>7} | {'F1':>3} {'F1WR':>6} | {'F2':>3} {'F2WR':>6} | {'F3':>3} {'F3WR':>6} | {'一致?':>6}")
print("  " + "-" * 90)

for W, H, t in test_params:
    fold_results = []
    for lo, hi in splits:
        sigs, _ = get_all_signals(W, H, t)
        results = []
        for gidx, d, p, z in sigs:
            if gidx < lo or gidx >= hi:
                continue
            r = settle(gidx, d)
            if r is not None:
                results.append(r)
        fold_results.append(results)
    
    all_res = [r for fold in fold_results for r in fold]
    n = len(all_res)
    if n < 8:
        continue
    wr = sum(all_res) / n * 100
    pnl = sum(all_res) * PAYOUT - (n - sum(all_res)) * 1.0
    
    fold_stats = []
    for fr in fold_results:
        if len(fr) > 0:
            fold_stats.append((len(fr), sum(fr)/len(fr)*100))
        else:
            fold_stats.append((0, 0))
    
    all_positive = all(s[1] > BE for s in fold_stats if s[0] >= 3)
    consistent = "★★★" if all_positive and wr > 75 else ("★★" if wr > BE else "★")
    
    f1n, f1wr = fold_stats[0]
    f2n, f2wr = fold_stats[1]
    f3n, f3wr = fold_stats[2]
    bar = "█" * int(wr / 4)
    print(f"  {W:>5} {H:>7} {t:>6.2f} | {n:>4} {wr:>5.1f}% {pnl:>+6.1f} | {f1n:>3} {f1wr:>5.1f}% | {f2n:>3} {f2wr:>5.1f}% | {f3n:>3} {f3wr:>5.1f}% | {consistent:>6} {bar}")

# ============================================================
# Part 5: 最优策略候选 — 综合评分
# ============================================================
print(f"\n{'='*100}")
print(f"Part 5: 综合评分 — 综合WR/信号数/稳健性")
print(f"{'='*100}")

W_GRID = [60, 120, 180, 300, 600, 900]
H_GRID = [30, 60, 120, 300, 600, 900, 1200, 1800]
T_GRID = [0.05, 0.10, 0.15, 0.20, 0.25]

scored = []
for W in W_GRID:
    for H in H_GRID:
        for t in T_GRID:
            fold_results = []
            for lo, hi in splits:
                sigs, _ = get_all_signals(W, H, t)
                results = []
                for gidx, d, p, z in sigs:
                    if gidx < lo or gidx >= hi:
                        continue
                    r = settle(gidx, d)
                    if r is not None:
                        results.append(r)
                fold_results.append(results)
            
            all_res = [r for fold in fold_results for r in fold]
            n = len(all_res)
            if n < 12:
                continue
            wr = sum(all_res) / n * 100
            pnl = sum(all_res) * PAYOUT - (n - sum(all_res)) * 1.0
            
            # 3折一致性
            fold_wrs = []
            for fr in fold_results:
                if len(fr) >= 2:
                    fold_wrs.append(sum(fr)/len(fr)*100)
            
            if len(fold_wrs) < 3:
                continue
            
            min_fold_wr = min(fold_wrs)
            fold_consistency = min_fold_wr  # 最低折WR
            
            # 综合评分: WR × 信号数权重 × 最低折WR权重
            # 评分 = (WR-BE) × sqrt(N) × (min_fold_wr/BE)
            score = (wr - BE) * math.sqrt(n) * max(0, min_fold_wr / BE)
            
            scored.append((W, H, t, n, wr, pnl, min_fold_wr, fold_wrs, score))

scored.sort(key=lambda x: x[8], reverse=True)

print(f"\n  Top 20 (综合评分 = (WR-BE)×√N×minFoldWR/BE):")
print(f"  {'#':>3} {'W':>5} {'H':>7} {'t':>6} | {'N':>4} {'WR':>6} {'PNL':>7} | {'minFold':>7} {'F1':>6} {'F2':>6} {'F3':>6} | {'评分':>6}")
print("  " + "-" * 85)
for i, (W, H, t, n, wr, pnl, min_f, fws, score) in enumerate(scored[:20]):
    stars = "★★★" if min_f > BE else ("★★" if wr > BE else "★")
    bar = "█" * int(wr / 4)
    print(f"  {i+1:>3} {W:>5} {H:>7} {t:>6.2f} | {n:>4} {wr:>5.1f}% {pnl:>+6.1f} | {min_f:>6.1f}% {fws[0]:>5.1f}% {fws[1]:>5.1f}% {fws[2]:>5.1f}% | {score:>5.1f} {stars} {bar}")
