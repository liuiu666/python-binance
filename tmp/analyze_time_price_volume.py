"""
时间 × 价格 × 成交量 综合分析
数据源：
  btcusdt_1m.csv      — 1分钟 OHLCV (96天)
  btcusdt_taker.csv   — 5分钟 taker 买卖比 (38天)
  btcusdt_lsratio.csv — 5分钟 多空比 (38天)
  btcusdt_funding.csv — 8小时 资金费率 (96天)
"""
import math
import numpy as np
import pandas as pd
from scipy.stats import norm

PAYOUT = 0.80
BREAK_EVEN = 1 / (1 + PAYOUT)

# ── 加载主数据 ──
df = pd.read_csv("e:/python-binance/data/btcusdt_1m.csv")
df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
for c in ["open","high","low","close","volume"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")
df = df.dropna(subset=["open_time","close","volume"]).sort_values("open_time").reset_index(drop=True)

close = df["close"].values
vol = df["volume"].values
lr = np.log(close[1:] / close[:-1])
lr = np.where(np.isfinite(lr), lr, 0)
N = len(lr)

print(f"数据量: {N} 根1分钟K线 ({N/1440:.1f}天)")

# ── 加载 taker 数据 ──
taker = pd.read_csv("e:/python-binance/data/btcusdt_taker.csv")
taker["timestamp"] = pd.to_datetime(taker["timestamp"], utc=True)
taker = taker.sort_values("timestamp").reset_index(drop=True)
taker["taker_ratio"] = taker["buyVol"] / taker["sellVol"].replace(0, np.nan)
taker = taker.dropna(subset=["taker_ratio"])
print(f"Taker数据: {len(taker)} 行 ({len(taker)/288:.1f}天)")

# ── 加载 L/S ratio ──
ls = pd.read_csv("e:/python-binance/data/btcusdt_lsratio.csv")
ls["timestamp"] = pd.to_datetime(ls["timestamp"], utc=True)
ls = ls.sort_values("timestamp").reset_index(drop=True)
print(f"多空比数据: {len(ls)} 行 ({len(ls)/288:.1f}天)")

# ── 加载 funding ──
fund = pd.read_csv("e:/python-binance/data/btcusdt_funding.csv")
fund["fundingTime"] = pd.to_datetime(fund["fundingTime"], utc=True, format="mixed")
fund = fund.sort_values("fundingTime").reset_index(drop=True)
print(f"资金费率数据: {len(fund)} 行 ({len(fund)/3:.1f}天)")

# ── 基础信号计算 (W=60, H=10) ──
W, H = 60, 10
cs = np.cumsum(lr); cs2 = np.cumsum(lr**2)
cs = np.concatenate([[0], cs])
cs2 = np.concatenate([[0], cs2])

indices = np.arange(W, N - H)
s = cs[indices] - cs[indices - W]
s2 = cs2[indices] - cs2[indices - W]
mu = s / W
var = np.maximum((s2/W) - mu**2, 0) * W/(W-1)
sigma = np.sqrt(var)
valid = sigma > 1e-10
z = np.where(valid, np.sqrt(H) * mu / sigma, 0)
p_up = norm.cdf(z)

# 未来 H 分钟实际涨跌
lcs = np.cumsum(lr); lcs = np.concatenate([[0], lcs])
future_total = lcs[indices + H] - lcs[indices]
actual_up = future_total > 0

# 信号 mask
tail = 0.25
sig_up = valid & (p_up <= tail)
sig_dn = valid & (p_up >= 1 - tail)
sig_any = sig_up | sig_dn

# 信号正确性
sig_correct = np.where(sig_up, actual_up, np.where(sig_dn, ~actual_up, False))

# ════════════════════════════════════════════════════════
print(f"\n{'='*100}")
print("第一部分：成交量与信号准确率的关系")
print(f"{'='*100}")

# 计算每根 bar 的成交量
bar_vol = vol[indices]  # 当前 bar 的量
window_vol = np.array([vol[indices[i]-W+1:indices[i]+1].mean() for i in range(len(indices))])
vol_ratio = bar_vol / np.maximum(window_vol, 1e-10)  # 当前量 / 过去60min平均量

# 信号时刻的成交量分桶
sig_mask = sig_any
print(f"\ntail={tail}, W={W}min, H={H}min")
print(f"总信号数: {sig_mask.sum()}")
print(f"\n{'量比区间':>16} | {'信号数':>7} | {'胜率':>7} | {'盈利?':>6} | {'说明':>30}")
print("-" * 85)

vol_bins = [(0, 0.3), (0.3, 0.5), (0.5, 0.8), (0.8, 1.0), (1.0, 1.3), (1.3, 1.8), (1.8, 2.5), (2.5, 5.0), (5.0, 999)]
for lo, hi in vol_bins:
    mask = sig_mask & (vol_ratio >= lo) & (vol_ratio < hi)
    n = mask.sum()
    if n < 5:
        continue
    wr = sig_correct[mask].mean() * 100
    pnl = wr/100 * PAYOUT - (1-wr/100) * 1.0
    label = f"[{lo:.1f}, {hi:.1f})"
    hi_label = "≤5.0+" if hi >= 999 else f"{hi:.1f}"
    
    if vol_ratio[mask].mean() < 0.5:
        note = "缩量 → 弱信号"
    elif vol_ratio[mask].mean() < 1.0:
        note = "正常量"
    elif vol_ratio[mask].mean() < 2.0:
        note = "放量 → 趋势确认?"
    else:
        note = "巨量 → 恐慌/贪婪"
    
    profit = "✓" if pnl > 0 else "✗"
    print(f"{label:>16} | {n:>7} | {wr:>6.1f}% | {profit} {pnl:+.3f} | {note}")

# ════════════════════════════════════════════════════════
print(f"\n{'='*100}")
print("第二部分：放量 vs 缩量 × 信号方向的交互效应")
print(f"{'='*100}")
print(f"\n核心问题：放量时的反转信号和缩量时的反转信号，哪个更准？")
print(f"\n{'':>16} | {'UP信号(p_up≤0.25)':>30} | {'DOWN信号(p_up≥0.75)':>30}")
print(f"{'量比':>16} | {'信号数':>6} {'UP胜率':>8} {'说明':>14} | {'信号数':>6} {'DN胜率':>8} {'说明':>14}")
print("-" * 105)

for lo, hi, label in [(0, 0.5, "缩量(<0.5)"), (0.5, 0.8, "偏低(0.5-0.8)"), 
                       (0.8, 1.2, "正常(0.8-1.2)"), (1.2, 2.0, "放量(1.2-2.0)"),
                       (2.0, 5.0, "巨量(2.0+)")]:
    vmask = (vol_ratio >= lo) & (vol_ratio < hi)
    
    up_mask = sig_up & vmask
    dn_mask = sig_dn & vmask
    n_up = up_mask.sum()
    n_dn = dn_mask.sum()
    
    up_wr = actual_up[up_mask].mean() * 100 if n_up > 0 else 0
    dn_wr = (1 - actual_up[dn_mask]).mean() * 100 if n_dn > 0 else 0
    
    up_note = "✓有效" if up_wr > 55.56 else "✗"
    dn_note = "✓有效" if dn_wr > 55.56 else "✗"
    
    print(f"{label:>16} | {n_up:>6} {up_wr:>7.1f}% {up_note:>14} | {n_dn:>6} {dn_wr:>7.1f}% {dn_note:>14}")

# ════════════════════════════════════════════════════════
print(f"\n{'='*100}")
print("第三部分：Taker 买卖比作为信号过滤器")
print(f"{'='*100}")

# 把 taker 5min 数据对齐到 1min
df["taker_ratio"] = np.nan
for _, row in taker.iterrows():
    mask = (df["open_time"] >= row["timestamp"]) & (df["open_time"] < row["timestamp"] + pd.Timedelta(minutes=5))
    df.loc[mask, "taker_ratio"] = row["taker_ratio"]

taker_aligned = df["taker_ratio"].values

# 只在有 taker 数据的区间分析
has_taker = ~np.isnan(taker_aligned[indices])
print(f"\n有Taker数据的信号: {has_taker.sum()}/{sig_mask.sum()}")
print(f"\n逻辑：买UP时 taker_ratio应该 < 1（卖方主导，过度恐慌→反转涨）")
print(f"      买DOWN时 taker_ratio应该 > 1（买方主导，过度贪婪→反转跌）")

taker_at_sig = taker_aligned[indices]

print(f"\n{'过滤模式':>20} | {'通过信号':>8} | {'过滤掉':>8} | {'胜率':>7} | {'vs无过滤':>10} | {'盈利?':>6}")
print("-" * 80)

# 无过滤基线
base_mask = sig_mask & has_taker
base_n = base_mask.sum()
base_wr = sig_correct[base_mask].mean() * 100

print(f"{'无过滤(基线)':>20} | {base_n:>8} | {'—':>8} | {base_wr:>6.1f}% | {'—':>10} | {'✓' if base_wr > 55.56 else '✗'}")

modes = [
    ("align严格 0.9/1.1", 0.90, 1.10),
    ("align 0.85/1.15", 0.85, 1.15),
    ("align宽松 0.8/1.2", 0.80, 1.20),
    ("not_counter 1.0/1.0", 1.0, 1.0),
]
for name, up_thr, dn_thr in modes:
    # UP 信号：taker_ratio <= up_thr（卖方主导）
    # DOWN 信号：taker_ratio >= dn_thr（买方主导）
    filter_mask = np.where(
        sig_up & has_taker,
        taker_at_sig <= up_thr,
        np.where(sig_dn & has_taker, taker_at_sig >= dn_thr, False)
    )
    n_pass = filter_mask.sum()
    n_blocked = base_n - n_pass
    if n_pass > 0:
        wr = sig_correct[filter_mask].mean() * 100
    else:
        wr = 0
    delta = wr - base_wr
    pnl = wr/100 * PAYOUT - (1-wr/100) * 1.0
    profit = "✓" if pnl > 0 else "✗"
    print(f"{name:>20} | {n_pass:>8} | {n_blocked:>8} | {wr:>6.1f}% | {delta:>+9.1f}% | {profit} ({pnl:+.3f})")

# ════════════════════════════════════════════════════════
print(f"\n{'='*100}")
print("第四部分：多空比(L/S Ratio)作为情绪指标")
print(f"{'='*100}")

df["ls_ratio"] = np.nan
for _, row in ls.iterrows():
    mask = (df["open_time"] >= row["timestamp"]) & (df["open_time"] < row["timestamp"] + pd.Timedelta(minutes=5))
    df.loc[mask, "ls_ratio"] = row["longShortRatio"]

ls_aligned = df["ls_ratio"].values
has_ls = ~np.isnan(ls_aligned[indices])

print(f"\n有L/S数据的信号: {has_ls.sum()}/{sig_mask.sum()}")
print(f"逻辑：LS_ratio < 0.5 = 空头过多 → 反转涨概率高 → 配合UP信号")
print(f"      LS_ratio > 0.5 = 多头过多 → 反转跌概率高 → 配合DOWN信号")

ls_at_sig = ls_aligned[indices]

print(f"\n{'过滤条件':>25} | {'通过信号':>8} | {'胜率':>7} | {'vs基线':>8} | {'盈利?':>6}")
print("-" * 70)

ls_base = sig_mask & has_ls
ls_base_n = ls_base.sum()
ls_base_wr = sig_correct[ls_base].mean() * 100 if ls_base_n > 0 else 0

print(f"{'无过滤基线':>25} | {ls_base_n:>8} | {ls_base_wr:>6.1f}% | {'—':>8} | {'✓' if ls_base_wr > 55.56 else '✗'}")

for name, up_ls_max, dn_ls_min in [
    ("LS<0.48(UP) / >0.52(DN)", 0.48, 0.52),
    ("LS<0.45(UP) / >0.55(DN)", 0.45, 0.55),
    ("LS<0.50(中性)", 0.50, 0.50),
]:
    fmask = np.where(
        sig_up & has_ls,
        ls_at_sig <= up_ls_max,
        np.where(sig_dn & has_ls, ls_at_sig >= dn_ls_min, False)
    )
    n = fmask.sum()
    wr = sig_correct[fmask].mean() * 100 if n > 0 else 0
    delta = wr - ls_base_wr
    pnl = wr/100 * PAYOUT - (1-wr/100) * 1.0
    profit = "✓" if pnl > 0 else "✗"
    print(f"{name:>25} | {n:>8} | {wr:>6.1f}% | {delta:>+7.1f}% | {profit} ({pnl:+.3f})")

# ════════════════════════════════════════════════════════
print(f"\n{'='*100}")
print("第五部分：资金费率(Funding Rate)作为市场情绪")
print(f"{'='*100}")

# 把 funding 8h 数据 forward fill 到 1min
df["funding"] = np.nan
for _, row in fund.iterrows():
    mask = df["open_time"] >= row["fundingTime"]
    df.loc[mask, "funding"] = row["fundingRate"]
df["funding"] = df["funding"].ffill()

fund_aligned = df["funding"].values
has_fund = ~np.isnan(fund_aligned[indices])
fund_at_sig = fund_aligned[indices]

print(f"\n有Funding数据的信号: {has_fund.sum()}/{sig_mask.sum()}")
print(f"逻辑：负费率 = 空头付多头 = 空头情绪强 → 反转涨 → 配合UP")
print(f"      正费率 = 多头付空头 = 多头情绪强 → 反转跌 → 配合DOWN")

print(f"\n{'过滤条件':>30} | {'通过信号':>8} | {'胜率':>7} | {'vs基线':>8} | {'盈利?':>6}")
print("-" * 75)

fund_base = sig_mask & has_fund
fund_base_n = fund_base.sum()
fund_base_wr = sig_correct[fund_base].mean() * 100 if fund_base_n > 0 else 0

print(f"{'无过滤基线':>30} | {fund_base_n:>8} | {fund_base_wr:>6.1f}% | {'—':>8} | {'✓' if fund_base_wr > 55.56 else '✗'}")

for name, up_cond, dn_cond_fn in [
    ("FR<0(UP) / FR>0(DN)", lambda x: x < 0, lambda x: x > 0),
    ("FR<-0.0001(UP) / >0.0001(DN)", lambda x: x < -0.0001, lambda x: x > 0.0001),
    ("FR<-0.00005(UP) / >0.00005(DN)", lambda x: x < -0.00005, lambda x: x > 0.00005),
]:
    fmask = np.where(
        sig_up & has_fund,
        up_cond(fund_at_sig),
        np.where(sig_dn & has_fund, dn_cond_fn(fund_at_sig), False)
    )
    n = fmask.sum()
    wr = sig_correct[fmask].mean() * 100 if n > 0 else 0
    delta = wr - fund_base_wr
    pnl = wr/100 * PAYOUT - (1-wr/100) * 1.0
    profit = "✓" if pnl > 0 else "✗"
    print(f"{name:>30} | {n:>8} | {wr:>6.1f}% | {delta:>+7.1f}% | {profit} ({pnl:+.3f})")

# ════════════════════════════════════════════════════════
print(f"\n{'='*100}")
print("第六部分：组合过滤 — 成交量 + Taker + Funding")
print(f"{'='*100}")
print(f"找到最佳多因子组合")

# 组合1：放量(>1.2) + taker align(0.9/1.1)
# 组合2：缩量(<0.8) + taker align(0.9/1.1)  
# 组合3：正常量(0.8-1.2) + taker align(0.9/1.1)
# 组合4：放量 + taker + funding align
# 组合5：仅极端放量(>2.0)

combos = [
    ("放量>1.2 + taker0.9/1.1", 
     lambda: (sig_mask & has_taker & (vol_ratio > 1.2) &
              np.where(sig_up, taker_at_sig <= 0.90, np.where(sig_dn, taker_at_sig >= 1.10, False)))),
    ("缩量<0.8 + taker0.9/1.1",
     lambda: (sig_mask & has_taker & (vol_ratio < 0.8) &
              np.where(sig_up, taker_at_sig <= 0.90, np.where(sig_dn, taker_at_sig >= 1.10, False)))),
    ("正常量0.8-1.2 + taker",
     lambda: (sig_mask & has_taker & (vol_ratio >= 0.8) & (vol_ratio <= 1.2) &
              np.where(sig_up, taker_at_sig <= 0.90, np.where(sig_dn, taker_at_sig >= 1.10, False)))),
    ("巨量>2.0 (无taker过滤)",
     lambda: sig_mask & (vol_ratio > 2.0)),
    ("放量>1.2 (无taker过滤)",
     lambda: sig_mask & (vol_ratio > 1.2)),
    ("放量 + taker + funding",
     lambda: (sig_mask & has_taker & has_fund & (vol_ratio > 1.0) &
              np.where(sig_up, (taker_at_sig <= 0.90) & (fund_at_sig < 0),
                       np.where(sig_dn, (taker_at_sig >= 1.10) & (fund_at_sig > 0), False)))),
    ("taker严格0.85/1.15 + funding",
     lambda: (sig_mask & has_taker & has_fund &
              np.where(sig_up, (taker_at_sig <= 0.85) & (fund_at_sig < 0),
                       np.where(sig_dn, (taker_at_sig >= 1.15) & (fund_at_sig > 0), False)))),
]

print(f"\n{'组合':>35} | {'信号数':>7} | {'日均':>6} | {'胜率':>7} | {'vs基线':>8} | {'PNL/笔':>7} | {'评价':>15}")
print("-" * 105)

for name, fn in combos:
    mask = fn()
    n = mask.sum()
    if n < 5:
        print(f"{name:>35} | {n:>7} | {'—':>6} | {'—':>7} | {'—':>8} | {'—':>7} | 信号不足")
        continue
    wr = sig_correct[mask].mean() * 100
    pnl_per = wr/100 * PAYOUT - (1-wr/100) * 1.0
    # 估算日均交易（在有数据的时段内）
    days_with_data = has_taker[mask].sum() / max(n, 1) * (has_taker.sum() / 288) if has_taker.any() else N/1440
    est_days = has_taker.sum() / 288 if has_taker.any() and name != combos[-1][0] else N / 1440
    if "巨量" in name or "放量>1.2 (无" in name:
        est_days = N / 1440
    daily = n / max(est_days, 1)
    
    if pnl_per > 0.08 and daily > 3:
        rating = "★★★ 优秀"
    elif pnl_per > 0.05:
        rating = "★★ 可用"
    elif pnl_per > 0:
        rating = "★ 勉强"
    else:
        rating = "✗ 不行"
    
    delta = wr - base_wr if "基线" not in name else 0
    print(f"{name:>35} | {n:>7} | {daily:>5.1f} | {wr:>6.1f}% | {delta:>+7.1f}% | {pnl_per:>+6.3f} | {rating}")

print(f"\n{'='*100}")
print("总结")
print(f"{'='*100}")
