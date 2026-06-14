"""
97天1分钟数据 — 全局参数扫描
W_LIST=[10,15,20,30,45,60,90,120], H=10固定, TAIL_LIST=[0.10..0.35], cd=W
目标：找出哪些组合能稳定超越55.56%盈亏平衡线
"""
import math, numpy as np, pandas as pd

PAYOUT = 0.80
BE = 1.0 / (1.0 + PAYOUT) * 100  # 55.56%

# ── 加载 ──
df = pd.read_csv("e:/python-binance/data/btcusdt_1m.csv")
df["ts"] = pd.to_datetime(df["open_time"], utc=True, format="mixed")
for c in ["open", "high", "low", "close", "volume"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")
df = df.dropna(subset=["ts", "close"]).sort_values("ts").reset_index(drop=True)

close = df["close"].values.astype(float)
N = len(close)
DAYS = N / 1440.0

lr = np.log(close[1:] / close[:-1])
lr = np.where(np.isfinite(lr), lr, 0.0)

cs_lr  = np.concatenate([[0.0], np.cumsum(lr)])
cs_lr2 = np.concatenate([[0.0], np.cumsum(lr ** 2)])

def normal_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
ncdf = np.vectorize(normal_cdf)

H = 10  # 固定10分钟horizon
W_LIST  = [10, 15, 20, 30, 45, 60, 90, 120]
TAIL_LIST = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35]

max_idx = N - H
all_indices = np.arange(max_idx)  # 从0开始的全局索引

# 实际涨跌（全局）
future_close = close[all_indices + H]
current_close = close[all_indices]
actual_up = future_close > current_close

print(f"1分钟数据 | {N} bars ({DAYS:.1f}天) | H={H}min固定")
print(f"扫描: {len(W_LIST)}个W × {len(TAIL_LIST)}个tail = {len(W_LIST)*len(TAIL_LIST)}组合")
print(f"盈亏平衡线: BE={BE:.2f}%  (PAYOUT={PAYOUT})")
print("=" * 120)

# ============================================================
# 对每个W计算p_up（只依赖W），然后对所有tail扫描
# ============================================================
results = []

for W in W_LIST:
    # 计算该W的mu/sigma/p_up
    idx = np.arange(W, max_idx)  # 可评估点的全局索引
    
    s  = cs_lr[idx] - cs_lr[idx - W]
    s2 = cs_lr2[idx] - cs_lr2[idx - W]
    mu = s / W
    var = np.maximum((s2 / W) - mu ** 2, 0.0) * W / (W - 1)
    sigma = np.sqrt(var)
    z = np.sqrt(H) * mu / np.maximum(sigma, 1e-10)
    p_up = ncdf(z)
    
    cd = W  # cooldown = W
    
    for tail in TAIL_LIST:
        # 生成信号
        sig = np.zeros(len(idx), dtype=np.int8)
        sig[p_up <= tail] = 1       # UP
        sig[p_up >= 1 - tail] = -1  # DN
        
        # cooldown过滤
        filtered = sig.copy()
        last_bar = -999999
        sig_pos_list = []
        for i in range(len(filtered)):
            if filtered[i] != 0:
                if idx[i] - last_bar >= cd:
                    last_bar = idx[i]
                    sig_pos_list.append(i)
                else:
                    filtered[i] = 0
        
        n_sig = len(sig_pos_list)
        
        if n_sig == 0:
            results.append({"W": W, "tail": tail, "n_sig": 0, "wr": 0, "pnl": 0, 
                            "wins": 0, "losses": 0, "up_w": 0, "up_n": 0, "dn_w": 0, "dn_n": 0})
            continue
        
        # 回测
        wins = 0
        up_w = up_n = dn_w = dn_n = 0
        
        for si in sig_pos_list:
            gidx = idx[si]
            direction = filtered[si]
            win = (close[gidx + H] > close[gidx]) if direction == 1 else (close[gidx + H] < close[gidx])
            if direction == 1:
                up_n += 1
                if win: up_w += 1
            else:
                dn_n += 1
                if win: dn_w += 1
            if win:
                wins += 1
        
        losses = n_sig - wins
        wr = wins / n_sig * 100
        pnl = wins * PAYOUT - losses * 1.0
        
        results.append({
            "W": W, "tail": tail, "n_sig": n_sig, "wr": wr, "pnl": pnl,
            "wins": wins, "losses": losses,
            "up_w": up_w, "up_n": up_n, "dn_w": dn_w, "dn_n": dn_n
        })

# ============================================================
# Part 1: 全量结果表
# ============================================================
print(f"\n{'='*120}")
print(f"【Part 1】全量结果 — {len(results)}组合")
print("-" * 120)

# 表头：tail为列，W为行
header = f"{'W(min)':>8} |"
for t in TAIL_LIST:
    header += f"  tail={t:.2f}          |"
print(header)
subhdr = f"{'':>8} |"
for t in TAIL_LIST:
    subhdr += f"  N    WR%    PNL      |"
print(subhdr)
print("-" * 120)

for W in W_LIST:
    row = f"{'W='+str(W):>8} |"
    for t in TAIL_LIST:
        r = [x for x in results if x["W"] == W and x["tail"] == t][0]
        cell = f"  {r['n_sig']:>4}  {r['wr']:>5.1f}%  {r['pnl']:>+6.1f}   "
        if r["wr"] >= BE and r["n_sig"] >= 30:
            cell = "★" + cell[1:]  # 星标
        row += cell + "|"
    print(row)

# ============================================================
# Part 2: 盈利组合排名
# ============================================================
print(f"\n{'='*120}")
print(f"【Part 2】盈利组合排名（PNL>0 且 信号≥30）")
print("-" * 120)

profitable = [r for r in results if r["pnl"] > 0 and r["n_sig"] >= 30]
profitable.sort(key=lambda x: (-x["pnl"], -x["wr"]))

if profitable:
    print(f"{'排名':>4} | {'W':>6} {'tail':>6} | {'信号':>5} {'WR':>6} {'PNL':>7} | {'UP_WT':>6} {'DN_WR':>6} | 判断")
    print("-" * 100)
    for rank, r in enumerate(profitable, 1):
        up_wr = r["up_w"] / r["up_n"] * 100 if r["up_n"] > 0 else 0
        dn_wr = r["dn_w"] / r["dn_n"] * 100 if r["dn_n"] > 0 else 0
        margin = r["wr"] - BE
        note = ""
        if r["wr"] >= 60: note = "显著盈利"
        elif r["wr"] >= 57: note = "边际盈利"
        else: note = "微利"
        print(f"  {rank:>2}  | W={r['W']:>3} {r['tail']:>5.2f} | {r['n_sig']:>5} {r['wr']:>5.1f}% {r['pnl']:>+6.1f} | {up_wr:>5.1f}% {dn_wr:>5.1f}% | {note} (边际={margin:+.1f}%)")
else:
    print("  没有组合满足 PNL>0 且 信号≥30")

# ============================================================
# Part 3: 按W聚合（看哪个窗口系统性占优）
# ============================================================
print(f"\n{'='*120}")
print(f"【Part 3】按W聚合 — 哪个窗口宽度系统性占优？")
print("-" * 120)

print(f"{'W':>6} | {'平均WR':>7} {'中位WR':>7} {'平均PNL':>8} {'盈利组合':>8} {'总组合':>6} | {'W表现':>10}")
print("-" * 80)
for W in W_LIST:
    w_res = [r for r in results if r["W"] == W]
    wrs = [r["wr"] for r in w_res]
    pnls = [r["pnl"] for r in w_res]
    avg_wr = np.mean(wrs)
    med_wr = np.median(wrs)
    avg_pnl = np.mean(pnls)
    n_prof = sum(1 for r in w_res if r["pnl"] > 0 and r["n_sig"] >= 30)
    
    if avg_wr >= BE + 1: verdict = "系统性占优"
    elif avg_wr >= BE - 0.5: verdict = "≈平衡线"
    else: verdict = "系统性劣势"
    
    print(f" W={W:>3} | {avg_wr:>6.1f}% {med_wr:>6.1f}% {avg_pnl:>+7.1f} {n_prof:>6}/{len(w_res)} | {verdict}")

# ============================================================
# Part 4: 按tail聚合
# ============================================================
print(f"\n{'='*120}")
print(f"【Part 4】按tail聚合 — 哪个尾部阈值系统性占优？")
print("-" * 120)

print(f"{'tail':>6} | {'平均WR':>7} {'中位WR':>7} {'平均PNL':>8} {'盈利组合':>8} {'平均信号':>8} | {'表现':>10}")
print("-" * 90)
for t in TAIL_LIST:
    t_res = [r for r in results if r["tail"] == t]
    wrs = [r["wr"] for r in t_res]
    pnls = [r["pnl"] for r in t_res]
    nsigs = [r["n_sig"] for r in t_res]
    avg_wr = np.mean(wrs)
    med_wr = np.median(wrs)
    avg_pnl = np.mean(pnls)
    avg_sig = np.mean(nsigs)
    n_prof = sum(1 for r in t_res if r["pnl"] > 0 and r["n_sig"] >= 30)
    
    if avg_wr >= BE + 1: verdict = "系统性占优"
    elif avg_wr >= BE - 0.5: verdict = "≈平衡线"
    else: verdict = "系统性劣势"
    
    print(f" {t:>5.2f} | {avg_wr:>6.1f}% {med_wr:>6.1f}% {avg_pnl:>+7.1f} {n_prof:>6}/{len(t_res)} {avg_sig:>8.0f} | {verdict}")

# ============================================================
# Part 5: 最佳组合深度拆分
# ============================================================
print(f"\n{'='*120}")
print(f"【Part 5】最佳组合深度拆分")
print("-" * 120)

# 选PNL最高且信号≥50的组合
deep = [r for r in results if r["n_sig"] >= 50]
if not deep:
    deep = [r for r in results if r["n_sig"] >= 30]
deep.sort(key=lambda x: (-x["pnl"], -x["wr"]))
best = deep[0] if deep else results[0]

print(f"最佳组合: W={best['W']}min, tail={best['tail']}, cd={best['W']}min")
print(f"  信号: {best['n_sig']} ({best['wins']}W/{best['losses']}L)")
print(f"  WR: {best['wr']:.1f}% (BE={BE:.2f}%, 边际={best['wr']-BE:+.2f}%)")
print(f"  PNL: {best['pnl']:+.1f}")
print(f"  UP: {best['up_n']}笔, WR={best['up_w']/max(best['up_n'],1)*100:.1f}%")
print(f"  DN: {best['dn_n']}笔, WR={best['dn_w']/max(best['dn_n'],1)*100:.1f}%")

# ============================================================
# Part 6: 热力图（W × tail 的WR矩阵）
# ============================================================
print(f"\n{'='*120}")
print(f"【Part 6】WR热力图 (★=盈利, ✗=亏损)")
print("-" * 120)

print(f"{'':>8}", end="")
for t in TAIL_LIST:
    print(f" | tail={t:.2f}", end="")
print()
print("-" * (10 + len(TAIL_LIST) * 14))

for W in W_LIST:
    print(f"W={W:>4}", end="")
    for t in TAIL_LIST:
        r = [x for x in results if x["W"] == W and x["tail"] == t][0]
        wr = r["wr"]
        if wr >= 58: sym = "★"
        elif wr >= BE: sym = "✓"
        elif wr >= 53: sym = "~"
        else: sym = "✗"
        print(f" |  {wr:>5.1f}%{sym}", end="")
    print()

print(f"\n  ★=WR≥58%  ✓=WR≥{BE:.1f}%  ~=WR≥53%  ✗=WR<53%")

# ============================================================
# Part 7: 信号量 vs WR散点
# ============================================================
print(f"\n{'='*120}")
print(f"【Part 7】信号量 vs 胜率（发现过拟合区域）")
print("-" * 120)

print(f"{'W':>5} {'tail':>5} | {'信号':>5} {'WR':>6} {'PNL':>7} | {'信号/天':>7} | 判断")
print("-" * 70)
for r in sorted(results, key=lambda x: -x["n_sig"]):
    if r["n_sig"] < 30:
        continue
    sig_per_day = r["n_sig"] / DAYS
    if r["wr"] >= BE and r["pnl"] > 0:
        note = "★ 有效"
    elif r["wr"] >= BE:
        note = "~ 持平"
    else:
        note = "✗ 无效"
    print(f"W={r['W']:>3} {r['tail']:>5.2f} | {r['n_sig']:>5} {r['wr']:>5.1f}% {r['pnl']:>+6.1f} | {sig_per_day:>6.1f}/d | {note}")

print(f"\n{'='*120}")
print(f"【总结】")
n_prof_total = sum(1 for r in results if r["pnl"] > 0 and r["n_sig"] >= 30)
n_total = sum(1 for r in results if r["n_sig"] >= 30)
print(f"  有效组合数: {n_prof_total}/{n_total} ({n_prof_total/max(n_total,1)*100:.0f}%)")
if profitable:
    best_pnl = max(profitable, key=lambda x: x["pnl"])
    print(f"  最佳PNL: W={best_pnl['W']}, tail={best_pnl['tail']} → WR={best_pnl['wr']:.1f}%, PNL={best_pnl['pnl']:+.1f}, {best_pnl['n_sig']}信号")
    print(f"  最佳边际: WR-{BE:.2f}={best_pnl['wr']-BE:+.2f}%")
print(f"  关键问题: {'存在系统性占优区域' if n_prof_total > n_total * 0.4 else '大部分组合在平衡线上下浮动，无系统性占优'}")
