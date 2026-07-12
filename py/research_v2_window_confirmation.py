"""Compare normal-distribution windows under the same persistent confirmation rule."""

from __future__ import annotations

import json
import argparse
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_v2_persistent_reclaim as research  # noqa: E402


OUT_JSON = ROOT / "tmp" / "v2_window_confirmation_research.json"
OUT_TRADES = ROOT / "tmp" / "v2_window_confirmation_trades.csv"
WINDOWS = (600, 900, 1200)
CONFIRM = research.ConfirmSpec("repeat_3of30_span8", 30, 3, 8)


def load_sources(rules):
    loaded = {}
    cache = {}
    for name, source in research.DATASETS.items():
        key = (str(source["seconds"]), str(source["orderbook"]))
        if key not in cache:
            cache[key] = research.load_market(Path(source["seconds"]), Path(source["orderbook"]))
        start = pd.Timestamp(source["start"])
        end = pd.Timestamp(source["end"])
        full = cache[key]
        data = full[
            (full.index >= start - pd.Timedelta(seconds=4300))
            & (full.index < end + pd.Timedelta(seconds=rules.horizon_sec + 5))
        ].copy()
        loaded[name] = (data, start, end)
    return loaded


def run(windows=WINDOWS):
    base_rules = research.load_rules()
    loaded = load_sources(base_rules)
    reports = []
    all_trades = []
    for window in windows:
        rules = replace(base_rules, normal_window_sec=window)
        rows = []
        candidate_count = 0
        for name, (data, start, end) in loaded.items():
            _, candidates = research.candidate_stream(data, rules)
            candidates = [row for row in candidates if start <= row["time"] < end]
            candidate_count += len(candidates)
            selected = research.select_confirmed(data, candidates, rules, CONFIRM)
            rows.extend({**row, "dataset": name, "normal_window_sec": window} for row in selected)
        by_dataset = {
            name: research.metrics([row for row in rows if row["dataset"] == name])
            for name in research.DATASETS
        }
        shifts = research.timing_shift_metrics(rows, {key: value[0] for key, value in loaded.items()}, rules.horizon_sec)
        reports.append(
            {
                "normalWindowSec": window,
                "candidateSeconds": candidate_count,
                "overall": research.metrics(rows),
                "byDataset": by_dataset,
                "entryShiftStress": shifts,
            }
        )
        all_trades.extend(rows)
    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "confirmation": {
            "windowSec": CONFIRM.window_sec,
            "minHits": CONFIRM.min_hits,
            "minSpanSec": CONFIRM.min_span_sec,
        },
        "reports": reports,
        "note": "Research only. No production code or configuration was changed.",
    }
    OUT_JSON.write_text(json.dumps(research.clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(research.clean(all_trades)).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=int, action="append")
    args = parser.parse_args()
    print(json.dumps(research.clean(run(tuple(args.window) if args.window else WINDOWS)), ensure_ascii=False, indent=2))
