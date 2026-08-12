# 测试文件清单

> 更新日期：2026-08-13

## Node.js 测试（test/*.test.js）

运行方式：`node --test test/*.test.js`

| 文件 | 覆盖范围 |
|------|----------|
| api_smoke.test.js | HTTP 集成：信号、数据健康、影子交易、门禁、配置、审计导入、手工命令 |
| trading_engine.test.js | 交易引擎生命周期：stop 清理影子定时器、审计缓存失效、结算缓存失效 |
| trade_history.test.js | 订单历史归一化、结算、lifecycle gate、影子审计、LLM 日志 |
| trade_config.test.js | 策略配置标准化、实盘/影子开关、回测预设 |
| auto_btc_latency.test.js | 平板脚本低延迟及关键防线 |
| auto_btc_confirm_logic.test.js | AutoJS 确认与余额逻辑、队列防重 |
| auth.test.js | API token 认证和登录 |
| event_store.test.js | 审计事件标准化和导入去重 |

## Python 测试（根目录 test_*.py）

运行方式：`python -m pytest test_*.py` 或 `python test_<name>.py`

| 文件 | 覆盖范围 | 核心依赖 |
|------|----------|----------|
| test_auction_collector.py | 竞价数据采集器：订单簿增量、交易归类、强制平仓解析 | collect_auction_data |
| test_auction_features.py | 竞价特征工程：深度变化、流动性压力、因果实证 | build_auction_features |
| test_collect_second_data.py | 秒级数据采集：CSV 修复、聚合去重、零价过滤 | collect_second_data |
| test_data_retention.py | 数据保留策略：过期清理、日志裁剪、数据库维护 | cleanup_data_retention |
| test_second_backtest.py | 秒级回测框架：信号执行、策略锁、动态 zone 过滤、V21 路由 | second_backtest/ |

## 已删除的测试

| 文件 | 删除原因 |
|------|----------|
| test_research_path_exhaustion.py | 依赖已删脚本 research_long_minute_consensus_v1、research_path_exhaustion_reclaim_v2、research_normal_liquidity_orderbook |
| test_signal_modules.py | 依赖已删脚本 research_position_auction_v1、backtest_io |
