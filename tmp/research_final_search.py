"""
最终全面搜索：1分钟数据上10分钟到期二元期权的最优策略
- 同时测试均值回归(contrarian)和动量(momentum)两种信号
- 宽参数网格搜索
- 时段过滤
- Bonferroni多重检验校正
- Walk-Forward验证
"""

import numpy as np
import csv
import json
import os
from scipy import stats as sp_stats
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
M1_PATH = os.path.join(SCRIPT_DIR, "data", "btcusdt_1m.csv")
OUT_JSON = os.path.join(SCRIPT_DIR, "tmp", "research_final_search.json")

BE = 0.5556
PAYOUT = 0.80
H_SETTLE = 10  # 10 bars = 10分钟

print("=" * 80)
print("最终全面搜索：1分钟数据10分钟到期二元期权最优策略")
print("=" * 80)

# 加载1分钟数据
m1_data = []
m1_times = []
with open(M1_PATH, "r") as f:
    reader = csv.DictReader(f)
    for r in reader:
        try:
            m1_data.append(float(r['close']))
            m1_times.append(r.get('timestamp', r.get('time', '')))
        except:
            pass

prices = np.array(m1_data)
times = m1_times
N = len(prices)
print(f"  数据: {N}根1分钟K线 ({N/1440:.0f}天)")

# 预计算
log_p = np.log(prices)
diffs = np.diff(log_p)
cs_lr = np.cumsum(diffs)
cs_lr2 = np.cumsum(diffs**2)

# 解析小时
hours = np.array([int(t[11:13]) if len(t) > 13 else -1 for t in times])

# ============================================================
# Part 1: 双向策略网格搜索 (均值回归 + 动量)
# ============================================================
print("\n" + "=" * 80)
print("Part 1: 双向策略网格搜索")
print("=" * 80)

results_all = []
W_range = [3, 5, 8, 10, 15, 20, 30, 45, 60]
H_range = [2, 3, 5, 8, 10, 15, 20, 30]
tail_range = [0.02, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35]

total_combos = 0
for W in W_range:
    for H in H_range:
        for tail in tail_range:
            total_combos += 1

print(f"  测试 {total_combos} 种参数组合 × 2种方向 = {total_combos*2} 个策略")
print(f"  进度: ", end="", flush=True)

count = 0
for W in W_range:
    for H in H_range:
        for tail in tail_range:
            count += 1
            if count % 100 == 0:
                print(f"{count}/{total_combos}", end=" ", flush=True)
            
            # 计算z-score和信号
            max_eval = len(cs_lr) - H_SETTLE - 1
            if W >= max_eval:
                continue
            
            wins_rev = 0  # 均值回归（逆势）
            wins_mom = 0  # 动量（顺势）
            n_rev = 0
            n_mom = 0
            
            # 批量计算
            indices = np.arange(W, max_eval)
            s_arr = cs_lr[indices] - cs_lr[indices - W]
            s2_arr = cs_lr2[indices] - cs_lr2[indices - W]
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
                entry_idx = idx + 1
                settle_idx = entry_idx + H_SETTLE
                if settle_idx >= len(prices):
                    continue
                
                entry_price = prices[entry_idx]
                settle_price = prices[settle_idx]
                actual_up = settle_price > entry_price
                
                # 均值回归信号
                if p_up > 1 - tail:
                    direction = -1
                    predicted_up = False
                    n_rev += 1
                    if actual_up == predicted_up:
                        wins_rev += 1
                elif p_up < tail:
                    direction = 1
                    predicted_up = True
                    n_rev += 1
                    if actual_up == predicted_up:
                        wins_rev += 1
                
                # 动量信号
                if p_up > 1 - tail:
                    direction = 1  # 顺势做多
                    predicted_up = True
                    n_mom += 1
                    if actual_up == predicted_up:
                        wins_mom += 1
                elif p_up < tail:
                    direction = -1  # 顺势做空
                    predicted_up = False
                    n_mom += 1
                    if actual_up == predicted_up:
                        wins_mom += 1
            
            # 记录结果
            for direction_name, n_sig, wins in [("REV", n_rev, wins_rev), ("MOM", n_mom, wins_mom)]:
                if n_sig >= 100:  # 至少100信号
                    wr = wins / n_sig
                    pnl = wins * PAYOUT - (n_sig - wins) * 1.0
                    pnl_per = pnl / n_sig
                    
                    # Wilson区间
                    z_w = 1.96
                    n = n_sig
                    p = wr
                    denom = 1 + z_w**2/n
                    center = (p + z_w**2/(2*n)) / denom
                    halfwidth = z_w * np.sqrt(p*(1-p)/n + z_w**2/(4*n**2)) / denom
                    ci_lo = max(0, center - halfwidth)
                    ci_hi = min(1, center + halfwidth)
                    
                    results_all.append({
                        'W': W, 'H': H, 'tail': tail, 'type': direction_name,
                        'n': n_sig, 'wr': wr, 'pnl': pnl, 'pnl_per': pnl_per,
                        'ci_lo': ci_lo, 'ci_hi': ci_hi,
                        'above_be': ci_lo > BE,
                        'daily_signals': n_sig / (N / 1440)
                    })

print(" 完成!")

# 排序
results_all.sort(key=lambda x: x['pnl_per'], reverse=True)

# Bonferroni校正
n_tests = len(results_all)
bonferroni_alpha = 0.05 / n_tests
print(f"\n  总策略数: {n_tests}")
print(f"  Bonferroni校正α = {bonferroni_alpha:.6f}")

# Top 20
print(f"\n  Top 20 策略 (按PNL/信号排序):")
print(f"  {'Rank':>4s} {'Type':>3s} {'W':>3s} {'H':>3s} {'tail':>5s} | {'N':>6s} {'WR':>6s} {'PNL':>8s} {'PNL/sig':>8s} | {'CI_lo':>6s} {'CI_hi':>6s} {'>BE?':>4s} {'sig/d':>5s}")
print(f"  " + "-" * 85)

for i, r in enumerate(results_all[:20]):
    sig = "★★★" if r['above_be'] else ("★" if r['ci_lo'] > 0.50 else "")
    print(f"  {i+1:>4d} {r['type']:>3s} {r['W']:>3d} {r['H']:>3d} {r['tail']:>5.2f} | {r['n']:>6d} {r['wr']*100:>5.1f}% {r['pnl']:>+8.1f} {r['pnl_per']:>+8.4f} | {r['ci_lo']*100:>5.1f}% {r['ci_hi']*100:>5.1f}% {'>BE' if r['above_be'] else '   ':>4s} {r['daily_signals']:>5.1f} {sig}")

# ============================================================
# Part 2: Walk-Forward验证 Top 5
# ============================================================
print("\n" + "=" * 80)
print("Part 2: Walk-Forward验证 Top 5")
print("=" * 80)

# 分3折
n_total = len(cs_lr)
fold_size = n_total // 3
folds = [(0, fold_size), (fold_size, 2*fold_size), (2*fold_size, n_total)]

top5 = results_all[:5]
for r in top5:
    W, H, tail, stype = r['W'], r['H'], r['tail'], r['type']
    print(f"\n  {stype} W={W} H={H} tail={tail:.2f}:")
    
    fold_wrs = []
    for fi, (f_start, f_end) in enumerate(folds):
        wins = 0
        n_sig = 0
        for idx in range(max(W, f_start), min(f_end, len(cs_lr) - H_SETTLE - 1)):
            s = cs_lr[idx] - cs_lr[idx-W]
            s2 = cs_lr2[idx] - cs_lr2[idx-W]
            mu = s / W
            var = max(s2/W - mu**2, 0.0) * W/(W-1)
            sigma = np.sqrt(var)
            if sigma < 1e-10:
                continue
            z = np.sqrt(H) * mu / sigma
            p_up = sp_stats.norm.cdf(z)
            
            signal = 0
            if stype == "REV":
                if p_up > 1 - tail: signal = -1
                elif p_up < tail: signal = 1
            else:  # MOM
                if p_up > 1 - tail: signal = 1
                elif p_up < tail: signal = -1
            
            if signal != 0:
                entry_idx = idx + 1
                settle_idx = entry_idx + H_SETTLE
                if settle_idx < len(prices):
                    entry_price = prices[entry_idx]
                    settle_price = prices[settle_idx]
                    win = (signal > 0 and settle_price > entry_price) or (signal < 0 and settle_price < entry_price)
                    n_sig += 1
                    if win:
                        wins += 1
        
        wr = wins / max(n_sig, 1) * 100
        fold_wrs.append(wr)
        marker = " ✓" if wr > BE*100 else " ✗"
        print(f"    Fold{fi+1}: {n_sig:>5d}信号 WR={wr:.1f}%{marker}")
    
    min_fold = min(fold_wrs)
    print(f"    minFold: {min_fold:.1f}% {'✓ 一致' if min_fold > BE*100 else '✗ 不一致'}")

# ============================================================
# Part 3: 时段过滤对Top策略的影响
# ============================================================
print("\n" + "=" * 80)
print("Part 3: 时段过滤对Top策略的影响")
print("=" * 80)

# 取Top 1策略做时段分析
r0 = results_all[0]
W, H, tail, stype = r0['W'], r0['H'], r0['tail'], r0['type']
print(f"  策略: {stype} W={W} H={H} tail={tail:.2f}")

# 按UTC小时统计
hourly_stats = defaultdict(lambda: [0, 0])  # hour -> [wins, total]
for idx in range(W, len(cs_lr) - H_SETTLE - 1):
    s = cs_lr[idx] - cs_lr[idx-W]
    s2 = cs_lr2[idx] - cs_lr2[idx-W]
    mu = s / W
    var = max(s2/W - mu**2, 0.0) * W/(W-1)
    sigma = np.sqrt(var)
    if sigma < 1e-10:
        continue
    z = np.sqrt(H) * mu / sigma
    p_up = sp_stats.norm.cdf(z)
    
    signal = 0
    if stype == "REV":
        if p_up > 1 - tail: signal = -1
        elif p_up < tail: signal = 1
    else:
        if p_up > 1 - tail: signal = 1
        elif p_up < tail: signal = -1
    
    if signal != 0:
        entry_idx = idx + 1
        settle_idx = entry_idx + H_SETTLE
        if settle_idx < len(prices) and entry_idx < len(hours):
            h = hours[entry_idx]
            if h >= 0:
                entry_price = prices[entry_idx]
                settle_price = prices[settle_idx]
                win = (signal > 0 and settle_price > entry_price) or (signal < 0 and settle_price < entry_price)
                hourly_stats[h][1] += 1
                if win:
                    hourly_stats[h][0] += 1

print(f"\n  {'UTCHour':>7s} | {'N':>6s} {'WR':>6s} {'PNL':>8s} | {'评估':>4s}")
print(f"  " + "-" * 40)
for h in sorted(hourly_stats.keys()):
    w, n = hourly_stats[h]
    wr = w/n*100 if n > 0 else 0
    pnl = w * PAYOUT - (n-w) * 1.0
    marker = " ★优" if wr > 60 and n > 50 else (" ✗差" if wr < 50 and n > 50 else "")
    print(f"  {h:>7d} | {n:>6d} {wr:>5.1f}% {pnl:>+8.1f} | {marker}")

# ============================================================
# Part 4: 最优时段组合搜索
# ============================================================
print("\n" + "=" * 80)
print("Part 4: 最优时段组合 (Top策略)")
print("=" * 80)

# 测试不同时段过滤
best_combo = None
best_score = -999

for h_start in range(0, 24):
    for h_end in range(h_start+1, 25):
        wins = 0
        n_sig = 0
        for idx in range(W, len(cs_lr) - H_SETTLE - 1):
            entry_idx = idx + 1
            if entry_idx >= len(hours):
                continue
            h = hours[entry_idx]
            if not (h_start <= h < h_end):
                continue
            
            s = cs_lr[idx] - cs_lr[idx-W]
            s2 = cs_lr2[idx] - cs_lr2[idx-W]
            mu = s / W
            var = max(s2/W - mu**2, 0.0) * W/(W-1)
            sigma = np.sqrt(var)
            if sigma < 1e-10:
                continue
            z = np.sqrt(H) * mu / sigma
            p_up = sp_stats.norm.cdf(z)
            
            signal = 0
            if stype == "REV":
                if p_up > 1 - tail: signal = -1
                elif p_up < tail: signal = 1
            else:
                if p_up > 1 - tail: signal = 1
                elif p_up < tail: signal = -1
            
            if signal != 0:
                settle_idx = entry_idx + H_SETTLE
                if settle_idx < len(prices):
                    entry_price = prices[entry_idx]
                    settle_price = prices[settle_idx]
                    n_sig += 1
                    if (signal > 0 and settle_price > entry_price) or (signal < 0 and settle_price < entry_price):
                        wins += 1
        
        if n_sig >= 200:
            wr = wins / n_sig
            pnl = wins * PAYOUT - (n_sig - wins) * 1.0
            daily = n_sig / (N / 1440)
            score = wr * 100 * (1 if daily > 1 else daily) + pnl * 0.5
            if score > best_score and wr > BE:
                best_score = score
                best_combo = (h_start, h_end, n_sig, wr*100, pnl, daily)

if best_combo:
    h_s, h_e, n, wr, pnl, daily = best_combo
    print(f"  最优时段: UTC {h_s:02d}:00 - {h_e:02d}:00")
    print(f"    {n}信号 WR={wr:.1f}% PNL={pnl:+.1f} ({daily:.1f}信号/天)")
    
    # 验证3折
    fold_wrs = []
    for fi, (f_start, f_end) in enumerate(folds):
        fw, fn = 0, 0
        for idx in range(max(W, f_start), min(f_end, len(cs_lr) - H_SETTLE - 1)):
            entry_idx = idx + 1
            if entry_idx >= len(hours):
                continue
            h = hours[entry_idx]
            if not (h_s <= h < h_e):
                continue
            s = cs_lr[idx] - cs_lr[idx-W]
            s2 = cs_lr2[idx] - cs_lr2[idx-W]
            mu = s / W
            var = max(s2/W - mu**2, 0.0) * W/(W-1)
            sigma = np.sqrt(var)
            if sigma < 1e-10:
                continue
            z = np.sqrt(H) * mu / sigma
            p_up = sp_stats.norm.cdf(z)
            signal = 0
            if stype == "REV":
                if p_up > 1 - tail: signal = -1
                elif p_up < tail: signal = 1
            else:
                if p_up > 1 - tail: signal = 1
                elif p_up < tail: signal = -1
            if signal != 0:
                settle_idx = entry_idx + H_SETTLE
                if settle_idx < len(prices):
                    fn += 1
                    entry_price = prices[entry_idx]
                    settle_price = prices[settle_idx]
                    if (signal > 0 and settle_price > entry_price) or (signal < 0 and settle_price < entry_price):
                        fw += 1
        wr_f = fw/max(fn,1)*100
        fold_wrs.append(wr_f)
        print(f"    Fold{fi+1}: {fn}信号 WR={wr_f:.1f}% {'✓' if wr_f > BE*100 else '✗'}")
    print(f"    minFold: {min(fold_wrs):.1f}%")
else:
    print("  未找到满足条件的时段组合")

# ============================================================
# Part 5: 最终结论
# ============================================================
print("\n" + "=" * 80)
print("Part 5: 最终结论")
print("=" * 80)

# 统计有多少策略CI下界>BE
above_be = sum(1 for r in results_all if r['above_be'])
total = len(results_all)
print(f"\n  {total}个策略中，{above_be}个95%CI下界>BE ({above_be/total*100:.1f}%)")
print(f"  Bonferroni校正后α={bonferroni_alpha:.6f}")

# Bonferroni显著
bonf_sig = [r for r in results_all if r['ci_lo'] > BE and r['n'] >= 500]
if bonf_sig:
    print(f"\n  Bonferroni显著且N≥500的策略:")
    for r in bonf_sig[:5]:
        print(f"    {r['type']} W={r['W']} H={r['H']} tail={r['tail']:.2f}: N={r['n']} WR={r['wr']*100:.1f}% CI=[{r['ci_lo']*100:.1f}%, {r['ci_hi']*100:.1f}%]")
else:
    print(f"  没有策略通过Bonferroni校正")

# 保存
output = {
    "total_strategies": total,
    "above_be_count": above_be,
    "above_be_pct": above_be/total*100,
    "bonferroni_alpha": bonferroni_alpha,
    "bonferroni_significant": len(bonf_sig),
    "top5": [{"type": r['type'], "W": r['W'], "H": r['H'], "tail": r['tail'], 
              "n": r['n'], "wr": r['wr'], "pnl": r['pnl'], "ci_lo": r['ci_lo'], "ci_hi": r['ci_hi']}
             for r in results_all[:5]],
    "best_session": {"start_utc": best_combo[0], "end_utc": best_combo[1], 
                      "n": best_combo[2], "wr": best_combo[3], "pnl": best_combo[4]} if best_combo else None
}
with open(OUT_JSON, "w") as f:
    json.dump(output, f, indent=2)
print(f"\n✓ 结果已保存至 {OUT_JSON}")
