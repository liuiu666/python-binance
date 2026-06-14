"""
朴素回测 - 纯Python循环，零numpy技巧，可逐行验证。
对比向量化版本，检查是否有bug。
"""
import math
import pandas as pd

# ── 加载数据 ──
df = pd.read_csv("e:/python-binance/data/btcusdt_1m.csv")
df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
for c in ["open","high","low","close","volume"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")
df = df.dropna(subset=["open_time","close"]).sort_values("open_time").reset_index(drop=True)

close = df["close"].tolist()  # 用 list，不用 numpy

days = (df["open_time"].iloc[-1] - df["open_time"].iloc[0]).total_seconds() / 86400
N = len(close)
print(f"Data: {N} bars, {days:.1f} days")

PAYOUT = 0.80

def normal_cdf(x):
    """标准正态CDF，用math.erf实现"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def run_backtest(bar_min, tail_pct, window_min, cooldown_min, horizon_min=10):
    """朴素回测：纯循环，可逐行验证"""
    w = max(1, window_min // bar_min)      # 窗口K线数
    cd = max(0, cooldown_min // bar_min)   # 冷却K线数
    H = horizon_min // bar_min             # 预测K线数
    poc_thresh = 1.0 - tail_pct
    
    # 聚合K线（如果bar_min>1，取每bar_min根的最后一根close）
    bars = []
    for i in range(0, N, bar_min):
        bars.append(close[min(i + bar_min - 1, N - 1)])
    n = len(bars)
    
    # 预计算对数收益率
    lr = [0.0] * n
    for i in range(1, n):
        lr[i] = math.log(bars[i] / bars[i-1])
    
    wins = 0
    losses = 0
    flats = 0
    pnl = 0.0
    cum_pnl = 0.0
    peak = 0.0
    max_peak_dd = 0.0
    min_pnl = 0.0
    last_sig_bar = -999999
    max_win_streak = 0
    max_loss_streak = 0
    cur_win = 0
    cur_loss = 0
    trades_detail = []
    
    for i in range(w, n - H):
        # ── 1. 计算窗口内统计量 ──
        # 窗口: lr[i-w+1] ... lr[i]（最近w根K线的收益率）
        # lr[k] = log(bars[k]/bars[k-1]) 是第k根K线的收益率
        # 在第i根K线收盘后，可用的是 lr[1]...lr[i]
        # 取最后w根: lr[i-w+1]...lr[i]
        
        s = 0.0
        s2 = 0.0
        for k in range(i - w + 1, i + 1):
            s += lr[k]
            s2 += lr[k] * lr[k]
        
        mu = s / w
        var = (s2 / w) - mu * mu
        if var < 0:
            var = 0.0
        # 样本标准差（无偏）
        if w > 1:
            sigma = math.sqrt(var * w / (w - 1))
        else:
            sigma = math.sqrt(var)
        
        if sigma < 1e-12:
            continue
        
        # ── 2. 计算 z-score 和 p_up ──
        # z = H*mu / (sqrt(H)*sigma) = sqrt(H) * mu / sigma
        z = (H * mu) / (math.sqrt(H) * sigma)
        p_up = normal_cdf(z)
        
        # ── 3. 生成信号（反转策略）──
        # p_up <= tail_pct → 模型认为大概率跌 → 反着来，买UP
        # p_up >= poc_thresh → 模型认为大概率涨 → 反着来，买DOWN
        if p_up <= tail_pct:
            bet_up = True
        elif p_up >= poc_thresh:
            bet_up = False
        else:
            continue  # 无信号
        
        # ── 4. 冷却检查 ──
        actual_bar = i  # 在bar_min聚合K线中的位置
        if actual_bar - last_sig_bar < cd:
            continue
        
        # ── 5. 评估交易结果 ──
        entry_price = bars[i]
        exit_price = bars[i + H]
        
        if exit_price > entry_price:
            # 实际涨了
            correct = bet_up  # 买UP赢，买DOWN输
        elif exit_price < entry_price:
            # 实际跌了
            correct = not bet_up  # 买DOWN赢，买UP输
        else:
            # 平盘
            flats += 1
            continue
        
        last_sig_bar = actual_bar
        
        if correct:
            wins += 1
            pnl += PAYOUT
            cum_pnl += PAYOUT
            cur_win += 1
            cur_loss = 0
        else:
            losses += 1
            pnl -= 1.0
            cum_pnl -= 1.0
            cur_loss += 1
            cur_win = 0
        
        if cur_win > max_win_streak:
            max_win_streak = cur_win
        if cur_loss > max_loss_streak:
            max_loss_streak = cur_loss
        
        if cum_pnl < min_pnl:
            min_pnl = cum_pnl
        if cum_pnl > peak:
            peak = cum_pnl
        dd = peak - cum_pnl
        if dd > max_peak_dd:
            max_peak_dd = dd
        
        # 保存前几笔和最大回撤期的详细信息
        total_trades = wins + losses
        if total_trades <= 10 or total_trades % 5000 == 0:
            trades_detail.append({
                "trade": total_trades,
                "bar": i,
                "time": str(df["open_time"].iloc[min(i * bar_min, N-1)]),
                "entry": entry_price,
                "exit": exit_price,
                "mu": mu,
                "sigma": sigma,
                "z": z,
                "p_up": p_up,
                "bet": "UP" if bet_up else "DOWN",
                "correct": correct,
                "cum_pnl": cum_pnl,
            })
    
    total = wins + losses
    wr = wins / total * 100 if total > 0 else 0
    dd_pct = max_peak_dd / peak * 100 if peak > 0 else 999
    
    return {
        "trades": total, "wins": wins, "losses": losses, "flats": flats,
        "wr": wr, "pnl": pnl, "peak": peak, "max_peak_dd": max_peak_dd,
        "dd_pct": dd_pct, "min_pnl": min_pnl,
        "max_win": max_win_streak, "max_loss": max_loss_streak,
        "per_day": total / days, "pnl_day": pnl / days,
        "trades_detail": trades_detail,
    }

# ── 测试配置 ──
configs = [
    (1, 0.35, 120, 0),
    (1, 0.35, 120, 10),
    (1, 0.30, 60, 0),
    (1, 0.30, 60, 10),
    (1, 0.25, 60, 0),
    (1, 0.25, 60, 10),
    (1, 0.30, 120, 0),
    (1, 0.25, 120, 0),
]

print(f"\n{'='*130}")
print(f"{'bar':>3} | {'tail':>5} | {'win':>4} | {'cd':>4} | {'trades':>6} | {'/day':>6} | "
      f"{'WR':>6} | {'PNL':>9} | {'PNL/d':>7} | {'PeakDD':>8} | {'DD%':>5} | "
      f"{'MaxW':>5} | {'MaxL':>5} | {'MinP':>7}")
print("-" * 130)

results_map = {}
for bar_min, tail, win_min, cd_min in configs:
    r = run_backtest(bar_min, tail, win_min, cd_min)
    key = f"{bar_min}/{tail}/{win_min}/cd={cd_min}"
    results_map[key] = r
    ok = "✓" if r["wr"] > 55.56 else " "
    print(f"{bar_min:>3} | {tail:>5.2f} | {win_min:>4} | {cd_min:>4} | "
          f"{r['trades']:>6} | {r['per_day']:>6.1f} | {r['wr']:>5.1f}% | "
          f"{r['pnl']:>+9.1f} | {r['pnl_day']:>+6.2f} | "
          f"{r['max_peak_dd']:>8.1f} | {r['dd_pct']:>5.1f} | "
          f"{r['max_win']:>5} | {r['max_loss']:>5} | "
          f"{r['min_pnl']:>+7.1f} {ok}")

print(f"\n{'='*130}")

# ── 手动验证：打印第一笔交易详情 ──
key = "1/0.35/120/cd=0"
r = results_map[key]
print(f"\n=== 手动验证: {key} ===")
print(f"\n前10笔交易详情:")
print(f"  {'#':>3} {'bar':>6} {'time':>25} {'entry':>12} {'exit':>12} {'mu':>12} {'sigma':>12} {'z':>8} {'p_up':>6} {'bet':>5} {'ok':>4} {'cum':>8}")
for t in r["trades_detail"][:10]:
    print(f"  {t['trade']:>3} {t['bar']:>6} {t['time']:>25} {t['entry']:>12.1f} {t['exit']:>12.1f} "
          f"{t['mu']:>12.8f} {t['sigma']:>12.8f} {t['z']:>8.3f} {t['p_up']:>6.3f} "
          f"{t['bet']:>5} {'Y' if t['correct'] else 'N':>4} {t['cum_pnl']:>+8.2f}")

# ── 手动验证信号计算（第1笔交易）──
if r["trades_detail"]:
    t = r["trades_detail"][0]
    print(f"\n=== 手动验证第1笔交易的信号计算 ===")
    bar = t["bar"]
    print(f"信号K线: bar={bar}")
    print(f"窗口: lr[{bar-119}] ... lr[{bar}] (120根)")
    
    # 手动重算
    bars_close = close  # bar_min=1
    lr_list = []
    for k in range(bar - 119, bar + 1):
        ret = math.log(bars_close[k] / bars_close[k-1])
        lr_list.append(ret)
    
    n_w = len(lr_list)
    s_manual = sum(lr_list)
    s2_manual = sum(x*x for x in lr_list)
    mu_manual = s_manual / n_w
    var_manual = (s2_manual / n_w) - mu_manual**2
    sigma_manual = math.sqrt(var_manual * n_w / (n_w - 1))
    z_manual = (10 * mu_manual) / (math.sqrt(10) * sigma_manual)
    p_up_manual = normal_cdf(z_manual)
    
    print(f"手动计算: mu={mu_manual:.8f}, sigma={sigma_manual:.8f}, z={z_manual:.3f}, p_up={p_up_manual:.3f}")
    print(f"代码计算: mu={t['mu']:.8f}, sigma={t['sigma']:.8f}, z={t['z']:.3f}, p_up={t['p_up']:.3f}")
    match = abs(mu_manual - t['mu']) < 1e-10 and abs(sigma_manual - t['sigma']) < 1e-10
    print(f"{'✓ 匹配' if match else '✗ 不匹配！'}")

    # 验证交易结果
    entry = bars_close[bar]
    exit_p = bars_close[bar + 10]
    actual_up = exit_p > entry
    bet_up = p_up_manual <= 0.35
    correct = (actual_up and bet_up) or (not actual_up and not bet_up)
    print(f"\n交易验证: entry={entry:.1f}, exit={exit_p:.1f}, actual={'UP' if actual_up else 'DOWN'}, bet={'UP' if bet_up else 'DOWN'}, correct={correct}")
