"""
根因分析：为什么秒级策略有效但分钟级失败？
Root Cause Analysis: Why does the second-level strategy work but minute-level fail?

关键测试：
1. 将秒级数据重采样为分钟K线，在同一时间窗口测试策略
2. 自相关性分析 — 不同时间尺度的均值回归 vs 动量
3. Z-score分布对比 — 秒级 vs 分钟级
4. 方差比检验 (Lo-MacKinlay)
5. 滚动胜率 — 随数据增长WR是否下降
6. 微观结构噪声分析
"""

import numpy as np
import csv
import json
import os
from scipy import stats as sp_stats

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(SCRIPT_DIR, "tmp", "server_1s_trades.csv")
M1_PATH = os.path.join(SCRIPT_DIR, "data", "btcusdt_1m.csv")
OUT_JSON = os.path.join(SCRIPT_DIR, "tmp", "research_root_cause.json")

BE = 0.5556
PAYOUT = 0.80
H_SETTLE = 600  # 10分钟到期

print("=" * 80)
print("根因分析：秒级 vs 分钟级策略效果差异调查")
print("=" * 80)

# ============================================================
# 加载秒级数据
# ============================================================
print("\n[加载数据]")
rows = []
with open(CSV_PATH, "r") as f:
    reader = csv.DictReader(f)
    for r in reader:
        rows.append(r)

print(f"  秒级数据: {len(rows)} 行")
print(f"  时间范围: {rows[0]['timestamp'][:19]} → {rows[-1]['timestamp'][:19]}")

# 提取close价格
prices_1s = np.array([float(r['close']) for r in rows])
ts_1s = [r['timestamp'] for r in rows]
print(f"  价格范围: {prices_1s.min():.1f} - {prices_1s.max():.1f}")

# 秒级log return
lr_1s = np.diff(np.log(prices_1s))
print(f"  1s log returns: mean={lr_1s.mean()*1e6:.2f}μ, std={lr_1s.std()*1e6:.2f}μ")

# ============================================================
# Part 1: 重采样为1分钟K线，在同一窗口测试
# ============================================================
print("\n" + "=" * 80)
print("Part 1: 秒级→分钟级重采样，同窗口策略对比")
print("=" * 80)

# 重采样：每60秒取最后一个close
n_min = len(prices_1s) // 60
prices_1m = np.array([prices_1s[min((i+1)*60-1, len(prices_1s)-1)] for i in range(n_min)])
lr_1m = np.diff(np.log(prices_1m))
print(f"  重采样分钟数据: {len(prices_1m)} 根K线")
print(f"  1m log returns: mean={lr_1m.mean()*1e4:.4f}bps, std={lr_1m.std()*1e4:.2f}bps")

# 秒级策略
def run_strategy_1s(prices, W, H_p_up, tail, cd=600):
    """秒级策略：W=窗口, H_p_up=投影, tail=p_up阈值"""
    log_p = np.log(prices)
    cs_lr = np.cumsum(np.diff(log_p))
    cs_lr2 = np.cumsum(np.diff(log_p)**2)
    n = len(cs_lr)
    max_eval = n - H_SETTLE
    
    signals = []
    last_signal_ts = -999999
    
    for i in range(W, max_eval):
        if i < W:
            continue
        s = cs_lr[i] - cs_lr[i-W]
        s2 = cs_lr2[i] - cs_lr2[i-W]
        mu = s / W
        var = max(s2/W - mu**2, 0.0) * W/(W-1)
        sigma = np.sqrt(var)
        if sigma < 1e-10:
            continue
        
        z = np.sqrt(H_p_up) * mu / sigma
        p_up = 0.5 * (1 + sp_stats.norm.cdf(z) * 2 - 1)  # = ncdf(z)
        # Actually p_up = norm.cdf(z) using scipy
        p_up = sp_stats.norm.cdf(z)
        
        # 信号生成
        direction = 0
        if p_up > 1 - tail:
            direction = -1  # 做空（逆势）
        elif p_up < tail:
            direction = 1   # 做多（逆势）
        
        if direction != 0:
            # CD过滤
            if i - last_signal_ts >= cd:
                # 结算
                entry = prices[i+1]  # 下一秒入场
                settle_idx = i + 1 + H_SETTLE
                if settle_idx < len(prices):
                    settle = prices[settle_idx]
                    win = (direction > 0 and settle > entry) or (direction < 0 and settle < entry)
                    signals.append({
                        'idx': i,
                        'direction': direction,
                        'p_up': p_up,
                        'z': z,
                        'entry': entry,
                        'settle': settle,
                        'win': win
                    })
                    last_signal_ts = i
    
    if len(signals) == 0:
        return 0, 0.0, 0.0, []
    
    wins = sum(1 for s in signals if s['win'])
    wr = wins / len(signals)
    pnl = wins * PAYOUT - (len(signals) - wins) * 1.0
    return len(signals), wr * 100, pnl, signals

# 分钟级策略 (等效参数)
def run_strategy_1m(prices, W, H_p_up, tail, cd=10):
    """分钟级策略：W, H_p_up, tail以分钟为单位"""
    log_p = np.log(prices)
    H_settle_m = 10  # 10分钟到期
    diffs = np.diff(log_p)
    cs_lr = np.cumsum(diffs)
    cs_lr2 = np.cumsum(diffs**2)
    n = len(cs_lr)
    max_eval = n - H_settle_m
    
    signals = []
    last_signal_ts = -999999
    
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
        
        if direction != 0:
            if i - last_signal_ts >= cd:
                entry = prices[i+1]
                settle_idx = i + 1 + H_settle_m
                if settle_idx < len(prices):
                    settle = prices[settle_idx]
                    win = (direction > 0 and settle > entry) or (direction < 0 and settle < entry)
                    signals.append({
                        'idx': i,
                        'direction': direction,
                        'z': z,
                        'entry': entry,
                        'settle': settle,
                        'win': win
                    })
                    last_signal_ts = i
    
    if len(signals) == 0:
        return 0, 0.0, 0.0, []
    
    wins = sum(1 for s in signals if s['win'])
    wr = wins / len(signals)
    pnl = wins * PAYOUT - (len(signals) - wins) * 1.0
    return len(signals), wr * 100, pnl, signals

# 测试秒级策略
print("\n  [秒级策略 — 原始数据]")
for W, H, t, name in [(600, 300, 0.20, "W=600 H=300"), (120, 900, 0.05, "W=120 H=900")]:
    n, wr, pnl, sigs = run_strategy_1s(prices_1s, W, H, t)
    wins = sum(1 for s in sigs if s['win'])
    print(f"    {name} t={t:.2f}: {n}信号 WR={wr:.1f}% PNL={pnl:+.1f} (赢{wins}/输{n-wins})")

# 测试重采样分钟级策略（同一5小时窗口）
print("\n  [分钟级策略 — 重采样数据（同一5小时窗口）]")
# 等效参数: W=10min, H=5min, tail=0.20
for W, H, t, name in [(10, 5, 0.20, "W=10 H=5"), (2, 15, 0.05, "W=2 H=15"), (10, 10, 0.10, "W=10 H=10")]:
    n, wr, pnl, sigs = run_strategy_1m(prices_1m, W, H, t, cd=10)
    wins = sum(1 for s in sigs if s['win'])
    print(f"    {name} t={t:.2f}: {n}信号 WR={wr:.1f}% PNL={pnl:+.1f} (赢{wins}/输{n-wins})")

# ============================================================
# Part 2: 自相关性分析 — 均值回归 vs 动量
# ============================================================
print("\n" + "=" * 80)
print("Part 2: 自相关性分析 — 不同时间尺度的均值回归/动量")
print("=" * 80)

def autocorr(x, max_lag):
    """计算自相关函数"""
    x = x - x.mean()
    var = np.var(x)
    if var < 1e-20:
        return np.zeros(max_lag)
    acf = np.zeros(max_lag)
    for lag in range(1, max_lag + 1):
        acf[lag-1] = np.sum(x[:-lag] * x[lag:]) / (len(x) * var)
    return acf

# 秒级自相关（lag 1-60秒）
print("\n  [秒级log return自相关]")
acf_1s = autocorr(lr_1s, 60)
print(f"    lag=1s:  ρ={acf_1s[0]:.4f}")
print(f"    lag=2s:  ρ={acf_1s[1]:.4f}")
print(f"    lag=5s:  ρ={acf_1s[4]:.4f}")
print(f"    lag=10s: ρ={acf_1s[9]:.4f}")
print(f"    lag=30s: ρ={acf_1s[29]:.4f}")
print(f"    lag=60s: ρ={acf_1s[59]:.4f}")

neg_count = sum(1 for a in acf_1s[:30] if a < 0)
print(f"    前30个lag中负自相关: {neg_count}/30 ({'强均值回归' if neg_count > 18 else '弱/无均值回归'})")

# 分钟级自相关（从97天数据）
print("\n  [分钟级log return自相关 — 97天数据]")
# 读取1分钟数据
m1_data = []
with open(M1_PATH, "r") as f:
    reader = csv.DictReader(f)
    for r in reader:
        try:
            m1_data.append(float(r['close']))
        except:
            pass
m1_prices = np.array(m1_data[-100000:])  # 最后10万根K线
lr_m1_long = np.diff(np.log(m1_prices))
acf_m1 = autocorr(lr_m1_long, 30)
print(f"    lag=1m:  ρ={acf_m1[0]:.4f}")
print(f"    lag=2m:  ρ={acf_m1[1]:.4f}")
print(f"    lag=5m:  ρ={acf_m1[4]:.4f}")
print(f"    lag=10m: ρ={acf_m1[9]:.4f}")
print(f"    lag=30m: ρ={acf_m1[29]:.4f}")
neg_count_m = sum(1 for a in acf_m1[:10] if a < 0)
print(f"    前10个lag中负自相关: {neg_count_m}/10 ({'强均值回归' if neg_count_m > 6 else '弱/无均值回归'})")

# 重采样分钟数据的自相关（同5小时窗口）
print("\n  [重采样分钟级自相关 — 同5小时窗口]")
acf_1m_resample = autocorr(lr_1m, 10)
print(f"    lag=1m:  ρ={acf_1m_resample[0]:.4f}")
print(f"    lag=2m:  ρ={acf_1m_resample[1]:.4f}")
print(f"    lag=5m:  ρ={acf_1m_resample[4]:.4f}")
print(f"    lag=10m: ρ={acf_1m_resample[9]:.4f}")

# ============================================================
# Part 3: 方差比检验 (Variance Ratio / Lo-MacKinlay)
# ============================================================
print("\n" + "=" * 80)
print("Part 3: 方差比检验 (Lo-MacKinlay)")
print("  VR(q) = Var(q期收益) / (q × Var(1期收益))")
print("  VR=1 → 随机游走; VR<1 → 均值回归; VR>1 → 动量")
print("=" * 80)

def variance_ratio(returns, q):
    """计算方差比"""
    n = len(returns)
    if n < 2 * q:
        return np.nan
    var1 = np.var(returns[:n - n % q], ddof=1)
    q_returns = returns[:n - n % q].reshape(-1, q).sum(axis=1)
    varq = np.var(q_returns, ddof=1)
    return varq / (q * var1)

print("\n  [秒级方差比]")
for q in [2, 5, 10, 30, 60, 120, 300, 600]:
    vr = variance_ratio(lr_1s, q)
    label = f"{q}s" if q < 60 else f"{q//60}min"
    interp = "均值回归" if vr < 0.95 else ("动量" if vr > 1.05 else "随机游走")
    print(f"    VR({label:>5s}) = {vr:.4f}  {interp}")

print("\n  [分钟级方差比 — 97天数据]")
for q in [2, 5, 10, 30, 60, 120]:
    vr = variance_ratio(lr_m1_long, q)
    label = f"{q}m" if q < 60 else f"{q//60}h"
    interp = "均值回归" if vr < 0.95 else ("动量" if vr > 1.05 else "随机游走")
    print(f"    VR({label:>5s}) = {vr:.4f}  {interp}")

# ============================================================
# Part 4: Z-score分布对比
# ============================================================
print("\n" + "=" * 80)
print("Part 4: Z-score分布对比 — 秒级 vs 分钟级")
print("=" * 80)

# 秒级z-score (W=600, H=300)
W_1s, H_1s = 600, 300
cs_lr_1s = np.cumsum(lr_1s)
cs_lr2_1s = np.cumsum(lr_1s**2)
z_1s_arr = []
for i in range(W_1s, len(cs_lr_1s)):
    s = cs_lr_1s[i] - cs_lr_1s[i-W_1s]
    s2 = cs_lr2_1s[i] - cs_lr2_1s[i-W_1s]
    mu = s / W_1s
    var = max(s2/W_1s - mu**2, 0.0) * W_1s/(W_1s-1)
    sigma = np.sqrt(var)
    if sigma > 1e-10:
        z = np.sqrt(H_1s) * mu / sigma
        z_1s_arr.append(z)
z_1s_arr = np.array(z_1s_arr)

# 分钟级z-score (W=10, H=5)
W_m, H_m = 10, 5
cs_lr_m = np.cumsum(lr_1m)
cs_lr2_m = np.cumsum(lr_1m**2)
z_m_arr = []
for i in range(W_m, len(cs_lr_m)):
    s = cs_lr_m[i] - cs_lr_m[i-W_m]
    s2 = cs_lr2_m[i] - cs_lr2_m[i-W_m]
    mu = s / W_m
    var = max(s2/W_m - mu**2, 0.0) * W_m/(W_m-1)
    sigma = np.sqrt(var)
    if sigma > 1e-10:
        z = np.sqrt(H_m) * mu / sigma
        z_m_arr.append(z)
z_m_arr = np.array(z_m_arr)

print(f"\n  秒级 z-score (W=600 H=300): N={len(z_1s_arr)}")
print(f"    mean={z_1s_arr.mean():.3f}, std={z_1s_arr.std():.3f}")
print(f"    |z|>1.0: {(np.abs(z_1s_arr)>1.0).mean()*100:.1f}%")
print(f"    |z|>1.5: {(np.abs(z_1s_arr)>1.5).mean()*100:.1f}%")
print(f"    |z|>2.0: {(np.abs(z_1s_arr)>2.0).mean()*100:.1f}%")
print(f"    |z|>2.5: {(np.abs(z_1s_arr)>2.5).mean()*100:.1f}%")
print(f"    |z|>3.0: {(np.abs(z_1s_arr)>3.0).mean()*100:.1f}%")

print(f"\n  重采样分钟级 z-score (W=10 H=5): N={len(z_m_arr)}")
print(f"    mean={z_m_arr.mean():.3f}, std={z_m_arr.std():.3f}")
print(f"    |z|>1.0: {(np.abs(z_m_arr)>1.0).mean()*100:.1f}%")
print(f"    |z|>1.5: {(np.abs(z_m_arr)>1.5).mean()*100:.1f}%")
print(f"    |z|>2.0: {(np.abs(z_m_arr)>2.0).mean()*100:.1f}%")
print(f"    |z|>2.5: {(np.abs(z_m_arr)>2.5).mean()*100:.1f}%")

# 比较极端z-score后的方向预测能力
print(f"\n  [极端z-score后的10分钟方向预测准确率]")
# 秒级
for z_thresh in [1.0, 1.5, 2.0, 2.5]:
    mask_pos = z_1s_arr > z_thresh
    mask_neg = z_1s_arr < -z_thresh
    # 检查z产生后10分钟的价格方向
    correct_rev = 0
    total_rev = 0
    idx_start = W_1s
    for j, z in enumerate(z_1s_arr):
        idx = idx_start + j
        settle_idx = idx + H_SETTLE
        if settle_idx >= len(prices_1s):
            continue
        if abs(z) > z_thresh:
            direction = -1 if z > z_thresh else 1
            price_now = prices_1s[idx]
            price_settle = prices_1s[settle_idx]
            actual_up = price_settle > price_now
            predicted_up = direction > 0
            if actual_up == predicted_up:
                correct_rev += 1
            total_rev += 1
    if total_rev > 0:
        acc = correct_rev / total_rev * 100
        print(f"    秒级 |z|>{z_thresh:.1f}: {total_rev}次, 逆势准确率={acc:.1f}%")

# 分钟级（同窗口重采样）
for z_thresh in [1.0, 1.5, 2.0, 2.5]:
    correct_rev = 0
    total_rev = 0
    idx_start = W_m
    for j, z in enumerate(z_m_arr):
        idx = idx_start + j
        settle_idx = idx + 10  # 10分钟
        if settle_idx >= len(prices_1m):
            continue
        if abs(z) > z_thresh:
            direction = -1 if z > z_thresh else 1
            price_now = prices_1m[idx]
            price_settle = prices_1m[settle_idx]
            actual_up = price_settle > price_now
            predicted_up = direction > 0
            if actual_up == predicted_up:
                correct_rev += 1
            total_rev += 1
    if total_rev > 0:
        acc = correct_rev / total_rev * 100
        print(f"    分钟 |z|>{z_thresh:.1f}: {total_rev}次, 逆势准确率={acc:.1f}%")

# ============================================================
# Part 5: 滚动胜率 — 随数据增长WR是否下降
# ============================================================
print("\n" + "=" * 80)
print("Part 5: 滚动胜率分析 — 增量数据是否降低WR")
print("=" * 80)

# 增量测试：用前N分钟的数据计算WR
for n_min_test in [120, 180, 240, 290]:
    n_sec = n_min_test * 60
    if n_sec > len(prices_1s):
        n_sec = len(prices_1s)
    prices_sub = prices_1s[:n_sec]
    
    for W, H, t, name in [(600, 300, 0.20, "W=600"), (120, 900, 0.05, "W=120")]:
        n, wr, pnl, _ = run_strategy_1s(prices_sub, W, H, t)
        if n > 0:
            print(f"    前{n_min_test}分钟 {name}: {n}信号 WR={wr:.1f}% PNL={pnl:+.1f}")

# ============================================================
# Part 6: Hurst指数 — 长期记忆性
# ============================================================
print("\n" + "=" * 80)
print("Part 6: Hurst指数 — 趋势性 vs 均值回归性")
print("  H<0.5 → 均值回归; H=0.5 → 随机游走; H>0.5 → 趋势性")
print("=" * 80)

def hurst_exponent(prices_series, max_lag=100):
    """使用R/S分析计算Hurst指数"""
    n = len(prices_series)
    lags = range(2, min(max_lag, n//2))
    tau = []
    for lag in lags:
        # 标准差 of lagged differences
        pp = np.array(prices_series[lag:]) - np.array(prices_series[:-lag])
        tau.append(np.std(pp))
    tau = np.array(tau)
    lags = np.array(list(lags), dtype=float)
    # 线性回归 log(tau) vs log(lag)
    valid = tau > 0
    if valid.sum() < 5:
        return 0.5
    slope, _, _, _, _ = sp_stats.linregress(np.log(lags[valid]), np.log(tau[valid]))
    return slope

H_1s = hurst_exponent(np.log(prices_1s), max_lag=300)
H_1m = hurst_exponent(np.log(m1_prices[-5000:]), max_lag=100)
print(f"  秒级 Hurst指数: H={H_1s:.4f} ({'均值回归' if H_1s < 0.45 else '趋势' if H_1s > 0.55 else '随机'})")
print(f"  分钟级 Hurst指数: H={H_1m:.4f} ({'均值回归' if H_1m < 0.45 else '趋势' if H_1m > 0.55 else '随机'})")

# ============================================================
# Part 7: 微观结构 — 秒级价格跳动分析
# ============================================================
print("\n" + "=" * 80)
print("Part 7: 微观结构分析 — 秒级价格跳动特征")
print("=" * 80)

# 每秒价格变化分布
price_changes_1s = np.diff(prices_1s)
zero_changes = (np.abs(price_changes_1s) < 0.05).sum()
print(f"  零变化秒数: {zero_changes}/{len(price_changes_1s)} ({zero_changes/len(price_changes_1s)*100:.1f}%)")
print(f"  正变化: {(price_changes_1s > 0.05).sum()} ({(price_changes_1s > 0.05).mean()*100:.1f}%)")
print(f"  负变化: {(price_changes_1s < -0.05).sum()} ({(price_changes_1s < -0.05).mean()*100:.1f}%)")

# 连续同方向跳动后的反转概率
print(f"\n  [连续同方向跳动后的反转概率]")
for streak_len in [3, 5, 10, 20]:
    reversals = 0
    total = 0
    current_streak = 0
    last_dir = 0
    for i, ch in enumerate(price_changes_1s):
        if abs(ch) < 0.05:
            continue
        direction = 1 if ch > 0 else -1
        if direction == last_dir:
            current_streak += 1
        else:
            if current_streak >= streak_len and i + 1 < len(price_changes_1s):
                next_ch = price_changes_1s[i]
                if (direction > 0 and next_ch < 0) or (direction < 0 and next_ch > 0):
                    reversals += 1
                total += 1
            current_streak = 1
            last_dir = direction
    
    if total > 0:
        rev_prob = reversals / total * 100
        print(f"    连续{streak_len}次同方向后: {total}次, 反转概率={rev_prob:.1f}%")

# Bid-ask bounce分析
print(f"\n  [Bid-ask bounce检测]")
# 检测交替方向跳动
alternating = 0
for i in range(1, len(price_changes_1s)):
    if abs(price_changes_1s[i]) > 0.05 and abs(price_changes_1s[i-1]) > 0.05:
        if price_changes_1s[i] * price_changes_1s[i-1] < 0:
            alternating += 1
total_moves = (np.abs(price_changes_1s) > 0.05).sum()
print(f"  交替方向跳动: {alternating}/{total_moves} ({alternating/max(total_moves,1)*100:.1f}%)")
print(f"  → {'Bid-ask bounce主导' if alternating/max(total_moves,1) > 0.4 else '非bid-ask bounce主导'}")

# ============================================================
# Part 8: 综合诊断
# ============================================================
print("\n" + "=" * 80)
print("Part 8: 综合诊断结论")
print("=" * 80)

print(f"""
╔══════════════════════════════════════════════════════════════╗
║                    综合诊断结果                               ║
╠══════════════════════════════════════════════════════════════╣
║ 1. 自相关性                                                   ║
║    秒级 lag1 ρ = {acf_1s[0]:+.4f} ({'均值回归' if acf_1s[0] < -0.02 else '动量' if acf_1s[0] > 0.02 else '中性'})                     ║
║    分钟级 lag1 ρ = {acf_m1[0]:+.4f} ({'均值回归' if acf_m1[0] < -0.02 else '动量' if acf_m1[0] > 0.02 else '中性'})                     ║
║                                                              ║
║ 2. 方差比                                                     ║
║    秒级 VR(10min) = {variance_ratio(lr_1s, 600):.4f}                         ║
║    分钟级 VR(10min) = {variance_ratio(lr_m1_long, 10):.4f}                         ║
║                                                              ║
║ 3. Hurst指数                                                  ║
║    秒级 H = {H_1s:.4f} ({'均值回归' if H_1s < 0.45 else '趋势' if H_1s > 0.55 else '随机'})                                ║
║    分钟级 H = {H_1m:.4f} ({'均值回归' if H_1m < 0.45 else '趋势' if H_1m > 0.55 else '随机'})                                ║
║                                                              ║
║ 4. 微观结构                                                   ║
║    零变化比例 = {zero_changes/len(price_changes_1s)*100:.1f}%                                   ║
║    交替跳动比例 = {alternating/max(total_moves,1)*100:.1f}%                                   ║
╚══════════════════════════════════════════════════════════════╝
""")

# 保存结果
results = {
    "acf_1s_lag1": float(acf_1s[0]),
    "acf_m1_lag1": float(acf_m1[0]),
    "vr_1s_10min": float(variance_ratio(lr_1s, 600)),
    "vr_m1_10min": float(variance_ratio(lr_m1_long, 10)),
    "hurst_1s": float(H_1s),
    "hurst_1m": float(H_1m),
    "zero_change_pct": float(zero_changes/len(price_changes_1s)*100),
    "alternating_pct": float(alternating/max(total_moves,1)*100),
}
with open(OUT_JSON, "w") as f:
    json.dump(results, f, indent=2)
print(f"\n✓ 结果已保存至 {OUT_JSON}")
