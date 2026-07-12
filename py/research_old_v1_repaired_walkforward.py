from __future__ import annotations

import json
import math
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_v1_orderbook_up_quality as quality  # noqa: E402
from research_normal_liquidity_orderbook import build_features, generate_signals, read_orderbook  # noqa: E402
from research_v1_orderbook_bidwall_trap import flip_to_down, v1_config  # noqa: E402
from second_backtest.data import load_second_bars  # noqa: E402


DATASETS = {
    "2026-07-05_2026-07-06": {
        "seconds": ROOT / "tmp" / "latest_pull_20260706_2130" / "data" / "btcusdt_1s_trades.csv",
        "orderbook": ROOT / "tmp" / "latest_pull_20260706_2130" / "data" / "btcusdt_orderbook_1s.csv",
    },
    "2026-07-07_2026-07-08": {
        "seconds": ROOT / "tmp" / "latest_pull_20260708_204204" / "data" / "btcusdt_1s_trades.csv",
        "orderbook": ROOT / "tmp" / "latest_live_pull_20260709_220453" / "data_clean" / "btcusdt_orderbook_1s.csv",
    },
    "2026-07-09_2026-07-10": {
        "seconds": ROOT / "tmp" / "latest_pull_20260710_203217" / "data" / "btcusdt_1s_trades.csv",
        "orderbook": ROOT / "tmp" / "latest_pull_20260710_203217" / "data" / "btcusdt_orderbook_1s.csv",
    },
}
TRAIN_DATASETS = {"2026-07-05_2026-07-06", "2026-07-07_2026-07-08"}
TEST_DATASETS = {"2026-07-09_2026-07-10"}
OUT_JSON = ROOT / "tmp" / "old_v1_repaired_walkforward.json"
OUT_TRADES = ROOT / "tmp" / "old_v1_repaired_walkforward_trades.csv"


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def metrics(rows: list[dict[str, Any]], hours: float) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: pd.Timestamp(row["time"]))
    wins = sum(bool(row["won"]) for row in ordered)
    pnl = [4 if row["won"] else -5 for row in ordered]
    equity = peak = drawdown = loss_streak = max_loss = 0
    for row_pnl in pnl:
        equity += row_pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        if row_pnl > 0:
            loss_streak = 0
        else:
            loss_streak += 1
            max_loss = max(max_loss, loss_streak)
    return {
        "trades": len(ordered),
        "wins": wins,
        "winRate": round(wins / len(ordered) * 100.0, 2) if ordered else 0.0,
        "pnl": sum(pnl),
        "maxDrawdownU": drawdown,
        "maxLoss": max_loss,
        "tradesPerDay": round(len(ordered) / hours * 24.0, 2) if hours > 0 else 0.0,
    }


def apply_gap(rows: list[dict[str, Any]], gap_sec: int = 600) -> list[dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    last_time: pd.Timestamp | None = None
    for row in sorted(rows, key=lambda item: pd.Timestamp(item["time"])):
        timestamp = pd.Timestamp(row["time"])
        if last_time is not None and (timestamp - last_time).total_seconds() < gap_sec:
            continue
        accepted.append(row)
        last_time = timestamp
    return accepted


def repaired_candidates(
    rows: list[dict[str, Any]],
    trap_ret300: float = -5.0,
    trap_bid_growth: float = 2.0,
    down_bid_fade: float = -0.7,
    up_flow_min: float = -0.063,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        if (
            row["signal"] == "UP"
            and float(row["ret_300s_bps"]) <= trap_ret300
            and float(row["bid20_60s_chg"]) > trap_bid_growth
        ):
            row = flip_to_down(row)
        if row["signal"] == "DOWN" and float(row["bid20_60s_chg"]) <= down_bid_fade:
            continue
        if row["signal"] == "UP" and float(row["flow_60s"]) <= up_flow_min:
            continue
        output.append(row)
    return output


def baseline_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def bidwall_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for source in rows:
        row = dict(source)
        if (
            row["signal"] == "UP"
            and float(row["ret_300s_bps"]) <= -5.0
            and float(row["bid20_60s_chg"]) > 2.0
        ):
            row = flip_to_down(row)
        output.append(row)
    return output


def load_raw_candidates(paths: dict[str, Path]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bars = load_second_bars(paths["seconds"], include_shards=False)
    orderbook = read_orderbook(paths["orderbook"], bars.index)
    data = bars.join(orderbook, how="left")
    data = data[data["ob_available"].fillna(False)].copy()
    data = data[~data.index.duplicated(keep="last")].sort_index()
    # Generate every eligible second. Scenario rules run before the 10-minute cooldown.
    cfg = replace(v1_config(), signal_gap_sec=1)
    features = build_features(data, cfg.normal_window_sec, cfg)
    rows = quality.add_quality_features(generate_signals(data, features, cfg), data, features)
    hours = (data.index.max() - data.index.min()).total_seconds() / 3600.0
    return rows, {
        "start": data.index.min(),
        "end": data.index.max(),
        "hours": hours,
        "rows": len(data),
        "observedPct": float(data["observed"].mean() * 100.0),
        "rawCandidates": len(rows),
    }


def subset_metrics(
    rows_by_dataset: dict[str, list[dict[str, Any]]],
    meta: dict[str, dict[str, Any]],
    selected: set[str],
) -> dict[str, Any]:
    rows = [row for name in selected for row in rows_by_dataset[name]]
    hours = sum(float(meta[name]["hours"]) for name in selected)
    return metrics(rows, hours)


def run() -> dict[str, Any]:
    raw: dict[str, list[dict[str, Any]]] = {}
    meta: dict[str, dict[str, Any]] = {}
    for name, paths in DATASETS.items():
        raw[name], meta[name] = load_raw_candidates(paths)

    scenario_builders = {
        "old_v1": baseline_candidates,
        "bidwall_flip": bidwall_candidates,
        "repaired_v2_correct_order": repaired_candidates,
    }
    scenarios: dict[str, Any] = {}
    trade_rows: list[dict[str, Any]] = []
    for scenario, builder in scenario_builders.items():
        accepted: dict[str, list[dict[str, Any]]] = {}
        for name, candidates in raw.items():
            accepted[name] = apply_gap(builder(candidates))
            for source in accepted[name]:
                row = dict(source)
                row["dataset"] = name
                row["scenario"] = scenario
                trade_rows.append(row)
        scenarios[scenario] = {
            "train": subset_metrics(accepted, meta, TRAIN_DATASETS),
            "test": subset_metrics(accepted, meta, TEST_DATASETS),
            "all": subset_metrics(accepted, meta, set(DATASETS)),
            "byDataset": {
                name: metrics(rows, float(meta[name]["hours"])) for name, rows in accepted.items()
            },
        }

    sensitivity = []
    for trap_ret300 in (-3.0, -5.0, -8.0):
        for trap_bid_growth in (1.5, 2.0, 2.5):
            for down_bid_fade in (-0.6, -0.7, -0.8):
                for up_flow_min in (-0.04, -0.063, -0.08):
                    accepted = {
                        name: apply_gap(repaired_candidates(
                            candidates,
                            trap_ret300=trap_ret300,
                            trap_bid_growth=trap_bid_growth,
                            down_bid_fade=down_bid_fade,
                            up_flow_min=up_flow_min,
                        ))
                        for name, candidates in raw.items()
                    }
                    sensitivity.append({
                        "params": {
                            "trapRet300": trap_ret300,
                            "trapBidGrowth": trap_bid_growth,
                            "downBidFade": down_bid_fade,
                            "upFlowMin": up_flow_min,
                        },
                        "train": subset_metrics(accepted, meta, TRAIN_DATASETS),
                        "test": subset_metrics(accepted, meta, TEST_DATASETS),
                        "all": subset_metrics(accepted, meta, set(DATASETS)),
                    })

    default_neighbors = [
        row for row in sensitivity
        if row["params"]["trapRet300"] == -5.0
        and row["params"]["trapBidGrowth"] == 2.0
    ]
    stable_neighbors = [
        row for row in default_neighbors
        if row["test"]["trades"] >= 3 and row["test"]["winRate"] >= 55.0
    ]
    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "method": "Rules are applied to per-second candidates before the 600-second cooldown.",
        "payout": "win +4U, loss -5U, amount 5U",
        "datasets": meta,
        "scenarios": scenarios,
        "sensitivity": {
            "combinations": len(sensitivity),
            "defaultTrapNeighborhoods": len(default_neighbors),
            "defaultTrapStableOnTest": len(stable_neighbors),
            "defaultTrapStableRatePct": round(len(stable_neighbors) / len(default_neighbors) * 100.0, 2)
            if default_neighbors else 0.0,
            "rows": sensitivity,
        },
        "tradesCsv": str(OUT_TRADES),
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(trade_rows).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    result = run()
    print(json.dumps(clean({
        "datasets": result["datasets"],
        "scenarios": result["scenarios"],
        "sensitivity": {
            key: value for key, value in result["sensitivity"].items() if key != "rows"
        },
    }), ensure_ascii=False, indent=2))
