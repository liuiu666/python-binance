from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from liquidity_v2_core import (  # noqa: E402
    LiquidityV2Rules,
    build_features as core_build_features,
    is_bidwall_trap as core_is_bidwall_trap,
    normal_ready as core_normal_ready,
    quality_v2_veto_code,
    signal_from_row as core_signal_from_row,
)
from research_normal_liquidity_orderbook import read_orderbook  # noqa: E402
from second_backtest.data import load_second_bars  # noqa: E402


DATASETS = {
    "2026-07-05_2026-07-06": {
        "dir": ROOT / "tmp" / "latest_pull_20260706_2130" / "data",
        "start": "2026-07-05T00:00:00Z",
        "end": "2026-07-07T00:00:00Z",
    },
    "2026-07-07_2026-07-08": {
        "dir": ROOT / "tmp" / "latest_pull_20260708_204204" / "data",
        "start": "2026-07-07T00:00:00Z",
        "end": "2026-07-09T00:00:00Z",
    },
}
OUT_JSON = ROOT / "tmp" / "v2_live_backtest_parity.json"
OUT_TRADES = ROOT / "tmp" / "v2_live_backtest_parity_trades.csv"


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        value = float(value)
        return value if math.isfinite(value) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def cfg() -> LiquidityV2Rules:
    return LiquidityV2Rules(
        normal_window_sec=600,
        z_entry=1.2,
        z_reclaim=0.85,
        mode="reclaim",
        retest_sec=120,
        inside_min=0.55,
        observed_min_pct=88.0,
        center_slope_sec=300,
        center_slope_max_bps=8.0,
        sigma_min_bps=5.8,
        sigma_max_bps=55.0,
        sigma_expand_max=1.9,
        ob_imbalance_min=0.08,
        micro_min_bps=0.001,
        wall_ratio_min=1.0,
        flow_guard=0.12,
        true_break_flow=0.28,
        true_break_imbalance=0.28,
        min_gap_sec=600,
        horizon_sec=600,
    )


def build_features(data: pd.DataFrame, window: int, c: LiquidityV2Rules) -> pd.DataFrame:
    del window
    return core_build_features(data, c)


def safe_float(row: pd.Series, key: str, default: float = float("nan")) -> float:
    try:
        return float(row.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def load_data(data_dir: Path) -> pd.DataFrame:
    bars = load_second_bars(data_dir / "btcusdt_1s_trades.csv", include_shards=True)
    ob = read_orderbook(data_dir / "btcusdt_orderbook_1s.csv", bars.index)
    data = bars.join(ob, how="left")
    return data[~data.index.duplicated(keep="last")].sort_index()


def normal_ready(row: pd.Series, c: LiquidityV2Rules) -> bool:
    return core_normal_ready(row, c)


def signal_from_row(row: pd.Series, c: LiquidityV2Rules) -> tuple[str | None, str | None]:
    return core_signal_from_row(row, c)


def bidwall_trap(signal: str | None, reason: str | None, row: pd.Series) -> bool:
    return core_is_bidwall_trap(signal, reason, row, cfg())


def quality_v2_veto(signal: str | None, row: pd.Series) -> str | None:
    return quality_v2_veto_code(signal, row, cfg())


def payout(won: bool) -> int:
    return 4 if bool(won) else -5


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(rows, key=lambda row: (str(row["dataset"]), int(row["idx"])))
    n = len(rows)
    wins = sum(1 for row in rows if row["won"])
    equity = peak = max_dd = loss_streak = max_loss = 0
    for row in rows:
        pnl = payout(row["won"])
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if row["won"]:
            loss_streak = 0
        else:
            loss_streak += 1
            max_loss = max(max_loss, loss_streak)
    return {
        "trades": n,
        "wins": int(wins),
        "winRate": round(wins / n * 100.0, 2) if n else 0.0,
        "pnl": int(sum(payout(row["won"]) for row in rows)),
        "maxDrawdownU": int(max_dd),
        "maxLoss": int(max_loss),
    }


def split_metrics(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    if not rows:
        return {}
    frame = pd.DataFrame(rows)
    return {str(name): metrics(group.to_dict("records")) for name, group in frame.groupby(key, sort=True)}


def replay_dataset(name: str, spec: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    c = cfg()
    data_dir = Path(spec["dir"])
    data = load_data(data_dir)
    start = pd.Timestamp(spec["start"])
    end = pd.Timestamp(spec["end"])
    data = data[(data.index >= start) & (data.index < end)].copy()
    features = build_features(data, c.normal_window_sec, c)
    close = data["close"].to_numpy(float)
    features["ret_300s_bps"] = np.log(features["close"] / features["close"].shift(300)) * 10000.0
    features["bid20_chg_60"] = features["bid_qty_20"] / features["bid_qty_20"].shift(60).replace(0, np.nan) - 1.0

    rows: list[dict[str, Any]] = []
    vetoed: list[dict[str, Any]] = []
    raw_candidates = 0
    warmup = max(c.normal_window_sec, c.center_slope_sec, c.retest_sec, 900) + 10
    limit = len(data) - c.horizon_sec
    last_emit_idx = -10**12
    for idx in range(warmup, max(warmup, limit)):
        row = features.iloc[idx]
        if not bool(data["ob_available"].iloc[idx]) or not normal_ready(row, c):
            continue
        signal, reason = signal_from_row(row, c)
        if not signal:
            continue
        raw_candidates += 1
        raw_signal, raw_reason = signal, reason
        trap = bidwall_trap(signal, reason, row)
        if trap:
            signal = "DOWN"
            reason = "lower_reclaim_bidwall_trap_flip_down"
        veto = quality_v2_veto(signal, row)
        if veto:
            last_emit_idx = idx
            vetoed.append(
                {
                    "dataset": name,
                    "idx": int(idx),
                    "time": data.index[idx],
                    "raw_signal": raw_signal,
                    "raw_reason": raw_reason,
                    "vetoed_signal": signal,
                    "reason": veto,
                }
            )
            continue
        if idx - last_emit_idx < c.min_gap_sec:
            continue
        entry = float(close[idx])
        settle = float(close[idx + c.horizon_sec])
        won = bool(settle > entry if signal == "UP" else settle < entry)
        last_emit_idx = idx
        rows.append(
            {
                "dataset": name,
                "idx": int(idx),
                "time": data.index[idx],
                "signal": signal,
                "reason": reason,
                "raw_signal": raw_signal,
                "raw_reason": raw_reason,
                "bidwall_trap": bool(trap),
                "entry": entry,
                "settle": settle,
                "settle_time": data.index[idx + c.horizon_sec],
                "won": won,
                "z": round(float(row["z"]), 5),
                "inside1_ratio": round(float(row["inside1_ratio"]), 5),
                "observed_pct": round(float(row["observed_pct"]), 4),
                "center_slope_bps": round(float(row["center_slope_bps"]), 4),
                "sigma_bps": round(float(row["sigma_bps"]), 4),
                "sigma_expand": round(float(row["sigma_expand"]), 4),
                "flow_60": round(float(row["flow_60"]), 6),
                "imbalance_20": round(float(row["imbalance_20"]), 6),
                "micro_bps": round(float(row["micro_bps"]), 6),
                "ret_300s_bps": round(float(row["ret_300s_bps"]), 4),
                "bid20_chg_60": round(float(row["bid20_chg_60"]), 6),
            }
        )
    summary = {
        "dir": str(data_dir),
        "windowStart": start.isoformat(),
        "windowEnd": end.isoformat(),
        "rows": int(len(data)),
        "start": data.index.min().isoformat(),
        "end": data.index.max().isoformat(),
        "hours": round((data.index.max() - data.index.min()).total_seconds() / 3600.0, 2),
        "rawCandidatesBeforeV2AndGap": int(raw_candidates),
        "vetoedBeforeGap": int(len(vetoed)),
        "accepted": metrics(rows),
        "vetoedByReason": split_metrics(
            [{**row, "won": False, "signal": row["vetoed_signal"]} for row in vetoed],
            "reason",
        ),
    }
    return summary, rows


def run() -> dict[str, Any]:
    all_rows: list[dict[str, Any]] = []
    datasets: dict[str, Any] = {}
    for name, spec in DATASETS.items():
        summary, rows = replay_dataset(name, spec)
        datasets[name] = summary
        all_rows.extend(rows)
    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "parity": {
            "signalSource": "Mirrors SecondNormalLiquidityOrderbookV1Strategy row logic.",
            "gapOrder": "The first signal candidate in a 600s window owns the window; a V2-vetoed candidate also starts cooldown.",
            "knownDifference": "Live has an execution freshness gate and real tablet execution layer; this audit only checks signal logic.",
        },
        "overall": metrics(all_rows),
        "tradesPerDay": round(len(all_rows) / sum(row["hours"] for row in datasets.values()) * 24.0, 2),
        "byDataset": {name: item["accepted"] for name, item in datasets.items()},
        "byDay": split_metrics([{**row, "day": row["time"].strftime("%Y-%m-%d")} for row in all_rows], "day"),
        "bySide": split_metrics(all_rows, "signal"),
        "datasets": datasets,
        "tradesCsv": str(OUT_TRADES),
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(all_rows).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False))
