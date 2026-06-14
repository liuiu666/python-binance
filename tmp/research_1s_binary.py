"""
秒级10分钟二元期权策略研究
===========================================
固定: H_settle = 600s (10分钟到期结算)
自由参数: W(回看窗口), H_p_up(预测horizon), tail(触发阈值), cd(冷却)

关键洞察: H_p_up(模型预测窗口) ≠ H_settle(结算窗口)
  - 模型可以用20分钟视角发现极端偏离
  - 但期权只在10分钟后结算
  - 策略价值 = 20分钟极端信号在10分钟内回归
"""
import math, time
import numpy as np
import pandas as pd

PAYOUT = 0.80
BE = 1.0 / (1.0 + PAYOUT) * 100  # 55.56%
H_SETTLE = 600  # 10分钟，固定不变

# ── 加载秒级数据 ──
# CSV列名已验证: timestamp, close, volume, taker_buy_volume, taker_sell_volume, taker_buy_sell_ratio
df = pd.read_csv("e:/python-binance/tmp/server_1s_trades.csv")
df["ts"] = pd.to_datetime(df["timestamp"], utc=True)
df = df.sort_values("ts").reset_index(drop=True)
close = df["close"].values.astype(float)
volume = df["volume"].values.astype(float)
if "taker_buy_sell_ratio" in df.columns:
    tbsr = df["taker_buy_sell_ratio"].values.astype(float)
else:
    tbv = df.get("taker_buy_volume", pd.Series([0]*len(df))).values.astype(float)
    tsv = df.get("taker_sell_volume", pd.Series([1]*len(df))).values.astype(float)
    tbsr = np.where(tsv > 0, tbv / np.maximum(tsv, 1e-10), 1.0)

N = len(close)
MINUTES = N / 60.0

# ── 预计算 ──
lr = np.log(close[1:] / close[:-1])
lr = np.where(np.isfinite(lr), lr, 0.0)
vol_arr = volume[:-1].copy()

cs_lr   = np.concatenate([[0.0], np.cumsum(lr)])
cs_lr2  = np.concatenate([[0.0], np.cumsum(lr ** 2)])
cs_vol  = np.concatenate([[0.0], np.cumsum(vol_arr)])

ncdf = np.vectorize(lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0))))

# H_settle=600后的涨跌结果（全样本，只算一次）
# indices[i] + H_SETTLE < N
max_eval = N - H_SETTLE
future_close = close[H_SETTLE:]  # close[H_SETTLE], close[H_SETTLE+1], ...

print(f"秒级数据 | {N}行, {MINUTES:.0f}分钟 ({MINUTES/60:.1f}小时)")
print(f"H_settle = {H_SETTLE}s (10分钟到期) — 固定不变")
print(f"价格区间: {close.min():.1f} ~ {close.max():.1f} ({(close.max()/close.min()-1)*100:.2f}%)")
print(f"波动率: {np.std(lr)*np.sqrt(60)*1e4:.1f} bps/min")

# ============================================================
# Part 1: 数据评估
# ============================================================
print(f"\n{'='*100}")
print(f"【Part 1】数据评估 — 不同W下的可用窗口和信号密度")
print("-" * 100)

W_CHECK = [60, 120, 300, 600, 900, 1200, 1800, 2400, 3600, 4800, 6000]
print(f"{'W(秒)':>8} {'W(分)':>6} | {'可用窗口':>8} | {'估计信号数@tail=0.20':>22}")
print("-" * 60)
for W in W_CHECK:
    avail = max_eval - W
    if avail <= 0:
        print(f"W={W:>5} {W//60:>4}min |     0    | 不可用")
        continue
    # 估计信号密度：tail=0.20 → 约 2*0.20=40%的bar是信号候选（理论上）
    # 实际看数据
    idxs = np.arange(W, max_eval)
    if len(idxs) == 0:
        print(f"W={W:>5} {W//60:>4}min |     0    | 0")
        continue
    s  = cs_lr[idxs] - cs_lr[idxs - W]
    s2 = cs_lr2[idxs] - cs_lr2[idxs - W]
    mu = s / W
    var = np.maximum((s2 / W) - mu**2, 0.0) * W / (W - 1)
    sigma = np.sqrt(var)
    z = np.sqrt(600) * mu / np.maximum(sigma, 1e-10)  # 用H_p_up=600估计
    p = ncdf(z)
    n_raw = np.sum((p <= 0.20) | (p >= 0.80))
    print(f"W={W:>5} {W//60:>4}min | {avail:>6}s ({avail/60:.0f}min) | ~{n_raw}个原始信号 (cd前)")

# ============================================================
# Part 2: 核心参数扫描 W × H_p_up × tail
# ============================================================
print(f"\n{'='*100}")
print(f"【Part 2】全局参数扫描 — H_settle=600s固定")
print("-" * 100)

W_LIST     = [60, 120, 300, 600, 900, 1200, 1800, 2400, 3600, 4800, 6000]
HPUP_LIST  = [60, 120, 300, 600, 900, 1200, 1800]
TAIL_LIST  = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]

all_results = []

def run_backtest_1s(W, H_p_up, tail, cd=None):
    """回测：H_settle=600s固定，W/H_p_up/tail自由"""
    if cd is None:
        cd = max(W // 2, 30)
    
    max_idx = N - H_SETTLE
    if W >= max_idx:
        return None
    
    indices = np.arange(W, max_idx)
    
    # 用W窗口算μ, σ
    s  = cs_lr[indices] - cs_lr[indices - W]
    s2 = cs_lr2[indices] - cs_lr2[indices - W]
    mu = s / W
    var = np.maximum((s2 / W) - mu**2, 0.0) * W / (W - 1)
    sigma = np.sqrt(var)
    
    # z-score用H_p_up缩放
    z = np.sqrt(H_p_up) * mu / np.maximum(sigma, 1e-10)
    p_up = ncdf(z)
    
    # 信号
    sig = np.zeros(len(indices), dtype=np.int8)
    sig[p_up <= tail] = 1      # 做多信号
    sig[p_up >= 1-tail] = -1   # 做空信号
    
    # 结算：H_settle=600s后价格涨跌
    settle_up = future_close[indices] > close[indices]  # indices对齐future_close
    
    # cooldown过滤
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
        return {"W": W, "H_p_up": H_p_up, "tail": tail, "cd": cd,
                "n_sig": 0, "wr": 0, "pnl": 0, "wins": 0, "losses": 0,
                "up_w": 0, "up_n": 0, "dn_w": 0, "dn_n": 0,
                "p_up": p_up, "settle_up": settle_up, "indices": indices}
    
    wins = up_w = dn_w = 0
    for si in sig_pos:
        gidx = indices[si]
        d = filtered[si]
        went_up = settle_up[si]
        win = (went_up and d == 1) or (not went_up and d == -1)
        if win:
            wins += 1
            if d == 1: up_w += 1
            else: dn_w += 1
    
    up_n = sum(1 for si in sig_pos if filtered[si] == 1)
    dn_n = n_sig - up_n
    losses = n_sig - wins
    wr = wins / n_sig * 100
    pnl = wins * PAYOUT - losses * 1.0
    
    return {"W": W, "H_p_up": H_p_up, "tail": tail, "cd": cd,
            "n_sig": n_sig, "wr": wr, "pnl": pnl, "wins": wins, "losses": losses,
            "up_w": up_w, "up_n": up_n, "dn_w": dn_w, "dn_n": dn_n,
            "p_up": p_up, "settle_up": settle_up, "indices": indices}

t0 = time.time()
total_combos = len(W_LIST) * len(HPUP_LIST) * len(TAIL_LIST)
combo_idx = 0

for W in W_LIST:
    for H_p_up in HPUP_LIST:
        for tail in TAIL_LIST:
            combo_idx += 1
            r = run_backtest_1s(W, H_p_up, tail)
            if r and r["n_sig"] > 0:
                all_results.append(r)

elapsed = time.time() - t0
print(f"扫描完成: {total_combos}组合, {elapsed:.1f}s, {len(all_results)}个有信号结果")

# 盈利组合排名
profitable = [r for r in all_results if r["pnl"] > 0 and r["n_sig"] >= 5]
profitable.sort(key=lambda x: (x["wr"], -x["n_sig"]), reverse=True)

print(f"\n盈利组合 (N≥5, PNL>0, 按WR降序):")
print(f"{'排名':>4} {'W(秒)':>7} {'W(分)':>5} {'H_pup':>6} {'tail':>5} {'cd':>5} | {'N':>4} {'WR':>6} {'PNL':>7} {'边际':>7} | {'UP':>8} {'DN':>8}")
print("-" * 90)
for i, r in enumerate(profitable[:25]):
    margin = r["wr"] - BE
    up_str = f"{r['up_w']}/{r['up_n']}" if r["up_n"] > 0 else "-"
    dn_str = f"{r['dn_w']}/{r['dn_n']}" if r["dn_n"] > 0 else "-"
    print(f"{i+1:>4} {r['W']:>6}s {r['W']//60:>4}m {r['H_p_up']:>5}s {r['tail']:>5.2f} {r['cd']:>5}s | {r['n_sig']:>4} {r['wr']:>5.1f}% {r['pnl']:>+6.1f} {margin:>+6.1f}% | {up_str:>8} {dn_str:>8}")

# WR热力图: 固定tail=0.20, W × H_p_up
print(f"\nWR热力图 (tail=0.20, H_settle=600s):")
print(f"{'W/H_pup':>8}", end="")
for hp in HPUP_LIST:
    print(f" {hp//60:>5}min", end="")
print()
print("-" * (10 + 7 * len(HPUP_LIST)))
for W in W_LIST:
    print(f"W={W//60:>4}min", end="")
    for hp in HPUP_LIST:
        matches = [r for r in all_results if r["W"]==W and r["H_p_up"]==hp and r["tail"]==0.20]
        if matches and matches[0]["n_sig"] >= 3:
            r = matches[0]
            star = "★" if r["wr"] >= BE and r["pnl"] > 0 else ("~" if r["wr"] >= 50 else "✗")
            print(f" {r['wr']:>4.0f}{star}{r['n_sig']:>2}", end="")
        else:
            n = matches[0]["n_sig"] if matches else 0
            print(f"   --- ", end="")
    print()

# ============================================================
# Part 3: H_p_up解耦分析
# ============================================================
print(f"\n{'='*100}")
print(f"【Part 3】H_p_up vs H_settle 解耦 — H_settle=600s固定, 哪个H_p_up最好?")
print("-" * 100)

# 固定W=300, tail=0.20, 扫描H_p_up
W_FIX = 300
TAIL_FIX = 0.20
print(f"\n固定 W={W_FIX}s({W_FIX//60}min), tail={TAIL_FIX}, 扫描H_p_up:")
print(f"{'H_p_up':>8} | {'N':>5} {'WR':>6} {'PNL':>7} {'边际':>7} | {'H_pup vs H_settle':>18}")
print("-" * 65)
for hp in HPUP_LIST:
    matches = [r for r in all_results if r["W"]==W_FIX and r["H_p_up"]==hp and r["tail"]==TAIL_FIX]
    if matches and matches[0]["n_sig"] > 0:
        r = matches[0]
        rel = f"{hp/600:.1f}x settle" if hp != 600 else "= settle"
        print(f"{hp:>6}s({hp//60:>2}m) | {r['n_sig']:>5} {r['wr']:>5.1f}% {r['pnl']:>+6.1f} {r['wr']-BE:>+6.1f}% | {rel:>18}")

# 多W的H_p_up最优值
print(f"\n各W下的最优H_p_up (tail=0.20):")
print(f"{'W':>8} | {'最优H_pup':>10} {'WR':>6} {'N':>5} | {'H_pup=600 WR':>12} {'N':>5} | {'差异':>6}")
print("-" * 70)
for W in [60, 120, 300, 600, 1200, 1800, 2400, 3600]:
    hp_results = [(r["H_p_up"], r) for r in all_results if r["W"]==W and r["tail"]==0.20 and r["n_sig"]>=5]
    if not hp_results:
        continue
    best_hp, best_r = max(hp_results, key=lambda x: x[1]["wr"])
    settle_match = [r for hp, r in hp_results if hp == 600]
    settle_r = settle_match[0] if settle_match else None
    s_wr = f"{settle_r['wr']:.1f}%" if settle_r else "---"
    s_n = f"{settle_r['n_sig']}" if settle_r else "---"
    diff = best_r["wr"] - settle_r["wr"] if settle_r else 0
    print(f"W={W//60:>4}min | {best_hp:>6}s({best_hp//60:>2}m) {best_r['wr']:>5.1f}% {best_r['n_sig']:>5} | {s_wr:>12} {s_n:>5} | {diff:>+5.1f}%")

# ============================================================
# Part 4: p_up校准
# ============================================================
print(f"\n{'='*100}")
print(f"【Part 4】p_up校准 — H_settle=600s实际结果 vs 模型预测")
print("-" * 100)

# 用W=300, H_p_up=600做校准
CALIB_W, CALIB_HP = 300, 600
calib_match = [r for r in all_results if r["W"]==CALIB_W and r["H_p_up"]==CALIB_HP and r["tail"]==0.20]
if calib_match:
    r = calib_match[0]
    p_up_arr = r["p_up"]
    actual_arr = r["settle_up"]
    
    bins = np.arange(0, 1.05, 0.05)
    print(f"参数: W={CALIB_W}s, H_p_up={CALIB_HP}s, H_settle={H_SETTLE}s")
    print(f"{'p_up区间':>12} | {'样本':>6} | {'实际上涨%':>10} | {'模型预测%':>10} | {'偏差':>8} | {'信号方向':>8}")
    print("-" * 85)
    for j in range(len(bins) - 1):
        mask = (p_up_arr >= bins[j]) & (p_up_arr < bins[j + 1])
        cnt = mask.sum()
        if cnt > 10:
            actual_pct = actual_arr[mask].mean() * 100
            pred_pct = (bins[j] + bins[j + 1]) / 2 * 100
            bias = pred_pct - actual_pct
            direction = "→ DN信号" if pred_pct < 25 else ("→ UP信号" if pred_pct > 75 else "")
            print(f"  {bins[j]:.2f}-{bins[j+1]:.2f} | {cnt:>6} | {actual_pct:>9.1f}% | {pred_pct:>9.1f}% | {bias:>+7.1f}% | {direction:>8}")

# ============================================================
# Part 5: 赢输轨迹
# ============================================================
print(f"\n{'='*100}")
print(f"【Part 5】赢输轨迹 — H_settle=600s内逐分钟偏离")
print("-" * 100)

# 选盈利最好的组合做轨迹
if profitable:
    best = profitable[0]
    W_T, HP_T, TAIL_T = best["W"], best["H_p_up"], best["tail"]
    cd_t = best["cd"]
    print(f"使用: W={W_T}s({W_T//60}min) H_p_up={HP_T}s tail={TAIL_T} → {best['n_sig']}信号 WR={best['wr']:.1f}%")
    
    # 重新跑获取详细交易
    r_full = run_backtest_1s(W_T, HP_T, TAIL_T, cd_t)
    
    # 逐分钟轨迹
    check_points = list(range(0, H_SETTLE + 1, 60))  # 每60秒
    if H_SETTLE not in check_points:
        check_points.append(H_SETTLE)
    
    # 获取所有信号位置
    indices = r_full["indices"]
    sig = np.zeros(len(indices), dtype=np.int8)
    z_arr = np.sqrt(HP_T) * (cs_lr[indices] - cs_lr[indices-W_T]) / W_T / np.maximum(
        np.sqrt(np.maximum((cs_lr2[indices]-cs_lr2[indices-W_T])/W_T - ((cs_lr[indices]-cs_lr[indices-W_T])/W_T)**2, 0)*W_T/(W_T-1)), 1e-10)
    p_arr = ncdf(z_arr)
    sig[p_arr <= TAIL_T] = 1
    sig[p_arr >= 1-TAIL_T] = -1
    
    # cooldown
    filtered = sig.copy()
    last_bar = -99999
    sig_pos = []
    for i in range(len(filtered)):
        if filtered[i] != 0:
            if indices[i] - last_bar >= cd_t:
                last_bar = indices[i]
                sig_pos.append(i)
            else:
                filtered[i] = 0
    
    # 计算每笔交易的赢亏
    trades_detail = []
    for si in sig_pos:
        gidx = indices[si]
        d = filtered[si]
        entry = close[gidx]
        settle_price = close[gidx + H_SETTLE]
        went_up = settle_price > entry
        win = (went_up and d == 1) or (not went_up and d == -1)
        trades_detail.append({"idx": gidx, "dir": d, "entry": entry, "win": win})
    
    win_trades = [t for t in trades_detail if t["win"]]
    loss_trades = [t for t in trades_detail if not t["win"]]
    
    print(f"\n{'秒':>5} | {'赢({}笔)'.format(len(win_trades)):>14} | {'输({}笔)'.format(len(loss_trades)):>14} | {'差异':>8} |")
    print("-" * 60)
    
    for sec in check_points:
        w_vals = []
        l_vals = []
        for t in win_trades:
            idx = t["idx"] + sec
            if idx < N:
                dev = (close[idx] - t["entry"]) / t["entry"] * 10000
                if t["dir"] == -1: dev = -dev
                w_vals.append(dev)
        for t in loss_trades:
            idx = t["idx"] + sec
            if idx < N:
                dev = (close[idx] - t["entry"]) / t["entry"] * 10000
                if t["dir"] == -1: dev = -dev
                l_vals.append(dev)
        
        w_avg = np.mean(w_vals) if w_vals else 0
        l_avg = np.mean(l_vals) if l_vals else 0
        diff = w_avg - l_avg
        bar = "█" * int(abs(diff)) if abs(diff) > 1 else ""
        print(f"{sec:>4}s | {w_avg:>+10.2f} bps | {l_avg:>+10.2f} bps | {diff:>+6.2f} |{bar}")

# ============================================================
# Part 6: 方向拆分
# ============================================================
print(f"\n{'='*100}")
print(f"【Part 6】UP vs DN方向拆分 — H_settle=600s")
print("-" * 100)

print(f"{'W':>7} {'H_pup':>6} {'tail':>5} | {'UP_N':>5} {'UP_WR':>6} {'UP_PNL':>7} | {'DN_N':>5} {'DN_WR':>6} {'DN_PNL':>7} | {'差异':>6}")
print("-" * 80)
for r in sorted(profitable[:15], key=lambda x: x["W"]):
    up_wr = r["up_w"]/r["up_n"]*100 if r["up_n"] > 0 else 0
    dn_wr = r["dn_w"]/r["dn_n"]*100 if r["dn_n"] > 0 else 0
    up_pnl = r["up_w"]*PAYOUT - (r["up_n"]-r["up_w"])*1.0 if r["up_n"] > 0 else 0
    dn_pnl = r["dn_w"]*PAYOUT - (r["dn_n"]-r["dn_w"])*1.0 if r["dn_n"] > 0 else 0
    diff = up_wr - dn_wr if r["up_n"] > 0 and r["dn_n"] > 0 else 0
    print(f"{r['W']//60:>5}min {r['H_p_up']//60:>4}min {r['tail']:>5.2f} | {r['up_n']:>5} {up_wr:>5.1f}% {up_pnl:>+6.1f} | {r['dn_n']:>5} {dn_wr:>5.1f}% {dn_pnl:>+6.1f} | {diff:>+5.1f}%")

# ============================================================
# Part 7: 量比过滤
# ============================================================
print(f"\n{'='*100}")
print(f"【Part 7】量比过滤 — taker_buy_sell_ratio能否提升胜率?")
print("-" * 100)

# 用最优组合，测试量比过滤
if profitable:
    best = profitable[0]
    W_V, HP_V, TAIL_V = best["W"], best["H_p_up"], best["tail"]
    cd_v = best["cd"]
    
    r_base = run_backtest_1s(W_V, HP_V, TAIL_V, cd_v)
    indices_v = r_base["indices"]
    
    # 重新算信号
    s_v = cs_lr[indices_v] - cs_lr[indices_v - W_V]
    s2_v = cs_lr2[indices_v] - cs_lr2[indices_v - W_V]
    mu_v = s_v / W_V
    var_v = np.maximum((s2_v / W_V) - mu_v**2, 0.0) * W_V / (W_V - 1)
    sigma_v = np.sqrt(var_v)
    z_v = np.sqrt(HP_V) * mu_v / np.maximum(sigma_v, 1e-10)
    p_v = ncdf(z_v)
    sig_v = np.zeros(len(indices_v), dtype=np.int8)
    sig_v[p_v <= TAIL_V] = 1
    sig_v[p_v >= 1-TAIL_V] = -1
    
    # 每个信号点的量比
    tbsr_at_signal = tbsr[indices_v] if len(tbsr) > indices_v.max() else np.ones(len(indices_v))
    
    # 不过滤
    sig_raw = sig_v.copy()
    last = -99999
    pos_raw = []
    for i in range(len(sig_raw)):
        if sig_raw[i] != 0 and indices_v[i] - last >= cd_v:
            last = indices_v[i]
            pos_raw.append(i)
    
    wins_raw = sum(1 for si in pos_raw if 
        (future_close[indices_v[si]] > close[indices_v[si]] and sig_raw[si]==1) or
        (future_close[indices_v[si]] <= close[indices_v[si]] and sig_raw[si]==-1))
    n_raw = len(pos_raw)
    wr_raw = wins_raw/n_raw*100 if n_raw > 0 else 0
    
    print(f"基线: W={W_V}s H_pup={HP_V}s tail={TAIL_V} → {n_raw}信号 WR={wr_raw:.1f}%")
    print(f"\n量比过滤效果:")
    print(f"{'过滤条件':>20} | {'N':>5} {'WR':>6} {'PNL':>7} {'边际变化':>8}")
    print("-" * 60)
    
    for filt_name, filt_fn in [
        ("无过滤", lambda d, r: True),
        ("UP且ratio>1.0", lambda d, r: not (d == 1 and r <= 1.0)),
        ("UP且ratio>1.2", lambda d, r: not (d == 1 and r <= 1.2)),
        ("DN且ratio<1.0", lambda d, r: not (d == -1 and r >= 1.0)),
        ("DN且ratio<0.8", lambda d, r: not (d == -1 and r >= 0.8)),
        ("双向确认", lambda d, r: (d == 1 and r > 1.0) or (d == -1 and r < 1.0)),
    ]:
        wins_f = n_f = 0
        for si in pos_raw:
            d = sig_raw[si]
            r_val = tbsr_at_signal[si]
            if filt_fn(d, r_val):
                n_f += 1
                went_up = future_close[indices_v[si]] > close[indices_v[si]]
                if (went_up and d == 1) or (not went_up and d == -1):
                    wins_f += 1
        wr_f = wins_f/n_f*100 if n_f > 0 else 0
        pnl_f = wins_f*PAYOUT - (n_f-wins_f)*1.0 if n_f > 0 else 0
        margin_change = wr_f - wr_raw if n_f > 0 else 0
        print(f"{filt_name:>20} | {n_f:>5} {wr_f:>5.1f}% {pnl_f:>+6.1f} {margin_change:>+7.1f}%")

# ============================================================
# Part 8: 总结
# ============================================================
print(f"\n{'='*100}")
print(f"【Part 8】总结")
print("-" * 100)

print(f"\n数据: {N}行, {MINUTES:.0f}分钟 ({MINUTES/60:.1f}小时), 波动{(close.max()/close.min()-1)*100:.2f}%")
print(f"H_settle={H_SETTLE}s固定, 扫描{total_combos}组合")

if profitable:
    top5 = profitable[:5]
    print(f"\nTop 5盈利组合:")
    for i, r in enumerate(top5):
        print(f"  #{i+1}: W={r['W']}s({r['W']//60}m) H_pup={r['H_p_up']}s tail={r['tail']} cd={r['cd']}s → N={r['n_sig']} WR={r['wr']:.1f}% PNL={r['pnl']:+.1f} 边际={r['wr']-BE:+.1f}%")
    
    # H_p_up分布
    hp_dist = {}
    for r in top5:
        hp = r["H_p_up"]
        hp_dist[hp] = hp_dist.get(hp, 0) + 1
    print(f"\nTop5的H_p_up分布: {hp_dist}")
    
    # W分布
    w_dist = {}
    for r in top5:
        w = r["W"]
        w_dist[w] = w_dist.get(w, 0) + 1
    print(f"Top5的W分布: {w_dist}")
else:
    print(f"\n没有N≥5的盈利组合")

n_above_be = len([r for r in all_results if r["wr"] >= BE and r["n_sig"] >= 5])
n_total_valid = len([r for r in all_results if r["n_sig"] >= 5])
print(f"\n统计: {n_above_be}/{n_total_valid}个有效组合(N≥5)超越盈亏平衡线{BE:.1f}%")
print(f"\n注意: 仅{MINUTES:.0f}分钟数据, 价格波动仅{(close.max()/close.min()-1)*100:.2f}%,")
print(f"      所有结论需更多数据验证。建议至少积累24小时(86400行)再做最终判断。")
