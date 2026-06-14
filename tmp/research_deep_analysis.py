#!/usr/bin/env python3
"""
深度分析：降低cd + regime分析 + 扩展信号量
当cd=600s时只有76个信号，尝试cd=120/300来增加信号量
同时检查市场regime是否对策略有影响
"""
import csv, json, math
from datetime import datetime
import numpy as np
from scipy.stats import norm

DATA_FILE = "tmp/server_1s_trades.csv"
PAYOUT = 0.80
BE = 1.0 / (1.0 + PAYOUT)

def load_data():
    rows = []
    with open(DATA_FILE) as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                rows.append({
                    'price': float(r['close']),
                    'ts': r['timestamp'],
                    'vol': float(r.get('volume', 0)),
                    'trades': int(float(r.get('trades', 0))),
                    'tbs_ratio': float(r.get('taker_buy_sell_ratio', 1.0)),
                })
            except:
                continue
    return rows

def poc_backtest_v2(prices, W, H, tail, cd, H_settle=600, direction='rev'):
    n = len(prices)
    signals = []
    last_signal_idx = -cd
    
    for i in range(max(W, H), n - H_settle):
        if i - last_signal_idx < cd:
            continue
        
        window = prices[i-W:i]
        mu = window.mean()
        sigma = window.std()
        if sigma < 1e-10:
            continue
        
        hist = prices[i-H:i]
        z = math.sqrt(H) * (hist.mean() - mu) / sigma
        if abs(z) < 1e-10:
            continue
        p_up = norm.cdf(z)
        
        if direction == 'rev':
            if p_up > 1 - tail:
                d = -1
            elif p_up < tail:
                d = 1
            else:
                continue
        else:
            if p_up > 1 - tail:
                d = 1
            elif p_up < tail:
                d = -1
            else:
                continue
        
        settle_idx = i + H_settle
        if settle_idx >= n:
            continue
        
        entry = prices[i]
        settle = prices[settle_idx]
        win = (settle > entry) if d == 1 else (settle < entry)
        
        signals.append({'idx': i, 'dir': d, 'win': win, 'p_up': p_up, 
                       'entry': entry, 'settle': settle})
        last_signal_idx = i
    
    return signals

def eval_sig(signals, label=""):
    n = len(signals)
    if n == 0:
        return None
    wins = sum(1 for s in signals if s['win'])
    wr = wins / n
    pnl = wins * PAYOUT - (n - wins) * 1.0
    z = 1.96
    if n > 0:
        denom = 1 + z*z/n
        center = (wr + z*z/(2*n)) / denom
        half = z * math.sqrt(wr*(1-wr)/n + z*z/(4*n*n)) / denom
        lo, hi = center - half, center + half
    else:
        lo = hi = wr
    star = " ★" if lo > BE else ""
    print(f"  {label}: {n}信号 WR={wr*100:.1f}% PNL={pnl:+.1f} CI=[{lo*100:.1f}%, {hi*100:.1f}%]{star}")
    return {'n': n, 'wr': wr, 'pnl': pnl, 'lo': lo, 'hi': hi}

# ============================================================
# Part A: 降低cd增加信号量
# ============================================================
def test_lower_cd(prices):
    print("="*70)
    print("Part A: 降低cd增加信号量")
    print("cd=600s时只有76信号，尝试cd=120s和cd=300s")
    print("="*70)
    
    for cd in [120, 300, 600]:
        print(f"\n--- cd={cd}s ---")
        for W, H, tail in [(600, 300, 0.20), (600, 120, 0.20), (300, 60, 0.20), 
                           (900, 300, 0.15), (1200, 300, 0.15), (1800, 300, 0.15)]:
            for direction in ['rev', 'mom']:
                sigs = poc_backtest_v2(prices, W, H, tail, cd, 600, direction)
                if len(sigs) >= 20:
                    eval_sig(sigs, f"cd={cd} {direction.upper()} W={W} H={H} t={tail}")

# ============================================================
# Part B: 不同到期时间测试
# ============================================================
def test_different_expiries(prices):
    print("\n" + "="*70)
    print("Part B: 不同到期时间测试")
    print("除了10分钟(600s)，测试5min/15min/20min/30min到期")
    print("="*70)
    
    cd = 600  # 保持独立性
    
    for H_settle in [120, 300, 600, 900, 1200, 1800]:
        print(f"\n--- 到期={H_settle}s ({H_settle//60}min) ---")
        best_wr = 0
        best_config = None
        for W in [300, 600, 900, 1200]:
            for H in [60, 120, 300, 600]:
                if H > W:
                    continue
                for tail in [0.10, 0.15, 0.20, 0.25]:
                    for direction in ['rev', 'mom']:
                        sigs = poc_backtest_v2(prices, W, H, tail, cd, H_settle, direction)
                        if len(sigs) >= 20:
                            wins = sum(1 for s in sigs if s['win'])
                            wr = wins / len(sigs)
                            n = len(sigs)
                            z = 1.96
                            denom = 1 + z*z/n
                            center = (wr + z*z/(2*n)) / denom
                            half = z * math.sqrt(wr*(1-wr)/n + z*z/(4*n*n)) / denom
                            lo = center - half
                            if lo > BE and wr > best_wr:
                                best_wr = wr
                                best_config = (W, H, tail, direction, n, wr, lo)
        
        if best_config:
            W, H, tail, direction, n, wr, lo = best_config
            print(f"  最优: {direction.upper()} W={W} H={H} t={tail}: {n}信号 WR={wr*100:.1f}% CI_lo={lo*100:.1f}% ★")
        else:
            print(f"  无显著策略")

# ============================================================
# Part C: Regime分析
# ============================================================
def test_regime(rows):
    print("\n" + "="*70)
    print("Part C: Regime分析")
    print("按波动率/成交量分段检查策略表现")
    print("="*70)
    
    prices = np.array([r['price'] for r in rows])
    vols = np.array([r['vol'] for r in rows])
    n_trades = np.array([r['trades'] for r in rows])
    
    n = len(prices)
    
    # 计算滚动波动率（每5分钟）
    window = 300
    rolling_vol = np.zeros(n)
    for i in range(window, n):
        rolling_vol[i] = np.std(prices[i-window:i])
    
    # 分段：低波动 vs 高波动
    vol_median = np.median(rolling_vol[rolling_vol > 0])
    print(f"\n波动率中位数: {vol_median:.2f}")
    
    # 低波动时段
    low_vol_mask = rolling_vol < vol_median * 0.5
    high_vol_mask = rolling_vol > vol_median * 2.0
    
    low_vol_prices = prices[low_vol_mask]
    high_vol_prices = prices[high_vol_mask]
    
    print(f"低波动时段: {np.sum(low_vol_mask)}秒 ({np.sum(low_vol_mask)/n*100:.1f}%)")
    print(f"高波动时段: {np.sum(high_vol_mask)}秒 ({np.sum(high_vol_mask)/n*100:.1f}%)")
    
    # 但是这样切片会破坏时间连续性，不能直接用
    # 改用时段划分
    
    # 按时间分段（每2小时一段）
    print("\n--- 按2小时时段分析 ---")
    t0 = datetime.fromisoformat(rows[0]['ts'].replace('Z', '+00:00'))
    
    segments = {}
    for i, r in enumerate(rows):
        dt = datetime.fromisoformat(r['ts'].replace('Z', '+00:00'))
        hours_since_start = (dt - t0).total_seconds() / 3600
        seg = int(hours_since_start // 2)
        if seg not in segments:
            segments[seg] = []
        segments[seg].append(i)
    
    for seg_id in sorted(segments.keys()):
        indices = segments[seg_id]
        if len(indices) < 600:
            continue
        
        seg_prices = prices[indices]
        seg_vol = np.mean(vols[indices])
        seg_trades = np.mean(n_trades[indices])
        
        # 计算这个时段的收益特征
        rets = np.diff(seg_prices)
        nonzero = rets[rets != 0]
        up_pct = np.sum(nonzero > 0) / len(nonzero) * 100 if len(nonzero) > 0 else 50
        
        # 检查动量/反转
        if len(nonzero) > 10:
            # lag1自相关
            if len(nonzero) > 2:
                ac1 = np.corrcoef(nonzero[:-1], nonzero[1:])[0, 1]
            else:
                ac1 = 0
        else:
            ac1 = 0
        
        start_idx = indices[0]
        end_idx = indices[-1]
        start_ts = rows[start_idx]['ts'][11:19]
        end_ts = rows[end_idx]['ts'][11:19]
        
        print(f"  Seg{seg_id} ({start_ts}→{end_ts}): {len(indices)}s "
              f"价格={seg_prices[0]:.0f}→{seg_prices[-1]:.0f} "
              f"avg_vol={seg_vol:.2f} avg_trades={seg_trades:.0f} "
              f"up%={up_pct:.1f}% AC1={ac1:+.4f} {'(动量)' if ac1 > 0.02 else '(均值回归)' if ac1 < -0.02 else '(随机)'}")

# ============================================================
# Part D: 尝试不同信号类型（非POC）
# ============================================================
def test_alternative_signals(prices):
    print("\n" + "="*70)
    print("Part D: 替代信号类型测试")
    print("="*70)
    
    n = len(prices)
    H_settle = 600
    cd = 600
    
    # 1. RSI信号
    print("\n--- RSI信号 ---")
    for rsi_period in [60, 120, 300]:
        for rsi_thresh in [20, 25, 30, 35]:
            signals_rev = []
            signals_mom = []
            last_sig = -cd
            
            for i in range(rsi_period, n - H_settle):
                if i - last_sig < cd:
                    continue
                
                window = prices[i-rsi_period:i]
                deltas = np.diff(window)
                gains = np.where(deltas > 0, deltas, 0)
                losses = np.where(deltas < 0, -deltas, 0)
                avg_gain = gains.mean()
                avg_loss = losses.mean()
                
                if avg_loss < 1e-10:
                    rsi = 100
                else:
                    rs = avg_gain / avg_loss
                    rsi = 100 - 100 / (1 + rs)
                
                settle_idx = i + H_settle
                if settle_idx >= n:
                    continue
                
                entry = prices[i]
                settle = prices[settle_idx]
                
                # 超卖买入（反转）
                if rsi < rsi_thresh:
                    win = settle > entry
                    signals_rev.append(win)
                    last_sig = i
                # 超买卖出
                elif rsi > 100 - rsi_thresh:
                    win = settle < entry
                    signals_rev.append(win)
                    last_sig = i
            
            if len(signals_rev) >= 10:
                wins = sum(signals_rev)
                wr = wins / len(signals_rev) * 100
                pnl = wins * PAYOUT - (len(signals_rev) - wins) * 1.0
                print(f"  RSI({rsi_period}) thresh={rsi_thresh}: {len(signals_rev)}信号 WR={wr:.1f}% PNL={pnl:+.1f}")
    
    # 2. 布林带信号
    print("\n--- 布林带信号 ---")
    for bb_period in [60, 120, 300, 600]:
        for bb_std in [1.5, 2.0, 2.5]:
            signals = []
            last_sig = -cd
            
            for i in range(bb_period, n - H_settle):
                if i - last_sig < cd:
                    continue
                
                window = prices[i-bb_period:i]
                mu = window.mean()
                sigma = window.std()
                
                if sigma < 1e-10:
                    continue
                
                upper = mu + bb_std * sigma
                lower = mu - bb_std * sigma
                
                settle_idx = i + H_settle
                if settle_idx >= n:
                    continue
                
                entry = prices[i]
                settle = prices[settle_idx]
                
                if entry > upper:  # 突破上轨 → 做空（反转）
                    win = settle < entry
                    signals.append(win)
                    last_sig = i
                elif entry < lower:  # 突破下轨 → 做多（反转）
                    win = settle > entry
                    signals.append(win)
                    last_sig = i
            
            if len(signals) >= 10:
                wins = sum(signals)
                wr = wins / len(signals) * 100
                pnl = wins * PAYOUT - (len(signals) - wins) * 1.0
                print(f"  BB({bb_period}, {bb_std}σ): {len(signals)}信号 WR={wr:.1f}% PNL={pnl:+.1f}")
    
    # 3. 纯动量/趋势信号
    print("\n--- 动量信号（价格变化方向） ---")
    for mom_period in [60, 120, 300, 600]:
        signals_mom = []
        signals_rev = []
        last_sig = -cd
        
        for i in range(mom_period, n - H_settle):
            if i - last_sig < cd:
                continue
            
            change = prices[i] - prices[i - mom_period]
            if abs(change) < 1e-10:
                continue
            
            settle_idx = i + H_settle
            if settle_idx >= n:
                continue
            
            entry = prices[i]
            settle = prices[settle_idx]
            
            # 动量：同方向
            if change > 0:
                win_mom = settle > entry
                win_rev = settle < entry
            else:
                win_mom = settle < entry
                win_rev = settle > entry
            
            signals_mom.append(win_mom)
            signals_rev.append(win_rev)
            last_sig = i
        
        if len(signals_mom) >= 20:
            wins_mom = sum(signals_mom)
            wr_mom = wins_mom / len(signals_mom) * 100
            wins_rev = sum(signals_rev)
            wr_rev = wins_rev / len(signals_rev) * 100
            print(f"  动量({mom_period}s): {len(signals_mom)}信号 MOM_WR={wr_mom:.1f}% REV_WR={wr_rev:.1f}%")

# ============================================================
# Part E: 综合判定
# ============================================================
def final_verdict(rows):
    print("\n" + "="*70)
    print("Part E: 综合判定")
    print("="*70)
    
    prices = np.array([r['price'] for r in rows])
    n = len(prices)
    
    print(f"\n数据: {n}个秒级数据点 ({n/3600:.1f}小时)")
    print(f"价格: {prices[0]:.1f} → {prices[-1]:.1f}")
    print(f"盈亏平衡胜率: {BE*100:.2f}%")
    
    # 检查原始统计
    rets = np.diff(prices)
    nonzero = rets[rets != 0]
    if len(nonzero) > 2:
        ac1 = np.corrcoef(nonzero[:-1], nonzero[1:])[0, 1]
        print(f"\n秒级收益率AC1: {ac1:+.4f}")
        if ac1 > 0.02:
            print("  → 有微弱动量效应")
        elif ac1 < -0.02:
            print("  → 有微弱均值回归效应")
        else:
            print("  → 接近随机游走")
    
    # 检查价格是否在10分钟后倾向回归
    H_settle = 600
    forward_returns = []
    for i in range(0, n - H_settle, 60):  # 每60秒采样一次
        fr = prices[i + H_settle] - prices[i]
        forward_returns.append(fr)
    
    forward_returns = np.array(forward_returns)
    mean_fr = forward_returns.mean()
    std_fr = forward_returns.std()
    print(f"\n10分钟后价格变化: mean={mean_fr:+.2f} std={std_fr:.2f}")
    up_pct = np.sum(forward_returns > 0) / len(forward_returns) * 100
    print(f"10分钟后上涨概率: {up_pct:.1f}%")
    
    # 检查大幅移动后的回归
    print("\n--- 大幅移动后10分钟表现 ---")
    for threshold_pct in [0.01, 0.02, 0.05, 0.10]:
        threshold = std_fr * threshold_pct
        # 找大幅上涨和大幅下跌
        big_up = forward_returns[forward_returns > threshold]
        big_down = forward_returns[forward_returns < -threshold]
        
        # 这些大幅移动之后的下一个10分钟
        next_returns_up = []
        next_returns_down = []
        for i in range(0, len(forward_returns) - 1):
            if forward_returns[i] > threshold:
                next_returns_up.append(forward_returns[i+1])
            elif forward_returns[i] < -threshold:
                next_returns_down.append(forward_returns[i+1])
        
        if next_returns_up:
            nru = np.array(next_returns_up)
            rev_up = np.sum(nru < 0) / len(nru) * 100
            print(f"  阈值={threshold_pct}σ: "
                  f"大涨后({len(nru)}次) → 下个10min继续跌={rev_up:.1f}% "
                  f"(均值回归={rev_up:.1f}%)", end="")
            if rev_up > 55:
                print(" ★有回归效应")
            else:
                print()
        if next_returns_down:
            nrd = np.array(next_returns_down)
            rev_down = np.sum(nrd > 0) / len(nrd) * 100
            print(f"  阈值={threshold_pct}σ: "
                  f"大跌后({len(nrd)}次) → 下个10min继续涨={rev_down:.1f}%", end="")
            if rev_down > 55:
                print(" ★有回归效应")
            else:
                print()

if __name__ == '__main__':
    rows = load_data()
    prices = np.array([r['price'] for r in rows])
    
    test_lower_cd(prices)
    test_different_expiries(prices)
    test_regime(rows)
    test_alternative_signals(prices)
    final_verdict(rows)
    
    print("\n\n深度分析完成。")
