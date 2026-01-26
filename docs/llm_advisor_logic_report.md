# LLMAdvisor 模块实现报告

## 1. 概述
`LLMAdvisor` (`src/trading_skills/llm_advisor.py`) 是系统的核心决策模块，旨在结合**定量技术分析**与**大语言模型 (LLM)** 的推理能力，实现智能化的期货交易决策。它负责从市场扫描、初始建仓到持仓监控、动态风控的全生命周期管理。

## 2. 核心类与数据结构

### 2.1 `LLMDecision` (数据类)
用于标准化 LLM 的决策输出：
- `action`: 动作 (OPEN, CLOSE, HOLD, WAIT, BUY, SELL)。
- `direction`: 方向 (LONG, SHORT, NONE)。
- `reasoning`: 决策理由。
- `confidence`: 置信度。
- `suggested_params`: 建议参数（杠杆、金额、止损、止盈、部分止盈价格/比例）。

### 2.2 `_SymbolState` (内部状态类)
用于跟踪每个标的的运行时状态，确保策略执行的连续性：
- `initial_entry_qty`: 初始建仓数量（作为加仓/减仓的基准）。
- `scalp_peak_price` / `scalp_trough_price`: 记录持仓期间的最高/最低价，用于回调加仓判断。
- `last_scalp_add_ts`: 上次自动加仓的时间戳（用于冷却控制）。
- `profit_locked`: 利润锁定标志（当浮盈达到一定比例后置为 True）。

### 2.3 `LLMAdvisor` (主类)
- **依赖**: `FuturesTrader` (执行交易), `SmartAnalyzer` (定量分析)。
- **配置**: 加载 API Key、Base URL、模型名称 (默认 `deepseek-ai/DeepSeek-V3`)。
- **缓存**: `_analysis_cache` 缓存定量分析报告 (60秒有效)，减少重复计算。

---

## 3. 核心业务流程

### 3.1 初始建仓咨询 (`ask_llm`)
当系统发现潜在交易机会时调用此方法。

1.  **获取分析报告 (`get_analysis_report`)**:
    - 调用 `SmartAnalyzer` 生成定量评分 (`score`)、方向评分 (`direction_score`)、信号、风险因子等。
    - 包含 ATR、成交量比率、订单簿不平衡度等关键指标。
2.  **构建 Prompt (`_construct_prompt`)**:
    - 角色设定：执行交易员 (Execution Trader)。
    - 输入：账户余额、技术分析报告详细数据。
    - 指导原则：信任定量筛选，寻找最佳入场点；顺势而为；严格风控。
    - 输出要求：JSON 格式，包含动作、方向、理由、建议参数（止损、止盈、首个部分止盈位）。
3.  **调用 LLM (`_call_llm`)**:
    - 发送请求至 LLM API。
    - 解析 JSON 响应，转换为 `LLMDecision` 对象。

### 3.2 交易执行 (`execute_trade`)
根据 LLM 的决策执行实际下单。

1.  **参数准备**:
    - 设置杠杆。
    - 计算下单金额：如果未指定，调用 `_calc_default_usdt` (余额的 5%，最小 20U)。
2.  **市价开仓**:
    - 调用 `trader.place_market_entry_by_usdt`。
    - 获取成交数量 (`executed_qty`) 和均价 (`avg_price`)。
3.  **状态初始化**:
    - 记录 `initial_entry_qty`。
    - 重置 `profit_locked` 为 False。
    - 初始化 `scalp_peak_price` / `scalp_trough_price`。
4.  **风控挂单**:
    - **止损 (SL)**: 立即下市价止损单 (`STOP_MARKET`)，触发后平仓 (`close_position=True`)。
    - **止盈 (TP)**: 立即下市价止盈单 (`TAKE_PROFIT_MARKET`)，触发后平仓。
    - **部分止盈 (Partial TP)**: 下限价只减仓单 (`LIMIT`, `reduceOnly=True`)，用于在第一目标位锁定部分利润（通常为 50%）。

### 3.3 持仓监控与动态管理 (`monitor_position`)
这是模块最复杂的部分，负责在持仓期间不断评估风险和机会。

#### A. 状态同步与校验
1.  **获取持仓**: 检查当前是否有持仓，无持仓则清理状态。
2.  **更新分析**: 获取最新价格和技术指标。
3.  **订单校验 (`_validate_existing_orders`)**:
    - 检查现有的 SL/TP/Limit 单方向是否正确。
    - **逻辑一致性检查**:
        - 多单的 SL 必须 < 当前价，TP 必须 > 当前价。
        - 空单的 SL 必须 > 当前价，TP 必须 < 当前价。
        - **Limit 单检查**: 防止价格已穿过 Limit 单但未成交的异常情况。
    - 发现无效订单立即取消。

#### B. 利润锁定逻辑 (`profit_locked`)
- 计算当前浮盈比例 (`pnl_pct`)。
- **规则**: 如果 `pnl_pct > 1.5%`，将 `profit_locked` 标记为 `True`。
- **作用**: 一旦利润锁定，后续禁止放宽止损，且允许触发自动加仓逻辑。

#### C. 自动加仓/剥头皮 (`_handle_auto_scalp`)
在特定条件下触发**自动**（非 LLM）加仓逻辑，用于利用回调增加收益。
- **触发条件**:
    - 利润已锁定 (`profit_locked=True`)。
    - 有止损保护。
    - 没有未完成的部分止盈单。
    - 冷却时间已过 (240秒)。
    - 当前仓位未显著超过初始仓位（防止过度加仓）。
- **加仓信号**:
    - **多单**: 价格从高点 (`peak`) 回调一定幅度，且方向分 (`dir_score`) 依然看多。
    - **空单**: 价格从低点 (`trough`) 反弹一定幅度，且方向分依然看空。
- **执行**:
    - 市价加仓。
    - **立即挂出部分止盈单**: 加仓成交后，立刻根据 ATR 计算动态止盈位 (`_calc_dynamic_partial_tp_price`)，挂出对应的 Limit 减仓单。这构成了“低吸高抛”的微操作。

#### D. LLM 动态风控 (`_run_llm_monitor`)
如果未触发自动加仓，则咨询 LLM 进行高级风控。

1.  **智能建议生成 (Smart Suggestions)**:
    - **保本损 (Break Even)**: 浮盈 > 1.5% 时，建议止损移至开仓价附近。
    - **移动止损 (Trailing Stop)**: 浮盈 > 3.0% 时，建议按 ATR 动态上移止损。
2.  **构建 Monitor Prompt**:
    - 提供当前持仓数据（浮盈、均价、持仓量）。
    - 提供现有挂单状态。
    - 提供智能建议。
    - 询问动作: `CLOSE`, `REDUCE`, `ADD`, `ADJUST_TP_SL`, `HOLD`。
3.  **执行 LLM 决策**:
    - **CLOSE**: 直接市价全平。
    - **REDUCE**: 市价减仓。
    - **ADD**: 仅在 `profit_locked=True` 时允许加仓。
    - **ADJUST_TP_SL**:
        - 调整止损/止盈。
        - **安全检查**: 如果利润已锁定，**拒绝**任何试图放宽止损（让止损变远）的操作。
        - **操作**: 先取消所有旧挂单，再重新下达新的 SL/TP 组合，确保订单整洁。

## 4. 关键算法细节

### 4.1 动态部分止盈价格 (`_calc_dynamic_partial_tp_price`)
用于计算自动加仓后的即时止盈位。
- 基于 **ATR (平均真实波幅)**。
- 目标利润距离 = `ATR * 0.8` (范围 0.5% ~ 1.5%)。
- 确保目标价至少有微小的盈利空间（多单 > 当前价 * 1.002）。

### 4.2 动态默认金额 (`_calc_default_usdt`)
- 默认为账户余额的 5%。
- 设有下限 20 USDT。

## 5. 总结
`LLMAdvisor` 实现了一个**半自动、人机结合**的交易策略：
1.  **定量筛选**负责“选股”。
2.  **LLM** 负责“择时”和“制定计划”。
3.  **规则引擎** (`monitor_position`) 负责高频的“风控”和“自动剥头皮”。
4.  **安全机制**（如利润锁定、无效订单清理、加仓限制）贯穿始终，确保系统在自动化运行时的资金安全。
