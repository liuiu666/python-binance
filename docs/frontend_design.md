# BXM40 量化研究与沙盒平台 — 前端技术架构与设计规范 (研发/回测优先版)

根据最新的研发路线，系统重心已调整为**“量化研究与沙盒模拟”**阶段。真实交易模块延后，前端将围绕 **数据采集管理、历史数据分析、策略编辑与参数调整、回测引擎交互、以及模拟交易 (Paper Trading) 看板** 展开。

本项目前端位于 `frontend/` 目录，基于 **Vite 8 + React 19 + TypeScript 6** 搭建。本设计规范为该项目的开发提供了完整的技术方案与界面规划。

---

## 🏗️ 架构设计与技术选型

为了支持代码编辑、高频图表和策略回测等重度数据展现，前端核心依赖进行了专项配置：

| 模块 | 技术选型 | 优势与作用 |
| :--- | :--- | :--- |
| **主框架** | React 19 + TypeScript | 最新 React 渲染引擎，严格类型系统。 |
| **状态管理** | Zustand 5 | 极轻量的状态机，管理 WebSocket 实时行情及回测任务流。 |
| **K 线走势图** | TradingView Lightweight Charts 5 | 负责展示历史 K 线、回测成交信号（Buy/Sell 标记）及模拟交易实时 Tick。 |
| **数据分析图表** | Recharts 3 | 渲染回测资金曲线 (NAV)、回撤曲线、成交量泊松分布直方图。 |
| **策略编辑** | @monaco-editor/react (Monaco) | 微软 VS Code 核心编辑器 React 版，支持 Python 语法高亮和动态代码提交。 |
| **样式方案** | Vanilla CSS (暗黑科技风格) | 针对大屏幕、密集数据的交易台做定制化高清晰度暗黑样式。 |

---

## 🖥️ 核心页面与功能设计

### 1. 数据采集与存储管理面板 (Collector & Storage Manager)
该模块用于控制和监控底层数据采集器，防止磁盘满并提供对账工具。
*   **采集器监控器**:
    *   显示各 Symbol（如 `BTCUSDT`, `ETHUSDT`）的 WS 心跳延迟、最后消息接收时间、累积数据量。
    *   提供开关控制采集子任务：`[ 启动 WS 监听 ]` / `[ 停止 WS 监听 ]`。
*   **存储分析**:
    *   ClickHouse 历史 K 线表、Tick 表占用的磁盘空间。
    *   Redis Streams 中各 Stream（`market:*`, `depth:*`）的队列长度。
    *   一键清理数据操作：`[ 截断 90 天前历史数据 ]` (调用后端 ClickHouse TTL 触发命令)。
*   **校准对账小工具**:
    *   显示近 24 小时 REST 补偿器（DataCompensator）自动对账补齐的 K 线条数。
    *   提供手动对账触发按钮：选择 `时间段` 与 `Symbol`，一键拉取 REST 覆盖 ClickHouse 缺失块。

### 2. 探索性数据分析工作区 (Exploratory Data Analysis - EDA)
此模块协助用户在开发策略前进行数据探矿，特别是检验泊松成交量模型。
*   **直方图与拟合曲线**:
    *   使用 `Recharts` 绘制选定时间段内，1 分钟成交量 / 成交笔数（`trades_count`）的分布直方图。
    *   叠加**泊松分布拟合曲线**，计算并展示均值 $\lambda$（Lambda）、超额离散度（Overdispersion Factor）等统计参数。
*   **指标异常率分析 (Anomaly Analysis)**:
    *   显示自定义 z-score 阈值下的异常判定比例（例如：设定 $z > 3.0$ 时，历史数据中有 $1.2\%$ 的 K 线为异常量能）。
    *   支持多交易对相关性矩阵图展示（展示 BTC 波动与 ETH 波动在不同时间窗口下的 correlation 矩阵）。

### 3. 策略代码编辑与参数配置器 (Strategy Editor & Config)
不重新编译后端的情况下，动态调整策略逻辑与风控边界。
*   **Monaco 代码编辑器**:
    *   左侧为策略文件树（`strategies/ema_cross.py`，`strategies/poisson_anomaly.py` 等）。
    *   中间为 Monaco 编辑器，支持 Python 语法高亮、缩进、行号。
    *   点击 `[ 保存并重载策略 ]`，通过后端 API 写入策略文件，并触发 StrategyEngine 的热重启。
*   **参数配置网格 (Schema-based UI)**:
    *   前端解析后端的策略参数 JSON Schema，自动渲染表单：
      - 滑块调节：`ATR Multiplier` (1.5 ~ 4.0)、`RSI Period` (7 ~ 30)
      - 输入框：`max_order_pct` (单笔净值百分比限制)
      - 选项：`stop_loss_type` (ATR / Fixed)

### 4. 高级回测引擎控制台 (Backtest Studio)
核心模块。在历史数据上检验策略收益率。
*   **回测配置面板**:
    *   `策略选择`: 下拉框（均线交叉 / 泊松量能异常 / 突破）
    *   `标的与周期`: 交易对选择（支持多选）、K 线周期（1m, 5m, 15m, 1h）
    *   `回测区间`: 日期选择器（精确到分钟）
    *   `账户初始条件`: 初始资金 (默认 10,000 USDT)、最大杠杆 (1x ~ 10x)
    *   `交易摩擦`: 佣金率（默认 0.05%）、单边滑点（USDT）
*   **回测运行态**:
    *   点击 `[ 开始回测 ]` 后，展示进度条（%）与每秒处理 K 线的速度。
*   **回测结果可视化**:
    *   **资产曲线 (NAV)**: Recharts 折线图，展示净资产随时间的变化，叠加基准收益率（Buy & Hold）。
    *   **回撤曲线 (Drawdown Chart)**: 实时展示回撤幅度百分比。
    *   **结果统计卡片**: 
      - 年化收益率 (CAGR)、最大回撤比 (Max Drawdown %)、夏普比率 (Sharpe Ratio)、索提诺比率 (Sortino Ratio)
      - 胜率 (Win Rate %)、总交易笔数、盈亏比 (Profit Factor)
    *   **K线交易点回放**: 在 `lightweight-charts` 上加载回测期间的 K 线，并在生成信号的 Open/Close 点标上红色（卖出平仓）、绿色（买入开仓）箭头。

### 5. 模拟交易沙盒看板 (Paper Trading Workspace)
使用真实 WebSocket 行情，但在纯内存虚拟环境中跑单。
*   **沙盒钱包卡片**:
    *   虚拟 USDT 余额、锁定的仓位保证金、以及虚拟总资产净值（随实时 WebSocket 标记价波动）。
*   **虚拟持仓格**:
    *   显示模拟交易器的仓位。字段包括：Symbol、方向、大小、开仓均价、当前市价、未实现盈亏。
*   **虚拟成交历史**:
    *   按时间滚动刷新虚拟委托单（Simulated Orders）状态（已提交、部分成交、已成交、已撤单）。

---

## 🔌 API 端点设计 (Research & Sandbox 专属)

为配合前端实现，后端 FastAPI 需要提供对应的研究与回测 API 接口。

### 1. 策略编辑 API
*   **`GET /api/strategies`**
    *   获取所有已注册的策略列表及当前生效的代码/参数。
*   **`POST /api/strategies/save`**
    *   保存策略代码并触发引擎重载。
    *   *Payload*: `{"name": "poisson_anomaly", "code": "..."}`
*   **`POST /api/strategies/config`**
    *   更新某策略的运行时参数。
    *   *Payload*: `{"strategy": "ema_cross", "params": {"ema_fast": 9, "ema_slow": 21}}`

### 2. 数据分析与拟合 API
*   **`GET /api/analysis/poisson-fit`**
    *   获取 ClickHouse 中指定标的的成交量统计分布以进行拟合。
    *   *Params*: `symbol=BTCUSDT`, `start_time=1716900000000`, `end_time=1717000000000`
    *   *Response*: 
      ```json
      {
        "lambda": 154.2, 
        "overdispersion": 1.12, 
        "data_distribution": [{"volume_bucket": 50, "observed_count": 22}, ...]
      }
      ```

### 3. 回测任务 API
*   **`POST /api/backtest/run`**
    *   提交一个异步回测任务，返回 `task_id`。
    *   *Payload*: 
      ```json
      {
        "strategy": "ema_cross",
        "symbols": ["BTCUSDT"],
        "start_time": 1716900000000,
        "end_time": 1717000000000,
        "initial_balance": 10000.0,
        "commission": 0.0005,
        "leverage": 5
      }
      ```
*   **`GET /api/backtest/status/{task_id}`**
    *   轮询回测进度。
    *   *Response*: `{"status": "RUNNING", "progress": 45.5, "error": null}`
*   **`GET /api/backtest/result/{task_id}`**
    *   获取回测完整统计结果。
    *   *Response*: 
      ```json
      {
        "metrics": {
          "total_return": 0.154,
          "cagr": 0.22,
          "max_drawdown": 0.082,
          "sharpe_ratio": 1.85,
          "win_rate": 0.58,
          "profit_factor": 1.45,
          "total_trades": 84
        },
        "equity_curve": [{"timestamp": 1716900000000, "balance": 10000.0}, ...],
        "trades": [
          {
            "trade_id": 1,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "action": "OPEN",
            "price": 68000.0,
            "quantity": 0.1,
            "timestamp": 1716900400000,
            "pnl": 0.0
          },
          ...
        ]
      }
      ```

### 4. 模拟交易 API
*   **`GET /api/paper/account`**
    *   获取模拟账户余额和保证金占用。
*   **`GET /api/paper/positions`**
    *   获取当前的虚拟持仓。
*   **`POST /api/paper/order`**
    *   向模拟器提交一个虚拟订单（直接由沙盒撮合器处理，不发往币安主网）。

---

## 🗓️ 编码与集成步骤

### 第一步: 状态管理 (Zustand)
1. 编写 `frontend/src/store/useAnalysisStore.ts` 集中管理回测任务提交、状态轮询和分析图表数据。
2. 编写 `frontend/src/store/usePaperStore.ts` 建立 WebSocket 连接接收模拟行情，驱动虚拟账户变动。

### 第二步: 数据采集管理与分析页
1. 制作 `src/components/StorageStats.tsx` 抓取 ClickHouse/Redis 存储并实现一键对账与清理。
2. 制作 `src/components/PoissonFitChart.tsx` 渲染成交量拟合曲线。

### 第三步: 策略代码编辑
1. 引入 `@monaco-editor/react`，并在 `src/components/StrategyEditor.tsx` 中编写编辑器，实现与 `/api/strategies/save` 对接。

### 第四步: 回测工作区 (核心开发)
1. 编写回测控制面板表单 `src/components/BacktestForm.tsx`。
2. 使用 `recharts` 绘制双折线图（NAV 资金走势 vs 标的现货价格），用阴影高亮标出回撤深度。
3. 配合 `lightweight-charts` 渲染 K 线，并利用 `markers` API 将 `/api/backtest/result` 里的 `trades` 交易动作打印在 K 线走势中。

### 第五步: 模拟交易沙盒对接
1. 将后端配置调成沙盒执行器（不依赖真实 API Key 即可启动）。
2. 在前端调试实时 K 线更新、模拟挂单、以及策略信号触发下的虚拟平仓 PnL 变动。
