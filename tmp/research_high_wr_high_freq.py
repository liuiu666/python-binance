"""
高胜率高频率研究：量价综合优化
Phase 1-4: 因子扫描
Phase 5: 最优配置完整回测
"""
import math, numpy as np, pandas as pd
from scipy.stats import norm

PAYOUT = 0.80
BREAK_EVEN = 1 / (1 + PAYOUT)  # 55.56%

# ── 数据加载 ──
df = pd.read_csv("e:/python-binance/data/btcusdt_1m.csv")
df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
for c in ["open","high","low","close","volume"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")
df = df.dropna(subset=["open_time","close","volume"]).sort_values("open_time").reset_index(drop=True)

close = df["close"].values.astype(float)
vol = df["volume"].values.astype(float)
lr = np.log(close[1:] / close[:-1])
lr = np.where(np.isfinite(lr), lr, 0)
vol_lr = vol[:-1].copy()  # align volume with lr (both N-1 length)
N = len(lr)
DAYS = N / 1440

# 预计算 cumulative sums for fast windowed stats
cs_lr = np.concatenate([[0], np.cumsum(lr)])
cs_lr2 = np.concatenate([[0], np.cumsum(lr**2)])
cs_vol = np.concatenate([[0], np.cumsum(vol_lr)])

print(f"数据: {N} bars ({DAYS:.1f}天), PAYOUT={PAYOUT}, BE={BREAK_EVEN:.2f}%")

def window_stats(W, max_idx=None):
    """快速计算所有bar的窗口均值/标准差"""
    n = max_idx if max_idx is not None else N
    indices = np.arange(W, n)
    s = cs_lr[indices] - cs_lr[indices - W]
    s2 = cs_lr2[indices] - cs_lr2[indices - W]
    mu = s / W
    var = np.maximum((s2/W) - mu**2, 0) * W/(W-1)
    sigma = np.sqrt(var)
    return indices, mu, sigma

def window_vol_mean(W, indices):
    """窗口平均成交量"""
    return (cs_vol[indices] - cs_vol[indices - W]) / W

def future_return(indices, H):
    """未来H分钟总收益"""
    return cs_lr[indices + H] - cs_lr[indices]

def run_backtest(indices, p_up, actual_up, sig_up_mask, sig_dn_mask, cd_bars=10,
                 vol_filter=None, vol_ratio=None):
    """运行带cooldown的回测, 返回(wins, trades, pnl, peak_dd, max_loss_streak)"""
    sig_mask = sig_up_mask | sig_dn_mask
    if vol_filter is not None and vol_ratio is not None:
        sig_mask = sig_mask & (vol_ratio >= vol_filter)
    
    sig_correct = np.where(sig_up_mask, actual_up, np.where(sig_dn_mask, ~actual_up, False))
    if vol_filter is not None and vol_ratio is not None:
        sig_correct = sig_correct & sig_mask
    
    sig_positions = np.where(sig_mask)[0]
    
    wins = 0; trades = 0; pnl = 0.0
    peak = 0.0; max_dd = 0.0
    cur_loss = 0; max_loss = 0
    
    for idx in sig_positions:
        bar = indices[idx]
        # cooldown check
        if trades > 0 and (bar - last_bar) < cd_bars:
            continue
        last_bar = bar
        
        win = sig_correct[idx]
        trades += 1
        if win:
            pnl += PAYOUT
            cur_loss = 0
            wins += 1
        else:
            pnl -= 1.0
            cur_loss += 1
            max_loss = max(max_loss, cur_loss)
        
        peak = max(peak, pnl)
        dd = peak - pnl
        max_dd = max(max_dd, dd)
    
    return wins, trades, pnl, max_dd, max_loss

# ═══════════════════════════════════════════════════════════
# PHASE 1: 成交量过滤阈值精细扫描
# ═══════════════════════════════════════════════════════════
print(f"\n{'='*110}")
print("PHASE 1: 成交量过滤阈值精细扫描")
print(f"{'='*110}")
print("目标：找到最优量比阈值，同时最大化胜率和交易频率")

W = 60; H = 10
indices, mu_w, sigma_w = window_stats(W, max_idx=N - H)
valid = sigma_w > 1e-10
z = np.where(valid, np.sqrt(H) * mu_w / sigma_w, 0)
p_up = norm.cdf(z)
actual_up = future_return(indices, H) > 0
vol_mean = window_vol_mean(W, indices)
vol_ratio = vol[indices] / np.maximum(vol_mean, 1e-10)

print(f"\n{'量比阈值':>10} | {'信号数':>7} | {'日均':>6} | {'胜率':>7} | {'PNL/笔':>7} | {'总PNL':>8} | {'盈亏比':>7} | {'评价':>12}")
print("-" * 90)

for tail in [0.20, 0.25, 0.30]:
    sig_up = valid & (p_up <= tail)
    sig_dn = valid & (p_up >= 1 - tail)
    
    print(f"\n  --- tail={tail} ---")
    for vthr in [0.0, 0.5, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5]:
        mask = (sig_up | sig_dn)
        if vthr > 0:
            mask = mask & (vol_ratio >= vthr)
        n = mask.sum()
        if n < 20:
            continue
        
        # 无cooldown的快速胜率
        wins_raw = np.where(sig_up, actual_up, np.where(sig_dn, ~actual_up, False)) & mask
        wr_raw = wins_raw.sum() / n * 100
        
        # 带cooldown的完整回测
        wins, trades, pnl, max_dd, max_loss = run_backtest(
            indices, p_up, actual_up, sig_up, sig_dn,
            cd_bars=10, vol_filter=vthr if vthr > 0 else None, vol_ratio=vol_ratio
        )
        
        wr = wins/trades*100 if trades > 0 else 0
        daily = trades / DAYS
        pnl_per = wr/100 * PAYOUT - (1-wr/100) * 1.0
        ratio = pnl / max_dd if max_dd > 0 else 999
        
        if daily >= 10 and pnl_per > 0.05:
            rating = "★★★"
        elif daily >= 5 and pnl_per > 0.03:
            rating = "★★"
        elif pnl_per > 0:
            rating = "★"
        else:
            rating = "✗"
        
        label = f"≥{vthr:.1f}" if vthr > 0 else "不过滤"
        print(f"  {label:>8} | {n:>7} | {daily:>5.1f} | {wr:>6.1f}% | {pnl_per:>+6.3f} | {pnl:>+7.1f} | {ratio:>6.1f} | {rating}")

# ═══════════════════════════════════════════════════════════
# PHASE 2: UP/DOWN 不对称阈值
# ═══════════════════════════════════════════════════════════
print(f"\n{'='*110}")
print("PHASE 2: UP/DOWN 不对称阈值")
print(f"{'='*110}")
print("发现：放量时 UP信号(62.6%) 比 DOWN信号(57.6%) 更强")
print("测试：只用UP信号 / 只用DOWN信号 / 不对称tail")

best_configs_p2 = []

print(f"\n{'模式':>25} | {'量比':>5} | {'信号数':>7} | {'日均':>6} | {'胜率':>7} | {'PNL/笔':>7} | {'总PNL':>8} | {'回撤':>7} | {'最大连亏':>7} | {'评价':>8}")
print("-" * 115)

for vthr in [0.0, 1.0, 1.2, 1.5, 2.0]:
    for tail_up in [0.15, 0.20, 0.25, 0.30, 0.35]:
        for tail_dn in [0.15, 0.20, 0.25, 0.30, 0.35]:
            sig_up = valid & (p_up <= tail_up)
            sig_dn = valid & (p_up >= 1 - tail_dn)
            
            wins, trades, pnl, max_dd, max_loss = run_backtest(
                indices, p_up, actual_up, sig_up, sig_dn,
                cd_bars=10, vol_filter=vthr if vthr > 0 else None, vol_ratio=vol_ratio
            )
            
            if trades < 50:
                continue
            
            wr = wins/trades*100
            daily = trades / DAYS
            pnl_per = wr/100 * PAYOUT - (1-wr/100) * 1.0
            
            if pnl_per <= 0:
                continue
            
            best_configs_p2.append({
                "tail_up": tail_up, "tail_dn": tail_dn, "vthr": vthr,
                "trades": trades, "daily": daily, "wr": wr,
                "pnl": pnl, "pnl_per": pnl_per, "max_dd": max_dd,
                "max_loss": max_loss
            })

# 按 PNL 排序
best_configs_p2.sort(key=lambda x: -x["pnl"])
print("\n  >>> TOP 15 PNL:")
for i, c in enumerate(best_configs_p2[:15]):
    sym = "对称" if c["tail_up"] == c["tail_dn"] else "不对称"
    rating = "★★★" if c["daily"] >= 10 and c["pnl_per"] > 0.05 else ("★★" if c["daily"] >= 5 else "★")
    print(f"  UP≤{c['tail_up']:.2f}/DN≥{1-c['tail_dn']:.2f} v≥{c['vthr']:.1f} | {c['trades']:>5} | {c['daily']:>5.1f} | {c['wr']:>6.1f}% | {c['pnl_per']:>+6.3f} | {c['pnl']:>+7.1f} | {c['max_dd']:>6.1f} | {c['max_loss']:>7} | {rating} {sym}")

# 按胜率(>150笔)排序
best_configs_p2.sort(key=lambda x: -x["wr"])
print("\n  >>> TOP 10 胜率 (>150笔):")
for i, c in enumerate([c for c in best_configs_p2 if c["trades"] >= 150][:10]):
    rating = "★★★" if c["daily"] >= 10 else ("★★" if c["daily"] >= 5 else "★")
    print(f"  UP≤{c['tail_up']:.2f}/DN≥{1-c['tail_dn']:.2f} v≥{c['vthr']:.1f} | {c['trades']:>5} | {c['daily']:>5.1f} | {c['wr']:>6.1f}% | {c['pnl_per']:>+6.3f} | {c['pnl']:>+7.1f} | {c['max_dd']:>6.1f} | {c['max_loss']:>7} | {rating}")

# 按风险调整(PNL/MaxDD)排序
best_configs_p2.sort(key=lambda x: -x["pnl"]/max(x["max_dd"], 0.1))
print("\n  >>> TOP 10 风险调整比 (PNL/MaxDD):")
for i, c in enumerate([c for c in best_configs_p2 if c["trades"] >= 100][:10]):
    ratio = c["pnl"]/max(c["max_dd"], 0.1)
    rating = "★★★" if c["daily"] >= 10 else ("★★" if c["daily"] >= 5 else "★")
    print(f"  UP≤{c['tail_up']:.2f}/DN≥{1-c['tail_dn']:.2f} v≥{c['vthr']:.1f} | {c['trades']:>5} | {c['daily']:>5.1f} | {c['wr']:>6.1f}% | ratio={ratio:>5.1f} | {c['pnl']:>+7.1f} | dd={c['max_dd']:>6.1f} | {rating}")

# ═══════════════════════════════════════════════════════════
# PHASE 3: 量价融合信号
# ═══════════════════════════════════════════════════════════
print(f"\n{'='*110}")
print("PHASE 3: 量价融合信号 — 把成交量融入z-score")
print(f"{'='*110}")
print("思路A: 量加权均值(VWAP-based mu)")
print("思路B: 量加权方差(大成交量bar权重更高)")
print("思路C: 量价比率直接乘到z上 (z_vol = z × f(vol_ratio))")
print("思路D: OBV斜率替代简单收益率")

# --- 思路A: Volume-Weighted Mean ---
print(f"\n  --- 思路A: 量加权均值 ---")
print(f"  mu_vol = Σ(w_i × lr_i × v_i) / Σ(v_i)  其中 w_i=1 (等权量加权)")
print(f"  原始mu用价格收益, 新mu用成交量加权的价格收益")

# 计算量加权均值
cs_lr_vol = np.concatenate([[0], np.cumsum(lr * vol_lr)])
cs_vol_total = np.concatenate([[0], np.cumsum(vol_lr)])

W = 60; H = 10
indices_a = np.arange(W, N - H)
mu_vol = (cs_lr_vol[indices_a] - cs_lr_vol[indices_a - W]) / (cs_vol_total[indices_a] - cs_vol_total[indices_a - W] + 1e-10)
# 量加权方差
lr_vol_centered = lr * vol - mu_vol[:, None] if False else None  # Too slow, use approximation
# 近似: var_vol ≈ Σ(v_i × (lr_i - mu_vol)²) / Σ(v_i)
cs_lr2_vol = np.concatenate([[0], np.cumsum(vol_lr * lr**2)])
var_vol_approx = (cs_lr2_vol[indices_a] - cs_lr2_vol[indices_a - W]) / (cs_vol_total[indices_a] - cs_vol_total[indices_a - W] + 1e-10) - mu_vol**2
var_vol_approx = np.maximum(var_vol_approx, 0)
sigma_vol = np.sqrt(var_vol_approx * W / (W-1))  # scale similar to sample std

valid_a = sigma_vol > 1e-10
z_vol_a = np.where(valid_a, np.sqrt(H) * mu_vol / sigma_vol, 0)
p_up_vol_a = norm.cdf(z_vol_a)

# 对齐 actual_up
actual_up_a = future_return(indices_a, H) > 0
vol_ratio_a = vol_lr[indices_a] / np.maximum((cs_vol_total[indices_a] - cs_vol_total[indices_a - W]) / W, 1e-10)

print(f"\n  {'tail':>6} | {'量比':>5} | {'信号数':>7} | {'日均':>6} | {'原始胜率':>8} | {'量加权胜率':>9} | {'差异':>7}")
print("  " + "-" * 75)

for tail in [0.20, 0.25, 0.30]:
    for vthr in [0.0, 1.0, 1.2, 2.0]:
        # 原始信号
        sig_orig = valid & (p_up <= tail) | (p_up >= 1-tail)
        if vthr > 0: sig_orig &= (vol_ratio >= vthr)
        
        # 量加权信号
        sig_vol = valid_a & ((p_up_vol_a <= tail) | (p_up_vol_a >= 1-tail))
        if vthr > 0: sig_vol &= (vol_ratio_a >= vthr)
        
        if sig_vol.sum() < 50:
            continue
        
        wr_orig = (np.where(p_up <= tail, actual_up, np.where(p_up >= 1-tail, ~actual_up, False)) & sig_orig).sum() / max(sig_orig.sum(), 1) * 100
        
        # 对齐索引（两个用同样的indices_a区间）
        sig_up_v = valid_a & (p_up_vol_a <= tail)
        sig_dn_v = valid_a & (p_up_vol_a >= 1-tail)
        if vthr > 0:
            sig_up_v &= (vol_ratio_a >= vthr)
            sig_dn_v &= (vol_ratio_a >= vthr)
        
        wins_v = np.where(sig_up_v, actual_up_a, np.where(sig_dn_v, ~actual_up_a, False))
        mask_v = sig_up_v | sig_dn_v
        wr_vol = wins_v[mask_v].sum() / max(mask_v.sum(), 1) * 100
        
        diff = wr_vol - wr_orig
        print(f"  {tail:>6.2f} | ≥{vthr:>4.1f} | {mask_v.sum():>7} | {mask_v.sum()/DAYS:>5.1f} | {wr_orig:>7.1f}% | {wr_vol:>8.1f}% | {diff:>+6.1f}%")

# --- 思路C: z乘以量比函数 ---
print(f"\n  --- 思路C: z_vol = z × log(1 + vol_ratio) ---")
print(f"  放量时放大z → 更多极端信号 → 触发更多但更准")

for vol_func_name, vol_func in [
    ("log(1+vr)", lambda vr: np.log1p(vr)),
    ("sqrt(vr)", lambda vr: np.sqrt(np.maximum(vr, 0))),
    ("vr^0.5", lambda vr: np.maximum(vr, 0)**0.5),
    ("min(vr,3)/1.5", lambda vr: np.minimum(vr, 3) / 1.5),
]:
    z_adj = z * vol_func(vol_ratio)
    p_up_adj = norm.cdf(z_adj)
    
    print(f"\n  调整函数: {vol_func_name}")
    print(f"  {'tail':>6} | {'信号数':>7} | {'日均':>6} | {'胜率':>7} | {'PNL/笔':>7} | {'vs原始':>7}")
    print("  " + "-" * 65)
    
    for tail in [0.20, 0.25, 0.30]:
        sig_up_a = valid & (p_up_adj <= tail)
        sig_dn_a = valid & (p_up_adj >= 1-tail)
        mask_a = sig_up_a | sig_dn_a
        n = mask_a.sum()
        if n < 50:
            continue
        
        wins_a = np.where(sig_up_a, actual_up, np.where(sig_dn_a, ~actual_up, False))
        wr_a = wins_a[mask_a].sum() / n * 100
        
        # 原始同tail
        sig_orig = valid & ((p_up <= tail) | (p_up >= 1-tail))
        wins_orig = np.where(p_up <= tail, actual_up, np.where(p_up >= 1-tail, ~actual_up, False))
        wr_orig = wins_orig[sig_orig].sum() / max(sig_orig.sum(), 1) * 100
        
        pnl_per = wr_a/100 * PAYOUT - (1-wr_a/100) * 1.0
        diff = wr_a - wr_orig
        
        print(f"  {tail:>6.2f} | {n:>7} | {n/DAYS:>5.1f} | {wr_a:>6.1f}% | {pnl_per:>+6.3f} | {diff:>+6.1f}%")

# ═══════════════════════════════════════════════════════════
# PHASE 4: 多窗口组合
# ═══════════════════════════════════════════════════════════
print(f"\n{'='*110}")
print("PHASE 4: 多窗口组合 — 增加信号多样性")
print(f"{'='*110}")
print("不同window产生不同信号，组合使用可以增加频率")

# 计算3个窗口的信号
W_configs = [30, 60, 90]
tail = 0.25; vthr = 1.2

all_sigs = {}
for W_i in W_configs:
    idx_i, mu_i, sig_i = window_stats(W_i, max_idx=N - H)
    valid_i = sig_i > 1e-10
    z_i = np.where(valid_i, np.sqrt(H) * mu_i / sig_i, 0)
    p_i = norm.cdf(z_i)
    vm_i = window_vol_mean(W_i, idx_i)
    vr_i = vol[idx_i] / np.maximum(vm_i, 1e-10)
    au_i = future_return(idx_i, H) > 0
    
    sig_up_i = valid_i & (p_i <= tail) & (vr_i >= vthr)
    sig_dn_i = valid_i & (p_i >= 1-tail) & (vr_i >= vthr)
    
    # 映射到全局bar索引
    sig_up_global = np.zeros(N, dtype=bool)
    sig_dn_global = np.zeros(N, dtype=bool)
    actual_up_global = np.zeros(N, dtype=bool)
    valid_global = np.zeros(N, dtype=bool)
    
    for j, bi in enumerate(idx_i):
        if bi < N:
            sig_up_global[bi] = sig_up_i[j]
            sig_dn_global[bi] = sig_dn_i[j]
            actual_up_global[bi] = au_i[j]
            valid_global[bi] = True
    
    all_sigs[W_i] = {
        "sig_up": sig_up_global, "sig_dn": sig_dn_global,
        "actual_up": actual_up_global, "valid": valid_global
    }

# 策略1: 单W=60 (基线)
# 策略2: 三个W取并集（任一触发就交易）
# 策略3: 三个W投票（至少2个同方向）
# 策略4: 三个W取并集 + cd=5（缩短cooldown因为信号更独立）

print(f"\ntail={tail}, vol≥{vthr}, cd=10")
print(f"\n{'策略':>25} | {'信号数':>7} | {'日均':>6} | {'胜率':>7} | {'总PNL':>8} | {'回撤':>7} | {'连亏':>5}")
print("-" * 85)

for name, combiner, cd in [
    ("单W=60 (基线)", "single", 10),
    ("W∈{30,60,90} 并集", "union", 10),
    ("W∈{30,60,90} 并集 cd=5", "union", 5),
    ("W∈{30,60,90} ≥2票", "vote2", 10),
    ("W∈{30,60,90} ≥2票 cd=5", "vote2", 5),
    ("W∈{30,60,90} 3票一致", "vote3", 10),
]:
    if combiner == "single":
        s = all_sigs[60]
        sig_up_total = s["sig_up"]; sig_dn_total = s["sig_dn"]
        au_total = s["actual_up"]
    elif combiner == "union":
        sig_up_total = all_sigs[30]["sig_up"] | all_sigs[60]["sig_up"] | all_sigs[90]["sig_up"]
        sig_dn_total = all_sigs[30]["sig_dn"] | all_sigs[60]["sig_dn"] | all_sigs[90]["sig_dn"]
        au_total = all_sigs[60]["actual_up"]
    elif combiner == "vote2":
        up_votes = all_sigs[30]["sig_up"].astype(int) + all_sigs[60]["sig_up"].astype(int) + all_sigs[90]["sig_up"].astype(int)
        dn_votes = all_sigs[30]["sig_dn"].astype(int) + all_sigs[60]["sig_dn"].astype(int) + all_sigs[90]["sig_dn"].astype(int)
        sig_up_total = up_votes >= 2
        sig_dn_total = dn_votes >= 2
        au_total = all_sigs[60]["actual_up"]
    elif combiner == "vote3":
        sig_up_total = all_sigs[30]["sig_up"] & all_sigs[60]["sig_up"] & all_sigs[90]["sig_up"]
        sig_dn_total = all_sigs[30]["sig_dn"] & all_sigs[60]["sig_dn"] & all_sigs[90]["sig_dn"]
        au_total = all_sigs[60]["actual_up"]
    
    # 回测
    sig_mask = sig_up_total | sig_dn_total
    sig_correct = np.where(sig_up_total, au_total, np.where(sig_dn_total, ~au_total, False))
    
    sig_positions = np.where(sig_mask)[0]
    wins = 0; trades = 0; pnl = 0.0
    peak = 0.0; max_dd = 0.0; cur_loss = 0; max_loss = 0
    last_bar = -999
    
    for bar in sig_positions:
        if trades > 0 and (bar - last_bar) < cd:
            continue
        last_bar = bar
        win = sig_correct[bar]
        trades += 1
        if win:
            pnl += PAYOUT; cur_loss = 0; wins += 1
        else:
            pnl -= 1.0; cur_loss += 1; max_loss = max(max_loss, cur_loss)
        peak = max(peak, pnl); max_dd = max(max_dd, peak - pnl)
    
    wr = wins/trades*100 if trades > 0 else 0
    daily = trades / DAYS
    pnl_per = wr/100 * PAYOUT - (1-wr/100) * 1.0
    rating = "✓" if pnl_per > 0.05 else ("~" if pnl_per > 0 else "✗")
    
    print(f"{name:>25} | {trades:>7} | {daily:>5.1f} | {wr:>6.1f}% | {pnl:>+7.1f} | {max_dd:>6.1f} | {max_loss:>5} {rating}")

# ═══════════════════════════════════════════════════════════
# PHASE 5: 最优配置完整回测
# ═══════════════════════════════════════════════════════════
print(f"\n{'='*110}")
print("PHASE 5: 最优配置完整回测")
print(f"{'='*110}")

# 从Phase 1-4中选出最优配置
print("\n测试候选最优配置：")
print(f"\n{'#':>3} | {'配置':>45} | {'交易':>6} | {'日均':>5} | {'胜率':>7} | {'PNL':>8} | {'回撤':>7} | {'连亏':>5} | {'PNL/DD':>6} | {'评价':>10}")
print("-" * 130)

candidates = [
    # (name, tail_up, tail_dn, vthr, cd, z_adj_func_name)
    ("当前生产: tail=0.25/W60/cd10/无过滤", 0.25, 0.25, 0.0, 10, None),
    ("+量≥1.0", 0.25, 0.25, 1.0, 10, None),
    ("+量≥1.2", 0.25, 0.25, 1.2, 10, None),
    ("+量≥1.5", 0.25, 0.25, 1.5, 10, None),
    ("+量≥2.0", 0.25, 0.25, 2.0, 10, None),
    ("tail=0.20 + 量≥1.2", 0.20, 0.20, 1.2, 10, None),
    ("tail=0.20 + 量≥1.0", 0.20, 0.20, 1.0, 10, None),
    ("不对称 UP0.20/DN0.30 + 量≥1.0", 0.20, 0.30, 1.0, 10, None),
    ("不对称 UP0.20/DN0.25 + 量≥1.2", 0.20, 0.25, 1.2, 10, None),
    ("对称0.25 + 量≥1.2 + cd=5", 0.25, 0.25, 1.2, 5, None),
    ("对称0.20 + 量≥1.2 + cd=5", 0.20, 0.20, 1.2, 5, None),
    ("z_adj log(1+vr) tail=0.25 cd=10", 0.25, 0.25, 0.0, 10, "log"),
    ("z_adj log(1+vr) tail=0.25 + cd=5", 0.25, 0.25, 0.0, 5, "log"),
    ("z_adj sqrt(vr) tail=0.25 cd=10", 0.25, 0.25, 0.0, 10, "sqrt"),
    ("z_adj log tail=0.20 cd=10", 0.20, 0.20, 0.0, 10, "log"),
]

for i, (name, tu, td, vthr, cd, adj) in enumerate(candidates):
    if adj == "log":
        z_use = z * np.log1p(vol_ratio)
    elif adj == "sqrt":
        z_use = z * np.sqrt(vol_ratio)
    else:
        z_use = z
    
    p_use = norm.cdf(z_use)
    
    sig_up_c = valid & (p_use <= tu)
    sig_dn_c = valid & (p_use >= 1 - td)
    if vthr > 0:
        sig_up_c &= (vol_ratio >= vthr)
        sig_dn_c &= (vol_ratio >= vthr)
    
    sig_mask_c = sig_up_c | sig_dn_c
    sig_correct_c = np.where(sig_up_c, actual_up, np.where(sig_dn_c, ~actual_up, False))
    
    sig_positions = np.where(sig_mask_c)[0]
    wins = 0; trades = 0; pnl = 0.0
    peak = 0.0; max_dd = 0.0; cur_loss = 0; max_loss = 0
    last_bar = -999
    
    for bar in sig_positions:
        bi = indices[bar]
        if trades > 0 and (bi - last_bar) < cd:
            continue
        last_bar = bi
        win = sig_correct_c[bar]
        trades += 1
        if win:
            pnl += PAYOUT; cur_loss = 0; wins += 1
        else:
            pnl -= 1.0; cur_loss += 1; max_loss = max(max_loss, cur_loss)
        peak = max(peak, pnl); max_dd = max(max_dd, peak - pnl)
    
    wr = wins/trades*100 if trades > 0 else 0
    daily = trades / DAYS
    pnl_per = wr/100 * PAYOUT - (1-wr/100) * 1.0
    ratio = pnl / max_dd if max_dd > 0 else 999
    
    if daily >= 10 and pnl_per > 0.05:
        rating = "★★★ 优秀"
    elif daily >= 5 and pnl_per > 0.03:
        rating = "★★ 可用"
    elif pnl_per > 0:
        rating = "★ 勉强"
    else:
        rating = "✗"
    
    print(f"{i+1:>3} | {name:>45} | {trades:>6} | {daily:>5.1f} | {wr:>6.1f}% | {pnl:>+7.1f} | {max_dd:>6.1f} | {max_loss:>5} | {ratio:>5.1f} | {rating}")

print(f"\n{'='*110}")
print("研究完成")
print(f"{'='*110}")
