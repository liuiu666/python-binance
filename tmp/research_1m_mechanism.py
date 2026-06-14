"""
97天1分钟数据 — POC策略胜率机理研究
与秒级数据同一套分析框架，但数据量×100

Part 1: 数据概况 + 生产配置回测
Part 2: p_up校准（核心 — 模型预测 vs 实际）
Part 3: 赢 vs 输 方向归一化轨迹对比
Part 4: 分道扬镳时刻
Part 5: UP vs DOWN 信号拆分
Part 6: 胜率时间稳定性（按天）
Part 7: 不同波动环境下的p_up校准
"""
import math, numpy as np, pandas as pd

PAYOUT = 0.80
BE = 1.0 / (1.0 + PAYOUT) * 100  # 55.56%

# ── 加载1分钟数据 ──
df = pd.read_csv("e:/python-binance/data/btcusdt_1m.csv")
df["ts"] = pd.to_datetime(df["open_time"], utc=True, format="mixed")
for c in ["open", "high", "low", "close", "volume"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")
df = df.dropna(subset=["ts", "close"]).sort_values("ts").reset_index(drop=True)

close = df["close"].values.astype(float)
vol = df["volume"].values.astype(float)
N = len(close)
DAYS = N / 1440.0

print(f"1分钟数据 | {N} bars ({DAYS:.1f}天)")
print(f"价格: {close.min():.1f} ~ {close.max():.1f}")
print(f"时间: {df['ts'].iloc[0]} → {df['ts'].iloc[-1]}")
print("=" * 110)

# ── 收益率 ──
lr = np.log(close[1:] / close[:-1])
lr = np.where(np.isfinite(lr), lr, 0.0)
vol_lr = vol[:-1].copy()

# 累积和（前面加[0]）
cs_lr  = np.concatenate([[0.0], np.cumsum(lr)])
cs_lr2 = np.concatenate([[0.0], np.cumsum(lr ** 2)])
cs_vol = np.concatenate([[0.0], np.cumsum(vol_lr)])

def normal_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
ncdf = np.vectorize(normal_cdf)

# ============================================================
# Part 1: 生产配置回测
# ============================================================
W = 30    # 30分钟窗口
H = 10    # 10分钟horizon
cd = 30   # 30分钟冷却
tail = 0.20

max_idx = N - H
indices = np.arange(W, max_idx)

s  = cs_lr[indices] - cs_lr[indices - W]
s2 = cs_lr2[indices] - cs_lr2[indices - W]
mu = s / W
var = np.maximum((s2 / W) - mu ** 2, 0.0) * W / (W - 1)
sigma = np.sqrt(var)

cs_v = cs_vol[indices] - cs_vol[indices - W]
avg_vol = cs_v / W
vr = np.where(avg_vol > 0, vol_lr[indices] / np.maximum(avg_vol, 1e-10), 1.0)

z = np.sqrt(H) * mu / np.maximum(sigma, 1e-10)
p_up = ncdf(z)

# 实际结果
future_close = close[indices + H]
current_close = close[indices]
actual_up_arr = future_close > current_close

print(f"\n【Part 1】生产配置回测: W={W}bars({W}min) H={H}bars({H}min) tail={tail} cd={cd}bars")
print("-" * 110)
print(f"可评估点: {len(indices):,} bars ({len(indices)/1440:.1f}天)")
print(f"p_up: [{p_up.min():.4f}, {p_up.max():.4f}], mean={p_up.mean():.4f}, median={np.median(p_up):.4f}")

# 信号
sig_up_mask = p_up <= tail
sig_dn_mask = p_up >= 1.0 - tail

signals_arr = np.zeros(len(indices), dtype=int)
signals_arr[sig_up_mask] = 1
signals_arr[sig_dn_mask] = -1

# cooldown
filtered_arr = signals_arr.copy()
last_t = -99999
for i in range(len(filtered_arr)):
    if filtered_arr[i] != 0:
        if indices[i] - last_t < cd:
            filtered_arr[i] = 0
        else:
            last_t = indices[i]

sig_pos = np.where(filtered_arr != 0)[0]
n_sig = len(sig_pos)

# 回测
wins = 0
for si in sig_pos:
    tick_idx = indices[si]
    direction = filtered_arr[si]
    if direction == 1:
        win = close[tick_idx + H] > close[tick_idx]
    else:
        win = close[tick_idx + H] < close[tick_idx]
    if win:
        wins += 1

losses = n_sig - wins
wr = wins / n_sig * 100 if n_sig > 0 else 0
pnl = wins * PAYOUT - losses * 1.0
print(f"信号数: {n_sig} | {wins}W/{losses}L | WR={wr:.1f}% | PNL={pnl:+.1f} | BE={BE:.1f}%")
print(f"信号频率: {n_sig / DAYS:.1f}笔/天")

# ============================================================
# Part 2: p_up校准（核心）
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 2】p_up 校准 — 模型预测概率 vs 实际结果")
print(f"这是策略胜率的根本来源")
print("-" * 110)

bins = np.arange(0, 1.05, 0.05)
print(f"{'p_up区间':>12} | {'样本':>6} | {'实际上涨%':>10} | {'模型预测%':>10} | {'偏差':>8} | {'实际-50%':>9}")
print("-" * 80)
calib_data = []
for j in range(len(bins) - 1):
    mask = (p_up >= bins[j]) & (p_up < bins[j + 1])
    cnt = mask.sum()
    if cnt > 0:
        actual_pct = actual_up_arr[mask].mean() * 100
        pred_pct = (bins[j] + bins[j + 1]) / 2 * 100
        bias = pred_pct - actual_pct
        excess = actual_pct - 50
        print(f"  {bins[j]:.2f}-{bins[j+1]:.2f} | {cnt:>6} | {actual_pct:>9.1f}% | {pred_pct:>9.1f}% | {bias:>+7.1f}% | {excess:>+8.1f}%")
        calib_data.append((bins[j], bins[j+1], cnt, actual_pct, pred_pct, bias))

# ============================================================
# Part 3: 赢 vs 输 方向归一化轨迹对比
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 3】赢 vs 输 — 方向归一化平均偏离轨迹(bps)")
print(f"正值=朝有利方向移动, 负值=朝不利方向移动")
print("-" * 110)

trades = []
for si in sig_pos:
    tick_idx = indices[si]
    entry = close[tick_idx]
    direction = filtered_arr[si]
    win = (future_close[si] > current_close[si]) if direction == 1 else (future_close[si] < current_close[si])

    # 逐分钟路径（H+1个点）
    path = close[tick_idx: tick_idx + H + 1]
    dev = (path - entry) / entry * 10000  # bps
    if direction == -1:
        dev = -dev

    trades.append({"win": win, "dev_path": dev, "direction": direction})

win_devs = np.array([t["dev_path"] for t in trades if t["win"]])
loss_devs = np.array([t["dev_path"] for t in trades if not t["win"]])

win_avg = np.mean(win_devs, axis=0) if len(win_devs) > 0 else np.zeros(H + 1)
loss_avg = np.mean(loss_devs, axis=0) if len(loss_devs) > 0 else np.zeros(H + 1)

print(f"{'分钟':>5} | {'赢的({}笔)'.format(len(win_devs)):>14} | {'输的({}笔)'.format(len(loss_devs)):>14} | {'差异':>10} | 解读")
print("-" * 110)
for m in range(0, H + 1):
    w = win_avg[m]
    l = loss_avg[m]
    diff = w - l
    if abs(diff) < 1:
        note = "≈无差异"
    elif diff > 0:
        note = "赢的更早朝有利方向走"
    else:
        note = "输的早期朝有利方向走"
    print(f"{m:>5} | {w:>+10.2f} bps | {l:>+10.2f} bps | {diff:>+8.2f} | {note}")

# ============================================================
# Part 4: 分道扬镳时刻（逐分钟）
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 4】分道扬镳时刻 — 赢和输从第几分钟开始显著分离？")
print("-" * 110)

if len(win_devs) > 0 and len(loss_devs) > 0:
    for m in range(0, H + 1):
        diff = win_avg[m] - loss_avg[m]
        bar = "█" * int(abs(diff) * 2) if abs(diff) > 0.5 else ""
        if diff > 0:
            bar = " " + bar
        print(f"  {m:>2d}min: 赢{win_avg[m]:+7.2f} 输{loss_avg[m]:+7.2f} 差{diff:+7.2f} |{bar}")

# ============================================================
# Part 5: UP vs DOWN 信号拆分
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 5】UP信号 vs DOWN信号")
print("-" * 110)

up_trades = [t for t in trades if t["direction"] == 1]
dn_trades = [t for t in trades if t["direction"] == -1]

if up_trades:
    uw = sum(1 for t in up_trades if t["win"])
    print(f"  UP(做多反转): {len(up_trades)}笔, {uw}赢/{len(up_trades)-uw}输, WR={uw/len(up_trades)*100:.1f}%")
if dn_trades:
    dw = sum(1 for t in dn_trades if t["win"])
    print(f"  DN(做空反转): {len(dn_trades)}笔, {dw}赢/{len(dn_trades)-dw}输, WR={dw/len(dn_trades)*100:.1f}%")

# UP/DN各自的p_up校准
print(f"\n  UP信号(DN同理镜像)的p_up校准:")
for label, mask_dir in [("UP(p_up≤0.20)", sig_up_mask), ("DN(p_up≥0.80)", sig_dn_mask)]:
    if mask_dir.sum() == 0:
        continue
    actual = actual_up_arr[mask_dir].mean() * 100
    avg_pup = p_up[mask_dir].mean() * 100
    print(f"    {label}: {mask_dir.sum()}样本, 平均p_up={avg_pup:.1f}%, 实际上涨={actual:.1f}%")

# ============================================================
# Part 6: 胜率时间稳定性（按天）
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 6】胜率时间稳定性 — 按天拆分")
print("-" * 110)

# 给每笔信号打上日期标签
dates = []
for si in sig_pos:
    tick_idx = indices[si]
    d = df["ts"].iloc[tick_idx].date()
    dates.append(str(d))

unique_dates = sorted(set(dates))
print(f"{'日期':>12} | {'信号':>4} {'赢':>3} {'WR':>6} | {'PNL':>6} | 累计WR")
print("-" * 60)

cum_w = 0
cum_n = 0
daily_wrs = []
for d in unique_dates:
    d_trades = [(i, trades[k]) for k, i in enumerate(sig_pos) if dates[k] == d]
    dn = len(d_trades)
    dw = sum(1 for _, t in d_trades if t["win"])
    d_wr = dw / dn * 100 if dn > 0 else 0
    d_pnl = dw * PAYOUT - (dn - dw) * 1.0
    cum_w += dw
    cum_n += dn
    cum_wr = cum_w / cum_n * 100
    daily_wrs.append((d, dn, d_wr))
    star = " ★" if d_wr >= 65 else (" ☆" if d_wr >= 55 else (" ✗" if d_wr < 45 else ""))
    print(f"  {d} | {dn:>4} {dw:>3} {d_wr:>5.1f}% | {d_pnl:>+5.1f} | {cum_wr:>5.1f}%{star}")

# 按周聚合
print(f"\n  按周聚合:")
print(f"  {'周':>8} | {'信号':>4} {'赢':>3} {'WR':>6}")
print("  " + "-" * 35)
weekly = {}
for d, n, wr in daily_wrs:
    # ISO week
    from datetime import datetime
    dt = datetime.strptime(d, "%Y-%m-%d")
    wk = dt.strftime("%Y-W%W")
    if wk not in weekly:
        weekly[wk] = [0, 0]
    weekly[wk][0] += n
    # need to recount wins
for wk in sorted(weekly.keys()):
    w_trades = [(i, trades[k]) for k, i in enumerate(sig_pos) 
                if datetime.strptime(str(df["ts"].iloc[indices[i]].date()), "%Y-%m-%d").strftime("%Y-W%W") == wk]
    wn = len(w_trades)
    ww = sum(1 for _, t in w_trades if t["win"])
    wr_w = ww / wn * 100 if wn > 0 else 0
    print(f"  {wk} | {wn:>4} {ww:>3} {wr_w:>5.1f}%")

# ============================================================
# Part 7: 不同波动环境下的p_up校准
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 7】不同波动环境下的p_up校准")
print(f"sigma = 过去30分钟的已实现波动率")
print("-" * 110)

# sigma分位
sig_pct = np.percentile(sigma, [0, 25, 50, 75, 100])
print(f"sigma分布: P25={sig_pct[1]:.6f}, P50={sig_pct[2]:.6f}, P75={sig_pct[3]:.6f}")

vol_levels = [
    ("低波动 (≤P25)", sigma <= sig_pct[1]),
    ("中波动 (P25-P75)", (sigma > sig_pct[1]) & (sigma <= sig_pct[3])),
    ("高波动 (>P75)", sigma > sig_pct[3]),
]

for vol_label, vol_mask in vol_levels:
    print(f"\n  【{vol_label}】 {vol_mask.sum():,}样本")
    # 只看尾部区间
    tail_bins = [(0.0, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.25),
                 (0.75, 0.80), (0.80, 0.85), (0.85, 0.90), (0.90, 0.95), (0.95, 1.01)]
    print(f"    {'p_up区间':>12} | {'样本':>5} | {'实际上涨%':>10} | {'预测%':>8} | {'偏差':>7}")
    print("    " + "-" * 60)
    for lo, hi in tail_bins:
        mask = (p_up >= lo) & (p_up < hi) & vol_mask
        cnt = mask.sum()
        if cnt > 0:
            actual_pct = actual_up_arr[mask].mean() * 100
            pred_pct = (lo + hi) / 2 * 100
            bias = pred_pct - actual_pct
            print(f"    {lo:.2f}-{hi:.2f} | {cnt:>5} | {actual_pct:>9.1f}% | {pred_pct:>7.1f}% | {bias:>+6.1f}%")

# ============================================================
# 总结
# ============================================================
print(f"\n{'='*110}")
print(f"【总结】{DAYS:.0f}天, {n_sig}笔信号, WR={wr:.1f}%, PNL={pnl:+.1f}")
print(f"p_up校准核心发现:")
# 左尾和右尾的关键数据
left_mask = p_up <= 0.05
right_mask = p_up >= 0.95
if left_mask.sum() > 0:
    left_actual = actual_up_arr[left_mask].mean() * 100
    print(f"  左尾(p_up≤0.05): {left_mask.sum():,}样本, 实际上涨{left_actual:.1f}%, 模型预测~2.5%")
if right_mask.sum() > 0:
    right_actual = actual_up_arr[right_mask].mean() * 100
    print(f"  右尾(p_up≥0.95): {right_mask.sum():,}样本, 实际上涨{right_actual:.1f}%, 模型预测~97.5%")
print("分析完成")
