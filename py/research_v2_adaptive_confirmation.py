"""Research a two-tier confirmation layer for the normal/liquidity V2 strategy."""

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

import research_v2_persistent_reclaim as persistent  # noqa: E402


OUT_JSON = ROOT / "tmp" / "v2_adaptive_confirmation_research.json"
OUT_TRADES = ROOT / "tmp" / "v2_adaptive_confirmation_trades.csv"


@dataclass(frozen=True)
class AdaptiveSpec:
    name: str
    fast_enabled: bool
    fast_window_sec: int = 15
    fast_min_hits: int = 2
    fast_min_span_sec: int = 3
    fast_min_progress_bps: float = 0.5
    fast_min_votes: int = 2
    fast_flow: float = 0.12
    fast_imbalance: float = 0.35
    fast_micro_bps: float = 0.002
    slow_window_sec: int = 30
    slow_min_hits: int = 3
    slow_min_span_sec: int = 8


SPECS = [
    AdaptiveSpec("slow_only_3of30_span8", fast_enabled=False),
    AdaptiveSpec("adaptive_balanced", fast_enabled=True),
    AdaptiveSpec(
        "adaptive_strict",
        fast_enabled=True,
        fast_min_progress_bps=0.8,
        fast_flow=0.18,
        fast_imbalance=0.50,
        fast_micro_bps=0.0025,
    ),
    AdaptiveSpec(
        "adaptive_flow_confirmed",
        fast_enabled=True,
        fast_min_progress_bps=0.5,
        fast_min_votes=2,
        fast_flow=0.08,
        fast_imbalance=0.45,
        fast_micro_bps=0.002,
    ),
    AdaptiveSpec(
        "adaptive_three_votes",
        fast_enabled=True,
        fast_min_progress_bps=0.3,
        fast_min_votes=3,
        fast_flow=0.08,
        fast_imbalance=0.30,
        fast_micro_bps=0.0015,
    ),
]


def clean(value: Any) -> Any:
    return persistent.clean(value)


def prepare() -> tuple[persistent.LiquidityV2Rules, dict[str, pd.DataFrame], dict[str, list[dict]], dict]:
    rules = persistent.load_rules()
    loaded: dict[str, pd.DataFrame] = {}
    candidates: dict[str, list[dict]] = {}
    coverage: dict[str, dict] = {}
    cache: dict[tuple[str, str], pd.DataFrame] = {}
    for name, source in persistent.DATASETS.items():
        key = (str(source["seconds"]), str(source["orderbook"]))
        if key not in cache:
            cache[key] = persistent.load_market(Path(source["seconds"]), Path(source["orderbook"]))
        full = cache[key]
        start = pd.Timestamp(source["start"])
        end = pd.Timestamp(source["end"])
        data = full[
            (full.index >= start - pd.Timedelta(seconds=3700))
            & (full.index < end + pd.Timedelta(seconds=rules.horizon_sec + 5))
        ].copy()
        _, all_candidates = persistent.candidate_stream(data, rules)
        selected = [row for row in all_candidates if start <= row["time"] < end]
        day = data[(data.index >= start) & (data.index < end)]
        loaded[name] = data
        candidates[name] = selected
        coverage[name] = {
            "hours": round((day.index.max() - day.index.min()).total_seconds() / 3600.0, 3),
            "secondCoveragePct": round(float(day["observed"].mean() * 100.0), 3),
            "orderbookCoveragePct": round(float(day["ob_available"].mean() * 100.0), 3),
            "acceptedCandidateSeconds": len(selected),
        }
    return rules, loaded, candidates, coverage


def aligned_votes(row: dict, spec: AdaptiveSpec) -> tuple[int, list[str]]:
    sign = 1.0 if row["signal"] == "UP" else -1.0
    checks = {
        "flow": sign * float(row["flow_60"]) >= spec.fast_flow,
        "imbalance": sign * float(row["imbalance_20"]) >= spec.fast_imbalance,
        "microprice": sign * float(row["micro_bps"]) >= spec.fast_micro_bps,
    }
    passed = [name for name, ok in checks.items() if ok]
    return len(passed), passed


def select_adaptive(
    data: pd.DataFrame,
    candidates: list[dict],
    rules: persistent.LiquidityV2Rules,
    spec: AdaptiveSpec,
    scan_interval: int = 1,
    scan_phase: int = 0,
) -> list[dict]:
    close = data["close"].to_numpy(float)
    history: dict[str, deque[dict]] = {"UP": deque(), "DOWN": deque()}
    last_entry = -10**12
    rows: list[dict] = []
    for row in candidates:
        idx = int(row["idx"])
        if int(data.index[idx].timestamp()) % scan_interval != scan_phase:
            continue
        direction = str(row["signal"])
        queue = history[direction]
        queue.append(row)
        while queue and idx - int(queue[0]["idx"]) > spec.slow_window_sec:
            queue.popleft()
        if idx - last_entry < rules.min_gap_sec:
            continue

        span = idx - int(queue[0]["idx"])
        slow_ready = len(queue) >= spec.slow_min_hits and span >= spec.slow_min_span_sec
        fast_queue = [item for item in queue if idx - int(item["idx"]) <= spec.fast_window_sec]
        fast_span = idx - int(fast_queue[0]["idx"]) if fast_queue else 0
        first_price = float(fast_queue[0]["close"]) if fast_queue else float(row["close"])
        sign = 1.0 if direction == "UP" else -1.0
        progress = (float(row["close"]) / first_price - 1.0) * 10000.0 * sign
        votes, vote_names = aligned_votes(row, spec)
        fast_ready = (
            spec.fast_enabled
            and len(fast_queue) >= spec.fast_min_hits
            and fast_span >= spec.fast_min_span_sec
            and progress >= spec.fast_min_progress_bps
            and votes >= spec.fast_min_votes
        )
        if not fast_ready and not slow_ready:
            continue

        settle_idx = idx + rules.horizon_sec
        if settle_idx >= len(data):
            continue
        entry = float(close[idx])
        settle = float(close[settle_idx])
        margin = (settle / entry - 1.0) * 10000.0 * sign
        out = dict(row)
        out.update(
            adaptive_spec=spec.name,
            confirmation_tier="fast" if fast_ready else "slow",
            confirmation_hits=len(fast_queue) if fast_ready else len(queue),
            confirmation_span_sec=fast_span if fast_ready else span,
            confirmation_progress_bps=progress,
            confirmation_votes=votes,
            confirmation_vote_names=",".join(vote_names),
            entry=entry,
            settle=settle,
            settle_time=data.index[settle_idx],
            signed_outcome_bps=float(margin),
            won=bool(margin > 0.0),
            scan_interval=scan_interval,
            scan_phase=scan_phase,
        )
        rows.append(out)
        last_entry = idx
        history = {"UP": deque(), "DOWN": deque()}
    return rows


def metrics(rows: list[dict]) -> dict:
    return persistent.metrics(rows)


def entry_shift_metrics(rows: list[dict], datasets: dict[str, pd.DataFrame], horizon: int) -> dict:
    return persistent.timing_shift_metrics(rows, datasets, horizon)


def run() -> dict:
    rules, datasets, candidates, coverage = prepare()
    reports = []
    all_rows = []
    for spec in SPECS:
        rows = []
        for name, data in datasets.items():
            selected = select_adaptive(data, candidates[name], rules, spec)
            rows.extend({**row, "dataset": name} for row in selected)
        by_dataset = {
            name: metrics([row for row in rows if row["dataset"] == name])
            for name in persistent.DATASETS
        }
        by_tier = {
            tier: metrics([row for row in rows if row["confirmation_tier"] == tier])
            for tier in ("fast", "slow")
        }
        phase_stress = {}
        for interval in (2, 5):
            phase_stress[str(interval)] = []
            for phase in range(interval):
                phase_rows = []
                for name, data in datasets.items():
                    selected = select_adaptive(data, candidates[name], rules, spec, interval, phase)
                    phase_rows.extend({**row, "dataset": name} for row in selected)
                phase_stress[str(interval)].append({"phase": phase, **metrics(phase_rows)})
        reports.append(
            {
                "spec": asdict(spec),
                "overall": metrics(rows),
                "byDataset": by_dataset,
                "byTier": by_tier,
                "entryShiftStress": entry_shift_metrics(rows, datasets, rules.horizon_sec),
                "scanPhaseStress": phase_stress,
            }
        )
        all_rows.extend(rows)

    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "purpose": "Recover high-quality fast entries while retaining persistent confirmation for ordinary signals.",
        "coverage": coverage,
        "reports": reports,
        "note": "Research only. No live strategy or production configuration was changed.",
        "tradesCsv": str(OUT_TRADES),
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(clean(all_rows)).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    result = run()
    print(json.dumps(clean(result["reports"]), ensure_ascii=False, indent=2))
