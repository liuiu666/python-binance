# Hyperliquid vs Binance 合约交易对比分析

> **目的**：评估是否应将 Hyperliquid 作为数据源/交易所替代或补充方案

---

## 1. 核心对比

| 维度 | Binance 合约 | Hyperliquid |
|:---|:---|:---|
| **类型** | 中心化交易所 (CEX) | 去中心化永续合约 (DEX on-chain) |
| **资金托管** | 币安托管 | **自托管**（钱包自己控制） |
| **KYC** | 需要实名认证 | **无需 KYC** |
| **执行延迟** | 微秒级（HFT 级别） | 亚 100ms（自研 L1 链优化） |
| **稳定性** | 99.99% uptime，经历过多轮极端行情 | 较好，但极端波动时偶有异常 |
| **交易对数量** | 1500+ | 主流币为主（BTC/ETH 等） |
| **深度/流动性** | 全球第一，不可替代 | 主流币接近币安，山寨币差距大 |

---

## 2. 费率对比

| 费率类型 | Hyperliquid | Binance (标准) | 备注 |
|:---|:---|:---|:---|
| 合约 Taker | **0.045%** | 0.050% | Hyperliquid 更低 |
| 合约 Maker | **0.015%** | 0.020% | Hyperliquid 更低 |
| 现货 Taker | 0.070% | 0.100% | Hyperliquid 更低 |
| Gas 费 | **免费** | 不适用 | — |
| 额外折扣 | HYPE 质押 (最高 -40%) | BNB 抵扣 (-25% 现货 / -10% 合约) | — |

**结论**：纯费率角度，Hyperliquid 更有优势。

---

## 3. API / 数据获取稳定性对比（重点）

### 3.1 WebSocket 稳定性

| 对比项 | Binance | Hyperliquid |
|:---|:---|:---|
| 强制断连 | **每 24h 断一次**（已知且可预测） | **随时可能断**（文档明确声明不预告） |
| 重连后数据恢复 | 需自行 REST 补数据 | 重连后会推送快照 (snapshot) |
| 心跳机制 | 服务端发 ping，客户端回 pong | 需客户端主动实现心跳检测 |
| 多流复用 | ✅ 支持（单连接订阅多个流） | ❌ **不支持批量订阅** |
| 订阅上限 | 无明确硬限制 | **1000 订阅/IP**（每个 symbol 算一个） |
| 消息限制 | 无明确限制 | **2000 条/分钟** |

### 3.2 REST API 稳定性

| 对比项 | Binance | Hyperliquid |
|:---|:---|:---|
| 限流 | 1200 权重/分钟 | 有限流但文档较模糊 |
| API 成熟度 | 极其成熟，文档庞大 | 现代化、简洁，但文档较薄 |
| SDK 生态 | python-binance / ccxt 等大量库 | 官方 Python SDK + 社区 |
| 历史数据 | 支持拉取任意历史区间 K 线 | 有限支持 |

### 3.3 数据稳定性结论

> **Binance 数据获取更稳定。**

原因：
1. **断连可预测**：币安的 24h 断连是确定性行为，可以提前热切换；Hyperliquid 随时可能断
2. **多流复用**：币安单连接可订阅所有流，Hyperliquid 每个订阅独占计数，多品种交易时容易撞上 1000 上限
3. **生态成熟**：币安的 SDK、文档、社区问答极为丰富，踩坑有人带路
4. **历史数据完整**：币安可任意拉取历史 K 线做回测，Hyperliquid 能力有限

---

## 4. 什么场景选 Hyperliquid？

| 场景 | 推荐 |
|:---|:---|
| 需要自托管资金、不想放在交易所 | ✅ Hyperliquid |
| 不想 KYC / 需要隐私 | ✅ Hyperliquid |
| 想交易 TradFi 永续（标普500、黄金、个股） | ✅ Hyperliquid（独有） |
| 费率敏感型、高频 Maker 策略 | ✅ Hyperliquid（Maker 0.015%） |
| 需要极低延迟 HFT | ✅ Binance |
| 需要交易大量山寨币 | ✅ Binance |
| 需要大资金深度（鲸鱼级） | ✅ Binance |
| 数据稳定性优先 | ✅ Binance |

---

## 5. 对本项目架构的影响

### 推荐方案：以币安为主，预留 Hyperliquid 扩展接口

架构层面几乎不需要改动，只需在**数据采集层**做交易所抽象：

```python
# collector/exchange_adapter.py

class ExchangeAdapter(ABC):
    """交易所适配器基类"""
    @abstractmethod
    async def connect_ws(self, symbols: list, callbacks: dict): ...
    
    @abstractmethod
    async def get_klines(self, symbol: str, interval: str, limit: int): ...
    
    @abstractmethod
    async def place_order(self, symbol: str, side: str, quantity: float, **kwargs): ...

class BinanceAdapter(ExchangeAdapter):
    """币安实现 — Phase 1 优先开发"""
    ...

class HyperliquidAdapter(ExchangeAdapter):
    """Hyperliquid 实现 — 后续按需扩展"""
    ...
```

在 `config.py` 中配置使用哪个交易所：
```python
class Settings(BaseSettings):
    exchange: str = "binance"  # "binance" | "hyperliquid"
```

**这样的设计不改动现有架构，未来想切换或同时跑两个所只需新增一个 Adapter 类。**

---

## 6. 最终建议

| 决策 | 建议 |
|:---|:---|
| **Phase 1~3 开发阶段** | 用 **Binance**，数据稳定、文档齐全、调试方便 |
| **架构层面** | 预留交易所抽象接口 `ExchangeAdapter`（代码量极小） |
| **未来扩展** | 系统跑稳后，如果有自托管/低费率需求，新增 `HyperliquidAdapter` |
| **高级玩法** | 双所同时运行，币安做数据源 + Hyperliquid 做执行（利用费率优势） |
