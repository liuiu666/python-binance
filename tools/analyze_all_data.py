"""
综合数据分析工具 - 寻找最高收益操作策略
分析所有币种数据,识别最佳交易机会,生成详细操作报告
"""
import sys
from pathlib import Path
import pandas as pd
import numpy as np
from datetime import datetime
import json

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

def load_symbol_data(symbol_dir):
    """加载单个币种的所有数据快照并合并"""
    # 获取所有时间戳目录
    time_dirs = [d for d in symbol_dir.iterdir() if d.is_dir()]
    if not time_dirs:
        print(f"    - 未找到时间戳目录")
        return None
    
    # 按时间排序
    time_dirs = sorted(time_dirs, key=lambda x: x.name)
    
    print(f"    - 找到 {len(time_dirs)} 个时间目录")
    
    # 收集所有数据
    all_klines = {}  # {interval: dataframe}
    funding_rates = []
    orderbook_data = None
    meta_data = None
    
    for time_dir in time_dirs:
        try:
            # 读取meta
            meta_file = time_dir / "meta.json"
            if meta_file.exists() and meta_data is None:
                with open(meta_file, 'r') as f:
                    meta_data = json.load(f)
            
            # 读取K线(检查各种周期)
            for interval in ['1m', '5m', '15m', '1h', '4h']:
                klines_file = time_dir / f"klines_{interval}.csv"
                if klines_file.exists() and interval not in all_klines:
                    df = pd.read_csv(klines_file)
                    if not df.empty:
                        all_klines[interval] = df
                        print(f"    - 加载 klines_{interval}.csv ({len(df)}行)")
            
            # 读取资金费率
            funding_file = time_dir / "funding_rate.csv"
            if funding_file.exists():
                df_funding = pd.read_csv(funding_file)
                if not df_funding.empty:
                    funding_rates.append(df_funding['fundingRate'].iloc[-1])
            
            # 读取订单簿(只需要一份最新的)
            if orderbook_data is None:
                orderbook_file = time_dir / "order_book.json"
                if orderbook_file.exists():
                    with open(orderbook_file, 'r') as f:
                        orderbook_data = json.load(f)
        
        except Exception as e:
            print(f"    - 读取 {time_dir.name} 出错: {e}")
            continue
    
    if not all_klines or not meta_data:
        print(f"    - 数据不完整")
        return None
    
    # 使用最小周期的K线数据(优先1m,其次5m,15m,1h,4h)
    for interval in ['1m', '5m', '15m', '1h', '4h']:
        if interval in all_klines:
            df_klines = all_klines[interval]
            print(f"    - 使用 {interval} 周期K线进行分析")
            break
    else:
        print(f"    - 没有找到可用的K线数据")
        return None
    
    # 计算资金费率
    funding_rate = None
    if funding_rates:
        funding_rate = funding_rates[-1]  # 使用最新的
    
    # 处理订单簿
    orderbook_imbalance = None
    bid_ask_spread = None
    if orderbook_data and 'bids' in orderbook_data and 'asks' in orderbook_data:
        if orderbook_data['bids'] and orderbook_data['asks']:
            best_bid = float(orderbook_data['bids'][0][0])
            best_ask = float(orderbook_data['asks'][0][0])
            bid_ask_spread = (best_ask - best_bid) / best_bid * 10000  # bps
            
            # 计算订单簿失衡
            bid_volume = sum(float(b[1]) for b in orderbook_data['bids'][:20])
            ask_volume = sum(float(a[1]) for a in orderbook_data['asks'][:20])
            if bid_volume + ask_volume > 0:
                orderbook_imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume) * 100
    
    return {
        'symbol': meta_data['symbol'],
        'timestamp': time_dirs[-1].name,  # 最新的时间戳
        'df_klines': df_klines,
        'funding_rate': funding_rate,
        'orderbook_imbalance': orderbook_imbalance,
        'bid_ask_spread': bid_ask_spread,
        'open_interest': meta_data.get('open_interest', 0)
    }

def calculate_metrics(data):
    """计算关键交易指标"""
    df = data['df_klines']
    
    if df.empty or len(df) < 50:
        return None
    
    # 价格指标
    current_price = df['close'].iloc[-1]
    price_change_1h = (df['close'].iloc[-1] / df['close'].iloc[-60] - 1) * 100 if len(df) >= 60 else 0
    price_change_4h = (df['close'].iloc[-1] / df['close'].iloc[-240] - 1) * 100 if len(df) >= 240 else 0
    
    # 波动率(标准差)
    volatility = df['close'].iloc[-60:].pct_change().std() * 100 if len(df) >= 60 else 0
    
    # 成交量指标
    avg_volume_1h = df['quote_volume'].iloc[-60:].mean() if len(df) >= 60 else 0
    volume_surge = df['quote_volume'].iloc[-10:].mean() / avg_volume_1h if avg_volume_1h > 0 else 1
    
    # 资金流向指标
    if '资金净流入_估算' in df.columns:
        net_inflow_1h = df['资金净流入_估算'].iloc[-60:].sum() if len(df) >= 60 else 0
        net_inflow_recent = df['资金净流入_估算'].iloc[-10:].sum()
    else:
        net_inflow_1h = 0
        net_inflow_recent = 0
    
    # 趋势强度(使用简单移动平均)
    if len(df) >= 20:
        ma5 = df['close'].iloc[-5:].mean()
        ma20 = df['close'].iloc[-20:].mean()
        trend_strength = (ma5 / ma20 - 1) * 100
    else:
        trend_strength = 0
    
    # 动量指标(RSI简化版)
    if len(df) >= 14:
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).iloc[-14:].mean()
        loss = -delta.where(delta < 0, 0).iloc[-14:].mean()
        rs = gain / loss if loss != 0 else 100
        rsi = 100 - (100 / (1 + rs))
    else:
        rsi = 50
    
    return {
        'symbol': data['symbol'],
        'current_price': current_price,
        'price_change_1h': price_change_1h,
        'price_change_4h': price_change_4h,
        'volatility': volatility,
        'volume_surge': volume_surge,
        'avg_volume_1h': avg_volume_1h,
        'net_inflow_1h': net_inflow_1h,
        'net_inflow_recent': net_inflow_recent,
        'trend_strength': trend_strength,
        'rsi': rsi,
        'funding_rate': data['funding_rate'] or 0,
        'orderbook_imbalance': data['orderbook_imbalance'] or 0,
        'bid_ask_spread': data['bid_ask_spread'] or 0,
        'open_interest': data['open_interest'],
        'timestamp': data['timestamp']
    }

def score_opportunity(metrics):
    """评分交易机会(0-100分)"""
    score = 0
    signals = []
    
    # 1. 趋势强度(最高30分)
    if abs(metrics['trend_strength']) > 2:
        score += 30
        signals.append(f"强趋势({metrics['trend_strength']:.2f}%)")
    elif abs(metrics['trend_strength']) > 1:
        score += 20
        signals.append(f"中等趋势({metrics['trend_strength']:.2f}%)")
    elif abs(metrics['trend_strength']) > 0.5:
        score += 10
        signals.append(f"弱趋势({metrics['trend_strength']:.2f}%)")
    
    # 2. 资金流向(最高25分)
    if metrics['net_inflow_recent'] > 0 and metrics['net_inflow_1h'] > 0:
        score += 25
        signals.append("资金持续流入")
    elif metrics['net_inflow_recent'] > 0:
        score += 15
        signals.append("资金近期流入")
    elif metrics['net_inflow_recent'] < 0 and metrics['net_inflow_1h'] < 0:
        score += 10  # 持续流出也是做空机会
        signals.append("资金持续流出")
    
    # 3. 成交量异动(最高20分)
    if metrics['volume_surge'] > 2:
        score += 20
        signals.append(f"成交量激增({metrics['volume_surge']:.1f}x)")
    elif metrics['volume_surge'] > 1.5:
        score += 15
        signals.append(f"成交量增加({metrics['volume_surge']:.1f}x)")
    elif metrics['volume_surge'] > 1.2:
        score += 10
        signals.append(f"成交量温和增长({metrics['volume_surge']:.1f}x)")
    
    # 4. RSI指标(最高15分)
    if metrics['rsi'] < 30:
        score += 15
        signals.append(f"RSI超卖({metrics['rsi']:.1f})")
    elif metrics['rsi'] > 70:
        score += 15
        signals.append(f"RSI超买({metrics['rsi']:.1f})")
    elif 30 <= metrics['rsi'] <= 45:
        score += 8
        signals.append(f"RSI偏低({metrics['rsi']:.1f})")
    elif 55 <= metrics['rsi'] <= 70:
        score += 8
        signals.append(f"RSI偏高({metrics['rsi']:.1f})")
    
    # 5. 订单簿失衡(最高10分)
    if abs(metrics['orderbook_imbalance']) > 20:
        score += 10
        signals.append(f"订单簿严重失衡({metrics['orderbook_imbalance']:.1f}%)")
    elif abs(metrics['orderbook_imbalance']) > 10:
        score += 5
        signals.append(f"订单簿失衡({metrics['orderbook_imbalance']:.1f}%)")
    
    # 判断方向
    direction = "观望"
    if metrics['trend_strength'] > 0 and metrics['net_inflow_recent'] > 0:
        direction = "做多"
    elif metrics['trend_strength'] < 0 and metrics['net_inflow_recent'] < 0:
        direction = "做空"
    elif metrics['rsi'] < 30:
        direction = "做多(超卖反弹)"
    elif metrics['rsi'] > 70:
        direction = "做空(超买回调)"
    
    return score, direction, signals

def generate_symbol_report(metrics, score, direction, signals, output_dir):
    """生成单个币种的详细操作报告"""
    report = f"""# {metrics['symbol']} 操作分析报告

## 综合评估
- **机会评分**: {score}/100
- **操作方向**: {direction}
- **当前价格**: {metrics['current_price']:.6f}
- **分析时间**: {metrics['timestamp']}

## 关键信号
"""
    for signal in signals:
        report += f"- {signal}\n"
    
    report += f"""
## 详细指标

### 价格表现
- 1小时涨跌: {metrics['price_change_1h']:.2f}%
- 4小时涨跌: {metrics['price_change_4h']:.2f}%
- 波动率: {metrics['volatility']:.2f}%

### 资金分析
- 1小时资金净流入: {metrics['net_inflow_1h']:,.0f}
- 近期资金净流入: {metrics['net_inflow_recent']:,.0f}
- 资金费率: {metrics['funding_rate']:.6f}

### 市场结构
- RSI指标: {metrics['rsi']:.1f}
- 趋势强度: {metrics['trend_strength']:.2f}%
- 成交量倍数: {metrics['volume_surge']:.2f}x
- 订单簿失衡: {metrics['orderbook_imbalance']:.2f}%
- 买卖价差: {metrics['bid_ask_spread']:.2f} bps

### 持仓数据
- 持仓量: {metrics['open_interest']:,.0f}

## 操作建议

"""
    
    if direction == "做多":
        report += f"""### 做多策略
1. **入场点位**: 
   - 激进: 市价入场 {metrics['current_price']:.6f}
   - 稳健: 回调至 {metrics['current_price'] * 0.995:.6f} 附近挂单
   
2. **止损设置**: 
   - 建议止损: {metrics['current_price'] * 0.98:.6f} (-2%)
   - 激进止损: {metrics['current_price'] * 0.985:.6f} (-1.5%)
   
3. **止盈目标**:
   - 第一目标: {metrics['current_price'] * 1.02:.6f} (+2%)
   - 第二目标: {metrics['current_price'] * 1.03:.6f} (+3%)
   - 第三目标: {metrics['current_price'] * 1.05:.6f} (+5%)
   
4. **仓位建议**: 根据账户风险承受能力,建议使用5-10倍杠杆,单笔风险不超过总资金的1-2%
"""
    elif direction == "做空":
        report += f"""### 做空策略
1. **入场点位**: 
   - 激进: 市价入场 {metrics['current_price']:.6f}
   - 稳健: 反弹至 {metrics['current_price'] * 1.005:.6f} 附近挂单
   
2. **止损设置**: 
   - 建议止损: {metrics['current_price'] * 1.02:.6f} (+2%)
   - 激进止损: {metrics['current_price'] * 1.015:.6f} (+1.5%)
   
3. **止盈目标**:
   - 第一目标: {metrics['current_price'] * 0.98:.6f} (-2%)
   - 第二目标: {metrics['current_price'] * 0.97:.6f} (-3%)
   - 第三目标: {metrics['current_price'] * 0.95:.6f} (-5%)
   
4. **仓位建议**: 根据账户风险承受能力,建议使用5-10倍杠杆,单笔风险不超过总资金的1-2%
"""
    else:
        report += """### 观望建议
当前市场信号不明确,建议:
1. 继续观察,等待更明确的信号
2. 如有持仓,可以考虑减仓或平仓
3. 关注关键支撑位和压力位的突破情况
"""
    
    report += f"""
## 风险提示
1. 市场波动较大,严格执行止损
2. 控制仓位,避免过度杠杆
3. 关注市场整体走势和突发新闻
4. 本报告仅供参考,不构成投资建议

---
*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
"""
    
    # 保存报告
    output_file = output_dir / f"analysis_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(report)
    
    print(f"  [保存] 报告: {output_file.name}")
    
    return report

def generate_summary_report(all_results):
    """生成总体汇总报告"""
    if not all_results:
        return "# 分析报告\n\n未找到有效数据,请检查数据文件是否完整。"
    
    df = pd.DataFrame(all_results)
    df = df.sort_values('score', ascending=False)
    
    summary = f"""# 量化交易综合分析报告

## 分析概况
- **分析时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- **币种数量**: {len(df)}
- **有效信号**: {len(df[df['score'] >= 60])} 个

## 最佳交易机会 TOP 5

"""
    
    for idx, row in df.head(5).iterrows():
        summary += f"""### {idx+1}. {row['symbol']} (评分: {row['score']}/100)
- **操作方向**: {row['direction']}
- **当前价格**: {row['current_price']:.6f}
- **1小时涨跌**: {row['price_change_1h']:+.2f}%
- **RSI**: {row['rsi']:.1f}
- **成交量倍数**: {row['volume_surge']:.2f}x
- **关键信号**: {', '.join(row['signals'][:3])}

"""
    
    summary += """## 全币种评分排名

| 排名 | 币种 | 评分 | 方向 | 1H涨跌 | RSI | 成交量 | 关键信号 |
|------|------|------|------|--------|-----|--------|----------|
"""
    
    for idx, row in df.iterrows():
        summary += f"| {idx+1} | {row['symbol']} | {row['score']} | {row['direction']} | {row['price_change_1h']:+.2f}% | {row['rsi']:.0f} | {row['volume_surge']:.1f}x | {row['signals'][0] if row['signals'] else '-'} |\n"
    
    summary += f"""
## 市场整体观察

### 做多机会
"""
    long_opps = df[df['direction'].str.contains('多')]
    if not long_opps.empty:
        summary += f"共发现 {len(long_opps)} 个做多机会:\n"
        for _, row in long_opps.head(3).iterrows():
            summary += f"- **{row['symbol']}** (评分{row['score']}): {', '.join(row['signals'][:2])}\n"
    else:
        summary += "当前无明显做多机会\n"
    
    summary += f"""
### 做空机会
"""
    short_opps = df[df['direction'].str.contains('空')]
    if not short_opps.empty:
        summary += f"共发现 {len(short_opps)} 个做空机会:\n"
        for _, row in short_opps.head(3).iterrows():
            summary += f"- **{row['symbol']}** (评分{row['score']}): {', '.join(row['signals'][:2])}\n"
    else:
        summary += "当前无明显做空机会\n"
    
    summary += """
## 操作建议

### 优先级排序
1. **高分机会** (80分以上): 重点关注,可以积极布局
2. **中等机会** (60-80分): 适度参与,控制仓位
3. **低分机会** (60分以下): 观望为主,谨慎操作

### 风险管理
1. 分散投资,不要将所有资金投入单一币种
2. 严格执行止损,单笔风险控制在1-2%
3. 合理使用杠杆,建议5-10倍
4. 关注市场整体情绪和宏观因素

### 实战步骤
1. 从TOP 5中选择2-3个最符合自己风险偏好的标的
2. 根据各币种报告中的入场点位设置挂单
3. 设置好止损和止盈订单
4. 持续跟踪,动态调整

---
*本报告基于技术分析和量化模型生成,仅供参考,不构成投资建议*
"""
    
    return summary

def main():
    print("="*60)
    print("量化交易数据综合分析工具")
    print("="*60)
    print()
    
    # 获取所有币种目录
    symbol_dirs = [d for d in DATA_DIR.iterdir() if d.is_dir() and not d.name.startswith('_')]
    
    if not symbol_dirs:
        print("未找到任何币种数据!")
        return
    
    print(f"找到 {len(symbol_dirs)} 个币种数据,开始分析...\n")
    
    all_results = []
    
    for symbol_dir in sorted(symbol_dirs):
        print(f"分析 {symbol_dir.name}...")
        
        # 加载数据
        data = load_symbol_data(symbol_dir)
        if data is None:
            print(f"  [跳过] 数据不完整")
            continue
        
        # 计算指标
        metrics = calculate_metrics(data)
        if metrics is None:
            print(f"  [跳过] 数据不足")
            continue
        
        # 评分
        score, direction, signals = score_opportunity(metrics)
        
        print(f"  [完成] 评分: {score}/100, 方向: {direction}")
        
        # 生成单个币种报告
        generate_symbol_report(metrics, score, direction, signals, symbol_dir)
        
        # 收集结果
        all_results.append({
            'symbol': metrics['symbol'],
            'score': score,
            'direction': direction,
            'signals': signals,
            'current_price': metrics['current_price'],
            'price_change_1h': metrics['price_change_1h'],
            'rsi': metrics['rsi'],
            'volume_surge': metrics['volume_surge'],
            'metrics': metrics
        })
    
    print("\n" + "="*60)
    print("生成汇总报告...")
    
    # 生成总报告
    summary = generate_summary_report(all_results)
    
    # 保存总报告到 data 目录
    summary_file = DATA_DIR / f"_SUMMARY_REPORT_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    with open(summary_file, 'w', encoding='utf-8') as f:
        f.write(summary)
    
    print(f"[完成] 汇总报告已保存: {summary_file}")
    
    # 输出关键结果
    print("\n" + "="*60)
    print("分析完成! 关键发现:")
    print("="*60)
    
    if not all_results:
        print("\n未找到有效数据,请检查:")
        print("1. 数据文件是否完整")
        print("2. K线数据是否存在")
        print("3. 数据格式是否正确")
        return
    
    df = pd.DataFrame(all_results).sort_values('score', ascending=False)
    print("\nTOP 3 交易机会:")
    for idx, row in df.head(3).iterrows():
        print(f"\n{idx+1}. {row['symbol']} - 评分: {row['score']}/100")
        print(f"   方向: {row['direction']}")
        print(f"   价格: {row['current_price']:.6f}")
        print(f"   信号: {', '.join(row['signals'][:2])}")
    
    print(f"\n详细报告已保存在各币种文件夹下")
    print(f"汇总报告: {summary_file.name}")
    print()

if __name__ == "__main__":
    main()
