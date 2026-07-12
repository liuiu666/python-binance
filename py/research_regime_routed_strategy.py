"""Replay a unified router between normal reversion and trend formation signals."""

from __future__ import annotations

import json
import math
import sys
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_market_regime_classifier as regime  # noqa: E402
import research_v2_persistent_reclaim as normal  # noqa: E402


OUT_JSON = ROOT / "tmp" / "regime_routed_strategy_research.json"
OUT_TRADES = ROOT / "tmp" / "regime_routed_strategy_trades.csv"


@dataclass(frozen=True)
class TrendConfirm:
    name: str
    window_sec: int
    min_hits: int
    min_span_sec: int


TREND_CONFIRMS = (
    TrendConfirm("immediate", 0, 1, 0),
    TrendConfirm("repeat_2of15_span5", 15, 2, 5),
    TrendConfirm("repeat_3of30_span10", 30, 3, 10),
    TrendConfirm("repeat_5of30_span15", 30, 5, 15),
)
ROUTE_CONFIRMS = (
    TrendConfirm("repeat_6of40_span20", 40, 6, 20),
    TrendConfirm("repeat_7of50_span25", 50, 7, 25),
    TrendConfirm("repeat_8of60_span30", 60, 8, 30),
)


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


def metrics(rows: list[dict]) -> dict:
    equity = peak = drawdown = wins = 0
    margins = []
    for row in sorted(rows, key=lambda item: (item["dataset"], item["time"])):
        won = bool(row["won"])
        wins += int(won)
        equity += 4 if won else -5
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        margins.append(float(row["signed_outcome_bps"]))
    count = len(rows)
    return {
        "trades": count,
        "winRate": round(wins / count * 100.0, 2) if count else 0.0,
        "pnlU": int(equity),
        "maxDrawdownU": int(drawdown),
        "medianSignedBps": round(float(np.median(margins)), 3) if margins else 0.0,
        "thinAbsLe5": sum(abs(value) <= 5.0 for value in margins),
    }


def trend_formation_mask(classified: pd.DataFrame) -> pd.Series:
    direction = classified["trend_direction"]
    return (
        classified["regime"].eq("trend")
        & (direction * classified["ret_60s_bps"] >= 4.0)
        & (direction * classified["imbalance_60_mean"] >= 0.16)
        & (direction * classified["bandwalk_signed"] <= 0.60)
        & (classified["sigma_expand"] <= 1.60)
    )


def replay_dataset(
    name: str,
    prepared: dict,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    scan_interval: int,
    scan_phase: int,
    route_mode: str,
    trend_confirm: TrendConfirm,
) -> list[dict]:
    rules = normal.load_rules()
    data = prepared["data"]
    classified = prepared["classified"]
    trend_mask = prepared["trend_mask"]
    candidate_map = prepared["candidate_map"]
    close = data["close"].to_numpy(float)
    normal_history: dict[str, deque[dict]] = {"UP": deque(), "DOWN": deque()}
    trend_history: dict[str, deque[int]] = {"UP": deque(), "DOWN": deque()}
    last_entry_idx = -10**12
    trades = []
    first_idx = max(0, int(data.index.searchsorted(start)))
    last_idx = min(len(data) - rules.horizon_sec, int(data.index.searchsorted(end)))
    for idx in range(first_idx, last_idx):
        if int(data.index[idx].timestamp()) % scan_interval != scan_phase:
            continue
        candidate = candidate_map.get(idx)
        normal_ready = False
        normal_signal = None
        normal_meta = None
        if candidate is not None:
            direction = str(candidate["signal"])
            queue = normal_history[direction]
            queue.append(candidate)
            while queue and idx - int(queue[0]["idx"]) > 30:
                queue.popleft()
            if len(queue) >= 3 and idx - int(queue[0]["idx"]) >= 8:
                normal_ready = True
                normal_signal = direction
                normal_meta = candidate

        is_trend = bool(trend_mask.iloc[idx])
        trend_signal = "UP" if float(classified["trend_direction"].iloc[idx]) > 0 else "DOWN"
        trend_ready = False
        if is_trend:
            trend_queue = trend_history[trend_signal]
            trend_queue.append(idx)
            if trend_confirm.window_sec > 0:
                while trend_queue and idx - trend_queue[0] > trend_confirm.window_sec:
                    trend_queue.popleft()
            else:
                while len(trend_queue) > 1:
                    trend_queue.popleft()
            trend_ready = (
                len(trend_queue) >= trend_confirm.min_hits
                and idx - trend_queue[0] >= trend_confirm.min_span_sec
            )
        if idx - last_entry_idx < rules.min_gap_sec:
            continue

        signal = reason = kind = None
        if trend_ready:
            signal = trend_signal
            reason = "strict_trend_formation"
            kind = "trend"
        elif normal_ready and route_mode in {"trend_else_normal", "normal_nontrend"}:
            signal = normal_signal
            reason = str(normal_meta["reason"])
            kind = "normal"
        if route_mode == "trend_only" and not trend_ready:
            signal = None
        if signal is None:
            continue

        settle_idx = idx + rules.horizon_sec
        entry = float(close[idx])
        settle = float(close[settle_idx])
        sign = 1.0 if signal == "UP" else -1.0
        margin = (settle / entry - 1.0) * 10000.0 * sign
        trades.append(
            {
                "dataset": name,
                "time": data.index[idx],
                "settle_time": data.index[settle_idx],
                "kind": kind,
                "signal": signal,
                "reason": reason,
                "entry": entry,
                "settle": settle,
                "signed_outcome_bps": float(margin),
                "won": bool(margin > 0.0),
                "regime": str(classified["regime"].iloc[idx]),
                "trend_votes": int(classified["trend_votes"].iloc[idx]),
                "normal_votes": int(classified["normal_votes"].iloc[idx]),
                "ret_60s_bps": float(classified["ret_60s_bps"].iloc[idx]),
                "ret_600s_bps": float(classified["ret_600s_bps"].iloc[idx]),
                "efficiency_600": float(classified["efficiency_600"].iloc[idx]),
                "bandwalk_signed": float(classified["bandwalk_signed"].iloc[idx]),
                "sigma_expand": float(classified["sigma_expand"].iloc[idx]),
                "imbalance_60_mean": float(classified["imbalance_60_mean"].iloc[idx]),
                "scan_interval": scan_interval,
                "scan_phase": scan_phase,
            }
        )
        last_entry_idx = idx
        normal_history = {"UP": deque(), "DOWN": deque()}
        trend_history = {"UP": deque(), "DOWN": deque()}
    return trades


def load_datasets():
    rules = normal.load_rules()
    loaded = {}
    cache = {}
    for name, item in normal.DATASETS.items():
        key = (str(item["seconds"]), str(item["orderbook"]))
        if key not in cache:
            cache[key] = normal.load_market(Path(item["seconds"]), Path(item["orderbook"]))
        start = pd.Timestamp(item["start"])
        end = pd.Timestamp(item["end"])
        full = cache[key]
        data = full[
            (full.index >= start - pd.Timedelta(seconds=3700))
            & (full.index < end + pd.Timedelta(seconds=rules.horizon_sec + 5))
        ].copy()
        features = regime.build_regime_features(data)
        strict = next(item for item in regime.SPECS if item.name == "strict")
        classified = regime.classify(features, strict)
        _, candidate_rows = normal.candidate_stream(data, rules)
        loaded[name] = (
            {
                "data": data,
                "classified": classified,
                "trend_mask": trend_formation_mask(classified),
                "candidate_map": {
                    int(row["idx"]): row for row in candidate_rows if start <= row["time"] < end
                },
            },
            start,
            end,
        )
    return loaded


def summarize_case(rows: list[dict]) -> dict:
    return {
        "overall": metrics(rows),
        "byKind": {kind: metrics([row for row in rows if row["kind"] == kind]) for kind in ("normal", "trend")},
        "byDataset": {
            name: metrics([row for row in rows if row["dataset"] == name])
            for name in normal.DATASETS
        },
    }


def run() -> dict:
    loaded = load_datasets()
    reports = []
    all_rows = []
    for trend_confirm in ():
        route_mode = "trend_only"
        base_rows = []
        for name, (prepared, start, end) in loaded.items():
            base_rows.extend(replay_dataset(name, prepared, start, end, scan_interval=1, scan_phase=0, route_mode=route_mode, trend_confirm=trend_confirm))
        reports.append(
            {
                "routeMode": route_mode,
                "trendConfirm": asdict(trend_confirm),
                **summarize_case(base_rows),
            }
        )
        all_rows.extend({**row, "route_mode": route_mode, "trend_confirm": trend_confirm.name} for row in base_rows)
    selected_confirm = ROUTE_CONFIRMS[0]
    selected_route_rows = []
    for route_confirm in ROUTE_CONFIRMS:
        route_rows = []
        for name, (prepared, start, end) in loaded.items():
            route_rows.extend(
                replay_dataset(
                    name,
                    prepared,
                    start,
                    end,
                    scan_interval=1,
                    scan_phase=0,
                    route_mode="trend_else_normal",
                    trend_confirm=route_confirm,
                )
            )
        reports.append(
            {
                "routeMode": "trend_else_normal",
                "trendConfirm": asdict(route_confirm),
                **summarize_case(route_rows),
            }
        )
        all_rows.extend(
            {**row, "route_mode": "trend_else_normal", "trend_confirm": route_confirm.name}
            for row in route_rows
        )
        if route_confirm == selected_confirm:
            selected_route_rows = route_rows
    phase_reports = []
    for phase in range(5):
        phase_rows = []
        for name, (prepared, start, end) in loaded.items():
            phase_rows.extend(
                replay_dataset(
                    name,
                    prepared,
                    start,
                    end,
                    scan_interval=5,
                    scan_phase=phase,
                    route_mode="trend_else_normal",
                    trend_confirm=selected_confirm,
                )
            )
        phase_reports.append({"phase": phase, **summarize_case(phase_rows)})
    reports.append(
        {
            "routeMode": "trend_else_normal_phase_stress",
            "trendConfirm": asdict(selected_confirm),
            **summarize_case(selected_route_rows),
            "scan5sPhaseStress": phase_reports,
        }
    )
    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "reports": reports,
        "note": "Unified chronological replay. Regime features use past/current data; future prices are used only for settlement.",
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(clean(all_rows)).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
