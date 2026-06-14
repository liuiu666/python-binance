"""
 apples-to-apples对比：生产配置到底盈不盈利？
 
 生产真实配置（prod_config.json）:
   - source_minutes=2  → 2分钟K线
   - norm_window=60    → 60根2分钟bar = 120分钟回看
   - horizon=10        → 10根2分钟bar = 20分钟horizon
   - tail_pct=0.20
   - min_gap_minutes=30 → 30分钟cooldown = 15根2分钟bar

 我之前scan_1m_global.py用的:
   - 1分钟K线
   - W=30  → 30分钟回看  ← 只有生产的1/4！
   - H=10  → 10分钟horizon ← 只有生产的1/2！
   - tail=0.20
   - cd=30

 这就是矛盾根源：完全不同的参数！
"""
import math, numpy as np, pandas as pd

PAYOUT = 0.80
BE = 1.0 / (1.0 + PAYOUT) * 100

# ============================================================
# 加载1分钟数据
# ============================================================
df = pd.read_csv("e:/python-binance/data/btcusdt_1m.csv")
df["open_time"] = pd.to_datetime(df["open_time"], utc=True, format="mixed")
for c in ["open", "high", "low", "close", "volume"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")
df = df.dropna(subset=["open_time", "close"]).sort_values("open_time").reset_index(drop=True)
DAYS_1M = len(df) / 1440.0

def normal_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
ncdf = np.vectorize(normal_cdf)

def run_backtest(close_arr, vol_arr, W, H, cd, tail, label):
    """通用回测函数"""
    lr = np.log(close_arr[1:] / close_arr[:-1])
    lr = np.where(np.isfinite(lr), lr, 0.0)
    N = len(lr)
    
    cs_lr  = np.concatenate([[0.0], np.cumsum(lr)])
    cs_lr2 = np.concatenate([[0.0], np.cumsum(lr ** 2)])
    
    max_idx = N - H
    indices = np.arange(W, max_idx)
    
    s  = cs_lr[indices] - cs_lr[indices - W]
    s2 = cs_lr2[indices] - cs_lr2[indices - W]
    mu = s / W
    var = np.maximum((s2 / W) - mu ** 2, 0.0) * W / (W - 1)
    sigma = np.sqrt(var)
    z = np.sqrt(H) * mu / np.maximum(sigma, 1e-10)
    p_up = ncdf(z)
    
    sig = np.zeros(len(indices), dtype=np.int8)
    sig[p_up <= tail] = 1
    sig[p_up >= 1 - tail] = -1
    
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
        print(f"  {label}: 0信号")
        return
    
    wins = up_w = up_n = dn_w = dn_n = 0
    for si in sig_pos:
        gidx = indices[si]
        d = filtered[si]
        win = (close_arr[gidx + H] > close_arr[gidx]) if d == 1 else (close_arr[gidx + H] < close_arr[gidx])
        if d == 1: up_n += 1; 
        else: dn_n += 1
        if win:
            wins += 1
            if d == 1: up_w += 1
            else: dn_w += 1
    
    losses = n_sig - wins
    wr = wins / n_sig * 100
    pnl = wins * PAYOUT - losses * 1.0
    daily = n_sig / (N / (1440.0 / bar_min))
    
    print(f"  {label}")
    print(f"    数据: {N:,}根{bar_min}分钟bar ({N/(1440.0/bar_min):.1f}天)")
    print(f"    参数: W={W}({W*bar_min}min) H={H}({H*bar_min}min) tail={tail} cd={cd}({cd*bar_min}min)")
    print(f"    信号: {n_sig} ({daily:.1f}笔/天), {wins}W/{losses}L, WR={wr:.1f}%, PNL={pnl:+.1f}")
    print(f"    边际: WR-{BE:.2f}={wr-BE:+.2f}%")
    if up_n: print(f"    UP: {up_n}笔 WR={up_w/up_n*100:.1f}%")
    if dn_n: print(f"    DN: {dn_n}笔 WR={dn_w/dn_n*100:.1f}%")
    print()
    return wr, pnl, n_sig

# ============================================================
print("=" * 100)
print("【对比1】生产真实配置：2分钟线, W=60(120min), H=10(20min), tail=0.20, cd=15(30min)")
print("=" * 100)

# 聚合成2分钟线
df2 = df.copy()
df2["period"] = df2["open_time"].dt.floor("2min")
agg2 = df2.groupby("period").agg(
    close=("close", "last"),
    volume=("volume", "sum")
).reset_index().sort_values("period")
close_2m = agg2["close"].values.astype(float)
vol_2m = agg2["volume"].values.astype(float)

bar_min = 2
run_backtest(close_2m, vol_2m, W=60, H=10, cd=15, tail=0.20, 
             label="生产配置 (2分钟线)")

# ============================================================
print("=" * 100)
print("【对比2】我之前scan用的错误配置：1分钟线, W=30(30min), H=10(10min), tail=0.20, cd=30")
print("=" * 100)

close_1m = df["close"].values.astype(float)
vol_1m = df["volume"].values.astype(float)

bar_min = 1
run_backtest(close_1m, vol_1m, W=30, H=10, cd=30, tail=0.20,
             label="scan错误配置 (1分钟线)")

# ============================================================
print("=" * 100)
print("【对比3】1分钟线上等价于生产的配置：W=120(120min), H=20(20min), tail=0.20, cd=30")
print("=" * 100)

bar_min = 1
run_backtest(close_1m, vol_1m, W=120, H=20, cd=30, tail=0.20,
             label="等价配置 (1分钟线)")

# ============================================================
print("=" * 100)
print("【对比4】2分钟线上多tail扫描")
print("=" * 100)

bar_min = 2
for tail in [0.15, 0.20, 0.25, 0.27, 0.30]:
    run_backtest(close_2m, vol_2m, W=60, H=10, cd=15, tail=tail,
                 label=f"2分钟线 W=60 H=10 cd=15 tail={tail}")

# ============================================================
print("=" * 100)
print("【对比5】2分钟线上多W扫描（tail=0.20固定）")
print("=" * 100)

for W_2m in [30, 45, 60, 90, 120]:
    cd_2m = max(W_2m // 4, 15)  # cd ≈ W/4, 最小15(30min)
    run_backtest(close_2m, vol_2m, W=W_2m, H=10, cd=cd_2m, tail=0.20,
                 label=f"2分钟线 W={W_2m}({W_2m*2}min) H=10 cd={cd_2m}({cd_2m*2}min)")

# ============================================================
print("=" * 100)
print("【对比6】关键验证：PAYOUT=0.85时生产配置还盈利吗？")
print("=" * 100)

PAYOUT_OLD = PAYOUT
PAYOUT = 0.85
BE = 1.0 / (1.0 + PAYOUT) * 100
print(f"  PAYOUT={PAYOUT}, BE={BE:.2f}%")
bar_min = 2
run_backtest(close_2m, vol_2m, W=60, H=10, cd=15, tail=0.20,
             label=f"生产配置 PAYOUT={PAYOUT}")
PAYOUT = PAYOUT_OLD
BE = 1.0 / (1.0 + PAYOUT) * 100

# ============================================================
print("=" * 100)
print("【结论】")
print("=" * 100)
