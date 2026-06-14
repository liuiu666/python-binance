"""
秒级数据10分期权最终研究 — 新维度分析
======================================
1. Z-score统一框架: 直接|z|阈值扫描
2. 时段分析: 信号质量随UTC小时变化
3. 回撤分析: 最大连续亏损、资金曲线
4. 方向不对称: 做多 vs 做空
5. Kelly准则: 最优仓位
6. 信号间距: 连续信号质量
7. 多窗口确认: 不同W的信号重叠
8. 综合最优策略推荐
"""
import math, numpy as np, pandas as pd, json
from collections import defaultdict

PAYOUT = 0.80
BE = 1.0 / (1.0 + PAYOUT) * 100  # 55.56%
H_SETTLE = 600
CD = 600

# ============================================================
# 数据加载
# ============================================================
df = pd.read_csv("e:/python-binance/tmp/server_1s_trades.csv")
df["ts"] = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601")
df = df.sort_values("ts").reset_index(drop=True)
close = df["close"].values.astype(float)
ts_arr = df["ts"].values
N = len(close)
t_start = df["ts"].iloc[0]
t_end = df["ts"].iloc[-1]
duration_min = N / 60

print(f"数据: {N}行 ({duration_min:.0f}min = {duration_min/60:.1f}h)")
print(f"时间范围: {t_start} ~ {t_end}")
print(f"价格范围: {close.min():.1f} ~ {close.max():.1f}")
print(f"BE(盈亏平衡胜率) = {BE:.2f}%")

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

def get_signals(W, H_p_up, tail, cd=CD):
    """返回CD过滤后的信号列表 [(gidx, dir, p_up, z)]"""
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

def settle(gidx, d):
    s_idx = gidx + H_SETTLE
    if s_idx >= N:
        return None
    went_up = close[s_idx] > close[gidx]
    win = (went_up and d == 1) or (not went_up and d == -1)
    return win

def settle_detail(gidx, d):
    """返回(win, price_move_bps, max_adverse_bps, max_favorable_bps)"""
    s_idx = gidx + H_SETTLE
    if s_idx >= N:
        return None
    entry_price = close[gidx]
    settle_price = close[s_idx]
    segment = close[gidx:s_idx+1]
    went_up = settle_price > entry_price
    win = (went_up and d == 1) or (not went_up and d == -1)
    move_bps = (settle_price / entry_price - 1) * 10000
    if d == 1:
        max_adv = (np.min(segment) / entry_price - 1) * 10000
        max_fav = (np.max(segment) / entry_price - 1) * 10000
    else:
        max_adv = (entry_price / np.max(segment) - 1) * 10000
        max_fav = (entry_price / np.min(segment) - 1) * 10000
    return win, move_bps, max_adv, max_fav

# ============================================================
# Part 1: Z-score统一框架 — 直接|z|阈值扫描
# ============================================================
print(f"\n{'='*100}")
print(f"Part 1: Z-score统一框架 — 不同W下的|z|阈值扫描")
print(f"{'='*100}")

W_TEST = [60, 120, 300, 600, 900]
H_FIXED = 600  # 固定H=600计算z

print(f"\n  固定H_p_up={H_FIXED}, 扫描W和|z|阈值:")
print(f"  {'W':>5} {'|z|阈值':>8} | {'N':>4} {'WR':>6} {'PNL':>7} | {'做多N':>5} {'做多WR':>7} {'做空N':>5} {'做空WR':>7} | {'p_up等效':>8}")
print("  " + "-" * 85)

for W in W_TEST:
    indices = np.arange(W, max_eval)
    s = cs_lr[indices] - cs_lr[indices - W]
    s2 = cs_lr2[indices] - cs_lr2[indices - W]
    mu = s / W
    var = np.maximum((s2 / W) - mu**2, 0.0) * W / (W - 1)
    sigma = np.sqrt(var)
    z_arr = np.sqrt(H_FIXED) * mu / np.maximum(sigma, 1e-10)

    for z_thr in [1.5, 1.8, 2.0, 2.326, 2.5, 3.0]:
        sig_dir = np.zeros(len(indices), dtype=np.int8)
        sig_dir[z_arr >= z_thr] = -1
        sig_dir[z_arr <= -z_thr] = 1

        filtered = []
        last_bar = -99999
        for i in range(len(indices)):
            if sig_dir[i] != 0 and indices[i] - last_bar >= CD:
                last_bar = indices[i]
                filtered.append((indices[i], int(sig_dir[i])))

        results = [(g, d, settle(g, d)) for g, d in filtered]
        results = [(g, d, w) for g, d, w in results if w is not None]
        n = len(results)
        if n < 5:
            continue
        wins = sum(1 for _, _, w in results if w)
        wr = wins / n * 100
        pnl = wins * PAYOUT - (n - wins) * 1.0
        ups = [(g, d, w) for g, d, w in results if d == 1]
        dns = [(g, d, w) for g, d, w in results if d == -1]
        up_wr = sum(1 for _, _, w in ups if w) / len(ups) * 100 if ups else 0
        dn_wr = sum(1 for _, _, w in dns if w) / len(dns) * 100 if dns else 0
        p_eq = ncdf(np.array([-z_thr]))[0]

        bar = "█" * int(wr / 5)
        print(f"  {W:>5} {z_thr:>8.2f} | {n:>4} {wr:>5.1f}% {pnl:>+6.1f} | {len(ups):>5} {up_wr:>6.1f}% {len(dns):>5} {dn_wr:>6.1f}% | p={p_eq:.4f} {bar}")

# ============================================================
# Part 2: 时段分析 — UTC小时 vs 信号质量
# ============================================================
print(f"\n{'='*100}")
print(f"Part 2: 时段分析 — 信号质量随UTC小时变化")
print(f"{'='*100}")

# 用最优参数族收集信号
COMBOS = [
    (600, 300, 0.20), (600, 600, 0.10), (600, 120, 0.30),
    (120, 900, 0.05), (120, 120, 0.25),
]

all_trades = []
seen = set()
for W, H, t in COMBOS:
    sigs = get_signals(W, H, t)
    for gidx, d, p, zv in sigs:
        if gidx in seen:
            continue
        seen.add(gidx)
        r = settle(gidx, d)
        if r is not None:
            hour = pd.Timestamp(ts_arr[gidx]).hour
            all_trades.append({
                "gidx": gidx, "dir": d, "win": r, "hour": hour,
                "trend": trend_300[gidx], "z": zv, "p_up": p,
            })

hour_stats = defaultdict(list)
for t in all_trades:
    hour_stats[t["hour"]].append(t["win"])

print(f"\n  信号按UTC小时分布 (去重后 {len(all_trades)} 信号):")
print(f"  {'UTC时':>6} | {'N':>4} {'WR':>6} {'PNL':>7} | {'占比':>6} | 备注")
print("  " + "-" * 60)
for h in sorted(hour_stats.keys()):
    wins_list = hour_stats[h]
    n = len(wins_list)
    wins = sum(wins_list)
    wr = wins / n * 100
    pnl = wins * PAYOUT - (n - wins) * 1.0
    pct = n / len(all_trades) * 100
    bar = "█" * int(wr / 5)
    note = ""
    if wr > 80: note = "★优"
    elif wr < BE: note = "✗差"
    print(f"  {h:>4}:00 | {n:>4} {wr:>5.1f}% {pnl:>+6.1f} | {pct:>5.1f}% | {note} {bar}")

# ============================================================
# Part 3: 回撤分析 — 连续亏损、资金曲线
# ============================================================
print(f"\n{'='*100}")
print(f"Part 3: 回撤分析 — 连续亏损与资金曲线")
print(f"{'='*100}")

for W, H, t in [(600, 300, 0.20), (120, 900, 0.05), (600, 600, 0.10)]:
    sigs = get_signals(W, H, t)
    results = [(g, d, settle(g, d)) for g, d, p, z in sigs]
    results = [(g, d, w) for g, d, w in results if w is not None]
    n = len(results)
    if n < 5:
        continue

    # 资金曲线 (固定1单位/笔)
    equity = [0.0]
    for _, _, w in results:
        equity.append(equity[-1] + (PAYOUT if w else -1.0))

    # 最大连续亏损
    max_streak = 0
    cur_streak = 0
    for _, _, w in results:
        if not w:
            cur_streak += 1
            max_streak = max(max_streak, cur_streak)
        else:
            cur_streak = 0

    # 最大回撤
    peak = equity[0]
    max_dd = 0
    for e in equity:
        peak = max(peak, e)
        max_dd = max(max_dd, peak - e)

    wins = sum(1 for _, _, w in results if w)
    wr = wins / n * 100
    pnl = equity[-1]

    # 连续亏损序列统计
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

    print(f"\n  W={W} H={H} t={t:.2f}: {n}信号 WR={wr:.1f}% PNL={pnl:+.1f}")
    print(f"    最大连续亏损: {max_streak}笔")
    print(f"    最大回撤: {max_dd:.1f}单位")
    print(f"    亏损序列分布: {sorted(streaks, reverse=True)[:5]}")
    print(f"    资金曲线: ", end="")
    for q in range(0, len(equity), max(1, len(equity)//10)):
        print(f"{equity[q]:+.1f} ", end="")
    print(f"| 最终={equity[-1]:+.1f}")

    # 概率检验: P(>=k连续亏损 | WR)
    for k in [2, 3, 4]:
        prob_k = (1 - wr/100) ** k
        expected_count = n * prob_k * (wr/100)  # 粗略期望
        actual_count = sum(1 for s in streaks if s >= k)
        print(f"    P(>={k}连亏)={prob_k:.4f}  实际{actual_count}次 vs 期望~{expected_count:.1f}次")

# ============================================================
# Part 4: 方向不对称分析
# ============================================================
print(f"\n{'='*100}")
print(f"Part 4: 方向不对称 — 做多 vs 做空信号质量")
print(f"{'='*100}")

for W, H, t in [(600, 300, 0.20), (120, 900, 0.05), (600, 120, 0.30)]:
    sigs = get_signals(W, H, t)
    results = []
    for gidx, d, p, zv in sigs:
        r = settle_detail(gidx, d)
        if r:
            results.append((gidx, d, *r))

    ups = [r for r in results if r[1] == 1]
    dns = [r for r in results if r[1] == -1]
    n = len(results)
    if n < 5:
        continue

    up_wins = sum(1 for r in ups if r[2])
    dn_wins = sum(1 for r in dns if r[2])
    up_wr = up_wins / len(ups) * 100 if ups else 0
    dn_wr = dn_wins / len(dns) * 100 if dns else 0

    up_pnl = up_wins * PAYOUT - (len(ups) - up_wins) * 1.0
    dn_pnl = dn_wins * PAYOUT - (len(dns) - dn_wins) * 1.0

    # 平均价格偏移和最大逆向偏移
    up_move = np.mean([r[3] for r in ups]) if ups else 0
    dn_move = np.mean([r[3] for r in dns]) if dns else 0
    up_adv = np.mean([r[4] for r in ups]) if ups else 0
    dn_adv = np.mean([r[4] for r in dns]) if dns else 0

    print(f"\n  W={W} H={H} t={t:.2f}:")
    print(f"    做多: N={len(ups):>3} WR={up_wr:>5.1f}% PNL={up_pnl:>+5.1f} | 均偏移={up_move:>+6.1f}bps 均逆行={up_adv:>+6.1f}bps")
    print(f"    做空: N={len(dns):>3} WR={dn_wr:>5.1f}% PNL={dn_pnl:>+5.1f} | 均偏移={dn_move:>+6.1f}bps 均逆行={dn_adv:>+6.1f}bps")
    diff = abs(up_wr - dn_wr)
    if diff > 15:
        print(f"    ⚠️ 方向不对称={diff:.1f}% — 显著差异!")
    else:
        print(f"    方向差异={diff:.1f}% — 可接受")

# ============================================================
# Part 5: Kelly准则 — 最优仓位
# ============================================================
print(f"\n{'='*100}")
print(f"Part 5: Kelly准则 — 最优固定分数仓位")
print(f"{'='*100}")

for W, H, t in [(600, 300, 0.20), (120, 900, 0.05), (600, 600, 0.10)]:
    sigs = get_signals(W, H, t)
    results = [(g, d, settle(g, d)) for g, d, p, z in sigs]
    results = [(g, d, w) for g, d, w in results if w is not None]
    n = len(results)
    if n < 5:
        continue
    wins = sum(1 for _, _, w in results if w)
    wr = wins / n
    loss_rate = 1 - wr

    # Kelly: f* = (b*WR - (1-WR)) / b, where b=PAYOUT
    kelly = (PAYOUT * wr - loss_rate) / PAYOUT
    # 半Kelly更安全
    half_kelly = kelly / 2

    # 期望值/笔 (每1单位风险)
    ev_per_trade = wr * PAYOUT - loss_rate * 1.0
    # Sharpe-like: EV/std
    std_per_trade = math.sqrt(wr * (PAYOUT - ev_per_trade)**2 + loss_rate * (-1.0 - ev_per_trade)**2)
    sharpe_like = ev_per_trade / std_per_trade if std_per_trade > 0 else 0

    print(f"\n  W={W} H={H} t={t:.2f} ({n}信号 WR={wr*100:.1f}%):")
    print(f"    Kelly f* = {kelly*100:.1f}%  (半Kelly = {half_kelly*100:.1f}%)")
    print(f"    EV/笔 = {ev_per_trade:+.4f}  Sharpe-like = {sharpe_like:.3f}")
    print(f"    {'仓位':>8} | {'期望资金倍数(100笔)':>18} | {'破产概率':>10}")
    for frac in [0.05, 0.10, 0.15, 0.20, 0.30, kelly]:
        # 100笔后期望对数增长
        exp_log = wr * math.log(1 + frac * PAYOUT) + loss_rate * math.log(1 - frac)
        if exp_log > 0:
            mult_100 = math.exp(100 * exp_log)
            ruin = 0
            # 简单破产概率估计: 连续亏损到0
            max_losses = int(1 / frac) + 1
            ruin = loss_rate ** max_losses
        else:
            mult_100 = math.exp(100 * exp_log)
            ruin = 1.0
        label = f"{frac*100:.0f}%" if frac != kelly else f"Kelly({kelly*100:.0f}%)"
        print(f"    {label:>8} | {mult_100:>16.2f}x | {ruin:>10.6f}")

# ============================================================
# Part 6: 信号间距分析
# ============================================================
print(f"\n{'='*100}")
print(f"Part 6: 信号间距分析 — 连续信号质量")
print(f"{'='*100}")

sigs = get_signals(600, 300, 0.20)
results = []
for i, (gidx, d, p, zv) in enumerate(sigs):
    r = settle(gidx, d)
    if r is not None:
        if i > 0:
            gap = gidx - sigs[i-1][0]
        else:
            gap = 99999
        results.append((gidx, d, r, gap))

print(f"\n  W=600 H=300 t=0.20 ({len(results)}信号):")
print(f"  {'间距':>10} | {'N':>4} {'WR':>6} {'PNL':>7}")
print("  " + "-" * 40)
for gap_lo, gap_hi, label in [(600, 900, "10-15min"), (900, 1800, "15-30min"), (1800, 3600, "30-60min"), (3600, 99999, ">60min")]:
    seg = [r for r in results if gap_lo <= r[3] < gap_hi]
    if len(seg) < 3:
        continue
    wins = sum(1 for _, _, w, _ in seg if w)
    wr = wins / len(seg) * 100
    pnl = wins * PAYOUT - (len(seg) - wins) * 1.0
    print(f"  {label:>10} | {len(seg):>4} {wr:>5.1f}% {pnl:>+6.1f}")

# ============================================================
# Part 7: 多窗口确认
# ============================================================
print(f"\n{'='*100}")
print(f"Part 7: 多窗口确认 — 不同W同时产生信号")
print(f"{'='*100}")

# 用3个不同W收集信号
multi_sigs = {}
for W in [120, 300, 600]:
    sigs = get_signals(W, 600, 0.10)  # 统一H=600, tail=0.10
    multi_sigs[W] = {g: (g, d, p, z) for g, d, p, z in sigs}

# 找到在多个W中同时出现的信号 (±60秒窗口)
all_gidxs = sorted(set().union(*[set(v.keys()) for v in multi_sigs.values()]))
confirmations = []
for gidx in all_gidxs:
    votes = {}
    for W in [120, 300, 600]:
        # 检查±60s内是否有信号
        for offset in range(-60, 61):
            if gidx + offset in multi_sigs[W]:
                _, d, p, z = multi_sigs[W][gidx + offset]
                votes[W] = d
                break

    if len(votes) >= 2:
        dirs = list(votes.values())
        if all(d == dirs[0] for d in dirs):
            confirmations.append((gidx, dirs[0], len(votes)))

# CD过滤
filtered_conf = []
last_bar = -99999
for gidx, d, n_vote in sorted(confirmations):
    if gidx - last_bar >= CD:
        last_bar = gidx
        filtered_conf.append((gidx, d, n_vote))

print(f"\n  多窗口确认 (W∈{{120,300,600}}, H=600, tail=0.10, ±60s窗口):")
for min_votes in [2, 3]:
    seg = [(g, d) for g, d, v in filtered_conf if v >= min_votes]
    results = [(g, d, settle(g, d)) for g, d in seg]
    results = [(g, d, w) for g, d, w in results if w is not None]
    n = len(results)
    if n < 3:
        print(f"    >={min_votes}票确认: {n}信号 — 样本不足")
        continue
    wins = sum(1 for _, _, w in results if w)
    wr = wins / n * 100
    pnl = wins * PAYOUT - (n - wins) * 1.0
    print(f"    >={min_votes}票确认: {n}信号 WR={wr:.1f}% PNL={pnl:+.1f}")

# 单窗口对比
for W in [120, 300, 600]:
    sigs = get_signals(W, 600, 0.10)
    results = [(g, d, settle(g, d)) for g, d, p, z in sigs]
    results = [(g, d, w) for g, d, w in results if w is not None]
    n = len(results)
    if n < 3:
        continue
    wins = sum(1 for _, _, w in results if w)
    wr = wins / n * 100
    pnl = wins * PAYOUT - (n - wins) * 1.0
    print(f"    单W={W}: {n}信号 WR={wr:.1f}% PNL={pnl:+.1f}")

# ============================================================
# Part 8: 最优策略综合推荐
# ============================================================
print(f"\n{'='*100}")
print(f"Part 8: 综合最优策略推荐")
print(f"{'='*100}")

# 重新跑综合评分 (使用最新数据)
splits = [(0, N//3), (N//3, 2*N//3), (2*N//3, N)]
W_GRID = [60, 120, 180, 300, 600, 900]
H_GRID = [30, 60, 120, 300, 600, 900, 1200]
T_GRID = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]

scored = []
for W in W_GRID:
    for H in H_GRID:
        for t in T_GRID:
            sigs = get_signals(W, H, t)
            fold_results = []
            for lo, hi in splits:
                results = []
                for gidx, d, p, z in sigs:
                    if lo <= gidx < hi:
                        r = settle(gidx, d)
                        if r is not None:
                            results.append(r)
                fold_results.append(results)

            all_res = [r for fold in fold_results for r in fold]
            n = len(all_res)
            if n < 10:
                continue
            wr = sum(all_res) / n * 100
            pnl = sum(all_res) * PAYOUT - (n - sum(all_res)) * 1.0

            fold_wrs = []
            for fr in fold_results:
                if len(fr) >= 2:
                    fold_wrs.append(sum(fr)/len(fr)*100)
            if len(fold_wrs) < 3:
                continue
            min_fold_wr = min(fold_wrs)

            # 综合评分
            score = (wr - BE) * math.sqrt(n) * max(0, min_fold_wr / BE)
            scored.append({
                "W": W, "H": H, "t": t, "N": n, "WR": wr, "PNL": pnl,
                "minFold": min_fold_wr, "folds": fold_wrs, "score": score,
            })

scored.sort(key=lambda x: x["score"], reverse=True)

print(f"\n  综合评分 Top 15 (数据={N}行={duration_min:.0f}min):")
print(f"  {'#':>3} {'W':>5} {'H':>7} {'t':>6} | {'N':>4} {'WR':>6} {'PNL':>7} | {'minFold':>7} {'F1':>6} {'F2':>6} {'F3':>6} | {'评分':>6}")
print("  " + "-" * 80)
for i, s in enumerate(scored[:15]):
    stars = "★★★" if s["minFold"] > BE else ("★★" if s["WR"] > BE else "★")
    bar = "█" * int(s["WR"] / 5)
    print(f"  {i+1:>3} {s['W']:>5} {s['H']:>7} {s['t']:>6.2f} | {s['N']:>4} {s['WR']:>5.1f}% {s['PNL']:>+6.1f} | {s['minFold']:>6.1f}% {s['folds'][0]:>5.1f}% {s['folds'][1]:>5.1f}% {s['folds'][2]:>5.1f}% | {s['score']:>5.1f} {stars} {bar}")

# 保存最优参数
best = scored[0] if scored else None
if best:
    rec = {
        "data_rows": N,
        "data_minutes": duration_min,
        "time_range": [str(t_start), str(t_end)],
        "BE": BE,
        "PAYOUT": PAYOUT,
        "H_SETTLE": H_SETTLE,
        "CD": CD,
        "best_params": {
            "W": best["W"], "H_p_up": best["H"], "tail": best["t"],
            "N_signals": best["N"], "WR": best["WR"], "PNL": best["PNL"],
            "min_fold_WR": best["minFold"], "fold_WRs": best["folds"],
            "score": best["score"],
        },
        "top5": [{"W": s["W"], "H": s["H"], "t": s["t"], "N": s["N"],
                   "WR": s["WR"], "PNL": s["PNL"], "score": s["score"],
                   "minFold": s["minFold"]} for s in scored[:5]],
    }
    with open("e:/python-binance/tmp/research_1s_final.json", "w") as f:
        json.dump(rec, f, indent=2, default=str)
    print(f"\n  ✓ 最优参数已保存至 research_1s_final.json")

print(f"\n{'='*100}")
print(f"研究完成 — {N}行数据 / {duration_min:.1f}小时")
print(f"{'='*100}")
