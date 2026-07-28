"""Frozen causal rule for the position-build-up discovery candidate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


STRATEGY_ID = "BTC_10min_POSITION_BUILD_UP_V1"


@dataclass(frozen=True)
class PositionBuildUpConfig:
    horizon_sec: int = 600
    execution_delay_sec: int = 6
    min_gap_sec: int = 600
    price_lookback_sec: int = 300
    open_interest_lookback_min: int = 15


def evaluate_snapshot(ret_300_bps: float, open_interest_change_15m: float) -> dict[str, Any]:
    if ret_300_bps > 0.0 and open_interest_change_15m > 0.0:
        return {
            "signal": "UP",
            "reason": "price_up_with_new_open_interest",
            "reason_zh": "过去5分钟价格上涨且15分钟总持仓增加，新多仓推动候选，预测未来10分钟上涨。",
        }
    return {
        "signal": None,
        "reason": "waiting_price_and_open_interest_build_up",
        "reason_zh": "等待价格上涨与总持仓量同步增加。",
    }
