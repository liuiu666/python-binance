# 币安合约量化交易系统 — 详细研发计划

> **关联文档**: [architecture_design.md](./architecture_design.md) | [research_notes.md](./research_notes.md)  
> **总预估周期**: 5~6 周（个人全职开发）

---

## Phase 1：数据采集服务 (Data Collector) — 第 1 周

### 目标
稳定获取币安合约实时行情数据，写入 Redis Streams + ClickHouse，具备自动重连和数据校准能力。

### 任务清单

#### 1.1 基础设施搭建（Day 1）
- [ ] 创建 `docker-compose.yml`，一键启动 Redis 7 + ClickHouse + PostgreSQL
- [ ] 创建 `.env.example`，定义 `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `REDIS_URL`, `CH_URL`, `PG_URL`
- [ ] 创建 `common/config.py`，通过 `pydantic-settings` 读取环境变量
- [ ] 创建 `common/logger.py`，统一日志格式（JSON 结构化日志，含时间戳和模块名）
- [ ] 创建 `common/redis_client.py`，封装 Redis Streams 的 `XADD` / `XREADGROUP` / `XACK`
- [ ] 创建 `requirements.txt`，锁定核心依赖版本

**技术要点**：
```python
# common/config.py 示例结构
class Settings(BaseSettings):
    binance_api_key: str
    binance_api_secret: str
    redis_url: str = "redis://localhost:6379"
    clickhouse_url: str = "http://localhost:8123"
    pg_dsn: str = "postgresql://user:pass@localhost:5432/bxm40"
    ws_ping_interval: int = 20
    ws_ping_timeout: int = 10
    rest_compensate_interval: int = 30
    symbols: list[str] = ["BTCUSDT", "ETHUSDT"]
```

**验收标准**：`docker-compose up -d` 后 Redis / ClickHouse / PostgreSQL 全部健康运行。

---

#### 1.2 WebSocket 客户端（Day 2-3）
- [ ] `collector/ws_client.py`：基于 `websockets` 的异步客户端
  - 多流复用：`/stream?streams=btcusdt@kline_1m/btcusdt@aggTrade/btcusdt@depth20@500ms`
  - Ping-Pong 心跳：收到 ping 后 10s 内回复 pong
  - 指数退避自动重连：1s → 2s → 4s → ... → 60s 封顶
  - 24h 热切换：运行 23h55min 后主动建立新连接，旧连接平滑关闭
- [ ] `collector/ws_client.py`：消息分发器
  - 解析 `stream` 字段，按类型（kline / aggTrade / depth）分发到不同处理函数
  - 每条消息附加本地接收时间戳 `local_recv_ts`

**技术要点**：
```python
# 24h 热切换核心逻辑
async def _schedule_reconnect(self):
    """23h55min 后主动重建连接"""
    await asyncio.sleep(23 * 3600 + 55 * 60)
    new_ws = await websockets.connect(self._build_url(), ...)
    old_ws = self._ws
    self._ws = new_ws  # 原子切换
    await old_ws.close()
```

**验收标准**：运行 `python -m collector.main`，终端持续打印 BTCUSDT 实时 K 线和成交数据，断网后自动重连。

---

#### 1.3 REST 校准器（Day 3）
- [ ] `collector/rest_client.py`：基于 `aiohttp` 的 REST 客户端
  - `get_klines(symbol, interval, limit)` — 拉取最近 N 根 K 线
  - `get_mark_price(symbol)` — 获取标记价格和资金费率
  - 内置令牌桶限流：确保总请求 < 1200 权重/分钟
- [ ] 校准逻辑：每 30s 拉取最近 5 根 K 线，与内存中的数据比对
  - 发现缺失 → 补写 Redis Streams
  - 发现数据不一致 → 以 REST 为准覆盖，并记录告警日志

**验收标准**：手动断开 WS 30 秒后恢复，确认 K 线数据无缺失。

---

#### 1.4 User Data Stream（Day 4）
- [ ] `collector/user_stream.py`：监听账户级事件
  - 调用 `POST /fapi/v1/listenKey` 获取 listenKey
  - 每 30min 调用 `PUT /fapi/v1/listenKey` 续期
  - 订阅事件类型：`BALANCE_UPDATE`（余额变更，含资金费率）、`ORDER_TRADE_UPDATE`（订单状态）
  - 将事件写入 Redis Streams `account:updates`

**验收标准**：在币安手动下一笔单，系统实时打印订单状态变更。

---

#### 1.5 健康监控 & ClickHouse 写入（Day 5）
- [ ] `collector/health.py`：监控模块
  - 记录每个 symbol 最后一条消息的时间
  - 15s 无数据 → 触发重连 + 日志告警
  - 提供 `/health` HTTP 端点（供后续 Docker 健康检查）
- [ ] `common/clickhouse.py`：ClickHouse 异步写入
  - 建表语句：`klines` 表（MergeTree，按 symbol + open_time 排序）
  - 批量写入：攒 100 条或 5 秒一批，异步 INSERT
- [ ] 集成测试：运行 1 小时无异常

**验收标准**：ClickHouse 中可查询到连续无缺失的 K 线数据。

---

## Phase 2：交易执行器 (Execution Engine) — 第 2 周

### 目标
从 Redis Streams 消费交易信号，经风控校验后调用币安 API 下单，并维护本地持仓状态。

### 任务清单

#### 2.1 风控模块（Day 1-2）
- [ ] `executor/risk_manager.py`：前置风控引擎
  - 规则 1：单笔最大金额不超过账户净值的 N%（可配置，默认 5%）
  - 规则 2：总持仓不超过 M 个（可配置，默认 3 个）
  - 规则 3：日累计亏损超过 X USDT 后进入"只读模式"，拒绝一切新开仓
  - 规则 4：同一 symbol 同方向信号去重（检查 `pending_signals` 集合）
  - 规则 5：杠杆不超过配置上限
- [ ] 每条规则独立函数，返回 `(pass: bool, reason: str)`，便于日志追溯

**验收标准**：构造 5 种违规信号，风控模块全部正确拦截并输出拒绝原因。

---

#### 2.2 订单管理器（Day 2-3）
- [ ] `executor/order_manager.py`：订单生命周期管理
  - `place_order(signal)` → 调用 `POST /fapi/v1/order`
  - 幂等性：每个信号的 `signal_id` 写入 Redis SET，重复信号直接丢弃
  - 订单状态追踪：PENDING → PARTIALLY_FILLED → FILLED / CANCELED / EXPIRED
  - 超时撤单：限价单超过 N 秒未成交 → 自动撤单 → 可选转市价单
- [ ] `executor/rate_limiter.py`：令牌桶限流器
  - 每分钟最多 1200 权重
  - HTTP 429 响应 → 全局进入冷却模式 60s

**验收标准**：模拟盘（testnet）成功下单、查询、撤单全流程。

---

#### 2.3 持仓同步（Day 4）
- [ ] `executor/position_sync.py`：本地 vs 币安持仓对账
  - 每 60s 调用 `GET /fapi/v2/positionRisk` 获取实际持仓
  - 与本地 PostgreSQL 中的持仓表比对
  - 差异 → 记录告警日志 + 推送钉钉通知
- [ ] PostgreSQL 表设计：
  - `trades` 表：每笔成交记录
  - `positions` 表：当前持仓快照
  - `daily_pnl` 表：每日盈亏汇总

**验收标准**：手动在币安平仓一笔，系统 60s 内检测到差异并告警。

---

#### 2.4 通知服务（Day 5）
- [ ] `common/notify.py`：钉钉 + Telegram 通知封装
  - 开仓通知：symbol、方向、数量、价格、止损、止盈
  - 平仓通知：盈亏金额、持仓时长、胜率更新
  - 风控告警：被拦截的信号及拒绝原因
  - 系统告警：WS 断连、持仓不一致、API 限流
- [ ] 消息模板化，支持 Markdown 格式

**验收标准**：执行器完成一笔模拟交易，钉钉收到格式正确的通知。

---

## Phase 3：策略引擎 (Strategy Engine) — 第 3 周前半

### 目标
从 Redis Streams 消费行情数据，计算技术指标，运行策略逻辑，输出标准化交易信号。

### 任务清单

#### 3.1 环形缓冲区 & 指标计算（Day 1-2）
- [ ] `strategy/ring_buffer.py`：内存 K 线缓冲区
  - 每个 symbol 维护最近 500 根 K 线的 DataFrame
  - 新 K 线到来时 `append` + `drop oldest`，O(1) 操作
  - 支持多周期：1m / 5m / 15m / 1h（从 1m 数据聚合生成）
- [ ] `strategy/indicators.py`：指标计算模块
  - EMA (快/慢线)、RSI、ATR、MACD、Bollinger Bands
  - 使用 `pandas-ta` 或 `TA-Lib`
  - 增量计算：新 K 线到来时只更新最后一行，不全量重算

**验收标准**：给定 100 根测试 K 线，指标计算结果与 TradingView 一致。

---

#### 3.2 策略框架 & 示例策略（Day 2-3）
- [ ] `strategy/base_strategy.py`：策略基类
  ```python
  class BaseStrategy(ABC):
      @abstractmethod
      def on_kline(self, symbol: str, df: pd.DataFrame) -> Optional[Signal]: ...
      @abstractmethod
      def on_trade(self, symbol: str, trade: dict) -> None: ...
  ```
- [ ] `strategy/strategies/ema_cross.py`：均线交叉策略
  - EMA(9) 上穿 EMA(21) → 做多信号
  - EMA(9) 下穿 EMA(21) → 做空信号
  - ATR 止损：入场价 ± 2×ATR
- [ ] `strategy/strategies/breakout.py`：突破策略
  - 价格突破最近 20 根 K 线最高点 → 做多
  - 价格跌破最近 20 根 K 线最低点 → 做空
- [ ] 信号标准化格式：
  ```json
  {
    "signal_id": "uuid",
    "timestamp": 1717000000000,
    "symbol": "BTCUSDT",
    "action": "OPEN",
    "side": "BUY",
    "quantity": 0.01,
    "price": 68000,
    "stop_loss": 67200,
    "take_profit": 69600,
    "strategy": "ema_cross",
    "reason": "EMA9上穿EMA21, RSI=42"
  }
  ```

**验收标准**：策略模块运行后，Redis Streams 中可观察到符合格式的交易信号。

---

#### 3.3 全链路闭环联调（Day 3）
- [ ] 同时启动 `collector` + `strategy` + `executor`（3 个独立进程）
- [ ] 使用币安 testnet（模拟盘），验证信号从产生到下单的完整流程
- [ ] 确认以下场景正常：
  - 正常开仓 → 成交 → 通知
  - 风控拦截 → 日志记录 → 不下单
  - WS 断连 → 自动重连 → 数据无缺失 → 策略继续运行
  - 策略进程重启 → 从 Redis Streams 的 last-ack 位置继续消费，不丢信号

**验收标准**：系统在 testnet 上自动运行 24h 无异常，至少完成 3 笔完整交易。

---

## Phase 4：前端看板 (Dashboard) — 第 3-4 周

### 目标
搭建 Web 看板，实时展示账户状态、K 线图（含策略标记）和交易记录，支持手动干预。

### 任务清单

#### 4.1 FastAPI 后端（Day 1-2）
- [ ] `api/main.py`：FastAPI 应用入口
  - CORS 中间件（允许前端跨域）
  - JWT 鉴权（简单 token 即可，保护 API）
- [ ] `api/routes/account.py`：
  - `GET /api/account` — 账户权益、可用余额、未实现盈亏
  - `GET /api/positions` — 当前持仓列表
- [ ] `api/routes/trades.py`：
  - `GET /api/trades` — 交易记录（分页）
  - `GET /api/daily-pnl` — 每日盈亏曲线
  - `GET /api/stats` — 统计：胜率、盈亏比、最大回撤、夏普比率
- [ ] `api/routes/control.py`：
  - `POST /api/close-all` — 一键全平
  - `POST /api/pause` — 暂停策略
  - `POST /api/emergency-order` — 紧急市价单
- [ ] `api/ws_handler.py`：
  - WebSocket 端点 `/ws`，订阅 Redis Streams 转发实时数据给前端

**验收标准**：Swagger UI (`/docs`) 中所有接口可正常调用。

---

#### 4.2 React 前端（Day 3-5）
- [ ] 使用 Vite + React + TypeScript 初始化项目
- [ ] 页面与组件：
  - **Dashboard 首页**：
    - 顶栏：总权益、今日盈亏、胜率、运行状态指示灯
    - K 线图区域（TradingView Lightweight Charts）：叠加买卖点箭头 + 均线
    - 持仓卡片列表：symbol、方向、数量、浮动盈亏、开仓价、当前价
    - 最近交易记录表格
  - **盈亏分析页**：
    - 每日盈亏柱状图
    - 累计收益曲线
    - 胜率 / 盈亏比 / 最大回撤统计卡片
  - **控制面板**：
    - 一键全平按钮（需二次确认）
    - 暂停/恢复策略开关
    - 当前运行策略列表及参数
  - **系统日志页**：
    - 实时滚动日志（WS 推送）
    - 筛选：按级别（INFO / WARN / ERROR）
- [ ] WebSocket 连接管理：自动重连 + 连接状态指示
- [ ] 深色主题，配色方案：
  - 背景：`#0d1117` / `#161b22`
  - 涨色：`#00c853`，跌色：`#ff1744`
  - 强调色：`#58a6ff`

**验收标准**：前端页面与后端联调，实时显示持仓和 K 线，点击"全平"能成功平仓。

---

## Phase 5：AI 智能模块 — 第 5 周

### 目标
接入 LLM 实现情绪分析、交易复盘和策略参数建议，以旁路异步方式运行，不影响交易热路径。

### 任务清单

#### 5.1 情绪分析 Agent（Day 1-2）
- [ ] `ai/sentiment.py`：
  - 数据源：币安广场 API / Twitter API / CryptoNews RSS
  - 每 4 小时运行一次
  - 调用 Gemini API，Prompt 要求输出结构化 JSON：
    ```json
    {"sentiment_score": 0.3, "dominant_emotion": "cautious_optimism", "key_events": ["..."], "suggestion": "维持多头倾向但缩小仓位"}
    ```
  - 写入 Redis key `ai:sentiment:{symbol}` 供策略读取
- [ ] 策略端适配：`base_strategy.py` 增加 `get_sentiment()` 方法，策略可选择性参考

#### 5.2 交易复盘 Agent（Day 2-3）
- [ ] `ai/reviewer.py`：
  - 每日 22:00 触发
  - 从 PostgreSQL 读取当日所有交易记录
  - 构造 Prompt：包含每笔交易的入场/出场价格、持仓时间、盈亏、当时的技术指标快照
  - LLM 输出复盘报告：表现评分、改进建议、发现的非理性行为
  - 报告推送至钉钉 + 存入 PostgreSQL `ai_reports` 表
  - 前端"复盘"页面可查看历史报告

#### 5.3 参数微调 Agent（Day 3-4）
- [ ] `ai/tuner.py`：
  - 每日凌晨运行
  - 输入：最近 7 天的 K 线特征（平均 ATR、趋势斜率、波动率变化）
  - LLM 根据市况特征输出参数建议：
    ```json
    {"ema_fast": 7, "ema_slow": 25, "atr_multiplier": 2.5, "reason": "近期波动率上升，建议加宽止损"}
    ```
  - 写入 Redis key `ai:params:{strategy_name}`
  - 策略模块下次 tick 时读取并决定是否采纳（需满足参数合法性校验）

#### 5.4 调度器（Day 4）
- [ ] `ai/scheduler.py`：基于 APScheduler 的定时调度
  - 情绪分析：每 4h
  - 交易复盘：每日 22:00
  - 参数微调：每日 02:00
  - 异常重试：单次 Agent 失败后重试 2 次，间隔 60s
  - 超时保护：单次 Agent 运行超过 120s 强制终止

**验收标准**：AI 模块独立运行 3 天，每日准时推送复盘报告到钉钉，情绪指数正常更新。

---

## Phase 6：部署与运维 — 第 6 周

### 任务清单

#### 6.1 Docker 化（Day 1-2）
- [ ] 为 `collector` / `strategy` / `executor` / `api` / `ai` 各写 Dockerfile
- [ ] 更新 `docker-compose.yml`：
  ```yaml
  services:
    redis:       # Redis 7
    clickhouse:  # ClickHouse
    postgres:    # PostgreSQL
    collector:   # 数据采集
    strategy:    # 策略引擎
    executor:    # 交易执行
    api:         # FastAPI 后端
    frontend:    # Nginx + React 静态文件
    ai:          # AI 模块
  ```
- [ ] 健康检查：每个服务配置 `healthcheck`
- [ ] 日志收集：统一输出到 `stdout`，由 Docker 日志驱动收集

#### 6.2 监控告警（Day 3）
- [ ] Grafana + Prometheus 监控（可选，Docker 容器内）
  - 采集延迟、消息队列长度、下单成功率、API 权重使用率
- [ ] 钉钉/Telegram 告警：系统级异常自动推送

#### 6.3 安全加固（Day 3）
- [ ] API Key 通过 Docker Secrets 或 `.env` 注入，不写死在代码中
- [ ] FastAPI 开启 HTTPS（Let's Encrypt 或自签证书）
- [ ] IP 白名单限制后端 API 访问

#### 6.4 文档与回测（Day 4-5）
- [ ] `README.md`：项目介绍、快速启动指南、配置说明
- [ ] 回测模块（简易版）：
  - 读取 ClickHouse 历史数据
  - 复用策略模块代码，模拟运行
  - 输出盈亏曲线、最大回撤、夏普比率

**验收标准**：`docker-compose up -d` 一键启动全部服务，系统持续稳定运行 72h。

---

## 技术栈汇总

| 层 | 技术 | 版本 |
|:---|:---|:---|
| 语言 | Python | 3.11+ |
| 异步框架 | asyncio + websockets + aiohttp | — |
| 消息总线 | Redis Streams | 7.x |
| 时序存储 | ClickHouse | 24.x |
| 关系存储 | PostgreSQL | 16.x |
| 后端 API | FastAPI + Uvicorn | 0.110+ |
| 前端 | React + TypeScript + Vite | React 18 |
| K 线图 | TradingView Lightweight Charts | 4.x |
| 指标计算 | pandas-ta / TA-Lib | — |
| AI | Google Gemini SDK / OpenAI SDK | — |
| 调度 | APScheduler | 3.x |
| 部署 | Docker Compose | — |
| 通知 | 钉钉 Webhook + Telegram Bot API | — |

---

## 风险与应对

| 风险 | 概率 | 影响 | 应对方案 |
|:---|:---|:---|:---|
| 币安 API 变更 | 中 | 高 | 采集层做接口抽象，变更只改 adapter |
| WS 长时间断连 | 中 | 高 | REST 校准 + 指数退避重连 + 钉钉告警 |
| ClickHouse 磁盘满 | 低 | 中 | TTL 自动清理 90 天以上数据 |
| LLM 幻觉导致错误建议 | 高 | 低 | AI 仅输出建议，策略保留决策权 |
| 网络延迟导致滑点 | 中 | 中 | 部署在低延迟机房 + 滑点保护阈值 |
