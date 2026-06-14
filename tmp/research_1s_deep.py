"""
深入研究: 信号聚合投票 + W=600深度 + cooldown敏感度
====================================================
关键发现: 单参数组合信号太少(12-27个), OOS失败率高
方向: 多参数投票增加信号密度 + 找最稳健的单参数家族
"""
import math, json, numpy as np, pandas as pd
from datetime import datetime, timezone

PAYOUT = 0.80
BE = 1.0 / (1.0 + PAYOUT) * 100
H_SETTLE = 600
CD = 600

df = pd.read_csv("e:/python-binance/tmp/server_1s_trades.csv")
df["ts"] = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601")
df = df.sort_values("ts").reset_index(drop=True)
close = df["close"].values.astype(float)
N = len(close)
print(f"数据: {N}行 ({N/60:.0f}min, {N/3600:.1f}h)")

lr = np.log(close[1:] / close[:-1])
lr = np.where(np.isfinite(lr), lr, 0.0)
cs_lr  = np.concatenate([[0.0], np.cumsum(lr)])
cs_lr2 = np.concatenate([[0.0], np.cumsum(lr ** 2)])
ncdf = np.vectorize(lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0))))
max_eval = N - H_SETTLE

trend_300 = np.zeros(N)
for i in range(300, N):
    trend_300[i] = (close[i] / close[i - 300] - 1) * 10000

def get_signals(W, H_p_up, tail, cd=CD):
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
            if indices[i] - last_bar >= cd:
                last_bar = indices[i]
                sig_pos.append((indices[i], filtered[i], p_up[i]))
            else:
                filtered[i] = 0
    return sig_pos

def settle(gidx, d):
    s_idx = gidx + H_SETTLE
    if s_idx >= N:
        return None
    went_up = close[s_idx] > close[gidx]
    win = (went_up and d == 1) or (not went_up and d == -1)
    return win

# ============================================================
# Part 1: W=600 家族深度分析
# ============================================================
print(f"\n{'='*100}")
print(f"Part 1: W=600 家族深度分析")
print(f"{'='*100}")

print(f"\n  W=600 + 不同H和tail的组合:")
print(f"  {'H':>7} {'tail':>6} | {'信号':>4} {'WR':>6} {'PNL':>7} | {'前半WR':>7} {'后半WR':>7} | {'做多':>4} {'做空':>4} {'多WR':>6} {'空WR':>6}")
print("  " + "-" * 85)

w600_results = []
for H in [30, 60, 120, 300, 600, 900, 1200, 1800]:
    for t in [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]:
        sigs = get_signals(600, H, t)
        if len(sigs) < 8:
            continue
        results = []
        for gidx, d, p in sigs:
            r = settle(gidx, d)
            if r is not None:
                results.append((gidx, d, p, r))
        n = len(results)
        if n < 8:
            continue
        wins = sum(1 for _, _, _, w in results if w)
        wr = wins / n * 100
        pnl = wins * PAYOUT - (n - wins) * 1.0
        
        mid = N // 2
        first_wins = sum(1 for g, d, p, w in results if g < mid and w)
        first_n = sum(1 for g, d, p, w in results if g < mid)
        second_wins = sum(1 for g, d, p, w in results if g >= mid and w)
        second_n = sum(1 for g, d, p, w in results if g >= mid)
        f_wr = first_wins / first_n * 100 if first_n > 0 else 0
        s_wr = second_wins / second_n * 100 if second_n > 0 else 0
        
        ups = [r for r in results if r[1] == 1]
        dns = [r for r in results if r[1] == -1]
        up_wr = sum(1 for r in ups if r[3]) / len(ups) * 100 if ups else 0
        dn_wr = sum(1 for r in dns if r[3]) / len(dns) * 100 if dns else 0
        
        bar = "█" * int(wr / 4)
        print(f"  {H:>7} {t:>6.2f} | {n:>4} {wr:>5.1f}% {pnl:>+6.1f} | {f_wr:>6.1f}% {s_wr:>6.1f}% | {len(ups):>4} {len(dns):>4} {up_wr:>5.1f}% {dn_wr:>5.1f}% {bar}")
        w600_results.append((H, t, n, wr, pnl, f_wr, s_wr))

# ============================================================
# Part 2: Cooldown 敏感度分析
# ============================================================
print(f"\n{'='*100}")
print(f"Part 2: Cooldown 敏感度 (W=600 H=300 t=0.10)")
print(f"{'='*100}")

print(f"\n  {'CD':>5} | {'信号':>4} {'WR':>6} {'PNL':>7} | {'信号/小时':>8} | {'说明':>20}")
print("  " + "-" * 60)
for cd in [300, 600, 900, 1200, 1800, 3600]:
    sigs = get_signals(600, 300, 0.10, cd=cd)
    results = [(gidx, d, settle(gidx, d)) for gidx, d, _ in sigs]
    results = [(g, d, w) for g, d, w in results if w is not None]
    n = len(results)
    if n == 0:
        continue
    wins = sum(1 for _, _, w in results if w)
    wr = wins / n * 100
    pnl = wins * PAYOUT - (n - wins) * 1.0
    per_hour = n / (N / 3600)
    note = "cd<H_settle⚠️" if cd < H_SETTLE else ("cd=H_settle" if cd == H_SETTLE else "保守")
    bar = "█" * int(wr / 4)
    print(f"  {cd:>5} | {n:>4} {wr:>5.1f}% {pnl:>+6.1f} | {per_hour:>7.1f}/h | {note:>20} {bar}")

# ============================================================
# Part 3: 多参数投票 — 同时用多个(W,H,t)组合
# ============================================================
print(f"\n{'='*100}")
print(f"Part 3: 多参数投票策略")
print(f"{'='*100}")

# 选稳健组合做投票
VOTE_COMBOS = [
    (600, 120, 0.30),
    (600, 300, 0.20),
    (600, 600, 0.10),
    (600, 900, 0.05),
    (120, 900, 0.05),
    (120, 120, 0.25),
]

print(f"\n  投票组合: {VOTE_COMBOS}")
print(f"  策略: 在{CD}s窗口内, 多个组合产生同方向信号时入场")

# 为每个组合生成信号
all_vote_sigs = {}
for W, H, t in VOTE_COMBOS:
    sigs = get_signals(W, H, t)
    for gidx, d, p in sigs:
        key = gidx // CD  # 按CD窗口分组
        if key not in all_vote_sigs:
            all_vote_sigs[key] = []
        all_vote_sigs[key].append((gidx, d, W, H, t, p))

# 投票: 统计每个CD窗口内信号方向
print(f"\n  {'投票数':>4} {'方向':>4} | {'窗口数':>4} {'入场':>4} {'WR':>6} {'PNL':>7} | {'说明':>20}")
print("  " + "-" * 65)

for min_votes in [1, 2, 3, 4]:
    for direction_mode in ["多数", "一致"]:
        trades = []
        for key, sigs in sorted(all_vote_sigs.items()):
            ups = sum(1 for _, d, *_ in sigs if d == 1)
            dns = sum(1 for _, d, *_ in sigs if d == -1)
            total = ups + dns
            
            if direction_mode == "多数":
                if ups > dns and ups >= min_votes:
                    d = 1
                elif dns > ups and dns >= min_votes:
                    d = -1
                else:
                    continue
            else:  # 一致
                if ups == total and total >= min_votes:
                    d = 1
                elif dns == total and total >= min_votes:
                    d = -1
                else:
                    continue
            
            # 用窗口内第一个信号的gidx作为入场点
            gidx = min(g for g, _, *_ in sigs)
            r = settle(gidx, d)
            if r is not None:
                trades.append((gidx, d, r))
        
        n = len(trades)
        if n == 0:
            continue
        wins = sum(1 for _, _, w in trades if w)
        wr = wins / n * 100
        pnl = wins * PAYOUT - (n - wins) * 1.0
        desc = f">={min_votes}票{'一致' if direction_mode=='一致' else '多数'}"
        bar = "█" * int(wr / 4)
        print(f"  {min_votes:>4} {direction_mode:>4} | {len(all_vote_sigs):>4} {n:>4} {wr:>5.1f}% {pnl:>+6.1f} | {desc:>20} {bar}")

# ============================================================
# Part 4: 不同CD窗口重叠分析 — 信号时间分布
# ============================================================
print(f"\n{'='*100}")
print(f"Part 4: 信号时间分布 & 连续性")
print(f"{'='*100}")

sigs = get_signals(600, 300, 0.10)
if sigs:
    gaps = []
    for i in range(1, len(sigs)):
        gap = sigs[i][0] - sigs[i-1][0]
        gaps.append(gap / 60)  # 转分钟
    
    print(f"\n  W=600 H=300 t=0.10 信号间隔分布:")
    print(f"    信号数: {len(sigs)}")
    print(f"    平均间隔: {np.mean(gaps):.1f}min")
    print(f"    中位间隔: {np.median(gaps):.1f}min")
    print(f"    最小间隔: {np.min(gaps):.1f}min (CD={CD/60:.0f}min)")
    print(f"    最大间隔: {np.max(gaps):.1f}min")
    
    # 赢/输的聚类
    win_times = [s[0]/3600 for s in sigs if settle(s[0], s[1])]
    loss_times = [s[0]/3600 for s in sigs if settle(s[0], s[1]) is False]
    print(f"\n    赢的时间点(小时): {[f'{t:.2f}' for t in win_times]}")
    print(f"    输的时间点(小时): {[f'{t:.2f}' for t in loss_times]}")
    
    # 检查输的信号是否聚集
    if loss_times:
        print(f"\n    输信号分析:")
        print(f"      做多输: {sum(1 for s in sigs if s[1]==1 and settle(s[0],s[1]) is False)}")
        print(f"      做空输: {sum(1 for s in sigs if s[1]==-1 and settle(s[0],s[1]) is False)}")

# ============================================================
# Part 5: 价格偏移分布 — 10分钟内价格走多远
# ============================================================
print(f"\n{'='*100}")
print(f"Part 5: 10分钟价格偏移分布 (全样本)")
print(f"{'='*100}")

devs = []
for i in range(0, N - H_SETTLE, 60):  # 每分钟采样
    dev = abs(close[i + H_SETTLE] / close[i] - 1) * 10000
    devs.append(dev)

devs = np.array(devs)
print(f"\n  10分钟价格绝对偏移 (每分钟采样, {len(devs)}个点):")
print(f"    P10: {np.percentile(devs, 10):.1f}bps")
print(f"    P25: {np.percentile(devs, 25):.1f}bps")
print(f"    P50: {np.percentile(devs, 50):.1f}bps")
print(f"    P75: {np.percentile(devs, 75):.1f}bps")
print(f"    P90: {np.percentile(devs, 90):.1f}bps")
print(f"    均值: {np.mean(devs):.1f}bps")
print(f"    > 5bps: {np.mean(devs > 5)*100:.1f}%")
print(f"    >10bps: {np.mean(devs > 10)*100:.1f}%")
print(f"    >20bps: {np.mean(devs > 20)*100:.1f}%")

# 信号时刻vs随机的偏移
sig_devs = []
for W, H, t in [(600, 300, 0.10), (120, 900, 0.05)]:
    for gidx, d, p in get_signals(W, H, t):
        dev = abs(close[gidx + H_SETTLE] / close[gidx] - 1) * 10000
        sig_devs.append(dev)
if sig_devs:
    sig_devs = np.array(sig_devs)
    print(f"\n  信号时刻10分钟偏移 ({len(sig_devs)}个信号):")
    print(f"    P50: {np.percentile(sig_devs, 50):.1f}bps vs 全局{np.percentile(devs, 50):.1f}bps")
    print(f"    均值: {np.mean(sig_devs):.1f}bps vs 全局{np.mean(devs):.1f}bps")
    print(f"    → 信号时刻偏移{'更大' if np.mean(sig_devs) > np.mean(devs) else '更小'}")

# ============================================================
# Part 6: 逐笔明细 — W=600 H=300 t=0.10
# ============================================================
print(f"\n{'='*100}")
print(f"Part 6: 逐笔明细 W=600 H=300 t=0.10")
print(f"{'='*100}")

sigs = get_signals(600, 300, 0.10)
print(f"\n  {'#':>3} {'时刻':>8} {'方向':>4} {'赢':>3} {'p_up':>6} | {'trend300':>8} {'入场价':>10} {'结算价':>10} {'偏移':>6} | {'时段':>6}")
print("  " + "-" * 85)

for i, (gidx, d, p) in enumerate(sigs):
    r = settle(gidx, d)
    win = r if r is not None else False
    ts_str = f"{gidx//3600}h{(gidx%3600)//60:02d}m"
    dir_str = "↑多" if d == 1 else "↓空"
    win_str = "✓" if win else "✗"
    t300 = trend_300[gidx]
    entry = close[gidx]
    settle_price = close[min(gidx + H_SETTLE, N-1)]
    dev = (settle_price / entry - 1) * 10000
    dev_str = f"{dev:+.1f}" if d == 1 else f"{-dev:+.1f}"
    hour = gidx // 3600
    session = f"{hour}h段"
    print(f"  {i+1:>3} {ts_str:>8} {dir_str:>4} {win_str:>3} {p:>5.3f} | {t300:>+7.1f} {entry:>10.1f} {settle_price:>10.1f} {dev_str:>5}bps | {session:>6}")
