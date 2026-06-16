"""Reusable 1-second BTC binary-options backtest framework."""

from .data import audit_second_csv, load_second_bars
from .execution import execute_signals
from .metrics import split_metrics, summarize_trades
from .strategies import (
    SecondChipConfig,
    SecondNormalConfig,
    generate_chip_signals,
    generate_normal_signals,
)

__all__ = [
    "SecondChipConfig",
    "SecondNormalConfig",
    "audit_second_csv",
    "execute_signals",
    "generate_chip_signals",
    "generate_normal_signals",
    "load_second_bars",
    "split_metrics",
    "summarize_trades",
]
