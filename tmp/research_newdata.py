#!/usr/bin/env python3
"""
新数据全面重测 - 14小时秒级数据
检查秒级W=600策略是否仍然有效，以及发现新数据中的最佳策略
"""
import csv, json, math, sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import numpy as np
from scipy.stats import norm

DATA_FILE = "tmp/server_1s_trades.csv"
M1_FILE = "data/btcusdt_1m.csv"
PAYOUT = 0.80
BE = 1.0 / (1.0 + PAYOUT)  # 55.56%

# ============================================================
# Part 1: 数据概况
# ============================================================
def load_seconds():
    rows = []
    with open(DATA_FILE) as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    prices = []
    timestamps = []
    for r in rows:
        try:
            p = float(r['close'])
            ts = r['timestamp']
            prices.append(p)
            timestamps.append(ts)
        except:
            continue
    return np.array(prices), timestamps

def data_summary(prices, timestamps):
    print("="*70)
    print("Part 1: 新数据概况")
    print("="*70)
    print(f"总行数: {len(prices)}")
    print(f"时间范围: {timestamps[0]} → {timestamps[-1]}")
    
    # 解析时间
    t0 = datetime.fromisoformat(timestamps[0].replace('Z','+00:00'))
    t1 = datetime.fromisoformat(timestamps[-1].replace('Z','+00:00'))
    duration = (t1 - t0).total_seconds() / 3600
    print(f"持续时间: {duration:.1f} 小时")
    print(f"价格范围: {prices.min():.1f} - {prices.max():.1f}")
    print(f"价格均值: {prices.mean():.1f}")
    print(f"价格std: {prices.std():.1f}")
    
    # 计算每秒收益率
    rets = np.diff(prices)
    nonzero = rets[rets != 0]
    print(f"零变化秒数: {len(rets)-len(nonzero)}/{len(rets)} ({(len(rets)-len(nonzero))/len(rets)*100:.1f}%)")
    print(f"非零变化秒数: {len(nonzero)}")
    
    return duration

# ============================================================
# Part 2: 秒级POC策略全面测试
# ============================================================
def poc_backtest(prices, W, H, tail, cd, direction='rev'):
    """
    direction: 'rev'=均值回归(逆势), 'mom'=动量(顺势)
    """
    n = len(prices)
    signals = []
    last_signal_idx = -cd
    
    H_settle = 600  # 10分钟到期
    
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
                d = -1  # 逆势做空
            elif p_up < tail:
                d = 1   # 逆势做多
            else:
                continue
        else:  # mom
            if p_up > 1 - tail:
                d = 1   # 顺势做多
            elif p_up < tail:
                d = -1  # 顺势做空
            else:
                continue
        
        settle_idx = i + H_settle
        if settle_idx >= n:
            continue
        
        entry = prices[i]
        settle = prices[settle_idx]
        
        if d == 1:
            win = settle > entry
        else:
            win = settle < entry
        
        signals.append({
            'idx': i,
            'ts_idx': i,
            'direction': d,
            'p_up': p_up,
            'win': win,
            'entry': entry,
            'settle': settle
        })
        last_signal_idx = i
    
    return signals

def eval_signals(signals, label=""):
    n = len(signals)
    if n == 0:
        print(f"  {label}: 0信号")
        return None
    wins = sum(1 for s in signals if s['win'])
    wr = wins / n
    pnl = wins * PAYOUT - (n - wins) * 1.0
    
    # Wilson 95% CI
    if n > 0:
        z = 1.96
        denom = 1 + z*z/n
        center = (wr + z*z/(2*n)) / denom
        half = z * math.sqrt(wr*(1-wr)/n + z*z/(4*n*n)) / denom
        lo = center - half
        hi = center + half
    else:
        lo = hi = wr
    
    print(f"  {label}: {n}信号 WR={wr*100:.1f}% PNL={pnl:+.1f} CI=[{lo*100:.1f}%, {hi*100:.1f}%] {'★' if lo > BE else ''}")
    return {'n': n, 'wr': wr, 'pnl': pnl, 'lo': lo, 'hi': hi}

def test_seconds_strategies(prices):
    print("\n" + "="*70)
    print("Part 2: 秒级策略全面测试 (14小时新数据)")
    print("="*70)
    
    results = {}
    
    # 之前最优策略
    print("\n--- 之前最优策略验证 ---")
    for W, H, tail, label in [
        (600, 300, 0.20, "W=600 H=300 t=0.20 (之前71.4%)"),
        (600, 300, 0.15, "W=600 H=300 t=0.15"),
        (600, 300, 0.25, "W=600 H=300 t=0.25"),
        (600, 300, 0.10, "W=600 H=300 t=0.10"),
        (600, 120, 0.20, "W=600 H=120 t=0.20"),
        (600, 600, 0.20, "W=600 H=600 t=0.20"),
        (600, 60, 0.20, "W=600 H=60 t=0.20"),
    ]:
        cd = max(600, W)  # 确保独立性
        sigs_rev = poc_backtest(prices, W, H, tail, cd, 'rev')
        r = eval_signals(sigs_rev, f"REV {label}")
        if r:
            results[f"rev_{W}_{H}_{tail}"] = r
        
        sigs_mom = poc_backtest(prices, W, H, tail, cd, 'mom')
        r2 = eval_signals(sigs_mom, f"MOM {label}")
        if r2:
            results[f"mom_{W}_{H}_{tail}"] = r2
    
    # 扩展窗口搜索
    print("\n--- 扩展窗口搜索 ---")
    for W in [300, 600, 900, 1200, 1800, 2400, 3600]:
        for H in [60, 120, 300, 600]:
            if H > W:
                continue
            for tail in [0.10, 0.15, 0.20, 0.25]:
                cd = max(600, W)
                sigs = poc_backtest(prices, W, H, tail, cd, 'rev')
                if len(sigs) >= 10:
                    r = eval_signals(sigs, f"REV W={W} H={H} t={tail}")
                    results[f"rev_{W}_{H}_{tail}"] = r if r else results.get(f"rev_{W}_{H}_{tail}")
    
    # 找最优
    print("\n--- 秒级Top 10策略 ---")
    sorted_results = sorted(results.items(), key=lambda x: x[1]['lo'] if x[1] else 0, reverse=True)
    for i, (k, v) in enumerate(sorted_results[:10]):
        if v and v['n'] >= 10:
            print(f"  #{i+1} {k}: {v['n']}信号 WR={v['wr']*100:.1f}% CI=[{v['lo']*100:.1f}%, {v['hi']*100:.1f}%]")
    
    return results

# ============================================================
# Part 3: OOS验证 (前半 vs 后半)
# ============================================================
def test_oos(prices):
    print("\n" + "="*70)
    print("Part 3: OOS验证 (前7h vs 后7h)")
    print("="*70)
    
    mid = len(prices) // 2
    p_first = prices[:mid]
    p_second = prices[mid:]
    
    t0_first = "前半段"
    t0_second = "后半段"
    
    configs = [
        (600, 300, 0.20, 'rev'),
        (600, 300, 0.20, 'mom'),
        (600, 120, 0.20, 'rev'),
        (600, 120, 0.20, 'mom'),
        (300, 60, 0.20, 'rev'),
        (300, 60, 0.20, 'mom'),
        (900, 300, 0.15, 'rev'),
        (900, 300, 0.15, 'mom'),
    ]
    
    for W, H, tail, direction in configs:
        cd = max(600, W)
        s1 = poc_backtest(p_first, W, H, tail, cd, direction)
        s2 = poc_backtest(p_second, W, H, tail, cd, direction)
        
        n1, n2 = len(s1), len(s2)
        wr1 = sum(s['win'] for s in s1)/n1*100 if n1 > 0 else 0
        wr2 = sum(s['win'] for s in s2)/n2*100 if n2 > 0 else 0
        
        label = f"{direction.upper()} W={W} H={H} t={tail}"
        consistent = "✓一致" if (n1 >= 5 and n2 >= 5 and wr1 > BE*100 and wr2 > BE*100) else "✗不一致"
        print(f"  {label}:")
        print(f"    前半: {n1}信号 WR={wr1:.1f}%")
        print(f"    后半: {n2}信号 WR={wr2:.1f}%")
        print(f"    {consistent}")

# ============================================================
# Part 4: 秒级 vs 分钟级对比（同一时间窗口）
# ============================================================
def resample_to_1m(prices, timestamps):
    """将秒级数据重采样为1分钟OHLC"""
    bars = {}
    for i, ts in enumerate(timestamps):
        # 提取分钟
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        minute_key = dt.strftime('%Y-%m-%dT%H:%M')
        if minute_key not in bars:
            bars[minute_key] = {'open': prices[i], 'high': prices[i], 'low': prices[i], 'close': prices[i]}
        else:
            b = bars[minute_key]
            b['high'] = max(b['high'], prices[i])
            b['low'] = min(b['low'], prices[i])
            b['close'] = prices[i]
    
    sorted_keys = sorted(bars.keys())
    closes = np.array([bars[k]['close'] for k in sorted_keys])
    return closes, sorted_keys

def test_seconds_vs_minutes(prices, timestamps):
    print("\n" + "="*70)
    print("Part 4: 秒级 vs 分钟级对比（同一14小时窗口）")
    print("="*70)
    
    # 秒级测试
    print("\n--- 秒级策略 ---")
    cd = 600
    for W, H, tail in [(600, 300, 0.20), (600, 120, 0.20), (300, 60, 0.20)]:
        sigs_rev = poc_backtest(prices, W, H, tail, cd, 'rev')
        eval_signals(sigs_rev, f"秒级 REV W={W} H={H} t={tail}")
        sigs_mom = poc_backtest(prices, W, H, tail, cd, 'mom')
        eval_signals(sigs_mom, f"秒级 MOM W={W} H={H} t={tail}")
    
    # 重采样为1分钟
    m1_closes, m1_keys = resample_to_1m(prices, timestamps)
    print(f"\n--- 重采样1分钟K线: {len(m1_closes)}根 ---")
    
    # 分钟级测试（窗口单位为分钟）
    for W_m, H_m, tail in [
        (10, 5, 0.20),   # 等效 W=600s H=300s
        (10, 5, 0.15),
        (10, 3, 0.20),
        (5, 3, 0.20),
        (20, 10, 0.20),
        (60, 15, 0.10),  # 之前发现的最优分钟级策略
        (60, 10, 0.20),  # 高信号量版
    ]:
        W_s = W_m * 60
        H_s = H_m * 60
        cd_m = max(10, W_m)  # 分钟级cd
        H_settle_m = 10  # 10分钟到期
        
        signals = []
        last_sig = -cd_m
        n = len(m1_closes)
        for i in range(max(W_m, H_m), n - H_settle_m):
            if i - last_sig < cd_m:
                continue
            window = m1_closes[i-W_m:i]
            mu = window.mean()
            sigma = window.std()
            if sigma < 1e-10:
                continue
            hist = m1_closes[i-H_m:i]
            z = math.sqrt(H_m) * (hist.mean() - mu) / sigma
            if abs(z) < 1e-10:
                continue
            p_up = norm.cdf(z)
            
            if p_up > 1 - tail:
                d = -1  # rev
            elif p_up < tail:
                d = 1
            else:
                continue
            
            settle_idx = i + H_settle_m
            if settle_idx >= n:
                continue
            entry = m1_closes[i]
            settle = m1_closes[settle_idx]
            win = (settle > entry) if d == 1 else (settle < entry)
            signals.append(win)
        
        if len(signals) >= 5:
            wins = sum(signals)
            wr = wins / len(signals)
            pnl = wins * PAYOUT - (len(signals) - wins) * 1.0
            print(f"  分钟级 REV W={W_m}m H={H_m}m t={tail}: {len(signals)}信号 WR={wr*100:.1f}% PNL={pnl:+.1f}")

# ============================================================
# Part 5: 滚动胜率分析
# ============================================================
def test_rolling_wr(prices):
    print("\n" + "="*70)
    print("Part 5: 滚动胜率分析（每50信号窗口）")
    print("="*70)
    
    for W, H, tail in [(600, 300, 0.20), (600, 120, 0.20)]:
        cd = 600
        sigs = poc_backtest(prices, W, H, tail, cd, 'rev')
        n = len(sigs)
        if n < 20:
            print(f"  REV W={W} H={H} t={tail}: 信号不足({n})")
            continue
        
        wins = [1 if s['win'] else 0 for s in sigs]
        
        # 全局
        print(f"  REV W={W} H={H} t={tail}: 全局{n}信号 WR={sum(wins)/n*100:.1f}%")
        
        # 每50信号一段
        for start in range(0, n, 50):
            end = min(start + 50, n)
            segment = wins[start:end]
            seg_wr = sum(segment) / len(segment) * 100
            ts_idx = sigs[start]['ts_idx']
            print(f"    信号{start:4d}-{end:4d} (idx={ts_idx:5d}): WR={seg_wr:.1f}% ({len(segment)}信号)")
        
        # 按时间分段
        quarter = n // 4
        if quarter >= 5:
            print(f"    --- 按时间四分位 ---")
            for q in range(4):
                s = q * quarter
                e = (q + 1) * quarter if q < 3 else n
                seg = wins[s:e]
                print(f"    Q{q+1}: WR={sum(seg)/len(seg)*100:.1f}% ({len(seg)}信号)")

# ============================================================
# Part 6: 综合结论
# ============================================================
def final_conclusion(prices, duration, sec_results):
    print("\n" + "="*70)
    print("Part 6: 综合结论")
    print("="*70)
    
    print(f"\n数据量: {len(prices)} 秒级数据点, {duration:.1f} 小时")
    print(f"信号独立性cd: 600s")
    print(f"到期时间: 600s (10分钟)")
    print(f"盈亏平衡胜率: {BE*100:.2f}%")
    
    print(f"\n--- 秒级策略结果汇总 ---")
    
    # 检查之前最优策略
    key = "rev_600_300_0.20"
    if key in sec_results and sec_results[key]:
        r = sec_results[key]
        print(f"\n之前最优策略 REV W=600 H=300 t=0.20:")
        print(f"  新数据: {r['n']}信号 WR={r['wr']*100:.1f}%")
        print(f"  之前(5h): 21信号 WR=71.4%")
        if r['n'] >= 30:
            if r['lo'] > BE:
                print(f"  结论: ✓ 在更大样本上仍然有效！CI下界>{BE*100:.1f}%")
            else:
                print(f"  结论: ✗ 在更大样本上不再显著。CI下界<{BE*100:.1f}%")
        else:
            print(f"  样本量仍然不足({r['n']}信号)")
    
    # 找新数据中最优
    valid = {k: v for k, v in sec_results.items() if v and v['n'] >= 20}
    if valid:
        best = max(valid.items(), key=lambda x: x[1]['lo'])
        print(f"\n新数据中CI下界最高的策略:")
        print(f"  {best[0]}: {best[1]['n']}信号 WR={best[1]['wr']*100:.1f}% CI=[{best[1]['lo']*100:.1f}%, {best[1]['hi']*100:.1f}%]")
        if best[1]['lo'] > BE:
            print(f"  ★ CI下界 > 盈亏平衡点，统计显著！")
        else:
            print(f"  CI下界 < 盈亏平衡点，不显著")

# ============================================================
# Main
# ============================================================
if __name__ == '__main__':
    print("="*70)
    print("新数据全面重测 - 14小时秒级数据")
    print("数据源: tmp/server_1s_trades.csv")
    print("="*70)
    
    prices, timestamps = load_seconds()
    duration = data_summary(prices, timestamps)
    
    sec_results = test_seconds_strategies(prices)
    test_oos(prices)
    test_seconds_vs_minutes(prices, timestamps)
    test_rolling_wr(prices)
    final_conclusion(prices, duration, sec_results)
    
    # 保存结果
    output = {}
    for k, v in sec_results.items():
        if v:
            output[k] = v
    with open('tmp/research_newdata.json', 'w') as f:
        json.dump(output, f, indent=2, default=str)
    
    print("\n\n结果已保存到 tmp/research_newdata.json")
