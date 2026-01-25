import sys
from pathlib import Path
import pandas as pd
import time
from datetime import datetime

# Add project root to path
ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT))

from src.trading_skills.settings import Settings
from src.trading_skills.binance_client import create_client
from src.trading_skills.data_fetcher import FuturesDataFetcher
from src.analysis.smart_analyzer import analyze_symbol, save_individual_report

def get_target_symbols(client, mode='small', limit=20):
    """
    获取目标分析币种
    mode='hot': 热门币 (成交量前N)
    mode='small': 小市值/次热门 (成交量排名 30-60，且>5M成交额)
    mode='cheap': 便宜小币 (价格<5U, 成交量>5M, 排除前20热门)
    """
    try:
        tickers = client.futures_ticker()
        # Filter for USDT pairs ending in USDT
        usdt_tickers = [t for t in tickers if t['symbol'].endswith('USDT') and not t['symbol'].startswith('_')]
        # Sort by quote volume (volume in USDT)
        sorted_tickers = sorted(usdt_tickers, key=lambda x: float(x['quoteVolume']), reverse=True)
        
        if mode == 'hot':
            return [t['symbol'] for t in sorted_tickers[:limit]]
            
        elif mode == 'small':
            # 跳过前20个热门币，选取之后的币种
            start_rank = 20
            filtered = [
                t for t in sorted_tickers[start_rank:] 
                if float(t['quoteVolume']) > 5_000_000
            ]
            return [t['symbol'] for t in filtered[:limit]]
            
        elif mode == 'cheap':
            # 便宜小币: 价格低 + 排除超级热门 + 有一定流动性
            start_rank = 20
            filtered = [
                t for t in sorted_tickers[start_rank:] 
                if float(t['quoteVolume']) > 5_000_000 and float(t['lastPrice']) < 5.0
            ]
            return [t['symbol'] for t in filtered[:limit]]
            
    except Exception as e:
        print(f"获取币种失败: {e}")
        return []

def fetch_live_data(fetcher, symbol):
    """获取单个币种的实时分析所需数据"""
    data = {'symbol': symbol}
    
    try:
        # 1. 获取 1H K线 (用于趋势)
        klines_1h = fetcher.fetch_klines(symbol, '1h', limit=100)
        if not klines_1h.empty:
            data['klines_1h'] = klines_1h
            
        # 2. 获取 1m K线 (用于短期爆发和量能)
        klines_1m = fetcher.fetch_klines(symbol, '1m', limit=100)
        if not klines_1m.empty:
            data['klines_1m'] = klines_1m
            
        # 3. 获取盘口 (用于微观结构)
        ob = fetcher.fetch_order_book(symbol, limit=20)
        if ob:
            data['orderbook'] = ob
            
        # 4. 获取资金费率和溢价
        pi = fetcher.fetch_mark_price(symbol)
        if pi:
            data['premium_index'] = pi
            data['funding_rate'] = float(pi.get('lastFundingRate', 0)) # 直接从 mark_price 获取当前费率
            
        # 5. 获取持仓量历史 (用于OI分析)
        oi_hist = fetcher.fetch_open_interest_hist(symbol, period="5m", limit=30)
        if not oi_hist.empty:
            data['oi_hist'] = oi_hist
            
        data['timestamp'] = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        return data
    except Exception as e:
        print(f"  获取数据失败 {symbol}: {e}")
        return None

def main():
    print("启动实时智能选币 (Live Smart Scan)...")
    
    # 1. 初始化
    try:
        settings = Settings.load(ROOT)
        client = create_client(settings)
        fetcher = FuturesDataFetcher(client)
    except Exception as e:
        print(f"初始化失败: {e}")
        return

    # 2. 获取目标币种
    print("正在获取潜在机会币种 (便宜小币 <5U)...")
    symbols = get_target_symbols(client, mode='cheap', limit=15) # 扫描便宜小币
    if not symbols:
        print("未找到币种，退出。")
        return
        
    print(f"即将扫描: {', '.join(symbols)}\n")
    
    results = []
    
    # 3. 循环扫描
    for symbol in symbols:
        print(f"正在分析 {symbol}...", end="", flush=True)
        data = fetch_live_data(fetcher, symbol)
        
        if data:
            res = analyze_symbol(data)
            results.append(res)
            print(f" {res['score']}分 {'✅' if res['passed_screening'] else '❌'}")
            
            # 如果通过筛选，保存实时报告到 data/LIVE_SCAN 目录
            if res['passed_screening']:
                live_report_dir = ROOT / "data" / "LIVE_SCAN" / symbol
                live_report_dir.mkdir(parents=True, exist_ok=True)
                save_individual_report(res, live_report_dir)
        else:
            print(" 跳过 (数据获取失败)")
            
    # 4. 汇总报告
    results.sort(key=lambda x: x['score'], reverse=True)
    
    report = "# 实时智能选币报告 (Live Scan)\n\n"
    report += f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    report += f"扫描范围: 成交量前 {len(symbols)} 币种\n\n"
    
    report += "## 🏆 实时优选 (Top Picks)\n"
    valid_picks = [r for r in results if r['passed_screening']]
    
    if not valid_picks:
        report += "> ⚠️ 当前无符合「严选标准」的标的。\n\n"
        display_picks = results[:3]
    else:
        display_picks = valid_picks
        
    for r in display_picks:
        report += f"### {r['symbol']} (评分: {r['score']})\n"
        report += f"- **现价**: {r.get('current_price', 0)}\n"
        report += f"- **信号**: {', '.join(r['signals'])}\n"
        if r['risk_factors']:
            report += f"- **风险**: {', '.join(r['risk_factors'])}\n"
        
        # 简单建议
        price = r.get('current_price', 0)
        atr_pct = r.get('atr_pct', 1) / 100
        
        # 使用 direction_score 判断方向
        d_score = r.get('direction_score', 0)
        if d_score > 0:
            direction = "做多"
        elif d_score < 0:
            direction = "做空"
        else:
            direction = "做多" if r['score'] > 50 else "做空"
        
        if price:
            sl_dist = 2 * atr_pct
            tp_dist = 3 * atr_pct
            sl = price * (1 - sl_dist) if direction == "做多" else price * (1 + sl_dist)
            tp = price * (1 + tp_dist) if direction == "做多" else price * (1 - tp_dist)
            
            report += f"- **建议**: {direction} | 止损 {sl:.4f} | 止盈 {tp:.4f}\n"
        report += "\n"
        
    report += "## 📊 扫描概览\n"
    report += "| 标的 | 评分 | 状态 | 关键信号 |\n"
    report += "|---|---|---|---|\n"
    for r in results:
        status = "✅" if r['passed_screening'] else "❌"
        main_signal = r['signals'][0] if r['signals'] else "-"
        report += f"| {r['symbol']} | {r['score']} | {status} | {main_signal} |\n"
        
    out_file = ROOT / "data" / "LIVE_SCAN_REPORT.md"
    out_file.parent.mkdir(exist_ok=True)
    with open(out_file, 'w', encoding='utf-8') as f:
        f.write(report)
        
    print(f"\n报告已生成: {out_file}")

if __name__ == "__main__":
    main()
