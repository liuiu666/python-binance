from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from second_backtest.data import audit_second_csv, load_second_bars
from second_backtest.execution import execute_signals
from second_backtest.metrics import compact_metrics, payout_for_horizon, split_metrics
from second_backtest.strategies import SecondRangeBreakoutConfig, generate_range_breakout_signals


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / "tmp" / "latest_server_pull_20260622" / "btcusdt_1s_trades_latest_tail120k.csv"
DEFAULT_OUT = ROOT / "tmp" / "latest_server_pull_20260622" / "range_breakout_direction_research.json"


def _continuous_segments(csv: Path, *, min_hours: float = 1.0) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    df = pd.read_csv(csv, usecols=["timestamp"])
    ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce").dropna().dt.floor("s")
    ts = ts.drop_duplicates().sort_values().reset_index(drop=True)
    if ts.empty:
        return []
    segments = []
    start = ts.iloc[0]
    prev = ts.iloc[0]
    for t, gap in zip(ts.iloc[1:], ts.diff().dt.total_seconds().fillna(1).iloc[1:]):
        if gap > 30:
            if (prev - start).total_seconds() / 3600.0 >= min_hours:
                segments.append((start, prev))
            start = t
        prev = t
    if (prev - start).total_seconds() / 3600.0 >= min_hours:
        segments.append((start, prev))
    return segments


def _side_metrics(trades: list[dict], start: pd.Timestamp, end: pd.Timestamp, amount: float) -> dict:
    out = {}
    for side in ("UP", "DOWN"):
        rows = [row for row in trades if row.get("signal") == side]
        out[side] = compact_metrics(split_metrics(rows, start, end, amount=amount, payout_rate=payout_for_horizon(600)))["all"]
    return out


def _score(metrics: dict) -> float:
    all_m = metrics["all"]
    if all_m["trades"] < 10 or all_m["winRate"] is None:
        return -999999.0
    daily = metrics.get("byUtcDay") or []
    bad_days = sum(1 for day in daily if day["trades"] >= 3 and (day["winRate"] or 0) < 55)
    return round(
        all_m["pnl"]
        + all_m["winRate"] * 2.0
        - all_m["maxLoss"] * 20.0
        - bad_days * 25.0
        + min(all_m["tradesPerDay"], 18) * 2.0,
        4,
    )


def _run_case(bars: pd.DataFrame, cfg: SecondRangeBreakoutConfig) -> dict:
    raw = generate_range_breakout_signals(bars, cfg, apply_config_gap=True)
    executed, rejected = execute_signals(
        raw,
        per_strategy_lock=True,
        global_lock_sec=0,
        cooldown_sec=cfg.horizon_sec,
        use_horizon_as_lock=True,
    )
    metrics = split_metrics(
        executed,
        bars.index.min(),
        bars.index.max(),
        amount=cfg.amount,
        payout_rate=payout_for_horizon(cfg.horizon_sec),
    )
    compact = compact_metrics(metrics)
    return {
        "params": {
            "lookbackSec": cfg.lookback_sec,
            "zEntry": cfg.z_entry,
            "preSlopeSec": cfg.pre_slope_sec,
            "minSlopeBps": cfg.min_slope_bps,
            "minFlowImbalance": cfg.min_flow_imbalance,
            "minVolumeRatio": cfg.min_volume_ratio,
            "minVolatilityRatio": cfg.min_volatility_ratio,
            "maxAgeBeyondSec": cfg.max_age_beyond_sec,
            "signalGapSec": cfg.signal_gap_sec,
        },
        "score": _score(compact),
        "rawSignals": len(raw),
        "executed": len(executed),
        "metrics": compact,
        "bySide": _side_metrics(executed, bars.index.min(), bars.index.max(), cfg.amount),
        "sampleTrades": [
            {
                "time": row["time"].isoformat(),
                "signal": row["signal"],
                "won": bool(row["won"]),
                "entry": round(float(row["entry"]), 2),
                "settle": round(float(row["settle"]), 2),
                "rangeZ": row.get("range_z"),
                "slopeBps": row.get("pre_slope_bps"),
                "flow": row.get("pre_flow_imbalance"),
                "volumeRatio": row.get("pre_volume_ratio"),
            }
            for row in executed[-12:]
        ],
    }


def _grid() -> list[SecondRangeBreakoutConfig]:
    out = []
    for lookback in (1200, 1800, 3600):
        for z_entry in (1.8, 2.0, 2.2):
            for min_slope in (6.0, 8.0, 10.0, 12.0):
                for min_flow in (0.08, 0.12, 0.18):
                    out.append(
                        SecondRangeBreakoutConfig(
                            strategy_id=f"RANGE_BREAKOUT_{lookback}_{z_entry}_{min_slope}_{min_flow}",
                            lookback_sec=lookback,
                            horizon_sec=600,
                            signal_gap_sec=600,
                            z_entry=z_entry,
                            pre_slope_sec=300,
                            min_slope_bps=min_slope,
                            min_flow_imbalance=min_flow,
                            min_volume_ratio=0.45,
                            min_volatility_ratio=0.55,
                            max_age_beyond_sec=180,
                            amount=15,
                        )
                    )
    return out


def build_report(args: argparse.Namespace) -> dict:
    csv = Path(args.csv)
    bars = load_second_bars(csv, include_shards=False)
    cases = [_run_case(bars, cfg) for cfg in _grid()]
    ranked = sorted(
        cases,
        key=lambda item: (
            item["score"],
            item["metrics"]["all"]["pnl"],
            item["metrics"]["all"]["winRate"] or 0,
            -item["metrics"]["all"]["maxLoss"],
        ),
        reverse=True,
    )
    segments = []
    for start, end in _continuous_segments(csv):
        part = bars.loc[start:end].copy()
        if len(part) < 7200:
            continue
        best_cfg = SecondRangeBreakoutConfig(**{
            "strategy_id": "segment_best",
            "lookback_sec": int(ranked[0]["params"]["lookbackSec"]),
            "horizon_sec": 600,
            "signal_gap_sec": int(ranked[0]["params"]["signalGapSec"]),
            "z_entry": float(ranked[0]["params"]["zEntry"]),
            "pre_slope_sec": int(ranked[0]["params"]["preSlopeSec"]),
            "min_slope_bps": float(ranked[0]["params"]["minSlopeBps"]),
            "min_flow_imbalance": float(ranked[0]["params"]["minFlowImbalance"]),
            "min_volume_ratio": float(ranked[0]["params"]["minVolumeRatio"]),
            "min_volatility_ratio": float(ranked[0]["params"]["minVolatilityRatio"]),
            "max_age_beyond_sec": int(ranked[0]["params"]["maxAgeBeyondSec"]),
            "amount": 15,
        })
        result = _run_case(part, best_cfg)
        segments.append({
            "start": start.isoformat(),
            "end": end.isoformat(),
            "hours": round((end - start).total_seconds() / 3600.0, 2),
            "observedPct": round(float(part["observed"].mean() * 100), 2),
            "bestParamsResult": result,
        })
    return {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "source": str(csv),
        "sample": {
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "hours": round((bars.index.max() - bars.index.min()).total_seconds() / 3600.0, 2),
            "rows": int(len(bars)),
            "observedPct": round(float(bars["observed"].mean() * 100), 2),
            "audit": audit_second_csv(csv),
        },
        "method": (
            "Direction strategy: UP when price breaks above a rolling normal range, "
            "DOWN when it breaks below. Filters require aligned 5m slope, taker-flow imbalance, "
            "minimum volume, and minimum volatility before the signal. Settlement is 10 minutes later."
        ),
        "rankedTop": ranked[:30],
        "segments": segments,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = build_report(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["sample"], ensure_ascii=False))
    for item in report["rankedTop"][:12]:
        metrics = item["metrics"]["all"]
        print(json.dumps({"params": item["params"], "score": item["score"], "all": metrics, "bySide": item["bySide"]}, ensure_ascii=False))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
