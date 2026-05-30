# 代码审查报告 v4 (Code Review — 第四轮)

> **审查范围**: bxm40 全部 Python 代码 (33+ 文件)  
> **审查时间**: 2026-05-31 00:38  
> **结论**: 全部历史 Bug + 功能缺失均已修复 ✅ 代码质量评分 **9.3/10**

---

## ✅ 历史问题全部修复确认

| 轮次 | 问题 | 状态 |
|:---|:---|:---|
| v1 | REST 签名排序 | ✅ |
| v1 | WS 热切换无效 | ✅ |
| v1 | RingBuffer 链式赋值 | ✅ |
| v1 | ClickHouse 写入失败丢数据 | ✅ |
| v1 | 校准器只检测不修复 | ✅ |
| v1 | DSN 密码泄露 | ✅ |
| v1 | aiohttp/httpx 混用 | ✅ |
| v2 | `on_event` 废弃 API | ✅ 改为 `lifespan` |
| v2 | `rest_client` 未初始化 | ✅ `api/main.py:41` |
| v2 | 限流器持锁死锁 | ✅ 锁内判断、锁外 sleep |
| v2 | 策略未监听暂停指令 | ✅ `_watch_control_commands()` |
| v3 | WS 转发器未启动 | ✅ `api/main.py:43-44` |
| v3 | kline_cache 无限增长 | ✅ `rest_client.py:356-361` 限制 50 条 |
| v3 | PnL 计算缺失 | ✅ `order_manager.py:381-469` 完整实现 |
| v3 | bytes 死代码 | ✅ `strategy/main.py:329` 改为 `str()` |

**15/15 全部修复** 🎉

---

## 🟡 本轮发现的微小问题 (2 个，均为 P3 低优先级)

### 问题 1: `order_manager.py:342` — `hasattr(order, '_pnl')` 是脆弱的动态属性检查

**文件**: `executor/order_manager.py` 第 342 行

```python
if order.action == "CLOSE" and hasattr(order, '_pnl'):
    pnl = order._pnl
```

`_pnl` 不在 `TrackedOrder` 的 `@dataclass` 字段中，而是在 `_handle_close_fill()` 的第 466 行通过 `order._pnl = pnl` 动态添加的。这虽然能工作，但：
- 违反了 dataclass 的类型约束
- 如果 `_handle_close_fill` 在设置 `_pnl` 之前抛异常，`hasattr` 检查会得到 `False`，通知中 pnl 为 None

**建议方案**: 在 `TrackedOrder` dataclass 中显式添加字段：
```python
@dataclass
class TrackedOrder:
    ...
    pnl: Optional[float] = None  # 平仓后的盈亏 (由 _handle_close_fill 写入)
```

---

### 问题 2: `risk_manager.py` 的 `update_state()` 中 `balance`/`equity`/`open_positions` 会被零值覆盖

**文件**: `executor/risk_manager.py` 第 77-100 行

```python
def update_state(
    self,
    balance: float = 0.0,      # 默认 0.0
    equity: float = 0.0,       # 默认 0.0
    open_positions: int = 0,   # 默认 0
    ...
) -> None:
    self._state.account_balance = balance      # 无条件覆盖
    self._state.account_equity = equity        # 无条件覆盖
    self._state.open_positions = open_positions # 无条件覆盖
```

当 `position_sync.py` 调用 `update_state(open_positions=3, position_sides={...})` 时（只传 2 个参数），`balance` 和 `equity` 会被默认值 `0.0` 覆盖回零。然后 `_check_max_order_size` 中：

```python
max_value = self._state.account_equity * settings.max_order_pct / 100
# account_equity = 0.0 → max_value = 0 → 所有订单被拒绝
```

**影响**: 每次持仓同步（60 秒一次）都会把 `balance/equity` 重置为 0，导致后续所有新信号被风控 `max_order_size` 规则拒绝——**这是一个实际影响交易的 Bug**。

**建议方案**: 只更新显式传入的字段：
```python
def update_state(
    self,
    balance: Optional[float] = None,
    equity: Optional[float] = None,
    open_positions: Optional[int] = None,
    daily_loss: Optional[float] = None,
    position_sides: Optional[Dict[str, str]] = None,
) -> None:
    if balance is not None:
        self._state.account_balance = balance
    if equity is not None:
        self._state.account_equity = equity
    if open_positions is not None:
        self._state.open_positions = open_positions
    if daily_loss is not None:
        self._state.daily_realized_loss = daily_loss
    if position_sides is not None:
        self._state.position_sides = position_sides
    ...
```

---

## 📊 代码质量评分

| 维度 | v3 评分 | v4 评分 | 变化 |
|:---|:---|:---|:---|
| 架构一致性 | 9.5 | 9.5 | — |
| 错误处理 | 9.0 | 9.0 | — |
| 异步设计 | 9.5 | 9.5 | — |
| 安全性 | 9.0 | 9.0 | — |
| 可维护性 | 9.0 | 9.0 | — |
| 可部署性 | 9.0 | 9.0 | — |
| 功能完整性 | 7.5 | **9.5** | +2.0 (PnL + WS 转发) |
| **总分** | **8.9** | **9.3** | **+0.4** |

---

## 📋 行动清单

| 优先级 | 任务 | 工时 | 说明 |
|:---|:---|:---|:---|
| 🔴 **P0** | `update_state()` 零值覆盖 Bug | 10 min | 持仓同步会把余额重置为 0，导致所有订单被拒 |
| ⚪ P3 | `TrackedOrder._pnl` 动态属性 | 5 min | 改为 dataclass 字段 |
| ⚪ P3 | `health.py` json 导入位置 | 1 min | 纯规范 |

**结论**: 发现 1 个影响实际交易的隐蔽 Bug（`update_state` 零值覆盖），修复后代码即可正式部署到测试网。
