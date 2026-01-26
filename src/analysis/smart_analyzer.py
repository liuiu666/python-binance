import pandas as pd
import numpy as np
import json
from datetime import datetime

def calculate_rsi(series, period=14):
    """计算 RSI 指标"""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def analyze_snapshot_mode(data):
    """仅基于盘口和资金费率的快照分析模式 (当缺少K线数据时使用)"""
    result = {
        'symbol': data['symbol'],
        'passed_screening': False,
        'signals': [],
        'risk_factors': [],
        'score': 0,
        'direction_score': 0,
        'mode': 'snapshot'
    }
    
    score = 50 # 初始中性分
    direction_score = 0
    
    # 1. 盘口分析 (权重高)
    ob = data.get('orderbook')
    if ob and ob.get('bids') and ob.get('asks'):
        bids = ob['bids']
        asks = ob['asks']
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid_price = (best_bid + best_ask) / 2
        result['current_price'] = mid_price
        
        # 价差
        spread_bps = (best_ask - best_bid) / mid_price * 10000
        result['spread_bps'] = spread_bps
        
        if spread_bps < 5:
            score += 10
            result['signals'].append(f"极低价差({spread_bps:.1f}bps)")
        elif spread_bps > 15:
            score -= 10
            result['risk_factors'].append(f"价差过大({spread_bps:.1f}bps)")
            
        # 失衡 (前20档)
        bid_vol = sum(float(x[1]) for x in bids[:20])
        ask_vol = sum(float(x[1]) for x in asks[:20])
        imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)
        result['imbalance'] = imbalance
        
        if imbalance > 0.15:
            score += 20
            direction_score += 1
            result['signals'].append(f"买盘强劲(失衡+{imbalance*100:.0f}%)")
        elif imbalance < -0.15:
            score += 20 
            direction_score -= 1
            result['signals'].append(f"卖盘压制(失衡{imbalance*100:.0f}%)")
        else:
            # 微弱失衡不加减分
            pass

    # 2. 资金费率与价格偏离
    pi = data.get('premium_index')
    if pi:
        fr = float(pi.get('lastFundingRate', 0))
        result['funding_rate'] = fr
        
        if fr < 0: # 费率为负, 利好做多
            score += 5
            direction_score += 0.5
            result['signals'].append(f"费率负值({fr*100:.4f}%)")
        elif fr > 0.0005: # 费率过高, 利空/利好做空
            score -= 5
            direction_score -= 0.5
            result['signals'].append(f"费率偏高({fr*100:.4f}%)")
            
        mark_price = float(pi.get('markPrice', 0))
        index_price = float(pi.get('indexPrice', 0))
        if mark_price and index_price:
            divergence = (mark_price - index_price) / index_price * 100
            if divergence > 0.1: # 标记价格高于指数, 溢价, 可能回调(做空)
                score -= 5
                direction_score -= 0.5
                result['signals'].append(f"正溢价({divergence:.2f}%)")
            elif divergence < -0.1: # 折价, 可能反弹(做多)
                score += 5
                direction_score += 0.5
                result['signals'].append(f"负溢价({divergence:.2f}%)")

    result['score'] = score
    result['direction_score'] = direction_score
    
    # 判定通过标准: 只要评分达到40分（即有一定信号或未被严重扣分）即通过
    if score >= 40:
        result['passed_screening'] = True
        
    return result

def analyze_symbol(data):
    """应用筛选漏斗 - 核心分析逻辑"""
    # 检查是否有K线数据, 如果没有则切换到快照模式
    if data.get('klines_1h') is None and data.get('klines_1m') is None:
        return analyze_snapshot_mode(data)

    result = {
        'symbol': data['symbol'],
        'passed_screening': False,
        'signals': [],
        'risk_factors': [],
        'score': 0,
        'direction_score': 0,
        'mode': 'full'
    }
    
    # ------------------------------------------------------
    # 1. 趋势共振筛选 (Trend Alignment)
    # ------------------------------------------------------
    df_1h = data.get('klines_1h')
    trend_score = 0
    direction_score = 0
    
    if df_1h is not None and len(df_1h) > 60:
        close = df_1h['close']
        ma20 = close.rolling(20).mean().iloc[-1]
        ma60 = close.rolling(60).mean().iloc[-1]
        current_price = close.iloc[-1]
        
        # 多头排列
        if current_price > ma20 > ma60:
            trend_score += 30
            direction_score += 2
            result['signals'].append("1H均线多头排列")
        # 空头排列
        elif current_price < ma20 < ma60:
            trend_score += 30
            direction_score -= 2
            result['signals'].append("1H均线空头排列")
            
        # RSI 检查
        rsi = calculate_rsi(close).iloc[-1]
        if 50 <= rsi <= 70:
            trend_score += 10
            direction_score += 0.5
            result['signals'].append(f"RSI强势区({rsi:.1f})")
        elif 30 <= rsi <= 50:
            trend_score += 10 # 空头强势
            direction_score -= 0.5
            
        # 波动率检查 (ATR)
        high = df_1h['high']
        low = df_1h['low']
        c_prev = close.shift(1)
        
        # Calculate True Range using pandas to avoid numpy warnings
        tr1 = high - low
        tr2 = (high - c_prev).abs()
        tr3 = (low - c_prev).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        atr = tr.rolling(14).mean().iloc[-1]
        atr_pct = (atr / current_price) * 100
        
        # 调整ATR阈值：0.5% 对于1H级别可能过高，改为0.3%
        if atr_pct < 0.3:
            result['risk_factors'].append(f"波动率过低(ATR={atr_pct:.2f}%)")
            trend_score -= 10 # 降低惩罚力度
        else:
            result['signals'].append(f"波动率合格(ATR={atr_pct:.2f}%)")

        result['atr_pct'] = atr_pct
        result['current_price'] = current_price

    # ------------------------------------------------------
    # 2. 资金与量能验证 (Volume & Flow)
    # ------------------------------------------------------
    # 使用 1m 或 15m 数据看短期爆发
    df_short = data.get('klines_1m') if data.get('klines_1m') is not None else data.get('klines_15m')
    
    if df_short is not None and len(df_short) > 50:
        vol = df_short['quote_volume']
        # Use the previous completed candle (iloc[-2]) to avoid low volume from current incomplete candle
        avg_vol = vol.iloc[-52:-2].mean() # Average of previous 50 completed candles
        cur_vol = vol.iloc[-2] # Last completed candle
        
        vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 0
        result['volume_ratio'] = vol_ratio
        
        # 资金流向 (简易估算: 收盘>开盘 * 成交量)
        price_change = df_short['close'].iloc[-1] - df_short['open'].iloc[-1]
        flow_direction = "流入" if price_change > 0 else "流出"
        
        if vol_ratio > 1.2: # 放宽成交量要求，从1.5降至1.2
            trend_score += 15
            # 量能方向加权
            if price_change > 0:
                direction_score += 1
                result['signals'].append(f"放量上涨({vol_ratio:.1f}x)")
            else:
                direction_score -= 1
                result['signals'].append(f"放量下跌({vol_ratio:.1f}x)")
        elif vol_ratio < 0.6:
            trend_score -= 5
            result['risk_factors'].append("成交量萎缩")
            
        # 资金流向 (简易估算: 收盘>开盘 * 成交量)
        # price_change 已经在上面计算过
        
        # 检查背离
        trend_direction = "多" if "多头" in str(result['signals']) else "空" if "空头" in str(result['signals']) else "震荡"
        
        # 只有在明确趋势下才严厉惩罚背离
        if trend_direction == "多" and flow_direction == "流出":
            result['risk_factors'].append("量价背离(涨价缩量/流出)")
            trend_score -= 15
        elif trend_direction == "空" and flow_direction == "流入":
            result['risk_factors'].append("量价背离(跌价放量/流入)")
            trend_score -= 15

    # ------------------------------------------------------
    # 2.5 持仓量与资金费率 (OI & Funding) - 用户规则核心
    # ------------------------------------------------------
    oi_hist = data.get('oi_hist')
    funding_rate = data.get('funding_rate')
    
    # 1. 资金费率 (Funding Rate)
    if funding_rate is not None:
        result['funding_rate'] = funding_rate
        # 费率正值过高 -> 做多成本高，倾向做空
        if funding_rate > 0.0005: 
             trend_score -= 5
             direction_score -= 0.5
             result['signals'].append(f"费率偏高({funding_rate*100:.4f}%)")
        # 费率负值过高 -> 做空成本高，倾向做多
        elif funding_rate < -0.0005: 
             trend_score += 5
             direction_score += 0.5
             result['signals'].append(f"费率负值({funding_rate*100:.4f}%)")

    # 2. 持仓量 (Open Interest)
    if oi_hist is not None and not oi_hist.empty:
        # 取最近 30 分钟 (6个点, 因为是5m数据)
        recent_oi = oi_hist.tail(6)
        if len(recent_oi) >= 2:
            oi_start = recent_oi['sumOpenInterest'].iloc[0]
            oi_end = recent_oi['sumOpenInterest'].iloc[-1]
            oi_change = (oi_end - oi_start) / oi_start if oi_start > 0 else 0
            
            result['oi_change_30m'] = oi_change
            
            if oi_change > 0.03: # 30分钟增仓3%
                trend_score += 15
                result['signals'].append(f"持仓激增({oi_change*100:.1f}%)")
                # 顺势加分
                if direction_score > 0.5: direction_score += 1
                elif direction_score < -0.5: direction_score -= 1
            elif oi_change < -0.03: # 减仓
                trend_score -= 5
                result['risk_factors'].append(f"持仓大幅下降({oi_change*100:.1f}%)")
                
            # 简单的背离检查
            # 如果当前是多头评分(direction_score > 0) 但 OI 下降 -> 顶背离风险
            if direction_score > 1 and oi_change < -0.01:
                result['risk_factors'].append("上涨减仓(顶背离风险)")
                trend_score -= 10
            # 如果当前是空头评分(direction_score < 0) 但 OI 下降 -> 底背离风险
            elif direction_score < -1 and oi_change < -0.01:
                result['risk_factors'].append("下跌减仓(底背离风险)")
                trend_score -= 10

    # ------------------------------------------------------
    # 3. 盘口与流动性 (Orderbook)
    # ------------------------------------------------------
    ob = data.get('orderbook')
    if ob and ob.get('bids') and ob.get('asks'):
        bids = ob['bids']
        asks = ob['asks']
        
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid_price = (best_bid + best_ask) / 2
        
        # 价差检查
        spread_bps = (best_ask - best_bid) / mid_price * 10000
        result['spread_bps'] = spread_bps
        
        if spread_bps > 15: # 放宽价差容忍度至 15bps (0.15%)
            trend_score -= 30
            result['risk_factors'].append(f"价差过大({spread_bps:.1f}bps)")
        elif spread_bps < 8:
            trend_score += 10
            result['signals'].append("流动性良好")
            
        # 失衡检查 (前20档)
        bid_vol = sum(float(x[1]) for x in bids[:20])
        ask_vol = sum(float(x[1]) for x in asks[:20])
        imbalance = (bid_vol - ask_vol) / (bid_vol + ask_vol)
        result['imbalance'] = imbalance
        
        if imbalance > 0.15: # 放宽失衡阈值
            trend_score += 20
            direction_score += 1
            result['signals'].append(f"买盘强劲(失衡+{imbalance*100:.0f}%)")
        elif imbalance < -0.15:
            trend_score += 20
            direction_score -= 1
            result['signals'].append(f"卖盘压制(失衡{imbalance*100:.0f}%)")
            
    result['score'] = trend_score
    result['direction_score'] = direction_score
        # 降低及格线：适应弱势行情，只要有亮点（>40分）即可进入观察
    if trend_score >= 40: 
        result['passed_screening'] = True
        
    return result

def save_individual_report(result, symbol_dir):
    """生成单币种详细分析报告"""
    file_path = symbol_dir / "ANALYSIS_REPORT.md"
    
    # 确定方向和建议
    mode = result.get('mode', 'full')
    score = result['score']
    price = result.get('current_price', 0)
    
    direction = "观望"
    if result['passed_screening']:
        # 使用 direction_score 判断方向，如果为0则回退到 score 判断 (虽然不太可能)
        d_score = result.get('direction_score', 0)
        if d_score > 0:
            direction = "做多"
        elif d_score < 0:
            direction = "做空"
        else:
            direction = "做多" if score > 50 else "做空"
    
    content = f"# {result['symbol']} 分析报告\n\n"
    content += f"- **生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    content += f"- **分析模式**: {'快照模式 (仅盘口)' if mode == 'snapshot' else '完整模式 (K线+盘口)'}\n"
    content += f"- **综合评分**: {score}分\n"
    content += f"- **当前价格**: {price}\n"
    content += f"- **建议方向**: {direction}\n\n"
    
    content += "## 1. 核心信号\n"
    if result['signals']:
        for s in result['signals']:
            content += f"- ✅ {s}\n"
    else:
        content += "- 无明显积极信号\n"
    content += "\n"
    
    content += "## 2. 风险提示\n"
    if result['risk_factors']:
        for r in result['risk_factors']:
            content += f"- ⚠️ {r}\n"
    else:
        content += "- 无明显风险\n"
    content += "\n"
    
    content += "## 3. 详细指标\n"
    content += f"- **价差 (Spread)**: {result.get('spread_bps', 0):.1f} bps\n"
    content += f"- **盘口失衡 (Imbalance)**: {result.get('imbalance', 0):.2f}\n"
    if mode == 'full':
        content += f"- **波动率 (ATR)**: {result.get('atr_pct', 0):.2f}%\n"
        content += f"- **量比 (Vol Ratio)**: {result.get('volume_ratio', 0):.2f}\n"
    elif mode == 'snapshot':
        content += f"- **资金费率**: {result.get('funding_rate', 0)*100:.4f}%\n"
    content += "\n"
    
    content += "## 4. 操作建议 (仅供参考)\n"
    if direction == "观望":
        content += "> 当前评分未达到入场标准，建议继续观察。\n"
    else:
        if mode == 'snapshot':
             sl = price * 0.995 if direction == "做多" else price * 1.005
             tp = price * 1.01 if direction == "做多" else price * 0.99
             content += f"- **入场**: 现价 {price}\n"
             content += f"- **止损**: {sl:.6f} (0.5%)\n"
             content += f"- **止盈**: {tp:.6f} (1.0%)\n"
             content += "- *注: 快照模式数据有限，建议轻仓尝试或人工二次确认。*\n"
        else:
            atr_pct = result.get('atr_pct', 1) / 100
            sl_dist = 2 * atr_pct
            tp_dist = 3 * atr_pct
            
            sl = price * (1 - sl_dist) if direction == "做多" else price * (1 + sl_dist)
            tp = price * (1 + tp_dist) if direction == "做多" else price * (1 - tp_dist)
            
            content += f"- **入场**: 现价 {price}\n"
            content += f"- **止损**: {sl:.6f} ({sl_dist*100:.2f}% - 2ATR)\n"
            content += f"- **止盈**: {tp:.6f} ({tp_dist*100:.2f}% - 3ATR)\n"
            
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        print(f"写入报告失败 {result['symbol']}: {e}")
