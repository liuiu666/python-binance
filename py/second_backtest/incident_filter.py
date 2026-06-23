from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class IncidentFilterConfig:
    enabled: bool = False
    mode: str = "directional_only"
    window_sec: int = 10
    min_move_bps: float = 10.0
    min_volume_quantile: float = 0.99
    min_flow_imbalance: float = 0.80
    cooldown_sec: int = 10


def bool_from_config(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "enabled")
    return bool(value)


def incident_config_from_dict(cfg: dict[str, Any] | None) -> IncidentFilterConfig:
    cfg = cfg or {}
    return IncidentFilterConfig(
        enabled=bool_from_config(cfg.get("incident_filter_enabled"), False),
        mode=str(cfg.get("incident_filter_mode", "directional_only")).lower(),
        window_sec=max(2, int(cfg.get("incident_window_sec", 10))),
        min_move_bps=max(0.0, float(cfg.get("incident_min_move_bps", 10.0))),
        min_volume_quantile=min(
            0.9999,
            max(0.50, float(cfg.get("incident_min_volume_quantile", 0.99))),
        ),
        min_flow_imbalance=min(
            1.0,
            max(0.0, float(cfg.get("incident_min_flow_imbalance", 0.80))),
        ),
        cooldown_sec=max(0, int(cfg.get("incident_cooldown_sec", 10))),
    )


def incident_config_to_dict(cfg: IncidentFilterConfig) -> dict[str, Any]:
    return {
        "enabled": bool(cfg.enabled),
        "mode": cfg.mode,
        "windowSec": int(cfg.window_sec),
        "minMoveBps": float(cfg.min_move_bps),
        "minVolumeQuantile": float(cfg.min_volume_quantile),
        "minFlowImbalance": float(cfg.min_flow_imbalance),
        "cooldownSec": int(cfg.cooldown_sec),
    }


def latest_incident_state(bars: pd.DataFrame, cfg: IncidentFilterConfig) -> dict[str, Any]:
    if not cfg.enabled or bars is None or bars.empty:
        return {"active": False, "reason": "disabled" if not cfg.enabled else "empty_bars"}
    state = build_incident_state(bars, cfg)
    idx = len(bars) - 1
    active = bool(state["buy_shock"][idx] or state["sell_shock"][idx])
    direction = "buy" if state["buy_shock"][idx] else "sell" if state["sell_shock"][idx] else None
    return {
        "active": active,
        "direction": direction,
        "reason": "incident_active" if active else "normal",
        "moveBps": round(float(state["move_bps"][idx]), 6),
        "volume": round(float(state["volume_sum"][idx]), 8),
        "volumeThreshold": round(float(state["volume_threshold"]), 8),
        "flowImbalance": round(float(state["flow_imbalance"][idx]), 6),
        "config": incident_config_to_dict(cfg),
    }


def build_incident_state(bars: pd.DataFrame, cfg: IncidentFilterConfig) -> dict[str, Any]:
    close = bars["close"].astype(float)
    volume = bars["volume"].astype(float)
    buy = bars["buy_qty"].astype(float)
    sell = bars["sell_qty"].astype(float)
    window = max(2, int(cfg.window_sec))
    move_bps = (close / close.shift(window - 1) - 1.0).fillna(0.0) * 10000.0
    volume_sum = volume.rolling(window, min_periods=1).sum()
    net_sum = (buy - sell).rolling(window, min_periods=1).sum()
    flow_imbalance = net_sum / volume_sum.clip(lower=1e-12)
    observed = bars["observed"] if "observed" in bars.columns else pd.Series(True, index=bars.index)
    reference = volume_sum[observed.astype(bool)]
    volume_threshold = float(reference.quantile(float(cfg.min_volume_quantile))) if len(reference) else float("inf")
    move_ok = move_bps.abs() >= float(cfg.min_move_bps)
    volume_ok = volume_sum >= volume_threshold
    buy_shock = move_ok & volume_ok & (flow_imbalance >= float(cfg.min_flow_imbalance))
    sell_shock = move_ok & volume_ok & (flow_imbalance <= -float(cfg.min_flow_imbalance))
    return {
        "buy_shock": buy_shock.to_numpy(bool),
        "sell_shock": sell_shock.to_numpy(bool),
        "move_bps": move_bps.to_numpy(float),
        "volume_sum": volume_sum.to_numpy(float),
        "flow_imbalance": flow_imbalance.to_numpy(float),
        "volume_threshold": volume_threshold,
    }


def apply_incident_filter_to_signals(
    bars: pd.DataFrame,
    signals: list[dict],
    cfg: IncidentFilterConfig,
) -> tuple[list[dict], list[dict], dict[str, Any]]:
    if not cfg.enabled or cfg.cooldown_sec <= 0 or not signals:
        return list(signals), [], {"enabled": bool(cfg.enabled), "blocked": 0}
    state = build_incident_state(bars, cfg)
    buy_times = bars.index[state["buy_shock"]]
    sell_times = bars.index[state["sell_shock"]]
    buy_ns = _ns_array(buy_times)
    sell_ns = _ns_array(sell_times)
    cooldown_ns = int(cfg.cooldown_sec) * 1_000_000_000
    accepted: list[dict] = []
    blocked: list[dict] = []
    for row in sorted(signals, key=lambda item: item["time"]):
        signal = row.get("signal")
        row_ns = pd.Timestamp(row["time"]).value
        block = None
        if signal == "UP":
            block = _recent_trigger(row_ns, sell_ns, sell_times, cooldown_ns)
            reason = "incident_sell_shock_blocks_up"
        elif signal == "DOWN":
            block = _recent_trigger(row_ns, buy_ns, buy_times, cooldown_ns)
            reason = "incident_buy_shock_blocks_down"
        else:
            reason = "incident_unsupported_signal"
        if block is None:
            accepted.append(row)
            continue
        trigger_time, age_sec = block
        skipped = dict(row)
        skipped["skipReason"] = reason
        skipped["incident_trigger_time"] = trigger_time.isoformat()
        skipped["incident_age_sec"] = round(float(age_sec), 3)
        skipped["incident_filter"] = incident_config_to_dict(cfg)
        blocked.append(skipped)
    diagnostics = {
        "enabled": True,
        "mode": cfg.mode,
        "blocked": len(blocked),
        "buyShockSeconds": int(state["buy_shock"].sum()),
        "sellShockSeconds": int(state["sell_shock"].sum()),
        "volumeThreshold": round(float(state["volume_threshold"]), 8),
        "config": incident_config_to_dict(cfg),
    }
    return accepted, blocked, diagnostics


def apply_incident_filter_to_live_signals(
    bars: pd.DataFrame,
    signals: dict[str, dict],
    config_map: dict[str, dict],
) -> dict[str, dict]:
    if not signals:
        return signals
    out: dict[str, dict] = {}
    for strategy_id, row in signals.items():
        cfg = incident_config_from_dict(config_map.get(strategy_id, {}))
        if not cfg.enabled or not row.get("signal"):
            out[strategy_id] = row
            continue
        accepted, blocked, diagnostics = apply_incident_filter_to_signals(
            bars,
            [
                {
                    **row,
                    "time": pd.to_datetime(row.get("time"), utc=True),
                }
            ],
            cfg,
        )
        if accepted:
            next_row = dict(row)
            next_row["incident_filter"] = diagnostics
            out[strategy_id] = next_row
            continue
        skipped = dict(row)
        blocked_row = blocked[0]
        skipped["blocked_signal"] = skipped.get("signal")
        skipped["blocked_confidence"] = skipped.get("confidence")
        skipped["signal"] = None
        skipped["high_conf"] = False
        skipped["reason"] = blocked_row.get("skipReason", "incident_filter_blocked")
        skipped["incident_filter"] = diagnostics
        skipped["incident_trigger_time"] = blocked_row.get("incident_trigger_time")
        skipped["incident_age_sec"] = blocked_row.get("incident_age_sec")
        out[strategy_id] = skipped
    return out


def _ns_array(index: pd.DatetimeIndex) -> np.ndarray:
    return np.array([pd.Timestamp(item).value for item in index], dtype=np.int64)


def _recent_trigger(row_ns: int, trigger_ns: np.ndarray, trigger_times: pd.DatetimeIndex, cooldown_ns: int):
    if len(trigger_ns) == 0:
        return None
    pos = int(np.searchsorted(trigger_ns, row_ns, side="right") - 1)
    if pos < 0:
        return None
    age_ns = row_ns - int(trigger_ns[pos])
    if age_ns > cooldown_ns:
        return None
    return pd.Timestamp(trigger_times[pos]), age_ns / 1_000_000_000.0
