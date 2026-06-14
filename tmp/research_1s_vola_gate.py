"""
波动率门控研究
================
核心问题: 信号前波动率能否过滤低质量信号?

波动率定义(只用过去数据):
  vola(t) = std(log_return[t-W_vola : t]) × sqrt(365×24×3600) × 1e4  (年化bps)
  或简化: vola(t) = std(log_return[t-W_vola : t]) × sqrt(60) × 1e4    (bps/min)

1. 波动率分布: 信号时刻 vs 全局
2. 多窗口波动率与胜率: 60s/120s/300s/600s
3. 阈值扫描: 从1到10 bps/min
4. 最优门控效果: WR提升 vs 信号损失
5. 时间分段稳健性: 前半段 vs 后半段
6. 波动率×趋势强度交叉
7. 波动率与p_up的交互
"""
import math, numpy as np, pandas as pd

PAYOUT = 0.80
BE = 1.0 / (1.0 + PAYOUT) * 100
H_SETTLE = 600
CD = 600

df = pd.read_csv("e:/python-binance/tmp/server_1s_trades.csv")
df["ts"] = pd.to_datetime(df["timestamp"], utc=True)
df = df.sort_values("ts").reset_index(drop=True)
close = df["close"].values.astype(float)
volume = df["volume"].values.astype(float)
N = len(close)

lr = np.log(close[1:] / close[:-1])
lr = np.where(np.isfinite(lr), lr, 0.0)
cs_lr  = np.concatenate([[0.0], np.cumsum(lr)])
cs_lr2 = np.concatenate([[0.0], np.cumsum(lr ** 2)])
ncdf = np.vectorize(lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0))))
max_eval = N - H_SETTLE

# ============================================================
# 预计算: 多窗口波动率 (只用过去数据)
# ============================================================
# vola_w[t] = std(lr[t-w : t]) * sqrt(60) * 1e4  (bps/min)
VOLA_WINDOWS = [30, 60, 120, 300, 600]
vola = {}
for w in VOLA_WINDOWS:
    v_arr = np.zeros(N)
    for t in range(w, N):
        seg = lr[t - w:t]
        v_arr[t] = np.std(seg) * math.sqrt(60) * 1e4
    vola[w] = v_arr

# 预计算: 趋势 (300s窗口, 用于交叉分析)
trend_300 = np.zeros(N)
for t in range(300, N):
    trend_300[t] = (close[t] / close[t - 300] - 1) * 10000

COMBOS = [
    (120, 900, 0.05),
    (300, 60, 0.25),
    (300, 120, 0.15),
    (600, 300, 0.10),
    (300, 300, 0.20),
]

def get_signals(W, H_p_up, tail):
    indices = np.arange(W, max_eval)
    s  = cs_lr[indices] - cs_lr[indices - W]
    s2 = cs_lr2[indices] - cs_lr2[indices - W]
    mu = s / W
    var = np.maximum((s2 / W) - mu**2, 0.0) * W / (W - 1)
    sigma = np.sqrt(var)
    z = np.sqrt(H_p_up) * mu / np.maximum(sigma, 1e-10)
    p_up = ncdf(z)
    sig = np.zeros(len(indices), dtype=np.int8)
    sig[p_up <= tail] = 1
    sig[p_up >= 1-tail] = -1
    filtered = sig.copy()
    last_bar = -99999
    sig_pos = []
    for i in range(len(filtered)):
        if filtered[i] != 0:
            if indices[i] - last_bar >= CD:
                last_bar = indices[i]
                sig_pos.append(i)
            else:
                filtered[i] = 0
    return [(indices[i], filtered[i], p_up[i]) for i in sig_pos]

def settle(gidx, d):
    s_idx = gidx + H_SETTLE
    if s_idx >= N:
        return None
    went_up = close[s_idx] > close[gidx]
    win = (went_up and d == 1) or (not went_up and d == -1)
    return win

# ============================================================
# 收集所有信号 + 波动率
# ============================================================
all_sigs = []
for W, H_p_up, tail in COMBOS:
    for gidx, d, p in get_signals(W, H_p_up, tail):
        r = settle(gidx, d)
        if r is not None:
            all_sigs.append({
                "gidx": gidx, "dir": d, "p_up": p, "win": r,
                "W": W, "H": H_p_up, "tail": tail,
                "ts_minute": gidx // 60,
            })

# 去重
seen = set()
unique_sigs = []
for s in all_sigs:
    if s["gidx"] not in seen:
        seen.add(s["gidx"])
        for w in VOLA_WINDOWS:
            s[f"vola_{w}"] = vola[w][s["gidx"]]
        s["trend_300"] = trend_300[s["gidx"]]
        unique_sigs.append(s)

n_total = len(unique_sigs)
wr_all = sum(s["win"] for s in unique_sigs) / n_total * 100
pnl_all = sum(1 for s in unique_sigs if s["win"]) * PAYOUT - sum(1 for s in unique_sigs if not s["win"]) * 1.0

print(f"数据: {N}行, {N/60:.0f}分钟")
print(f"信号: {n_total}个 (去重), 整体WR={wr_all:.1f}%, PNL={pnl_all:+.1f}, BE={BE:.2f}%")
print("=" * 110)

# ============================================================
# Part 1: 波动率分布 — 信号时刻 vs 全局
# ============================================================
print("\n【Part 1】波动率分布 — 信号时刻 vs 全局")
print("-" * 110)

for w in VOLA_WINDOWS:
    global_vola = vola[w][w:]
    sig_vola = np.array([s[f"vola_{w}"] for s in unique_sigs])
    
    print(f"\n  波动率窗口={w}s:")
    print(f"    {'':>10} | {'P10':>6} {'P25':>6} {'P50':>6} {'P75':>6} {'P90':>6} | {'均值':>6} {'std':>6}")
    print(f"    {'全局':>10} |", end="")
    for p in [10, 25, 50, 75, 90]:
        print(f" {np.percentile(global_vola, p):>5.1f}", end="")
    print(f" | {np.mean(global_vola):>5.1f} {np.std(global_vola):>5.1f}")
    print(f"    {'信号时刻':>10} |", end="")
    for p in [10, 25, 50, 75, 90]:
        print(f" {np.percentile(sig_vola, p):>5.1f}", end="")
    print(f" | {np.mean(sig_vola):>5.1f} {np.std(sig_vola):>5.1f}")

# ============================================================
# Part 2: 波动率分位 vs 胜率
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 2】波动率分位 vs 胜率 — 哪个窗口最能区分胜负?")
print("-" * 110)

for w in VOLA_WINDOWS:
    key = f"vola_{w}"
    vola_vals = np.array([s[key] for s in unique_sigs])
    sorted_idx = np.argsort(vola_vals)
    n = len(unique_sigs)
    
    print(f"\n  波动率窗口={w}s (bps/min), 按5等分:")
    print(f"    {'分位':>10} | {'vola范围':>18} | {'N':>4} {'WR':>6} {'PNL':>7} | {'做多占比':>8} | {'avg_p_up':>9}")
    print("    " + "-" * 80)
    
    for q in range(5):
        lo = q * n // 5
        hi = (q + 1) * n // 5
        seg = [unique_sigs[i] for i in sorted_idx[lo:hi]]
        seg_n = len(seg)
        if seg_n == 0:
            continue
        wins = sum(1 for s in seg if s["win"])
        wr = wins / seg_n * 100
        pnl = wins * PAYOUT - (seg_n - wins) * 1.0
        v_lo = vola_vals[sorted_idx[lo]]
        v_hi = vola_vals[sorted_idx[hi - 1]]
        up_pct = sum(1 for s in seg if s["dir"] == 1) / seg_n * 100
        avg_p = np.mean([s["p_up"] for s in seg])
        bar = "█" * int(wr / 4)
        print(f"    {'Q'+str(q+1):>10} | [{v_lo:>6.1f}, {v_hi:>6.1f}] | {seg_n:>4} {wr:>5.1f}% {pnl:>+6.1f} | {up_pct:>6.1f}% | {avg_p:>8.3f} {bar}")

# ============================================================
# Part 3: 阈值扫描 — vola > threshold 才入场
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 3】阈值扫描 — 波动率>v阈值才入场")
print("-" * 110)

THRESHOLDS = [0, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0, 7.0]

for w in [60, 120, 300]:
    key = f"vola_{w}"
    print(f"\n  波动率窗口={w}s:")
    print(f"    {'阈值':>6} | {'入场':>4} {'跳过':>4} | {'WR':>6} {'PNL':>7} {'avg_dev':>8} | {'跳过WR':>7} {'跳过PNL':>8} | {'判定':>10}")
    print("    " + "-" * 85)
    
    for thr in THRESHOLDS:
        gated = [s for s in unique_sigs if s[key] >= thr]
        skipped = [s for s in unique_sigs if s[key] < thr]
        g_n = len(gated)
        s_n = len(skipped)
        if g_n == 0:
            continue
        g_wins = sum(1 for s in gated if s["win"])
        g_wr = g_wins / g_n * 100
        g_pnl = g_wins * PAYOUT - (g_n - g_wins) * 1.0
        s_wr = sum(1 for s in skipped if s["win"]) / s_n * 100 if s_n > 0 else 0
        s_pnl = sum(1 for s in skipped if s["win"]) * PAYOUT - sum(1 for s in skipped if not s["win"]) * 1.0 if s_n > 0 else 0
        verdict = "✓有效" if (s_wr < BE and g_wr > wr_all) else ("?中性" if g_n > 0 else "✗")
        bar = "█" * int(g_wr / 4)
        thr_str = f">{thr:.1f}" if thr > 0 else "全部"
        print(f"    {thr_str:>6} | {g_n:>4} {s_n:>4} | {g_wr:>5.1f}% {g_pnl:>+6.1f} {g_wr-g_wr:>8} | {s_wr:>6.1f}% {s_pnl:>+7.1f} | {verdict:>10} {bar}")

# ============================================================
# Part 4: 最优门控详细分析
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 4】最优门控详细分析")
print("-" * 110)

# 找每个窗口的最优阈值(最大化WR, 要求N>=20)
print(f"\n  {'窗口':>6} | {'最优阈值':>8} | {'入场':>4} {'WR':>6} {'PNL':>7} | {'跳过':>4} {'跳过WR':>7} | {'WR提升':>7}")
print("  " + "-" * 70)

best_configs = []
for w in VOLA_WINDOWS:
    key = f"vola_{w}"
    best_thr = 0
    best_wr = wr_all
    best_pnl = pnl_all
    best_skip_wr = 0
    best_n = n_total
    
    for thr in np.arange(0, 8.0, 0.5):
        gated = [s for s in unique_sigs if s[key] >= thr]
        skipped = [s for s in unique_sigs if s[key] < thr]
        g_n = len(gated)
        if g_n < 15:
            continue
        g_wins = sum(1 for s in gated if s["win"])
        g_wr = g_wins / g_n * 100
        g_pnl = g_wins * PAYOUT - (g_n - g_wins) * 1.0
        s_wr = sum(1 for s in skipped if s["win"]) / len(skipped) * 100 if skipped else 0
        
        if g_wr > best_wr and s_wr < g_wr:
            best_thr = thr
            best_wr = g_wr
            best_pnl = g_pnl
            best_skip_wr = s_wr
            best_n = g_n
    
    delta = best_wr - wr_all
    bar = "█" * int(best_wr / 4)
    print(f"  {w:>4}s | >{best_thr:>5.1f}  | {best_n:>4} {best_wr:>5.1f}% {best_pnl:>+6.1f} | {n_total-best_n:>4} {best_skip_wr:>6.1f}% | {delta:>+6.1f}% {bar}")
    best_configs.append((w, best_thr))

# ============================================================
# Part 5: 时间分段稳健性 — 前半 vs 后半
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 5】时间分段稳健性 — 前半段 vs 后半段")
print("-" * 110)

mid_point = N // 2
sigs_first = [s for s in unique_sigs if s["gidx"] < mid_point]
sigs_second = [s for s in unique_sigs if s["gidx"] >= mid_point]

print(f"\n  数据分段: 前半={len(sigs_first)}信号({mid_point//60}min), 后半={len(sigs_second)}信号({(N-mid_point)//60}min)")

for w, thr in best_configs:
    key = f"vola_{w}"
    print(f"\n  波动率窗口={w}s, 阈值>{thr:.1f}:")
    print(f"    {'段':>6} | {'全部N':>5} {'全部WR':>7} | {'门控N':>5} {'门控WR':>7} {'跳过N':>5} {'跳过WR':>7} | {'一致?':>6}")
    print("    " + "-" * 75)
    
    for label, seg in [("前半", sigs_first), ("后半", sigs_second)]:
        all_n = len(seg)
        all_wr = sum(1 for s in seg if s["win"]) / all_n * 100 if all_n > 0 else 0
        gated = [s for s in seg if s[key] >= thr]
        skipped = [s for s in seg if s[key] < thr]
        g_wr = sum(1 for s in gated if s["win"]) / len(gated) * 100 if gated else 0
        s_wr = sum(1 for s in skipped if s["win"]) / len(skipped) * 100 if skipped else 0
        consistent = "✓" if (g_wr > all_wr and len(gated) >= 5) else ("?" if len(gated) < 5 else "✗")
        print(f"    {label:>6} | {all_n:>5} {all_wr:>6.1f}% | {len(gated):>5} {g_wr:>6.1f}% {len(skipped):>5} {s_wr:>6.1f}% | {consistent:>6}")

# ============================================================
# Part 6: 波动率 × 趋势强度交叉
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 6】波动率 × 趋势强度交叉")
print("-" * 110)

w_vola = 120
key_vola = f"vola_{w_vola}"
trend_threshold = 5.0  # bps

print(f"\n  波动率窗口={w_vola}s, 趋势窗口=300s(|trend|>{trend_threshold}bps=强趋势)")
print(f"    {'类别':>16} | {'N':>4} {'WR':>6} {'PNL':>7} | {'说明':>25}")
print("    " + "-" * 65)

cats = [
    ("高波动+强趋势", [s for s in unique_sigs if s[key_vola] >= 3.0 and abs(s["trend_300"]) >= trend_threshold]),
    ("高波动+弱趋势", [s for s in unique_sigs if s[key_vola] >= 3.0 and abs(s["trend_300"]) < trend_threshold]),
    ("低波动+强趋势", [s for s in unique_sigs if s[key_vola] < 3.0 and abs(s["trend_300"]) >= trend_threshold]),
    ("低波动+弱趋势", [s for s in unique_sigs if s[key_vola] < 3.0 and abs(s["trend_300"]) < trend_threshold]),
]

for label, cat in cats:
    n = len(cat)
    if n == 0:
        print(f"    {label:>16} | {0:>4}    ---")
        continue
    wins = sum(1 for s in cat if s["win"])
    wr = wins / n * 100
    pnl = wins * PAYOUT - (n - wins) * 1.0
    desc = f"vola{'≥' if '高' in label else '<'}3.0 & |trend|{'≥' if '强' in label else '<'}{trend_threshold}"
    bar = "█" * int(wr / 4)
    print(f"    {label:>16} | {n:>4} {wr:>5.1f}% {pnl:>+6.1f} | {desc:>25} {bar}")

# ============================================================
# Part 7: 波动率与p_up的交互 (全样本)
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 7】波动率与p_up的交互 — 高/低波动下p_up校准")
print("-" * 110)

W_model = 300
H_p_up = 60
indices_all = np.arange(W_model, max_eval)
s_all = cs_lr[indices_all] - cs_lr[indices_all - W_model]
s2_all = cs_lr2[indices_all] - cs_lr2[indices_all - W_model]
mu_all = s_all / W_model
var_all = np.maximum((s2_all / W_model) - mu_all**2, 0.0) * W_model / (W_model - 1)
sigma_all = np.sqrt(var_all)
z = np.sqrt(H_p_up) * mu_all / np.maximum(sigma_all, 1e-10)
p_up_all = ncdf(z)

settle_up = np.array([close[i + H_SETTLE] > close[i] for i in indices_all])
vola_at_signal = vola[120][indices_all]  # 用120s波动率

# 中位数分高低波动
vola_median = np.median(vola_at_signal)
high_vola = vola_at_signal >= vola_median
low_vola = vola_at_signal < vola_median

print(f"\n  W=300 H_p_up=60s, 波动率窗口=120s, 中位数={vola_median:.1f}bps/min")
print(f"  {'波动率':>8} | {'p_up区间':>12} | {'样本':>6} {'实际涨%':>8} {'模型%':>7} {'偏差':>7} | {'含义':>10}")
print("  " + "-" * 75)

for vola_label, vola_mask in [("高波动", high_vola), ("低波动", low_vola)]:
    for p_lo, p_hi, p_label in [(0, 0.05, "≤0.05"), (0.05, 0.10, "0.05-0.10"), (0.10, 0.90, "0.10-0.90"), (0.90, 0.95, "0.90-0.95"), (0.95, 1.01, "≥0.95")]:
        mask = vola_mask & (p_up_all >= p_lo) & (p_up_all < p_hi)
        n = np.sum(mask)
        if n < 10:
            continue
        actual_up = np.mean(settle_up[mask]) * 100
        model_p = np.mean(p_up_all[mask]) * 100
        bias = actual_up - model_p
        meaning = "DN" if p_hi <= 0.11 else ("UP" if p_lo >= 0.90 else "中间")
        bar = "█" * int(abs(bias) / 5)
        print(f"  {vola_label:>8} | {p_label:>12} | {n:>6} {actual_up:>7.1f}% {model_p:>6.1f}% {bias:>+6.1f} | {meaning:>10} {bar}")

# ============================================================
# Part 8: 逐笔波动率明细
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 8】逐笔波动率明细 (W=300 H=60 t=0.25)")
print("-" * 110)

W, H_p_up, tail = 300, 60, 0.25
sigs = get_signals(W, H_p_up, tail)

print(f"\n  {'#':>3} {'时刻':>8} {'方向':>4} {'赢':>3} | {'v60':>5} {'v120':>5} {'v300':>5} {'v600':>5} | {'trend300':>8} | {'波动判定':>8}")
print("  " + "-" * 80)

for i, (gidx, d, p) in enumerate(sigs):
    r = settle(gidx, d)
    win = r if r is not None else False
    v60 = vola[60][gidx]
    v120 = vola[120][gidx]
    v300 = vola[300][gidx]
    v600 = vola[600][gidx]
    t300 = trend_300[gidx]
    ts_str = f"{gidx//3600}h{(gidx%3600)//60:02d}m"
    dir_str = "↑多" if d == 1 else "↓空"
    win_str = "✓" if win else "✗"
    v_judge = "高波动" if v120 >= 3.0 else "低波动"
    print(f"  {i+1:>3} {ts_str:>8} {dir_str:>4} {win_str:>3} | {v60:>4.1f} {v120:>4.1f} {v300:>4.1f} {v600:>4.1f} | {t300:>+7.1f} | {v_judge:>8}")
