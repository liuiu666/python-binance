"""Replay the current online strategy configuration on the latest local pull."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from backtest_io import read_orderbook  # noqa: E402
from current_v2_augmented_v9_core import AugmentedV9Rules, original_v2_regime_veto_code  # noqa: E402
from liquidity_v2_core import LiquidityV2Rules, build_features, evaluate_candidate, normal_ready  # noqa: E402
from multiscale_phase_gate_core import MultiscalePhaseGateConfig, build_snapshots  # noqa: E402
from second_backtest.data import load_second_bars  # noqa: E402


DATA = ROOT / "tmp" / "frozen_position_forward"
SECONDS = DATA / "btcusdt_1s_trades.csv"
ORDERBOOK = DATA / "btcusdt_orderbook_1s.csv"
CONFIG_API = "http://115.190.218.128:3000/api/config"
OUT_JSON = ROOT / "tmp" / "online_strategies_latest_backtest.json"
OUT_CSV = ROOT / "tmp" / "online_strategies_latest_backtest_trades.csv"
CONFIG_SNAPSHOT = ROOT / "tmp" / "online_config_latest_snapshot.json"
DELAYS = (0, 5, 6, 10)


def fetch_config() -> dict[str, Any]:
    with urlopen(CONFIG_API, timeout=20) as response:  # noqa: S310 - fixed production endpoint
        config = json.load(response)
    CONFIG_SNAPSHOT.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config


def variant(config: dict[str, Any], strategy_id: str) -> dict[str, Any]:
    for row in config.get("strategyVariants", []):
        if row.get("id") == strategy_id:
            return row
    raise KeyError(strategy_id)


def liquidity_rules(row: dict[str, Any]) -> LiquidityV2Rules:
    return LiquidityV2Rules(
        normal_window_sec=int(row.get("normalWindowSec", 600)),
        horizon_sec=int(row.get("horizonSec", 600)),
        min_gap_sec=int(row.get("gapSec", 600)),
        z_entry=float(row.get("zEntry", 1.2)),
        z_reclaim=float(row.get("zReclaim", 0.85)),
        retest_sec=int(row.get("retestSec", 120)),
        inside_min=float(row.get("insideMin", 0.55)),
        observed_min_pct=float(row.get("observedMinPct", 88.0)),
        center_slope_sec=int(row.get("centerSlopeSec", 300)),
        center_slope_max_bps=float(row.get("centerSlopeMaxBps", 8.0)),
        sigma_min_bps=float(row.get("sigmaMinBps", 5.8)),
        sigma_max_bps=float(row.get("sigmaMaxBps", 55.0)),
        sigma_expand_max=float(row.get("sigmaExpandMax", 1.9)),
        orderbook_max_age_sec=int(row.get("orderbookMaxAgeSec", 3)),
        ob_imbalance_min=float(row.get("obImbalanceMin", 0.08)),
        micro_min_bps=float(row.get("microMinBps", 0.001)),
        wall_ratio_min=float(row.get("wallRatioMin", 1.0)),
        flow_guard=float(row.get("flowGuard", 0.12)),
        true_break_flow=float(row.get("trueBreakFlow", 0.28)),
        true_break_imbalance=float(row.get("trueBreakImbalance", 0.28)),
        bidwall_trap_enabled=bool(row.get("bidwallTrapEnabled", True)),
        bidwall_trap_ret300_max_bps=float(row.get("bidwallTrapRet300MaxBps", -5.0)),
        bidwall_trap_bid20_chg60_min=float(row.get("bidwallTrapBid20Chg60Min", 2.0)),
        bidwall_trap_ret600_min_bps=float(row.get("bidwallTrapRet600MinBps", -20.0)),
        quality_v2_enabled=bool(row.get("qualityV2Enabled", True)),
        quality_v2_down_bid20_chg60_min=float(row.get("qualityV2DownBid20Chg60Min", -0.7)),
        quality_v2_up_flow60_min=float(row.get("qualityV2UpFlow60Min", -0.063)),
        trend_space_enabled=bool(row.get("trendSpaceEnabled", True)),
        trend_space_sigma_expand_max=float(row.get("trendSpaceSigmaExpandMax", 1.6)),
        trend_space_center_slope_abs_max_bps=float(row.get("trendSpaceCenterSlopeAbsMaxBps", 6.0)),
        trend_space_inside_max=float(row.get("trendSpaceInsideMax", 0.75)),
        trend_space_trend_ret_1800_bps=float(row.get("trendSpaceTrendRet1800Bps", 15.0)),
        trend_space_up_pos_1800_min=float(row.get("trendSpaceUpPos1800Min", 0.72)),
        trend_space_down_pos_1800_max=float(row.get("trendSpaceDownPos1800Max", 0.28)),
        trend_space_block_countertrend=bool(row.get("trendSpaceBlockCountertrend", True)),
        trend_space_block_upper_fade_pullback=bool(row.get("trendSpaceBlockUpperFadePullback", True)),
        trend_space_short_ret_600_up_bps=float(row.get("trendSpaceShortRet600UpBps", 12.0)),
        trend_space_short_pos_600_min=float(row.get("trendSpaceShortPos600Min", 0.65)),
        mode=str(row.get("liquidityMode", "reclaim")),
    )


def phase_config(row: dict[str, Any]) -> MultiscalePhaseGateConfig:
    return MultiscalePhaseGateConfig(
        horizon_sec=int(row.get("horizonSec", 600)),
        min_gap_sec=int(row.get("gapSec", 600)),
        orderbook_max_age_sec=int(row.get("orderbookMaxAgeSec", 3)),
        max_emit_age_sec=int(row.get("maxEmitAgeSec", 8)),
        phase_lookback_sec=int(row.get("phaseLookbackSec", 3600)),
        maturity_history_sec=int(row.get("maturityHistorySec", 3600)),
        maturity_min_periods=int(row.get("maturityMinPeriods", 1800)),
        maturity_quantile=float(row.get("maturityQuantile", 0.75)),
        min_flow60=float(row.get("minFlow60", 0.08)),
        min_imbalance20=float(row.get("minImbalance20", 0.05)),
        min_microprice_bps=float(row.get("minMicropriceBps", 0.0)),
        min_volume_ratio=float(row.get("minVolumeRatio", 0.8)),
    )


def price_at(data: pd.DataFrame, target: pd.Timestamp) -> tuple[pd.Timestamp, float] | None:
    pos = int(data.index.searchsorted(target))
    if pos >= len(data) or (data.index[pos] - target).total_seconds() > 1.0:
        return None
    return data.index[pos], float(data["close"].iloc[pos])


def outcome_fields(data: pd.DataFrame, timestamp: pd.Timestamp, signal: str) -> dict[str, Any] | None:
    direction = 1.0 if signal == "UP" else -1.0
    result: dict[str, Any] = {}
    for delay in DELAYS:
        entry = price_at(data, timestamp + pd.Timedelta(seconds=delay))
        settle = price_at(data, timestamp + pd.Timedelta(seconds=delay + 600))
        if entry is None or settle is None:
            return None
        signed = (settle[1] / entry[1] - 1.0) * 10000.0 * direction
        result[f"entry_time_d{delay}"] = entry[0]
        result[f"settle_time_d{delay}"] = settle[0]
        result[f"signed_bps_d{delay}"] = signed
        result[f"won_d{delay}"] = bool(signed > 0.0)
    return result


def causal_incident_blocked(data: pd.DataFrame, pos: int, signal: str, cfg: dict[str, Any]) -> bool:
    if not bool(cfg.get("incidentFilterEnabled", False)):
        return False
    window = max(2, int(cfg.get("incidentWindowSec", 10)))
    cooldown = max(0, int(cfg.get("incidentCooldownSec", 10)))
    history_start = max(0, pos - 7800)
    history = data.iloc[history_start : pos + 1]
    close = history["close"].astype(float)
    volume = history["volume"].astype(float)
    buy = history["buy_qty"].astype(float)
    sell = history["sell_qty"].astype(float)
    move = (close / close.shift(window - 1) - 1.0).fillna(0.0) * 10000.0
    volume_sum = volume.rolling(window, min_periods=1).sum()
    flow = (buy - sell).rolling(window, min_periods=1).sum() / volume_sum.clip(lower=1e-12)
    threshold = float(volume_sum.quantile(float(cfg.get("incidentMinVolumeQuantile", 0.99))))
    active = (
        (move.abs() >= float(cfg.get("incidentMinMoveBps", 10.0)))
        & (volume_sum >= threshold)
        & (flow.abs() >= float(cfg.get("incidentMinFlowImbalance", 0.8)))
    )
    recent = history.loc[active].tail(cooldown + 1)
    if recent.empty:
        return False
    recent = recent[(history.index[-1] - recent.index).total_seconds() <= cooldown]
    if recent.empty:
        return False
    if signal == "UP":
        return bool((flow.loc[recent.index] < 0.0).any())
    return bool((flow.loc[recent.index] > 0.0).any())


def replay_liquidity(data: pd.DataFrame, row: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rules = liquidity_rules(row)
    v9_rules = AugmentedV9Rules.from_config(row)
    features = build_features(data, rules)
    warmup = max(3600, rules.normal_window_sec, rules.center_slope_sec, rules.retest_sec) + 10
    last_owner = -10**9
    trades: list[dict[str, Any]] = []
    counts = {"wait": 0, "veto": 0, "accepted": 0, "incident": 0, "trades": 0, "v9_original_regime_veto": 0}
    for pos in range(warmup, len(data) - 610):
        feature = features.iloc[pos]
        if not bool(feature.get("ob_available", False)) or not normal_ready(feature, rules):
            continue
        if pos - last_owner < rules.min_gap_sec:
            continue
        decision = evaluate_candidate(feature, rules)
        status = str(decision.get("status") or "wait")
        counts[status] = counts.get(status, 0) + 1
        if status == "wait":
            continue
        last_owner = pos
        if status != "accepted":
            continue
        signal = str(decision["signal"])
        timestamp = data.index[pos]
        regime_veto = original_v2_regime_veto_code(signal, feature, v9_rules)
        if regime_veto is not None:
            counts["v9_original_regime_veto"] += 1
            continue
        if causal_incident_blocked(data, pos, signal, row):
            counts["incident"] += 1
            continue
        outcome = outcome_fields(data, timestamp, signal)
        if outcome is None:
            continue
        counts["trades"] += 1
        trades.append({
            "strategy_id": row["id"],
            "time": timestamp,
            "signal": signal,
            "reason": decision.get("reason"),
            **outcome,
        })
    return trades, counts


def replay_phase(data: pd.DataFrame, row: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    cfg = phase_config(row)
    snapshots = build_snapshots(data, cfg)
    trades: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    last_owner: pd.Timestamp | None = None
    for candidate in snapshots.to_dict("records"):
        phase = str(candidate.get("phase") or "unknown")
        counts[phase] = counts.get(phase, 0) + 1
        signal = candidate.get("signal")
        if signal not in {"UP", "DOWN"}:
            continue
        timestamp = pd.Timestamp(candidate["detected_time"])
        if last_owner is not None and (timestamp - last_owner).total_seconds() < cfg.min_gap_sec:
            continue
        last_owner = timestamp
        outcome = outcome_fields(data, timestamp, signal)
        if outcome is None:
            continue
        trades.append({
            "strategy_id": row["id"],
            "time": timestamp,
            "signal": signal,
            "reason": candidate.get("reason"),
            "phase": phase,
            **outcome,
        })
    return trades, counts


def metrics(frame: pd.DataFrame, delay: int, amount: float, hours: float) -> dict[str, Any]:
    if frame.empty:
        return {"trades": 0, "winRate": None, "pnlU": 0.0, "tradesPerDay": 0.0}
    wins = frame[f"won_d{delay}"].astype(bool)
    pnl = np.where(wins, amount * 0.8, -amount)
    equity = np.cumsum(pnl)
    prior_peak = np.maximum.accumulate(np.r_[0.0, equity])[:-1]
    streak = maximum = 0
    for won in wins:
        streak = 0 if won else streak + 1
        maximum = max(maximum, streak)
    return {
        "trades": int(len(frame)),
        "wins": int(wins.sum()),
        "winRate": round(float(wins.mean()) * 100.0, 2),
        "pnlU": round(float(pnl.sum()), 2),
        "maxDrawdownU": round(float(np.max(np.maximum(0.0, prior_peak - equity))), 2),
        "maxLossStreak": int(maximum),
        "tradesPerDay": round(len(frame) / max(hours, 1.0) * 24.0, 2),
        "medianSignedBps": round(float(frame[f"signed_bps_d{delay}"].median()), 3),
        "thinMarginPctLe3bp": round(float((frame[f"signed_bps_d{delay}"].abs() <= 3.0).mean()) * 100.0, 2),
    }


def summarize(trades: list[dict[str, Any]], amount: float, hours: float) -> dict[str, Any]:
    frame = pd.DataFrame(trades)
    if frame.empty:
        return {"delay6": metrics(frame, 6, amount, hours), "byShanghaiDay": {}}
    frame = frame.sort_values("time")
    frame["shanghai_day"] = pd.to_datetime(frame["time"], utc=True).dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    return {
        "delaySensitivity": {str(delay): metrics(frame, delay, amount, hours) for delay in DELAYS},
        "byShanghaiDay": {
            str(day): metrics(group, 6, amount, 24.0) for day, group in frame.groupby("shanghai_day")
        },
        "byDirection": {
            str(signal): metrics(group, 6, amount, hours) for signal, group in frame.groupby("signal")
        },
    }


def main() -> None:
    config = fetch_config()
    bars = load_second_bars(SECONDS, include_shards=False)
    orderbook = read_orderbook(ORDERBOOK, bars.index, max_age_sec=3)
    data = bars.join(orderbook, how="left").sort_index()
    hours = (data.index.max() - data.index.min()).total_seconds() / 3600.0
    v2 = variant(config, "BTC_10min_NORMAL_LIQ_OB_V2_QUALITY")
    phase = variant(config, "BTC_10min_MULTISCALE_PHASE_GATE_V1")
    v2_trades, v2_counts = replay_liquidity(data, v2)
    phase_trades, phase_counts = replay_phase(data, phase)
    all_trades = pd.DataFrame([*v2_trades, *phase_trades])
    if not all_trades.empty:
        all_trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    report = {
        "method": {
            "configSource": CONFIG_API,
            "sharedCores": ["liquidity_v2_core", "multiscale_phase_gate_core"],
            "entryDelaysSec": DELAYS,
            "settlementFromEntrySec": 600,
            "payoutRate": 0.8,
            "serverBacktest": False,
        },
        "data": {
            "start": data.index.min(),
            "end": data.index.max(),
            "hours": round(hours, 3),
            "secondCoveragePct": round(float(data["observed"].mean()) * 100.0, 4),
            "orderbookCoveragePct": round(float(data["ob_available"].mean()) * 100.0, 4),
        },
        "strategies": {
            v2["id"]: {
                "amount": float(v2.get("amount", 5.0)),
                "candidateCounts": v2_counts,
                **summarize(v2_trades, float(v2.get("amount", 5.0)), hours),
            },
            phase["id"]: {
                "amount": float(phase.get("amount", 10.0)),
                "phaseCounts": phase_counts,
                **summarize(phase_trades, float(phase.get("amount", 10.0)), hours),
            },
        },
        "warning": "This latest period has already been inspected and is explanatory, not a new untouched holdout.",
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
