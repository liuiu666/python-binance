"""
Monte Carlo最终检验：
1. 从97天1分钟数据中随机抽取20笔交易的胜率分布
2. 入场时点敏感性分析（秒级精度是否关键）
3. 小样本统计功效分析
4. 最终结论生成
"""

import numpy as np
import csv
import json
import os
from scipy import stats as sp_stats

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_1S = os.path.join(SCRIPT_DIR, "tmp", "server_1s_trades.csv")
M1_PATH = os.path.join(SCRIPT_DIR, "data", "btcusdt_1m.csv")
OUT_JSON = os.path.join(SCRIPT_DIR, "tmp", "research_montecarlo.json")

BE = 0.5556
PAYOUT = 0.80
H_SETTLE = 600
np.random.seed(42)

print("=" * 80)
print("Monte Carlo最终检验 — 秒级策略是否真实有效？")
print("=" * 80)

# ============================================================
# 加载数据
# ============================================================
print("\n[加载秒级数据]")
rows_1s = []
with open(CSV_1S, "r") as f:
    reader = csv.DictReader(f)
    for r in reader:
        rows_1s.append(r)
prices_1s = np.array([float(r['close']) for r in rows_1s])
print(f"  {len(prices_1s)} 行, {rows_1s[0]['timestamp'][:19]} → {rows_1s[-1]['timestamp'][:19]}")

print("\n[加载1分钟数据(97天)]")
m1_data = []
with open(M1_PATH, "r") as f:
    reader = csv.DictReader(f)
    for r in reader:
        try:
            m1_data.append(float(r['close']))
        except:
            pass
m1_prices = np.array(m1_data)
print(f"  {len(m1_prices)} 行")

# ============================================================
# Part 1: Monte Carlo — 从97天数据随机抽20笔，胜率分布
# ============================================================
print("\n" + "=" * 80)
print("Part 1: Monte Carlo模拟 — 随机20笔交易的胜率分布")
print("=" * 80)

# 生成所有可能的1分钟级交易（使用等效z-score策略）
log_m1 = np.log(m1_prices)
diffs_m1 = np.diff(log_m1)
cs_lr_m1 = np.cumsum(diffs_m1)
cs_lr2_m1 = np.cumsum(diffs_m1**2)

all_trade_outcomes = []
W_m, H_m, tail_m = 10, 5, 0.20
for i in range(W_m, len(cs_lr_m1) - 10):
    s = cs_lr_m1[i] - cs_lr_m1[i-W_m]
    s2 = cs_lr2_m1[i] - cs_lr2_m1[i-W_m]
    mu = s / W_m
    var = max(s2/W_m - mu**2, 0.0) * W_m/(W_m-1)
    sigma = np.sqrt(var)
    if sigma < 1e-10:
        continue
    z = np.sqrt(H_m) * mu / sigma
    p_up = sp_stats.norm.cdf(z)
    
    direction = 0
    if p_up > 1 - tail_m:
        direction = -1
    elif p_up < tail_m:
        direction = 1
    
    if direction != 0:
        entry_idx = i + 1
        settle_idx = i + 1 + 10
        if settle_idx < len(m1_prices):
            entry = m1_prices[entry_idx]
            settle = m1_prices[settle_idx]
            win = (direction > 0 and settle > entry) or (direction < 0 and settle < entry)
            all_trade_outcomes.append(win)

all_outcomes = np.array(all_trade_outcomes)
true_wr_long = all_outcomes.mean() * 100
print(f"  97天总信号数: {len(all_outcomes)}")
print(f"  真实长期胜率: {true_wr_long:.2f}%")
print(f"  盈亏平衡胜率: {BE*100:.2f}%")
print(f"  长期PNL/信号: {all_outcomes.mean()*PAYOUT - (1-all_outcomes.mean())*1.0:.4f}")

# Monte Carlo: 随机抽20笔
N_SIM = 100000
sample_sizes = [20, 21, 29]
print(f"\n  Monte Carlo模拟 ({N_SIM}次):")
for n in sample_sizes:
    sample_wrs = np.zeros(N_SIM)
    for b in range(N_SIM):
        sample = np.random.choice(all_outcomes, size=n, replace=True)
        sample_wrs[b] = sample.mean() * 100
    
    pct_above_71 = (sample_wrs >= 71).mean() * 100
    pct_above_75 = (sample_wrs >= 75).mean() * 100
    pct_above_be = (sample_wrs >= BE*100).mean() * 100
    ci_lo = np.percentile(sample_wrs, 2.5)
    ci_hi = np.percentile(sample_wrs, 97.5)
    
    print(f"\n    N={n}笔 (从{len(all_outcomes)}个信号中随机抽):")
    print(f"      95% CI: [{ci_lo:.1f}%, {ci_hi:.1f}%]")
    print(f"      P(WR≥55.6%) = {pct_above_be:.2f}%")
    print(f"      P(WR≥71%)   = {pct_above_71:.2f}%  ← 秒级实测值")
    print(f"      P(WR≥75%)   = {pct_above_75:.2f}%")

# ============================================================
# Part 2: 秒级入场时点敏感性
# ============================================================
print("\n" + "=" * 80)
print("Part 2: 入场时点敏感性 — 秒级精度是否关键？")
print("=" * 80)

lr_1s = np.diff(np.log(prices_1s))
cs_lr = np.cumsum(lr_1s)
cs_lr2 = np.cumsum(lr_1s**2)
CD = 600

# 不同入场延迟测试
def test_entry_delay(prices, W, H_p_up, tail, delay_seconds):
    """测试不同入场延迟的效果"""
    n = len(cs_lr)
    max_eval = n - H_SETTLE - delay_seconds
    
    signals = []
    last_ts = -999999
    
    for i in range(W, max_eval):
        s = cs_lr[i] - cs_lr[i-W]
        s2 = cs_lr2[i] - cs_lr2[i-W]
        mu = s / W
        var = max(s2/W - mu**2, 0.0) * W/(W-1)
        sigma = np.sqrt(var)
        if sigma < 1e-10:
            continue
        
        z = np.sqrt(H_p_up) * mu / sigma
        p_up = sp_stats.norm.cdf(z)
        
        direction = 0
        if p_up > 1 - tail:
            direction = -1
        elif p_up < tail:
            direction = 1
        
        if direction != 0 and (i - last_ts) >= CD:
            entry_idx = i + 1 + delay_seconds
            settle_idx = entry_idx + H_SETTLE
            if settle_idx < len(prices):
                entry = prices[entry_idx]
                settle = prices[settle_idx]
                win = (direction > 0 and settle > entry) or (direction < 0 and settle < entry)
                signals.append(win)
                last_ts = i
    
    if len(signals) == 0:
        return 0, 0.0, 0.0
    wins = sum(signals)
    wr = wins / len(signals) * 100
    pnl = wins * PAYOUT - (len(signals) - wins) * 1.0
    return len(signals), wr, pnl

print(f"\n  W=600 H=300 t=0.20 — 不同入场延迟:")
for delay in [0, 1, 5, 10, 30, 60, 120, 300]:
    n, wr, pnl = test_entry_delay(prices_1s, 600, 300, 0.20, delay)
    if n > 0:
        marker = " ★ 立即入场" if delay == 0 else ""
        print(f"    延迟{delay:>3d}s: {n}信号 WR={wr:.1f}% PNL={pnl:+.1f}{marker}")

print(f"\n  W=120 H=900 t=0.05 — 不同入场延迟:")
for delay in [0, 1, 5, 10, 30, 60, 120, 300]:
    n, wr, pnl = test_entry_delay(prices_1s, 120, 900, 0.05, delay)
    if n > 0:
        marker = " ★ 立即入场" if delay == 0 else ""
        print(f"    延迟{delay:>3d}s: {n}信号 WR={wr:.1f}% PNL={pnl:+.1f}{marker}")

# ============================================================
# Part 3: 小样本统计功效
# ============================================================
print("\n" + "=" * 80)
print("Part 3: 小样本统计功效分析")
print("=" * 80)

# 如果真实WR是60%（略高于BE），需要多少样本才有80%功效检测到？
print("\n  假设真实WR=60%（略高于BE=55.6%）:")
for n in [20, 50, 100, 200, 500, 1000]:
    # 二项检验：P(检测到WR>BE | 真实WR=60%)
    # 用正态近似
    p_true = 0.60
    p_null = BE
    se = np.sqrt(p_true * (1-p_true) / n)
    z_crit = 1.645  # 单侧95%
    threshold = p_null + z_crit * np.sqrt(p_null*(1-p_null)/n)
    power = 1 - sp_stats.norm.cdf((threshold - p_true) / se)
    print(f"    N={n:>5d}: 功效={power*100:.1f}% {'✓足够' if power > 0.8 else '✗不足'}")

print("\n  假设真实WR=55.6%（=BE，策略无效）:")
# P(在N=20时观察到WR≥71%)
for n in [20, 21, 29]:
    p_obs = 0.714
    # 精确二项检验
    prob = sum(sp_stats.binom.pmf(k, n, BE) for k in range(int(p_obs*n), n+1))
    print(f"    N={n}: P(WR≥{p_obs*100:.0f}% | 真实WR=BE) = {prob*100:.2f}%")

print("\n  假设真实WR=51.4%（1分钟长期验证值）:")
for n in [20, 21, 29]:
    p_true = 0.514
    p_obs = 0.714
    prob = sum(sp_stats.binom.pmf(k, n, p_true) for k in range(int(p_obs*n), n+1))
    print(f"    N={n}: P(WR≥{p_obs*100:.0f}% | 真实WR=51.4%) = {prob*100:.4f}%")

# ============================================================
# Part 4: 时段特异性 — 这5小时是否特殊？
# ============================================================
print("\n" + "=" * 80)
print("Part 4: 时段特异性分析 — 这5小时是否特殊？")
print("=" * 80)

# 检查97天数据中，相同UTC时段（13:00-19:00）的WR
print("\n  从97天1分钟数据中提取同时段(13:00-19:00 UTC)信号:")
# 读取时间戳
m1_times = []
with open(M1_PATH, "r") as f:
    reader = csv.DictReader(f)
    for r in reader:
        m1_times.append(r.get('timestamp', r.get('time', '')))

# 统计同时段WR
if m1_times and len(m1_times) == len(m1_prices):
    hour_mask = np.array([13 <= int(t[11:13]) <= 18 if len(t) > 13 else False for t in m1_times[-len(m1_prices):]])
    print(f"    13-19 UTC时段数据占比: {hour_mask.mean()*100:.1f}%")
    
    # 重新计算信号索引（用全量数据的cs_lr）
    all_signal_indices = []
    W_m2, H_m2, tail_m2 = 10, 5, 0.20
    for i in range(W_m2, len(cs_lr_m1) - 10):
        s = cs_lr_m1[i] - cs_lr_m1[i-W_m2]
        s2 = cs_lr2_m1[i] - cs_lr2_m1[i-W_m2]
        mu = s / W_m2
        var = max(s2/W_m2 - mu**2, 0.0) * W_m2/(W_m2-1)
        sigma = np.sqrt(var)
        if sigma < 1e-10:
            continue
        z = np.sqrt(H_m2) * mu / sigma
        p_up = sp_stats.norm.cdf(z)
        
        direction = 0
        if p_up > 1 - tail_m2:
            direction = -1
        elif p_up < tail_m2:
            direction = 1
        
        if direction != 0 and i+11 < len(m1_prices):
            entry = m1_prices[i+1]
            settle = m1_prices[i+11]
            win = (direction > 0 and settle > entry) or (direction < 0 and settle < entry)
            is_target_hour = 13 <= int(m1_times[i+1][11:13]) <= 18 if len(m1_times[i+1]) > 13 else False
            all_signal_indices.append((win, is_target_hour))
    
    if all_signal_indices:
        # 同时段信号
        target_signals = [x[0] for x in all_signal_indices if x[1]]
        other_signals = [x[0] for x in all_signal_indices if not x[1]]
        
        if target_signals:
            wr_target = np.mean(target_signals) * 100
            print(f"    13-19 UTC时段: {len(target_signals)}信号 WR={wr_target:.1f}%")
        if other_signals:
            wr_other = np.mean(other_signals) * 100
            print(f"    其他时段:      {len(other_signals)}信号 WR={wr_other:.1f}%")
else:
    print("    (无法解析时间戳)")

# ============================================================
# Part 5: 秒级信号的独特性 — 是否存在分钟级无法捕获的alpha
# ============================================================
print("\n" + "=" * 80)
print("Part 5: 秒级精度Alpha检测")
print("=" * 80)

# 对秒级策略的每个信号，检查如果改用分钟级入场，WR如何变化
W_1s, H_1s, tail_1s = 600, 300, 0.20
signals_detail = []
last_ts = -999999

for i in range(W_1s, len(cs_lr) - H_SETTLE):
    s = cs_lr[i] - cs_lr[i-W_1s]
    s2 = cs_lr2[i] - cs_lr2[i-W_1s]
    mu = s / W_1s
    var = max(s2/W_1s - mu**2, 0.0) * W_1s/(W_1s-1)
    sigma = np.sqrt(var)
    if sigma < 1e-10:
        continue
    
    z = np.sqrt(H_1s) * mu / sigma
    p_up = sp_stats.norm.cdf(z)
    
    direction = 0
    if p_up > 1 - tail_1s:
        direction = -1
    elif p_up < tail_1s:
        direction = 1
    
    if direction != 0 and (i - last_ts) >= CD:
        # 秒级入场
        entry_1s_idx = i + 1
        settle_1s_idx = entry_1s_idx + H_SETTLE
        if settle_1s_idx < len(prices_1s):
            # 秒级结算
            entry_1s = prices_1s[entry_1s_idx]
            settle_1s = prices_1s[settle_1s_idx]
            win_1s = (direction > 0 and settle_1s > entry_1s) or (direction < 0 and settle_1s < entry_1s)
            
            # 分钟级入场（向下取整到最近的分钟边界）
            minute_idx = (entry_1s_idx // 60) + 1  # 下一分钟
            settle_minute_idx = minute_idx + 10
            if settle_minute_idx < len(prices_1s) // 60 * 60:
                prices_1m_resampled = prices_1s[:len(prices_1s)//60*60].reshape(-1, 60)[:, -1]
                if minute_idx < len(prices_1m_resampled) and settle_minute_idx//60 < len(prices_1m_resampled):
                    entry_1m = prices_1m_resampled[minute_idx//60] if minute_idx % 60 == 0 else prices_1m_resampled[minute_idx//60]
                    settle_1m = prices_1m_resampled[min(settle_minute_idx//60, len(prices_1m_resampled)-1)]
                    win_1m = (direction > 0 and settle_1m > entry_1m) or (direction < 0 and settle_1m < entry_1m)
                    
                    signals_detail.append({
                        'idx': i, 'dir': direction, 'z': z,
                        'win_1s': win_1s, 'win_1m': win_1m,
                        'entry_1s': entry_1s, 'entry_1m': entry_1m
                    })
            last_ts = i

if signals_detail:
    wins_1s = sum(1 for s in signals_detail if s['win_1s'])
    wins_1m = sum(1 for s in signals_detail if s['win_1m'])
    n = len(signals_detail)
    wr_1s = wins_1s / n * 100
    wr_1m = wins_1m / n * 100
    
    # 入场价格偏差
    entry_diffs = [abs(s['entry_1s'] - s['entry_1m']) / s['entry_1s'] * 10000 for s in signals_detail]
    avg_entry_diff = np.mean(entry_diffs)
    
    print(f"  对比同一批信号 ({n}笔):")
    print(f"    秒级入场: WR={wr_1s:.1f}% ({wins_1s}/{n})")
    print(f"    分钟级入场: WR={wr_1m:.1f}% ({wins_1m}/{n})")
    print(f"    WR差异: {wr_1s - wr_1m:+.1f}%")
    print(f"    平均入场价偏差: {avg_entry_diff:.2f} bps")
    
    # 分歧信号
    disagreements = sum(1 for s in signals_detail if s['win_1s'] != s['win_1m'])
    print(f"    分歧信号数: {disagreements}/{n} ({disagreements/n*100:.0f}%)")
    
    # 在分歧信号中，谁更对？
    dis_sd = [s for s in signals_detail if s['win_1s'] != s['win_1m']]
    if dis_sd:
        sd_1s_right = sum(1 for s in dis_sd if s['win_1s'])
        sd_1m_right = sum(1 for s in dis_sd if s['win_1m'])
        print(f"    分歧中秒级对: {sd_1s_right}, 分钟级对: {sd_1m_right}")

# ============================================================
# Part 6: 最终判定
# ============================================================
print("\n" + "=" * 80)
print("Part 6: 最终判定")
print("=" * 80)

# 计算关键概率
# 如果长期真实WR=51.4%，在N=20中观察到71.4%的概率
p_true_1m = true_wr_long / 100
n_obs = 20
wr_obs = 0.714
prob_71_given_51 = sum(sp_stats.binom.pmf(k, n_obs, p_true_1m) for k in range(int(wr_obs*n_obs), n_obs+1))

print(f"""
╔══════════════════════════════════════════════════════════════════╗
║                    最终判定：秒级策略是否真实有效？                 ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  证据1: 1分钟级97天长期验证                                       ║
║    → {len(all_outcomes)}信号 WR={true_wr_long:.2f}% (低于BE={BE*100:.2f}%)              ║
║    → 等效策略在长期数据上完全无效                                 ║
║                                                                  ║
║  证据2: Monte Carlo模拟                                          ║
║    → 从真实分布中抽20笔，P(WR≥71%)={prob_71_given_51*100:.2f}%           ║
║    → 有{prob_71_given_51*100:.1f}%的概率纯属偶然观察到71%WR                    ║
║                                                                  ║
║  证据3: 同窗口重采样对比                                          ║
║    → 秒级: WR=71.4%                                              ║
║    → 分钟级(同5h): WR=61.1%                                      ║
║    → 差异存在但部分来自偶然                                       ║
║                                                                  ║
║  证据4: 方差比结构性差异                                          ║
║    → 秒级VR(10min)=1.37 (强动量)                                ║
║    → 分钟级VR(10min)=0.94 (弱均值回归)                           ║
║    → 秒级和分钟级数据有本质不同的统计结构                         ║
║                                                                  ║
║  证据5: Z-score方向预测能力                                       ║
║    → 秒级|z|>1.0逆势准确率仅51.2% (≈随机)                       ║
║    → 高WR来自CD过滤后的小样本偶然                                 ║
║                                                                  ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  ★ 结论：秒级策略的71%WR极可能是小样本偶然现象                    ║
║                                                                  ║
║  理由：                                                          ║
║  1. 等效参数在97天分钟级数据上WR仅{true_wr_long:.1f}%                        ║
║  2. 从该分布抽20笔有{prob_71_given_51*100:.1f}%概率获得≥71%WR                   ║
║  3. 秒级精度的入场偏差极小(平均{avg_entry_diff:.1f}bps)                    ║
║  4. Z-score的方向预测能力接近随机                                 ║
║                                                                  ║
║  建议：                                                          ║
║  • 不要基于5小时20笔交易部署实盘                                  ║
║  • 需要至少48-72小时秒级数据(200+信号)才能初步验证               ║
║  • 当前数据不足以区分真实alpha和噪声                              ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
""")

# 保存
results = {
    "long_term_wr_1m": float(true_wr_long),
    "n_long_term_signals": int(len(all_outcomes)),
    "prob_71_given_51": float(prob_71_given_51 * 100),
    "wr_1s": float(wr_1s) if signals_detail else 0,
    "wr_1m_same_window": float(wr_1m) if signals_detail else 0,
    "avg_entry_diff_bps": float(avg_entry_diff) if signals_detail else 0,
    "vr_1s_10min": 1.3716,
    "vr_1m_10min": 0.9443,
    "hurst_1s": 0.5132,
    "hurst_1m": 0.4325,
    "verdict": "秒级策略的71%WR极可能是小样本偶然现象"
}
with open(OUT_JSON, "w") as f:
    json.dump(results, f, indent=2, ensure_ascii=False)
print(f"✓ 结果已保存至 {OUT_JSON}")
