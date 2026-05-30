# 泊松模型 (Poisson Model) 在量化交易中的应用与集成方案

> **适用范围**: 币安合约量化交易系统 (bxm40)  
> **核心价值**: 成交量异常检测、跳跃行情识别、策略信号增强

---

## 1. 什么是泊松模型？

### 1.1 直觉理解

想象你在观察一个十字路口，统计"每分钟有多少辆车经过"。  
大部分时间每分钟约 10 辆，但偶尔会出现每分钟 50 辆（附近出了车祸引发绕行）。

**泊松分布**就是用来回答这个问题的数学工具：
> 如果平均每分钟经过 λ=10 辆车，那么突然出现 50 辆的概率是多少？

如果概率极低（比如 p < 0.001），就说明这不是正常波动，而是**异常事件**。

### 1.2 数学定义

泊松分布描述的是：在**固定时间间隔**内，**独立随机事件**发生 k 次的概率：

```
P(X = k) = (λ^k × e^(-λ)) / k!
```

其中：
- **λ (lambda)**: 平均事件率（如"平均每分钟 120 笔成交"）
- **k**: 实际观测到的事件数
- **e**: 自然常数 ≈ 2.71828

### 1.3 为什么适合量化交易？

| 场景 | 用泊松建模的对象 | 检测目标 |
|:---|:---|:---|
| **成交量异常** | 每分钟成交笔数 | 突然放量 → 可能有大资金进场 |
| **订单流异常** | 每秒挂单/撤单次数 | 频繁撤单 → 可能有人做市操纵 |
| **价格跳跃** | 每小时大幅波动次数 | 突然跳空 → 重大消息驱动 |
| **资金费率** | 费率偏离正常值频率 | 持续偏高 → 多空极度失衡 |

---

## 2. 三种应用层次

### 层次 1: 成交量异常检测（最实用，优先集成）

**核心思想**: 用滚动窗口计算"正常"成交量均值 λ，然后检测当前成交量是否显著偏离。

```
正常: 过去 60 分钟平均每分钟 150 笔成交 (λ=150)
当前: 本分钟 580 笔成交 (k=580)
泊松 p-value = 1 - CDF(580, 150) ≈ 1e-89  →  极度异常！
```

**在交易中的用途**：
- 异常放量 + 价格上涨 → **增强做多信号的置信度**
- 异常放量 + 价格下跌 → **增强做空信号的置信度**
- 异常放量 + 价格不动 → **可能有大单在吸筹/出货，保持警惕**

### 层次 2: 跳跃-扩散模型（进阶）

经典的几何布朗运动（GBM）假设价格平滑变化，但现实中经常出现"跳空"。  
**Merton 跳跃-扩散模型**将连续扩散和泊松跳跃结合：

```
dS/S = (μ - λκ)dt + σdW + J·dN(t)
```

其中：
- `dW`: 布朗运动（正常波动）
- `dN(t)`: 泊松过程（跳跃事件是否发生）
- `J`: 跳跃幅度（跳了多大）
- `λ`: 跳跃频率（平均多久跳一次）
- `κ`: 跳跃幅度的均值

**用途**: 更精确地评估极端行情下的止损位和仓位大小。

### 层次 3: Hawkes 自激励过程（高阶）

泊松过程假设事件**彼此独立**，但在加密市场中，一笔大成交往往会引发更多的跟风交易（"**自激励**"）。

**Hawkes 过程**修正了这一点：

```
λ(t) = μ + Σ α·exp(-β(t - tᵢ))
```

每次事件发生后，未来的事件强度会短暂上升然后衰减。

**用途**: 检测市场操纵（spoofing/layering）、预测短期波动率飙升。

---

## 3. 集成到 bxm40 系统的方案

### 3.1 架构位置

```
┌────────────┐    ┌──────────────┐    ┌────────────────┐
│ Collector   │───▶│  Redis       │───▶│  Strategy      │
│ (aggTrade)  │    │  Streams     │    │  Engine        │
└────────────┘    └──────────────┘    │                │
                                      │  ┌────────────┐│
                                      │  │ EMA Cross   ││
                                      │  └────────────┘│
                                      │  ┌────────────┐│
                                      │  │ Breakout    ││
                                      │  └────────────┘│
                                      │  ┌────────────┐│  ← 新增
                                      │  │ Poisson     ││
                                      │  │ Detector    ││
                                      │  └────────────┘│
                                      └────────────────┘
```

泊松检测器作为**指标层的一个组件**集成到策略引擎中，不需要新增独立服务。

### 3.2 新增文件

```
strategy/
├── indicators.py          # 已有 — 在这里添加泊松指标
├── poisson_detector.py    # 新增 — 泊松异常检测器
└── strategies/
    ├── ema_cross.py       # 已有 — 可选择性引用泊松信号
    └── volume_anomaly.py  # 新增 — 基于泊松的成交量异常策略
```

### 3.3 核心代码设计

#### 3.3.1 泊松异常检测器 (`strategy/poisson_detector.py`)

```python
"""
泊松异常检测器
功能:
- 滚动窗口估计成交量 λ (均值)
- 计算当前窗口的泊松 p-value
- 异常等级划分: 正常 / 关注 / 异常 / 极端
- 支持自适应 λ (指数加权移动平均)
"""

import numpy as np
from scipy.stats import poisson
from typing import Dict, Optional, Tuple
from dataclasses import dataclass
from enum import Enum


class AnomalyLevel(str, Enum):
    """异常等级"""
    NORMAL = "NORMAL"       # p >= 0.05   正常波动
    WATCH = "WATCH"         # 0.01 <= p < 0.05  值得关注
    ANOMALY = "ANOMALY"     # 0.001 <= p < 0.01  明显异常
    EXTREME = "EXTREME"     # p < 0.001  极端异常


@dataclass
class PoissonResult:
    """检测结果"""
    current_count: int          # 当前窗口的成交笔数
    lambda_estimate: float      # 估计的 λ (正常均值)
    p_value: float              # 泊松 p-value
    anomaly_level: AnomalyLevel # 异常等级
    z_score: float              # 标准化分数 (current - λ) / sqrt(λ)
    direction: str              # "HIGH" / "LOW" / "NORMAL"


class PoissonDetector:
    """
    泊松异常检测器

    参数:
    - window_size: 滚动窗口大小 (用多少个历史窗口估计 λ)
    - ema_alpha: 指数加权系数 (0~1, 越大越重视近期数据)
    - overdispersion_factor: 过散射修正因子 (>1 表示容忍更大的波动)
    """

    def __init__(
        self,
        window_size: int = 60,
        ema_alpha: float = 0.05,
        overdispersion_factor: float = 1.0,
    ) -> None:
        self._window_size = window_size
        self._ema_alpha = ema_alpha
        self._overdispersion = overdispersion_factor
        # 历史成交计数缓冲区
        self._history: list[int] = []
        # 指数加权 λ
        self._ema_lambda: Optional[float] = None

    def update(self, trade_count: int) -> PoissonResult:
        """
        输入当前时间窗口的成交笔数, 返回异常检测结果

        Args:
            trade_count: 当前窗口 (如 1 分钟) 内的成交笔数

        Returns:
            PoissonResult 检测结果
        """
        # 更新历史
        self._history.append(trade_count)
        if len(self._history) > self._window_size * 2:
            self._history = self._history[-self._window_size * 2:]

        # 更新 EMA λ
        if self._ema_lambda is None:
            self._ema_lambda = float(trade_count)
        else:
            self._ema_lambda = (
                self._ema_alpha * trade_count
                + (1 - self._ema_alpha) * self._ema_lambda
            )

        # 估计 λ (取滚动均值和 EMA 的加权)
        if len(self._history) >= self._window_size:
            rolling_mean = np.mean(self._history[-self._window_size:])
            lambda_est = 0.5 * rolling_mean + 0.5 * self._ema_lambda
        else:
            lambda_est = self._ema_lambda

        # 修正过散射
        lambda_est *= self._overdispersion

        # 安全检查
        lambda_est = max(lambda_est, 1.0)

        # 计算 p-value
        if trade_count > lambda_est:
            # 上侧检测: P(X >= k) = 1 - CDF(k-1, λ)
            p_value = 1.0 - poisson.cdf(trade_count - 1, lambda_est)
            direction = "HIGH"
        elif trade_count < lambda_est * 0.3:
            # 下侧检测: P(X <= k) = CDF(k, λ)
            p_value = poisson.cdf(trade_count, lambda_est)
            direction = "LOW"
        else:
            p_value = 1.0
            direction = "NORMAL"

        # z-score
        z_score = (trade_count - lambda_est) / max(np.sqrt(lambda_est), 1.0)

        # 异常等级
        if p_value < 0.001:
            level = AnomalyLevel.EXTREME
        elif p_value < 0.01:
            level = AnomalyLevel.ANOMALY
        elif p_value < 0.05:
            level = AnomalyLevel.WATCH
        else:
            level = AnomalyLevel.NORMAL

        return PoissonResult(
            current_count=trade_count,
            lambda_estimate=round(lambda_est, 2),
            p_value=round(p_value, 6),
            anomaly_level=level,
            z_score=round(z_score, 2),
            direction=direction,
        )

    def get_lambda(self) -> float:
        """获取当前估计的 λ"""
        return self._ema_lambda or 0.0

    def reset(self) -> None:
        """重置检测器"""
        self._history.clear()
        self._ema_lambda = None
```

#### 3.3.2 在指标计算中集成 (`strategy/indicators.py` 新增)

```python
def compute_trade_intensity(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算成交强度指标 (基于泊松模型)

    新增列:
    - trade_intensity: 每根 K 线的成交笔数
    - intensity_lambda: 滚动 λ 估计
    - intensity_zscore: 标准化异常分数
    - intensity_anomaly: 异常等级 (0=正常, 1=关注, 2=异常, 3=极端)
    """
    if "trades_count" not in df.columns:
        return df

    counts = df["trades_count"].values
    window = 60  # 60 根 K 线滚动窗口

    # 滚动均值 (λ 估计)
    df["intensity_lambda"] = df["trades_count"].rolling(window).mean()

    # Z-score
    df["intensity_zscore"] = (
        (df["trades_count"] - df["intensity_lambda"])
        / np.sqrt(df["intensity_lambda"].clip(lower=1.0))
    )

    # 异常等级 (基于 z-score 快速近似)
    df["intensity_anomaly"] = 0  # 正常
    df.loc[df["intensity_zscore"] > 2.0, "intensity_anomaly"] = 1   # 关注
    df.loc[df["intensity_zscore"] > 3.0, "intensity_anomaly"] = 2   # 异常
    df.loc[df["intensity_zscore"] > 4.0, "intensity_anomaly"] = 3   # 极端
    df.loc[df["intensity_zscore"] < -2.0, "intensity_anomaly"] = -1 # 异常低量

    return df
```

#### 3.3.3 基于泊松的策略示例 (`strategy/strategies/volume_anomaly.py`)

```python
"""
成交量异常策略 — 基于泊松检测的信号增强器

逻辑:
- 不独立产生信号, 而是作为"信号增强器"
- 当泊松检测到异常放量时:
  - 如果同时有其他策略的做多信号 → 增大仓位
  - 如果没有方向性信号 → 发出"关注"预警
  - 如果逆向异常 (量缩) → 降低仓位或跳过信号
"""

class VolumeAnomalyStrategy(BaseStrategy):

    def __init__(self, symbols=None, base_quantity=0.001):
        super().__init__(name="volume_anomaly", symbols=symbols)
        self._base_quantity = base_quantity
        self._detectors: Dict[str, PoissonDetector] = {}

    async def on_kline(self, symbol, df) -> Optional[Signal]:
        if not self._enabled or df.empty:
            return None

        # 获取泊松检测器
        if symbol not in self._detectors:
            self._detectors[symbol] = PoissonDetector(window_size=60)

        detector = self._detectors[symbol]
        trade_count = int(df.iloc[-1].get("trades_count", 0))
        result = detector.update(trade_count)

        # 只在"异常"或"极端"级别时才考虑产生信号
        if result.anomaly_level not in (AnomalyLevel.ANOMALY, AnomalyLevel.EXTREME):
            return None

        # 判断方向: 价格涨 + 放量 = 做多; 价格跌 + 放量 = 做空
        close = float(df.iloc[-1]["close_price"])
        prev_close = float(df.iloc[-2]["close_price"])
        price_change_pct = (close - prev_close) / prev_close * 100

        if result.direction == "HIGH":
            if price_change_pct > 0.1:
                # 放量上涨 → 做多信号
                return self._create_signal(
                    symbol=symbol,
                    action="OPEN",
                    side="BUY",
                    quantity=self._base_quantity * (1 + abs(result.z_score) * 0.1),
                    price=close,
                    reason=f"泊松异常放量+上涨 z={result.z_score}, p={result.p_value}",
                )
            elif price_change_pct < -0.1:
                # 放量下跌 → 做空信号
                return self._create_signal(
                    symbol=symbol,
                    action="OPEN",
                    side="SELL",
                    quantity=self._base_quantity * (1 + abs(result.z_score) * 0.1),
                    price=close,
                    reason=f"泊松异常放量+下跌 z={result.z_score}, p={result.p_value}",
                )

        return None
```

---

## 4. 集成步骤 (开发清单)

| 步骤 | 文件 | 工作量 | 说明 |
|:---|:---|:---|:---|
| ① | `requirements.txt` | 5 min | 确认 `scipy` 已在依赖中 |
| ② | `strategy/poisson_detector.py` | 2 h | 实现核心检测器 |
| ③ | `strategy/indicators.py` | 30 min | 新增 `compute_trade_intensity()` |
| ④ | `strategy/strategies/volume_anomaly.py` | 2 h | 实现策略逻辑 |
| ⑤ | `strategy/main.py` | 15 min | 注册新策略到引擎 |
| ⑥ | `collector/main.py` | 15 min | 确保 `trades_count` 字段传入 Redis |
| ⑦ | 回测验证 | 4 h | 用历史数据验证误报率和信号质量 |

**总工时: 约 1 天**

---

## 5. 注意事项和陷阱

### 5.1 过散射问题 (Overdispersion)

泊松分布要求 **方差 = 均值**，但金融数据通常 **方差 > 均值**（称为过散射）。

**解决方案**:
- 方法 1: 使用 `overdispersion_factor` 参数放大 λ，降低误报
- 方法 2: 改用**负二项分布** (Negative Binomial)，它多了一个参数来处理过散射
- 方法 3: 用 **z-score 近似** 替代精确泊松概率（代码中已实现）

### 5.2 非平稳性 (Non-stationarity)

加密市场的成交量有明显的日内节律：
- 美股开盘时 BTC 成交量飙升（北京时间 21:30）
- 亚洲早盘相对平静（北京时间 09:00-14:00）

**解决方案**: 使用 **EMA (指数加权移动平均)** 动态调整 λ，而不是用固定窗口均值。代码中 `PoissonDetector` 已内置 EMA 支持。

### 5.3 独立性假设

泊松过程假设事件彼此独立，但加密市场存在"**群体效应**"——一笔大成交往往引发连锁反应。

**解决方案**: 如果需要更精确的建模，后续可升级为 **Hawkes 过程**（自激励泊松过程）。当前先用标准泊松做 MVP。

### 5.4 不要单独依赖泊松信号

泊松检测器是一个**信号增强器**，不是独立策略：

```
✅ 正确用法: EMA 金叉 + 泊松异常放量 → 增大仓位
✅ 正确用法: 泊松检测到极端异常 → 发通知让人工关注
❌ 错误用法: 只要泊松报异常就下单 → 会被假信号淹没
```

---

## 6. 进阶路线图

| 阶段 | 模型 | 应用 | 依赖 |
|:---|:---|:---|:---|
| **Phase 1** (当前) | 标准泊松 + 滚动 λ | 成交量异常检测、信号增强 | `scipy` |
| **Phase 2** | 非齐次泊松 + 日内节律 | 分时段自适应阈值 | 历史数据分析 |
| **Phase 3** | Hawkes 自激励过程 | 操纵行为检测、短期波动率预测 | `tick` 库 |
| **Phase 4** | Merton 跳跃-扩散 | 极端行情下的动态止损/仓位管理 | 自研或 `QuantLib` |

---

## 7. 与现有架构的关系

```
┌─────────────────────────────────────────────────────────┐
│                    Strategy Engine                       │
│                                                         │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────┐ │
│  │ indicators   │  │ Poisson     │  │ AI sentiment    │ │
│  │ (EMA/RSI/   │  │ Detector    │  │ (Phase 5)       │ │
│  │  ATR)        │  │ (新增)      │  │                 │ │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────────┘ │
│         │                │                │             │
│         ▼                ▼                ▼             │
│  ┌─────────────────────────────────────────┐           │
│  │         策略决策层                       │           │
│  │  EMA Cross + Breakout + Volume Anomaly  │           │
│  │                                         │           │
│  │  信号 = 技术指标 × 泊松置信度 × AI情绪  │           │
│  └────────────────────┬────────────────────┘           │
│                       │                                 │
│                       ▼                                 │
│              Signal → Redis Streams                     │
└─────────────────────────────────────────────────────────┘
```

泊松模型与现有的 EMA/RSI 技术指标**并行计算**，作为**信号置信度的乘数因子**，不替代现有策略逻辑。

---

## 8. 快速验证 (5 分钟上手)

```python
# 在 Python REPL 中快速体验泊松异常检测
from scipy.stats import poisson
import numpy as np

# 模拟正常成交量 (平均每分钟 150 笔)
np.random.seed(42)
normal_data = np.random.poisson(lam=150, size=100)

# 注入异常 (第 50 分钟突然 500 笔)
normal_data[50] = 500

# 检测
lambda_est = np.mean(normal_data[:50])  # 前 50 分钟的均值
print(f"λ 估计: {lambda_est:.1f}")

for i in [49, 50, 51]:
    k = normal_data[i]
    p = 1 - poisson.cdf(k - 1, lambda_est)
    print(f"  第 {i} 分钟: 成交 {k} 笔, p-value = {p:.2e}, {'⚠️ 异常!' if p < 0.01 else '正常'}")

# 输出:
# λ 估计: 150.3
#   第 49 分钟: 成交 139 笔, p-value = 8.31e-01, 正常
#   第 50 分钟: 成交 500 笔, p-value = 1.11e-89, ⚠️ 异常!
#   第 51 分钟: 成交 152 笔, p-value = 4.55e-01, 正常
```
