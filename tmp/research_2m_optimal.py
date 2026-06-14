"""
2分钟线完整优化：不对称tail + 量比过滤 + cooldown + 窗口扫描
基于已验证正确的 research_1m_vs_2m_diagnosis.py 的 run_test 逻辑

Bug检查清单：
  [x] cs_lr/cs_lr2/cs_vol 前面加 [0]
  [x] PNL: Win→+PAYOUT(0.8), Loss→-1.0
  [x] entry=close[bi], exit=close[bi+H], bi是lr数组索引
  [x] vol_lr=vol[:-1] 与 lr 对齐
  [x] 窗口用 lr[i-W..i-1], 不含当前bar的未来信息
  [x] max_idx = N - H, 不越界
  [x] cooldown 用 indices[i] 的真实bar位置比较
"""
import math, numpy as np, pandas as pd, json, itertools

PAYOUT = 0.80
BE = 1.0 / (1.0 + PAYOUT) * 100  # 55.56%

# ── 加载并聚合2分钟线 ──
df = pd.read_csv("e:/python-binance/data/btcusdt_1m.csv")
df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
for c in ["open", "high", "low", "close", "volume"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")
df = df.dropna(subset=["open_time", "close", "volume"]).sort_values("open_time").reset_index(drop=True)

df["period"] = df["open_time"].dt.floor("2min")
agg = df.groupby("period").agg(
    close=("close", "last"),
    volume=("volume", "sum")
).reset_index().sort_values("period")
close = agg["close"].values.astype(float)
vol = agg["volume"].values.astype(float)
DAYS = len(close) / 720.0

# ── 预计算 lr 和 vol_lr（全局，只算一次）──
lr = np.log(close[1:] / close[:-1])
lr = np.where(np.isfinite(lr), lr, 0.0)
vol_lr = vol[:-1].copy()
N = len(lr)

print(f"2分钟线优化 | {N} bars ({DAYS:.1f}天) | PAYOUT={PAYOUT} BE={BE:.2f}%")
print("=" * 130)

# ── 累积和（前面加 [0]，防止索引偏移）──
cs_lr  = np.concatenate([[0.0], np.cumsum(lr)])
cs_lr2 = np.concatenate([[0.0], np.cumsum(lr ** 2)])
cs_vol = np.concatenate([[0.0], np.cumsum(vol_lr)])

def normal_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
ncdf = np.vectorize(normal_cdf)


def backtest(W, H, cd, tail_up, tail_dn, vol_thresh=0.0):
    """
    不对称 POC 反转策略回测
    UP信号: p_up <= tail_up   (认为跌太多→做多)
    DN信号: p_up >= 1-tail_dn (认为涨太多→做空)
    vol_thresh: 当前bar量比 >= vol_thresh 才触发
    """
    max_idx = N - H
    if W >= max_idx:
        return None
    indices = np.arange(W, max_idx)

    # ── 窗口统计: lr[i-W .. i-1] ──
    s  = cs_lr[indices] - cs_lr[indices - W]
    s2 = cs_lr2[indices] - cs_lr2[indices - W]
    mu = s / W
    var = np.maximum((s2 / W) - mu ** 2, 0.0) * W / (W - 1)
    sigma = np.sqrt(var)

    # ── 量比 ──
    cs_v = cs_vol[indices] - cs_vol[indices - W]
    avg_vol = cs_v / W
    vr = np.where(avg_vol > 0, vol_lr[indices] / np.maximum(avg_vol, 1e-10), 1.0)

    # ── p_up ──
    z = np.sqrt(H) * mu / np.maximum(sigma, 1e-10)
    p_up = ncdf(z)

    # ── 信号 ──
    sig_up = p_up <= tail_up
    sig_dn = p_up >= (1.0 - tail_dn)

    if vol_thresh > 0:
        sig_up &= vr >= vol_thresh
        sig_dn &= vr >= vol_thresh

    signals = np.zeros(len(indices), dtype=np.int8)
    signals[sig_up] = 1
    signals[sig_dn] = -1

    # ── cooldown ──
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
        return None

    # ── 逐笔结算 ──
    wins = 0
    losses = 0
    pnl = 0.0
    max_pnl = 0.0
    max_dd = 0.0
    streak = 0
    max_streak = 0
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
    wr = wins / total * 100.0
    daily = total / DAYS
    pnl_dd = pnl / max_dd if max_dd > 0 else 0.0
    return {
        "trades": total, "wins": wins, "losses": losses,
        "wr": wr, "pnl": pnl, "max_dd": max_dd, "pnl_dd": pnl_dd,
        "daily": daily, "max_streak": max_streak,
    }


def fmt(r, label=""):
    if r is None:
        print(f"  {label:50s} | 无信号")
        return
    print(f"  {label:50s} | {r['trades']:>5d}笔 {r['daily']:>5.1f}/天 | "
          f"WR {r['wr']:5.1f}% | PNL {r['pnl']:+8.1f} | DD {r['max_dd']:6.1f} | "
          f"P/DD {r['pnl_dd']:5.1f} | 连亏{r['max_streak']}")


# ============================================================
# Part 1: 当前生产配置基线
# ============================================================
print("\n【Part 1】当前生产配置基线（对称 tail）")
print("-" * 130)

# 生产: W=60min→30根2m, H=10min→5根2m, cd=30min→15根2m, tail=0.20
r = backtest(W=30, H=5, cd=15, tail_up=0.20, tail_dn=0.20, vol_thresh=0)
fmt(r, "生产基线 tail=0.20 cd=30min(15根)")

r = backtest(W=30, H=5, cd=15, tail_up=0.20, tail_dn=0.20, vol_thresh=1.0)
fmt(r, "生产基线 + vol≥1.0")

r = backtest(W=30, H=5, cd=15, tail_up=0.20, tail_dn=0.20, vol_thresh=1.2)
fmt(r, "生产基线 + vol≥1.2")

# ============================================================
# Part 2: cooldown 扫描
# ============================================================
print("\n【Part 2】Cooldown 扫描（tail=0.20, vol≥1.2）")
print("-" * 130)
for cd in [2, 3, 5, 8, 10, 15]:
    r = backtest(W=30, H=5, cd=cd, tail_up=0.20, tail_dn=0.20, vol_thresh=1.2)
    fmt(r, f"cd={cd}根({cd*2}min)")

# ============================================================
# Part 3: 不对称 tail 扫描
# ============================================================
print("\n【Part 3】不对称 tail 扫描（W=30, H=5, cd=5, vol≥1.2）")
print("-" * 130)
print(f"{'tail_up':>8s} {'tail_dn':>8s} | {'笔数':>6s} {'/天':>5s} | {'WR':>6s} | {'PNL':>8s} | {'DD':>6s} | {'P/DD':>5s}")
print("-" * 130)

results_p3 = []
for tu, td in [(0.20, 0.20), (0.25, 0.20), (0.25, 0.25), (0.25, 0.30),
                (0.30, 0.25), (0.30, 0.30), (0.20, 0.25), (0.20, 0.30),
                (0.15, 0.20), (0.15, 0.25), (0.30, 0.35), (0.35, 0.30)]:
    r = backtest(W=30, H=5, cd=5, tail_up=tu, tail_dn=td, vol_thresh=1.2)
    if r:
        results_p3.append((tu, td, r))
        print(f"{tu:>8.2f} {td:>8.2f} | {r['trades']:>6d} {r['daily']:>5.1f} | "
              f"{r['wr']:5.1f}% | {r['pnl']:+8.1f} | {r['max_dd']:6.1f} | {r['pnl_dd']:5.1f}")

# ============================================================
# Part 4: 量比过滤扫描（不对称最优 + 不同量比）
# ============================================================
print("\n【Part 4】量比过滤扫描")
print("-" * 130)
# 用 Part3 里 PNL 最高的 top2 组合
results_p3.sort(key=lambda x: x[2]["pnl"], reverse=True)
best_combos = results_p3[:3]
for tu, td, _ in best_combos:
    print(f"\n  tail_up={tu:.2f} tail_dn={td:.2f}:")
    for vt in [0.0, 0.8, 1.0, 1.2, 1.5, 1.8, 2.0]:
        r = backtest(W=30, H=5, cd=5, tail_up=tu, tail_dn=td, vol_thresh=vt)
        if r:
            print(f"    vol≥{vt:<4.1f} | {r['trades']:>5d}笔 {r['daily']:>5.1f}/天 | "
                  f"WR {r['wr']:5.1f}% | PNL {r['pnl']:+8.1f} | DD {r['max_dd']:6.1f} | P/DD {r['pnl_dd']:5.1f}")

# ============================================================
# Part 5: 窗口 W 扫描
# ============================================================
print("\n【Part 5】窗口 W 扫描（最优不对称 + vol≥1.2, cd=5）")
print("-" * 130)
best_tu, best_td = best_combos[0][0], best_combos[0][1]
for W in [15, 20, 25, 30, 40, 50, 60]:
    r = backtest(W=W, H=5, cd=5, tail_up=best_tu, tail_dn=best_td, vol_thresh=1.2)
    if r:
        print(f"  W={W:>3d}({W*2}min) | {r['trades']:>5d}笔 {r['daily']:>5.1f}/天 | "
              f"WR {r['wr']:5.1f}% | PNL {r['pnl']:+8.1f} | DD {r['max_dd']:6.1f} | P/DD {r['pnl_dd']:5.1f}")

# ============================================================
# Part 6: 全局最优参数组合排名
# ============================================================
print("\n【Part 6】全局排名 Top 15")
print("-" * 130)
all_results = []
for W in [20, 25, 30, 40]:
    for tu in [0.15, 0.20, 0.25, 0.30]:
        for td in [0.20, 0.25, 0.30]:
            for vt in [0.0, 1.0, 1.2, 1.5]:
                for cd in [3, 5, 8]:
                    r = backtest(W=W, H=5, cd=cd, tail_up=tu, tail_dn=td, vol_thresh=vt)
                    if r and r["trades"] >= 50:
                        all_results.append({
                            "W": W, "cd": cd, "tu": tu, "td": td, "vt": vt, **r
                        })

# 按 PNL/DD 排序
all_results.sort(key=lambda x: x["pnl_dd"], reverse=True)
print(f"{'#':>3s} {'W':>4s} {'cd':>3s} {'tu':>5s} {'td':>5s} {'vol':>5s} | "
      f"{'笔':>5s} {'/天':>5s} | {'WR':>6s} | {'PNL':>8s} | {'DD':>6s} | {'P/DD':>5s} | {'连亏':>4s}")
print("-" * 130)
for i, r in enumerate(all_results[:15]):
    print(f"{i+1:>3d} {r['W']:>4d} {r['cd']:>3d} {r['tu']:>5.2f} {r['td']:>5.2f} {r['vt']:>5.1f} | "
          f"{r['trades']:>5d} {r['daily']:>5.1f} | {r['wr']:5.1f}% | {r['pnl']:+8.1f} | "
          f"{r['max_dd']:6.1f} | {r['pnl_dd']:5.1f} | {r['max_streak']:>4d}")

# 保存结果
with open("e:/python-binance/tmp/research_2m_optimal_result.json", "w") as f:
    json.dump(all_results[:30], f, indent=2)
print(f"\nTop 30 已保存到 tmp/research_2m_optimal_result.json")
