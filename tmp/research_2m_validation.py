"""
2分钟线验证：不对称阈值 + 量比过滤
将5分钟最优配置迁移到2分钟线，验证是否同样有效
"""
import math, numpy as np, pandas as pd
from scipy.stats import norm

PAYOUT = 0.80
BE = 1 / (1 + PAYOUT)  # 55.56%

# ── 数据加载：1m → 2m 聚合 ──
df = pd.read_csv("e:/python-binance/data/btcusdt_1m.csv")
df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
for c in ["open","high","low","close","volume"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")
df = df.dropna(subset=["open_time","close","volume"]).sort_values("open_time").reset_index(drop=True)

# 聚合成2分钟K线
df["period"] = df["open_time"].dt.floor("2min")
agg = df.groupby("period").agg(
    close=("close","last"),
    volume=("volume","sum"),
    high=("high","max"),
    low=("low","min"),
    open_p=("open","first")
).reset_index().rename(columns={"period":"open_time"})
agg = agg.sort_values("open_time").reset_index(drop=True)

close = agg["close"].values.astype(float)
vol = agg["volume"].values.astype(float)
N = len(close)
DAYS = N / 720  # 2m bars: 720 per day
print(f"数据: {N} bars (2分钟线), {DAYS:.1f}天, PAYOUT={PAYOUT}, BE={BE:.2%}")

# 收益率
lr = np.log(close[1:] / close[:-1])
lr = np.where(np.isfinite(lr), lr, 0)
vol_lr = vol[:-1].copy()
N = len(lr)

# 累积和（向量化窗口统计）
cs_lr = np.cumsum(lr)
cs_lr2 = np.cumsum(lr * lr)
cs_vol = np.cumsum(vol_lr)

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

def vol_ratio(W, max_idx=None):
    """计算量比 = 当前bar成交量 / 过去W根bar平均成交量"""
    n = max_idx if max_idx is not None else N
    indices = np.arange(W, n)
    cs_v = cs_vol[indices] - cs_vol[indices - W]
    avg_vol = cs_v / W
    cur_vol = vol_lr[indices]
    vr = np.where(avg_vol > 0, cur_vol / avg_vol, 1.0)
    return indices, vr

def normal_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

ncdf = np.vectorize(normal_cdf)

def backtest_asym(tail_up, tail_dn, vol_thresh, W=30, H=5, cd=2, label=""):
    """
    不对称阈值回测
    W=30 (60分钟/2分钟), H=5 (10分钟/2分钟), cd=2 (4分钟冷却)
    tail_up: UP信号阈值 (p_up <= tail_up → UP)
    tail_dn: DOWN信号阈值 (p_up >= (1-tail_dn) → DOWN)
    vol_thresh: 最小量比 (0=不过滤)
    """
    poc_up = tail_up
    poc_dn = 1.0 - tail_dn
    indices, mu, sigma = window_stats(W, max_idx=N-H)
    idx_vol, vr = vol_ratio(W, max_idx=N-H)

    z = np.sqrt(H) * mu / np.maximum(sigma, 1e-10)
    p_up = ncdf(z)

    # 信号
    sig_up = p_up <= poc_up
    sig_dn = p_up >= poc_dn

    if vol_thresh > 0:
        mask_vol = vr >= vol_thresh
        sig_up = sig_up & mask_vol
        sig_dn = sig_dn & mask_vol

    signals = np.zeros(len(indices), dtype=int)  # 0=none, 1=UP, -1=DOWN
    signals[sig_up] = 1
    signals[sig_dn] = -1

    # cooldown 过滤
    filtered = signals.copy()
    last_sig_idx = -99999
    for i in range(len(filtered)):
        if filtered[i] != 0:
            if indices[i] - last_sig_idx < cd:
                filtered[i] = 0
            else:
                last_sig_idx = indices[i]

    # 回测
    actual = np.zeros(len(filtered), dtype=bool)  # win=UP wins
    # UP signal wins if close[i+H] > close[i]
    # DOWN signal wins if close[i+H] < close[i]
    sig_idx = np.where(filtered != 0)[0]
    if len(sig_idx) == 0:
        return None

    results = []
    pnl = 0.0
    wins = 0
    losses = 0
    max_pnl = 0
    max_dd = 0
    streak = 0
    max_streak = 0
    for si in sig_idx:
        bar_idx = indices[si]
        entry = close[bar_idx]
        exit_p = close[bar_idx + H]
        direction = filtered[si]
        if direction == 1:
            win = exit_p > entry
        else:
            win = exit_p < entry
        if win:
            pnl += PAYOUT
            wins += 1
            streak = 0
        else:
            pnl -= 1.0
            losses += 1
            streak += 1
            max_streak = max(max_streak, streak)
        max_pnl = max(max_pnl, pnl)
        max_dd = max(max_dd, max_pnl - pnl)

    total = wins + losses
    wr = wins / total if total > 0 else 0
    per_day = total / DAYS
    pnl_per_sig = pnl / total if total > 0 else 0
    ratio = pnl / max_dd if max_dd > 0 else 999

    stars = "★" * (3 if ratio > 8 else 2 if ratio > 4 else 1 if wr > BE else 0)
    print(f"  {label:45s} | {total:5d} | {per_day:5.1f} | {wr:6.1%} | {pnl:+7.1f} | {max_dd:6.1f} | {max_streak:3d} | {ratio:5.1f} | {stars}")
    return dict(total=total, per_day=per_day, wr=wr, pnl=pnl, dd=max_dd,
                streak=max_streak, ratio=ratio)

# ============================================================
print("="*110)
print("PART 1: 对称阈值基线 — 2分钟线 vs 5分钟线")
print("="*110)
print(f"\nW=30(60min), H=5(10min), cd=3(6min), 2分钟线")
print(f"\n{'配置':45s} | {'交易':>5s} | {'日均':>5s} | {'胜率':>6s} | {'PNL':>7s} | {'回撤':>6s} | {'连亏':>3s} | {'PNL/DD':>5s} | 评价")
print("-"*110)

for tail, vth in [(0.20, 0), (0.20, 1.0), (0.20, 1.2),
                   (0.25, 0), (0.25, 1.0), (0.25, 1.2), (0.25, 1.5),
                   (0.30, 0), (0.30, 1.2), (0.30, 1.5)]:
    vlabel = f"无过滤" if vth == 0 else f"vol≥{vth}"
    backtest_asym(tail, tail, vth, W=30, H=5, cd=3,
                  label=f"对称 tail={tail} {vlabel}")

# ============================================================
print(f"\n{'='*110}")
print("PART 2: 不对称阈值 — UP严格 / DOWN放宽")
print("="*110)
print(f"\n{'配置':45s} | {'交易':>5s} | {'日均':>5s} | {'胜率':>6s} | {'PNL':>7s} | {'回撤':>6s} | {'连亏':>3s} | {'PNL/DD':>5s} | 评价")
print("-"*110)

configs_2m = [
    # 5分钟最优配置的直接迁移
    (0.25, 0.30, 1.2, "UP0.25/DN0.30 vol≥1.2 (5m最优迁移)"),
    (0.25, 0.30, 1.5, "UP0.25/DN0.30 vol≥1.5"),
    (0.25, 0.30, 1.0, "UP0.25/DN0.30 vol≥1.0"),
    (0.20, 0.30, 1.2, "UP0.20/DN0.30 vol≥1.2"),
    (0.20, 0.30, 1.0, "UP0.20/DN0.30 vol≥1.0"),
    (0.25, 0.25, 1.2, "UP0.25/DN0.25 vol≥1.2"),
    (0.20, 0.25, 1.2, "UP0.20/DN0.25 vol≥1.2"),
    # 放宽DOWN阈值（p_up≥0.70→p_up≥0.75→p_up≥0.80）
    (0.25, 0.25, 1.5, "UP0.25/DN0.25 vol≥1.5"),
    (0.20, 0.25, 1.5, "UP0.20/DN0.25 vol≥1.5"),
    (0.25, 0.20, 1.2, "UP0.25/DN0.20 vol≥1.2 (DN更严格)"),
    # 高频方向
    (0.30, 0.30, 1.2, "UP0.30/DN0.30 vol≥1.2 (宽tail)"),
    (0.30, 0.35, 1.2, "UP0.30/DN0.35 vol≥1.2"),
    (0.30, 0.35, 1.5, "UP0.30/DN0.35 vol≥1.5"),
]

results_p2 = []
for tu, td, vt, label in configs_2m:
    r = backtest_asym(tu, td, vt, W=30, H=5, cd=3, label=label)
    if r:
        results_p2.append((label, r))

# ============================================================
print(f"\n{'='*110}")
print("PART 3: cooldown 对比 (cd=2 vs cd=3 vs cd=5)")
print("="*110)
# 2m线上: cd=2→4min, cd=3→6min, cd=5→10min, cd=8→16min
print(f"\n最优配置的cooldown扫描:")
print(f"\n{'cd':>4s} | {'实际间隔':>8s} | {'交易':>5s} | {'日均':>5s} | {'胜率':>6s} | {'PNL':>7s} | {'回撤':>6s} | {'连亏':>3s} | {'PNL/DD':>5s} | 评价")
print("-"*110)

best_base = None
# 找PART2中PNL/DD最高的
if results_p2:
    best_base = max(results_p2, key=lambda x: x[1]['ratio'])
    print(f"\n基准配置: {best_base[0]}\n")

for cd_val, cd_min in [(2, 4), (3, 6), (5, 10), (8, 16)]:
    # 用最优配置
    for tu, td, vt, label in [(0.25, 0.30, 1.2, "UP0.25/DN0.30 v1.2"),
                               (0.20, 0.30, 1.2, "UP0.20/DN0.30 v1.2"),
                               (0.25, 0.30, 1.5, "UP0.25/DN0.30 v1.5")]:
        r = backtest_asym(tu, td, vt, W=30, H=5, cd=cd_val,
                          label=f"cd={cd_val}({cd_min}min) {label}")
        if cd_val == cd_min // 2 + 1:  # 只打一次分隔
            pass

# ============================================================
print(f"\n{'='*110}")
print("PART 4: 窗口大小扫描 (W=20/30/45 对应 40/60/90分钟)")
print("="*110)
print(f"\n不对称0.25/0.30 + vol≥1.2:")
print(f"\n{'W':>4s} | {'分钟':>4s} | {'交易':>5s} | {'日均':>5s} | {'胜率':>6s} | {'PNL':>7s} | {'回撤':>6s} | {'连亏':>3s} | {'PNL/DD':>5s} | 评价")
print("-"*110)

for W_val, W_min in [(20, 40), (30, 60), (45, 90), (60, 120)]:
    for tu, td, vt in [(0.25, 0.30, 1.2), (0.25, 0.30, 1.5)]:
        r = backtest_asym(tu, td, vt, W=W_val, H=5, cd=3,
                          label=f"W={W_val}({W_min}min) UP{tu}/DN{td} v{vt}")

# ============================================================
print(f"\n{'='*110}")
print("PART 5: 最终对比 — 当前生产 vs 推荐配置")
print("="*110)
print(f"\n{'配置':55s} | {'交易':>5s} | {'日均':>5s} | {'胜率':>6s} | {'PNL':>7s} | {'回撤':>6s} | {'连亏':>3s} | {'PNL/DD':>5s} | 评价")
print("-"*110)

# 当前生产: 对称tail=0.20, 无量比, cd=15(30min/2min)
backtest_asym(0.20, 0.20, 0, W=30, H=5, cd=15, label="当前生产: tail=0.20 对称 无过滤 cd=15(30min)")
backtest_asym(0.20, 0.20, 0, W=30, H=5, cd=3, label="当前但cd=3(6min)")

# 推荐方案A: 不对称0.25/0.30 + vol≥1.2 + cd=3
backtest_asym(0.25, 0.30, 1.2, W=30, H=5, cd=3, label="方案A: UP0.25/DN0.30 v1.2 cd=3(6min)")
backtest_asym(0.25, 0.30, 1.2, W=30, H=5, cd=2, label="方案A2: UP0.25/DN0.30 v1.2 cd=2(4min)")

# 推荐方案B: 不对称0.20/0.30 + vol≥1.2 + cd=3
backtest_asym(0.20, 0.30, 1.2, W=30, H=5, cd=3, label="方案B: UP0.20/DN0.30 v1.2 cd=3(6min)")
backtest_asym(0.20, 0.30, 1.5, W=30, H=5, cd=3, label="方案B2: UP0.20/DN0.30 v1.5 cd=3(6min)")

# 高频方案
backtest_asym(0.30, 0.35, 1.2, W=30, H=5, cd=3, label="高频: UP0.30/DN0.35 v1.2 cd=3(6min)")
backtest_asym(0.30, 0.35, 1.5, W=30, H=5, cd=3, label="高频2: UP0.30/DN0.35 v1.5 cd=3(6min)")

print(f"\n{'='*110}")
print("研究完成")
print("="*110)
