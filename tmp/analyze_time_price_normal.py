"""
研究正态分布中时间与价格的关系。
核心问题：时间窗口(W)和预测时长(H)如何影响信号的准确率？
"""
import math
import pandas as pd
import numpy as np
from scipy.stats import norm

df = pd.read_csv("e:/python-binance/data/btcusdt_1m.csv")
df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
for c in ["open","high","low","close","volume"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")
df = df.dropna(subset=["open_time","close"]).sort_values("open_time").reset_index(drop=True)

close = df["close"].values
lr = np.log(close[1:] / close[:-1])
lr = lr[np.isfinite(lr)]
N = len(lr)
days = (df["open_time"].iloc[-1] - df["open_time"].iloc[0]).total_seconds() / 86400

print("=" * 90)
print("第一部分：理论框架")
print("=" * 90)
print("""
假设每分钟收益率 r_i ~ Normal(μ, σ²) 独立同分布

则未来 H 分钟的总收益率：
  R_H = r_1 + r_2 + ... + r_H ~ Normal(Hμ, Hσ²)

  z = Hμ / (σ√H) = √H × (μ/σ)

  P(未来H分钟上涨) = Φ(z)

关键洞察：
  - z 与 √H 成正比（时间越长，信号越强）
  - z 与 μ/σ 成正比（趋势越强 / 波动越小，信号越强）
  - 但 μ 是从过去 W 分钟估计的，假设它代表当前趋势
  - 策略赌的是：μ 所代表的方向在 H 分钟后会反转 ← 这是矛盾点！

矛盾：
  z 增大（趋势信号强） → p_up 远离 0.5 → 触发反转信号
  但 z 大也意味着趋势真的很强 → 反转概率低
  这就是为什么策略在强趋势中亏钱
""")

# ── 第二部分：实证分析 ──
print("=" * 90)
print("第二部分：实证 — z-score 分布与实际涨跌概率")
print("=" * 90)

W = 60  # 固定 window=60min
H_values = [1, 3, 5, 10, 15, 20, 30, 60]

print(f"\nWindow = {W} min, PAYOUT=0.80, 盈亏平衡胜率=55.56%")
print(f"\n{'H(分钟)':>8} | {'样本数':>8} | {'实际涨%':>8} | {'z范围':>16} | {'说明':>20}")
print("-" * 80)

for H in H_values:
    indices = np.arange(W, N - H)
    cumsum = np.cumsum(lr); cumsum2 = np.cumsum(lr**2)
    s = cumsum[indices-1] - cumsum[indices-W-1]
    s2 = cumsum2[indices-1] - cumsum2[indices-W-1]
    mu = s / W
    var = np.maximum((s2/W) - mu**2, 0) * W/(W-1)
    sigma = np.sqrt(var)
    valid = sigma > 1e-10
    z = np.where(valid, np.sqrt(H) * mu / sigma, 0)
    p_up_model = norm.cdf(z)
    
    future_ret = lr[indices + H - 1] + lr[indices + H] if H == 1 else np.array([
        sum(lr[indices[i]:indices[i]+H]) for i in range(len(indices))
    ]) if len(indices) < 50000 else None
    
    # 用 cumsum 快速算 H 分钟总收益
    lr_cumsum = np.cumsum(lr)
    future_total = lr_cumsum[indices + H - 1] - lr_cumsum[indices - 1]
    actual_up = future_total > 0
    
    up_rate = actual_up[valid].mean() * 100
    z_min, z_max = z[valid].min(), z[valid].max()
    
    print(f"{H:>8} | {valid.sum():>8} | {up_rate:>7.1f}% | [{z_min:>+.3f}, {z_max:>+.3f}] | "
          f"{'z跨度↑' if z_max - z_min > 2 else 'z跨度小'}")

# ── 第三部分：按 z-score 分桶看实际涨跌概率 ──
print(f"\n{'='*90}")
print("第三部分：按 p_up 分桶 — 模型概率 vs 实际概率")
print(f"{'='*90}")

H_test = 10  # 固定 horizon=10min
print(f"\nhorizon={H_test}min, window={W}min")
print(f"\n{'p_up区间':>16} | {'样本数':>7} | {'模型概率':>8} | {'实际涨%':>8} | {'差异':>8} | {'分析':>30}")
print("-" * 95)

indices = np.arange(W, N - H_test)
cumsum = np.cumsum(lr); cumsum2 = np.cumsum(lr**2)
s = cumsum[indices-1] - cumsum[indices-W-1]
s2 = cumsum2[indices-1] - cumsum2[indices-W-1]
mu = s / W
var = np.maximum((s2/W) - mu**2, 0) * W/(W-1)
sigma = np.sqrt(var)
valid = sigma > 1e-10
z = np.where(valid, np.sqrt(H_test) * mu / sigma, 0)
p_up_model = norm.cdf(z)

lr_cumsum = np.cumsum(lr)
future_total = lr_cumsum[indices + H_test - 1] - lr_cumsum[indices - 1]
actual_up = future_total > 0

bins = [(0.0, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.25),
        (0.25, 0.35), (0.35, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 0.75),
        (0.75, 0.80), (0.80, 0.85), (0.85, 0.90), (0.90, 0.95), (0.95, 1.0)]

for lo, hi in bins:
    mask = valid & (p_up_model >= lo) & (p_up_model < hi)
    n = mask.sum()
    if n < 10:
        continue
    model_mid = (lo + hi) / 2 * 100
    actual = actual_up[mask].mean() * 100
    diff = actual - model_mid
    
    # 分析
    if abs(diff) < 3:
        analysis = "✓ 模型准确"
    elif diff > 0 and model_mid < 50:
        analysis = "↑ 实际比预期更涨（反转弱）"
    elif diff < 0 and model_mid > 50:
        analysis = "↓ 实际比预期更跌（反转弱）"
    elif diff > 0 and model_mid > 50:
        analysis = "↑ 趋势延续"
    elif diff < 0 and model_mid < 50:
        analysis = "↓ 趋势延续"
    else:
        analysis = "?"
    
    # 策略在尾部做空/做多的话
    if hi <= 0.25:
        strat = "←策略买UP"
    elif lo >= 0.75:
        strat = "←策略买DOWN"
    else:
        strat = ""
    
    print(f"[{lo:.2f}, {hi:.2f}) | {n:>7} | {model_mid:>7.1f}% | {actual:>7.1f}% | {diff:>+7.1f}% | {analysis}{strat}")

# ── 第四部分：反转策略在各 p_up 区间的胜率 ──
print(f"\n{'='*90}")
print("第四部分：反转策略在各 p_up 区间的胜率")
print(f"{'='*90}")
print(f"\n如果 p_up ≤ X → 买UP（赌反转涨），如果 p_up ≥ (1-X) → 买DOWN（赌反转跌）")
print(f"\n{'tail阈值':>10} | {'UP信号数':>8} | {'UP实际涨%':>9} | {'DN信号数':>8} | {'DN实际跌%':>9} | {'合计胜率':>8} | {'合计交易':>8} | {'盈利?':>6}")
print("-" * 100)

for tp in [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]:
    poc = 1 - tp
    mask_up = valid & (p_up_model <= tp)
    mask_dn = valid & (p_up_model >= poc)
    
    n_up = mask_up.sum()
    n_dn = mask_dn.sum()
    
    # UP信号：赌涨，实际涨才算赢
    if n_up > 0:
        up_win = actual_up[mask_up].mean() * 100
    else:
        up_win = 0
    
    # DOWN信号：赌跌，实际跌才算赢
    if n_dn > 0:
        dn_win = (1 - actual_up[mask_dn]).mean() * 100  # 实际跌的比例
    else:
        dn_win = 0
    
    total_trades = n_up + n_dn
    if total_trades > 0:
        total_wins = actual_up[mask_up].sum() + (~actual_up[mask_dn]).sum()
        total_wr = total_wins / total_trades * 100
    else:
        total_wr = 0
    
    pnl_per_trade = total_wr/100 * 0.8 - (1-total_wr/100) * 1.0
    profitable = "✓" if pnl_per_trade > 0 else "✗"
    
    print(f"{'≤'+str(tp):>10} | {n_up:>8} | {up_win:>8.1f}% | {n_dn:>8} | {dn_win:>8.1f}% | "
          f"{total_wr:>7.1f}% | {total_trades:>8} | {profitable} ({pnl_per_trade:+.3f})")

# ── 第五部分：window vs horizon 的最优组合 ──
print(f"\n{'='*90}")
print("第五部分：时间维度热力图 — window(W) × horizon(H) 的胜率")
print(f"tail=0.25, cd=10min")
print(f"{'='*90}")

W_values = [15, 30, 60, 90, 120, 180]
H_values2 = [5, 10, 15, 20, 30]
cd_bars = 10

header = f"{'W/H':>6} |"
for H in H_values2:
    header += f" {H:>5}min |"
header += f" {'说明':>20}"
print(header)
print("-" * len(header))

for W in W_values:
    row = f"{W:>5}m |"
    note = ""
    best_h = None
    best_wr = 0
    for H in H_values2:
        if W >= N - H:
            row += f" {'N/A':>6} |"
            continue
        indices = np.arange(W, N - H)
        cs = np.cumsum(lr); cs2 = np.cumsum(lr**2)
        s = cs[indices-1] - cs[indices-W-1]
        s2 = cs2[indices-1] - cs2[indices-W-1]
        mu_w = s / W
        var_w = np.maximum((s2/W) - mu_w**2, 0) * W/(W-1)
        sigma_w = np.sqrt(var_w)
        valid_w = sigma_w > 1e-10
        z_w = np.where(valid_w, np.sqrt(H) * mu_w / sigma_w, 0)
        p_w = norm.cdf(z_w)
        
        sig_up = valid_w & (p_w <= 0.25)
        sig_dn = valid_w & (p_w >= 0.75)
        
        lcs = np.cumsum(lr)
        future = lcs[indices + H - 1] - lcs[indices - 1]
        act_up = future > 0
        
        # Apply cooldown
        sig_idx = np.where(sig_up | sig_dn)[0]
        wins = 0; trades = 0; last = -999999
        for idx in sig_idx:
            ai = indices[idx]
            if ai - last < cd_bars:
                continue
            if (sig_up[idx] and act_up[idx]) or (sig_dn[idx] and not act_up[idx]):
                wins += 1
            trades += 1
            last = ai
        
        wr = wins/trades*100 if trades > 0 else 0
        row += f" {wr:>5.1f}% |"
        if wr > best_wr:
            best_wr = wr
            best_h = H
    
    if best_wr > 55.56:
        note = f"最佳H={best_h}min ({best_wr:.1f}%)"
    else:
        note = f"最佳H={best_h}min ({best_wr:.1f}%) ✗"
    row += f" {note}"
    print(row)

print(f"""
结论要点：
1. z = √H × (μ/σ) — 时间H以平方根方式放大信号
2. 但H越大，预测越难（H=5min比H=10min容易）
3. W越大，μ越稳定但响应越慢
4. 反转策略在 p_up 极端区间（≤0.10或≥0.90）效果最好
   但信号太少，是"高胜率低频率"的tradeoff
""")
