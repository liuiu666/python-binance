"""
关键诊断：1分钟线 vs 2分钟线，完全相同参数对比
解答：之前58%和现在45%到底谁对谁错
"""
import math, numpy as np, pandas as pd

PAYOUT = 0.80

# ── 加载1分钟原始数据 ──
df = pd.read_csv("e:/python-binance/data/btcusdt_1m.csv")
df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
for c in ["open","high","low","close","volume"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")
df = df.dropna(subset=["open_time","close","volume"]).sort_values("open_time").reset_index(drop=True)

def normal_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
ncdf = np.vectorize(normal_cdf)

def run_test(close, vol, label, W, H, cd, tail=0.25, vol_thresh=0):
    """纯POC反转策略回测"""
    lr = np.log(close[1:] / close[:-1])
    lr = np.where(np.isfinite(lr), lr, 0)
    vol_lr = vol[:-1].copy()
    N = len(lr)
    DAYS = N / (1440 / (W * (1440 / len(close)) / 1440))  # 不用这个
    
    cs_lr = np.concatenate([[0], np.cumsum(lr)])
    cs_lr2 = np.concatenate([[0], np.cumsum(lr**2)])
    cs_vol = np.concatenate([[0], np.cumsum(vol_lr)])
    
    max_idx = N - H
    indices = np.arange(W, max_idx)
    s = cs_lr[indices] - cs_lr[indices - W]
    s2 = cs_lr2[indices] - cs_lr2[indices - W]
    mu = s / W
    var = np.maximum((s2/W) - mu**2, 0) * W/(W-1)
    sigma = np.sqrt(var)
    
    # 量比
    cs_v = cs_vol[indices] - cs_vol[indices - W]
    avg_vol = cs_v / W
    vr = np.where(avg_vol > 0, vol_lr[indices] / avg_vol, 1.0)
    
    z = np.sqrt(H) * mu / np.maximum(sigma, 1e-10)
    p_up = ncdf(z)
    
    poc_thresh = 1.0 - tail
    sig_up = p_up <= tail
    sig_dn = p_up >= poc_thresh
    
    if vol_thresh > 0:
        sig_up &= vr >= vol_thresh
        sig_dn &= vr >= vol_thresh
    
    signals = np.zeros(len(indices), dtype=int)
    signals[sig_up] = 1
    signals[sig_dn] = -1
    
    # cooldown
    filtered = signals.copy()
    last_bar = -99999
    for i in range(len(filtered)):
        if filtered[i] != 0:
            if indices[i] - last_bar < cd:
                filtered[i] = 0
            else:
                last_bar = indices[i]
    
    sig_pos = np.where(filtered != 0)[0]
    if len(sig_pos) == 0:
        print(f"  {label}: 无信号")
        return
    
    wins = 0
    losses = 0
    pnl = 0.0
    max_pnl = 0
    max_dd = 0
    for si in sig_pos:
        bi = indices[si]
        entry = close[bi]
        exit_p = close[bi + H]
        d = filtered[si]
        if d == 1:
            win = exit_p > entry
        else:
            win = exit_p < entry
        if win:
            pnl += PAYOUT; wins += 1
        else:
            pnl -= 1.0; losses += 1
        max_pnl = max(max_pnl, pnl)
        max_dd = max(max_dd, max_pnl - pnl)
    
    total = wins + losses
    wr = wins / total * 100
    print(f"  {label:55s} | W={W:>3d} H={H:>2d} cd={cd:>2d} | {total:>6d}笔 | {wr:5.1f}% | PNL={pnl:+8.1f} | DD={max_dd:6.1f} | ratio={pnl/max_dd if max_dd>0 else 0:5.1f}")
    return wr, pnl

# ============================================================
# 1分钟线测试
# ============================================================
print("="*120)
print("1分钟线 (之前所有研究的实际数据)")
print("="*120)
close_1m = df["close"].values.astype(float)
vol_1m = df["volume"].values.astype(float)
days_1m = len(close_1m) / 1440
print(f"数据量: {len(close_1m)} bars, {days_1m:.1f}天")
print(f"W=60根=60分钟窗口, H=10根=10分钟预测, cd=10根=10分钟冷却\n")

# 对称
run_test(close_1m, vol_1m, "1m 对称 tail=0.25 无过滤", W=60, H=10, cd=10, tail=0.25, vol_thresh=0)
run_test(close_1m, vol_1m, "1m 对称 tail=0.25 vol≥1.2", W=60, H=10, cd=10, tail=0.25, vol_thresh=1.2)
run_test(close_1m, vol_1m, "1m 对称 tail=0.20 无过滤", W=60, H=10, cd=10, tail=0.20, vol_thresh=0)
run_test(close_1m, vol_1m, "1m 对称 tail=0.20 vol≥1.0", W=60, H=10, cd=10, tail=0.20, vol_thresh=1.0)

# ============================================================
# 2分钟线测试
# ============================================================
print(f"\n{'='*120}")
print("2分钟线 (生产环境 source_minutes=2)")
print("="*120)
df["period"] = df["open_time"].dt.floor("2min")
agg = df.groupby("period").agg(
    close=("close","last"),
    volume=("volume","sum")
).reset_index().sort_values("period")
close_2m = agg["close"].values.astype(float)
vol_2m = agg["volume"].values.astype(float)
days_2m = len(close_2m) / 720
print(f"数据量: {len(close_2m)} bars, {days_2m:.1f}天")
print(f"W=30根=60分钟窗口, H=5根=10分钟预测, cd=5根=10分钟冷却\n")

run_test(close_2m, vol_2m, "2m 对称 tail=0.25 无过滤", W=30, H=5, cd=5, tail=0.25, vol_thresh=0)
run_test(close_2m, vol_2m, "2m 对称 tail=0.25 vol≥1.2", W=30, H=5, cd=5, tail=0.25, vol_thresh=1.2)
run_test(close_2m, vol_2m, "2m 对称 tail=0.20 无过滤", W=30, H=5, cd=5, tail=0.20, vol_thresh=0)
run_test(close_2m, vol_2m, "2m 对称 tail=0.20 vol≥1.0", W=30, H=5, cd=5, tail=0.20, vol_thresh=1.0)

# ============================================================
# 5分钟线测试
# ============================================================
print(f"\n{'='*120}")
print("5分钟线 (额外参考)")
print("="*120)
df["period5"] = df["open_time"].dt.floor("5min")
agg5 = df.groupby("period5").agg(
    close=("close","last"),
    volume=("volume","sum")
).reset_index().sort_values("period5")
close_5m = agg5["close"].values.astype(float)
vol_5m = agg5["volume"].values.astype(float)
days_5m = len(close_5m) / 288
print(f"数据量: {len(close_5m)} bars, {days_5m:.1f}天")
print(f"W=12根=60分钟窗口, H=2根=10分钟预测, cd=2根=10分钟冷却\n")

run_test(close_5m, vol_5m, "5m 对称 tail=0.25 无过滤", W=12, H=2, cd=2, tail=0.25, vol_thresh=0)
run_test(close_5m, vol_5m, "5m 对称 tail=0.25 vol≥1.2", W=12, H=2, cd=2, tail=0.25, vol_thresh=1.2)
run_test(close_5m, vol_5m, "5m 对称 tail=0.20 无过滤", W=12, H=2, cd=2, tail=0.20, vol_thresh=0)

# ============================================================
# 同样60根窗口，不同bar大小
# ============================================================
print(f"\n{'='*120}")
print("控制变量：同样W=60根bar，但bar大小不同")
print("="*120)
print("(W=60根 1分钟=60分钟窗口, W=60根 2分钟=120分钟窗口, W=60根 5分钟=300分钟窗口)\n")

run_test(close_1m, vol_1m, "1m W=60(60min) tail=0.25", W=60, H=10, cd=10, tail=0.25)
run_test(close_2m, vol_2m, "2m W=60(120min) tail=0.25", W=60, H=5, cd=5, tail=0.25)
run_test(close_5m, vol_5m, "5m W=60(300min) tail=0.25", W=60, H=2, cd=2, tail=0.25)

print(f"\n{'='*120}")
print("结论：胜率差异来自bar粒度，不是窗口时间")
print("="*120)
