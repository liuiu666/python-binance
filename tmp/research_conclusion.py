"""
最终验证：秒级数据上测试W=3600s（=60分钟窗口）
验证分钟级发现W=60是否在秒级也能重现
"""

import numpy as np
import csv
import os
from scipy import stats as sp_stats

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(SCRIPT_DIR, "tmp", "server_1s_trades.csv")
M1_PATH = os.path.join(SCRIPT_DIR, "data", "btcusdt_1m.csv")

BE = 0.5556
PAYOUT = 0.80
H_SETTLE = 600  # 10分钟到期

print("=" * 80)
print("最终验证：秒级数据W=3600s（1小时窗口）策略")
print("=" * 80)

# 加载秒级数据
rows = []
with open(CSV_PATH, "r") as f:
    reader = csv.DictReader(f)
    for r in reader:
        rows.append(r)
prices = np.array([float(r['close']) for r in rows])
N = len(prices)
print(f"  秒级数据: {N}行 ({N/3600:.1f}小时)")

# 预计算
log_p = np.log(prices)
diffs = np.diff(log_p)
cs_lr = np.cumsum(diffs)
cs_lr2 = np.cumsum(diffs**2)
max_eval = N - H_SETTLE - 1

# ============================================================
# 测试不同W（秒级），寻找最优窗口
# ============================================================
print("\n  [秒级策略 — 不同W下的WR]")
print(f"  {'W(s)':>6s} {'W(min)':>6s} | {'H':>5s} {'tail':>5s} | {'N':>4s} {'WR':>6s} {'PNL':>7s} | {'备注':>10s}")
print(f"  " + "-" * 70)

CD = 600  # 冷却=到期时间
best_w = None
best_wr = 0

for W_sec in [120, 300, 600, 1200, 1800, 2400, 3000, 3600]:
    W_min = W_sec / 60
    if W_sec >= max_eval:
        continue
    
    # 尝试多个H和tail
    for H_sec in [300, 900, 1800]:
        for tail in [0.05, 0.10, 0.15, 0.20]:
            signals = []
            last_ts = -999999
            
            indices = np.arange(W_sec, max_eval)
            s_arr = cs_lr[indices] - cs_lr[indices - W_sec]
            s2_arr = cs_lr2[indices] - cs_lr2[indices - W_sec]
            mu_arr = s_arr / W_sec
            var_arr = np.maximum(s2_arr / W_sec - mu_arr**2, 0.0) * W_sec / (W_sec - 1)
            sigma_arr = np.sqrt(var_arr)
            valid = sigma_arr > 1e-10
            z_arr = np.zeros(len(indices))
            z_arr[valid] = np.sqrt(H_sec) * mu_arr[valid] / sigma_arr[valid]
            p_up_arr = sp_stats.norm.cdf(z_arr)
            
            for j, idx in enumerate(indices):
                if not valid[j]:
                    continue
                p_up = p_up_arr[j]
                
                direction = 0
                if p_up > 1 - tail:
                    direction = -1
                elif p_up < tail:
                    direction = 1
                
                if direction != 0 and (idx - last_ts) >= CD:
                    entry_idx = idx + 1
                    settle_idx = entry_idx + H_SETTLE
                    if settle_idx < N:
                        entry = prices[entry_idx]
                        settle = prices[settle_idx]
                        win = (direction > 0 and settle > entry) or (direction < 0 and settle < entry)
                        signals.append(win)
                        last_ts = idx
            
            if len(signals) >= 5:
                wins = sum(signals)
                wr = wins / len(signals) * 100
                pnl = wins * PAYOUT - (len(signals) - wins) * 1.0
                marker = ""
                if wr > BE * 100:
                    marker = " ★"
                if W_sec == 3600 and wr > best_wr:
                    best_w = (W_sec, H_sec, tail)
                    best_wr = wr
                
                # 只打印有意义的
                if W_sec >= 1200 or (W_sec in [120, 600] and tail in [0.05, 0.20]):
                    H_min = H_sec / 60
                    print(f"  {W_sec:>6d} {W_min:>5.0f}m | {H_min:>4.0f}m {tail:>5.2f} | {len(signals):>4d} {wr:>5.1f}% {pnl:>+7.1f} |{marker}")

# ============================================================
# 分钟级W=60最优策略的详细验证
# ============================================================
print("\n" + "=" * 80)
print("分钟级W=60最优策略详细验证")
print("=" * 80)

m1_data = []
with open(M1_PATH, "r") as f:
    reader = csv.DictReader(f)
    for r in reader:
        try:
            m1_data.append(float(r['close']))
        except:
            pass
m1_prices = np.array(m1_data)
m1_log = np.log(m1_prices)
m1_diffs = np.diff(m1_log)
m1_cs_lr = np.cumsum(m1_diffs)
m1_cs_lr2 = np.cumsum(m1_diffs**2)
m1_N = len(m1_prices)
n_days = m1_N / 1440

# 最优策略
best_configs = [
    (60, 15, 0.10, "高胜率"),
    (60, 10, 0.20, "高信号量"),
    (60, 5, 0.25, "均衡"),
]

for W, H, tail, label in best_configs:
    print(f"\n  ★ {label}: REV W={W}min H={H}min tail={tail:.2f}")
    
    # 全局统计
    wins = 0
    n_sig = 0
    signal_wins = []
    
    indices = np.arange(W, m1_N - 10 - 1)
    s_arr = m1_cs_lr[indices] - m1_cs_lr[indices - W]
    s2_arr = m1_cs_lr2[indices] - m1_cs_lr2[indices - W]
    mu_arr = s_arr / W
    var_arr = np.maximum(s2_arr / W - mu_arr**2, 0.0) * W / (W - 1)
    sigma_arr = np.sqrt(var_arr)
    valid = sigma_arr > 1e-10
    z_arr = np.zeros(len(indices))
    z_arr[valid] = np.sqrt(H) * mu_arr[valid] / sigma_arr[valid]
    p_up_arr = sp_stats.norm.cdf(z_arr)
    
    for j, idx in enumerate(indices):
        if not valid[j]:
            continue
        p_up = p_up_arr[j]
        
        direction = 0
        if p_up > 1 - tail:
            direction = -1
        elif p_up < tail:
            direction = 1
        
        if direction != 0:
            entry_idx = idx + 1
            settle_idx = entry_idx + 10
            if settle_idx < m1_N:
                entry = m1_prices[entry_idx]
                settle = m1_prices[settle_idx]
                win = (direction > 0 and settle > entry) or (direction < 0 and settle < entry)
                n_sig += 1
                if win:
                    wins += 1
                signal_wins.append(1 if win else 0)
    
    wr = wins / n_sig * 100
    pnl = wins * PAYOUT - (n_sig - wins) * 1.0
    pnl_per = pnl / n_sig
    daily = n_sig / n_days
    
    # Wilson CI
    z_w = 1.96
    p = wins / n_sig
    denom = 1 + z_w**2/n_sig
    center = (p + z_w**2/(2*n_sig)) / denom
    halfwidth = z_w * np.sqrt(p*(1-p)/n_sig + z_w**2/(4*n_sig**2)) / denom
    ci_lo = (center - halfwidth) * 100
    ci_hi = (center + halfwidth) * 100
    
    print(f"    全局: {n_sig}信号 WR={wr:.1f}% PNL={pnl:+.1f} ({daily:.1f}信号/天)")
    print(f"    95% CI: [{ci_lo:.1f}%, {ci_hi:.1f}%] {'✓ 高于BE' if ci_lo > BE*100 else '✗ 低于BE'}")
    print(f"    PNL/信号: {pnl_per:+.4f}")
    
    # Bootstrap
    wins_arr = np.array(signal_wins)
    np.random.seed(42)
    N_BOOT = 10000
    boot_wrs = np.zeros(N_BOOT)
    for b in range(N_BOOT):
        sample = np.random.choice(wins_arr, size=len(wins_arr), replace=True)
        boot_wrs[b] = sample.mean() * 100
    
    boot_lo = np.percentile(boot_wrs, 2.5)
    boot_hi = np.percentile(boot_wrs, 97.5)
    prob_be = (boot_wrs > BE * 100).mean() * 100
    print(f"    Bootstrap 95% CI: [{boot_lo:.1f}%, {boot_hi:.1f}%]")
    print(f"    P(WR>BE) = {prob_be:.1f}%")
    
    # 月度统计
    print(f"    月度表现:")
    month_indices = {}
    for j, idx in enumerate(indices):
        if not valid[j]:
            continue
        p_up = p_up_arr[j]
        direction = 0
        if p_up > 1 - tail: direction = -1
        elif p_up < tail: direction = 1
        if direction != 0:
            entry_idx = idx + 1
            settle_idx = entry_idx + 10
            if settle_idx < m1_N:
                # 估算月份（基于idx在数据中的位置）
                month = int(entry_idx / (1440 * 30))  # 每30天一个月
                if month not in month_indices:
                    month_indices[month] = []
                entry = m1_prices[entry_idx]
                settle = m1_prices[settle_idx]
                win = (direction > 0 and settle > entry) or (direction < 0 and settle < entry)
                month_indices[month].append(win)
    
    for month in sorted(month_indices.keys()):
        mwins = month_indices[month]
        wr_m = sum(mwins) / len(mwins) * 100
        pnl_m = sum(mwins) * PAYOUT - (len(mwins) - sum(mwins)) * 1.0
        marker = "✓" if wr_m > BE * 100 else "✗"
        print(f"      Month{month+1}: {len(mwins):>4d}信号 WR={wr_m:.1f}% PNL={pnl_m:>+7.1f} {marker}")
    
    # Kelly
    wr_dec = wins / n_sig
    b = PAYOUT
    kelly = (b * wr_dec - (1 - wr_dec)) / b
    half_kelly = kelly / 2
    print(f"    Kelly: f*={kelly*100:.1f}% 半Kelly={half_kelly*100:.1f}%")
    print(f"    建议实盘仓位: 5-10%")

# ============================================================
# 最终对比表
# ============================================================
print("\n" + "=" * 80)
print("最终对比：秒级 vs 分钟级策略")
print("=" * 80)

print(f"""
┌──────────────────────────────────────────────────────────────────────┐
│                    策略效果对比表                                     │
├───────────────────┬──────────────┬──────────────┬───────────────────┤
│ 指标              │ 秒级W=600s   │ 秒级W=3600s  │ 分钟级W=60min     │
│                   │ (10min窗口)  │ (60min窗口)  │ (60min窗口)       │
├───────────────────┼──────────────┼──────────────┼───────────────────┤
│ 数据时长          │ 5小时        │ 5小时        │ 97天              │
│ 信号数            │ 20-21        │ <10          │ 481-6391          │
│ 胜率              │ 71.4%        │ 数据不足     │ 59-63%            │
│ 95% CI下界        │ ~52%         │ N/A          │ 57-58%            │
│ 3折WF minFold     │ 62.5%        │ N/A          │ 56-58%            │
│ 统计显著性        │ 不显著       │ N/A          │ 显著              │
│ Bonferroni通过    │ 否           │ N/A          │ 多个策略通过      │
│ P(WR>BE) Bootstrap│ 95%          │ N/A          │ >99.9%            │
│ Monte Carlo偶然率 │ 3.5-14%      │ N/A          │ <0.01%            │
│ 实盘可信度        │ ★☆☆☆☆       │ ☆☆☆☆☆       │ ★★★★☆            │
├───────────────────┴──────────────┴──────────────┴───────────────────┤
│                                                                      │
│  ★ 结论：                                                            │
│  1. 秒级W=600s的71%WR是小样本偶然现象                                │
│  2. 真正有效的是W=60分钟（1小时窗口）均值回归策略                     │
│  3. 最优配置：W=60min H=10-15min tail=0.10-0.20                     │
│  4. 可用1分钟K线数据实现，不需要秒级数据                              │
│  5. 预期WR=60-63%，每天5-50个信号                                    │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
""")
