from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

import research_all_branch_matrix as matrix
import research_all_branch_vote_router as vote
import research_precursor_confirmed_vote_strategy as precursor
from research_all_branch_router_strategy import metrics


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "tmp" / "precursor_confirmed_vote_strategy_extra.json"
OUT_CSV = ROOT / "tmp" / "precursor_confirmed_vote_strategy_extra_trades.csv"


EXTRA_SOURCES = (
    (
        "extra_2026-07-07_08",
        ROOT / "tmp" / "latest_pull_20260708_204204" / "data" / "btcusdt_1s_trades.csv",
        ROOT
        / "tmp"
        / "latest_live_pull_20260709_220453"
        / "data_clean"
        / "btcusdt_orderbook_1s.csv",
    ),
)

EXTRA_TRAIN_EXCLUDES = {
    # The usable aligned part of this pull is 2026-07-08 01:01-12:33 UTC,
    # which overlaps the original 2026-07-08_09 source. Exclude that source
    # from training to keep this as an out-of-sample check.
    "extra_2026-07-07_08": {"2026-07-08_09"},
}


def source_label(source: tuple[str, Path, Path]) -> dict[str, Any]:
    name, seconds, orderbook = source
    return {
        "name": name,
        "seconds": str(seconds),
        "orderbook": str(orderbook),
    }


def build_frames(sources: tuple[tuple[str, Path, Path], ...]) -> tuple[list[pd.DataFrame], dict[str, float], dict[str, Any]]:
    frames: list[pd.DataFrame] = []
    hours: dict[str, float] = {}
    info: dict[str, Any] = {}
    for source_name, seconds, orderbook in sources:
        frame, duration_hours = precursor.build_source_snapshots(source_name, seconds, orderbook)
        frames.append(frame)
        hours[source_name] = duration_hours
        if not frame.empty:
            start = pd.to_datetime(frame["time"], utc=True).min()
            end = pd.to_datetime(frame["time"], utc=True).max()
        else:
            start = None
            end = None
        info[source_name] = {
            "seconds": str(seconds),
            "orderbook": str(orderbook),
            "hours": round(duration_hours, 4),
            "usableMinuteSnapshots": int(len(frame)),
            "start": start,
            "end": end,
        }
    return frames, hours, info


def run() -> dict[str, Any]:
    base_sources = tuple(matrix.SOURCES)
    base_frames, base_hours, base_info = build_frames(base_sources)
    extra_frames, extra_hours, extra_info = build_frames(EXTRA_SOURCES)

    all_snapshots = precursor.add_lag_features(pd.concat(base_frames + extra_frames, ignore_index=True))
    base_names = {name for name, _, _ in base_sources}
    extra_names = {name for name, _, _ in EXTRA_SOURCES}
    train = all_snapshots[all_snapshots["source"].isin(base_names)].copy()
    compiled = vote.compile_rules(train, "balanced")

    variants = ("base", "up_confirm", "down_confirm", "both_confirm")
    reports = []
    all_trades = []
    for variant in variants:
        variant_trades = []
        folds = []
        for source_name in sorted(extra_names):
            train_names = base_names - EXTRA_TRAIN_EXCLUDES.get(source_name, set())
            train = all_snapshots[all_snapshots["source"].isin(train_names)].copy()
            compiled = vote.compile_rules(train, "balanced")
            test = all_snapshots[all_snapshots["source"] == source_name].copy()
            trades = precursor.apply_strategy(test, compiled, variant)
            if not trades.empty:
                trades["variant"] = variant
                trades["testSource"] = source_name
                variant_trades.append(trades)
                all_trades.append(trades)
            folds.append(
                {
                    "testSource": source_name,
                    "trainSources": sorted(train_names),
                    "result": metrics(trades, extra_hours[source_name]),
                    "byReason": {
                        str(reason): metrics(group, extra_hours[source_name])
                        for reason, group in trades.groupby("reason")
                    }
                    if not trades.empty
                    else {},
                }
            )
        combined = pd.concat(variant_trades, ignore_index=True) if variant_trades else pd.DataFrame()
        reports.append(
            {
                "variant": variant,
                "total": metrics(combined, sum(extra_hours.values())),
                "folds": folds,
                "byReason": {
                    str(reason): metrics(group, sum(extra_hours.values()))
                    for reason, group in combined.groupby("reason")
                }
                if not combined.empty
                else {},
            }
        )

    if all_trades:
        pd.concat(all_trades, ignore_index=True).to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    output = {
        "method": (
            "Train balanced vote rules on the original three sources only, then test precursor "
            "confirmation variants on extra local sources. This avoids fitting the added period."
        ),
        "baseSources": base_info,
        "extraSources": extra_info,
        "reports": reports,
        "best": sorted(
            reports,
            key=lambda item: (
                item["total"]["pnlU"],
                item["total"]["winRate"],
                -item["total"]["maxLoss"],
                -item["total"]["maxDrawdownU"],
            ),
            reverse=True,
        ),
        "csv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(precursor.clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    print(json.dumps(precursor.clean(run()), ensure_ascii=False, indent=2))
