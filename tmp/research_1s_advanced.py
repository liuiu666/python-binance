"""
秒级数据深度分析 — Bootstrap/到期对比/时段过滤/价格regime
======================================================
1. Bootstrap 95%置信区间 — WR的统计可信度
2. 不同到期时间对比 — 5min/10min/15min/20min
3. 时段过滤效果 — UTC 15-16h过滤后表现
4. 价格regime分析 — 信号在趋势/震荡市的表现
5. 敏感性分析 — 参数微调对WR的影响
"""
import math, numpy as np, pandas as pd, json
from collections import defaultdict

PAYOUT = 0.80
BE = 1.0 / (1.0 + PAYOUT) * 100
H_SETTLE = 600
CD = 600

df = pd.read_csv("e:/python-binance/tmp/server_1s_trades.csv")
df["ts"] = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601")
df = df.sort_values("ts").reset_index(drop=True)
close = df["close"].values.astype(float)
ts_arr = df["ts"].values
N = len(close)
print(f"数据: {N}行 ({N/60:.0f}min = {N/60/60:.1f}h)")

lr = np.log(close[1:] / close[:-1])
lr = np.where(np.isfinite(lr), lr, 0.0)
cs_lr  = np.concatenate([[0.0], np.cumsum(lr)])
cs_lr2 = np.concatenate([[0.0], np.cumsum(lr ** 2)])
ncdf = np.vectorize(lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0))))
max_eval = N - 1800  # 留够20min

trend_300 = np.zeros(N)
for i in range(300, N):
    trend_300[i] = (close[i] / close[i - 300] - 1) * 10000

def get_signals(W, H_p_up, tail, cd=CD):
    indices = np.arange(W, max_eval)
    s  = cs_lr[indices] - cs_lr[indices - W]
    s2 = cs_lr2[indices] - cs_lr2[indices - W]
    mu = s / W
    var = np.maximum((s2 / W) - mu**2, 0.0) * W / (W - 1)
    sigma = np.sqrt(var)
    z = np.sqrt(H_p_up) * mu / np.maximum(sigma, 1e-10)
    p_up = ncdf(z)

    all_sigs = []
    for i in range(len(indices)):
        d = 0
        if p_up[i] <= tail:
            d = 1
        elif p_up[i] >= 1 - tail:
            d = -1
        if d != 0:
            all_sigs.append((indices[i], d, p_up[i], z[i]))

    filtered = []
    last_bar = -99999
    for gidx, d, p, zv in all_sigs:
        if gidx - last_bar >= cd:
            last_bar = gidx
            filtered.append((gidx, d, p, zv))
    return filtered

def settle_at(gidx, d, h_settle):
    s_idx = gidx + h_settle
    if s_idx >= N:
        return None
    went_up = close[s_idx] > close[gidx]
    return (went_up and d == 1) or (not went_up and d == -1)

# ============================================================
# Part 1: Bootstrap 95% CI
# ============================================================
print(f"\n{'='*100}")
print(f"Part 1: Bootstrap 95%置信区间 — WR统计可信度")
print(f"{'='*100}")

np.random.seed(42)
N_BOOT = 10000

for W, H, t in [(600, 300, 0.20), (600, 600, 0.10), (120, 900, 0.05)]:
    sigs = get_signals(W, H, t)
    wins_arr = np.array([1 if settle_at(g, d, H_SETTLE) else 0 for g, d, _, _ in sigs
                         if settle_at(g, d, H_SETTLE) is not None])
    n = len(wins_arr)
    wr = wins_arr.mean() * 100
    pnl = wins_arr.sum() * PAYOUT - (n - wins_arr.sum()) * 1.0

    # Bootstrap
    boot_wrs = np.zeros(N_BOOT)
    for b in range(N_BOOT):
        sample = np.random.choice(wins_arr, size=n, replace=True)
        boot_wrs[b] = sample.mean() * 100

    ci_lo = np.percentile(boot_wrs, 2.5)
    ci_hi = np.percentile(boot_wrs, 97.5)
    prob_above_be = (boot_wrs > BE).mean() * 100

    # Wilson interval (analytical)
    z_crit = 1.96
    p_hat = wins_arr.mean()
    n_hat = n
    denom = 1 + z_crit**2 / n_hat
    center = (p_hat + z_crit**2 / (2 * n_hat)) / denom
    margin = z_crit * math.sqrt(p_hat * (1 - p_hat) / n_hat + z_crit**2 / (4 * n_hat**2)) / denom
    wilson_lo = (center - margin) * 100
    wilson_hi = (center + margin) * 100

    print(f"\n  W={W} H={H} t={t:.2f}: {n}信号 WR={wr:.1f}% PNL={pnl:+.1f}")
    print(f"    Bootstrap 95% CI: [{ci_lo:.1f}%, {ci_hi:.1f}%]")
    print(f"    Wilson 95% CI:    [{wilson_lo:.1f}%, {wilson_hi:.1f}%]")
    print(f"    P(WR > {BE:.1f}%): {prob_above_be:.1f}%")
    print(f"    P(WR > 70%):      {(boot_wrs > 70).mean()*100:.1f}%")
    print(f"    P(WR > 65%):      {(boot_wrs > 65).mean()*100:.1f}%")

# ============================================================
# Part 2: 不同到期时间对比
# ============================================================
print(f"\n{'='*100}")
print(f"Part 2: 不同到期时间对比 — 5min/10min/15min/20min")
print(f"{'='*100}")

EXPIRIES = [180, 300, 600, 900, 1200]  # 3/5/10/15/20 min

print(f"\n  {'参数':>20} | {'到期':>6} | {'N':>4} {'WR':>6} {'PNL':>7} | {'做多WR':>7} {'做空WR':>7} | {'BE差距':>7}")
print("  " + "-" * 80)

for W, H, t in [(600, 300, 0.20), (120, 900, 0.05), (120, 120, 0.25)]:
    for exp in EXPIRIES:
        sigs = get_signals(W, H, t)
        results = []
        for gidx, d, p, z in sigs:
            r = settle_at(gidx, d, exp)
            if r is not None:
                results.append((gidx, d, r))

        n = len(results)
        if n < 5:
            continue
        wins = sum(1 for _, _, w in results if w)
        wr = wins / n * 100
        pnl = wins * PAYOUT - (n - wins) * 1.0
        ups = [r for r in results if r[1] == 1]
        dns = [r for r in results if r[1] == -1]
        up_wr = sum(1 for r in ups if r[2]) / len(ups) * 100 if ups else 0
        dn_wr = sum(1 for r in dns if r[2]) / len(dns) * 100 if dns else 0
        be_gap = wr - BE
        bar = "█" * int(wr / 5)
        exp_label = f"{exp//60}min"
        marker = " ★" if exp == 600 else ""
        print(f"  W={W} H={H:>4} t={t:.2f} | {exp_label:>6} | {n:>4} {wr:>5.1f}% {pnl:>+6.1f} | {up_wr:>6.1f}% {dn_wr:>6.1f}% | {be_gap:>+6.1f}%{marker} {bar}")
    print()

# ============================================================
# Part 3: 时段过滤效果
# ============================================================
print(f"\n{'='*100}")
print(f"Part 3: 时段过滤效果")
print(f"{'='*100}")

# 收集去重信号
COMBOS = [(600, 300, 0.20), (600, 600, 0.10), (120, 900, 0.05)]
all_trades = []
seen = set()
for W, H, t in COMBOS:
    sigs = get_signals(W, H, t)
    for gidx, d, p, z in sigs:
        if gidx in seen:
            continue
        seen.add(gidx)
        r = settle_at(gidx, d, H_SETTLE)
        if r is not None:
            hour = pd.Timestamp(ts_arr[gidx]).hour
            all_trades.append({"gidx": gidx, "dir": d, "win": r, "hour": hour})

# 全时段
n_all = len(all_trades)
wr_all = sum(t["win"] for t in all_trades) / n_all * 100
pnl_all = sum(t["win"] for t in all_trades) * PAYOUT - (n_all - sum(t["win"] for t in all_trades)) * 1.0

# 过滤掉UTC 18:00 (差时段)
filtered_trades = [t for t in all_trades if t["hour"] != 18]
n_f = len(filtered_trades)
wr_f = sum(t["win"] for t in filtered_trades) / n_f * 100
pnl_f = sum(t["win"] for t in filtered_trades) * PAYOUT - (n_f - sum(t["win"] for t in filtered_trades)) * 1.0

# 只在UTC 15-16 (优时段)
peak_trades = [t for t in all_trades if t["hour"] in [15, 16]]
n_p = len(peak_trades)
if n_p > 0:
    wr_p = sum(t["win"] for t in peak_trades) / n_p * 100
    pnl_p = sum(t["win"] for t in peak_trades) * PAYOUT - (n_p - sum(t["win"] for t in peak_trades)) * 1.0

print(f"\n  全时段:       {n_all:>3}信号 WR={wr_all:.1f}% PNL={pnl_all:+.1f}")
print(f"  排除UTC18:    {n_f:>3}信号 WR={wr_f:.1f}% PNL={pnl_f:+.1f}")
print(f"  仅UTC15-16:   {n_p:>3}信号 WR={wr_p:.1f}% PNL={pnl_p:+.1f}")

# 注意警告
print(f"\n  ⚠️ 注意: 时段效果仅基于5小时数据，可能是偶然而非规律")
print(f"     UTC 15-16h = 北京时间23-24h = 美国开盘前")

# ============================================================
# Part 4: 价格regime分析 — 趋势 vs 震荡
# ============================================================
print(f"\n{'='*100}")
print(f"Part 4: 价格regime — 趋势 vs 震荡市场")
print(f"{'='*100}")

for W, H, t in [(600, 300, 0.20), (120, 900, 0.05)]:
    sigs = get_signals(W, H, t)
    results = []
    for gidx, d, p, z in sigs:
        r = settle_at(gidx, d, H_SETTLE)
        if r is not None:
            tr = trend_300[gidx]
            results.append({"gidx": gidx, "dir": d, "win": r, "trend": tr})

    # 按300s趋势强度分组
    print(f"\n  W={W} H={H} t={t:.2f} — 按|trend_300s|分组:")
    print(f"  {'趋势强度':>12} | {'N':>4} {'WR':>6} {'PNL':>7} | {'做多N':>5} {'做空N':>5}")
    print("  " + "-" * 55)

    for lo, hi, label in [(0, 3, "0-3bps"), (3, 5, "3-5bps"), (5, 10, "5-10bps"), (10, 999, ">10bps")]:
        seg = [r for r in results if lo <= abs(r["trend"]) < hi]
        if len(seg) < 2:
            continue
        wins = sum(1 for r in seg if r["win"])
        wr = wins / len(seg) * 100
        pnl = wins * PAYOUT - (len(seg) - wins) * 1.0
        ups = sum(1 for r in seg if r["dir"] == 1)
        dns = len(seg) - ups
        bar = "█" * int(wr / 5)
        print(f"  {label:>12} | {len(seg):>4} {wr:>5.1f}% {pnl:>+6.1f} | {ups:>5} {dns:>5} {bar}")

    # 趋势方向 vs 信号方向
    print(f"\n  信号方向 vs 趋势方向:")
    for sig_dir, trend_cond, label in [
        (1, 1, "做多+上涨趋势(顺势)"),
        (1, -1, "做多+下跌趋势(逆势)"),
        (-1, 1, "做空+上涨趋势(逆势)"),
        (-1, -1, "做空+下跌趋势(顺势)"),
    ]:
        seg = [r for r in results if r["dir"] == sig_dir and
               (r["trend"] * trend_cond > 2)]
        if len(seg) < 2:
            continue
        wins = sum(1 for r in seg if r["win"])
        wr = wins / len(seg) * 100
        pnl = wins * PAYOUT - (len(seg) - wins) * 1.0
        print(f"    {label:>20}: {len(seg):>3}信号 WR={wr:.1f}% PNL={pnl:+.1f}")

# ============================================================
# Part 5: 参数敏感性 — 微调对WR的影响
# ============================================================
print(f"\n{'='*100}")
print(f"Part 5: 参数敏感性 — W=600附近微调")
print(f"{'='*100}")

print(f"\n  固定H=300, tail=0.20, 扫描W:")
print(f"  {'W':>5} | {'N':>4} {'WR':>6} {'PNL':>7} | {'信号变化':>8}")
print("  " + "-" * 40)
base_sigs = None
for W in [480, 540, 600, 660, 720, 900, 1200]:
    sigs = get_signals(W, 300, 0.20)
    results = [(g, d, settle_at(g, d, H_SETTLE)) for g, d, p, z in sigs]
    results = [(g, d, w) for g, d, w in results if w is not None]
    n = len(results)
    if n < 3:
        continue
    wins = sum(1 for _, _, w in results if w)
    wr = wins / n * 100
    pnl = wins * PAYOUT - (n - wins) * 1.0
    gidxs = set(g for g, _, _ in results)
    if base_sigs is None:
        base_sigs = gidxs
        overlap = "基准"
    else:
        inter = len(gidxs & base_sigs)
        overlap = f"{inter}/{len(gidxs)}重叠"
    marker = " ★" if W == 600 else ""
    print(f"  {W:>5} | {n:>4} {wr:>5.1f}% {pnl:>+6.1f} | {overlap:>8}{marker}")

print(f"\n  固定W=600, tail=0.20, 扫描H:")
print(f"  {'H':>5} | {'N':>4} {'WR':>6} {'PNL':>7} | {'等效z_thr':>9}")
print("  " + "-" * 45)
for H in [60, 120, 180, 300, 600, 900, 1200]:
    sigs = get_signals(600, H, 0.20)
    results = [(g, d, settle_at(g, d, H_SETTLE)) for g, d, p, z in sigs]
    results = [(g, d, w) for g, d, w in results if w is not None]
    n = len(results)
    if n < 3:
        continue
    wins = sum(1 for _, _, w in results if w)
    wr = wins / n * 100
    pnl = wins * PAYOUT - (n - wins) * 1.0
    # z threshold ≈ ncdf_inv(0.20) * sqrt(H/600)... actually z = sqrt(H)*mu/sigma
    # tail=0.20 means p_up<=0.20 or >=0.80, z threshold = ncdf_inv(0.20) = -0.8416
    # so |z| >= 0.8416, which means sqrt(H)*|mu|/sigma >= 0.8416
    # The effective z-threshold in terms of sqrt(H)*mu/sigma is constant = 0.8416
    z_eff = 0.8416  # constant for tail=0.20
    print(f"  {H:>5} | {n:>4} {wr:>5.1f}% {pnl:>+6.1f} | z_eff={z_eff:.3f}")

# ============================================================
# Part 6: 总结 — 最优策略画像
# ============================================================
print(f"\n{'='*100}")
print(f"Part 6: 最优策略完整画像")
print(f"{'='*100}")

# 收集最优参数的完整统计
W, H, t = 600, 300, 0.20
sigs = get_signals(W, H, t)
trade_log = []
for gidx, d, p, z in sigs:
    r = settle_at(gidx, d, H_SETTLE)
    if r is not None:
        hour = pd.Timestamp(ts_arr[gidx]).hour
        tr = trend_300[gidx]
        entry_price = close[gidx]
        settle_price = close[gidx + H_SETTLE]
        move_bps = (settle_price / entry_price - 1) * 10000
        trade_log.append({
            "gidx": gidx, "dir": d, "win": r, "hour": hour,
            "trend": tr, "z": z, "p_up": p,
            "entry": entry_price, "settle": settle_price,
            "move_bps": move_bps,
            "time": str(pd.Timestamp(ts_arr[gidx])),
        })

n = len(trade_log)
wins = sum(1 for t in trade_log if t["win"])
wr = wins / n * 100
pnl = wins * PAYOUT - (n - wins) * 1.0

# 逐笔明细
print(f"\n  逐笔交易明细 (W={W} H={H} t={t}):")
print(f"  {'#':>3} {'时间':>20} {'方向':>4} {'入场':>10} {'结算':>10} {'偏移bps':>8} {'趋势':>7} {'z':>6} {'结果':>4}")
print("  " + "-" * 90)
for i, tr in enumerate(trade_log):
    direction = "多" if tr["dir"] == 1 else "空"
    result = "✓赢" if tr["win"] else "✗亏"
    pnl_single = PAYOUT if tr["win"] else -1.0
    print(f"  {i+1:>3} {tr['time'][:19]:>20} {direction:>4} {tr['entry']:>10.1f} {tr['settle']:>10.1f} {tr['move_bps']:>+7.1f} {tr['trend']:>+6.1f} {tr['z']:>+5.2f} {result:>4} ({pnl_single:+.2f})")

cum_pnl = 0
cum_curve = []
for tr in trade_log:
    cum_pnl += PAYOUT if tr["win"] else -1.0
    cum_curve.append(cum_pnl)

print(f"\n  === 策略摘要 ===")
print(f"  参数: W={W}s H_p_up={H}s tail={t}")
print(f"  信号数: {n} ({n/(N/3600):.1f}信号/小时)")
print(f"  胜率: {wr:.1f}% (BE={BE:.1f}%, 边际={wr-BE:+.1f}%)")
print(f"  PNL: {pnl:+.1f} (EV/笔={pnl/n:+.3f})")
print(f"  最大回撤: {max(cum_curve[i] - c for i, c in enumerate(cum_curve) for j in range(i+1) if True if cum_curve[i] == max(cum_curve[:i+1])):.1f}" if cum_curve else "")
print(f"  资金曲线终值: {cum_pnl:+.1f}")

# 保存完整报告
report = {
    "data": {"rows": N, "hours": N/3600},
    "strategy": {"W": W, "H_p_up": H, "tail": t, "CD": CD, "H_SETTLE": H_SETTLE},
    "performance": {"N": n, "WR": wr, "PNL": pnl, "BE": BE, "EV_per_trade": pnl/n},
    "trades": [{k: v for k, v in t.items() if k != "gidx"} for t in trade_log],
}
with open("e:/python-binance/tmp/research_1s_advanced.json", "w") as f:
    json.dump(report, f, indent=2, default=str)
print(f"\n  ✓ 完整报告已保存至 research_1s_advanced.json")
