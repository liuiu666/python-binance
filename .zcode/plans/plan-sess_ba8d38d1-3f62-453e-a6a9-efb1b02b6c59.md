# 实施计划：把 LLM 方向预测写成 python-binance 正式策略（支持影子单+实盘）

## 目标
让 LLM（GLM-5.2）方向预测成为 python-binance 的一个正式策略，和现有 V13 等策略并列：
- 数据走主链路（1m CSV 聚合，不单独拉）
- 策略嵌入 `signal_btc.py`（不单独写进程）
- 支持 prod_config 控制 `trade_enabled`（影子/实盘开关）
- 10 分钟节流（不阻塞主循环）

## 改动总览（3 个文件）

| # | 文件 | 改动 | 说明 |
|---|---|---|---|
| 1 | `py/update_live_data.py` | 保留 taker_buy_vol | fetch_klines 不再丢弃 taker 字段，1m CSV 增列 `taker_buy_vol` |
| 2 | `py/signal_btc.py` | 新增 LLM 策略类 + 注册 | 加 `LLMDirectionStrategy` 类、model_type 白名单、_make_strategy 分支 |
| 3 | `data/prod_config.json` | 加策略配置 | `BTC_10min_LLM_GLM52` 条目，`trade_enabled:false` 起步 |

## 详细设计

### 1. 补全数据采集（`update_live_data.py`）

**改动点**：`fetch_klines` 函数（第 209 行）保留 taker_buy_vol。

现在：
```python
df = df[["open_time", "open", "high", "low", "close", "volume"]]
```
改成：
```python
df = df[["open_time", "open", "high", "low", "close", "volume", "taker_buy_vol"]]
```

同样改 `backfill_kline_gaps`（第 266 行）保持一致。

效果：`btcusdt_1m.csv` 多一列 `taker_buy_vol`，所有下游都能用。

### 2. LLM 策略类（`signal_btc.py`）

新增 `LLMDirectionStrategy(Strategy)` 类，核心设计：

```python
class LLMDirectionStrategy(Strategy):
    """LLM 方向预测策略 - 每10分钟调一次大模型, 中间返回缓存结果"""
    
    def __init__(self, strategy_id, cfg):
        super().__init__(strategy_id, cfg)
        self.interval_sec = int(cfg.get("llm_interval_sec", 600))  # 10分钟
        self.last_call_time = 0
        self.last_result = None  # 缓存上次预测
    
    def predict(self, df5=None):
        now = time.time()
        # 节流: 不到10分钟, 返回上次缓存
        if now - self.last_call_time < self.interval_sec:
            return self._cached_result()
        
        # 到时间了: 用1m数据聚合多周期, 构建提示词, 调LLM
        try:
            k1m = self._load_1m_with_taker()  # 读btcusdt_1m.csv(含taker)
            if k1m is None or len(k1m) < 100:
                return self._no_signal("data_insufficient")
            
            prompt = self._build_prompt(k1m)  # 1m聚合5m/15m/1h, 纯目标提示词
            direction, confidence, reason = self._call_llm(prompt)
            
            self.last_call_time = now
            self.last_result = {
                "strategy_id": self.id,
                "signal": direction,  # UP 或 DOWN
                "direction": direction,
                "confidence": confidence,
                "model_type": "llm_direction",
                "reason": reason,
                "time": datetime.utcnow().strftime(...),
                "price": float(k1m.iloc[-1]["close"]),
                # 现有策略需要的标准字段
                "avg_prob": 0.5, "rsi_value": None, "high_conf": confidence > 0.6,
                "agree": True, "vol_ok": True, "session_gate_ok": True,
                "rsi_extreme": False, "horizon_min": 10,
            }
            return self.last_result
        except Exception as e:
            return self._no_signal("llm_error: %s" % e)
```

**数据获取（复用主链路）**：
- 读 `data/btcusdt_1m.csv`（含 taker_buy_vol，由 update_live_data 持续刷新）
- 1m → 聚合出 5m/15m/1h（resample，同源数据更准确）
- 不单独拉币安、不依赖秒级数据

**LLM 调用**（10 分钟一次，不阻塞）：
- 主循环每秒调 predict → 内部检查距上次调用是否满 10 分钟
- 没满：返回缓存的上次结果（signal 保留）
- 满了：真正调 GLM-5.2（耗时 30-60 秒，主循环会等这一次）
- GLM key 从 prod_config 读（明文配置，方便部署改）

**注册**（3 处改动）：
1. `_make_strategy`（5466行）：加 `if cfg.get("model_type") == "llm_direction": return LLMDirectionStrategy(sid, cfg)`
2. `build_strategies`（5504行）：model_type 白名单加 `"llm_direction"`
3. `apply_trend_mode_switch`（5520行）：不加 LLM 策略（LLM 不受趋势模式过滤）

### 3. prod_config 配置（`data/prod_config.json`）

```json
"BTC_10min_LLM_GLM52": {
    "enabled": true,
    "trade_enabled": false,
    "model_type": "llm_direction",
    "llm_interval_sec": 600,
    "llm_api_url": "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions",
    "llm_api_key": "4b4ff2daff0443e4b676d944c62d22a0.KKmdwVSvNx2Frzyf",
    "llm_model": "glm-5.2",
    "llm_max_tokens": 8000,
    "horizon_min": 10,
    "symbol": "btcusdt"
}
```

`trade_enabled: false` 起步 → 走影子单路径（placeShadowTrade）。
验证稳定后改 `true` → 接入实盘（平板 auto_btc.js 自动下单）。

## 关键设计决策

1. **1m 聚合多周期**：不单独拉 5m/15m/1h，用 1m resample 聚合。同源数据，避免不同接口数据不一致。
2. **10 分钟节流**：predict 每秒被调，但内部只有满 10 分钟才真正调 LLM。代价是主循环每 10 分钟会阻塞一次约 30-60 秒（LLM 思考时间），其他策略在那 1 分钟内暂停。
3. **复用现有信号链路**：predict 返回标准格式 → 写 live_signals.json → server.js 门控 → /api/signal → 平板/影子。完全和 V13 等策略一样的路径，不需要改 server.js。
4. **GLM key 在 prod_config**：明文存储，部署到服务器后直接改配置文件换 key，不用环境变量。

## 不做的事（明确边界）

- 不改 server.js（信号门控/下单链路不变）
- 不改 auto_btc.js（平板下单不变）
- 不单独写进程（嵌入 signal_btc.py 主循环）
- 不单独拉币安（用主链路的 1m CSV 聚合）

## 验证方式

1. 改完后语法检查 signal_btc.py + update_live_data.py
2. 单测 LLMDirectionStrategy：手动实例化、调 predict、确认返回标准格式
3. 确认 prod_config 能热加载（signal_btc 检测 mtime 变化自动重载）
4. 部署到服务器后：signal_btc 重启 → LLM 策略注册 → 每10分钟输出 signal_snapshot → server.js 自动路由到影子/实盘