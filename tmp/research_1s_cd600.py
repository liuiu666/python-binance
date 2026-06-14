"""
秒级10分钟二元期权策略研究 — cd≥H_settle修正版
==================================================
关键修正: cd = 600s (= H_settle)，保证10分钟内最多1个信号
之前 cd=150s 导致同一10分钟周期内信号重叠4倍，WR被虚高
"""
import math, time
import numpy as np
import pandas as pd

PAYOUT = 0.80
BE = 1.0 / (1.0 + PAYOUT) * 100  # 55.56%
H_SETTLE = 600  # 10分钟，固定不变
CD = 600       # = H_settle，保证不重叠

# ── 加载秒级数据 ──
df = pd.read_csv("e:/python-binance/tmp/server_1s_trades.csv")
df["ts"] = pd.to_datetime(df["timestamp"], utc=True)
df = df.sort_values("ts").reset_index(drop=True)
close = df["close"].values.astype(float)
N = len(close)
MINUTES = N / 60.0

lr = np.log(close[1:] / close[:-1])
lr = np.where(np.isfinite(lr), lr, 0.0)
cs_lr  = np.concatenate([[0.0], np.cumsum(lr)])
cs_lr2 = np.concatenate([[0.0], np.cumsum(lr ** 2)])
ncdf = np.vectorize(lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0))))

max_eval = N - H_SETTLE
future_close = close[H_SETTLE:]

print(f"秒级数据 | {N}行, {MINUTES:.0f}分钟 ({MINUTES/60:.1f}小时)")
print(f"H_settle = {H_SETTLE}s (10分钟到期) — 固定")
print(f"cd = {CD}s (= H_settle) — 10分钟内最多1个信号")
print(f"价格区间: {close.min():.1f} ~ {close.max():.1f} ({(close.max()/close.min()-1)*100:.2f}%)")
print(f"可用评估窗口: {MINUTES:.0f}分钟, 理论最大独立信号数: {MINUTES/(CD/60):.0f}")
print("=" * 100)

def run_backtest(W, H_p_up, tail):
    max_idx = N - H_SETTLE
    if W >= max_idx:
        return None
    
    indices = np.arange(W, max_idx)
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
    
    # cd=600 过滤
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
    
    n_sig = len(sig_pos)
    if n_sig == 0:
        return {"W": W, "H_p_up": H_p_up, "tail": tail, "n_sig": 0, "wr": 0, "pnl": 0,
                "wins": 0, "losses": 0, "up_w": 0, "up_n": 0, "dn_w": 0, "dn_n": 0,
                "p_up": p_up, "settle_up": future_close[indices] > close[indices]}
    
    wins = up_w = dn_w = 0
    trades_detail = []
    for si in sig_pos:
        gidx = indices[si]
        d = filtered[si]
        went_up = future_close[gidx] > close[gidx]
        win = (went_up and d == 1) or (not went_up and d == -1)
        if win:
            wins += 1
            if d == 1: up_w += 1
            else: dn_w += 1
        trades_detail.append({"idx": gidx, "dir": d, "entry": close[gidx],
                              "win": win, "p_up": p_up[si], "time": df["ts"].iloc[gidx]})
    
    up_n = sum(1 for si in sig_pos if filtered[si] == 1)
    dn_n = n_sig - up_n
    losses = n_sig - wins
    wr = wins / n_sig * 100
    pnl = wins * PAYOUT - losses * 1.0
    
    return {"W": W, "H_p_up": H_p_up, "tail": tail, "n_sig": n_sig, "wr": wr, "pnl": pnl,
            "wins": wins, "losses": losses, "up_w": up_w, "up_n": up_n, "dn_w": dn_w, "dn_n": dn_n,
            "p_up": p_up, "settle_up": future_close[indices] > close[indices],
            "trades": trades_detail}

# ============================================================
# Part 1: 全局扫描 (cd=600固定)
# ============================================================
print(f"\n【Part 1】全局扫描 — cd=600s(=H_settle), 10分钟内最多1信号")
print("-" * 100)

W_LIST     = [60, 120, 300, 600, 900, 1200, 1800, 2400, 3600, 4800, 6000]
HPUP_LIST  = [60, 120, 300, 600, 900, 1200, 1800]
TAIL_LIST  = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]

all_results = []
t0 = time.time()
for W in W_LIST:
    for H_p_up in HPUP_LIST:
        for tail in TAIL_LIST:
            r = run_backtest(W, H_p_up, tail)
            if r and r["n_sig"] > 0:
                all_results.append(r)
elapsed = time.time() - t0

print(f"扫描完成: {len(W_LIST)*len(HPUP_LIST)*len(TAIL_LIST)}组合, {elapsed:.1f}s, {len(all_results)}个有信号\n")

# 全部有效结果 (N≥1)
all_results.sort(key=lambda x: (x["wr"], -x["n_sig"]), reverse=True)

print(f"全部结果 (N≥1, 按WR降序):")
print(f"{'排名':>4} {'W':>6} {'H_pup':>6} {'tail':>5} | {'N':>3} {'WR':>6} {'PNL':>7} {'边际':>7} | {'UP':>8} {'DN':>8}")
print("-" * 85)
for i, r in enumerate(all_results[:40]):
    margin = r["wr"] - BE
    up_str = f"{r['up_w']}/{r['up_n']}" if r["up_n"] > 0 else "-"
    dn_str = f"{r['dn_w']}/{r['dn_n']}" if r["dn_n"] > 0 else "-"
    star = "★" if r["wr"] >= BE and r["pnl"] > 0 else ("~" if r["wr"] >= 50 else "✗")
    print(f"{i+1:>4} {r['W']//60:>4}min {r['H_p_up']//60:>4}min {r['tail']:>5.2f} | {r['n_sig']:>3} {r['wr']:>5.1f}%{star}{r['pnl']:>+6.1f} {margin:>+6.1f}% | {up_str:>8} {dn_str:>8}")

# ============================================================
# Part 2: WR热力图
# ============================================================
print(f"\n{'='*100}")
print(f"【Part 2】WR热力图 (tail=0.20, cd=600s)")
print("-" * 100)
print(f"{'W/H_pup':>8}", end="")
for hp in HPUP_LIST:
    print(f" {hp//60:>6}m", end="")
print()
print("-" * (10 + 8 * len(HPUP_LIST)))
for W in W_LIST:
    print(f"W={W//60:>4}min", end="")
    for hp in HPUP_LIST:
        matches = [r for r in all_results if r["W"]==W and r["H_p_up"]==hp and abs(r["tail"]-0.20)<0.001]
        if matches and matches[0]["n_sig"] >= 1:
            r = matches[0]
            star = "★" if r["wr"] >= BE and r["pnl"] > 0 else ("~" if r["wr"] >= 50 else "✗")
            print(f" {r['wr']:>4.0f}{star}{r['n_sig']:>2}", end="")
        else:
            print(f"    ---", end="")
    print()

# 多tail热力图
for tail_show in [0.05, 0.10, 0.15, 0.25, 0.30]:
    print(f"\nWR热力图 (tail={tail_show:.2f}, cd=600s):")
    print(f"{'W/H_pup':>8}", end="")
    for hp in HPUP_LIST:
        print(f" {hp//60:>6}m", end="")
    print()
    print("-" * (10 + 8 * len(HPUP_LIST)))
    for W in W_LIST:
        print(f"W={W//60:>4}min", end="")
        for hp in HPUP_LIST:
            matches = [r for r in all_results if r["W"]==W and r["H_p_up"]==hp and abs(r["tail"]-tail_show)<0.001]
            if matches and matches[0]["n_sig"] >= 1:
                r = matches[0]
                star = "★" if r["wr"] >= BE and r["pnl"] > 0 else ("~" if r["wr"] >= 50 else "✗")
                print(f" {r['wr']:>4.0f}{star}{r['n_sig']:>2}", end="")
            else:
                print(f"    ---", end="")
        print()

# ============================================================
# Part 3: cd=600 vs cd=W/2 对比
# ============================================================
print(f"\n{'='*100}")
print(f"【Part 3】cd=600(修正) vs cd=W/2(之前) — WR虚高了多少?")
print("-" * 100)

def run_backtest_free_cd(W, H_p_up, tail, cd):
    max_idx = N - H_SETTLE
    if W >= max_idx:
        return None
    indices = np.arange(W, max_idx)
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
                sig_pos.append(i)
            else:
                filtered[i] = 0
    n_sig = len(sig_pos)
    if n_sig == 0:
        return {"n_sig": 0, "wr": 0, "pnl": 0}
    wins = sum(1 for si in sig_pos if
        (future_close[indices[si]] > close[indices[si]] and filtered[si]==1) or
        (future_close[indices[si]] <= close[indices[si]] and filtered[si]==-1))
    return {"n_sig": n_sig, "wr": wins/n_sig*100, "pnl": wins*PAYOUT-(n_sig-wins)*1.0}

print(f"{'W':>6} {'H_pup':>6} {'tail':>5} | {'cd=W/2 N':>8} {'WR':>6} {'PNL':>7} | {'cd=600 N':>8} {'WR':>6} {'PNL':>7} | {'WR差异':>7} {'N稀释':>6}")
print("-" * 90)
compare_combos = [
    (300, 60, 0.05), (300, 60, 0.15), (300, 60, 0.20),
    (300, 120, 0.05), (300, 120, 0.15),
    (300, 300, 0.05), (300, 300, 0.20),
    (600, 300, 0.10), (600, 300, 0.20),
    (600, 600, 0.10), (600, 600, 0.20),
    (900, 300, 0.20), (900, 600, 0.10),
]
for W, H_p_up, tail in compare_combos:
    cd_old = max(W // 2, 30)
    r_old = run_backtest_free_cd(W, H_p_up, tail, cd_old)
    r_new = run_backtest_free_cd(W, H_p_up, tail, 600)
    if r_old["n_sig"] > 0 and r_new["n_sig"] > 0:
        diff = r_old["wr"] - r_new["wr"]
        dilution = r_old["n_sig"] / r_new["n_sig"]
        print(f"{W//60:>4}min {H_p_up//60:>4}min {tail:>5.2f} | {r_old['n_sig']:>8} {r_old['wr']:>5.1f}% {r_old['pnl']:>+6.1f} | {r_new['n_sig']:>8} {r_new['wr']:>5.1f}% {r_new['pnl']:>+6.1f} | {diff:>+6.1f}% {dilution:>5.1f}x")

# ============================================================
# Part 4: p_up校准 (cd=600, 全样本)
# ============================================================
print(f"\n{'='*100}")
print(f"【Part 4】p_up校准 — H_settle=600s实际结果 (全样本, 非信号)")
print("-" * 100)

# 用W=300, H_p_up=300 做校准（全样本量最大）
W_C, HP_C = 300, 300
max_idx_c = N - H_SETTLE
indices_c = np.arange(W_C, max_idx_c)
s_c  = cs_lr[indices_c] - cs_lr[indices_c - W_C]
s2_c = cs_lr2[indices_c] - cs_lr2[indices_c - W_C]
mu_c = s_c / W_C
var_c = np.maximum((s2_c / W_C) - mu_c**2, 0.0) * W_C / (W_C - 1)
sigma_c = np.sqrt(var_c)
z_c = np.sqrt(HP_C) * mu_c / np.maximum(sigma_c, 1e-10)
p_up_c = ncdf(z_c)
actual_c = future_close[indices_c] > close[indices_c]

bins = np.arange(0, 1.05, 0.05)
print(f"参数: W={W_C}s, H_p_up={HP_C}s, H_settle={H_SETTLE}s (全样本)")
print(f"{'p_up区间':>12} | {'样本':>6} | {'实际上涨%':>10} | {'模型预测%':>10} | {'偏差':>8} | {'信号':>6}")
print("-" * 80)
for j in range(len(bins) - 1):
    mask = (p_up_c >= bins[j]) & (p_up_c < bins[j + 1])
    cnt = mask.sum()
    if cnt > 10:
        actual_pct = actual_c[mask].mean() * 100
        pred_pct = (bins[j] + bins[j + 1]) / 2 * 100
        bias = pred_pct - actual_pct
        sig_type = "DN信号" if pred_pct < 25 else ("UP信号" if pred_pct > 75 else "")
        print(f"  {bins[j]:.2f}-{bins[j+1]:.2f} | {cnt:>6} | {actual_pct:>9.1f}% | {pred_pct:>9.1f}% | {bias:>+7.1f}% | {sig_type:>6}")

# ============================================================
# Part 5: 赢输轨迹
# ============================================================
print(f"\n{'='*100}")
print(f"【Part 5】赢输轨迹 — cd=600, H_settle=600s")
print("-" * 100)

# 选N最多且盈利的组合
profitable = [r for r in all_results if r["pnl"] > 0 and r["n_sig"] >= 3]
if profitable:
    # 选信号最多的盈利组合
    best = max(profitable, key=lambda x: x["n_sig"])
    print(f"使用: W={best['W']}s({best['W']//60}m) H_pup={best['H_p_up']}s tail={best['tail']} → {best['n_sig']}信号 WR={best['wr']:.1f}%")
    
    trades = best["trades"]
    win_t = [t for t in trades if t["win"]]
    loss_t = [t for t in trades if not t["win"]]
    
    check_points = list(range(0, H_SETTLE + 1, 60))
    print(f"\n{'秒':>5} | {'赢({}笔)'.format(len(win_t)):>14} | {'输({}笔)'.format(len(loss_t)):>14} | {'差异':>8} |")
    print("-" * 60)
    for sec in check_points:
        w_vals = []
        l_vals = []
        for t in win_t:
            idx = t["idx"] + sec
            if idx < N:
                dev = (close[idx] - t["entry"]) / t["entry"] * 10000
                if t["dir"] == -1: dev = -dev
                w_vals.append(dev)
        for t in loss_t:
            idx = t["idx"] + sec
            if idx < N:
                dev = (close[idx] - t["entry"]) / t["entry"] * 10000
                if t["dir"] == -1: dev = -dev
                l_vals.append(dev)
        w_avg = np.mean(w_vals) if w_vals else 0
        l_avg = np.mean(l_vals) if l_vals else 0
        diff = w_avg - l_avg
        bar = "█" * int(abs(diff)) if abs(diff) > 1 else ""
        print(f"{sec:>4}s | {w_avg:>+10.2f} bps | {l_avg:>+10.2f} bps | {diff:>+6.2f} |{bar}")

# ============================================================
# Part 6: 逐笔交易明细
# ============================================================
print(f"\n{'='*100}")
print(f"【Part 6】逐笔交易明细 — Top5组合")
print("-" * 100)

for rank, r in enumerate(all_results[:5]):
    print(f"\n#{rank+1}: W={r['W']}s({r['W']//60}m) H_pup={r['H_p_up']}s tail={r['tail']} → N={r['n_sig']} WR={r['wr']:.1f}% PNL={r['pnl']:+.1f}")
    for t in r["trades"]:
        d_str = "UP" if t["dir"] == 1 else "DN"
        w_str = "WIN" if t["win"] else "LOSS"
        print(f"  {t['time'].strftime('%H:%M:%S')} | {d_str} entry={t['entry']:.1f} p_up={t['p_up']:.3f} → {w_str}")

# ============================================================
# Part 7: 总结
# ============================================================
print(f"\n{'='*100}")
print(f"【Part 7】总结")
print("-" * 100)
print(f"数据: {N}行, {MINUTES:.0f}分钟 ({MINUTES/60:.1f}h), 波动{(close.max()/close.min()-1)*100:.2f}%")
print(f"cd={CD}s(=H_settle), 理论最大独立信号: {MINUTES/(CD/60):.0f}个")

if profitable:
    print(f"\n盈利组合 (N≥3, PNL>0): {len(profitable)}个")
    for i, r in enumerate(profitable[:10]):
        print(f"  #{i+1}: W={r['W']//60}m H_pup={r['H_p_up']//60}m tail={r['tail']} → N={r['n_sig']} WR={r['wr']:.1f}% PNL={r['pnl']:+.1f} 边际={r['wr']-BE:+.1f}%")
else:
    print(f"\n无N≥3的盈利组合")

n_above = len([r for r in all_results if r["wr"] >= BE])
n_total = len(all_results)
print(f"\n超越盈亏平衡线: {n_above}/{n_total}组合 ({n_above/n_total*100:.0f}%)")
print(f"\n关键: cd=600后信号量大幅减少, 3.7小时最多~22个独立窗口。")
print(f"      需要至少12-24小时数据才能得到≥20个独立信号的可靠统计。")
