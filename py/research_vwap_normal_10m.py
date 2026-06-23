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
DEFAULT_OUT = ROOT / "tmp" / "vwap_normal_10m_research_latest.json"


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(frozen=True)
class VwapNormalConfig:
    lookback_sec: int
    tail_pct: float
    min_vwap_dev_bps: float
    max_vwap_slope_bps: float | None = None
    flow_mode: str = "none"
    min_abs_flow: float = 0.0


def _observed_pct(prefix: np.ndarray, start: int, end_exclusive: int) -> float:
    if end_exclusive <= start:
        return 0.0
    return 100.0 * float(prefix[end_exclusive] - prefix[start]) / float(end_exclusive - start)


def _settle(
    bars: pd.DataFrame,
    idx: int,
    *,
    signal: str,
    strategy_id: str,
    model_type: str,
    horizon_sec: int,
    extra: dict[str, Any],
) -> dict[str, Any]:
    close = bars["close"].to_numpy(float)
    entry = float(close[idx])
    settle = float(close[idx + horizon_sec])
    return {
        "strategy_id": strategy_id,
        "model_type": model_type,
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


def _make_configs() -> list[VwapNormalConfig]:
    configs: list[VwapNormalConfig] = []
    for lookback_sec in (1800, 2700, 3600, 7200):
        for tail_pct in (0.20, 0.25, 0.27, 0.30):
            for dev in (0.0, 5.0, 10.0, 15.0, 20.0, 30.0):
                configs.append(VwapNormalConfig(lookback_sec, tail_pct, dev))
                configs.append(VwapNormalConfig(lookback_sec, tail_pct, dev, max_vwap_slope_bps=8.0))
                configs.append(
                    VwapNormalConfig(
                        lookback_sec,
                        tail_pct,
                        dev,
                        max_vwap_slope_bps=8.0,
                        flow_mode="not_against",
                        min_abs_flow=0.12,
                    )
                )
    return configs


def generate_signals(
    bars: pd.DataFrame,
    cfg: VwapNormalConfig,
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
    close_s = pd.Series(close, index=index)
    volume_s = pd.Series(volume, index=index)
    log_ret = np.diff(np.log(close), prepend=np.nan)
    ret_s = pd.Series(log_ret, index=index)

    min_periods = max(300, cfg.lookback_sec // 3)
    mu = ret_s.rolling(cfg.lookback_sec, min_periods=min_periods).mean().to_numpy(float)
    sigma = ret_s.rolling(cfg.lookback_sec, min_periods=min_periods).std(ddof=1).to_numpy(float)

    vol_sum = volume_s.rolling(cfg.lookback_sec, min_periods=min_periods).sum().to_numpy(float)
    pv_sum = (close_s * volume_s).rolling(cfg.lookback_sec, min_periods=min_periods).sum().to_numpy(float)
    vwap = pv_sum / np.maximum(vol_sum, 1e-12)
    vwap_dev_bps = (close / np.maximum(vwap, 1e-12) - 1.0) * 10000.0
    slope_window = min(300, max(60, cfg.lookback_sec // 6))
    prior_vwap = np.roll(vwap, slope_window)
    vwap_slope_bps = np.full(len(vwap), np.nan, dtype=float)
    np.divide(vwap, prior_vwap, out=vwap_slope_bps, where=prior_vwap > 1e-12)
    vwap_slope_bps = (vwap_slope_bps - 1.0) * 10000.0
    vwap_slope_bps[:slope_window] = np.nan

    flow_window = 180
    buy_recent = pd.Series(buy, index=index).rolling(flow_window, min_periods=1).sum().to_numpy(float)
    sell_recent = pd.Series(sell, index=index).rolling(flow_window, min_periods=1).sum().to_numpy(float)
    flow = (buy_recent - sell_recent) / np.maximum(buy_recent + sell_recent, 1e-12)

    rows: list[dict[str, Any]] = []
    first = max(cfg.lookback_sec, slope_window, flow_window)
    last = len(close) - horizon_sec - 1
    hi = 1.0 - cfg.tail_pct
    for i in range(first, last, max(1, int(entry_step_sec))):
        if _observed_pct(observed_prefix, i - cfg.lookback_sec + 1, i + 1) < min_observed_pct:
            continue
        if _observed_pct(observed_prefix, i + 1, i + horizon_sec + 1) < min_observed_pct:
            continue
        if not np.isfinite(mu[i]) or not np.isfinite(sigma[i]) or sigma[i] <= 1e-12:
            continue
        z = float(horizon_sec * mu[i] / (math.sqrt(horizon_sec) * sigma[i]))
        p_up = normal_cdf(z)
        signal = "DOWN" if p_up >= hi else "UP" if p_up <= cfg.tail_pct else None
        if not signal:
            continue
        dev = float(vwap_dev_bps[i])
        if signal == "UP" and dev > -float(cfg.min_vwap_dev_bps):
            continue
        if signal == "DOWN" and dev < float(cfg.min_vwap_dev_bps):
            continue
        slope = float(vwap_slope_bps[i])
        if cfg.max_vwap_slope_bps is not None:
            if not np.isfinite(slope) or abs(slope) > float(cfg.max_vwap_slope_bps):
                continue
        flow_value = float(flow[i])
        if cfg.flow_mode == "not_against":
            # Reversion should not be fighting a still-dominant taker flow.
            if signal == "UP" and flow_value <= -float(cfg.min_abs_flow):
                continue
            if signal == "DOWN" and flow_value >= float(cfg.min_abs_flow):
                continue

        suffix = f"L{cfg.lookback_sec}_T{int(cfg.tail_pct * 100)}_D{int(cfg.min_vwap_dev_bps)}"
        if cfg.max_vwap_slope_bps is not None:
            suffix += f"_S{int(cfg.max_vwap_slope_bps)}"
        if cfg.flow_mode != "none":
            suffix += "_FLOW"
        rows.append(
            _settle(
                bars,
                i,
                signal=signal,
                strategy_id=f"VWAP_NORMAL_REVERT_{suffix}",
                model_type="vwap_normal_revert",
                horizon_sec=horizon_sec,
                extra={
                    "p_up": float(p_up),
                    "z_score": float(z),
                    "lookback_sec": int(cfg.lookback_sec),
                    "tail_pct": float(cfg.tail_pct),
                    "vwap": float(vwap[i]),
                    "vwap_dev_bps": dev,
                    "vwap_slope_bps": slope,
                    "flow180": flow_value,
                    "min_vwap_dev_bps": float(cfg.min_vwap_dev_bps),
                    "max_vwap_slope_bps": cfg.max_vwap_slope_bps,
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
        "p_up",
        "vwap",
        "vwap_dev_bps",
        "vwap_slope_bps",
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
        rows = generate_signals(
            bars,
            cfg,
            horizon_sec=args.horizon_sec,
            entry_step_sec=args.entry_step_sec,
            min_observed_pct=args.min_observed_pct,
        )
        for row in rows:
            by_strategy.setdefault(row["strategy_id"], []).append(row)

    reports = [
        _variant_report(bars, strategy_id, rows, args.horizon_sec)
        for strategy_id, rows in by_strategy.items()
    ]
    reports.sort(
        key=lambda row: (
            row["score"],
            row["metrics"]["all"]["pnl"],
            row["metrics"]["all"]["trades"],
        ),
        reverse=True,
    )

    positive = [
        item
        for item in sorted(
            reports,
            key=lambda row: (
                row["metrics"]["all"]["pnl"],
                row["metrics"]["all"]["winRate"] or 0.0,
                row["metrics"]["all"]["trades"],
            ),
            reverse=True,
        )
        if item["metrics"]["all"]["trades"] >= args.min_report_trades
    ]

    return {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "source": str(Path(args.csv).resolve()),
        "method": {
            "goal": "10m binary-option research combining normal distribution tail probability and rolling price VWAP deviation.",
            "causal": "Uses only data at or before signal time; settles at signal + horizon_sec.",
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
        "topByScore": reports[:30],
        "topByPnl": positive[:30],
        "allVariants": reports,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Research normal probability + rolling VWAP filters.")
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
