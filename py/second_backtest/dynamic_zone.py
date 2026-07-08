from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


NONE_FILTERS = {"", "none", "off", "false", "disabled"}
DYNAMIC_FILTERS = {
    "dynamic_v1",
    "zone_dynamic_v1",
    "normal_zone_v1",
    "dynamic_v2",
    "zone_dynamic_v2",
    "dynamic_v3",
    "zone_dynamic_v3",
}


def is_dynamic_zone_filter_enabled(name: str | None) -> bool:
    return str(name or "none").lower() not in NONE_FILTERS


def _ret_bps(close: np.ndarray, idx: int, seconds: int) -> float:
    j = idx - int(seconds)
    if j < 0:
        return float("nan")
    base = float(close[j])
    now = float(close[idx])
    if not math.isfinite(base) or not math.isfinite(now) or base <= 0:
        return float("nan")
    return (now / base - 1.0) * 10000.0


def _range_bps(close: np.ndarray, idx: int, seconds: int) -> float:
    start = max(0, idx - int(seconds) + 1)
    window = close[start : idx + 1]
    window = window[np.isfinite(window) & (window > 0)]
    if len(window) < max(10, min(int(seconds), 60)):
        return float("nan")
    price = float(close[idx])
    if not math.isfinite(price) or price <= 0:
        return float("nan")
    return (float(np.max(window)) - float(np.min(window))) / price * 10000.0


def _position(close: np.ndarray, idx: int, seconds: int) -> float:
    start = max(0, idx - int(seconds) + 1)
    window = close[start : idx + 1]
    window = window[np.isfinite(window) & (window > 0)]
    if len(window) < max(10, min(int(seconds), 60)):
        return float("nan")
    low = float(np.min(window))
    high = float(np.max(window))
    if high <= low:
        return 0.5
    return (float(close[idx]) - low) / (high - low)


def _flow_imbalance(buy_qty: np.ndarray, sell_qty: np.ndarray, idx: int, seconds: int) -> float:
    start = max(0, idx - int(seconds) + 1)
    buy = float(np.nansum(buy_qty[start : idx + 1]))
    sell = float(np.nansum(sell_qty[start : idx + 1]))
    total = buy + sell
    if total <= 1e-12:
        return 0.0
    return (buy - sell) / total


def dynamic_zone_context_from_arrays(
    close: np.ndarray,
    buy_qty: np.ndarray | None,
    sell_qty: np.ndarray | None,
    idx: int,
) -> dict[str, Any]:
    close = np.asarray(close, dtype=float)
    if buy_qty is None:
        buy_qty = np.zeros(len(close), dtype=float)
    else:
        buy_qty = np.asarray(buy_qty, dtype=float)
    if sell_qty is None:
        sell_qty = np.zeros(len(close), dtype=float)
    else:
        sell_qty = np.asarray(sell_qty, dtype=float)

    idx = int(idx)
    pos30 = _position(close, idx, 1800)
    if not math.isfinite(pos30):
        zone = "unknown"
    elif pos30 < 0.2:
        zone = "0-20"
    elif pos30 < 0.4:
        zone = "20-40"
    elif pos30 < 0.6:
        zone = "40-60"
    elif pos30 < 0.8:
        zone = "60-80"
    else:
        zone = "80-100"

    return {
        "zone_position": pos30,
        "zone_label": zone,
        "trend_300s_bps": _ret_bps(close, idx, 300),
        "trend_600s_bps": _ret_bps(close, idx, 600),
        "trend_1800s_bps": _ret_bps(close, idx, 1800),
        "trend_3600s_bps": _ret_bps(close, idx, 3600),
        "range_10m_bps": _range_bps(close, idx, 600),
        "range_30m_bps": _range_bps(close, idx, 1800),
        "flow_5m": _flow_imbalance(buy_qty, sell_qty, idx, 300),
    }


def dynamic_zone_context_from_bars(bars: pd.DataFrame, idx: int | None = None) -> dict[str, Any]:
    if idx is None:
        idx = len(bars) - 1
    close = bars["close"].to_numpy(float)
    buy_qty = bars["buy_qty"].to_numpy(float) if "buy_qty" in bars else None
    sell_qty = bars["sell_qty"].to_numpy(float) if "sell_qty" in bars else None
    return dynamic_zone_context_from_arrays(close, buy_qty, sell_qty, int(idx))


def _finite(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return out if math.isfinite(out) else float("nan")


def dynamic_zone_allows(
    filter_name: str | None,
    signal: str,
    context: dict[str, Any],
) -> tuple[bool, str]:
    name = str(filter_name or "none").lower()
    if name in NONE_FILTERS:
        return True, "zone_filter_disabled"
    if name not in DYNAMIC_FILTERS:
        return False, f"unknown_zone_filter_{name}"

    pos = _finite(context.get("zone_position"))
    if not math.isfinite(pos):
        return False, "zone_context_unavailable"

    signal = str(signal or "").upper()
    trend5 = _finite(context.get("trend_300s_bps"))
    trend30 = _finite(context.get("trend_1800s_bps"))
    range10 = _finite(context.get("range_10m_bps"))
    flow5 = _finite(context.get("flow_5m"))

    if name in {"dynamic_v3", "zone_dynamic_v3"}:
        if pos < 0.2:
            if signal == "UP":
                return True, "zone_v3_lower_tail_up"
            return False, "zone_v3_lower_tail_only_up"
        if pos < 0.4:
            return False, "zone_v3_20_40_block"
        if pos < 0.8:
            if signal == "DOWN":
                return True, "zone_v3_mid_upper_down"
            return False, "zone_v3_mid_upper_only_down"
        return False, "zone_v3_80_100_block"

    if name in {"dynamic_v2", "zone_dynamic_v2"}:
        if not (0.6 <= pos < 0.8):
            return False, "zone_v2_only_60_80"
        if signal != "DOWN":
            return False, "zone_v2_only_down"
        if math.isfinite(range10) and range10 > 65:
            return False, "zone_v2_range_too_wide"
        if math.isfinite(flow5) and flow5 > 0.1:
            return False, "zone_v2_flow_still_chasing_up"
        if math.isfinite(trend30) and trend30 <= 0:
            return False, "zone_v2_not_upper_trend"
        trend60 = _finite(context.get("trend_3600s_bps"))
        if math.isfinite(trend60) and trend60 > 100:
            return False, "zone_v2_long_trend_too_strong"
        if math.isfinite(trend5) and math.isfinite(trend30) and trend5 > trend30:
            return False, "zone_v2_short_trend_accelerating"
        return True, "zone_v2_60_80_upper_reversion"

    if pos < 0.2:
        if signal != "UP":
            return False, "zone_0_20_only_up"
        if trend5 > 0 or flow5 > 0:
            return True, "zone_0_20_reversal_confirmed"
        return False, "zone_0_20_no_reversal_confirm"

    if pos < 0.4:
        return False, "zone_20_40_fake_rebound_block"

    if pos < 0.6:
        if math.isfinite(range10) and range10 > 45:
            return False, "zone_40_60_range_too_wide"
        return True, "zone_40_60_balanced"

    if pos < 0.8:
        if signal != "DOWN":
            return False, "zone_60_80_only_down"
        if trend5 < trend30 or flow5 <= 0:
            return True, "zone_60_80_upper_reversion"
        return False, "zone_60_80_runaway_block"

    if signal == "DOWN" and trend5 < 0 and flow5 < 0:
        return True, "zone_80_100_exhaustion_confirmed"
    return False, "zone_80_100_breakout_block"


def dynamic_zone_signal_hint(filter_name: str | None, signal: str | None, context: dict[str, Any]) -> str:
    name = str(filter_name or "none").lower()
    if name in NONE_FILTERS:
        return "未启用动态区间过滤，信号只按正态尾部触发。"
    if name not in DYNAMIC_FILTERS:
        return f"未知动态区间过滤：{name}。"

    pos = _finite(context.get("zone_position"))
    if not math.isfinite(pos):
        return "区间数据不足，等待最近30分钟秒级数据补齐后再判断。"

    sig = str(signal or "").upper()
    pct = round(pos * 100.0, 1)
    if name in {"dynamic_v3", "zone_dynamic_v3"}:
        if pos < 0.2:
            if sig == "UP":
                return f"当前在30分钟低位区({pct}%)，dynamic_v3允许做多；下一步看正态尾部是否继续给UP。"
            return f"当前在30分钟低位区({pct}%)，只等UP；如果正态给DOWN会被拦截。"
        if pos < 0.4:
            return f"当前在20-40禁做区({pct}%)，预计要等价格跌回0-20只做UP，或上到40-80只做DOWN。"
        if pos < 0.8:
            if sig == "DOWN":
                return f"当前在30分钟中上区({pct}%)，dynamic_v3允许做空；下一步看正态尾部是否继续给DOWN。"
            return f"当前在30分钟中上区({pct}%)，只等DOWN；如果正态给UP会被拦截。"
        return f"当前在80-100突破/高位禁做区({pct}%)，预计要等价格回落到40-80后才恢复做DOWN。"

    return f"当前30分钟区间位置 {pct}%，等待动态区间规则和正态尾部方向同时满足。"


def compact_zone_context(context: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in context.items():
        if isinstance(value, str):
            out[key] = value
            continue
        number = _finite(value)
        out[key] = None if not math.isfinite(number) else round(float(number), 6)
    return out
