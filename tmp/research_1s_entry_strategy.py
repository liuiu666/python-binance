"""
入场策略优化对比
=================
基于时效性规律设计三种入场方案:
  A: 立即入场     → entry=gidx,       settle=gidx+600
  B: 延迟10秒     → entry=gidx+10,    settle=gidx+610
  C: 放量确认     → entry=确认时刻,   settle=确认时刻+600

放量确认定义:
  baseline = 信号前W秒的平均秒级成交量
  信号后逐秒扫描, 当trailing 60s均量 > k × baseline 时确认入场
  超时窗口 max_wait 内未确认 → 跳过该信号

附加策略:
  D: 量比门控     → 信号时刻量比>k才入场, 否则跳过
"""
import math, numpy as np, pandas as pd

PAYOUT = 0.80
BE = 1.0 / (1.0 + PAYOUT) * 100
H_SETTLE = 600
CD = 600

df = pd.read_csv("e:/python-binance/tmp/server_1s_trades.csv")
df["ts"] = pd.to_datetime(df["timestamp"], utc=True)
df = df.sort_values("ts").reset_index(drop=True)
close = df["close"].values.astype(float)
volume = df["volume"].values.astype(float)
N = len(close)

lr = np.log(close[1:] / close[:-1])
lr = np.where(np.isfinite(lr), lr, 0.0)
cs_lr  = np.concatenate([[0.0], np.cumsum(lr)])
cs_lr2 = np.concatenate([[0.0], np.cumsum(lr ** 2)])
ncdf = np.vectorize(lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0))))
max_eval = N - H_SETTLE

COMBOS = [
    (120, 900, 0.05),
    (300, 60, 0.25),
    (300, 120, 0.15),
    (600, 300, 0.10),
    (300, 300, 0.20),
]

def get_signals(W, H_p_up, tail):
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
            if indices[i] - last_bar >= CD:
                last_bar = indices[i]
                sig_pos.append(i)
            else:
                filtered[i] = 0
    return [(indices[i], filtered[i], p_up[i]) for i in sig_pos]

def settle(entry_idx, direction):
    """结算: entry后600s价格方向 vs 信号方向"""
    s_idx = entry_idx + H_SETTLE
    if s_idx >= N:
        return None
    entry_price = close[entry_idx]
    settle_price = close[s_idx]
    went_up = settle_price > entry_price
    win = (went_up and direction == 1) or (not went_up and direction == -1)
    dev = (settle_price - entry_price) / entry_price * 10000
    if direction == -1:
        dev = -dev
    return win, dev

def find_vol_confirmation(gidx, W, k_threshold, max_wait):
    """
    在信号后 max_wait 秒内, 寻找trailing 60s成交量 > k×baseline 的时刻
    返回确认时刻的绝对index, 或None
    """
    baseline = np.mean(volume[max(0, gidx - W):gidx]) if gidx > W else np.mean(volume[:gidx+1])
    if baseline < 1e-10:
        baseline = 1e-10
    
    end_scan = min(gidx + max_wait, N - H_SETTLE)
    for t in range(gidx + 30, end_scan):  # 从+30s开始扫描(跳过无效应期)
        # trailing 60s average volume at time t
        v_start = max(0, t - 59)
        rolling_vol = np.mean(volume[v_start:t + 1])
        if rolling_vol > k_threshold * baseline:
            return t
    return None

print(f"数据: {N}行, {N/60:.0f}分钟, H_settle={H_SETTLE}s")
print(f"盈亏平衡胜率 BE = {BE:.2f}%")
print("=" * 110)

# ============================================================
# Part 1: 三策略直接对比
# ============================================================
print("\n【Part 1】三策略对比 — 立即 vs 延迟10s vs 放量确认")
print("=" * 110)

# 放量确认参数
K_VOL = 1.5        # 量比阈值
MAX_WAIT = 300     # 最长等待300s(5min)

print(f"放量确认: k={K_VOL}x baseline, 最长等待{MAX_WAIT}s\n")

header = f"{'组合':>30} | {'策略':>8} | {'N':>4} {'WR':>6} {'PNL':>7} {'avg_dev':>8} {'跳过':>4} | {'vs立即WR':>8}"
print(header)
print("-" * 110)

all_results = []  # 存所有结果供后续分析

for W, H_p_up, tail in COMBOS:
    sigs = get_signals(W, H_p_up, tail)
    n_total = len(sigs)
    combo_str = f"W={W}s({W//60}m) H={H_p_up}s t={tail}"
    
    for strat_name in ["立即", "延迟10s", "放量确认"]:
        wins = 0
        devs = []
        n_valid = 0
        n_skip = 0
        confirm_times = []
        
        for gidx, d, p in sigs:
            if strat_name == "立即":
                entry_idx = gidx
            elif strat_name == "延迟10s":
                entry_idx = gidx + 10
            elif strat_name == "放量确认":
                conf = find_vol_confirmation(gidx, W, K_VOL, MAX_WAIT)
                if conf is None:
                    n_skip += 1
                    continue
                entry_idx = conf
                confirm_times.append(conf - gidx)
            
            result = settle(entry_idx, d)
            if result is None:
                n_skip += 1
                continue
            win, dev = result
            n_valid += 1
            if win:
                wins += 1
            devs.append(dev)
        
        wr = wins / n_valid * 100 if n_valid > 0 else 0
        pnl = wins * PAYOUT - (n_valid - wins) * 1.0
        avg_dev = np.mean(devs) if devs else 0
        
        all_results.append({
            "combo": combo_str, "W": W, "H_p_up": H_p_up, "tail": tail,
            "strategy": strat_name, "n_total": n_total, "n_valid": n_valid,
            "n_skip": n_skip, "wins": wins, "wr": wr, "pnl": pnl,
            "avg_dev": avg_dev, "confirm_times": confirm_times if strat_name == "放量确认" else []
        })
    
    # 打印该组合的三策略对比
    wr_immediate = all_results[-3]["wr"] if len(all_results) >= 3 else 0
    for i, r in enumerate(all_results[-3:]):
        delta_wr = r["wr"] - wr_immediate if i > 0 else 0
        delta_str = f"{delta_wr:>+7.1f}%" if i > 0 else "   ---"
        bar = "█" * int(r["wr"] / 3) if r["wr"] > 0 else ""
        print(f"{combo_str:>30} | {r['strategy']:>8} | {r['n_valid']:>4} {r['wr']:>5.1f}% {r['pnl']:>+6.1f} {r['avg_dev']:>+7.1f} {r['n_skip']:>4} | {delta_str} {bar}")
    print()

# ============================================================
# Part 2: 放量确认时刻分布
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 2】放量确认时刻分布 — 信号后多久才放量?")
print("-" * 110)

vol_results = [r for r in all_results if r["strategy"] == "放量确认" and r["confirm_times"]]
for r in vol_results:
    ct = np.array(r["confirm_times"])
    print(f"\n  {r['combo']} → 确认{len(ct)}/{r['n_total']}信号 (跳过{r['n_skip']})")
    print(f"    确认时刻: 中位数={np.median(ct):.0f}s({np.median(ct)/60:.1f}m)  均值={np.mean(ct):.0f}s({np.mean(ct)/60:.1f}m)  范围=[{ct.min()}s, {ct.max()}s]")
    # 分桶
    buckets = [(0,60,"0-1m"), (60,120,"1-2m"), (120,180,"2-3m"), (180,240,"3-4m"), (240,300,"4-5m")]
    for lo, hi, label in buckets:
        cnt = np.sum((ct >= lo) & (ct < hi))
        pct = cnt / len(ct) * 100
        bar = "█" * int(pct / 3)
        print(f"      {label}: {cnt:>3} ({pct:>4.1f}%) {bar}")

# ============================================================
# Part 3: 放量确认跳过的信号是输的吗?
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 3】放量确认跳过的信号 → 如果入场, 是赢还是输?")
print("-" * 110)

for W, H_p_up, tail in COMBOS:
    sigs = get_signals(W, H_p_up, tail)
    combo_str = f"W={W}s({W//60}m) H={H_p_up}s t={tail}"
    
    skipped_wins = 0
    skipped_losses = 0
    confirmed_wins = 0
    confirmed_losses = 0
    
    for gidx, d, p in sigs:
        conf = find_vol_confirmation(gidx, W, K_VOL, MAX_WAIT)
        # 无论是否确认, 都看立即入场的结果
        result = settle(gidx, d)
        if result is None:
            continue
        win, dev = result
        
        if conf is None:
            # 被跳过的信号
            if win:
                skipped_wins += 1
            else:
                skipped_losses += 1
        else:
            if win:
                confirmed_wins += 1
            else:
                confirmed_losses += 1
    
    total_skip = skipped_wins + skipped_losses
    total_conf = confirmed_wins + confirmed_losses
    skip_wr = skipped_wins / total_skip * 100 if total_skip > 0 else 0
    conf_wr = confirmed_wins / total_conf * 100 if total_conf > 0 else 0
    
    print(f"\n  {combo_str}")
    print(f"    被确认的信号: {total_conf}笔  赢{confirmed_wins} 输{confirmed_losses}  WR={conf_wr:.1f}%")
    print(f"    被跳过的信号: {total_skip}笔  赢{skipped_wins} 输{skipped_losses}  WR={skip_wr:.1f}%")
    if total_skip > 0:
        verdict = "✓ 过滤有效" if skip_wr < BE else "✗ 过滤失败(跳过的信号反而赢)"
        print(f"    判定: {verdict}")

# ============================================================
# Part 4: 放量阈值灵敏度 — k=1.2/1.5/2.0 对比
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 4】放量阈值灵敏度 — 不同k值的确认率和胜率")
print("-" * 110)

k_list = [1.2, 1.5, 2.0, 2.5, 3.0]

for W, H_p_up, tail in COMBOS[:3]:  # 取前3个组合
    sigs = get_signals(W, H_p_up, tail)
    combo_str = f"W={W}s H={H_p_up}s t={tail}"
    
    print(f"\n  {combo_str} ({len(sigs)}信号)")
    print(f"    {'阈值':>6} | {'确认N':>5} {'跳过':>4} | {'确认后WR':>8} {'PNL':>7} | {'跳过的WR':>8} | {'判定':>15}")
    print("    " + "-" * 80)
    
    for k in k_list:
        conf_wins = 0
        conf_n = 0
        skip_wins = 0
        skip_n = 0
        
        for gidx, d, p in sigs:
            conf = find_vol_confirmation(gidx, W, k, MAX_WAIT)
            result = settle(gidx, d)
            if result is None:
                continue
            win, dev = result
            
            if conf is not None:
                # 放量确认后的结果
                r = settle(conf, d)
                if r is not None:
                    conf_n += 1
                    if r[0]:
                        conf_wins += 1
            else:
                skip_n += 1
                if win:
                    skip_wins += 1
        
        conf_wr = conf_wins / conf_n * 100 if conf_n > 0 else 0
        skip_wr = skip_wins / skip_n * 100 if skip_n > 0 else 0
        conf_pnl = conf_wins * PAYOUT - (conf_n - conf_wins) * 1.0 if conf_n > 0 else 0
        verdict = "✓有效" if skip_wr < BE and conf_n > 0 else ("?中性" if conf_n > 0 else "✗全跳过")
        
        bar = "█" * int(conf_wr / 4)
        print(f"    k={k:>4.1f}x | {conf_n:>5} {skip_n:>4} | {conf_wr:>7.1f}% {conf_pnl:>+6.1f} | {skip_wr:>7.1f}% | {verdict:>15} {bar}")

# ============================================================
# Part 5: 量比门控策略 (策略D)
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 5】量比门控策略 — 信号时刻量比>k才入场, 否则跳过")
print("-" * 110)

for W, H_p_up, tail in COMBOS:
    sigs = get_signals(W, H_p_up, tail)
    combo_str = f"W={W}s({W//60}m) H={H_p_up}s t={tail}"
    
    print(f"\n  {combo_str} ({len(sigs)}信号)")
    print(f"    {'门控k':>6} | {'入场N':>5} {'WR':>6} {'PNL':>7} | {'跳过N':>5} {'跳过WR':>7} | {'判定':>15}")
    print("    " + "-" * 70)
    
    for k in [0.5, 0.8, 1.0, 1.2, 1.5]:
        gated_wins = 0
        gated_n = 0
        skip_wins = 0
        skip_n = 0
        
        for gidx, d, p in sigs:
            baseline = np.mean(volume[max(0, gidx-W):gidx]) if gidx > W else 1e-10
            vol_now = volume[gidx]
            vr = vol_now / max(baseline, 1e-10)
            
            result = settle(gidx, d)
            if result is None:
                continue
            win, dev = result
            
            if vr >= k:
                gated_n += 1
                if win:
                    gated_wins += 1
            else:
                skip_n += 1
                if win:
                    skip_wins += 1
        
        gated_wr = gated_wins / gated_n * 100 if gated_n > 0 else 0
        gated_pnl = gated_wins * PAYOUT - (gated_n - gated_wins) * 1.0 if gated_n > 0 else 0
        skip_wr = skip_wins / skip_n * 100 if skip_n > 0 else 0
        verdict = "✓有效" if skip_wr < BE and gated_n > 0 else "?中性"
        
        bar = "█" * int(gated_wr / 4)
        print(f"    k>={k:>4.1f} | {gated_n:>5} {gated_wr:>5.1f}% {gated_pnl:>+6.1f} | {skip_n:>5} {skip_wr:>6.1f}% | {verdict:>15} {bar}")

# ============================================================
# Part 6: 策略汇总对比
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 6】策略汇总 — 每策略跨所有组合汇总")
print("-" * 110)

strategies = ["立即", "延迟10s", "放量确认"]
print(f"\n  {'策略':>8} | {'总信号':>5} {'入场':>5} {'跳过':>5} | {'总赢':>4} {'总输':>4} {'WR':>6} {'总PNL':>7} {'avg_dev':>8}")
print("  " + "-" * 75)

for strat in strategies:
    sub = [r for r in all_results if r["strategy"] == strat]
    total_sig = sum(r["n_total"] for r in sub)
    total_valid = sum(r["n_valid"] for r in sub)
    total_skip = sum(r["n_skip"] for r in sub)
    total_wins = sum(r["wins"] for r in sub)
    total_losses = total_valid - total_wins
    wr = total_wins / total_valid * 100 if total_valid > 0 else 0
    total_pnl = sum(r["pnl"] for r in sub)
    avg_dev = np.mean([r["avg_dev"] for r in sub])
    
    bar = "█" * int(wr / 3)
    print(f"  {strat:>8} | {total_sig:>5} {total_valid:>5} {total_skip:>5} | {total_wins:>4} {total_losses:>4} {wr:>5.1f}% {total_pnl:>+6.1f} {avg_dev:>+7.1f} {bar}")

print(f"\n  盈亏平衡线 BE = {BE:.2f}%")

# ============================================================
# Part 7: 逐笔交易明细 (放量确认 vs 立即入场)
# ============================================================
print(f"\n{'='*110}")
print(f"【Part 7】逐笔对比 — 立即 vs 放量确认 (W=300 H=60 t=0.25)")
print("-" * 110)

W, H_p_up, tail = 300, 60, 0.25
sigs = get_signals(W, H_p_up, tail)

print(f"\n  {'#':>3} {'信号时刻':>8} {'方向':>4} {'p_up':>6} | {'立即入场':>16} | {'放量确认':>22} | {'差异':>10}")
print(f"  {'':>3} {'':>8} {'':>4} {'':>6} | {'dev':>8} {'结果':>6} | {'延迟':>6} {'dev':>8} {'结果':>6} | {'dev差':>10}")
print("  " + "-" * 95)

for i, (gidx, d, p) in enumerate(sigs):
    ts_str = f"{gidx//3600}h{(gidx%3600)//60:02d}m"
    dir_str = "↑多" if d == 1 else "↓空"
    
    # 立即
    r1 = settle(gidx, d)
    imm_dev = r1[1] if r1 else 0
    imm_win = "赢" if (r1 and r1[0]) else ("输" if r1 else "?")
    
    # 放量确认
    conf = find_vol_confirmation(gidx, W, K_VOL, MAX_WAIT)
    if conf is not None:
        r2 = settle(conf, d)
        delay = conf - gidx
        conf_dev = r2[1] if r2 else 0
        conf_win = "赢" if (r2 and r2[0]) else ("输" if r2 else "?")
        delay_str = f"{delay:>4}s"
    else:
        conf_dev = 0
        conf_win = "跳过"
        delay_str = "  ---"
    
    dev_diff = conf_dev - imm_dev if conf_win != "跳过" else 0
    match = "==" if imm_win == conf_win else "!="
    
    print(f"  {i+1:>3} {ts_str:>8} {dir_str:>4} {p:>5.3f} | {imm_dev:>+7.1f} {imm_win:>6} | {delay_str:>6} {conf_dev:>+7.1f} {conf_win:>6} | {dev_diff:>+9.1f} {match}")
