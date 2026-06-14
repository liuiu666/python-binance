"""
信号时效性与量价关系分析
==========================
核心问题: 信号触发后, 什么时间内有效, 什么时间失效?

1. 逐秒结算WR曲线: 如果在1s/2s/.../600s结算, WR是多少?
2. 入场延迟分析: 信号后延迟0s/30s/60s/120s/300s入场, WR变化
3. 赢输路径分类: 一直赢/先赢后输/先输后赢/一直输
4. 极值时间分布: 赢的信号第几分钟到峰值, 输的第几分钟穿零
5. 量价关系: 信号时刻的成交量/波动率与胜率
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
MINUTES = N / 60.0

lr = np.log(close[1:] / close[:-1])
lr = np.where(np.isfinite(lr), lr, 0.0)
cs_lr  = np.concatenate([[0.0], np.cumsum(lr)])
cs_lr2 = np.concatenate([[0.0], np.cumsum(lr ** 2)])
cs_vol = np.concatenate([[0.0], np.cumsum(volume[:-1])])
ncdf = np.vectorize(lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0))))

max_eval = N - H_SETTLE

def get_signals(W, H_p_up, tail):
    """返回信号位置列表 (cd=600过滤后)"""
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

# 选几个代表性组合
COMBOS = [
    (120, 900, 0.05),   # W=2min H_pup=15min 21信号 WR=76%
    (300, 60, 0.25),    # W=5min H_pup=1min 17信号 WR=82%
    (300, 120, 0.15),   # W=5min H_pup=2min 15信号 WR=80%
    (600, 300, 0.10),   # W=10min H_pup=5min 9信号 WR=89%
    (300, 300, 0.20),   # W=5min H_pup=5min 20信号 WR=75%
]

print(f"数据: {N}行, {MINUTES:.0f}分钟, cd={CD}s")
print(f"H_settle={H_SETTLE}s(10分钟)")
print("=" * 100)

# ============================================================
# Part 1: 逐秒结算WR曲线
# ============================================================
print(f"\n【Part 1】逐秒结算WR — 如果在t秒结算, 胜率是多少?")
print("-" * 100)

check_secs = [1, 5, 10, 30, 60, 90, 120, 180, 240, 300, 360, 420, 480, 540, 600]

for W, H_p_up, tail in COMBOS:
    sigs = get_signals(W, H_p_up, tail)
    n = len(sigs)
    if n < 3:
        continue
    
    print(f"\n  W={W}s({W//60}m) H_pup={H_p_up}s tail={tail} → {n}信号")
    print(f"  {'结算时刻':>8} | {'WR':>6} {'赢':>4} {'输':>4} | {'avg_dev(bps)':>13} | {'趋势':>20}")
    print("  " + "-" * 75)
    
    prev_wr = 0
    for sec in check_secs:
        wins = 0
        devs = []
        for gidx, d, p in sigs:
            if gidx + sec < N:
                price_now = close[gidx + sec]
                entry = close[gidx]
                went_up = price_now > entry
                win = (went_up and d == 1) or (not went_up and d == -1)
                if win:
                    wins += 1
                dev = (price_now - entry) / entry * 10000
                if d == -1:
                    dev = -dev
                devs.append(dev)
        wr = wins / n * 100
        avg_dev = np.mean(devs)
        # 趋势箭头
        arrow = ""
        if sec > 1:
            if wr > prev_wr + 3:
                arrow = "↑↑ 加速"
            elif wr > prev_wr:
                arrow = "↑ 上升"
            elif wr < prev_wr - 3:
                arrow = "↓↓ 衰减"
            elif wr < prev_wr:
                arrow = "↓ 下降"
            else:
                arrow = "→ 持平"
        prev_wr = wr
        bar = "█" * int(wr / 3)
        print(f"  {sec:>5}s({sec//60}m{sec%60:02d}s) | {wr:>5.1f}% {wins:>4} {n-wins:>4} | {avg_dev:>+11.2f} | {arrow:>20} {bar}")

# ============================================================
# Part 2: 入场延迟分析
# ============================================================
print(f"\n{'='*100}")
print(f"【Part 2】入场延迟 — 信号后等X秒再入场, 10分钟后结算, WR变化")
print("-" * 100)

delays = [0, 10, 30, 60, 120, 180, 300]

for W, H_p_up, tail in COMBOS:
    sigs = get_signals(W, H_p_up, tail)
    n = len(sigs)
    if n < 3:
        continue
    
    print(f"\n  W={W}s({W//60}m) H_pup={H_p_up}s tail={tail} → {n}信号")
    print(f"  {'延迟入场':>8} | {'有效N':>6} | {'WR':>6} {'PNL':>7} | {'avg_dev':>8} | {'变化':>8}")
    print("  " + "-" * 65)
    
    wr0 = None
    for delay in delays:
        wins = 0
        valid = 0
        devs = []
        for gidx, d, p in sigs:
            entry_idx = gidx + delay
            settle_idx = entry_idx + H_SETTLE
            if settle_idx < N:
                entry_price = close[entry_idx]
                settle_price = close[settle_idx]
                went_up = settle_price > entry_price
                win = (went_up and d == 1) or (not went_up and d == -1)
                if win:
                    wins += 1
                valid += 1
                dev = (settle_price - entry_price) / entry_price * 10000
                if d == -1:
                    dev = -dev
                devs.append(dev)
        if valid > 0:
            wr = wins / valid * 100
            pnl = wins * PAYOUT - (valid - wins) * 1.0
            avg_dev = np.mean(devs)
            change = wr - wr0 if wr0 is not None else 0
            if wr0 is None:
                wr0 = wr
            d_str = f"+{delay}s" if delay > 0 else "立即"
            print(f"  {d_str:>8} | {valid:>6} | {wr:>5.1f}% {pnl:>+6.1f} | {avg_dev:>+7.1f} | {change:>+7.1f}%")

# ============================================================
# Part 3: 价格路径分类
# ============================================================
print(f"\n{'='*100}")
print(f"【Part 3】价格路径分类 — 信号后10分钟内走势形态")
print("-" * 100)

for W, H_p_up, tail in COMBOS:
    sigs = get_signals(W, H_p_up, tail)
    n = len(sigs)
    if n < 3:
        continue
    
    # 每个信号逐60秒的方向偏离 (方向归一化)
    path_categories = {"一直赢": 0, "先赢后输": 0, "先输后赢": 0, "一直输": 0, "反复震荡": 0}
    
    all_paths = []
    for gidx, d, p in sigs:
        entry = close[gidx]
        # 每60秒采样
        devs = []
        for sec in range(0, H_SETTLE + 1, 60):
            idx = gidx + sec
            if idx < N:
                dev = (close[idx] - entry) / entry * 10000
                if d == -1:
                    dev = -dev
                devs.append(dev)
        
        if len(devs) < 2:
            continue
        all_paths.append(devs)
        
        final_dev = devs[-1]
        max_dev = max(devs)
        min_dev = min(devs)
        
        # 分类
        pos_count = sum(1 for d_val in devs if d_val > 0)
        neg_count = sum(1 for d_val in devs if d_val < 0)
        
        if final_dev > 0 and min_dev >= -1:
            path_categories["一直赢"] += 1
        elif final_dev > 0 and min_dev < -1:
            path_categories["先输后赢"] += 1
        elif final_dev < 0 and max_dev <= 1:
            path_categories["一直输"] += 1
        elif final_dev < 0 and max_dev > 1:
            path_categories["先赢后输"] += 1
        else:
            path_categories["反复震荡"] += 1
    
    print(f"\n  W={W}s({W//60}m) H_pup={H_p_up}s tail={tail} → {n}信号")
    for cat, cnt in path_categories.items():
        pct = cnt / n * 100
        bar = "█" * int(pct / 3)
        print(f"    {cat:>8}: {cnt:>3} ({pct:>4.1f}%) {bar}")
    
    # 中位数路径
    if all_paths:
        min_len = min(len(p) for p in all_paths)
        arr = np.array([p[:min_len] for p in all_paths])
        median_path = np.median(arr, axis=0)
        mean_path = np.mean(arr, axis=0)
        print(f"    中位数路径(bps): ", end="")
        for i, (med, avg) in enumerate(zip(median_path, mean_path)):
            if i % 3 == 0:
                print(f"[{i*60}s] med={med:+.1f} avg={avg:+.1f}  ", end="")
        print()

# ============================================================
# Part 4: 极值时间分析
# ============================================================
print(f"\n{'='*100}")
print(f"【Part 4】极值时间 — 赢的信号第几分钟到峰? 输的第几分钟穿零?")
print("-" * 100)

for W, H_p_up, tail in COMBOS:
    sigs = get_signals(W, H_p_up, tail)
    n = len(sigs)
    if n < 5:
        continue
    
    win_peaks = []  # 赢的信号达到最大偏移的时间
    loss_cross = [] # 输的信号首次穿零的时间
    
    for gidx, d, p in sigs:
        entry = close[gidx]
        settle = close[gidx + H_SETTLE]
        went_up = settle > entry
        win = (went_up and d == 1) or (not went_up and d == -1)
        
        # 逐秒路径
        devs = []
        for sec in range(0, H_SETTLE + 1):
            idx = gidx + sec
            if idx < N:
                dev = (close[idx] - entry) / entry * 10000
                if d == -1:
                    dev = -dev
                devs.append((sec, dev))
        
        if not devs:
            continue
        
        if win:
            # 找最大偏移的时间
            max_sec, max_dev = max(devs, key=lambda x: x[1])
            win_peaks.append(max_sec)
        else:
            # 找首次从正穿负的时间 (信号失效时刻)
            # 或者最大亏损时间
            min_sec, min_dev = min(devs, key=lambda x: x[1])
            loss_cross.append(min_sec)
    
    print(f"\n  W={W}s({W//60}m) H_pup={H_p_up}s tail={tail} → {n}信号 ({len(win_peaks)}赢/{len(loss_cross)}输)")
    if win_peaks:
        wp = np.array(win_peaks)
        print(f"    赢的峰值时间: 中位数={np.median(wp):.0f}s({np.median(wp)/60:.1f}m) 均值={np.mean(wp):.0f}s({np.mean(wp)/60:.1f}m) 分布=", end="")
        for pct in [25, 50, 75]:
            print(f" P{pct}={np.percentile(wp,pct):.0f}s", end="")
        print()
    if loss_cross:
        lc = np.array(loss_cross)
        print(f"    输的最差时刻: 中位数={np.median(lc):.0f}s({np.median(lc)/60:.1f}m) 均值={np.mean(lc):.0f}s({np.mean(lc)/60:.1f}m) 分布=", end="")
        for pct in [25, 50, 75]:
            print(f" P{pct}={np.percentile(lc,pct):.0f}s", end="")
        print()

# ============================================================
# Part 5: 成交量与胜率
# ============================================================
print(f"\n{'='*100}")
print(f"【Part 5】成交量与信号胜率 — 信号时刻量越大, 胜率越高?")
print("-" * 100)

for W, H_p_up, tail in COMBOS:
    sigs = get_signals(W, H_p_up, tail)
    n = len(sigs)
    if n < 5:
        continue
    
    # 每个信号的: 信号时刻量, 前W分钟均量, 后10分钟总量
    records = []
    for gidx, d, p in sigs:
        settle_idx = gidx + H_SETTLE
        if settle_idx >= N:
            continue
        entry = close[gidx]
        settle = close[settle_idx]
        went_up = settle > entry
        win = (went_up and d == 1) or (not went_up and d == -1)
        
        vol_now = volume[gidx] if gidx < len(volume) else 0
        vol_pre = np.mean(volume[max(0,gidx-W):gidx]) if gidx > W else 0
        vol_post = np.sum(volume[gidx:min(gidx+H_SETTLE, len(volume))])
        # 信号前W分钟的波动率
        lr_window = lr[max(0,gidx-W):gidx]
        vola = np.std(lr_window) * np.sqrt(60) * 1e4 if len(lr_window) > 1 else 0  # bps/min
        
        records.append({"win": win, "vol_now": vol_now, "vol_pre": vol_pre,
                       "vol_post": vol_post, "vr": vol_now/max(vol_pre,1e-10), "vola": vola})
    
    if len(records) < 5:
        continue
    
    rdf = pd.DataFrame(records)
    wr = rdf["win"].mean() * 100
    
    print(f"\n  W={W}s({W//60}m) H_pup={H_p_up}s tail={tail} → {len(rdf)}信号 WR={wr:.1f}%")
    
    # 按量比分组
    rdf["vr_group"] = pd.cut(rdf["vr"], bins=[0, 0.5, 1.0, 1.5, 2.0, 100], labels=["<0.5","0.5-1","1-1.5","1.5-2",">2"])
    print(f"    {'量比(vr)':>10} | {'N':>4} {'WR':>6} {'avg_post_vol':>12} | {'avg_vola':>9}")
    print("    " + "-" * 60)
    for grp, sub in rdf.groupby("vr_group", observed=True):
        if len(sub) > 0:
            w = sub["win"].mean() * 100
            pv = sub["vol_post"].mean()
            va = sub["vola"].mean()
            print(f"    {str(grp):>10} | {len(sub):>4} {w:>5.1f}% {pv:>12.0f} | {va:>7.1f}bps/m")
    
    # 按波动率分组
    rdf["vola_group"] = pd.cut(rdf["vola"], bins=[0, 2, 4, 6, 100], labels=["<2","2-4","4-6",">6"])
    print(f"    {'波动率':>10} | {'N':>4} {'WR':>6}")
    print("    " + "-" * 30)
    for grp, sub in rdf.groupby("vola_group", observed=True):
        if len(sub) > 0:
            w = sub["win"].mean() * 100
            print(f"    {str(grp):>10} | {len(sub):>4} {w:>5.1f}%")
    
    # 按信号时刻量分组
    vol_median = rdf["vol_now"].median()
    high_vol = rdf[rdf["vol_now"] >= vol_median]
    low_vol = rdf[rdf["vol_now"] < vol_median]
    print(f"    信号时刻量: 高于中位数({vol_median:.0f}) WR={high_vol['win'].mean()*100:.1f}%({len(high_vol)}笔) vs 低于中位数 WR={low_vol['win'].mean()*100:.1f}%({len(low_vol)}笔)")

# ============================================================
# Part 6: 信号后逐分钟成交量 vs 非信号时段
# ============================================================
print(f"\n{'='*100}")
print(f"【Part 6】信号后逐分钟成交量 vs 全局平均")
print("-" * 100)

# 用所有信号（合并多个组合增加样本）
all_sigs = []
for W, H_p_up, tail in COMBOS:
    sigs = get_signals(W, H_p_up, tail)
    all_sigs.extend(sigs)

# 去重（cd=600已保证大部分不重叠）
seen = set()
unique_sigs = []
for gidx, d, p in all_sigs:
    if gidx not in seen:
        seen.add(gidx)
        unique_sigs.append((gidx, d, p))

print(f"合并信号数: {len(unique_sigs)} (去重后)")
print(f"\n{'分钟':>5} | {'信号后avg_vol':>14} | {'全局avg_vol':>12} | {'倍数':>6} | {'趋势':>15}")
print("-" * 65)

# 全局平均每分钟成交量
global_minute_vols = []
for i in range(0, N - 60, 60):
    global_minute_vols.append(np.sum(volume[i:i+60]))
global_avg = np.mean(global_minute_vols)

prev_ratio = 1.0
for minute in range(11):  # 0-10分钟
    sec_start = minute * 60
    sec_end = sec_start + 60
    sig_vols = []
    for gidx, d, p in unique_sigs:
        start = gidx + sec_start
        end = min(gidx + sec_end, N)
        if start < N and end > start:
            sig_vols.append(np.sum(volume[start:end]))
    sig_avg = np.mean(sig_vols) if sig_vols else 0
    ratio = sig_avg / max(global_avg, 1e-10)
    trend = ""
    if minute > 0:
        if ratio > prev_ratio * 1.1:
            trend = "↑ 放量"
        elif ratio < prev_ratio * 0.9:
            trend = "↓ 缩量"
        else:
            trend = "→ 平稳"
    prev_ratio = ratio
    bar = "█" * int(ratio * 10)
    print(f"  {minute:>2}min | {sig_avg:>14.0f} | {global_avg:>12.0f} | {ratio:>5.2f}x | {trend:>15} {bar}")
