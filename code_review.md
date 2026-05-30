# 代码审查报告 (Code Review)

> **审查范围**: bxm40 项目全部 Python 代码 (30+ 文件)  
> **审查时间**: 2026-05-31  
> **结论**: 整体架构合理、代码质量较高，发现 **3 个必须修复的 Bug** + **5 个建议优化项**

---

## 🔴 必须修复 (Critical Bugs)

### Bug 1: REST 签名参数排序方式有安全隐患

**文件**: `collector/rest_client.py` 第 128 行

```python
# 当前代码 — 错误
query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
```

**问题**: 币安签名要求参数按**原始顺序**拼接，而非按 key 字母排序。当前用 `sorted()` 会改变参数顺序，部分接口可能因签名不匹配被拒绝（尤其是含有多个同类参数时）。

**修复**:
```python
# 正确 — 保持原始插入顺序
query = "&".join(f"{k}={v}" for k, v in params.items())
```

---

### Bug 2: WebSocket 热切换不会真正切换消息流

**文件**: `collector/ws_client.py` 第 211-246 行

**问题**: `_schedule_hot_swap()` 创建了新的 WS 连接并替换 `self._ws`，但实际的消息接收循环在 `_connect_and_listen()` 中的 `async for raw_message in ws:` 仍然绑定的是旧的 `ws` 局部变量。替换 `self._ws` 并不会改变正在迭代的对象。

```python
# _connect_and_listen 中:
async with websockets.connect(...) as ws:  # 局部变量 ws
    self._ws = ws                          # 存了引用
    async for raw_message in ws:           # 遍历的是局部 ws，不是 self._ws
        ...
```

**修复方案**: 热切换应该通过让当前连接正常退出 → 外层 `while self._running` 自动重连来实现，而不是在运行中直接替换连接引用。

```python
async def _schedule_hot_swap(self) -> None:
    """24h 热切换 — 通过主动关闭旧连接触发重连"""
    wait_seconds = 24 * 3600 - settings.ws_24h_reconnect_offset
    await asyncio.sleep(wait_seconds)
    if not self._running:
        return
    logger.info("ws.hot_swap_triggering")
    # 主动关闭当前连接，外层 while 循环会自动重连
    if self._ws:
        await self._ws.close()
```

---

### Bug 3: RingBuffer `iloc` 赋值不会生效

**文件**: `strategy/ring_buffer.py` 第 82-84 行

```python
# 当前代码 — 链式赋值 (Chained Assignment)，pandas 不保证写入
for col in new_row:
    if col in self._df.columns:
        self._df.iloc[-1][col] = new_row[col]  # ⚠️ SettingWithCopyWarning
```

**问题**: `self._df.iloc[-1][col]` 是链式索引 (chained indexing)，在 Pandas 中是**不安全的赋值操作**，可能只修改了一个临时副本，原始 DataFrame 不会被更新。

**修复**:
```python
# 正确 — 使用 .loc 或 .at 直接定位赋值
idx = self._df.index[-1]
for col in new_row:
    if col in self._df.columns:
        self._df.at[idx, col] = new_row[col]
```

---

## 🟡 建议优化 (Warnings)

### 优化 1: ClickHouse `_flush_table` 缓冲区丢失风险

**文件**: `common/clickhouse.py` 第 161 行

```python
rows = self._buffers.pop(table, [])  # pop 后如果写入失败，数据就丢了
```

**建议**: 失败时将数据放回缓冲区：
```python
async def _flush_table(self, table: str) -> None:
    rows = self._buffers.pop(table, [])
    if not rows or self._client is None:
        return
    try:
        columns = list(rows[0].keys())
        data = [[row.get(col) for col in columns] for row in rows]
        await asyncio.to_thread(self._client.insert, table, data, column_names=columns)
    except Exception:
        logger.exception("clickhouse.flush_error", table=table)
        # 写入失败，放回缓冲区
        self._buffers.setdefault(table, []).extend(rows)
```

---

### 优化 2: health.py 中 `import json` 应放在文件顶部

**文件**: `collector/health.py` 第 208 行

```python
# 当前 — 函数体内导入
import json
body = json.dumps({...})
```

这是一个函数体内延迟导入，应该移到文件顶部。`json` 是标准库模块，没有延迟导入的必要。

---

### 优化 3: `common/notify.py` 中 aiohttp 与 httpx 混用风险

`requirements.txt` 中同时列出了 `aiohttp` 和 `httpx`。`notify.py` 可能使用了其中一个来发钉钉/Telegram。建议统一使用 `aiohttp`（其他模块已广泛使用），或统一使用 `httpx`（更现代），避免两套 HTTP 客户端共存增加依赖和内存。

---

### 优化 4: 日志中直接打印 PostgreSQL DSN（含密码）

**文件**: `common/db.py` 第 95 行

```python
logger.info("postgres.connected", dsn=settings.pg_dsn)
```

DSN 中包含明文密码（`postgresql://bxm40:bxm40_secret@...`），会被记录到日志文件中。

**建议**: 打印时脱敏
```python
safe_dsn = settings.pg_dsn.split("@")[-1] if "@" in settings.pg_dsn else settings.pg_dsn
logger.info("postgres.connected", host=safe_dsn)
```

---

### 优化 5: `DataCompensator` 发现缺失后没有实际补写 Redis

**文件**: `collector/rest_client.py` 第 367-408 行

`_compensate()` 方法在发现缺失 K 线后只打了日志，没有实际将 REST 拉取的数据补写到 Redis Streams。校准只做了"检测"而没做"修复"。

**建议**: 在发现缺失时，调用 `redis_client.xadd()` 将 REST 数据写入 Redis，确保下游策略能收到完整数据。

---

## ✅ 代码质量亮点

| 方面 | 评价 |
|:---|:---|
| **架构一致性** | ✅ 完全符合 architecture_design.md 的设计，模块划分清晰 |
| **异步设计** | ✅ 全异步 asyncio，无阻塞调用（ClickHouse 用 `to_thread` 包装） |
| **错误处理** | ✅ 每个模块都有 try-except + 结构化日志，不会因单个异常崩溃 |
| **生命周期管理** | ✅ 每个服务都有 `start()` / `stop()` 方法，优雅退出 |
| **风控设计** | ✅ 7 条规则独立函数，返回结构化结果，便于追溯 |
| **信号标准化** | ✅ Signal dataclass + UUID，全链路可追踪 |
| **Docker 部署** | ✅ docker-compose 配置完整，healthcheck 齐全 |
| **代码规范** | ✅ 类型注解完整，docstring 清晰，命名规范 |

---

## 📋 修复优先级总结

| 级别 | 问题 | 文件 | 影响 |
|:---|:---|:---|:---|
| 🔴 P0 | REST 签名排序 | `rest_client.py:128` | 签名校验失败导致无法下单 |
| 🔴 P0 | WS 热切换无效 | `ws_client.py:211-246` | 24h 后不会真正切换，仍会被币安强断 |
| 🔴 P0 | RingBuffer 链式赋值 | `ring_buffer.py:82-84` | K 线数据不更新，指标计算出错 |
| 🟡 P1 | ClickHouse 写入失败丢数据 | `clickhouse.py:161` | 历史数据缺失 |
| 🟡 P1 | 校准器只检测不修复 | `rest_client.py:367-408` | WS 丢包后数据无法自动补回 |
| 🟡 P2 | DSN 密码泄露到日志 | `db.py:95` | 安全风险 |
| 🟡 P2 | json 导入位置 | `health.py:208` | 代码规范 |
| 🟡 P2 | aiohttp/httpx 混用 | `requirements.txt` | 依赖冗余 |

**要我立刻帮你修复这 3 个 P0 级别的 Bug 吗？**
