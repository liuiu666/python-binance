"""
Smart Scan Analysis Tool
实现了 GENERAL_TRADING_GUIDE.md 中的通用筛选逻辑
"""
import sys
import json
from pathlib import Path
import pandas as pd
import numpy as np
from datetime import datetime

# 设置根目录
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

def load_latest_data(symbol_dir):
    """加载最新的 K 线和盘口数据 (跨多个时间戳目录查找)"""
    # 找到所有时间戳目录，按时间倒序排列
    time_dirs = sorted([d for d in symbol_dir.iterdir() if d.is_dir()], key=lambda x: x.name, reverse=True)
    if not time_dirs:
        return None
    
    data = {}
    data['symbol'] = symbol_dir.name
    
    # 需要查找的文件模式
    required_intervals = ['1m', '5m', '15m', '1h', '4h']
    
    # 1. 查找最新的 K 线数据
    for interval in required_intervals:
        key = f'klines_{interval}'
        fname = f"klines_{interval}.csv"
        
        # 遍历目录查找该 interval 的最新文件
        for d in time_dirs:
            fpath = d / fname
            if fpath.exists():
                try:
                    df = pd.read_csv(fpath)
                    if not df.empty:
                        data[key] = df
                        break # Found latest for this interval
                except Exception:
                    continue
    
    # 2. 查找最新的 Orderbook, Meta, Premium Index
    # 优先从最新的目录找
    for d in time_dirs:
        if 'orderbook' not in data and (d / "order_book.json").exists():
            try:
                with open(d / "order_book.json", 'r') as f:
                    data['orderbook'] = json.load(f)
            except: pass
        
        if 'premium_index' not in data and (d / "premium_index.json").exists():
            try:
                with open(d / "premium_index.json", 'r') as f:
                    data['premium_index'] = json.load(f)
            except: pass
            
        if 'meta' not in data and (d / "meta.json").exists():
            try:
                with open(d / "meta.json", 'r') as f:
                    data['meta'] = json.load(f)
            except: pass
                
        # 只要找到最新的即可，不需要遍历所有
        if 'orderbook' in data and 'premium_index' in data:
            break
            
    if time_dirs:
        data['timestamp'] = time_dirs[0].name
    
    return data



from src.analysis.smart_analyzer import analyze_symbol

def main():
    print("启动智能选币程序 (Smart Scan)...")
    print("基于策略: GENERAL_TRADING_GUIDE.md\n")
    
    results = []
    
    # 遍历数据目录
    for symbol_dir in DATA_DIR.iterdir():
        if symbol_dir.is_dir() and not symbol_dir.name.startswith("_"):
            try:
                data = load_latest_data(symbol_dir)
                if data:
                    res = analyze_symbol(data)
                    results.append(res)
                    save_individual_report(res, symbol_dir)
                    print(f"分析 {res['symbol']}: {res['score']}分 {'✅' if res['passed_screening'] else '❌'}")
            except Exception as e:
                print(f"跳过 {symbol_dir.name}: {e}")
                
    # 排序 (按偏离度排序, 越高/越低越好)
    results.sort(key=lambda x: abs(x['score'] - 50) if x.get('mode') == 'snapshot' else x['score'], reverse=True)
    
    # 生成报告内容
    report = "# 智能选币分析报告 (Smart Scan Result)\n\n"
    report += f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    report += "注意: 部分币种因缺失K线数据，仅使用盘口快照模式(Snapshot Mode)分析。\n\n"
    
    report += "## 🏆 优选标的 (Top Picks)\n"
    valid_picks = [r for r in results if r['passed_screening']]
    
    if not valid_picks:
        report += "> ⚠️ 当前市场无符合「严选标准」的标的，建议空仓观望。\n\n"
        # 选前3个分最高的展示
        display_picks = results[:3]
    else:
        display_picks = valid_picks
        
    for r in display_picks:
        mode_str = "(快照模式)" if r.get('mode') == 'snapshot' else ""
        report += f"### {r['symbol']} {mode_str} (评分: {r['score']})\n"
        report += f"- **当前价格**: {r.get('current_price', 0):.6f}\n"
        report += f"- **信号**: {', '.join(r['signals'])}\n"
        if r['risk_factors']:
            report += f"- **⚠️ 风险**: {', '.join(r['risk_factors'])}\n"
            
        # 建议参数
        price = r.get('current_price', 1)
        
        report += "- **执行建议**:\n"
        if r.get('mode') == 'snapshot':
            direction = "做多" if r['score'] > 50 else "做空"
            report += f"  - **方向**: {direction}\n"
            if direction == "做多":
                 report += f"  - **建议止损**: {price * 0.995:.6f} (-0.5%)\n"
                 report += f"  - **建议止盈**: {price * 1.01:.6f} (+1.0%)\n"
            else:
                 report += f"  - **建议止损**: {price * 1.005:.6f} (+0.5%)\n"
                 report += f"  - **建议止盈**: {price * 0.99:.6f} (-1.0%)\n"
        else:
            atr_pct = r.get('atr_pct', 1) / 100
            report += f"  - **止损位**: {price * (1 - 2*atr_pct):.6f} (多) / {price * (1 + 2*atr_pct):.6f} (空) (基于2ATR)\n"
            report += f"  - **止盈位**: {price * (1 + 3*atr_pct):.6f} (多) / {price * (1 - 3*atr_pct):.6f} (空)\n"
        report += "\n"
        
    report += "## 📊 全市场扫描概览\n"
    report += "| 标的 | 评分 | 模式 | 价差(bps) | 状态 | 关键信号 |\n"
    report += "|---|---|---|---|---|---|\n"
    for r in results:
        status = "✅ 推荐" if r['passed_screening'] else "❌ 观望"
        mode = "快照" if r.get('mode') == 'snapshot' else "完整"
        main_signal = r['signals'][0] if r['signals'] else "-"
        report += f"| {r['symbol']} | {r['score']} | {mode} | {r.get('spread_bps', 0):.1f} | {status} | {main_signal} |\n"

        
    # 保存
    out_file = DATA_DIR / "SMART_SCAN_REPORT.md"
    with open(out_file, 'w', encoding='utf-8') as f:
        f.write(report)
        
    print(f"\n报告已生成: {out_file}")

if __name__ == "__main__":
    main()
