"""Compare reclaim-only and edge+reclaim V2 signals under persistent confirmation."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_v2_persistent_reclaim as research  # noqa: E402


OUT_JSON = ROOT / "tmp" / "v2_hybrid_edge_confirmation.json"
OUT_TRADES = ROOT / "tmp" / "v2_hybrid_edge_confirmation_trades.csv"


def prepare_source_data(rules):
    loaded = {}
    coverage = {}
    cache = {}
    for name, source in research.DATASETS.items():
        key = (str(source["seconds"]), str(source["orderbook"]))
        if key not in cache:
            cache[key] = research.load_market(Path(source["seconds"]), Path(source["orderbook"]))
        start = pd.Timestamp(source["start"])
        end = pd.Timestamp(source["end"])
        full = cache[key]
        data = full[
            (full.index >= start - pd.Timedelta(seconds=3700))
            & (full.index < end + pd.Timedelta(seconds=rules.horizon_sec + 5))
        ].copy()
        loaded[name] = (data, start, end)
        day = data[(data.index >= start) & (data.index < end)]
        coverage[name] = {
            "hours": round((day.index.max() - day.index.min()).total_seconds() / 3600.0, 3),
            "secondCoveragePct": round(float(day["observed"].mean() * 100.0), 3),
            "orderbookCoveragePct": round(float(day["ob_available"].mean() * 100.0), 3),
        }
    return loaded, coverage


def run():
    base_rules = research.load_rules()
    loaded, coverage = prepare_source_data(base_rules)
    specs = [
        research.ConfirmSpec("repeat_2of20_span5", 20, 2, 5),
        research.ConfirmSpec("repeat_3of30_span8", 30, 3, 8),
    ]
    reports = []
    trade_rows = []
    for mode in ("reclaim", "hybrid"):
        rules = replace(base_rules, mode=mode)
        candidates = {}
        for name, (data, start, end) in loaded.items():
            _, all_rows = research.candidate_stream(data, rules)
            candidates[name] = [row for row in all_rows if start <= row["time"] < end]
        for spec in specs:
            rows = []
            for name, (data, _, _) in loaded.items():
                selected = research.select_confirmed(data, candidates[name], rules, spec)
                rows.extend({**row, "dataset": name, "mode": mode} for row in selected)
            by_dataset = {
                name: research.metrics([row for row in rows if row["dataset"] == name])
                for name in research.DATASETS
            }
            by_reason = {
                reason: research.metrics([row for row in rows if row["reason"] == reason])
                for reason in sorted({row["reason"] for row in rows})
            }
            phases = []
            for phase in range(5):
                phase_rows = []
                for name, (data, _, _) in loaded.items():
                    selected = research.select_confirmed(data, candidates[name], rules, spec, 5, phase)
                    phase_rows.extend({**row, "dataset": name} for row in selected)
                phases.append({"phase": phase, **research.metrics(phase_rows)})
            reports.append(
                {
                    "mode": mode,
                    "confirmation": spec.name,
                    "candidateSeconds": sum(len(value) for value in candidates.values()),
                    "overall": research.metrics(rows),
                    "byDataset": by_dataset,
                    "byReason": by_reason,
                    "entryShiftStress": research.timing_shift_metrics(rows, {k: v[0] for k, v in loaded.items()}, rules.horizon_sec),
                    "scan5sPhaseStress": phases,
                }
            )
            trade_rows.extend({**row, "research_case": f"{mode}_{spec.name}"} for row in rows)
    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "coverage": coverage,
        "reports": reports,
        "note": "Research only. No production code or configuration was changed.",
    }
    OUT_JSON.write_text(json.dumps(research.clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(research.clean(trade_rows)).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    print(json.dumps(research.clean(run()), ensure_ascii=False, indent=2))
