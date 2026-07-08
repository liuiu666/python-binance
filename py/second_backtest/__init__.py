"""Reusable 1-second BTC binary-options backtest framework."""

from .data import audit_second_csv, load_second_bars
from .execution import execute_signals
from .metrics import split_metrics, summarize_trades
from .normal_state_v9 import NormalStateV9Config, generate_normal_state_v9_signals
from .normal_state_v11 import NormalStateV11Config, generate_normal_state_v11_signals
from .strategies import (
    SecondChipConfig,
    SecondNormalConfig,
    SecondNormalDirection3mConfig,
    SecondNormalMultiframeConfig,
    SecondNormalRouterV21Config,
    SecondNormalVwConfirmConfig,
    SecondRangeBreakoutConfig,
    SecondRangeBreakoutConfirmConfig,
    SecondTrendPullbackDownConfig,
    generate_chip_signals,
    generate_normal_signals,
    generate_normal_direction_3m_signals,
    generate_normal_multiframe_signals,
    generate_normal_router_v21_signals,
    generate_normal_vw_confirm_signals,
    generate_range_breakout_confirm_signals,
    generate_range_breakout_signals,
    generate_trend_pullback_down_signals,
)

__all__ = [
    "SecondChipConfig",
    "SecondNormalConfig",
    "SecondNormalDirection3mConfig",
    "SecondNormalMultiframeConfig",
    "SecondNormalRouterV21Config",
    "NormalStateV9Config",
    "NormalStateV11Config",
    "SecondNormalVwConfirmConfig",
    "SecondRangeBreakoutConfig",
    "SecondRangeBreakoutConfirmConfig",
    "SecondTrendPullbackDownConfig",
    "audit_second_csv",
    "execute_signals",
    "generate_chip_signals",
    "generate_normal_signals",
    "generate_normal_direction_3m_signals",
    "generate_normal_multiframe_signals",
    "generate_normal_router_v21_signals",
    "generate_normal_state_v9_signals",
    "generate_normal_state_v11_signals",
    "generate_normal_vw_confirm_signals",
    "generate_range_breakout_confirm_signals",
    "generate_range_breakout_signals",
    "generate_trend_pullback_down_signals",
    "load_second_bars",
    "split_metrics",
    "summarize_trades",
]
