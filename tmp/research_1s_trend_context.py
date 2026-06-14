"""
信号方向 vs 趋势上下文分析
============================
核心问题: DN信号更强是策略特征还是趋势噪声?

1. 数据段趋势画像: 这3.7小时BTC在怎么走?
2. 趋势定义: 多窗口滚动收益 (60s/300s/600s/1800s)
3. 信号方向 vs 趋势: 同向(顺势) vs 反向(逆势)
4. 胜率分解: 顺势信号WR vs 逆势信号WR
5. 趋势强度分位: 弱趋势 vs 强趋势下信号表现
6. 动态趋势过滤可行性: 只做顺势/逆势信号的效果
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

# 趋势指标: 多窗口滚动收益(bps)
TREND_WINDOWS = [60, 300, 600, 1800, 3600]
trend_ret = {}
for tw in TREND_WINDOWS:
    ret = np.zeros(N)
    for i in range(tw, N):
        ret[i] = (close[i] / close[i - tw] - 1) * 10000  # bps
    trend_ret[tw] = ret

print(f"数据: {N}行, {N/60:.0f}分钟")
print("=" * 110)

# ============================================================
# Part 1: 数据段趋势画像
# ============================================================
print("\n【Part 1】趋势画像 — 这3.7小时BTC怎么走的?")
print("-" * 110)

# 价格区间
p_start = close[0]
p_end = close[-1]
p_max = close.max()
p_min = close.min()
p_max_idx = np.argmax(close)
p_min_idx = np.argmin(close)

print(f"  起: {p_start:.1f}  止: {p_end:.1f}  区间: [{p_min:.1f}, {p_max:.1f}]")
print(f"  总涨跌: {(p_end/p_start-1)*10000:+.1f} bps ({(p_end/p_start-1)*100:+.2f}%)")
print(f"  最高点: {p_max:.1f} @ {p_max_idx//3600}h{(p_max_idx%3600)//60:02d}m")
print(f"  最低点: {p_min:.1f} @ {p_min_idx//3600}h{(p_min_idx%3600)//60:02d}m")

# 每10分钟一段的趋势
print(f"\n  {'时段':>10} | {'起价':>8} {'止价':>8} | {'涨跌bps':>8} | {'方向':>6}")
print("  " + "-" * 50)
for t in range(0, N - 600, 600):
    seg_start = close[t]
    seg_end = close[min(t + 600, N - 1)]
    seg_ret = (seg_end / seg_start - 1) * 10000
    direction = "↑涨" if seg_ret > 0 else "↓跌"
    ts_str = f"{t//3600}h{(t%3600)//60:02d}m"
    bar = "█" * min(int(abs(seg_ret) / 5), 30)
    print(f"  {ts_str:>10} | {seg_start:>8.1f} {seg_end:>8.1f} | {seg_ret:>+7.1f} | {direction:>6} {bar}")

# ============================================================
# Part 2: 多窗口趋势分布
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 2】多窗口趋势分布")
print("-" * 110)

print(f"  {'窗口':>8} | {'均值bps':>8} {'中位数':>8} {'std':>6} | {'>0占比':>6} {'强涨(>10bps)':>12} {'强跌(<-10bps)':>13}")
print("  " + "-" * 75)
for tw in TREND_WINDOWS:
    valid = trend_ret[tw][tw:]
    mean_r = np.mean(valid)
    med_r = np.median(valid)
    std_r = np.std(valid)
    pos_pct = np.mean(valid > 0) * 100
    strong_up = np.mean(valid > 10) * 100
    strong_dn = np.mean(valid < -10) * 100
    print(f"  {tw:>4}s({tw//60}m) | {mean_r:>+7.1f} {med_r:>+7.1f} {std_r:>5.1f} | {pos_pct:>5.1f}% {strong_up:>10.1f}% {strong_dn:>11.1f}%")

# ============================================================
# Part 3: 信号方向 vs 趋势方向 — 顺势 vs 逆势
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 3】信号方向 vs 趋势方向 — 顺势信号 vs 逆势信号胜率")
print("-" * 110)

# 合并所有组合的信号(去重)
all_sigs = []
for W, H_p_up, tail in COMBOS:
    sigs = get_signals(W, H_p_up, tail)
    for gidx, d, p in sigs:
        all_sigs.append({"gidx": gidx, "dir": d, "p_up": p, "W": W, "H": H_p_up, "tail": tail,
                        "win": settle(gidx, d)})

# 去重 (按gidx)
seen = set()
unique_sigs = []
for s in all_sigs:
    if s["gidx"] not in seen and s["win"] is not None:
        seen.add(s["gidx"])
        unique_sigs.append(s)

n_total = len(unique_sigs)
print(f"合并去重后: {n_total}信号\n")

for tw in TREND_WINDOWS:
    print(f"\n  趋势窗口 = {tw}s({tw//60}min)")
    print(f"  {'类别':>12} | {'N':>4} {'WR':>6} | {'avg_trend':>10} | {'avg_dev信号方向':>14}")
    print("  " + "-" * 65)
    
    for s in unique_sigs:
        s["trend"] = trend_ret[tw][s["gidx"]]
        # 顺势: 信号方向与趋势一致 (做多+上涨趋势, 做空+下跌趋势)
        # 反转策略: p_up<=tail → 做多(赌反弹), p_up>=1-tail → 做空(赌回落)
        # 所以"顺势"=做多时趋势也在涨? 不对——反转策略是逆势的
        # 做多信号(p_up低)出现在下跌趋势中=经典逆势
        # 做多信号出现在上涨趋势中=顺势(但p_up低不该出现在上涨趋势...)
        # 更好的定义: 信号方向 vs 过去tw秒价格变化方向
        # trend>0(涨) + dir=1(做多) = 顺势(赌继续涨? 但p_up低说明模型看跌...)
        # 其实应该看: 信号做多时, 趋势是涨还是跌
        if s["dir"] == 1:
            s["aligned"] = s["trend"] > 0  # 做多+上涨趋势=同向
        else:
            s["aligned"] = s["trend"] < 0  # 做空+下跌趋势=同向
    
    aligned = [s for s in unique_sigs if s["aligned"]]
    counter = [s for s in unique_sigs if not s["aligned"]]
    
    up_sigs = [s for s in unique_sigs if s["dir"] == 1]
    dn_sigs = [s for s in unique_sigs if s["dir"] == -1]
    
    # 更细致: 4个类别
    cats = [
        ("做多+上涨趋势", [s for s in unique_sigs if s["dir"]==1 and s["trend"]>0]),
        ("做多+下跌趋势", [s for s in unique_sigs if s["dir"]==1 and s["trend"]<0]),
        ("做空+上涨趋势", [s for s in unique_sigs if s["dir"]==-1 and s["trend"]>0]),
        ("做空+下跌趋势", [s for s in unique_sigs if s["dir"]==-1 and s["trend"]<0]),
    ]
    
    for label, cat in cats:
        n = len(cat)
        if n == 0:
            print(f"  {label:>12} | {0:>4}    --- |          --- |            ---")
            continue
        wins = sum(1 for s in cat if s["win"])
        wr = wins / n * 100
        avg_trend = np.mean([s["trend"] for s in cat])
        avg_dir = np.mean([1 if s["dir"]==1 else -1 for s in cat])
        bar = "█" * int(wr / 4)
        print(f"  {label:>12} | {n:>4} {wr:>5.1f}% | {avg_trend:>+9.1f} | {avg_dir:>+13.1f} {bar}")
    
    # 汇总
    al_wr = sum(1 for s in aligned if s["win"]) / len(aligned) * 100 if aligned else 0
    ct_wr = sum(1 for s in counter if s["win"]) / len(counter) * 100 if counter else 0
    print(f"  {'顺势小计':>12} | {len(aligned):>4} {al_wr:>5.1f}%")
    print(f"  {'逆势小计':>12} | {len(counter):>4} {ct_wr:>5.1f}%")
    delta = al_wr - ct_wr
    print(f"  {'差异':>12} | {delta:>+5.1f}% {'(顺势更优)' if delta>0 else '(逆势更优)'}")

# ============================================================
# Part 4: 趋势强度分位 — 强趋势 vs 弱趋势
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 4】趋势强度分位 — 不同趋势强度下信号胜率")
print("-" * 110)

# 用300s窗口做主分析
tw_main = 300
trend_main = np.array([s["trend"] if "trend" in s else trend_ret[tw_main][s["gidx"]] for s in unique_sigs])
sorted_indices = np.argsort(trend_main)
n = len(unique_sigs)

print(f"\n  趋势窗口={tw_main}s, 按趋势强度5等分:")
print(f"  {'分位':>10} | {'趋势范围(bps)':>20} | {'N':>4} {'WR':>6} | {'做多占比':>8}")
print("  " + "-" * 70)

for q in range(5):
    lo = q * n // 5
    hi = (q + 1) * n // 5
    seg = [unique_sigs[i] for i in sorted_indices[lo:hi]]
    seg_n = len(seg)
    if seg_n == 0:
        continue
    wins = sum(1 for s in seg if s["win"])
    wr = wins / seg_n * 100
    t_lo = trend_main[sorted_indices[lo]]
    t_hi = trend_main[sorted_indices[hi - 1]]
    up_pct = np.mean([1 for s in seg if s["dir"] == 1]) / seg_n * 100
    bar = "█" * int(wr / 4)
    label = f"Q{q+1}({q*20}-{(q+1)*20}%)"
    print(f"  {label:>10} | [{t_lo:>+7.1f}, {t_hi:>+7.1f}] | {seg_n:>4} {wr:>5.1f}% | {up_pct:>6.1f}% {bar}")

# ============================================================
# Part 5: 单个组合趋势上下文
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 5】单组合趋势上下文 (W=300 H=60 t=0.25)")
print("-" * 110)

W, H_p_up, tail = 300, 60, 0.25
sigs = get_signals(W, H_p_up, tail)

print(f"\n  {'#':>3} {'时刻':>8} {'方向':>4} {'p_up':>6} {'赢':>3} | {'60s趋势':>8} {'300s趋势':>9} {'600s趋势':>9} | {'300s判定':>8}")
print("  " + "-" * 85)

for i, (gidx, d, p) in enumerate(sigs):
    r = settle(gidx, d)
    win = r if r is not None else False
    t60 = trend_ret[60][gidx]
    t300 = trend_ret[300][gidx]
    t600 = trend_ret[600][gidx]
    ts_str = f"{gidx//3600}h{(gidx%3600)//60:02d}m"
    dir_str = "↑多" if d == 1 else "↓空"
    win_str = "✓" if win else "✗"
    
    # 300s趋势判定
    if d == 1:
        judge = "顺势" if t300 > 0 else "逆势"
    else:
        judge = "顺势" if t300 < 0 else "逆势"
    
    print(f"  {i+1:>3} {ts_str:>8} {dir_str:>4} {p:>5.3f} {win_str:>3} | {t60:>+7.1f} {t300:>+8.1f} {t600:>+8.1f} | {judge:>8}")

# ============================================================
# Part 6: 动态趋势过滤模拟
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 6】动态趋势过滤 — 只做顺势/逆势/全部的效果")
print("-" * 110)

for tw in [60, 300, 600]:
    print(f"\n  趋势窗口={tw}s:")
    print(f"  {'过滤模式':>10} | {'入场':>4} {'跳过':>4} | {'WR':>6} {'PNL':>7} | {'说明':>30}")
    print("  " + "-" * 75)
    
    for mode in ["全部", "只顺势", "只逆势", "强趋势", "弱趋势"]:
        wins = 0
        valid = 0
        skip = 0
        for s in unique_sigs:
            t = trend_ret[tw][s["gidx"]]
            aligned = (s["dir"] == 1 and t > 0) or (s["dir"] == -1 and t < 0)
            abs_t = abs(t)
            
            if mode == "只顺势" and not aligned:
                skip += 1
                continue
            if mode == "只逆势" and aligned:
                skip += 1
                continue
            if mode == "强趋势" and abs_t < 5:
                skip += 1
                continue
            if mode == "弱趋势" and abs_t >= 5:
                skip += 1
                continue
            
            valid += 1
            if s["win"]:
                wins += 1
        
        wr = wins / valid * 100 if valid > 0 else 0
        pnl = wins * PAYOUT - (valid - wins) * 1.0
        desc = {"全部":"不过滤", "只顺势":"信号方向=趋势方向",
                "只逆势":"信号方向≠趋势方向", "强趋势":"|trend|>5bps",
                "弱趋势":"|trend|<5bps"}[mode]
        bar = "█" * int(wr / 4)
        print(f"  {mode:>10} | {valid:>4} {skip:>4} | {wr:>5.1f}% {pnl:>+6.1f} | {desc:>30} {bar}")

# ============================================================
# Part 7: 趋势与p_up的交互
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 7】趋势与p_up的交互 — 不同趋势下p_up的预测准确度")
print("-" * 110)

# 全样本p_up校准, 分趋势上下
indices_all = np.arange(300, max_eval)
trend_300 = trend_ret[300][indices_all]

# 信号时刻的p_up
W = 300
s_all = cs_lr[indices_all] - cs_lr[indices_all - W]
s2_all = cs_lr2[indices_all] - cs_lr2[indices_all - W]
mu_all = s_all / W
var_all = np.maximum((s2_all / W) - mu_all**2, 0.0) * W / (W - 1)
sigma_all = np.sqrt(var_all)

for H_p_up in [60, 300]:
    z = np.sqrt(H_p_up) * mu_all / np.maximum(sigma_all, 1e-10)
    p_up_all = ncdf(z)
    
    # 结算方向
    settle_up = np.array([close[i + H_SETTLE] > close[i] for i in indices_all])
    
    print(f"\n  W=300 H_p_up={H_p_up}s, 按300s趋势分上下:")
    print(f"  {'趋势':>8} | {'p_up区间':>12} | {'样本':>6} {'实际涨%':>8} {'模型%':>7} {'偏差':>8} | {'含义':>20}")
    print("  " + "-" * 85)
    
    for trend_label, trend_mask in [
        ("强涨>10bps", trend_300 > 10),
        ("弱涨0-10bps", (trend_300 > 0) & (trend_300 <= 10)),
        ("弱跌-10~0", (trend_300 < 0) & (trend_300 >= -10)),
        ("强跌<-10bps", trend_300 < -10),
    ]:
        for p_lo, p_hi, p_label in [(0, 0.10, "≤0.10"), (0.10, 0.90, "0.10-0.90"), (0.90, 1.01, "≥0.90")]:
            mask = trend_mask & (p_up_all >= p_lo) & (p_up_all < p_hi)
            n = np.sum(mask)
            if n < 10:
                continue
            actual_up = np.mean(settle_up[mask]) * 100
            model_p = np.mean(p_up_all[mask]) * 100
            bias = actual_up - model_p
            meaning = "DN信号" if p_hi <= 0.11 else ("UP信号" if p_lo >= 0.90 else "中间区")
            bar = "█" * int(abs(bias) / 4)
            print(f"  {trend_label:>8} | {p_label:>12} | {n:>6} {actual_up:>7.1f}% {model_p:>6.1f}% {bias:>+7.1f} | {meaning:>20} {bar}")
