# 本轮代码审查待确认问题

更新时间: 2026-05-31

## P0: 订单成交事件是否正确反序列化

需要确认 `account:updates` 里的 `ORDER_TRADE_UPDATE` 消息，在进入 `OrderManager.handle_order_update()` 前是否已经把字段 `o` 从 JSON 字符串还原成 dict。

当前风险:
- `collector/user_stream.py` 会把 Binance 原始事件写入 Redis Stream。
- `common/redis_client.py` 会把嵌套字段序列化为 JSON 字符串。
- `executor/main.py` 当前直接把 `fields` 传给 `handle_order_update()`。
- 如果 `o` 仍是字符串，`order_data.get(...)` 会报错，成交入库和 PnL 计算会失效。

建议:
- 在 `_process_account_event()` 中对每个 Redis 字段做一次 `json.loads()` 尝试，和 `_process_signal()` 的处理方式保持一致。

## P1: 前端构建是否已修复

需要确认 `frontend` 当前能否通过:

```powershell
npm.cmd run build
```

已发现的构建错误:
- `Chart.tsx` 需要使用 type-only import。
- `lightweight-charts` v5 API 不能再按旧版方式调用 `addCandlestickSeries()` / `setMarkers()`。
- `PnLChart.tsx` 中 `Bar` 不能使用 `cell` prop，应改为 `<Cell />` 子节点。
- `TradeTable.tsx` 存在未使用的 `TradeRecord` 类型导入。

## P1: WebSocket 是否实际订阅频道

需要确认前端连接 `/ws` 后是否调用了 `subscribe()`。

当前风险:
- 后端只会向已订阅频道的连接广播。
- `StatusBar.tsx` 创建了 WebSocket，但没有订阅 `market:*` 或具体行情频道。
- 结果是实时价格更新逻辑可能永远收不到消息。

建议:
- 在 `StatusBar.tsx` 中根据配置或后端 symbol 列表订阅 `market:btcusdt`、`market:ethusdt` 等频道。

## P2: pending_signals 是否会释放

需要确认风控通过后加入的 `pending_signals` 是否会在订单成功、失败、拒绝、取消或异常时移除。

当前风险:
- `RiskManager.check_signal()` 通过后会加入 `pending_signals`。
- `remove_pending_signal()` 当前没有明显调用点。
- 下单失败或后续异常时，同一 `signal_id` 会在内存里长期保持“重复信号”状态。

建议:
- 在执行器完成订单处理后统一调用 `remove_pending_signal(signal_id)`。
- 对风控通过但下单失败的路径也要释放。

## 建议验证清单

```powershell
python -m compileall -q ai api collector common executor strategy
cd frontend
npm.cmd run build
```

---

# 整体架构审查 v2

更新时间: 2026-05-31

本轮审查顺序: 数据获取 -> 数据分析 / AI -> 策略 -> 执行风控 -> 前端界面管理 -> key / 部署配置。

## 已确认修复

- `executor/main.py` 已在 `_process_account_event()` 中反序列化 Redis Stream 字段，`ORDER_TRADE_UPDATE.o` 不再直接以 JSON 字符串传给订单管理器。
- `executor/main.py` 已在下单成功、失败和异常路径释放 `pending_signals`。
- Python 代码通过 `python -m compileall -q ai api collector common executor strategy`。

## P0: `.env.example` 的 `SYMBOLS=BTCUSDT,ETHUSDT` 会导致服务启动失败

位置:
- `.env.example:28`
- `common/config.py:51`

原因:
- `symbols` 类型是 `List[str]`。
- pydantic-settings 对复杂类型默认按 JSON 解析。
- 当前示例里的 `BTCUSDT,ETHUSDT` 不是合法 JSON list。

验证结果:

```powershell
$env:SYMBOLS='BTCUSDT,ETHUSDT'
python -c "from common.config import Settings; print(Settings().symbols)"
```

会抛出 `SettingsError: error parsing value for field "symbols"`。

建议:
- 方案 A: 把 `.env.example` 改成 `SYMBOLS=["BTCUSDT","ETHUSDT"]`。
- 方案 B: 在 `Settings` 里加 field validator，兼容逗号分隔字符串。

## P0: Docker Compose 内部服务默认会连向容器自己的 localhost

位置:
- `.env.example:14`
- `.env.example:17`
- `.env.example:24`
- `common/config.py:38`
- `common/config.py:41`
- `common/config.py:48`
- `docker-compose.yml`

风险:
- `REDIS_URL=redis://localhost:6379/0`
- `CLICKHOUSE_HOST=localhost`
- `PG_DSN=postgresql://...@localhost:5432/bxm40`

这些配置适合宿主机直跑，但在 compose 容器内，`localhost` 指向当前容器，不是 `redis` / `clickhouse` / `postgres` 服务。按当前 `.env.example` 一键部署，collector / strategy / executor / api / ai 大概率连不上基础设施。

建议:
- 在 `docker-compose.yml` 的应用服务里显式覆盖:

```yaml
environment:
  REDIS_URL: redis://redis:6379/0
  CLICKHOUSE_HOST: clickhouse
  PG_DSN: postgresql://bxm40:bxm40_secret@postgres:5432/bxm40
```

或提供 `.env.docker.example`。

## P0: AI 调参读取 ClickHouse 但 AI 服务没有连接 ClickHouse

位置:
- `ai/scheduler.py:62-63`
- `ai/tuner.py:117`
- `ai/tuner.py:122`
- `docker-compose.yml:135`

风险:
- `TunerAgent._compute_features()` 调用 `clickhouse_client.query()`。
- `AIScheduler.start()` 只连接 Redis 和 PostgreSQL，没有 `clickhouse_client.connect()`。
- compose 里 `ai` 服务也没有依赖 `clickhouse`。

结果:
- 每日 02:00 参数微调任务会失败，AI 参数不会写入 Redis。

建议:
- AI scheduler 启动时连接 ClickHouse，并在停止时 close。
- compose 给 `ai` 增加 `clickhouse` dependency。

## P0: 策略产生 stop_loss / take_profit，但执行层不会真正挂止损止盈单

位置:
- `strategy/strategies/ema_cross.py:114-124`
- `strategy/strategies/breakout.py:101-111`
- `executor/order_manager.py:122-123`
- `executor/order_manager.py:180-181`

风险:
- 策略信号包含 `stop_loss` 和 `take_profit`。
- 订单管理器只是保存到 `TrackedOrder` / trades 表。
- 没有向交易所提交 `STOP_MARKET` / `TAKE_PROFIT_MARKET` / reduce-only 保护单。
- 策略本身也没有 CLOSE 信号逻辑。

结果:
- 自动开仓后不会自动止损/止盈，除非手动全平或外部另行处理。

建议:
- 开仓成交后按方向挂 reduce-only 止损止盈单。
- 或策略层实现 CLOSE 信号，并由执行层处理平仓。

## P1: 信号里的 leverage 只检查上限，没有真正设置到交易所

位置:
- `executor/risk_manager.py:242-251`
- `executor/order_manager.py:160`
- `collector/rest_client.py:289`

风险:
- 风控会检查 `leverage <= settings.max_leverage`。
- 下单前没有调用 Binance `/fapi/v1/leverage`。
- 实际杠杆取决于交易所账户当前设置，和信号不一定一致。

建议:
- 增加 `set_leverage(symbol, leverage)`。
- 在首次开仓或杠杆变化时设置，并记录失败时拒单。

## P1: 成交均价使用了最后一笔成交价，PnL 可能不准

位置:
- `executor/order_manager.py:286-302`

风险:
- `avg_price = float(order_data.get("L", 0))`
- Binance 订单更新里的 `L` 通常是最后成交价，不是整单平均成交价。
- 分批成交时，最终入库 entry / exit 价格和 PnL 会偏差。

建议:
- 优先使用订单更新里的平均成交价字段，如 `ap`。
- 没有平均价时再 fallback 到 `L`。

## P1: 数据补偿只补 Redis，不补 ClickHouse 历史库

位置:
- `collector/rest_client.py:392-424`
- `collector/main.py:219`

风险:
- WS 闭合 K 线会写入 Redis 和 ClickHouse。
- REST compensator 发现缺失后只写 Redis Stream。
- ClickHouse 仍然缺历史 K 线。

影响:
- 策略短期可能能消费补偿数据。
- AI 调参 / 历史分析依赖 ClickHouse，会继续看到缺口。

建议:
- compensator 补偿时同步写 ClickHouse，或让 collector 对 `source=compensator` 的闭合 K 线也持久化。

## P1: 策略服务没有历史 warmup，重启后至少等 30 根闭合 K 线

位置:
- `strategy/main.py:85`
- `strategy/main.py:223`
- `strategy/indicators.py:35`

风险:
- 消费组以 `id="$"` 创建，只消费新消息。
- RingBuffer 启动时为空。
- 策略要求至少 30 根 K 线。

结果:
- 服务重启后至少约 30 分钟不会产生信号。

建议:
- 启动时从 ClickHouse 或 REST 拉最近 N 根 K 线填充 RingBuffer。
- 消费组首次创建是否从 `$` 或 `0` 开始应由配置控制。

## P1: AI 参数和情绪被读取，但具体策略没有应用

位置:
- `strategy/base_strategy.py:58-101`
- `strategy/main.py:270-297`
- `strategy/strategies/ema_cross.py`
- `strategy/strategies/breakout.py`

风险:
- `StrategyEngine` 会读取 `ai:sentiment:*` 和 `ai:params:*`。
- `BaseStrategy` 只保存 `_ai_params` / `_sentiment_score`。
- 具体策略逻辑没有用这些值调整阈值、仓位或过滤信号。

结果:
- AI 调参和情绪分析目前是“写入了，但没有真正影响交易”。

建议:
- 在策略执行前应用参数白名单，例如 `ema_fast`、`ema_slow`、`atr_multiplier`、`lookback`、`volume_ratio`。
- 情绪分数只作为降杠杆/减仓/过滤开仓信号，不建议直接反向交易。

## P1: 前端仍无法构建

验证命令:

```powershell
cd frontend
npm.cmd run build
```

当前错误:
- `Chart.tsx` 类型导入需要 `import type`。
- `lightweight-charts` v5 API 与当前 `addCandlestickSeries()` / `setMarkers()` 写法不兼容。
- `PnLChart.tsx` 的 `Bar` 没有 `cell` prop，应使用 `<Cell />`。
- `TradeTable.tsx` 未使用 `TradeRecord`。

## P1: WebSocket 建连但 StatusBar 没有订阅行情频道

位置:
- `frontend/src/hooks/useWebSocket.ts:53-67`
- `frontend/src/components/StatusBar.tsx:18-19`
- `api/ws_handler.py:99-100`

风险:
- 后端只向订阅了对应 channel 的连接广播。
- `StatusBar` 只创建连接，没有调用 `subscribe()`。

结果:
- 实时价格 `updatePrice()` 不会收到 market 数据。

建议:
- 根据后端 symbols 订阅 `market:btcusdt`、`market:ethusdt`。
- 或后端支持 `market:*` 通配订阅。

## P1: 控制接口没有鉴权，暴露后可以被任意调用

位置:
- `api/routes/control.py:34`
- `api/routes/control.py:88`
- `api/main.py:72`

风险:
- `/api/close-all` 可全平。
- `/api/emergency-order` 可直接市价下单。
- `/api/pause` 可暂停策略。
- CORS 当前允许 `*`。

建议:
- 至少增加管理 token / API key 鉴权。
- 生产环境限制 CORS origin。
- 对紧急下单接口增加 symbol 白名单、side 枚举、数量上限和二次确认字段。

## P2: 前端控制面 symbol 写死为 BTCUSDT / ETHUSDT

位置:
- `frontend/src/components/ControlPanel.tsx:12`
- `frontend/src/components/ControlPanel.tsx:88-89`

风险:
- 后端 `settings.symbols` 变化后，前端控制面不会同步。

建议:
- 增加 `/api/config` 或 `/api/symbols`。
- 前端从后端读取可交易 symbol 列表。

## P2: 账户 API 失败时返回 200 + error，前端类型不匹配

位置:
- `api/routes/account.py:16-27`
- `frontend/src/lib/api.ts:19-26`

风险:
- `get_account()` 失败时返回 `{"error": "..."}`，HTTP 状态仍是 200。
- 前端按 `AccountInfo` 类型处理，可能出现 `undefined.toFixed` 一类问题。

建议:
- 后端失败时抛 `HTTPException(status_code=502, ...)`。
- 前端统一展示错误状态。

## 建议优先级

1. 先修 `.env.example` / compose 网络配置，否则一键部署不可靠。
2. 修 AI ClickHouse 连接，否则调参任务不可用。
3. 补止损止盈 / CLOSE 机制，否则自动交易风险过高。
4. 修前端 build 和 WebSocket 订阅，恢复界面可用性。
5. 给控制接口加鉴权，再考虑生产环境暴露。

---

# 新版复查 v3

更新时间: 2026-05-31

本轮根据最新改动复查了配置、Docker Compose、AI 调度、策略 warmup、AI 参数应用、执行器止损止盈、前端构建和 WebSocket。

## 已修复 / 有进展

- `executor/main.py` 继续保持订单事件 JSON 反序列化，`pending_signals` 也会在下单后和异常路径释放。
- `docker-compose.yml` 已为应用容器覆盖 `REDIS_URL` / `PG_DSN` / `CLICKHOUSE_HOST`，容器网络问题基本修复。
- `ai/scheduler.py` 已连接 ClickHouse，`docker-compose.yml` 也给 `ai` 增加了 ClickHouse 依赖。
- `strategy/main.py` 已增加 REST warmup，重启后不必等 30 根新 K 线。
- `ema_cross.py` / `breakout.py` 已开始应用 AI 参数白名单。
- `executor/order_manager.py` 已优先用 `ap` 作为成交均价，降低分批成交 PnL 偏差。
- `StatusBar.tsx` 已订阅 `market:btcusdt` 和 `market:ethusdt`。
- `PnLChart.tsx` 和 `TradeTable.tsx` 的构建错误已修复。
- `api/routes/account.py` 账户 API 失败时已改为 502。

验证:

```powershell
python -m compileall -q ai api collector common executor strategy
```

结果: 通过。

## P0: `SYMBOLS=BTCUSDT,ETHUSDT` 仍然会导致启动失败

位置:
- `.env.example:28`
- `common/config.py:51`
- `common/config.py:57`

现状:
- 这一版加了 `@field_validator("symbols", mode="before")`。
- 但 pydantic-settings 在进入 validator 前仍会先把复杂类型按 JSON decode。
- 所以逗号分隔字符串仍然会在 settings source 阶段报错。

验证:

```powershell
$env:SYMBOLS='BTCUSDT,ETHUSDT'
python -c "from common.config import Settings; print(Settings().symbols)"
```

结果: 仍然抛 `SettingsError: error parsing value for field "symbols"`。

建议:
- 最快修复: `.env.example` 改为 `SYMBOLS=["BTCUSDT","ETHUSDT"]`。
- 若必须兼容逗号格式: 使用 pydantic-settings 的 `NoDecode` 注解或自定义 settings source，让 validator 接收到原始字符串。

## P0: 数据补偿写 ClickHouse 的新代码不可用

位置:
- `collector/rest_client.py:428-429`
- `common/clickhouse.py:131`

问题:
- `collector/rest_client.py` 调用了 `clickhouse_client.batch_insert("klines_1m", to_compensate)`。
- `ClickHouseClient` 没有 `batch_insert()` 方法。
- 当前实际表名是 `klines`，不是 `klines_1m`。

结果:
- compensator 发现缺失后仍只能补 Redis。
- ClickHouse 历史缺口仍然存在，AI 调参和历史分析仍会受影响。

建议:
- 改成循环 `await clickhouse_client.insert("klines", cleaned_kline)`，字段要去掉 `is_closed` / `source`。
- 或在 `ClickHouseClient` 增加真正的 async `batch_insert(table, rows)`，并使用正确表名 `klines`。

## P0: 止损止盈保护单可能被交易所拒绝，开仓后仍可能裸奔

位置:
- `executor/order_manager.py:512-519`
- `executor/order_manager.py:536-543`

问题:
- 当前保护单同时传了 `closePosition="true"` 和 `reduceOnly="true"`。
- Binance USD-M Futures 的 Close-All 条件单通常不能和 `reduceOnly` 一起传。
- `closePosition=true` 也不需要 `quantity`，但当前逻辑虽然没有传 quantity，`reduceOnly` 仍可能导致接口拒绝。

影响:
- 开仓成交后 `_place_sl_tp()` 可能两张保护单都失败。
- 代码只打 warning，不会回滚开仓或触发紧急处理。

建议:
- 使用二选一方案:
  - Close-All 方案: `STOP_MARKET/TAKE_PROFIT_MARKET + closePosition=true`，不要传 `reduceOnly` 和 `quantity`。
  - 固定数量方案: 传 `quantity=filled_qty + reduceOnly=true`，不要传 `closePosition=true`。
- 如果止损单失败，应至少发送高优先级告警，必要时市价平仓。

## P1: 杠杆设置失败后仍继续下单

位置:
- `executor/order_manager.py:149`
- `executor/order_manager.py:481-496`

问题:
- `_ensure_leverage()` 调用失败只记录异常。
- `place_order()` 不检查设置结果，仍继续下单。
- 另外它直接调用 `rest_client._request()` 私有方法，接口边界不清晰。

影响:
- 实际交易杠杆可能和信号不一致。

建议:
- 在 `BinanceRESTClient` 增加公开方法 `set_leverage(symbol, leverage) -> bool`。
- `_ensure_leverage()` 返回 bool；失败时拒单并释放 pending signal。

## P1: 前端构建仍失败，但范围缩小到 Chart.tsx

验证命令:

```powershell
cd frontend
npm.cmd run build
```

当前错误:
- `frontend/src/components/Chart.tsx:6` 类型需要 `import type`。
- `frontend/src/components/Chart.tsx:52` `addCandlestickSeries()` 不兼容 lightweight-charts v5。
- `frontend/src/components/Chart.tsx:88` `setMarkers()` 不兼容当前类型/API。

结果:
- `PnLChart.tsx` 和 `TradeTable.tsx` 的错误已消失。
- 前端仍不能生产构建。

## P1: WebSocket Hook 卸载后仍会自动重连

位置:
- `frontend/src/hooks/useWebSocket.ts:28-31`
- `frontend/src/hooks/useWebSocket.ts:46-50`

问题:
- cleanup 里调用 `ws.close()`。
- `onclose` 总是 `setTimeout(connect, 3000)`。
- 组件卸载或 React 严格模式重复挂载时，可能产生后台重连和多连接。

建议:
- 增加 `shouldReconnectRef`。
- cleanup 时设为 false，并 clear pending reconnect timer。

## P1: 管理接口鉴权默认仍是关闭状态

位置:
- `common/config.py:91`
- `api/main.py:84-92`

现状:
- 新增了 `admin_api_key` 和中间件，这是进展。
- 但默认空字符串时直接跳过鉴权。
- `.env.example` 里也还没有 `ADMIN_API_KEY` 示例。

建议:
- `.env.example` 增加 `ADMIN_API_KEY=change_me`。
- 生产环境如果为空，应启动失败或至少 warning。

## P2: StatusBar 行情订阅仍然写死 BTC/ETH

位置:
- `frontend/src/components/StatusBar.tsx:25-26`

影响:
- 如果 `settings.symbols` 改了，前端实时价格不会跟随。

建议:
- 提供 `/api/config` 或 `/api/symbols`。
- 前端按后端 symbol 列表订阅。

## 当前验证结论

- 后端 Python 编译: 通过。
- 前端构建: 失败，剩 `Chart.tsx`。
- `SYMBOLS` 逗号格式: 失败。
- 这版修复了不少主链路问题，但仍建议先处理 P0 三项: `SYMBOLS` 配置、ClickHouse 补偿写入、止损止盈保护单参数。
