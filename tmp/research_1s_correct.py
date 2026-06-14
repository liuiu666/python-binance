"""
秒级POC策略研究 — 用正确的生产参数等价

生产配置（prod_config.json已读）:
  source_minutes=2, norm_window=60, horizon=10, tail_pct=0.20, min_gap_minutes=30
  → 秒级等价: W=7200s(120min), H=1200s(20min), cd=1800s(30min), tail=0.20

数据: 13217行, 223分钟, 无空洞
评估窗口: 223 - 120 - 20 = 83分钟（生产参数下极少信号）

策略: 
  Part 1: 生产真实参数回测（W=7200, H=1200, cd=1800, tail=0.20）
  Part 2: H=1200s固定, 多W扫描（看窗口对胜率影响）
  Part 3: H=600s vs H=1200s对比（horizon影响）
  Part 4: p_up校准（H=1200s正确版本）
  Part 5: 赢输轨迹（H=1200s）
"""
import math, numpy as np, pandas as pd

PAYOUT = 0.80
BE = 1.0 / (1.0 + PAYOUT) * 100  # 55.56%

# ── 加载秒级数据 ──
# CSV列名已验证: timestamp, close, volume, taker_buy_volume, taker_sell_volume
df = pd.read_csv("e:/python-binance/tmp/server_1s_trades.csv")
df["ts"] = pd.to_datetime(df["timestamp"], utc=True)
df = df.sort_values("ts").reset_index(drop=True)
close = df["close"].values.astype(float)
volume = df["volume"].values.astype(float)
N = len(close)
MINUTES = N / 60.0

print(f"秒级数据 | {N}行, {MINUTES:.0f}分钟 ({MINUTES/60:.1f}小时)")
print(f"价格: {close.min():.1f} ~ {close.max():.1f}")
print(f"生产参数等价: W=7200s(120min) H=1200s(20min) cd=1800s(30min) tail=0.20")
print(f"可用评估窗口: {MINUTES:.0f} - 120 - 20 = {MINUTES-120-20:.0f}分钟")
print("=" * 110)

lr = np.log(close[1:] / close[:-1])
lr = np.where(np.isfinite(lr), lr, 0.0)
vol_lr = volume[:-1].copy()

cs_lr  = np.concatenate([[0.0], np.cumsum(lr)])
cs_lr2 = np.concatenate([[0.0], np.cumsum(lr ** 2)])
cs_vol = np.concatenate([[0.0], np.cumsum(vol_lr)])

def normal_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
ncdf = np.vectorize(normal_cdf)

def run_backtest(W, H, cd, tail, vol_thresh=0.0):
    """回测，返回详细结果字典"""
    max_idx = N - H
    if W >= max_idx:
        return None
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
    
    sig = np.zeros(len(indices), dtype=np.int8)
    sig[p_up <= tail] = 1
    sig[p_up >= 1 - tail] = -1
    
    if vol_thresh > 0:
        sig[vr < vol_thresh] = 0
    
    # cooldown
    filtered = sig.copy()
    last_bar = -99999
    sig_pos = []
    for i in range(len(filtered)):
        if filtered[i] != 0:
            if indices[i] - last_bar >= cd:
                last_bar = indices[i]
                sig_pos.append(i)
            else:
                filtered[i] = 0
    
    n_sig = len(sig_pos)
    if n_sig == 0:
        return {"W": W, "H": H, "cd": cd, "tail": tail, "vol": vol_thresh,
                "n_sig": 0, "wr": 0, "pnl": 0, "wins": 0, "losses": 0,
                "p_up": p_up, "actual_up": None, "trades": []}
    
    wins = up_w = up_n = dn_w = dn_n = 0
    trades = []
    for si in sig_pos:
        gidx = indices[si]
        d = filtered[si]
        entry = close[gidx]
        exit_p = close[gidx + H]
        win = (exit_p > entry) if d == 1 else (exit_p < entry)
        if d == 1: up_n += 1
        else: dn_n += 1
        if win:
            wins += 1
            if d == 1: up_w += 1
            else: dn_w += 1
        trades.append({"idx": gidx, "dir": d, "entry": entry, "exit": exit_p, "win": win,
                       "p_up": p_up[si], "time": df["ts"].iloc[gidx]})
    
    losses = n_sig - wins
    wr = wins / n_sig * 100
    pnl = wins * PAYOUT - losses * 1.0
    
    # 实际涨跌（全样本）
    future = close[indices + H]
    current = close[indices]
    actual_up = future > current
    
    return {"W": W, "H": H, "cd": cd, "tail": tail, "vol": vol_thresh,
            "n_sig": n_sig, "wr": wr, "pnl": pnl, "wins": wins, "losses": losses,
            "up_w": up_w, "up_n": up_n, "dn_w": dn_w, "dn_n": dn_n,
            "p_up": p_up, "actual_up": actual_up, "indices": indices,
            "trades": trades}

# ============================================================
# Part 1: 生产真实参数
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 1】生产真实参数: W=7200s(120min), H=1200s(20min), cd=1800s(30min), tail=0.20")
print("-" * 110)

r = run_backtest(W=7200, H=1200, cd=1800, tail=0.20)
if r and r["n_sig"] > 0:
    print(f"  信号: {r['n_sig']} ({r['wins']}W/{r['losses']}L), WR={r['wr']:.1f}%, PNL={r['pnl']:+.1f}")
    print(f"  边际: WR-{BE:.2f}={r['wr']-BE:+.2f}%")
    if r["up_n"]: print(f"  UP: {r['up_n']}笔 WR={r['up_w']/r['up_n']*100:.1f}%")
    if r["dn_n"]: print(f"  DN: {r['dn_n']}笔 WR={r['dn_w']/r['dn_n']*100:.1f}%")
    for t in r["trades"]:
        d_str = "UP" if t["dir"] == 1 else "DN"
        w_str = "WIN" if t["win"] else "LOSS"
        print(f"    {t['time']} | {d_str} entry={t['entry']:.1f} exit={t['exit']:.1f} p_up={t['p_up']:.3f} → {w_str}")
else:
    print(f"  信号: {r['n_sig'] if r else 0}（数据不足以产生信号）")
    avail = MINUTES - 120 - 20
    print(f"  原因: 评估窗口只有{avail:.0f}分钟, cooldown=30min, 最多{avail/30:.1f}个独立窗口")

# ============================================================
# Part 2: H=1200s固定, 多W扫描
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 2】H=1200s(20min)固定 — 多W扫描（匹配生产horizon）")
print("-" * 110)

H_FIXED = 1200  # 20分钟，生产真实horizon
W_LIST_S = [300, 600, 900, 1200, 1800, 2400, 3600, 4800, 6000, 7200]
TAIL_LIST = [0.10, 0.15, 0.20, 0.25, 0.30]

results_1200 = []
print(f"{'W(秒)':>8} {'W(分)':>6} |", end="")
for t in TAIL_LIST:
    print(f" tail={t:.2f}      |", end="")
print()
print(f"{'':>8} {'':>6} |", end="")
for t in TAIL_LIST:
    print(f"  N   WR%  PNL   |", end="")
print()
print("-" * 100)

for W in W_LIST_S:
    cd = max(W // 4, 300)  # cd ≈ W/4, 最小5分钟
    print(f"W={W:>5} {W//60:>4}min |", end="")
    for tail in TAIL_LIST:
        r = run_backtest(W=W, H=H_FIXED, cd=cd, tail=tail)
        if r and r["n_sig"] > 0:
            results_1200.append(r)
            star = "★" if r["wr"] >= BE and r["pnl"] > 0 else ("~" if r["wr"] >= 53 else "✗")
            print(f" {r['n_sig']:>3} {r['wr']:>4.0f}% {r['pnl']:>+5.1f} {star}|", end="")
        else:
            print(f"    0  ---  ---- |", end="")
    print()

# ============================================================
# Part 3: H=600s vs H=1200s 对比
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 3】H=600s(10min) vs H=1200s(20min) — horizon影响")
print("-" * 110)

print(f"{'W(分)':>6} {'tail':>5} | {'H=600 N':>7} {'WR':>6} {'PNL':>7} | {'H=1200 N':>8} {'WR':>6} {'PNL':>7} | {'差异':>10}")
print("-" * 85)

for W in [600, 1200, 1800, 3600]:
    cd = max(W // 4, 300)
    for tail in [0.15, 0.20, 0.25]:
        r600 = run_backtest(W=W, H=600, cd=cd, tail=tail)
        r1200 = run_backtest(W=W, H=1200, cd=cd, tail=tail)
        n6 = r600["n_sig"] if r600 else 0
        w6 = r600["wr"] if r600 and r600["n_sig"] > 0 else 0
        p6 = r600["pnl"] if r600 and r600["n_sig"] > 0 else 0
        n12 = r1200["n_sig"] if r1200 else 0
        w12 = r1200["wr"] if r1200 and r1200["n_sig"] > 0 else 0
        p12 = r1200["pnl"] if r1200 and r1200["n_sig"] > 0 else 0
        diff = w12 - w6 if n6 > 0 and n12 > 0 else 0
        print(f"{W//60:>5}min {tail:>5.2f} | {n6:>7} {w6:>5.1f}% {p6:>+6.1f} | {n12:>8} {w12:>5.1f}% {p12:>+6.1f} | {diff:>+9.1f}%")

# ============================================================
# Part 4: p_up校准（H=1200s正确版）
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 4】p_up校准 — H=1200s(20min), 生产真实horizon")
print("-" * 110)

# 用W=3600s(60min)做校准（样本量最大）
r_calib = run_backtest(W=3600, H=1200, cd=900, tail=0.20)
if r_calib and r_calib["actual_up"] is not None:
    p_up_arr = r_calib["p_up"]
    actual_arr = r_calib["actual_up"]
    
    bins = np.arange(0, 1.05, 0.05)
    print(f"{'p_up区间':>12} | {'样本':>5} | {'实际上涨%':>10} | {'模型预测%':>10} | {'偏差':>8} | {'超额':>8}")
    print("-" * 75)
    for j in range(len(bins) - 1):
        mask = (p_up_arr >= bins[j]) & (p_up_arr < bins[j + 1])
        cnt = mask.sum()
        if cnt > 0:
            actual_pct = actual_arr[mask].mean() * 100
            pred_pct = (bins[j] + bins[j + 1]) / 2 * 100
            bias = pred_pct - actual_pct
            excess = actual_pct - 50
            print(f"  {bins[j]:.2f}-{bins[j+1]:.2f} | {cnt:>5} | {actual_pct:>9.1f}% | {pred_pct:>9.1f}% | {bias:>+7.1f}% | {excess:>+7.1f}%")

# ============================================================
# Part 5: 赢输轨迹（H=1200s，选信号最多的组合）
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 5】赢vs输轨迹 — H=1200s(20min)")
print("-" * 110)

# 选信号最多的组合
if results_1200:
    best_for_traj = max(results_1200, key=lambda x: x["n_sig"])
    print(f"使用: W={best_for_traj['W']}s({best_for_traj['W']//60}min) tail={best_for_traj['tail']} → {best_for_traj['n_sig']}信号")
    
    trades = best_for_traj["trades"]
    win_trades = [t for t in trades if t["win"]]
    loss_trades = [t for t in trades if not t["win"]]
    
    H_traj = best_for_traj["H"]
    
    # 逐秒轨迹
    check_points = list(range(0, H_traj + 1, max(H_traj // 20, 1)))
    if H_traj not in check_points:
        check_points.append(H_traj)
    
    print(f"\n{'秒':>6} | {'赢({}笔)'.format(len(win_trades)):>14} | {'输({}笔)'.format(len(loss_trades)):>14} | {'差异':>8}")
    print("-" * 60)
    
    for sec in check_points:
        w_vals = []
        l_vals = []
        for t in win_trades:
            idx = t["idx"] + sec
            if idx < N:
                dev = (close[idx] - t["entry"]) / t["entry"] * 10000
                if t["dir"] == -1:
                    dev = -dev
                w_vals.append(dev)
        for t in loss_trades:
            idx = t["idx"] + sec
            if idx < N:
                dev = (close[idx] - t["entry"]) / t["entry"] * 10000
                if t["dir"] == -1:
                    dev = -dev
                l_vals.append(dev)
        
        w_avg = np.mean(w_vals) if w_vals else 0
        l_avg = np.mean(l_vals) if l_vals else 0
        diff = w_avg - l_avg
        bar = "█" * int(abs(diff) / 2) if abs(diff) > 1 else ""
        print(f"{sec:>5}s | {w_avg:>+10.2f} bps | {l_avg:>+10.2f} bps | {diff:>+6.2f} |{bar}")

# ============================================================
# 总结
# ============================================================
print(f"\n{'='*110}")
print(f"【总结】{MINUTES:.0f}分钟秒级数据, 生产H=1200s(20min)")
print(f"  数据量: {MINUTES:.1f}小时 = {MINUTES/60:.1f}h")
print(f"  生产参数(W=7200s)可用评估窗口: {MINUTES-120-20:.0f}分钟")
if results_1200:
    valid = [r for r in results_1200 if r["n_sig"] >= 3]
    if valid:
        best = max(valid, key=lambda x: (x["wr"], x["n_sig"]))
        print(f"  最佳H=1200组合: W={best['W']}s({best['W']//60}min) tail={best['tail']} → {best['n_sig']}信号 WR={best['wr']:.1f}%")
    else:
        print(f"  没有组合产生≥3个信号（数据量不足）")
        max_sig = max(results_1200, key=lambda x: x["n_sig"])
        print(f"  最多信号: W={max_sig['W']}s tail={max_sig['tail']} → {max_sig['n_sig']}信号")
print(f"  结论: 3.7小时数据不足以对H=1200s做可靠统计, 需要至少24小时连续数据")
