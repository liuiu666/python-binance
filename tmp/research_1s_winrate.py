"""
秒级数据胜率机理研究

核心问题：68%胜率到底是什么？是稳定的统计优势，还是少数极端事件拉起来的？
不做参数优化，只做现象拆解。

数据：13217行秒级数据，3h42min（2026-06-13 13:33~17:16 UTC）
方案：
  Part 1: 把秒级数据聚合成2分钟K线，运行POC策略，找到信号触发点
  Part 2: 对每个信号，用秒级数据逐秒追踪从entry到exit的完整价格路径
  Part 3: 赢的交易 vs 输的交易，价格轨迹有什么本质区别？
  Part 4: 胜率的时间稳定性 — 滚动窗口看胜率是否一致
"""
import math, numpy as np, pandas as pd

PAYOUT = 0.80
BE = 1.0 / (1.0 + PAYOUT) * 100  # 55.56%

# ── 加载秒级数据 ──
raw = pd.read_csv("e:/python-binance/tmp/server_1s_trades.csv")
raw["ts"] = pd.to_datetime(raw["timestamp"], utc=True)
for c in ["open", "high", "low", "close", "volume",
          "taker_buy_volume", "taker_sell_volume", "trades"]:
    raw[c] = pd.to_numeric(raw[c], errors="coerce")
raw = raw.dropna(subset=["ts", "close"]).sort_values("ts").reset_index(drop=True)

print(f"秒级数据: {len(raw)} 行")
print(f"时间范围: {raw['ts'].iloc[0]} → {raw['ts'].iloc[-1]}")
print(f"持续时间: {(raw['ts'].iloc[-1] - raw['ts'].iloc[0]).total_seconds()/60:.1f} 分钟")
print(f"价格区间: {raw['close'].min():.1f} ~ {raw['close'].max():.1f}")
# 秒级全局变量（Part 2/3/5 都要用）
sec_ts = raw["ts"].values
sec_close = raw["close"].values.astype(float)
sec_vol = raw["volume"].values.astype(float)
sec_buy = raw["taker_buy_volume"].values.astype(float)
sec_sell = raw["taker_sell_volume"].values.astype(float)

print(f"价格波动: {(raw['close'].max()-raw['close'].min())/raw['close'].mean()*100:.2f}%")
print("=" * 100)

# ============================================================
# Part 1: 聚合成2分钟K线 + POC策略
# ============================================================
print("\n【Part 1】2分钟K线 POC 策略信号")
print("-" * 100)

raw["period"] = raw["ts"].dt.floor("2min")
bars = raw.groupby("period").agg(
    close=("close", "last"),
    volume=("volume", "sum"),
    taker_buy=("taker_buy_volume", "sum"),
    taker_sell=("taker_sell_volume", "sum"),
    trades=("trades", "sum"),
).reset_index().sort_values("period")

close = bars["close"].values.astype(float)
vol = bars["volume"].values.astype(float)
N_bars = len(close)
DAYS = N_bars / 720.0
print(f"2分钟K线: {N_bars} 根 ({DAYS*24*60:.0f}分钟)")

# POC参数（当前生产配置 + vol过滤）
W = 30   # 60分钟窗口
H = 5    # 10分钟预测
tail = 0.20

lr = np.log(close[1:] / close[:-1])
lr = np.where(np.isfinite(lr), lr, 0)
vol_lr = vol[:-1].copy()
N = len(lr)

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

cs_v = cs_vol[indices] - cs_vol[indices - W]
avg_vol = cs_v / W
vr = np.where(avg_vol > 0, vol_lr[indices] / np.maximum(avg_vol, 1e-10), 1.0)

z = np.sqrt(H) * mu / np.maximum(sigma, 1e-10)

def normal_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
ncdf = np.vectorize(normal_cdf)
p_up = ncdf(z)

# 信号生成
poc_thresh = 1.0 - tail
sig_up_mask = (p_up <= tail) & (vr >= 1.2)
sig_dn_mask = (p_up >= poc_thresh) & (vr >= 1.2)

signals = np.zeros(len(indices), dtype=int)
signals[sig_up_mask] = 1
signals[sig_dn_mask] = -1

# cooldown = 5根(10min)
cd = 5
filtered = signals.copy()
last_bar = -99999
for i in range(len(filtered)):
    if filtered[i] != 0:
        if indices[i] - last_bar < cd:
            filtered[i] = 0
        else:
            last_bar = indices[i]

sig_positions = np.where(filtered != 0)[0]
print(f"信号数: {len(sig_positions)}")

if len(sig_positions) == 0:
    # 放宽条件再试
    print("  vol≥1.2无信号，尝试无过滤...")
    sig_up_mask = p_up <= tail
    sig_dn_mask = p_up >= poc_thresh
    signals = np.zeros(len(indices), dtype=int)
    signals[sig_up_mask] = 1
    signals[sig_dn_mask] = -1
    filtered = signals.copy()
    last_bar = -99999
    for i in range(len(filtered)):
        if filtered[i] != 0:
            if indices[i] - last_bar < cd:
                filtered[i] = 0
            else:
                last_bar = indices[i]
    sig_positions = np.where(filtered != 0)[0]
    print(f"  无过滤信号数: {len(sig_positions)}")

if len(sig_positions) == 0:
    print("  再试 tail=0.25 + 无过滤 + cd=3...")
    sig_up_mask = p_up <= 0.25
    sig_dn_mask = p_up >= 0.75
    signals = np.zeros(len(indices), dtype=int)
    signals[sig_up_mask] = 1
    signals[sig_dn_mask] = -1
    filtered = signals.copy()
    last_bar = -99999
    for i in range(len(filtered)):
        if filtered[i] != 0:
            if indices[i] - last_bar < 3:
                filtered[i] = 0
            else:
                last_bar = indices[i]
    sig_positions = np.where(filtered != 0)[0]
    print(f"  tail=0.25 cd=3 信号数: {len(sig_positions)}")

# 记录信号详情
trade_records = []
for si in sig_positions:
    bi = indices[si]
    entry = close[bi]
    exit_p = close[bi + H]
    direction = filtered[si]
    if direction == 1:
        win = exit_p > entry
    else:
        win = exit_p < entry
    
    trade_records.append({
        "bar_idx": bi,
        "direction": "UP" if direction == 1 else "DOWN",
        "entry": entry,
        "exit": exit_p,
        "p_up": p_up[si],
        "z_score": z[si],
        "vol_ratio": vr[si],
        "win": win,
        "pnl": PAYOUT if win else -1.0,
        "bar_time": bars["period"].iloc[bi],
        "exit_time": bars["period"].iloc[bi + H],
        "mu": mu[si],
        "sigma": sigma[si],
    })

if trade_records:
    wins = sum(1 for t in trade_records if t["win"])
    losses = len(trade_records) - wins
    total = len(trade_records)
    wr = wins / total * 100 if total > 0 else 0
    print(f"\n结果: {total}笔, {wins}赢/{losses}输, WR={wr:.1f}%, BE={BE:.1f}%")
    
    for i, t in enumerate(trade_records):
        ret = (t["exit"] - t["entry"]) / t["entry"] * 100
        print(f"  #{i+1} {t['bar_time'].strftime('%H:%M')} {t['direction']:4s} "
              f"entry={t['entry']:.1f} exit={t['exit']:.1f} "
              f"p_up={t['p_up']:.3f} z={t['z_score']:.3f} vr={t['vol_ratio']:.2f} "
              f"{'WIN' if t['win'] else 'LOSS'} ({ret:+.3f}%)")
else:
    print("\n无信号触发。诊断 p_up 分布:")
    print(f"  p_up range: [{p_up.min():.4f}, {p_up.max():.4f}]")
    print(f"  p_up mean: {p_up.mean():.4f}, median: {np.median(p_up):.4f}")
    print(f"  z range: [{z.min():.4f}, {z.max():.4f}]")
    print(f"  z mean: {z.mean():.4f}")
    print(f"  mu range: [{mu.min():.8f}, {mu.max():.8f}]")
    print(f"  sigma range: [{sigma.min():.8f}, {sigma.max():.8f}]")
    print(f"  vr range: [{vr.min():.2f}, {vr.max():.2f}]")
    # 看有多少接近触发
    for t in [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]:
        n_up = np.sum(p_up <= t)
        n_dn = np.sum(p_up >= 1-t)
        print(f"  tail={t:.2f}: UP信号={n_up}, DN信号={n_dn} (共{len(p_up)}个评估点)")

# ============================================================
# Part 2: 逐秒追踪信号后的价格路径
# ============================================================
print(f"\n{'='*100}")
print("【Part 2】信号后价格逐秒路径分析")
print("-" * 100)

if not trade_records:
    print("无信号可分析")
else:
    # sec_ts / sec_close 已在全局定义
    
    HORIZON_SEC = 600  # 10分钟 = 600秒
    
    print(f"\n每笔信号在entry后600秒(10分钟)内的价格变化轨迹:")
    print(f"{'#':>3} {'方向':>4} {'结果':>4} | ", end="")
    for sec in [0, 10, 30, 60, 120, 180, 300, 600]:
        print(f"{sec:>4d}s", end="  ")
    print()
    print("-" * 100)
    
    all_paths = []
    for i, t in enumerate(trade_records):
        entry_time = t["bar_time"]
        # 找到entry对应的秒级时间索引
        entry_ts = pd.Timestamp(entry_time)
        # entry_time是2分钟K线的开始，实际entry是这根K线结束时的close
        # 所以实际entry时间 = bar开始 + 2分钟
        actual_entry_ts = entry_ts + pd.Timedelta(minutes=2)
        
        # 找最近的秒级数据点
        time_diff = np.abs((sec_ts - np.datetime64(actual_entry_ts)).astype('timedelta64[s]').astype(float))
        entry_sec_idx = np.argmin(time_diff)
        
        # 追踪后续600秒
        path = []
        for sec in range(0, HORIZON_SEC + 1):
            target_idx = entry_sec_idx + sec
            if target_idx < len(sec_close):
                path.append(sec_close[target_idx])
            else:
                path.append(np.nan)
        
        all_paths.append({
            "idx": i, "direction": t["direction"], "win": t["win"],
            "entry_price": t["entry"], "path": np.array(path)
        })
        
        # 打印关键时间点
        entry_price = t["entry"]
        print(f"{i+1:>3} {t['direction']:>4} {'WIN' if t['win'] else 'LOSS':>4} | ", end="")
        for sec in [0, 10, 30, 60, 120, 180, 300, 600]:
            idx_sec = min(sec, len(path)-1)
            if not np.isnan(path[idx_sec]):
                chg = (path[idx_sec] - entry_price) / entry_price * 10000  # bps
                print(f"{chg:+5.1f}bp", end="  ")
            else:
                print(f"{'N/A':>7s}", end="  ")
        print()

# ============================================================
# Part 3: 赢 vs 输 的价格轨迹对比
# ============================================================
print(f"\n{'='*100}")
print("【Part 3】赢的交易 vs 输的交易：价格轨迹差异")
print("-" * 100)

all_paths = all_paths if trade_records else []
if all_paths:
    win_paths = [p for p in all_paths if p["win"]]
    loss_paths = [p for p in all_paths if not p["win"]]
    
    print(f"\n赢的交易: {len(win_paths)}笔, 输的交易: {len(loss_paths)}笔")
    
    # 平均轨迹（按方向归一化：UP=保持，DOWN=翻转）
    def normalized_deviation(path, direction, entry_price):
        """返回相对entry的偏离(bps)，方向归一化后正值=有利方向"""
        dev = (path - entry_price) / entry_price * 10000  # bps
        if direction == "DOWN":
            dev = -dev  # 翻转：价格下跌对DOWN信号是正方向
        return dev
    
    print(f"\n{'秒':>5} | {'赢的(平均偏离)':>14} | {'输的(平均偏离)':>14} | {'差异':>10}")
    print("-" * 60)
    
    for sec in [1, 5, 10, 15, 30, 45, 60, 90, 120, 180, 240, 300, 400, 500, 600]:
        win_devs = []
        loss_devs = []
        for p in win_paths:
            if sec < len(p["path"]) and not np.isnan(p["path"][sec]):
                win_devs.append(normalized_deviation(p["path"][:sec+1], p["direction"], p["entry_price"])[sec])
        for p in loss_paths:
            if sec < len(p["path"]) and not np.isnan(p["path"][sec]):
                loss_devs.append(normalized_deviation(p["path"][:sec+1], p["direction"], p["entry_price"])[sec])
        
        w_avg = np.mean(win_devs) if win_devs else 0
        l_avg = np.mean(loss_devs) if loss_devs else 0
        diff = w_avg - l_avg
        print(f"{sec:>5d} | {w_avg:>+10.1f} bps | {l_avg:>+10.1f} bps | {diff:>+8.1f}")

# ============================================================
# Part 4: 胜率时间稳定性
# ============================================================
print(f"\n{'='*100}")
print("【Part 4】胜率稳定性分析（在2分钟K线全集上）")
print("-" * 100)

# 用全部2分钟K线数据，不限于有信号的，看POC p_up的分布
print(f"\np_up 分布:")
print(f"  min={p_up.min():.4f}, max={p_up.max():.4f}")
print(f"  mean={p_up.mean():.4f}, median={np.median(p_up):.4f}")
print(f"  std={np.std(p_up):.4f}")

# 分桶看实际胜率 vs p_up预测
bins = np.arange(0, 1.05, 0.1)
actual_up = np.zeros(len(bins)-1)
counts = np.zeros(len(bins)-1, dtype=int)
for i in range(len(indices)):
    bi = indices[i]
    actual = close[bi+H] > close[bi]  # 实际是否上涨
    for j in range(len(bins)-1):
        if bins[j] <= p_up[i] < bins[j+1]:
            counts[j] += 1
            if actual:
                actual_up[j] += 1
            break

print(f"\n{'p_up区间':>12} | {'样本数':>6} | {'实际上涨比例':>12} | {'差异(p_up-实际)':>16}")
print("-" * 65)
for j in range(len(bins)-1):
    if counts[j] > 0:
        actual_pct = actual_up[j] / counts[j] * 100
        pred_pct = (bins[j] + bins[j+1]) / 2 * 100
        diff = pred_pct - actual_pct
        print(f"  {bins[j]:.1f}-{bins[j+1]:.1f} | {counts[j]:>6d} | {actual_pct:>10.1f}% | {diff:>+12.1f}%")
    else:
        print(f"  {bins[j]:.1f}-{bins[j+1]:.11f} | {counts[j]:>6d} | {'N/A':>12} | {'N/A':>16}")

# ============================================================
# Part 5: 微观价格结构分析
# ============================================================
print(f"\n{'='*100}")
print("【Part 5】秒级微观价格结构")
print("-" * 100)

# 每秒收益率的分布特征
sec_lr = np.log(sec_close[1:] / sec_close[:-1])
sec_lr = np.where(np.isfinite(sec_lr), sec_lr, 0)

print(f"\n每秒对数收益率分布:")
print(f"  mean: {np.mean(sec_lr)*1e6:+.2f} μbp (1μbp=0.000001%)")
print(f"  std:  {np.std(sec_lr)*1e6:.2f} μbp")
print(f"  min:  {np.min(sec_lr)*1e6:+.2f} μbp")
print(f"  max:  {np.max(sec_lr)*1e6:+.2f} μbp")
print(f"  skew: {pd.Series(sec_lr).skew():.3f}")
print(f"  kurt: {pd.Series(sec_lr).kurtosis():.3f}")

# 正态性检验：用秒级收益率的前4阶矩
print(f"\n正态分布拟合质量:")
print(f"  偏度={pd.Series(sec_lr).skew():.3f} (正态=0)")
print(f"  超峰度={pd.Series(sec_lr).kurtosis():.3f} (正态=0)")
print(f"  → 偏度>0表示右偏(上涨概率>下跌概率)，超峰度>0表示厚尾")

# 自相关
print(f"\n秒级收益率自相关:")
for lag in [1, 2, 5, 10, 30, 60, 120, 300]:
    if lag < len(sec_lr):
        ac = np.corrcoef(sec_lr[:-lag], sec_lr[lag:])[0, 1]
        print(f"  lag={lag:>3d}s: {ac:+.4f}")

# 10分钟窗口的收益均值与标准差
print(f"\n10分钟(600秒)累积收益的统计:")
ret_600 = np.array([np.sum(sec_lr[i:i+600]) for i in range(0, len(sec_lr)-600, 60)])
print(f"  样本数: {len(ret_600)}")
print(f"  mean: {np.mean(ret_600)*1e4:+.2f} bps")
print(f"  std:  {np.std(ret_600)*1e4:.2f} bps")
print(f"  P(>0) = {np.mean(ret_600 > 0)*100:.1f}%")
print(f"  → 如果mean≈0且std合理，正态假设成立")

print(f"\n{'='*100}")
print("分析完成")
