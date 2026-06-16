from __future__ import annotations

import math
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

import numpy as np
import pandas as pd

from .execution import apply_signal_gap, execute_signals
from .metrics import compact_metrics, payout_for_horizon, robust_score, split_metrics
from .strategies import (
    SecondChipConfig,
    SecondNormalConfig,
    build_chip_features,
    generate_chip_signals,
    generate_normal_signals,
    settle_signal,
)


@dataclass(frozen=True)
class DirectionalNormalConfig:
    strategy_id: str
    lookback_sec: int
    horizon_sec: int = 600
    signal_gap_sec: int = 600
    tail_pct: float = 0.20
    second_filter: str = "none"
    direction_filter: str = "all"
    amount: float = 5.0
    label: str = "directional_normal_tail"


@dataclass(frozen=True)
class RobustZConfig:
    strategy_id: str
    lookback_sec: int
    horizon_sec: int = 600
    signal_gap_sec: int = 600
    z_abs: float = 0.75
    amount: float = 5.0
    label: str = "robust_z_reversal"


@dataclass(frozen=True)
class VwapDeviationConfig:
    strategy_id: str
    lookback_sec: int
    horizon_sec: int = 600
    signal_gap_sec: int = 600
    z_abs: float = 2.0
    min_volume_rank: float = 0.0
    amount: float = 5.0
    label: str = "vwap_deviation_reversal"


@dataclass(frozen=True)
class FlowMoveConfig:
    strategy_id: str
    trend_sec: int
    horizon_sec: int = 600
    signal_gap_sec: int = 600
    min_move_pct: float = 0.0015
    ratio_hi: float = 1.20
    ratio_lo: float = 0.80
    mode: str = "divergence"
    direction_filter: str = "all"
    amount: float = 5.0
    label: str = "flow_move"


def build_research_configs() -> list[Any]:
    configs: list[Any] = []

    for lookback in (1800, 3600, 7200):
        for tail in (0.15, 0.20, 0.23, 0.27):
            for gap in (600, 1800):
                pct = int(round(tail * 100))
                configs.append(
                    SecondNormalConfig(
                        strategy_id=f"RESEARCH_NORMAL_{lookback}_{pct}_{gap}",
                        lookback_sec=lookback,
                        horizon_sec=600,
                        signal_gap_sec=gap,
                        tail_pct=tail,
                        second_filter="none",
                        amount=5,
                        label=f"normal_tail lb={lookback}s tail={pct}% gap={gap}s",
                    )
                )

    for lookback in (3000, 3600, 4200):
        for tail in (0.20, 0.22, 0.23, 0.25):
            for direction in ("up_only", "down_only"):
                pct = int(round(tail * 100))
                configs.append(
                    DirectionalNormalConfig(
                        strategy_id=f"RESEARCH_DIR_NORMAL_{lookback}_{pct}_{direction.upper()}",
                        lookback_sec=lookback,
                        horizon_sec=600,
                        signal_gap_sec=1200,
                        tail_pct=tail,
                        second_filter="none",
                        direction_filter=direction,
                        amount=5,
                        label=(
                            f"directional normal lb={lookback}s tail={pct}% "
                            f"gap=1200s {direction}"
                        ),
                    )
                )

    for lookback in (1800, 3600, 7200):
        for z_abs in (0.35, 0.50, 0.75, 1.00):
            for gap in (600, 1800):
                configs.append(
                    RobustZConfig(
                        strategy_id=f"RESEARCH_ROBUSTZ_{lookback}_{int(z_abs * 100)}_{gap}",
                        lookback_sec=lookback,
                        horizon_sec=600,
                        signal_gap_sec=gap,
                        z_abs=z_abs,
                        amount=5,
                        label=f"robust_z lb={lookback}s z={z_abs:g} gap={gap}s",
                    )
                )

    for lookback in (900, 1800, 3600):
        for z_abs in (1.00, 1.50, 2.00, 2.50):
            for gap in (600, 1800):
                configs.append(
                    VwapDeviationConfig(
                        strategy_id=f"RESEARCH_VWAP_{lookback}_{int(z_abs * 100)}_{gap}",
                        lookback_sec=lookback,
                        horizon_sec=600,
                        signal_gap_sec=gap,
                        z_abs=z_abs,
                        amount=5,
                        label=f"vwap_deviation lb={lookback}s z={z_abs:g} gap={gap}s",
                    )
                )

    for trend in (60, 180, 300, 600):
        for min_move in (0.0008, 0.0015, 0.0025):
            for ratio_hi, ratio_lo in ((1.05, 0.95), (1.20, 0.80)):
                for mode in ("divergence", "exhaustion"):
                    configs.append(
                        FlowMoveConfig(
                            strategy_id=(
                                f"RESEARCH_FLOW_{mode.upper()}_{trend}_"
                                f"{int(min_move * 10000)}_{int(ratio_hi * 100)}"
                            ),
                            trend_sec=trend,
                            horizon_sec=600,
                            signal_gap_sec=600,
                            min_move_pct=min_move,
                            ratio_hi=ratio_hi,
                            ratio_lo=ratio_lo,
                            mode=mode,
                            direction_filter="all",
                            amount=5,
                            label=(
                                f"flow_{mode} trend={trend}s move={min_move:.4f} "
                                f"ratio={ratio_lo:g}/{ratio_hi:g}"
                            ),
                        )
                    )

    configs.extend(
        [
            SecondChipConfig(
                strategy_id="RESEARCH_CHIP_1800_20_20_40_ALL_WIDTH3",
                lookback_sec=1800,
                horizon_sec=600,
                signal_gap_sec=300,
                target_share=0.20,
                bin_size=20,
                break_pct=0.004,
                direction_filter="all",
                chip_filter="width_lte_3",
                amount=5,
                label="chip lb=1800 share=20 bin=20 break=0.40% all width<=3",
            ),
            SecondChipConfig(
                strategy_id="RESEARCH_CHIP_3600_20_20_23_UP",
                lookback_sec=3600,
                horizon_sec=600,
                signal_gap_sec=600,
                target_share=0.20,
                bin_size=20,
                break_pct=0.0023,
                direction_filter="breakout_up_only",
                chip_filter="none",
                amount=5,
                label="chip lb=3600 share=20 bin=20 break=0.23% up only",
            ),
            SecondChipConfig(
                strategy_id="RESEARCH_CHIP_3600_50_50_30_FLOW",
                lookback_sec=3600,
                horizon_sec=600,
                signal_gap_sec=1800,
                target_share=0.50,
                bin_size=50,
                break_pct=0.003,
                direction_filter="all",
                chip_filter="flow_reversal",
                amount=5,
                label="chip lb=3600 share=50 bin=50 break=0.30% flow reversal",
            ),
            SecondChipConfig(
                strategy_id="RESEARCH_CHIP_7200_50_50_30_FLOW",
                lookback_sec=7200,
                horizon_sec=600,
                signal_gap_sec=1800,
                target_share=0.50,
                bin_size=50,
                break_pct=0.003,
                direction_filter="all",
                chip_filter="flow_reversal",
                amount=5,
                label="chip lb=7200 share=50 bin=50 break=0.30% flow reversal",
            ),
            SecondChipConfig(
                strategy_id="RESEARCH_CHIP_3600_35_20_30_WIDTH5",
                lookback_sec=3600,
                horizon_sec=600,
                signal_gap_sec=600,
                target_share=0.35,
                bin_size=20,
                break_pct=0.003,
                direction_filter="all",
                chip_filter="width_lte_5",
                amount=5,
                label="chip lb=3600 share=35 bin=20 break=0.30% width<=5",
            ),
            SecondChipConfig(
                strategy_id="RESEARCH_CHIP_1800_35_20_30_ALL",
                lookback_sec=1800,
                horizon_sec=600,
                signal_gap_sec=600,
                target_share=0.35,
                bin_size=20,
                break_pct=0.003,
                direction_filter="all",
                chip_filter="none",
                amount=5,
                label="chip lb=1800 share=35 bin=20 break=0.30% all",
            ),
            SecondChipConfig(
                strategy_id="RESEARCH_CHIP_3600_20_20_40_WIDTH3",
                lookback_sec=3600,
                horizon_sec=600,
                signal_gap_sec=600,
                target_share=0.20,
                bin_size=20,
                break_pct=0.004,
                direction_filter="all",
                chip_filter="width_lte_3",
                amount=5,
                label="chip lb=3600 share=20 bin=20 break=0.40% width<=3",
            ),
            SecondChipConfig(
                strategy_id="RESEARCH_CHIP_1800_20_50_30_FLOW",
                lookback_sec=1800,
                horizon_sec=600,
                signal_gap_sec=600,
                target_share=0.20,
                bin_size=50,
                break_pct=0.003,
                direction_filter="all",
                chip_filter="flow_reversal",
                amount=5,
                label="chip lb=1800 share=20 bin=50 break=0.30% flow reversal",
            ),
        ]
    )

    return configs


def generate_robust_z_signals(
    bars: pd.DataFrame,
    cfg: RobustZConfig,
    *,
    apply_config_gap: bool = True,
) -> list[dict]:
    close = bars["close"].to_numpy(float)
    if len(close) <= cfg.lookback_sec + cfg.horizon_sec:
        return []

    logp = np.log(close)
    lr = np.diff(logp, prepend=np.nan)
    series = pd.Series(lr, index=bars.index)
    min_periods = max(60, min(cfg.lookback_sec, cfg.lookback_sec // 4))
    median = series.rolling(cfg.lookback_sec, min_periods=min_periods).median().to_numpy()
    q75 = series.rolling(cfg.lookback_sec, min_periods=min_periods).quantile(0.75).to_numpy()
    q25 = series.rolling(cfg.lookback_sec, min_periods=min_periods).quantile(0.25).to_numpy()
    robust_sigma = (q75 - q25) / 1.349

    rows: list[dict] = []
    for i in range(cfg.lookback_sec, len(close) - cfg.horizon_sec):
        sigma = robust_sigma[i]
        if not np.isfinite(median[i]) or not np.isfinite(sigma) or sigma < 1e-12:
            continue
        z = float(cfg.horizon_sec * median[i] / (math.sqrt(cfg.horizon_sec) * sigma))
        if z >= cfg.z_abs:
            signal = "DOWN"
        elif z <= -cfg.z_abs:
            signal = "UP"
        else:
            continue
        rows.append(
            settle_signal(
                bars=bars,
                idx=i,
                strategy_id=cfg.strategy_id,
                model_type="research_robust_z",
                signal=signal,
                horizon_sec=cfg.horizon_sec,
                amount=cfg.amount,
                extra={
                    "z_score": round(float(z), 6),
                    "z_abs": float(cfg.z_abs),
                    "lookback_sec": int(cfg.lookback_sec),
                },
            )
        )
    return apply_signal_gap(rows, cfg.signal_gap_sec) if apply_config_gap else rows


def generate_vwap_deviation_signals(
    bars: pd.DataFrame,
    cfg: VwapDeviationConfig,
    *,
    apply_config_gap: bool = True,
) -> list[dict]:
    close_s = bars["close"].astype(float)
    volume_s = bars["volume"].astype(float)
    if len(close_s) <= cfg.lookback_sec + cfg.horizon_sec:
        return []

    min_periods = max(60, min(cfg.lookback_sec, cfg.lookback_sec // 4))
    vol_sum = volume_s.rolling(cfg.lookback_sec, min_periods=min_periods).sum()
    pxv_sum = (close_s * volume_s).rolling(cfg.lookback_sec, min_periods=min_periods).sum()
    mean = close_s.rolling(cfg.lookback_sec, min_periods=min_periods).mean()
    vwap = (pxv_sum / vol_sum.replace(0, np.nan)).fillna(mean)
    sigma = close_s.rolling(cfg.lookback_sec, min_periods=min_periods).std(ddof=1)
    dev = ((close_s - vwap) / sigma.replace(0, np.nan)).to_numpy(float)

    volume_rank = np.full(len(close_s), np.nan)
    if cfg.min_volume_rank > 0:
        vol60 = volume_s.rolling(60, min_periods=1).sum()
        volume_rank = (
            vol60.rolling(cfg.lookback_sec, min_periods=30)
            .apply(lambda values: float((values <= values[-1]).mean()), raw=True)
            .to_numpy(float)
        )

    rows: list[dict] = []
    for i in range(cfg.lookback_sec, len(close_s) - cfg.horizon_sec):
        score = dev[i]
        if not np.isfinite(score):
            continue
        if cfg.min_volume_rank > 0 and (
            not np.isfinite(volume_rank[i]) or volume_rank[i] < cfg.min_volume_rank
        ):
            continue
        if score >= cfg.z_abs:
            signal = "DOWN"
        elif score <= -cfg.z_abs:
            signal = "UP"
        else:
            continue
        rows.append(
            settle_signal(
                bars=bars,
                idx=i,
                strategy_id=cfg.strategy_id,
                model_type="research_vwap_deviation",
                signal=signal,
                horizon_sec=cfg.horizon_sec,
                amount=cfg.amount,
                extra={
                    "vwap_z": round(float(score), 6),
                    "z_abs": float(cfg.z_abs),
                    "lookback_sec": int(cfg.lookback_sec),
                    "volume_rank_60s": None
                    if not np.isfinite(volume_rank[i])
                    else round(float(volume_rank[i]), 6),
                },
            )
        )
    return apply_signal_gap(rows, cfg.signal_gap_sec) if apply_config_gap else rows


def generate_flow_move_signals(
    bars: pd.DataFrame,
    cfg: FlowMoveConfig,
    *,
    apply_config_gap: bool = True,
) -> list[dict]:
    close = bars["close"].to_numpy(float)
    if len(close) <= cfg.trend_sec + cfg.horizon_sec:
        return []
    buy = bars["buy_qty"].astype(float).rolling(cfg.trend_sec, min_periods=1).sum().to_numpy()
    sell = bars["sell_qty"].astype(float).rolling(cfg.trend_sec, min_periods=1).sum().to_numpy()
    ratio = np.full(len(close), np.inf, dtype=float)
    np.divide(buy, sell, out=ratio, where=sell > 0)

    rows: list[dict] = []
    mode = cfg.mode.lower()
    for i in range(cfg.trend_sec, len(close) - cfg.horizon_sec):
        move = close[i] / max(close[i - cfg.trend_sec], 1e-12) - 1.0
        if not np.isfinite(move) or not np.isfinite(ratio[i]):
            continue
        signal = None
        if mode == "divergence":
            if move <= -cfg.min_move_pct and ratio[i] >= cfg.ratio_hi:
                signal = "UP"
            elif move >= cfg.min_move_pct and ratio[i] <= cfg.ratio_lo:
                signal = "DOWN"
        elif mode == "exhaustion":
            if move <= -cfg.min_move_pct and ratio[i] <= cfg.ratio_lo:
                signal = "UP"
            elif move >= cfg.min_move_pct and ratio[i] >= cfg.ratio_hi:
                signal = "DOWN"
        else:
            raise ValueError(f"unsupported flow mode: {cfg.mode}")
        if not signal:
            continue
        if not _research_direction_allowed(cfg.direction_filter, signal):
            continue
        rows.append(
            settle_signal(
                bars=bars,
                idx=i,
                strategy_id=cfg.strategy_id,
                model_type=f"research_flow_{mode}",
                signal=signal,
                horizon_sec=cfg.horizon_sec,
                amount=cfg.amount,
                extra={
                    "trend_sec": int(cfg.trend_sec),
                    "move_pct": round(float(move), 8),
                    "flow_ratio": round(float(ratio[i]), 6),
                    "min_move_pct": float(cfg.min_move_pct),
                    "ratio_hi": float(cfg.ratio_hi),
                    "ratio_lo": float(cfg.ratio_lo),
                    "direction_filter": cfg.direction_filter,
                },
            )
        )
    return apply_signal_gap(rows, cfg.signal_gap_sec) if apply_config_gap else rows


def run_research_scan(
    bars: pd.DataFrame,
    configs: list[Any] | None = None,
    *,
    global_lock_sec: int = 0,
) -> list[dict]:
    reports = []
    for cfg in configs or build_research_configs():
        raw = signals_for_config(bars, cfg, apply_config_gap=False)
        gap = apply_signal_gap(raw, int(getattr(cfg, "signal_gap_sec", 0)))
        executed, rejected = execute_signals(
            gap,
            per_strategy_lock=True,
            global_lock_sec=global_lock_sec,
            cooldown_sec=int(getattr(cfg, "horizon_sec", 600)),
            use_horizon_as_lock=True,
        )
        payout = payout_for_horizon(int(getattr(cfg, "horizon_sec", 600)))
        amount = float(getattr(cfg, "amount", 5.0))
        metrics = split_metrics(
            executed,
            bars.index.min(),
            bars.index.max(),
            amount=amount,
            payout_rate=payout,
        )
        raw_metrics = split_metrics(
            raw,
            bars.index.min(),
            bars.index.max(),
            amount=amount,
            payout_rate=payout,
        )
        reports.append(
            {
                "strategyId": getattr(cfg, "strategy_id"),
                "label": getattr(cfg, "label", getattr(cfg, "strategy_id")),
                "family": family_for_config(cfg),
                "params": config_to_dict(cfg),
                "score": robust_score(metrics),
                "stability": stability_summary(metrics),
                "rawSignals": compact_metrics(raw_metrics),
                "execution": {
                    "metrics": compact_metrics(metrics),
                    "policy": {
                        "perStrategyLock": True,
                        "globalLockSec": int(global_lock_sec),
                        "configuredGapSec": int(getattr(cfg, "signal_gap_sec", 0)),
                        "cooldownSec": int(getattr(cfg, "horizon_sec", 600)),
                        "accepted": len(executed),
                        "rejectedByStrategyLock": sum(
                            1 for row in rejected if row.get("skipReason") == "strategy_lock"
                        ),
                        "rejectedByGlobalLock": sum(
                            1 for row in rejected if row.get("skipReason") == "global_lock"
                        ),
                    },
                    "sampleTrades": [compact_trade(row) for row in executed[-8:]],
                },
            }
        )
    reports.sort(
        key=lambda item: (
            item["score"],
            item["execution"]["metrics"]["all"]["winRate"] or 0,
            item["execution"]["metrics"]["all"]["trades"],
        ),
        reverse=True,
    )
    return reports


def signals_for_config(
    bars: pd.DataFrame,
    cfg: Any,
    *,
    apply_config_gap: bool = True,
) -> list[dict]:
    if isinstance(cfg, SecondNormalConfig):
        return generate_normal_signals(bars, cfg, apply_config_gap=apply_config_gap)
    if isinstance(cfg, DirectionalNormalConfig):
        base = SecondNormalConfig(
            strategy_id=cfg.strategy_id,
            lookback_sec=cfg.lookback_sec,
            horizon_sec=cfg.horizon_sec,
            signal_gap_sec=cfg.signal_gap_sec,
            tail_pct=cfg.tail_pct,
            second_filter=cfg.second_filter,
            amount=cfg.amount,
            label=cfg.label,
        )
        rows = [
            row
            for row in generate_normal_signals(bars, base, apply_config_gap=False)
            if _research_direction_allowed(cfg.direction_filter, row.get("signal"))
        ]
        return apply_signal_gap(rows, cfg.signal_gap_sec) if apply_config_gap else rows
    if isinstance(cfg, SecondChipConfig):
        return generate_chip_signals(bars, cfg, apply_config_gap=apply_config_gap)
    if isinstance(cfg, RobustZConfig):
        return generate_robust_z_signals(bars, cfg, apply_config_gap=apply_config_gap)
    if isinstance(cfg, VwapDeviationConfig):
        return generate_vwap_deviation_signals(bars, cfg, apply_config_gap=apply_config_gap)
    if isinstance(cfg, FlowMoveConfig):
        return generate_flow_move_signals(bars, cfg, apply_config_gap=apply_config_gap)
    raise TypeError(f"unsupported research config: {cfg!r}")


def family_for_config(cfg: Any) -> str:
    if isinstance(cfg, SecondNormalConfig):
        return "normal_tail"
    if isinstance(cfg, DirectionalNormalConfig):
        return "directional_normal_tail"
    if isinstance(cfg, SecondChipConfig):
        return "chip_zone"
    if isinstance(cfg, RobustZConfig):
        return "robust_z"
    if isinstance(cfg, VwapDeviationConfig):
        return "vwap_deviation"
    if isinstance(cfg, FlowMoveConfig):
        return f"flow_{cfg.mode.lower()}"
    return "unknown"


def _research_direction_allowed(direction_filter: str, signal: str) -> bool:
    name = str(direction_filter or "all").lower()
    if name in ("", "none", "all"):
        return True
    if name in ("up", "up_only", "long_only"):
        return signal == "UP"
    if name in ("down", "down_only", "short_only"):
        return signal == "DOWN"
    return True


def config_to_dict(cfg: Any) -> dict:
    if is_dataclass(cfg):
        return asdict(cfg)
    return dict(cfg)


def stability_summary(metrics: dict) -> dict:
    thirds = [
        item["winRate"]
        for item in metrics.get("thirds", [])
        if item.get("winRate") is not None and item.get("trades", 0) > 0
    ]
    all_m = metrics["all"]
    last = metrics["last24h"]
    before = metrics["beforeLast24h"]
    min_third = min(thirds) if thirds else None
    std_third = round(float(np.std(thirds)), 2) if len(thirds) >= 2 else None
    warnings = []
    if all_m["trades"] < 12:
        warnings.append("low_total_trades")
    if last["trades"] < 3:
        warnings.append("low_last24_trades")
    if min_third is not None and min_third < 45:
        warnings.append("weak_split")
    if before["winRate"] is not None and last["winRate"] is not None:
        if last["winRate"] + 10 < before["winRate"]:
            warnings.append("recent_degradation")
    if all_m["maxLoss"] >= 5:
        warnings.append("long_loss_streak")
    return {
        "minThirdWinRate": min_third,
        "stdThirdWinRate": std_third,
        "last24WinRate": last["winRate"],
        "last24Trades": last["trades"],
        "warnings": warnings,
        "usable": not warnings,
    }


def compact_trade(row: dict) -> dict:
    keys = (
        "strategy_id",
        "model_type",
        "time",
        "signal",
        "entry",
        "settle_time",
        "settle",
        "won",
        "z_score",
        "p_up",
        "vwap_z",
        "move_pct",
        "flow_ratio",
        "breakout",
        "poc",
        "zone_low",
        "zone_high",
    )
    out = {}
    for key in keys:
        if key not in row:
            continue
        value = row[key]
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        elif isinstance(value, float):
            value = round(value, 6)
        out[key] = value
    return out
