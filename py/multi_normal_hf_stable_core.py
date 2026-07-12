"""Shared core for the adaptive multi-normal high-frequency strategy.

The evaluator is deliberately causal: it only consumes the market snapshot
available at the end of a completed minute. Backtests and live adapters must
call :func:`evaluate_snapshot` instead of reimplementing these rules.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterator

import pandas as pd

from branch_vote_startup_core import (
    BranchVoteStartupConfig,
    build_minute_snapshots,
)


STRATEGY_ID = "BTC_10min_MULTI_NORMAL_HF_STABLE_V1"
MODEL_TYPE = "second_multi_normal_hf_stable_v1"


@dataclass(frozen=True)
class MultiNormalHFStableConfig:
    normal_window_sec: int = 600
    horizon_sec: int = 600
    min_gap_sec: int = 600
    orderbook_max_age_sec: int = 3

    # Ultra-low-volatility normal reversion.
    lowvol_sigma_max_bps: float = 3.0
    lowvol_range_max_bps: float = 20.0
    lowvol_abs_ret10_max_bps: float = 5.0
    lowvol_z_min: float = 1.2
    lowvol_z_max: float = 1.8
    lowvol_min_signed_flow: float = 0.0
    # A tail is not a reversal while the last 30 seconds are still moving
    # outward. Scale the guard by current sigma so it adapts to volatility.
    lowvol_max_adverse_ret30_sigma: float = 0.5

    # Mature trend exhaustion. A high-volatility sprint needs less z-distance
    # because the absolute move is already large.
    trend_base_z_min: float = 1.2
    trend_high_vol_sigma_min_bps: float = 8.0
    trend_high_vol_z_min: float = 0.5
    trend_min_signed_flow: float = 0.12
    trend_max_signed_book: float = 0.08

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> "MultiNormalHFStableConfig":
        cfg = cfg or {}
        return cls(
            normal_window_sec=int(cfg.get("multi_normal_window_sec", 600)),
            horizon_sec=int(cfg.get("multi_normal_horizon_sec", 600)),
            min_gap_sec=int(cfg.get("multi_normal_signal_gap_sec", 600)),
            orderbook_max_age_sec=int(cfg.get("multi_normal_orderbook_max_age_sec", 3)),
            lowvol_sigma_max_bps=float(cfg.get("multi_normal_lowvol_sigma_max_bps", 3.0)),
            lowvol_range_max_bps=float(cfg.get("multi_normal_lowvol_range_max_bps", 20.0)),
            lowvol_abs_ret10_max_bps=float(cfg.get("multi_normal_lowvol_abs_ret10_max_bps", 5.0)),
            lowvol_z_min=float(cfg.get("multi_normal_lowvol_z_min", 1.2)),
            lowvol_z_max=float(cfg.get("multi_normal_lowvol_z_max", 1.8)),
            lowvol_min_signed_flow=float(cfg.get("multi_normal_lowvol_min_signed_flow", 0.0)),
            lowvol_max_adverse_ret30_sigma=float(
                cfg.get("multi_normal_lowvol_max_adverse_ret30_sigma", 0.5)
            ),
            trend_base_z_min=float(cfg.get("multi_normal_trend_base_z_min", 1.2)),
            trend_high_vol_sigma_min_bps=float(
                cfg.get("multi_normal_trend_high_vol_sigma_min_bps", 8.0)
            ),
            trend_high_vol_z_min=float(cfg.get("multi_normal_trend_high_vol_z_min", 0.5)),
            trend_min_signed_flow=float(cfg.get("multi_normal_trend_min_signed_flow", 0.12)),
            trend_max_signed_book=float(cfg.get("multi_normal_trend_max_signed_book", 0.08)),
        )

    def snapshot_config(self) -> BranchVoteStartupConfig:
        return BranchVoteStartupConfig(
            normal_window_sec=self.normal_window_sec,
            horizon_sec=self.horizon_sec,
            min_gap_sec=self.min_gap_sec,
            orderbook_max_age_sec=self.orderbook_max_age_sec,
        )


def _number(row: dict[str, Any] | pd.Series, key: str) -> float:
    try:
        value = float(row.get(key, float("nan")))
    except (TypeError, ValueError):
        return float("nan")
    return value if math.isfinite(value) else float("nan")


def _fmt(value: float, digits: int = 2, *, signed: bool = False) -> str:
    if not math.isfinite(value):
        return "--"
    prefix = "+" if signed and value > 0.0 else ""
    return f"{prefix}{value:.{digits}f}"


def _check(
    key: str,
    label: str,
    ok: bool | None,
    value: str,
    target: str,
    help_text: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "ok": None if ok is None else bool(ok),
        "value": value,
        "target": target,
        "help": help_text,
    }


def _path_payload(
    key: str,
    label: str,
    active: bool,
    candidate_signal: str | None,
    checks: list[dict[str, Any]],
    observation: dict[str, Any],
) -> dict[str, Any]:
    passed = sum(item["ok"] is True for item in checks)
    pending = [item["label"] for item in checks if item["ok"] is False]
    waiting_data = any(item["ok"] is None for item in checks)
    if checks and passed == len(checks):
        status = "ready"
        status_zh = "条件已满足"
        summary = "全部条件已满足，当前快照可以产生信号。"
    elif not active:
        status = "inactive"
        status_zh = "当前行情不适用"
        summary = "当前行情不属于这条信号路径。"
    elif waiting_data:
        status = "waiting_data"
        status_zh = "等待完整数据"
        summary = "部分指标尚未形成，等待下一完整分钟。"
    else:
        status = "watching"
        status_zh = "接近条件"
        missing = "、".join(pending[:3])
        summary = f"已通过 {passed}/{len(checks)}，还差：{missing}。"
    return {
        "key": key,
        "label": label,
        "status": status,
        "status_zh": status_zh,
        "candidate_signal": candidate_signal,
        "passed": passed,
        "total": len(checks),
        "summary": summary,
        "checks": checks,
        "observation": observation,
    }


def _diagnostic_payload(
    row: dict[str, Any] | pd.Series,
    cfg: MultiNormalHFStableConfig,
) -> dict[str, Any]:
    trend = str(row.get("trend") or "")
    normal_quality = str(row.get("normal_quality") or "")
    normal_pos = str(row.get("normal_pos") or "")
    sprint = str(row.get("sprint") or "")
    price = _number(row, "price")
    if not math.isfinite(price):
        price = _number(row, "close")
    center = _number(row, "normal_center")
    sigma_price = _number(row, "normal_sigma")
    z = _number(row, "z")
    sigma10 = _number(row, "sigma10_bps")
    range10 = _number(row, "range10_bps")
    ret10 = _number(row, "ret10_bps")
    flow5 = _number(row, "flow5")
    sec_ret30 = _number(row, "sec_ret30_bps")
    imb20 = _number(row, "imb20")
    inside = _number(row, "inside1_ratio")
    observed = _number(row, "observed_pct")
    center_slope = _number(row, "center_slope_bps")
    sigma_bps = _number(row, "sigma_bps")
    sigma_expand = _number(row, "sigma_expand")

    def finite_check(value: float, predicate) -> bool | None:
        return bool(predicate(value)) if math.isfinite(value) else None

    low_candidate = None
    if math.isfinite(z) and z != 0.0:
        low_candidate = "DOWN" if z > 0.0 else "UP"
    low_signed_flow = float("nan")
    if low_candidate and math.isfinite(flow5):
        low_signed_flow = flow5 if low_candidate == "UP" else -flow5
    low_signed_ret30 = float("nan")
    if low_candidate and math.isfinite(sec_ret30):
        low_signed_ret30 = sec_ret30 if low_candidate == "UP" else -sec_ret30
    low_ret30_floor = (
        -cfg.lowvol_max_adverse_ret30_sigma * sigma10
        if math.isfinite(sigma10)
        else float("nan")
    )
    if low_candidate == "UP":
        flow_target = f"做多要求 flow5 >= {_fmt(cfg.lowvol_min_signed_flow, 3)}"
        flow_help = "下沿做多需要近5分钟主动成交流转为买方。"
    elif low_candidate == "DOWN":
        flow_target = f"做空要求 flow5 <= {_fmt(-cfg.lowvol_min_signed_flow, 3)}"
        flow_help = "上沿做空需要近5分钟主动成交流转为卖方。"
    else:
        flow_target = "价格到达上沿或下沿后再判断方向"
        flow_help = "价格尚未形成明确的上沿或下沿候选方向。"

    quality_parts = []
    if math.isfinite(inside):
        quality_parts.append(f"区内{_fmt(inside * 100.0, 1)}%")
    if math.isfinite(observed):
        quality_parts.append(f"覆盖{_fmt(observed, 1)}%")
    if math.isfinite(sigma_expand):
        quality_parts.append(f"扩张{_fmt(sigma_expand, 2)}x")
    quality_value = "已成型" if normal_quality == "normal_ready" else "未成型"
    if quality_parts:
        quality_value += f"（{' / '.join(quality_parts)}）"

    low_checks = [
        _check(
            "lowvol_trend",
            "横盘状态",
            trend == "flat" if trend else None,
            {
                "flat": "横盘",
                "trend_up": "上涨趋势",
                "trend_down": "下跌趋势",
                "drift_up": "弱上涨",
                "drift_down": "弱下跌",
                "transition": "切换中",
            }.get(trend, trend or "--"),
            "必须是横盘",
            "这条路径只在横盘中做正态尾部回归。",
        ),
        _check(
            "lowvol_quality",
            "正态区间",
            normal_quality == "normal_ready" if normal_quality else None,
            quality_value,
            "区内>=45%，覆盖>=88%，sigma扩张<=1.25x",
            "过去10分钟还没有形成稳定、完整的正态区间。",
        ),
        _check(
            "lowvol_sigma",
            "10分钟波动",
            finite_check(sigma10, lambda value: value < cfg.lowvol_sigma_max_bps),
            f"{_fmt(sigma10, 2)}bp",
            f"要求 < {_fmt(cfg.lowvol_sigma_max_bps, 2)}bp",
            "当前波动不属于超低波动回归档。",
        ),
        _check(
            "lowvol_range",
            "10分钟振幅",
            finite_check(range10, lambda value: value <= cfg.lowvol_range_max_bps),
            f"{_fmt(range10, 2)}bp",
            f"要求 <= {_fmt(cfg.lowvol_range_max_bps, 2)}bp",
            "10分钟最高最低范围过大，不按窄幅正态处理。",
        ),
        _check(
            "lowvol_return",
            "10分钟涨跌",
            finite_check(ret10, lambda value: abs(value) <= cfg.lowvol_abs_ret10_max_bps),
            f"{_fmt(ret10, 2, signed=True)}bp",
            f"绝对值 <= {_fmt(cfg.lowvol_abs_ret10_max_bps, 2)}bp",
            "10分钟方向移动过强，暂不做逆向回归。",
        ),
        _check(
            "lowvol_z_min",
            "到达正态尾部",
            finite_check(z, lambda value: abs(value) >= cfg.lowvol_z_min),
            f"{_fmt(z, 3, signed=True)}σ",
            f"绝对值 >= {_fmt(cfg.lowvol_z_min, 1)}σ",
            "价格还没走到可入场的上沿或下沿。",
        ),
        _check(
            "lowvol_z_max",
            "没有离区过远",
            finite_check(z, lambda value: abs(value) <= cfg.lowvol_z_max),
            f"{_fmt(z, 3, signed=True)}σ",
            f"绝对值 <= {_fmt(cfg.lowvol_z_max, 1)}σ",
            "偏离超过换区警戒线，可能正在形成新区间。",
        ),
        _check(
            "lowvol_flow",
            "成交流转向",
            finite_check(low_signed_flow, lambda value: value >= cfg.lowvol_min_signed_flow),
            f"flow5 {_fmt(flow5, 3, signed=True)}",
            flow_target,
            flow_help,
        ),
        _check(
            "lowvol_short_move",
            "最近30秒停止外冲",
            finite_check(low_signed_ret30, lambda value: value >= low_ret30_floor),
            f"回归方向 {_fmt(low_signed_ret30, 2, signed=True)}bp",
            f"要求 >= {_fmt(low_ret30_floor, 2)}bp（不超过0.5倍当前波动）",
            "最近30秒仍明显向区间外移动，说明尾部尚未形成回归，暂不逆向接单。",
        ),
    ]

    upper_watch = center + cfg.lowvol_z_min * sigma_price if math.isfinite(center) and math.isfinite(sigma_price) else float("nan")
    lower_watch = center - cfg.lowvol_z_min * sigma_price if math.isfinite(center) and math.isfinite(sigma_price) else float("nan")
    upper_shift = center + cfg.lowvol_z_max * sigma_price if math.isfinite(center) and math.isfinite(sigma_price) else float("nan")
    lower_shift = center - cfg.lowvol_z_max * sigma_price if math.isfinite(center) and math.isfinite(sigma_price) else float("nan")

    trend_sign = 1.0 if trend == "trend_up" else -1.0 if trend == "trend_down" else float("nan")
    trend_candidate = "DOWN" if trend == "trend_up" else "UP" if trend == "trend_down" else None
    matching_sprint = None
    if math.isfinite(trend_sign):
        matching_sprint = (
            trend_sign > 0.0 and sprint in {"up_sprint", "up_walk"}
        ) or (
            trend_sign < 0.0 and sprint in {"down_sprint", "down_walk"}
        )
    signed_z = trend_sign * z if math.isfinite(trend_sign) and math.isfinite(z) else float("nan")
    trend_signed_flow = trend_sign * flow5 if math.isfinite(trend_sign) and math.isfinite(flow5) else float("nan")
    signed_book = trend_sign * imb20 if math.isfinite(trend_sign) and math.isfinite(imb20) else float("nan")
    high_volatility = math.isfinite(sigma10) and sigma10 >= cfg.trend_high_vol_sigma_min_bps
    trend_z_required = cfg.trend_high_vol_z_min if high_volatility else cfg.trend_base_z_min
    trend_checks = [
        _check(
            "trend_direction",
            "明确趋势",
            math.isfinite(trend_sign) if trend else None,
            "上涨趋势" if trend == "trend_up" else "下跌趋势" if trend == "trend_down" else "当前无成熟趋势",
            "必须是上涨趋势或下跌趋势",
            "弱趋势、横盘或切换行情不进入趋势衰竭路径。",
        ),
        _check(
            "trend_sprint",
            "同向冲刺",
            matching_sprint,
            {
                "up_sprint": "上涨冲刺",
                "down_sprint": "下跌冲刺",
                "up_walk": "连续上涨",
                "down_walk": "连续下跌",
                "none": "无冲刺",
            }.get(sprint, sprint or "--"),
            "趋势方向连续冲刺或行走",
            "趋势还没有出现可判断衰竭的连续推进形态。",
        ),
        _check(
            "trend_z",
            "趋势偏离",
            finite_check(signed_z, lambda value: value >= trend_z_required),
            f"同向 {_fmt(signed_z, 3)}σ",
            f"要求 >= {_fmt(trend_z_required, 1)}σ",
            "价格沿趋势方向的偏离还不够大。",
        ),
        _check(
            "trend_flow",
            "趋势成交流",
            finite_check(trend_signed_flow, lambda value: value >= cfg.trend_min_signed_flow),
            f"同向 {_fmt(trend_signed_flow, 3)}",
            f"要求 >= {_fmt(cfg.trend_min_signed_flow, 2)}",
            "主动成交还没有形成趋势末端的冲刺强度。",
        ),
        _check(
            "trend_book",
            "订单薄支撑衰减",
            finite_check(signed_book, lambda value: value <= cfg.trend_max_signed_book),
            f"同向 {_fmt(signed_book, 3)}",
            f"要求 <= {_fmt(cfg.trend_max_signed_book, 2)}",
            "订单薄仍明显支持原趋势，还不能反向做衰竭回归。",
        ),
    ]
    trend_watch = (
        center + trend_sign * trend_z_required * sigma_price
        if math.isfinite(center) and math.isfinite(sigma_price) and math.isfinite(trend_sign)
        else float("nan")
    )

    low_path = _path_payload(
        "lowvol_normal_reversion",
        "路径一：低波动正态回归",
        trend == "flat",
        low_candidate,
        low_checks,
        {
            "upper_watch_price": upper_watch if math.isfinite(upper_watch) else None,
            "lower_watch_price": lower_watch if math.isfinite(lower_watch) else None,
            "upper_shift_price": upper_shift if math.isfinite(upper_shift) else None,
            "lower_shift_price": lower_shift if math.isfinite(lower_shift) else None,
            "upper_watch_reached": bool(math.isfinite(z) and z >= cfg.lowvol_z_min),
            "lower_watch_reached": bool(math.isfinite(z) and z <= -cfg.lowvol_z_min),
            "upper_gap_bps": (
                max(0.0, (upper_watch / price - 1.0) * 10000.0)
                if math.isfinite(upper_watch) and math.isfinite(price) and price > 0.0
                else None
            ),
            "lower_gap_bps": (
                max(0.0, (price / lower_watch - 1.0) * 10000.0)
                if math.isfinite(lower_watch) and lower_watch > 0.0 and math.isfinite(price)
                else None
            ),
        },
    )
    trend_path = _path_payload(
        "mature_trend_exhaustion",
        "路径二：成熟趋势衰竭",
        math.isfinite(trend_sign),
        trend_candidate,
        trend_checks,
        {
            "watch_price": trend_watch if math.isfinite(trend_watch) else None,
            "watch_reached": bool(math.isfinite(signed_z) and signed_z >= trend_z_required),
            "z_required": trend_z_required,
            "high_volatility": high_volatility,
        },
    )

    position_labels = {
        "above_upper": "上沿外",
        "upper_edge": "上沿",
        "upper_inside": "上半区",
        "center": "中轴附近",
        "lower_inside": "下半区",
        "lower_edge": "下沿",
        "below_lower": "下沿外",
    }
    position_text = position_labels.get(normal_pos, normal_pos or "位置未知")
    if trend == "flat" and normal_quality == "normal_ready" and finite_check(sigma10, lambda value: value < cfg.lowvol_sigma_max_bps):
        market_code = "lowvol_normal"
        market_label = f"超低波动正态震荡 · {position_text}"
        market_detail = low_path["summary"]
        active_path = low_path["key"]
    elif trend == "flat":
        market_code = "flat_not_ready"
        market_label = f"横盘整理 · {position_text}"
        market_detail = low_path["summary"]
        active_path = low_path["key"]
    elif trend in {"trend_up", "trend_down"}:
        market_code = trend
        market_label = f"{'上涨' if trend == 'trend_up' else '下跌'}趋势 · {position_text}"
        market_detail = trend_path["summary"]
        active_path = trend_path["key"]
    elif trend in {"drift_up", "drift_down"}:
        market_code = trend
        market_label = "弱上涨过渡" if trend == "drift_up" else "弱下跌过渡"
        market_detail = "还未形成横盘正态或成熟趋势，两条路径都不发信号。"
        active_path = None
    else:
        market_code = trend or "unknown"
        market_label = "行情切换中" if trend == "transition" else "行情状态待确认"
        market_detail = "等待行情稳定归类后再选择信号路径。"
        active_path = None

    return {
        "market_state_detail": {
            "code": market_code,
            "label": market_label,
            "detail": market_detail,
            "active_path": active_path,
        },
        "normal_band": {
            "price": price if math.isfinite(price) else None,
            "center": center if math.isfinite(center) else None,
            "sigma_price": sigma_price if math.isfinite(sigma_price) else None,
            "sigma_bps": sigma_bps if math.isfinite(sigma_bps) else None,
            "lower_1sigma": _number(row, "normal_low") if math.isfinite(_number(row, "normal_low")) else None,
            "upper_1sigma": _number(row, "normal_high") if math.isfinite(_number(row, "normal_high")) else None,
            "z": z if math.isfinite(z) else None,
            "position": normal_pos or None,
        },
        "signal_paths": [low_path, trend_path],
    }


def _base_payload(row: dict[str, Any] | pd.Series) -> dict[str, Any]:
    keys = (
        "trend",
        "volatility",
        "range",
        "normal_quality",
        "normal_pos",
        "sprint",
        "volume",
    )
    payload = {key: row.get(key) for key in keys}
    for key in (
        "price",
        "normal_center",
        "normal_sigma",
        "normal_low",
        "normal_high",
        "z",
        "inside1_ratio",
        "observed_pct",
        "center_slope_bps",
        "sigma_bps",
        "sigma10_bps",
        "range10_bps",
        "ret10_bps",
        "ret30_bps",
        "ret60_bps",
        "flow5",
        "imb20",
        "sigma_expand",
        "sec_ret30_bps",
        "sec_ret60_bps",
        "flow30_now",
        "flow30_delta",
    ):
        value = _number(row, key)
        payload[key] = value if math.isfinite(value) else None
    return payload


def _decision(
    row: dict[str, Any] | pd.Series,
    cfg: MultiNormalHFStableConfig,
    signal: str | None,
    module: str | None,
    reason: str,
    reason_zh: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        **_base_payload(row),
        **_diagnostic_payload(row, cfg),
        "signal": signal,
        "module": module,
        "reason": reason,
        "reason_zh": reason_zh,
        **extra,
    }


def evaluate_snapshot(
    row: dict[str, Any] | pd.Series,
    cfg: MultiNormalHFStableConfig | None = None,
) -> dict[str, Any]:
    """Evaluate one completed-minute snapshot without using future columns."""

    cfg = cfg or MultiNormalHFStableConfig()
    trend = str(row.get("trend") or "")
    normal_quality = str(row.get("normal_quality") or "")
    sprint = str(row.get("sprint") or "")
    z = _number(row, "z")
    sigma10 = _number(row, "sigma10_bps")
    range10 = _number(row, "range10_bps")
    ret10 = _number(row, "ret10_bps")
    flow5 = _number(row, "flow5")
    imb20 = _number(row, "imb20")
    sec_ret30 = _number(row, "sec_ret30_bps")
    required = (z, sigma10, range10, ret10, flow5, imb20)
    if not all(math.isfinite(value) for value in required):
        return _decision(
            row,
            cfg,
            None,
            None,
            "snapshot_incomplete",
            "分钟特征或订单薄不完整，等待下一次完整分钟。",
        )

    lowvol_ready = (
        trend == "flat"
        and normal_quality == "normal_ready"
        and sigma10 < cfg.lowvol_sigma_max_bps
        and range10 <= cfg.lowvol_range_max_bps
        and abs(ret10) <= cfg.lowvol_abs_ret10_max_bps
        and cfg.lowvol_z_min <= abs(z) <= cfg.lowvol_z_max
    )
    if lowvol_ready:
        if not math.isfinite(sec_ret30):
            return _decision(
                row,
                cfg,
                None,
                None,
                "lowvol_short_move_incomplete",
                "低波动正态尾部已形成，但最近30秒价格变化尚不完整。",
            )
        signal = "DOWN" if z > 0.0 else "UP"
        signal_sign = 1.0 if signal == "UP" else -1.0
        signed_flow = signal_sign * flow5
        signed_ret30 = signal_sign * sec_ret30
        min_signed_ret30 = -cfg.lowvol_max_adverse_ret30_sigma * sigma10
        if signed_flow >= cfg.lowvol_min_signed_flow and signed_ret30 >= min_signed_ret30:
            side = "上沿" if signal == "DOWN" else "下沿"
            direction = "下跌" if signal == "DOWN" else "上涨"
            return _decision(
                row,
                cfg,
                signal,
                "lowvol_normal_reversion",
                "lowvol_normal_tail_reversion",
                f"超低波动正态区间{side}出现反向成交流，预测未来10分钟{direction}回归。",
                signed_flow=signed_flow,
                signed_ret30_bps=signed_ret30,
                min_signed_ret30_bps=min_signed_ret30,
                z_required=cfg.lowvol_z_min,
                z_limit=cfg.lowvol_z_max,
            )
        if signed_flow < cfg.lowvol_min_signed_flow:
            return _decision(
                row,
                cfg,
                None,
                None,
                "lowvol_flow_not_reversed",
                "价格在低波动正态尾部，但5分钟成交流尚未转向回归方向。",
                signed_flow=signed_flow,
                signed_ret30_bps=signed_ret30,
                min_signed_ret30_bps=min_signed_ret30,
            )
        return _decision(
            row,
            cfg,
            None,
            None,
            "lowvol_short_move_still_outward",
            "价格位于低波动正态尾部，但最近30秒仍明显向区间外移动，等待外冲停止。",
            signed_flow=signed_flow,
            signed_ret30_bps=signed_ret30,
            min_signed_ret30_bps=min_signed_ret30,
        )

    if trend in {"trend_up", "trend_down"}:
        trend_sign = 1.0 if trend == "trend_up" else -1.0
        matching_sprint = (
            trend_sign > 0.0 and sprint in {"up_sprint", "up_walk"}
        ) or (
            trend_sign < 0.0 and sprint in {"down_sprint", "down_walk"}
        )
        signed_z = trend_sign * z
        signed_flow = trend_sign * flow5
        signed_book = trend_sign * imb20
        z_required = (
            cfg.trend_high_vol_z_min
            if sigma10 >= cfg.trend_high_vol_sigma_min_bps
            else cfg.trend_base_z_min
        )
        if (
            matching_sprint
            and signed_z >= z_required
            and signed_flow >= cfg.trend_min_signed_flow
            and signed_book <= cfg.trend_max_signed_book
        ):
            signal = "DOWN" if trend_sign > 0.0 else "UP"
            direction = "下跌" if signal == "DOWN" else "上涨"
            return _decision(
                row,
                cfg,
                signal,
                "mature_trend_exhaustion",
                "trend_flow_strong_book_support_faded",
                f"趋势仍在冲刺，但订单薄支持已衰减，预测未来10分钟{direction}衰竭回归。",
                signed_z=signed_z,
                signed_flow=signed_flow,
                signed_book=signed_book,
                z_required=z_required,
                high_volatility=bool(sigma10 >= cfg.trend_high_vol_sigma_min_bps),
            )
        return _decision(
            row,
            cfg,
            None,
            None,
            "trend_not_mature_exhaustion",
            "趋势存在，但偏离、成交流、冲刺形态或订单薄衰减尚未同时确认。",
            signed_z=signed_z,
            signed_flow=signed_flow,
            signed_book=signed_book,
            z_required=z_required,
            matching_sprint=matching_sprint,
        )

    if trend == "flat" and abs(z) > cfg.lowvol_z_max:
        return _decision(
            row,
            cfg,
            None,
            None,
            "flat_tail_may_be_regime_shift",
            "横盘价格偏离超过可回归尾部，可能正在形成新区间，暂不逆势接单。",
        )
    return _decision(
        row,
        cfg,
        None,
        None,
        "waiting_supported_regime",
        "等待超低波动正态尾部，或等待成熟趋势出现订单薄衰减。",
    )


def last_completed_second(index: pd.DatetimeIndex) -> pd.Timestamp | None:
    if len(index) == 0:
        return None
    latest = pd.Timestamp(index.max())
    minute = latest.floor("min")
    minute_end = minute + pd.Timedelta(seconds=59)
    return minute_end if latest >= minute_end else minute - pd.Timedelta(seconds=1)


def build_snapshots(
    data: pd.DataFrame,
    source: str,
    cfg: MultiNormalHFStableConfig | None = None,
    *,
    include_future: bool = False,
    completed_only: bool = False,
) -> pd.DataFrame:
    cfg = cfg or MultiNormalHFStableConfig()
    work = data
    if completed_only:
        cutoff = last_completed_second(data.index)
        if cutoff is None:
            return pd.DataFrame()
        work = data.loc[data.index <= cutoff]
    return build_minute_snapshots(
        work,
        source,
        cfg.snapshot_config(),
        include_future=include_future,
    )


def iter_signal_decisions(
    snapshots: pd.DataFrame,
    cfg: MultiNormalHFStableConfig | None = None,
) -> Iterator[dict[str, Any]]:
    cfg = cfg or MultiNormalHFStableConfig()
    if snapshots.empty:
        return
    for row in snapshots.sort_values("time").to_dict("records"):
        decision = evaluate_snapshot(row, cfg)
        if not decision.get("signal"):
            continue
        minute_time = pd.Timestamp(row["time"])
        if minute_time.tzinfo is None:
            minute_time = minute_time.tz_localize("UTC")
        detected_time = minute_time.tz_convert("UTC") + pd.Timedelta(seconds=59)
        yield {
            **row,
            **decision,
            "minute_time": minute_time,
            "detected_time": detected_time,
        }


def evaluate_latest(
    snapshots: pd.DataFrame,
    cfg: MultiNormalHFStableConfig | None = None,
) -> dict[str, Any]:
    if snapshots.empty:
        return {
            "signal": None,
            "module": None,
            "reason": "no_completed_snapshot",
            "reason_zh": "还没有可用的完整分钟。",
        }
    row = snapshots.sort_values("time").iloc[-1].to_dict()
    minute_time = pd.Timestamp(row["time"])
    if minute_time.tzinfo is None:
        minute_time = minute_time.tz_localize("UTC")
    return {
        **row,
        **evaluate_snapshot(row, cfg),
        "minute_time": minute_time,
        "detected_time": minute_time.tz_convert("UTC") + pd.Timedelta(seconds=59),
    }
