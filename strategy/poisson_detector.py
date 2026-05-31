"""
泊松异常检测器
功能:
- 滚动窗口估计成交量 λ (均值)
- 计算当前窗口的泊松 p-value
- 异常等级划分: 正常 / 关注 / 异常 / 极端
- 支持自适应 λ (指数加权移动平均)
- 支持 scipy 库, 并提供纯 Python 兜底实现以防 scipy 未安装
"""

import math
import numpy as np
from typing import Dict, Optional, Tuple
from dataclasses import dataclass
from enum import Enum
from common.logger import get_logger

logger = get_logger(__name__)

try:
    from scipy.stats import poisson as scipy_poisson
    HAS_SCIPY = True
except ImportError:
    logger.warning("poisson.scipy_missing", message="scipy not installed, using pure-python fallback for Poisson distribution")
    HAS_SCIPY = False


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


def _pure_poisson_cdf(k: int, lam: float) -> float:
    """
    纯 Python 实现的泊松累积分布函数 (CDF)
    当 scipy 未安装时作为安全兜底
    """
    if k < 0:
        return 0.0
    
    # 针对大 lambda 使用正态近似（带连续性修正），防止 math.exp(-lam) 发生下溢
    if lam > 50.0:
        z = (k + 0.5 - lam) / math.sqrt(lam)
        # 标准正态分布 CDF 近似: 0.5 * (1 + erf(z / sqrt(2)))
        try:
            return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        except ValueError:
            return 1.0 if z > 0 else 0.0

    # 针对小 lambda 直接累加
    try:
        ans = 0.0
        term = math.exp(-lam)
        ans += term
        for i in range(1, int(k) + 1):
            term = term * lam / i
            ans += term
        return min(max(ans, 0.0), 1.0)
    except Exception:
        # 极端情况下的安全保护
        z = (k - lam) / math.sqrt(max(lam, 1.0))
        return 1.0 if z > 0 else 0.0


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
            if HAS_SCIPY:
                p_value = 1.0 - scipy_poisson.cdf(trade_count - 1, lambda_est)
            else:
                p_value = 1.0 - _pure_poisson_cdf(trade_count - 1, lambda_est)
            direction = "HIGH"
        elif trade_count < lambda_est * 0.3:
            # 下侧检测: P(X <= k) = CDF(k, λ)
            if HAS_SCIPY:
                p_value = scipy_poisson.cdf(trade_count, lambda_est)
            else:
                p_value = _pure_poisson_cdf(trade_count, lambda_est)
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
