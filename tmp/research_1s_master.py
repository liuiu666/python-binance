"""
10分钟二元期权秒级策略 — 综合优化研究
=====================================
目标: 找到最优策略配置 (W, H_p_up, tail, trend_filter)

Phase 1: 新数据基线验证
Phase 2: 多因子网格搜索
Phase 3: Out-of-Sample验证
"""
import math, json, time, os
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from itertools import product

PAYOUT = 0.80
BE = 1.0 / (1.0 + PAYOUT) * 100
H_SETTLE = 600
CD = 600

# ============================================================
# 数据加载
# ============================================================
DATA_FILE = "e:/python-binance/tmp/server_1s_trades.csv"
df = pd.read_csv(DATA_FILE)
df["ts"] = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601")
df = df.sort_values("ts").reset_index(drop=True)
close = df["close"].values.astype(float)
volume = df["volume"].values.astype(float)
N = len(close)
print(f"数据: {N}行, {N/60:.0f}分钟 ({N/3600:.1f}h)")
print(f"时间: {df['ts'].iloc[0]} → {df['ts'].iloc[-1]}")

# ============================================================
# 预计算
# ============================================================
lr = np.log(close[1:] / close[:-1])
lr = np.where(np.isfinite(lr), lr, 0.0)
cs_lr  = np.concatenate([[0.0], np.cumsum(lr)])
cs_lr2 = np.concatenate([[0.0], np.cumsum(lr ** 2)])
ncdf = np.vectorize(lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0))))
max_eval = N - H_SETTLE

# 趋势 (多用窗口)
TREND_WINDOWS = [60, 120, 300, 600]
trend_ret = {}
for tw in TREND_WINDOWS:
    ret = np.zeros(N)
    for i in range(tw, N):
        ret[i] = (close[i] / close[i - tw] - 1) * 10000  # bps
    trend_ret[tw] = ret

# 波动率
VOLA_WINDOWS = [60, 120, 300]
vola = {}
for w in VOLA_WINDOWS:
    v_arr = np.zeros(N)
    for t in range(w, N):
        seg = lr[t - w:t]
        v_arr[t] = np.std(seg) * math.sqrt(60) * 1e4
    vola[w] = v_arr

def get_signals(W, H_p_up, tail, cd=CD):
    indices = np.arange(W, max_eval)
    s  = cs_lr[indices] - cs_lr[indices - W]
    s2 = cs_lr2[indices] - cs_lr2[indices - W]
    mu = s / W
    var = np.maximum((s2 / W) - mu**2, 0.0) * W / (W - 1)
    sigma = np.sqrt(var)
    z = np.sqrt(H_p_up) * mu / np.maximum(sigma, 1e-10)
    p_up = ncdf(z)
    sig = np.zeros(len(indices), dtype=np.int8)
    sig[p_up <= tail] = 1
    sig[p_up >= 1-tail] = -1
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
    return [(indices[i], filtered[i], p_up[i]) for i in sig_pos]

def settle(gidx, d):
    s_idx = gidx + H_SETTLE
    if s_idx >= N:
        return None
    went_up = close[s_idx] > close[gidx]
    win = (went_up and d == 1) or (not went_up and d == -1)
    dev = abs(close[s_idx] - close[gidx]) / close[gidx] * 10000
    return win, dev

def eval_combo(W, H_p_up, tail, trend_tw=0, trend_thr=0.0, vola_w=0, vola_thr=0.0):
    """评估单个参数组合，可选趋势/波动率过滤"""
    sigs = get_signals(W, H_p_up, tail)
    results = []
    for gidx, d, p in sigs:
        r = settle(gidx, d)
        if r is None:
            continue
        win, dev = r
        # 趋势过滤
        if trend_tw > 0 and abs(trend_ret[trend_tw][gidx]) < trend_thr:
            continue
        # 波动率过滤
        if vola_w > 0 and vola[vola_w][gidx] < vola_thr:
            continue
        results.append({"gidx": gidx, "dir": d, "p_up": p, "win": win, "dev": dev,
                         "trend": trend_ret[300][gidx], "vola120": vola[120][gidx]})
    return results

def summarize(results, label=""):
    n = len(results)
    if n == 0:
        return {"label": label, "n": 0, "wr": 0, "pnl": 0}
    wins = sum(1 for r in results if r["win"])
    wr = wins / n * 100
    pnl = wins * PAYOUT - (n - wins) * 1.0
    pnl_per = pnl / n
    return {"label": label, "n": n, "wr": wr, "pnl": pnl, "pnl_per": pnl_per,
            "wins": wins, "losses": n - wins}

# ============================================================
# PHASE 1: 基线验证
# ============================================================
print(f"\n{'='*100}")
print(f"PHASE 1: 基线验证 (新数据 N={N})")
print(f"{'='*100}")

COMBOS = [
    (120, 900, 0.05),
    (300, 60, 0.25),
    (300, 120, 0.15),
    (600, 300, 0.10),
    (300, 300, 0.20),
]

print(f"\n{'W':>5} {'H_p_up':>7} {'tail':>6} | {'信号':>4} {'WR':>6} {'PNL':>7} {'PNL/笔':>7} | {'做多':>4} {'做空':>4} {'avg_dev':>7}")
print("-" * 80)

all_results = {}
for W, H, t in COMBOS:
    res = eval_combo(W, H, t)
    s = summarize(res, f"W={W} H={H} t={t}")
    ups = sum(1 for r in res if r["dir"] == 1)
    dns = sum(1 for r in res if r["dir"] == -1)
    avg_dev = np.mean([r["dev"] for r in res]) if res else 0
    bar = "█" * int(s["wr"] / 4)
    print(f"{W:>5} {H:>7} {t:>6.2f} | {s['n']:>4} {s['wr']:>5.1f}% {s['pnl']:>+6.1f} {s.get('pnl_per',0):>+6.2f} | {ups:>4} {dns:>4} {avg_dev:>6.1f} {bar}")
    all_results[(W, H, t)] = res

# 趋势过滤效果
print(f"\n--- 趋势强度过滤效果 (|trend_300|>5bps) ---")
print(f"{'W':>5} {'H':>7} {'t':>6} | {'全部':>4} {'全WR':>6} {'全PNL':>7} | {'过滤':>4} {'过WR':>6} {'过PNL':>7} | {'跳过':>4} {'跳WR':>6} | {'ΔWR':>5}")
print("-" * 95)

for W, H, t in COMBOS:
    res_all = eval_combo(W, H, t)
    res_filt = eval_combo(W, H, t, trend_tw=300, trend_thr=5.0)
    s_all = summarize(res_all)
    s_filt = summarize(res_filt)
    skipped_n = s_all["n"] - s_filt["n"]
    skipped_wins = s_all["wins"] - s_filt["wins"]
    skip_wr = skipped_wins / skipped_n * 100 if skipped_n > 0 else 0
    delta = s_filt["wr"] - s_all["wr"]
    verdict = "✓" if delta > 5 else ("?" if abs(delta) < 5 else "✗")
    print(f"{W:>5} {H:>7} {t:>6.2f} | {s_all['n']:>4} {s_all['wr']:>5.1f}% {s_all['pnl']:>+6.1f} | {s_filt['n']:>4} {s_filt['wr']:>5.1f}% {s_filt['pnl']:>+6.1f} | {skipped_n:>4} {skip_wr:>5.1f}% | {delta:>+4.1f} {verdict}")

# ============================================================
# PHASE 2: 多因子网格搜索
# ============================================================
print(f"\n{'='*100}")
print(f"PHASE 2: 多因子网格搜索")
print(f"{'='*100}")

# 2a: 无过滤 — 全参数扫描
W_GRID = [60, 120, 180, 300, 600, 900]
H_GRID = [30, 60, 120, 300, 600, 900, 1200, 1800, 3600, 5400]
T_GRID = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]

print(f"\n扫描: W={W_GRID}, H={H_GRID}, tail={T_GRID}")
print(f"组合数: {len(W_GRID)*len(H_GRID)*len(T_GRID)}")

best_no_filter = []
for W in W_GRID:
    for H in H_GRID:
        for t in T_GRID:
            res = eval_combo(W, H, t)
            if len(res) < 10:
                continue
            s = summarize(res)
            if s["n"] >= 10:
                best_no_filter.append((W, H, t, s["n"], s["wr"], s["pnl"], s["pnl_per"]))

best_no_filter.sort(key=lambda x: x[5], reverse=True)  # 按PNL排序

print(f"\n--- Top 20 (无过滤, 按PNL) ---")
print(f"{'#':>3} {'W':>5} {'H':>7} {'tail':>6} | {'信号':>4} {'WR':>6} {'PNL':>7} {'PNL/笔':>7} | {'判定':>10}")
print("-" * 70)
for i, (W, H, t, n, wr, pnl, pp) in enumerate(best_no_filter[:20]):
    verdict = "★★★" if wr > 80 and n >= 20 else ("★★" if wr > 75 else ("★" if wr > BE else ""))
    bar = "█" * int(wr / 4)
    print(f"{i+1:>3} {W:>5} {H:>7} {t:>6.2f} | {n:>4} {wr:>5.1f}% {pnl:>+6.1f} {pp:>+6.2f} | {verdict:>10} {bar}")

# 2b: 趋势过滤扫描
print(f"\n--- Top 15 (趋势过滤 |trend_300|>5bps, 按PNL) ---")
best_trend = []
for W in W_GRID:
    for H in H_GRID:
        for t in T_GRID:
            res = eval_combo(W, H, t, trend_tw=300, trend_thr=5.0)
            if len(res) < 8:
                continue
            s = summarize(res)
            best_trend.append((W, H, t, s["n"], s["wr"], s["pnl"], s["pnl_per"]))

best_trend.sort(key=lambda x: x[5], reverse=True)

print(f"{'#':>3} {'W':>5} {'H':>7} {'tail':>6} | {'信号':>4} {'WR':>6} {'PNL':>7} {'PNL/笔':>7} | {'判定':>10}")
print("-" * 70)
for i, (W, H, t, n, wr, pnl, pp) in enumerate(best_trend[:15]):
    verdict = "★★★" if wr > 85 and n >= 15 else ("★★" if wr > 80 else "★")
    bar = "█" * int(wr / 4)
    print(f"{i+1:>3} {W:>5} {H:>7} {t:>6.2f} | {n:>4} {wr:>5.1f}% {pnl:>+6.1f} {pp:>+6.2f} | {verdict:>10} {bar}")

# 2c: 不同趋势阈值
print(f"\n--- 趋势阈值灵敏度 (W=300 H=60 t=0.25) ---")
for thr in [0, 3, 5, 7, 10, 15, 20]:
    res = eval_combo(300, 60, 0.25, trend_tw=300, trend_thr=float(thr))
    s = summarize(res)
    res_all = eval_combo(300, 60, 0.25)
    skip_n = len(res_all) - s["n"]
    skip_w = sum(1 for r in res_all if r not in res) 
    print(f"  >{thr:>2}bps: N={s['n']:>3} WR={s['wr']:>5.1f}% PNL={s['pnl']:>+6.1f} (跳过{skip_n}个)")

# 2d: 不同趋势窗口
print(f"\n--- 趋势窗口对比 (W=300 H=60 t=0.25, thr=5bps) ---")
for tw in TREND_WINDOWS:
    res = eval_combo(300, 60, 0.25, trend_tw=tw, trend_thr=5.0)
    s = summarize(res)
    print(f"  tw={tw:>4}s: N={s['n']:>3} WR={s['wr']:>5.1f}% PNL={s['pnl']:>+6.1f}")

# ============================================================
# PHASE 3: Out-of-Sample 验证
# ============================================================
print(f"\n{'='*100}")
print(f"PHASE 3: Out-of-Sample 验证")
print(f"{'='*100}")

mid = N // 2
print(f"训练集: 0~{mid} ({mid/60:.0f}min), 验证集: {mid}~{N} ({(N-mid)/60:.0f}min)")

# 在训练集上找top参数，在验证集上测试
def eval_combo_range(W, H_p_up, tail, lo, hi, trend_tw=0, trend_thr=0.0):
    """在指定index范围内评估"""
    sigs = get_signals(W, H_p_up, tail)
    results = []
    for gidx, d, p in sigs:
        if gidx < lo or gidx >= hi:
            continue
        r = settle(gidx, d)
        if r is None:
            continue
        win, dev = r
        if trend_tw > 0 and abs(trend_ret[trend_tw][gidx]) < trend_thr:
            continue
        results.append({"gidx": gidx, "dir": d, "win": win, "dev": dev})
    return results

# 训练集top10 (无过滤)
train_results = []
for W in W_GRID:
    for H in H_GRID:
        for t in T_GRID:
            res = eval_combo_range(W, H, t, 0, mid)
            if len(res) < 5:
                continue
            s = summarize(res)
            train_results.append((W, H, t, s["n"], s["wr"], s["pnl"]))

train_results.sort(key=lambda x: x[5], reverse=True)
top10_train = train_results[:10]

print(f"\n{'W':>5} {'H':>7} {'t':>6} | {'训练信号':>6} {'训练WR':>7} {'训练PNL':>8} | {'验证信号':>6} {'验证WR':>7} {'验证PNL':>8} | {'OOS判定':>8}")
print("-" * 85)
for W, H, t, tn, twr, tpnl in top10_train:
    val_res = eval_combo_range(W, H, t, mid, N)
    vs = summarize(val_res)
    oos_ok = "✓稳健" if vs["wr"] > BE and vs["n"] >= 5 else ("?样本少" if vs["n"] < 5 else "✗失效")
    delta = vs["wr"] - twr
    print(f"{W:>5} {H:>7} {t:>6.2f} | {tn:>6} {twr:>6.1f}% {tpnl:>+7.1f} | {vs['n']:>6} {vs['wr']:>6.1f}% {vs['pnl']:>+7.1f} | {oos_ok:>8}")

# 训练集top10 (趋势过滤)
print(f"\n--- 趋势过滤 OOS (|trend_300|>5bps) ---")
train_trend = []
for W in W_GRID:
    for H in H_GRID:
        for t in T_GRID:
            res = eval_combo_range(W, H, t, 0, mid, trend_tw=300, trend_thr=5.0)
            if len(res) < 4:
                continue
            s = summarize(res)
            train_trend.append((W, H, t, s["n"], s["wr"], s["pnl"]))

train_trend.sort(key=lambda x: x[5], reverse=True)
top10_train_trend = train_trend[:10]

print(f"{'W':>5} {'H':>7} {'t':>6} | {'训练信号':>6} {'训练WR':>7} {'训练PNL':>8} | {'验证信号':>6} {'验证WR':>7} {'验证PNL':>8} | {'OOS判定':>8}")
print("-" * 85)
for W, H, t, tn, twr, tpnl in top10_train_trend:
    val_res = eval_combo_range(W, H, t, mid, N, trend_tw=300, trend_thr=5.0)
    vs = summarize(val_res)
    oos_ok = "✓稳健" if vs["wr"] > BE and vs["n"] >= 3 else ("?样本少" if vs["n"] < 3 else "✗失效")
    print(f"{W:>5} {H:>7} {t:>6.2f} | {tn:>6} {twr:>6.1f}% {tpnl:>+7.1f} | {vs['n']:>6} {vs['wr']:>6.1f}% {vs['pnl']:>+7.1f} | {oos_ok:>8}")

# ============================================================
# PHASE 4: 稳健参数筛选 — 训练和验证都赚钱
# ============================================================
print(f"\n{'='*100}")
print(f"PHASE 4: 稳健参数 — 两段都赚钱")
print(f"{'='*100}")

robust = []
for W in W_GRID:
    for H in H_GRID:
        for t in T_GRID:
            # 无过滤
            tr = eval_combo_range(W, H, t, 0, mid)
            va = eval_combo_range(W, H, t, mid, N)
            if len(tr) < 5 or len(va) < 3:
                continue
            str_ = summarize(tr)
            sva = summarize(va)
            if str_["pnl"] > 0 and sva["pnl"] > 0:
                all_res = eval_combo(W, H, t)
                sall = summarize(all_res)
                robust.append((W, H, t, "无过滤", sall["n"], sall["wr"], sall["pnl"], str_["wr"], sva["wr"]))

            # 趋势过滤
            tr2 = eval_combo_range(W, H, t, 0, mid, trend_tw=300, trend_thr=5.0)
            va2 = eval_combo_range(W, H, t, mid, N, trend_tw=300, trend_thr=5.0)
            if len(tr2) < 4 or len(va2) < 2:
                continue
            str2 = summarize(tr2)
            sva2 = summarize(va2)
            if str2["pnl"] > 0 and sva2["pnl"] > 0:
                all_res2 = eval_combo(W, H, t, trend_tw=300, trend_thr=5.0)
                sall2 = summarize(all_res2)
                robust.append((W, H, t, "趋势过滤", sall2["n"], sall2["wr"], sall2["pnl"], str2["wr"], sva2["wr"]))

robust.sort(key=lambda x: x[6], reverse=True)

print(f"\n{'#':>3} {'W':>5} {'H':>7} {'t':>6} {'过滤':>6} | {'全信号':>4} {'全WR':>6} {'全PNL':>7} | {'训练WR':>7} {'验证WR':>7} | {'判定':>10}")
print("-" * 85)
for i, (W, H, t, filt, n, wr, pnl, tr_wr, va_wr) in enumerate(robust[:20]):
    consistent = "★★★" if tr_wr > BE and va_wr > BE and wr > 80 else ("★★" if wr > 75 else "★")
    bar = "█" * int(wr / 4)
    print(f"{i+1:>3} {W:>5} {H:>7} {t:>6.2f} {filt:>6} | {n:>4} {wr:>5.1f}% {pnl:>+6.1f} | {tr_wr:>6.1f}% {va_wr:>6.1f}% | {consistent:>10} {bar}")

print(f"\n共{len(robust)}个稳健参数组合 (两段PNL>0)")
print(f"其中WR>75%的: {sum(1 for r in robust if r[5]>75)}个")
print(f"其中WR>80%的: {sum(1 for r in robust if r[5]>80)}个")

# ============================================================
# 保存结果
# ============================================================
result_json = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "data_rows": N,
    "data_minutes": N // 60,
    "best_no_filter": [{"W":r[0],"H":r[1],"tail":r[2],"n":r[3],"wr":r[4],"pnl":r[5],"pnl_per":r[6]} for r in best_no_filter[:10]],
    "best_trend": [{"W":r[0],"H":r[1],"tail":r[2],"n":r[3],"wr":r[4],"pnl":r[5],"pnl_per":r[6]} for r in best_trend[:10]],
    "robust": [{"W":r[0],"H":r[1],"tail":r[2],"filter":r[3],"n":r[4],"wr":r[5],"pnl":r[6],"train_wr":r[7],"val_wr":r[8]} for r in robust[:10]],
}
out_path = "e:/python-binance/tmp/research_1s_master.json"
with open(out_path, "w") as f:
    json.dump(result_json, f, ensure_ascii=False, indent=2)
print(f"\n结果已保存: {out_path}")
