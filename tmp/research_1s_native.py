"""
秒级原生POC策略 — 不聚合，直接在1秒tick上运行
多窗口×多horizon×多tail 全局扫描

核心目标：理解胜率机理，不是优化参数
  Part 1: W×H×tail 全局扫描，找最优组合
  Part 2: 最优组合的逐笔详情 + 逐秒路径
  Part 3: 赢 vs 输 的平均轨迹对比 + 分道扬镳时刻
  Part 4: p_up 校准（预测概率 vs 实际结果）
"""
import math, numpy as np, pandas as pd

PAYOUT = 0.80
BE = 1.0 / (1.0 + PAYOUT) * 100  # 55.56%

# ── 加载秒级数据 ──
raw = pd.read_csv("e:/python-binance/tmp/server_1s_trades.csv")
raw["ts"] = pd.to_datetime(raw["timestamp"], utc=True)
for c in ["open", "high", "low", "close", "volume",
          "taker_buy_volume", "taker_sell_volume", "trades"]:
    raw[c] = pd.to_numeric(raw[c], errors="coerce")
raw = raw.dropna(subset=["ts", "close"]).sort_values("ts").reset_index(drop=True)

close = raw["close"].values.astype(float)
vol = raw["volume"].values.astype(float)
buy = raw["taker_buy_volume"].values.astype(float)
sell = raw["taker_sell_volume"].values.astype(float)
N = len(close)
DURATION_MIN = N / 60.0

print(f"秒级原生POC | {N} ticks ({DURATION_MIN:.1f}分钟)")
print(f"价格: {close.min():.1f} ~ {close.max():.1f} (波动{(close.max()-close.min())/close.mean()*100:.2f}%)")
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

def run_window(W, H):
    """对给定W和H，返回所有评估点的mu, sigma, z, p_up, vol_ratio"""
    max_idx = N - H
    indices = np.arange(W, max_idx)
    if len(indices) < 10:
        return None
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
    return indices, mu, sigma, z, p_up, vr

# ============================================================
# Part 1: 全局扫描 W × H × tail
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 1】全局扫描: W(窗口) × H(horizon) × tail(阈值)")
print(f"数据: {N} ticks ({DURATION_MIN:.0f}分钟)")
print("=" * 110)

# 窗口: 1min ~ 60min
W_LIST = [60, 120, 180, 300, 600, 900, 1200, 1800, 2400, 3600]
# Horizon: 1min ~ 10min
H_LIST = [60, 120, 180, 300, 600]
# Tail: 宽到窄
TAIL_LIST = [0.35, 0.30, 0.25, 0.20, 0.15, 0.10]

results = []

for W in W_LIST:
    for H in H_LIST:
        ret = run_window(W, H)
        if ret is None:
            continue
        indices_w, mu_w, sigma_w, z_w, p_up_w, vr_w = ret
        n_eval = len(indices_w)
        
        # 实际结果
        actual_up = np.array([close[indices_w[i] + H] > close[indices_w[i]] for i in range(n_eval)])
        
        for tail in TAIL_LIST:
            sig_up_mask = p_up_w <= tail
            sig_dn_mask = p_up_w >= 1.0 - tail
            
            # 无vol过滤
            for vol_f in [0.0, 1.0, 1.2, 1.5]:
                if vol_f > 0:
                    vmask = vr_w >= vol_f
                    sig_up_m = sig_up_mask & vmask
                    sig_dn_m = sig_dn_mask & vmask
                else:
                    sig_up_m = sig_up_mask
                    sig_dn_m = sig_dn_mask
                
                # cooldown = H
                cd = H
                signals_arr = np.zeros(n_eval, dtype=int)
                signals_arr[sig_up_m] = 1
                signals_arr[sig_dn_m] = -1
                
                filtered_arr = signals_arr.copy()
                last_t = -99999
                for i in range(n_eval):
                    if filtered_arr[i] != 0:
                        if indices_w[i] - last_t < cd:
                            filtered_arr[i] = 0
                        else:
                            last_t = indices_w[i]
                
                sp = np.where(filtered_arr != 0)[0]
                n_sig = len(sp)
                if n_sig < 2:
                    continue
                
                wins = 0
                for si in sp:
                    tick_idx = indices_w[si]
                    direction = filtered_arr[si]
                    if direction == 1:
                        win = close[tick_idx + H] > close[tick_idx]
                    else:
                        win = close[tick_idx + H] < close[tick_idx]
                    if win:
                        wins += 1
                
                wr = wins / n_sig * 100
                pnl = wins * PAYOUT - (n_sig - wins) * 1.0
                wr_label = "  ★" if wr >= 60 and n_sig >= 5 else ("  ☆" if wr >= 55 and n_sig >= 5 else "")
                
                results.append({
                    "W": W, "H": H, "tail": tail, "vol": vol_f,
                    "n_sig": n_sig, "wins": wins, "wr": wr, "pnl": pnl,
                })

# 排序输出：按信号数降序（找最丰富的），同时分WR档
results.sort(key=lambda x: (-x["n_sig"], -x["wr"]))

print(f"\n{'W':>6} {'H':>5} {'tail':>5} {'vol':>5} | {'信号':>4} {'赢':>3} {'WR':>6} {'PNL':>7} | 备注")
print("-" * 80)

shown = set()
cnt = 0
for r in results:
    key = (r["W"], r["H"], r["tail"], r["vol"])
    wr_label = ""
    if r["wr"] >= 60 and r["n_sig"] >= 5:
        wr_label = "★高胜率"
    elif r["wr"] >= 55 and r["n_sig"] >= 5:
        wr_label = "☆可交易"
    elif r["n_sig"] >= 10:
        wr_label = "高频"
    
    Wmin = r["W"] / 60
    Hmin = r["H"] / 60
    print(f"{r['W']:>4}m {Hmin:>3.0f}m {r['tail']:>5.2f} {r['vol']:>4.1f} | "
          f"{r['n_sig']:>4} {r['wins']:>3} {r['wr']:>5.1f}% {r['pnl']:>+7.1f} | {wr_label}")
    cnt += 1
    if cnt >= 80:
        print(f"  ... 还有 {len(results)-80} 行")
        break

# 按WR排序，找最高胜率且有足够信号
print(f"\n{'='*110}")
print(f"【Top 15 按WR排序】(信号≥5)")
print("-" * 80)
valid = [r for r in results if r["n_sig"] >= 5]
valid.sort(key=lambda x: -x["wr"])
for r in valid[:15]:
    Wmin = r["W"] / 60
    Hmin = r["H"] / 60
    star = " ★" if r["wr"] >= 60 else ""
    print(f"{r['W']:>4}s({Wmin:.0f}m) H={r['H']:>4}s({Hmin:.0f}m) tail={r['tail']:.2f} vol≥{r['vol']:.1f} | "
          f"{r['n_sig']:>3}笔 WR={r['wr']:.1f}% PNL={r['pnl']:+.1f}{star}")

# 按PNL排序
print(f"\n{'='*110}")
print(f"【Top 15 按PNL排序】(信号≥5)")
print("-" * 80)
valid.sort(key=lambda x: -x["pnl"])
for r in valid[:15]:
    Wmin = r["W"] / 60
    Hmin = r["H"] / 60
    print(f"{r['W']:>4}s({Wmin:.0f}m) H={r['H']:>4}s({Hmin:.0f}m) tail={r['tail']:.2f} vol≥{r['vol']:.1f} | "
          f"{r['n_sig']:>3}笔 WR={r['wr']:.1f}% PNL={r['pnl']:+.1f}")

# ============================================================
# 选最优组合做深度分析
# 强制 H=600s (10分钟)，与生产策略一致
# ============================================================
H_FIXED = 600  # 10分钟horizon
h_valid = [r for r in results if r["H"] == H_FIXED and r["n_sig"] >= 3]
if not h_valid:
    h_valid = [r for r in results if r["H"] == H_FIXED]

# 先输出 H=600 所有组合一览
print(f"\n{'='*110}")
print(f"【H=600s(10min) 所有组合一览】")
print("-" * 90)
h_valid.sort(key=lambda x: (-x["wr"], -x["n_sig"]))
for r in h_valid:
    star = " ★" if r["wr"] >= 60 else (" ☆" if r["wr"] >= 55 else "")
    print(f"  W={r['W']:>4}s({r['W']/60:>4.0f}m) tail={r['tail']:.2f} vol≥{r['vol']:.1f} | "
          f"{r['n_sig']:>3}笔 WR={r['wr']:>5.1f}% PNL={r['pnl']:>+6.1f}{star}")

# 选信号最多的组合
best = max(h_valid, key=lambda x: (x["n_sig"], x["wr"]))
# 如果有WR≥58%且信号≥8的组合，优先
for r in h_valid:
    if r["wr"] >= 58 and r["n_sig"] >= 8:
        best = r
        break

W = best["W"]
H = best["H"]
tail = best["tail"]
vol_f = best["vol"]
cd = H

ret = run_window(W, H)
indices, mu, sigma, z, p_up, vr = ret

print(f"\n{'='*110}")
print(f"【深度分析组合】W={W}s({W/60:.0f}min) H={H}s({H/60:.0f}min) tail={tail} vol≥{vol_f} cd={cd}s")
print(f"可评估点: {len(indices)} ticks")
print("=" * 110)

# ============================================================
# Part 2: 逐信号分析 — 逐秒价格路径
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 2】逐笔详情 + 逐秒价格路径")
print("-" * 110)

# 重新生成信号
sig_up_mask = p_up <= tail
sig_dn_mask = p_up >= 1.0 - tail
if vol_f > 0:
    vmask = vr >= vol_f
    sig_up_mask = sig_up_mask & vmask
    sig_dn_mask = sig_dn_mask & vmask

signals_arr = np.zeros(len(indices), dtype=int)
signals_arr[sig_up_mask] = 1
signals_arr[sig_dn_mask] = -1

filtered_arr = signals_arr.copy()
last_t = -99999
for i in range(len(filtered_arr)):
    if filtered_arr[i] != 0:
        if indices[i] - last_t < cd:
            filtered_arr[i] = 0
        else:
            last_t = indices[i]

sig_pos = np.where(filtered_arr != 0)[0]
print(f"信号数: {len(sig_pos)} (tail={tail}, vol≥{vol_f}, cd={cd}s)")

# p_up 分布概览
print(f"\np_up 分布: [{p_up.min():.4f}, {p_up.max():.4f}], mean={p_up.mean():.4f}, median={np.median(p_up):.4f}")
print(f"z range: [{z.min():.4f}, {z.max():.4f}], mu range: [{mu.min():.8f}, {mu.max():.8f}]")
print(f"sigma range: [{sigma.min():.8f}, {sigma.max():.8f}]")

trades = []
for si in sig_pos:
    tick_idx = indices[si]
    entry = close[tick_idx]
    exit_p = close[tick_idx + H]
    direction = filtered_arr[si]

    if direction == 1:
        win = exit_p > entry
    else:
        win = exit_p < entry

    path = close[tick_idx: tick_idx + H + 1]
    dev = (path - entry) / entry * 10000  # bps
    if direction == -1:
        dev = -dev

    trades.append({
        "tick": tick_idx,
        "time": raw["ts"].iloc[tick_idx],
        "direction": "UP" if direction == 1 else "DOWN",
        "entry": entry,
        "exit": exit_p,
        "p_up": p_up[si],
        "z": z[si],
        "vr": vr[si],
        "mu": mu[si],
        "sigma": sigma[si],
        "win": win,
        "dev_path": dev,
        "raw_path": path,
    })

if trades:
    wins = sum(1 for t in trades if t["win"])
    losses = len(trades) - wins
    total = len(trades)
    wr = wins / total * 100
    pnl = sum(PAYOUT if t["win"] else -1.0 for t in trades)
    print(f"\n结果: {total}笔 | {wins}W/{losses}L | WR={wr:.1f}% | PNL={pnl:+.1f} | BE={BE:.1f}%")
    print()

    # 逐秒检查点
    checkpoints = sorted(set([1, 5, 10, 15, 30, 60, 90, 120, 180, 300, H]))
    checkpoints = [s for s in checkpoints if s <= H]
    
    hdr = f"{'#':>2} {'时间':>8} {'方向':>4} {'结果':>4} | {'entry':>10} {'exit':>10} | "
    hdr += f"{'p_up':>6} {'z':>7} {'vr':>5} | "
    for sec in checkpoints:
        hdr += f"{sec}s  "
    print(hdr)
    print("-" * len(hdr))

    for i, t in enumerate(trades):
        line = f"{i+1:>2} {t['time'].strftime('%H:%M:%S'):>8} {t['direction']:>4} "
        line += f"{'WIN' if t['win'] else 'LOSS':>4} | {t['entry']:>10.1f} {t['exit']:>10.1f} | "
        line += f"{t['p_up']:.3f} {t['z']:+7.3f} {t['vr']:>5.2f} | "
        for sec in checkpoints:
            if sec < len(t["dev_path"]):
                line += f"{t['dev_path'][sec]:+5.1f}  "
            else:
                line += "  N/A  "
        print(line)

    # ============================================================
    # Part 3: 赢 vs 输 — 平均轨迹 + 分道扬镽时刻
    # ============================================================
    print(f"\n{'='*110}")
    print(f"【Part 3】赢 vs 输 — 方向归一化平均偏离轨迹(bps)")
    print(f"正值=朝有利方向移动, 负值=朝不利方向移动")
    print("-" * 110)

    win_devs = np.array([t["dev_path"] for t in trades if t["win"]])
    loss_devs = np.array([t["dev_path"] for t in trades if not t["win"]])

    if len(win_devs) > 0:
        win_avg = np.mean(win_devs, axis=0)
    else:
        win_avg = np.zeros(H + 1)
    if len(loss_devs) > 0:
        loss_avg = np.mean(loss_devs, axis=0)
    else:
        loss_avg = np.zeros(H + 1)

    print(f"{'秒':>5} | {'赢的({}笔)'.format(len(win_devs)):>14} | {'输的({}笔)'.format(len(loss_devs)):>14} | {'差异':>10} | 解读")
    print("-" * 110)

    for sec in checkpoints:
        if sec < len(win_avg):
            w = win_avg[sec]
            l = loss_avg[sec]
            diff = w - l
            if abs(diff) < 1:
                note = "≈无差异"
            elif diff > 0:
                note = "赢的更早朝有利方向走"
            else:
                note = "输的早期朝有利方向走"
            print(f"{sec:>5} | {w:>+10.2f} bps | {l:>+10.2f} bps | {diff:>+8.2f} | {note}")

    # 分道扬镽时刻详细图
    print(f"\n{'='*110}")
    print(f"【分道扬镽时刻】赢和输的轨迹从第几秒开始显著分离？")
    print("-" * 110)

    if len(win_devs) > 0 and len(loss_devs) > 0:
        step = max(1, H // 60)  # 大约60个点
        for sec in range(0, H + 1, step):
            if sec < len(win_avg):
                diff = win_avg[sec] - loss_avg[sec]
                bar = "█" * int(abs(diff) * 2) if abs(diff) > 0.5 else ""
                if diff > 0:
                    bar = " " + bar
                print(f"  {sec:>4d}s: 赢{win_avg[sec]:+7.2f} 输{loss_avg[sec]:+7.2f} 差{diff:+7.2f} |{bar}")

    # ============================================================
    # Part 4: p_up 校准 — 预测概率 vs 实际结果
    # ============================================================
    print(f"\n{'='*110}")
    print(f"【Part 4】p_up 校准 — 模型预测概率 vs 实际结果")
    print("-" * 110)

    actual_up = np.array([close[indices[i] + H] > close[indices[i]] for i in range(len(indices))])

    # 细分 bins
    bins = np.arange(0, 1.05, 0.05)
    print(f"{'p_up区间':>12} | {'样本':>5} | {'实际上涨%':>10} | {'模型预测%':>10} | {'偏差':>8}")
    print("-" * 65)
    for j in range(len(bins) - 1):
        mask = (p_up >= bins[j]) & (p_up < bins[j + 1])
        cnt = mask.sum()
        if cnt > 0:
            actual_pct = actual_up[mask].mean() * 100
            pred_pct = (bins[j] + bins[j + 1]) / 2 * 100
            bias = pred_pct - actual_pct
            print(f"  {bins[j]:.2f}-{bins[j+1]:.2f} | {cnt:>5} | {actual_pct:>9.1f}% | {pred_pct:>9.1f}% | {bias:>+7.1f}%")

    # ============================================================
    # Part 5: 方向拆分
    # ============================================================
    print(f"\n{'='*110}")
    print(f"【Part 5】UP信号 vs DOWN信号")
    print("-" * 110)

    up_trades = [t for t in trades if t["direction"] == "UP"]
    dn_trades = [t for t in trades if t["direction"] == "DOWN"]

    if up_trades:
        uw = sum(1 for t in up_trades if t["win"])
        print(f"  UP(做多反转): {len(up_trades)}笔, {uw}赢/{len(up_trades)-uw}输, WR={uw/len(up_trades)*100:.1f}%")
    if dn_trades:
        dw = sum(1 for t in dn_trades if t["win"])
        print(f"  DN(做空反转): {len(dn_trades)}笔, {dw}赢/{len(dn_trades)-dw}输, WR={dw/len(dn_trades)*100:.1f}%")
else:
    print("无信号触发，无法做深度分析。")
    print("建议：拉取更长时间的秒级数据，或进一步放宽参数。")

print(f"\n{'='*110}")
print("分析完成")
