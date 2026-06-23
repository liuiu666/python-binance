from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from second_backtest.data import audit_second_csv, load_second_bars
from second_backtest.execution import execute_signals
from second_backtest.metrics import compact_metrics, payout_for_horizon, split_metrics
from second_backtest.strategies import (
    SecondNormalMultiframeConfig,
    generate_normal_multiframe_signals,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / "tmp" / "latest_server_pull_20260622" / "btcusdt_1s_trades_latest_tail120k.csv"
DEFAULT_OUT = ROOT / "tmp" / "latest_server_pull_20260622" / "normal_multiframe_research.json"


def _group_metrics(trades: list[dict], key: str, start: pd.Timestamp, end: pd.Timestamp, amount: float) -> dict:
    out = {}
    for value in sorted({str(row.get(key)) for row in trades}):
        rows = [row for row in trades if str(row.get(key)) == value]
        out[value] = compact_metrics(split_metrics(rows, start, end, amount=amount, payout_rate=payout_for_horizon(600)))["all"]
    return out


def _score(metrics: dict) -> float:
    all_m = metrics["all"]
    before = metrics["beforeLast24h"]
    last = metrics["last24h"]
    if all_m["trades"] < 8 or all_m["winRate"] is None:
        return -999999.0
    daily = metrics.get("byUtcDay") or []
    bad_days = sum(1 for day in daily if day["trades"] >= 3 and (day["winRate"] or 0) < 55)
    last_penalty = 60.0 if last["trades"] >= 3 and (last["winRate"] or 0) < 55 else 0.0
    before_penalty = 40.0 if before["trades"] >= 5 and (before["winRate"] or 0) < 55 else 0.0
    return round(
        all_m["pnl"]
        + all_m["winRate"] * 2.4
        + min(all_m["tradesPerDay"], 12) * 2.5
        - all_m["maxLoss"] * 22.0
        - bad_days * 25.0
        - last_penalty
        - before_penalty,
        4,
    )


def _run_case(bars: pd.DataFrame, cfg: SecondNormalMultiframeConfig) -> dict:
    raw = generate_normal_multiframe_signals(bars, cfg, apply_config_gap=True)
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
            "lowerZ": cfg.lower_z,
            "upperZ": cfg.upper_z,
            "lowerBigMode": cfg.lower_big_mode,
            "upperBigMode": cfg.upper_big_mode,
            "coreEnabled": cfg.core_enabled,
            "coreZAbsMax": cfg.core_z_abs_max,
            "lowerMinSlope1Bps": cfg.lower_min_slope1_bps,
            "lowerMinFlow1": cfg.lower_min_flow1,
            "upperMinSlope1Bps": cfg.upper_min_slope1_bps,
            "upperMinFlow1": cfg.upper_min_flow1,
            "coreMinSlope1Bps": cfg.core_min_slope1_bps,
            "coreMinFlow1": cfg.core_min_flow1,
            "signalGapSec": cfg.signal_gap_sec,
        },
        "score": _score(compact),
        "rawSignals": len(raw),
        "executed": len(executed),
        "rejected": len(rejected),
        "metrics": compact,
        "bySide": _group_metrics(executed, "signal", bars.index.min(), bars.index.max(), cfg.amount),
        "byReason": _group_metrics(executed, "reason", bars.index.min(), bars.index.max(), cfg.amount),
        "sampleTrades": [
            {
                "time": row["time"].isoformat(),
                "signal": row["signal"],
                "reason": row.get("reason"),
                "won": bool(row["won"]),
                "entry": round(float(row["entry"]), 2),
                "settle": round(float(row["settle"]), 2),
                "z10": row.get("z10"),
                "midSlope": row.get("mid_slope_bps"),
                "longSlope": row.get("long_slope_bps"),
                "slope1": row.get("slope1_bps"),
                "flow1": row.get("flow1"),
            }
            for row in executed[-12:]
        ],
    }


def _grid() -> list[SecondNormalMultiframeConfig]:
    out = []
    for lower_z in (-0.5, -1.0):
        for upper_z in (1.0, 1.5):
            for lower_big in ("align_up", "signal_counter", "any"):
                for upper_big in ("any", "signal_align", "signal_counter"):
                    for upper_flow in (0.0, 0.05, 0.08):
                        for core_enabled in (False, True):
                            out.append(
                                SecondNormalMultiframeConfig(
                                    strategy_id=(
                                        "NORMAL_MULTIFRAME_"
                                        f"{lower_z}_{upper_z}_{lower_big}_{upper_big}_{upper_flow}_{core_enabled}"
                                    ),
                                    short_window_sec=600,
                                    mid_window_sec=1800,
                                    long_window_sec=3600,
                                    horizon_sec=600,
                                    signal_gap_sec=600,
                                    lower_z=lower_z,
                                    upper_z=upper_z,
                                    lower_big_mode=lower_big,
                                    upper_big_mode=upper_big,
                                    core_enabled=core_enabled,
                                    core_z_abs_max=1.0,
                                    lower_min_slope1_bps=0.0,
                                    lower_min_flow1=0.0,
                                    upper_min_slope1_bps=0.0,
                                    upper_min_flow1=upper_flow,
                                    core_min_slope1_bps=0.0,
                                    core_min_flow1=0.05,
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
            "Multiframe normal range strategy: rolling 10m z-score defines lower/upper/core position, "
            "30m/60m rolling mean slopes define larger context, 1m slope and taker-flow confirm entries. "
            "Signals settle 10 minutes after entry."
        ),
        "rankedTop": ranked[:40],
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
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report["sample"], ensure_ascii=False))
    for item in report["rankedTop"][:15]:
        metrics = item["metrics"]["all"]
        print(json.dumps({"params": item["params"], "score": item["score"], "all": metrics, "bySide": item["bySide"], "byReason": item["byReason"]}, ensure_ascii=False))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
