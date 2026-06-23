from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from second_backtest.data import audit_second_sources, load_second_bars
from second_backtest.execution import execute_signals
from second_backtest.metrics import compact_metrics, payout_for_horizon, robust_score, split_metrics


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / "tmp" / "latest_second_pull_20260622_upfix" / "btcusdt_1s_trades.csv"
DEFAULT_OUT = ROOT / "tmp" / "poc_normal_10m_research_latest.json"


@dataclass(frozen=True)
class PocNormalConfig:
    lookback_sec: int
    bin_size: float
    z_entry: float
    min_sigma_bps: float = 4.0
    max_sigma_bps: float = 80.0
    flow_mode: str = "none"
    max_against_flow: float = 0.25


def _observed_pct(prefix: np.ndarray, start: int, end_exclusive: int) -> float:
    if end_exclusive <= start:
        return 0.0
    return 100.0 * float(prefix[end_exclusive] - prefix[start]) / float(end_exclusive - start)


def _make_configs() -> list[PocNormalConfig]:
    configs: list[PocNormalConfig] = []
    for lookback_sec in (1800, 3600, 7200):
        for bin_size in (20.0, 50.0):
            for z_entry in (1.0, 1.25, 1.5, 2.0):
                configs.append(PocNormalConfig(lookback_sec, bin_size, z_entry))
                configs.append(
                    PocNormalConfig(
                        lookback_sec,
                        bin_size,
                        z_entry,
                        flow_mode="not_strong_against",
                        max_against_flow=0.25,
                    )
                )
    return configs


def _poc_normal_features(
    close: np.ndarray,
    volume: np.ndarray,
    i: int,
    cfg: PocNormalConfig,
) -> dict[str, float] | None:
    start = i - cfg.lookback_sec + 1
    if start < 0:
        return None
    window_close = close[start : i + 1]
    window_volume = volume[start : i + 1]
    total_volume = float(window_volume.sum())
    if total_volume <= 1e-12:
        return None

    bin_ids = np.floor(window_close / cfg.bin_size).astype(np.int64)
    uniq, inv = np.unique(bin_ids, return_inverse=True)
    vol_by_bin = np.bincount(inv, weights=window_volume)
    if len(uniq) == 0:
        return None
    poc_pos = int(np.argmax(vol_by_bin))
    poc_bin = int(uniq[poc_pos])
    poc = (poc_bin + 0.5) * cfg.bin_size
    centers = (uniq.astype(float) + 0.5) * cfg.bin_size
    sigma = math.sqrt(float(np.sum(vol_by_bin * (centers - poc) ** 2) / total_volume))
    if not np.isfinite(sigma) or sigma <= 1e-12:
        return None
    price = float(close[i])
    sigma_bps = sigma / price * 10000.0
    if sigma_bps < cfg.min_sigma_bps or sigma_bps > cfg.max_sigma_bps:
        return None
    z = (price - poc) / sigma
    return {
        "poc": float(poc),
        "poc_sigma": float(sigma),
        "poc_sigma_bps": float(sigma_bps),
        "poc_z": float(z),
        "poc_bin_share": float(vol_by_bin[poc_pos] / total_volume),
    }


def _settle(
    bars: pd.DataFrame,
    idx: int,
    *,
    signal: str,
    strategy_id: str,
    horizon_sec: int,
    extra: dict[str, Any],
) -> dict[str, Any]:
    close = bars["close"].to_numpy(float)
    entry = float(close[idx])
    settle = float(close[idx + horizon_sec])
    return {
        "strategy_id": strategy_id,
        "model_type": "poc_centered_normal_revert",
        "idx": int(idx),
        "time": bars.index[idx],
        "signal": signal,
        "entry": entry,
        "settle_time": bars.index[idx + horizon_sec],
        "settle": settle,
        "won": bool(settle > entry if signal == "UP" else settle < entry),
        "horizon_sec": int(horizon_sec),
        "amount": 5.0,
        **extra,
    }


def generate_signals(
    bars: pd.DataFrame,
    cfg: PocNormalConfig,
    *,
    horizon_sec: int,
    entry_step_sec: int,
    min_observed_pct: float,
) -> list[dict[str, Any]]:
    close = bars["close"].to_numpy(float)
    volume = bars["volume"].to_numpy(float)
    buy = bars["buy_qty"].to_numpy(float)
    sell = bars["sell_qty"].to_numpy(float)
    observed = bars["observed"].astype(bool).to_numpy()
    observed_prefix = np.concatenate([[0], np.cumsum(observed.astype(int))])

    index = bars.index
    flow_window = 180
    buy_recent = pd.Series(buy, index=index).rolling(flow_window, min_periods=1).sum().to_numpy(float)
    sell_recent = pd.Series(sell, index=index).rolling(flow_window, min_periods=1).sum().to_numpy(float)
    flow = (buy_recent - sell_recent) / np.maximum(buy_recent + sell_recent, 1e-12)

    rows: list[dict[str, Any]] = []
    first = max(cfg.lookback_sec, flow_window)
    last = len(close) - horizon_sec - 1
    for i in range(first, last, max(1, int(entry_step_sec))):
        if _observed_pct(observed_prefix, i - cfg.lookback_sec + 1, i + 1) < min_observed_pct:
            continue
        if _observed_pct(observed_prefix, i + 1, i + horizon_sec + 1) < min_observed_pct:
            continue
        features = _poc_normal_features(close, volume, i, cfg)
        if not features:
            continue
        z = float(features["poc_z"])
        signal = "DOWN" if z >= cfg.z_entry else "UP" if z <= -cfg.z_entry else None
        if not signal:
            continue
        flow_value = float(flow[i])
        if cfg.flow_mode == "not_strong_against":
            if signal == "UP" and flow_value <= -cfg.max_against_flow:
                continue
            if signal == "DOWN" and flow_value >= cfg.max_against_flow:
                continue
        suffix = f"L{cfg.lookback_sec}_B{int(cfg.bin_size)}_Z{str(cfg.z_entry).replace('.', 'p')}"
        if cfg.flow_mode != "none":
            suffix += "_FLOW"
        rows.append(
            _settle(
                bars,
                i,
                signal=signal,
                strategy_id=f"POC_NORMAL_REVERT_{suffix}",
                horizon_sec=horizon_sec,
                extra={
                    **features,
                    "lookback_sec": int(cfg.lookback_sec),
                    "bin_size": float(cfg.bin_size),
                    "z_entry": float(cfg.z_entry),
                    "flow180": flow_value,
                    "flow_mode": cfg.flow_mode,
                },
            )
        )
    return rows


def _compact_trade(row: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "strategy_id",
        "time",
        "signal",
        "entry",
        "settle_time",
        "settle",
        "won",
        "poc",
        "poc_z",
        "poc_sigma_bps",
        "poc_bin_share",
        "flow180",
    ]
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


def _variant_report(bars: pd.DataFrame, strategy_id: str, raw: list[dict[str, Any]], horizon_sec: int) -> dict:
    executed, rejected = execute_signals(
        raw,
        per_strategy_lock=True,
        global_lock_sec=0,
        cooldown_sec=horizon_sec,
        use_horizon_as_lock=True,
    )
    metrics = split_metrics(
        executed,
        bars.index.min(),
        bars.index.max(),
        amount=5.0,
        payout_rate=payout_for_horizon(horizon_sec),
    )
    return {
        "strategyId": strategy_id,
        "rawSignals": len(raw),
        "rejectedByLock": len(rejected),
        "score": robust_score(metrics),
        "metrics": compact_metrics(metrics),
        "sampleTrades": [_compact_trade(row) for row in executed[-8:]],
    }


def build_report(args: argparse.Namespace) -> dict:
    bars = load_second_bars(args.csv, include_shards=not args.no_shards)
    by_strategy: dict[str, list[dict[str, Any]]] = {}
    for cfg in _make_configs():
        for row in generate_signals(
            bars,
            cfg,
            horizon_sec=args.horizon_sec,
            entry_step_sec=args.entry_step_sec,
            min_observed_pct=args.min_observed_pct,
        ):
            by_strategy.setdefault(row["strategy_id"], []).append(row)

    reports = [
        _variant_report(bars, strategy_id, rows, args.horizon_sec)
        for strategy_id, rows in by_strategy.items()
    ]
    reports.sort(
        key=lambda row: (
            row["metrics"]["all"]["pnl"],
            row["metrics"]["all"]["winRate"] or 0.0,
            row["metrics"]["all"]["trades"],
        ),
        reverse=True,
    )

    return {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "source": str(Path(args.csv).resolve()),
        "method": {
            "goal": "10m binary-option test using the highest-volume price bin (POC) as the normal-distribution center.",
            "causal": "POC and sigma use only the rolling window before entry; settlement is entry + horizon_sec.",
            "execution": "Per-strategy 10m lock; no global dedupe.",
            "breakevenWinRateAt80pct": round(100.0 / 1.8, 4),
            "entryStepSec": int(args.entry_step_sec),
            "minObservedPct": float(args.min_observed_pct),
            "configs": [asdict(cfg) for cfg in _make_configs()],
        },
        "dataQuality": audit_second_sources(args.csv, include_shards=not args.no_shards),
        "sample": {
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "hours": round((bars.index.max() - bars.index.min()).total_seconds() / 3600.0, 2),
            "rows": int(len(bars)),
            "observedRows": int(bars["observed"].sum()),
            "filledRows": int((~bars["observed"]).sum()),
        },
        "variantCount": len(reports),
        "topByPnl": [item for item in reports if item["metrics"]["all"]["trades"] >= args.min_report_trades][:30],
        "allVariants": reports,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Research POC-centered normal reversion signals.")
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--horizon-sec", type=int, default=600)
    parser.add_argument("--entry-step-sec", type=int, default=10)
    parser.add_argument("--min-observed-pct", type=float, default=95.0)
    parser.add_argument("--min-report-trades", type=int, default=8)
    parser.add_argument("--no-shards", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = build_report(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report["topByPnl"][:12], ensure_ascii=False, indent=2, default=str))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
