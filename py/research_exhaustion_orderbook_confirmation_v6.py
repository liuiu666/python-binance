"""Order-book confirmation for fixed exhaustion-reclaim candidates."""

from __future__ import annotations

import gc
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research_long_minute_consensus_v1 import read_minutes
from research_normal_liquidity_orderbook import read_orderbook
from research_path_efficiency_router_v1 import attach_delay_outcomes, clean, metrics
from research_path_exhaustion_reclaim_v2 import build_candidates
from run_multi_normal_hf_stable_backtest import DEFAULT_SOURCES
from second_backtest.data import load_second_bars


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "tmp" / "exhaustion_orderbook_confirmation_v6.json"
OUT_CSV = ROOT / "tmp" / "exhaustion_orderbook_confirmation_v6_trades.csv"
DELAYS = (0, 5, 6, 10)


@dataclass(frozen=True)
class Source:
    name: str
    seconds: Path
    orderbook: Path
    start: str | None = None
    end: str | None = None


SOURCES = tuple(
    Source(item.name, item.seconds, item.orderbook, item.start, item.end)
    for item in DEFAULT_SOURCES
) + (
    Source(
        "july11_12",
        ROOT / "tmp" / "latest_pull_20260712_migration_fix" / "extracted" / "data" / "btcusdt_1s_trades.csv",
        ROOT / "tmp" / "latest_pull_20260712_migration_fix" / "extracted" / "data" / "btcusdt_orderbook_1s.csv",
        "2026-07-11T16:00:00Z",
    ),
    Source(
        "july12_13",
        ROOT / "tmp" / "phase_live_audit_20260713" / "btcusdt_1s_trades.csv",
        ROOT / "tmp" / "phase_live_audit_20260713" / "btcusdt_orderbook_1s.csv",
        "2026-07-12T15:20:00Z",
    ),
    Source(
        "july14_15",
        ROOT / "tmp" / "frozen_position_forward" / "btcusdt_1s_trades.csv",
        ROOT / "tmp" / "frozen_position_forward" / "btcusdt_orderbook_1s.csv",
    ),
)


def utc(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")


def flow_ratio(frame: pd.DataFrame) -> float:
    buy = float(frame.buy_qty.fillna(0.0).sum())
    sell = float(frame.sell_qty.fillna(0.0).sum())
    return (buy - sell) / (buy + sell) if buy + sell > 0.0 else 0.0


def extract_source(source: Source, candidates: pd.DataFrame) -> list[dict[str, Any]]:
    if not source.seconds.exists() or not source.orderbook.exists():
        return []
    bars = load_second_bars(source.seconds, include_shards=False)
    book = read_orderbook(source.orderbook, bars.index)
    data = bars.join(book, how="left").sort_index()
    start = utc(source.start) if source.start else utc(data.index.min())
    end = utc(source.end) if source.end else utc(data.index.max())
    subset = candidates[
        candidates.time.add(pd.Timedelta(seconds=59)).between(start, end)
    ]
    rows: list[dict[str, Any]] = []
    for row in subset.to_dict("records"):
        detected = pd.Timestamp(row["time"]) + pd.Timedelta(seconds=59)
        end_pos = int(data.index.searchsorted(detected, side="right") - 1)
        if end_pos < 59 or abs((data.index[end_pos] - detected).total_seconds()) > 2:
            continue
        window = data.iloc[end_pos - 59:end_pos + 1]
        observed = window.get("observed", pd.Series(True, index=window.index)).fillna(False)
        available = window.get("ob_available", pd.Series(False, index=window.index)).fillna(False)
        if float(observed.mean()) < 0.90 or float(available.mean()) < 0.90:
            continue
        flow = flow_ratio(window)
        imbalance = float(pd.to_numeric(window.imbalance_20, errors="coerce").mean())
        micro = float(pd.to_numeric(window.microprice_edge_bps, errors="coerce").mean())
        if not all(np.isfinite(value) for value in (flow, imbalance, micro)):
            continue
        direction = 1.0 if row["signal"] == "UP" else -1.0
        votes = int(direction * flow > 0.0) + int(direction * imbalance > 0.0) + int(direction * micro > 0.0)
        item = dict(row)
        item.update(
            source=source.name,
            flow_60=flow,
            imbalance_60=imbalance,
            micro_60=micro,
            confirmation_votes=votes,
        )
        rows.append(item)
    del data, bars, book
    gc.collect()
    return rows


def run() -> dict[str, Any]:
    candidates = build_candidates(read_minutes())
    candidates = candidates[candidates.time.ge(pd.Timestamp("2026-07-05", tz="UTC"))].copy()
    rows: list[dict[str, Any]] = []
    for source in SOURCES:
        rows.extend(extract_source(source, candidates))
    confirmed = pd.DataFrame(rows)
    if not confirmed.empty:
        confirmed = confirmed.sort_values(["time", "source"]).drop_duplicates("time", keep="last")
        confirmed = confirmed[confirmed.confirmation_votes.ge(2)].copy()
    confirmed["period"] = np.select(
        [
            confirmed.time.lt(pd.Timestamp("2026-07-11", tz="UTC")),
            confirmed.time.lt(pd.Timestamp("2026-07-14", tz="UTC")),
        ],
        ["development_july5_10", "validation_july11_13"],
        default="forward_july14_15",
    )
    confirmed["beijing_day"] = confirmed.time.dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    latest = attach_delay_outcomes(confirmed[confirmed.period.eq("forward_july14_15")].copy())
    for delay in DELAYS:
        confirmed.loc[latest.index, f"move_bps_d{delay}"] = latest[f"move_bps_d{delay}"]
    confirmed.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    period_hours = {
        "development_july5_10": 144.0,
        "validation_july11_13": 72.0,
        "forward_july14_15": 35.16,
    }
    report = {
        "method": {
            "parameterSearch": False, "causal": True,
            "baseSignal": "fixed one-sided exhaustion reclaim V2",
            "confirmation": "at least two of trailing-60s flow, depth imbalance and microprice align with reclaim",
            "minimumSecondAndBookCoveragePct": 90,
        },
        "candidateCountSinceJuly5": len(candidates),
        "confirmedCount": len(confirmed),
        "periods": {
            period: metrics(group, "move_bps", period_hours[period])
            for period, group in confirmed.groupby("period")
        },
        "forwardDelays": {
            f"delay{delay}s": metrics(latest, f"move_bps_d{delay}", 35.16)
            for delay in DELAYS
        },
        "forwardByDayDelay6s": {
            day: metrics(group, "move_bps_d6", 24.0)
            for day, group in latest.groupby("beijing_day")
        },
        "acceptance": {"minTradesPerDay": 10.0, "minWinRate": 55.56, "maxDrawdownU": 20.0, "maxLossStreak": 3, "allDelaysMustBeProfitable": True},
        "tradesCsv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
