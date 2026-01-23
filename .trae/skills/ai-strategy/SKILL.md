---
name: "ai-strategy"
description: "使用大语言模型(LLM)分析市场数据，生成交易决策。当需要更高维度的智能判断时调用，替代传统的规则策略。"
---

# AI Strategy Skill

此技能引入 LLM 作为核心决策大脑。它不再依赖死板的代码规则（如 RSI>70 就卖），而是将所有数据喂给大模型，让模型根据其内在的金融知识库进行综合判断。

## 功能特性
1. **全数据融合**：同时考虑 K 线形态、技术指标、资金流向等多个维度。
2. **自然语言解释**：不仅输出 BUY/SELL 信号，还会告诉你理由（例如："虽然 RSI 超买，但资金持续流入，建议继续持有"）。
3. **模型无关性**：支持 GPT-4, Claude 3.5, DeepSeek 等任何兼容 OpenAI 接口的模型。

## 配置方法
在使用前，请确保 `config.json` 或环境变量中配置了正确的 API Key。

```json
{
    "llm": {
        "api_key": "sk-xxxx",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o"
    }
}
```

## 使用方法

```python
from handlers.llm_client import LLMClient

llm = LLMClient()

# 准备数据
market_data = {
    "symbol": "BTCUSDT",
    "current_price": 65000,
    "rsi": 75,
    "net_inflow": 5000000
    # ... 其他指标
}

# 获取建议
signal, reason = llm.get_trading_advice(market_data)

if signal == 'BUY':
    print(f"AI 建议买入: {reason}")
```
