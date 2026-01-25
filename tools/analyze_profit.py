import json
import os
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional

@dataclass
class AnalysisResult:
    symbol: str
    score: float
    recommendation: str
    price: float
    spread_bps: float
    imbalance_top20: float
    path: Path

def 读取_json(路径: Path):
    with open(路径, "r", encoding="utf-8") as f:
        return json.load(f)

def 计算失衡(买盘, 卖盘, 档数=None):
    买额 = sum(float(x[1]) * float(x[0]) for x in 买盘[:档数])
    卖额 = sum(float(x[1]) * float(x[0]) for x in 卖盘[:档数])
    if 买额 + 卖额 == 0:
        return 0.0
    return (买额 - 卖额) / (买额 + 卖额)

def 分析目录(目录: Path) -> Optional[AnalysisResult]:
    订单簿路径 = 目录 / "order_book.json"
    溢价路径 = 目录 / "premium_index.json"
    元数据路径 = 目录 / "meta.json"

    if not (订单簿路径.exists() and 溢价路径.exists()):
        return None

    try:
        订单簿 = 读取_json(订单簿路径)
        溢价 = 读取_json(溢价路径)
        元数据 = 读取_json(元数据路径) if 元数据路径.exists() else {}

        买盘 = 订单簿.get("bids", [])
        卖盘 = 订单簿.get("asks", [])
        if not 买盘 or not 卖盘:
            return None

        买一 = float(买盘[0][0])
        卖一 = float(卖盘[0][0])
        中价 = (买一 + 卖一) / 2
        价差基点 = (卖一 - 买一) / 中价 * 10000

        失衡全量 = 计算失衡(买盘, 卖盘)
        失衡前20 = 计算失衡(买盘, 卖盘, 20)

        资金费率 = float(溢价.get("lastFundingRate", 0))
        标记价 = float(溢价.get("markPrice", 0))
        标记偏离基点 = (标记价 - 中价) / 中价 * 10000 if 中价 else 0.0

        评分 = 50
        信号 = "观望"
        依据 = []

        if 价差基点 < 2:
            评分 += 10
            依据.append("价差极小")
        elif 价差基点 > 10:
            评分 -= 10
            依据.append("价差偏大")

        if 失衡前20 > 0.2:
            评分 += 15
            信号 = "偏多"
            依据.append("买盘强势")
        elif 失衡前20 < -0.2:
            评分 -= 15
            信号 = "偏空"
            依据.append("卖盘强势")

        if 资金费率 < -0.0005:
            评分 += 5
            依据.append("资金费率为负")
        elif 资金费率 > 0.0005:
            评分 -= 5
            依据.append("资金费率偏高")

        if 评分 >= 70:
            建议 = "强烈建议做多"
        elif 评分 >= 60:
            建议 = "建议逢低做多"
        elif 评分 <= 30:
            建议 = "强烈建议做空"
        elif 评分 <= 40:
            建议 = "建议逢高做空"
        else:
            建议 = "建议观望"

        symbol = 元数据.get("symbol", "未知")
        time_str = 元数据.get("saved_at_utc", "未知")

        报告 = f"""# 快照操作分析报告

## 基本信息
- 标的：{symbol}
- 时间：{time_str}
- 综合评分：{评分}/100
- 操作建议：{建议}

## 盘口与资金面
- 买一：{买一}
- 卖一：{卖一}
- 价差：{价差基点:.2f} 基点
- 订单簿失衡（前20档）：{失衡前20:.2%}
- 订单簿失衡（全量）：{失衡全量:.2%}
- 资金费率：{资金费率:.6f}
- 标记价偏离：{标记偏离基点:.2f} 基点

## 收益最大化执行要点
1. 方向选择：{信号}（{", ".join(依据) if 依据 else "信号不足"}）
2. 进场策略：做多优先挂买一或下方支撑；做空优先挂卖一或上方压力
3. 风险控制：若价差扩大或失衡快速收敛，立即降低仓位或观望

## 说明
- 报告基于订单簿与溢价指数快照生成，建议结合多周期行情确认
"""

        报告路径 = 目录 / "report.md"
        报告路径.write_text(报告, encoding="utf-8")
        
        return AnalysisResult(
            symbol=symbol,
            score=评分,
            recommendation=建议,
            price=中价,
            spread_bps=价差基点,
            imbalance_top20=失衡前20,
            path=报告路径
        )
    except Exception as e:
        print(f"Error analyzing {目录}: {e}")
        return None

def main():
    基础目录 = Path(r"e:\量化\bxm40\data")
    results: List[AnalysisResult] = []
    
    for 根, _, 文件 in os.walk(基础目录):
        if "order_book.json" in 文件 and "premium_index.json" in 文件:
            res = 分析目录(Path(根))
            if res:
                results.append(res)
    
    print(f"已生成 {len(results)} 份报告。\n")
    
    # Sort by score descending
    results.sort(key=lambda x: x.score, reverse=True)
    
    print("=" * 60)
    print("收益潜力排行榜 (TOP 5)")
    print("=" * 60)
    print(f"{'标的':<12} {'评分':<6} {'建议':<12} {'价格':<12} {'价差(bps)':<10} {'失衡(Top20)':<10}")
    print("-" * 60)
    
    top_picks = results[:5]
    for r in top_picks:
        print(f"{r.symbol:<12} {r.score:<6} {r.recommendation:<12} {r.price:<12.4f} {r.spread_bps:<10.2f} {r.imbalance_top20:<10.2%}")
        
    print("\n" + "=" * 60)
    print("做空潜力榜 (评分倒序 TOP 5)")
    print("=" * 60)
    print(f"{'标的':<12} {'评分':<6} {'建议':<12} {'价格':<12} {'价差(bps)':<10} {'失衡(Top20)':<10}")
    print("-" * 60)
    
    # Sort by score ascending for shorts
    results.sort(key=lambda x: x.score)
    bottom_picks = results[:5]
    for r in bottom_picks:
        print(f"{r.symbol:<12} {r.score:<6} {r.recommendation:<12} {r.price:<12.4f} {r.spread_bps:<10.2f} {r.imbalance_top20:<10.2%}")

if __name__ == "__main__":
    main()
