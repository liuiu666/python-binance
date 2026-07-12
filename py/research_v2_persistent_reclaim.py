"""Research a timing-robust confirmation layer for the normal/liquidity V2 strategy.

The production rule core is reused unchanged. This script only changes signal
selection: an accepted reclaim must repeat in the same direction after a
minimum wall-clock span before entry. No future data is used for confirmation.
"""

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

from research_normal_liquidity_orderbook import read_orderbook  # noqa: E402
from second_backtest.data import load_second_bars  # noqa: E402
from liquidity_v2_core import (  # noqa: E402
    LiquidityV2Rules,
    build_features,
    evaluate_candidate,
    normal_ready,
)


DATASETS = {
    "2026-07-05_06": {
        "seconds": ROOT / "tmp" / "latest_pull_20260706_2130" / "data" / "btcusdt_1s_trades.csv",
        "orderbook": ROOT / "tmp" / "latest_pull_20260706_2130" / "data" / "btcusdt_orderbook_1s.csv",
        "start": "2026-07-05T00:00:00Z",
        "end": "2026-07-07T00:00:00Z",
    },
    "2026-07-08": {
        "seconds": ROOT / "tmp" / "latest_live_pull_20260709_101331" / "data" / "btcusdt_1s_trades.csv",
        "orderbook": ROOT / "tmp" / "latest_live_pull_20260709_101331" / "data" / "btcusdt_orderbook_1s.csv",
        "start": "2026-07-08T00:00:00Z",
        "end": "2026-07-09T00:00:00Z",
    },
    "2026-07-09": {
        "seconds": ROOT / "tmp" / "latest_pull_20260710_203217" / "data" / "btcusdt_1s_trades.csv",
        "orderbook": ROOT / "tmp" / "latest_pull_20260710_203217" / "data" / "btcusdt_orderbook_1s.csv",
        "start": "2026-07-09T00:00:00Z",
        "end": "2026-07-10T00:00:00Z",
    },
    "2026-07-10": {
        "seconds": ROOT / "tmp" / "latest_pull_20260710_203217" / "data" / "btcusdt_1s_trades.csv",
        "orderbook": ROOT / "tmp" / "latest_pull_20260710_203217" / "data" / "btcusdt_orderbook_1s.csv",
        "start": "2026-07-10T00:00:00Z",
        "end": "2026-07-11T00:00:00Z",
    },
}

OUT_JSON = ROOT / "tmp" / "v2_persistent_reclaim_research.json"
OUT_TRADES = ROOT / "tmp" / "v2_persistent_reclaim_trades.csv"


@dataclass(frozen=True)
class ConfirmSpec:
    name: str
    window_sec: int
    min_hits: int
    min_span_sec: int
    require_current: bool = True
    conflict_free: bool = False
    min_progress_bps: float | None = None


SPECS = [
    ConfirmSpec("instant", 0, 1, 0),
    ConfirmSpec("repeat_2of12_span3", 12, 2, 3),
    ConfirmSpec("repeat_2of20_span5", 20, 2, 5),
    ConfirmSpec("repeat_3of20_span5", 20, 3, 5),
    ConfirmSpec("progress_3of30_span5_p05", 30, 3, 5, min_progress_bps=0.5),
    ConfirmSpec("progress_3of30_span5_p10", 30, 3, 5, min_progress_bps=1.0),
    ConfirmSpec("progress_3of30_span5_p20", 30, 3, 5, min_progress_bps=2.0),
    ConfirmSpec("repeat_3of25_span6", 25, 3, 6),
    ConfirmSpec("repeat_2of30_span8", 30, 2, 8),
    ConfirmSpec("repeat_3of30_span6", 30, 3, 6),
    ConfirmSpec("repeat_3of30_span7", 30, 3, 7),
    ConfirmSpec("repeat_3of30_span8", 30, 3, 8),
    ConfirmSpec("consensus_3of30_span6", 30, 3, 6, conflict_free=True),
    ConfirmSpec("consensus_3of30_span8", 30, 3, 8, conflict_free=True),
    ConfirmSpec("repeat_3of30_span9", 30, 3, 9),
    ConfirmSpec("repeat_3of30_span10", 30, 3, 10),
    ConfirmSpec("repeat_4of30_span8", 30, 4, 8),
    ConfirmSpec("repeat_3of40_span8", 40, 3, 8),
]


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


def load_rules() -> LiquidityV2Rules:
    raw = json.loads((ROOT / "data" / "prod_config.json").read_text(encoding="utf-8-sig"))
    cfg = raw["BTC_10min_NORMAL_LIQ_OB_V2_QUALITY"]
    return LiquidityV2Rules.from_config(cfg)


def load_market(seconds: Path, orderbook: Path) -> pd.DataFrame:
    bars = load_second_bars(seconds, include_shards=True)
    ob = read_orderbook(orderbook, bars.index)
    data = bars.join(ob, how="left")
    return data[~data.index.duplicated(keep="last")].sort_index()


def candidate_stream(data: pd.DataFrame, rules: LiquidityV2Rules) -> tuple[pd.DataFrame, list[dict]]:
    features = build_features(data, rules)
    warmup = max(rules.normal_window_sec, rules.center_slope_sec, rules.retest_sec, 3600) + 10
    limit = len(data) - rules.horizon_sec
    rows: list[dict] = []
    for idx in range(warmup, max(warmup, limit)):
        row = features.iloc[idx]
        if not bool(data["ob_available"].iloc[idx]) or not normal_ready(row, rules):
            continue
        decision = evaluate_candidate(row, rules)
        if decision["status"] != "accepted":
            continue
        rows.append(
            {
                "idx": idx,
                "time": data.index[idx],
                "signal": decision["signal"],
                "reason": decision["reason"],
                "close": float(row["close"]),
                "z": float(row["z"]),
                "flow_60": float(row["flow_60"]),
                "imbalance_20": float(row["imbalance_20"]),
                "micro_bps": float(row["micro_bps"]),
                "center_slope_bps": float(row["center_slope_bps"]),
                "sigma_bps": float(row["sigma_bps"]),
                "ret_600s_bps": float(row["ret_600s_bps"]),
                "bidwall_trap": bool(decision["bidwall_trap"]),
            }
        )
    return features, rows


def select_confirmed(
    data: pd.DataFrame,
    candidates: list[dict],
    rules: LiquidityV2Rules,
    spec: ConfirmSpec,
    scan_interval: int = 1,
    scan_phase: int = 0,
) -> list[dict]:
    close = data["close"].to_numpy(float)
    history: dict[str, deque[dict]] = {"UP": deque(), "DOWN": deque()}
    recent: deque[dict] = deque()
    last_entry = -10**12
    selected: list[dict] = []
    for row in candidates:
        idx = int(row["idx"])
        epoch = int(data.index[idx].timestamp())
        if epoch % scan_interval != scan_phase:
            continue
        direction = str(row["signal"])
        recent.append(row)
        while recent and idx - int(recent[0]["idx"]) > spec.window_sec:
            recent.popleft()
        queue = history[direction]
        queue.append(row)
        if spec.window_sec > 0:
            while queue and idx - int(queue[0]["idx"]) > spec.window_sec:
                queue.popleft()
        else:
            while len(queue) > 1:
                queue.popleft()
        if idx - last_entry < rules.min_gap_sec:
            continue
        if len(queue) < spec.min_hits:
            continue
        span = idx - int(queue[0]["idx"])
        if span < spec.min_span_sec:
            continue
        if spec.conflict_free and any(item["signal"] != direction for item in recent):
            continue
        first_price = float(queue[0]["close"])
        direction_sign = 1.0 if direction == "UP" else -1.0
        progress_bps = (float(row["close"]) / first_price - 1.0) * 10000.0 * direction_sign
        if spec.min_progress_bps is not None and progress_bps < spec.min_progress_bps:
            continue
        settle_idx = idx + rules.horizon_sec
        if settle_idx >= len(data):
            continue
        entry = float(close[idx])
        settle = float(close[settle_idx])
        signed_bp = (settle / entry - 1.0) * 10000.0 * (1.0 if direction == "UP" else -1.0)
        item = dict(row)
        item.update(
            confirmation=spec.name,
            confirmation_hits=len(queue),
            confirmation_span_sec=span,
            confirmation_progress_bps=float(progress_bps),
            entry=entry,
            settle=settle,
            settle_time=data.index[settle_idx],
            signed_outcome_bps=float(signed_bp),
            won=bool(signed_bp > 0.0),
            scan_interval=scan_interval,
            scan_phase=scan_phase,
        )
        selected.append(item)
        last_entry = idx
        history = {"UP": deque(), "DOWN": deque()}
        recent = deque()
    return selected


def metrics(rows: list[dict]) -> dict:
    ordered = sorted(rows, key=lambda row: (str(row["dataset"]), int(row["idx"])))
    equity = peak = max_dd = 0
    wins = 0
    margins: list[float] = []
    for row in ordered:
        won = bool(row["won"])
        wins += int(won)
        equity += 4 if won else -5
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        margins.append(float(row["signed_outcome_bps"]))
    count = len(ordered)
    return {
        "trades": count,
        "winRate": round(wins / count * 100.0, 2) if count else 0.0,
        "pnlU": equity,
        "maxDrawdownU": max_dd,
        "medianSignedBps": round(float(np.median(margins)), 3) if margins else 0.0,
        "thinAbsLe5": sum(abs(value) <= 5.0 for value in margins),
        "strongWinGe10": sum(value >= 10.0 for value in margins),
    }


def timing_shift_metrics(rows: list[dict], datasets: dict[str, pd.DataFrame], horizon: int) -> dict:
    report = {}
    for shift in (-5, -2, -1, 0, 1, 2, 5):
        shifted = []
        for row in rows:
            data = datasets[str(row["dataset"])]
            idx = int(row["idx"]) + shift
            settle_idx = idx + horizon
            if idx < 0 or settle_idx >= len(data):
                continue
            entry = float(data["close"].iloc[idx])
            settle = float(data["close"].iloc[settle_idx])
            sign = 1.0 if row["signal"] == "UP" else -1.0
            margin = (settle / entry - 1.0) * 10000.0 * sign
            shifted.append({**row, "won": margin > 0.0, "signed_outcome_bps": margin})
        report[str(shift)] = metrics(shifted)
    return report


def run() -> dict:
    rules = load_rules()
    loaded: dict[str, pd.DataFrame] = {}
    candidates_by_dataset: dict[str, list[dict]] = {}
    coverage: dict[str, dict] = {}
    source_cache: dict[tuple[str, str], pd.DataFrame] = {}
    for name, source in DATASETS.items():
        key = (str(source["seconds"]), str(source["orderbook"]))
        if key not in source_cache:
            source_cache[key] = load_market(Path(source["seconds"]), Path(source["orderbook"]))
        full_data = source_cache[key]
        start = pd.Timestamp(source["start"])
        end = pd.Timestamp(source["end"])
        # Keep enough pre-window rows for all rolling features and post-window rows for settlement.
        data = full_data[
            (full_data.index >= start - pd.Timedelta(seconds=3700))
            & (full_data.index < end + pd.Timedelta(seconds=rules.horizon_sec + 5))
        ].copy()
        _, all_candidates = candidate_stream(data, rules)
        candidates = [row for row in all_candidates if start <= row["time"] < end]
        loaded[name] = data
        candidates_by_dataset[name] = candidates
        day = data[(data.index >= start) & (data.index < end)]
        coverage[name] = {
            "start": day.index.min().isoformat() if len(day) else None,
            "end": day.index.max().isoformat() if len(day) else None,
            "hours": round((day.index.max() - day.index.min()).total_seconds() / 3600.0, 3) if len(day) else 0.0,
            "secondCoveragePct": round(float(day["observed"].mean() * 100.0), 3) if len(day) else 0.0,
            "orderbookCoveragePct": round(float(day["ob_available"].mean() * 100.0), 3) if len(day) else 0.0,
            "acceptedCandidateSeconds": len(candidates),
        }

    reports = []
    all_trade_rows: list[dict] = []
    for spec in SPECS:
        rows = []
        for name, data in loaded.items():
            selected = select_confirmed(data, candidates_by_dataset[name], rules, spec)
            rows.extend({**row, "dataset": name} for row in selected)
        by_day = {
            name: metrics([row for row in rows if row["dataset"] == name])
            for name in DATASETS
        }
        phase_stress = {}
        for interval in (2, 5):
            phase_stress[str(interval)] = []
            for phase in range(interval):
                phase_rows = []
                for name, data in loaded.items():
                    selected = select_confirmed(
                        data,
                        candidates_by_dataset[name],
                        rules,
                        spec,
                        scan_interval=interval,
                        scan_phase=phase,
                    )
                    phase_rows.extend({**row, "dataset": name} for row in selected)
                phase_stress[str(interval)].append({"phase": phase, **metrics(phase_rows)})
        report = {
            "spec": asdict(spec),
            "overall": metrics(rows),
            "byDataset": by_day,
            "entryShiftStress": timing_shift_metrics(rows, loaded, rules.horizon_sec),
            "scanPhaseStress": phase_stress,
        }
        reports.append(report)
        all_trade_rows.extend({**row, "research_spec": spec.name} for row in rows)

    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "purpose": "Test repeated same-direction confirmation to reduce second-level timing sensitivity.",
        "rules": asdict(rules),
        "coverage": coverage,
        "reports": reports,
        "note": "Research only. No production configuration or live code was changed.",
        "tradesCsv": str(OUT_TRADES),
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(clean(all_trade_rows)).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    result = run()
    compact = [
        {
            "name": row["spec"]["name"],
            "overall": row["overall"],
            "byDataset": row["byDataset"],
            "entryShiftStress": row["entryShiftStress"],
            "scanPhaseStress": row["scanPhaseStress"],
        }
        for row in result["reports"]
    ]
    print(json.dumps(clean(compact), ensure_ascii=False, indent=2))
