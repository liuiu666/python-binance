# 下一步研发计划

> **更新时间**: 2026-05-31  
> **当前状态**: 后端 6 轮审查通过 (9.5/10)，基础设施已部署 Docker，代码零 Bug  
> **目标**: 从"代码完成"推进到"可实盘运行"

---

## 阶段 1: 本地联调验证 (预计 2-3 天)

> 目标: 确认数据采集 → 策略 → 执行全链路在测试网上跑通

### 1.1 环境准备
- [ ] 复制 `.env.example` 为 `.env`，填入币安测试网 API Key/Secret
- [ ] 确认 `BINANCE_TESTNET=true`
- [ ] `pip install -r requirements.txt` 安装依赖
- [ ] 确认 Docker 基础设施正常（`docker-compose -f docker-compose.infra.yml up -d`）

### 1.2 逐模块启动调试
- [ ] **Collector**: `python -m collector.main`
  - 验证 WS 连接成功、K 线数据写入 Redis
  - 验证 ClickHouse 表自动创建、K 线写入
  - 验证 healthcheck 端点 `http://localhost:8080/health` 返回 200
  - 观察 30s 校准器是否正常运行
- [ ] **Strategy**: `python -m strategy.main`
  - 验证从 Redis 消费 K 线消息
  - 验证指标计算（EMA/RSI/ATR/MACD/布林带）无报错
  - 观察是否产生交易信号
- [ ] **Executor**: `python -m executor.main`
  - 验证信号消费 → 风控检查 → 测试网下单
  - 验证 PostgreSQL trades 表写入
  - 验证持仓同步（60s 周期）
- [ ] **API**: `python -m api.main`
  - 验证 `http://localhost:8000/docs` Swagger 文档可访问
  - 验证 `/api/trades`、`/api/stats` 返回数据
  - 验证 WebSocket `/ws` 连接 + 订阅后收到实时推送

### 1.3 端到端验证
- [ ] 手动触发一笔测试网交易（通过 `/api/control/emergency-order`）
- [ ] 确认交易记录写入 PostgreSQL
- [ ] 确认钉钉/Telegram 通知送达（如已配置）
- [ ] 运行 2 小时以上，观察无内存泄漏、无连接断开

---

## 阶段 2: 前端开发 (预计 5-7 天)

> 目标: 搭建实时交易看板，替代纯日志监控

### 2.1 技术栈
- 框架: **Vite + React + TypeScript**（`frontend/` 已有脚手架）
- 图表: **TradingView Lightweight Charts**（K 线图）
- 样式: **Vanilla CSS**（暗色主题）
- 实时通信: **WebSocket**（对接后端 `/ws`）

### 2.2 核心页面
- [ ] **Dashboard 首页**: 账户总览（余额、净值、今日盈亏）+ 持仓卡片
- [ ] **K 线图页**: TradingView 图表 + 实时 K 线推送 + 指标叠加
- [ ] **交易记录页**: 分页表格 + 筛选（symbol/日期）+ 盈亏统计
- [ ] **策略控制页**: 暂停/恢复策略、查看策略参数、手动下单
- [ ] **系统状态页**: 各模块健康状态、Redis/PG/CH 连接状态、日志流

### 2.3 组件
- [ ] 实时盈亏曲线（累计 PnL 折线图）
- [ ] 持仓卡片组件（symbol + 方向 + 未实现盈亏）
- [ ] 交易通知 Toast（新成交时弹出）
- [ ] WebSocket 连接状态指示器

---

## 阶段 3: 泊松异常检测集成 (预计 1-2 天)

> 目标: 将成交量异常检测作为信号增强器集成到策略引擎

### 3.1 开发任务
- [ ] 实现 `strategy/poisson_detector.py`（核心检测器，方案见 `docs/poisson_model.md`）
- [ ] 在 `strategy/indicators.py` 新增 `compute_trade_intensity()` 指标列
- [ ] 实现 `strategy/strategies/volume_anomaly.py` 策略
- [ ] 在 `strategy/main.py` 注册新策略
- [ ] 确认 `collector/main.py` 中 `trades_count` 字段已传入 Redis（已确认 ✅）

### 3.2 验证
- [ ] 用历史数据回测，统计误报率
- [ ] 观察泊松 z-score 与实际行情的相关性
- [ ] 调整阈值参数（window_size、ema_alpha、overdispersion_factor）

---

## 阶段 4: AI 模块对接 (预计 3-5 天)

> 目标: 让 AI 模块（情绪分析、交易复盘、参数微调）真正产生价值

### 4.1 开发任务
- [ ] 实现 `ai/sentiment.py` — 调用 Gemini/OpenAI 分析市场情绪
- [ ] 实现 `ai/reviewer.py` — 每日 22:00 自动复盘当天交易
- [ ] 实现 `ai/tuner.py` — 根据复盘结果微调策略参数
- [ ] 验证 `ai/scheduler.py` 定时任务正常触发

### 4.2 数据流
- [ ] 情绪分析结果写入 `ai:sentiment:{symbol}` Redis key
- [ ] 策略引擎读取情绪分数，作为信号置信度乘数
- [ ] 参数建议写入 `ai:params:{strategy_name}` Redis key

---

## 阶段 5: 生产加固 (预计 3-5 天)

> 目标: 从测试网切换到主网前的最后准备

### 5.1 安全加固
- [ ] `.env` 加入 `.gitignore`（已有 ✅，再次确认）
- [ ] API 接口加认证（JWT 或 API Key）
- [ ] CORS `allow_origins` 从 `["*"]` 改为具体前端域名
- [ ] 限制紧急下单接口的调用频率

### 5.2 监控与告警
- [ ] 接入 Prometheus + Grafana（可选，Docker 中添加）
- [ ] 关键指标: 信号产生速率、下单成功率、延迟分布、Redis 内存
- [ ] 日志归档: 配置 logrotate 或 ELK

### 5.3 容灾与恢复
- [ ] PostgreSQL 自动备份（pg_dump cron）
- [ ] Redis AOF 持久化已开启（✅ `--appendonly yes`）
- [ ] 服务崩溃后自动重启（Docker `restart: unless-stopped` ✅）
- [ ] 策略引擎启动时从 Redis 恢复未处理信号（pending 消息重消费）

### 5.4 性能优化
- [ ] RingBuffer 指标增量计算（只算最后一行，不全量重算）
- [ ] ClickHouse 批量写入调参（batch_size / flush_interval）
- [ ] Redis Streams XTRIM 策略确认（maxlen 是否合理）

---

## 阶段 6: 主网上线 (预计 1 天)

- [ ] 将 `BINANCE_TESTNET` 改为 `false`
- [ ] 确认 API Key 权限（仅开启合约交易，不开提币）
- [ ] 用最小仓位（0.001 BTC）运行 24 小时观察
- [ ] 确认所有通知渠道畅通
- [ ] 逐步放大仓位

---

## 里程碑时间线

```
当前 ──▶ 阶段1(联调) ──▶ 阶段2(前端) ──▶ 阶段3(泊松) ──▶ 阶段4(AI) ──▶ 阶段5(加固) ──▶ 阶段6(上线)
         2-3天            5-7天           1-2天          3-5天         3-5天          1天
                                                                              总计: 15-23天
```
