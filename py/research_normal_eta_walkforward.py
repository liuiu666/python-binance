from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from research_arrival_forecast import DEFAULT_OLD_CSV, DEFAULT_PROD_CONFIG, DEFAULT_SHARD_DIR, load_bars
from research_normal_eta import run_grid


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "tmp" / "normal_eta_walkforward_latest.json"


def metric_from_days(days: list[dict]) -> dict:
    trades = sum(day["trades"] for day in days)
    wins = sum(round(day["trades"] * day["winRate"] / 100) for day in days)
    pnl = sum(day["pnlU_5u_80pct"] for day in days)
    max_loss = max((day["maxLoss"] for day in days), default=0)
    active_days = len(days)
    return {
        "days": active_days,
        "trades": int(trades),
        "winRate": round(wins / trades * 100, 2) if trades else 0.0,
        "pnlU_5u_80pct": int(pnl),
        "maxDailyLossStreak": int(max_loss),
        "tradesPerDay": round(trades / active_days, 2) if active_days else 0.0,
    }


def case_key(item: dict) -> tuple:
    return (
        int(item["lookbackSec"]),
        float(item["tailPct"]),
        float(item["targetBps"]),
        int(item["maxWaitSec"]),
        bool(item["downOnly"]),
    )


def select_candidate(train_report: dict, mode: str) -> dict | None:
    candidates = train_report["ranked"]
    if mode == "balanced":
        candidates = [
            item for item in candidates
            if item["eta_tradesPerDay"] >= 8 and item["eta_maxLoss"] <= 4 and item["eta_trades"] >= 20
        ]
        key = lambda item: (item["eta_pnlU_5u_80pct"], item["eta_winRate"], -item["eta_maxLoss"], item["eta_tradesPerDay"])
    elif mode == "stable":
        candidates = [
            item for item in candidates
            if item["eta_winRate"] >= 62 and item["eta_tradesPerDay"] >= 6 and item["eta_maxLoss"] <= 3 and item["eta_trades"] >= 20
        ]
        key = lambda item: (item["eta_winRate"], item["eta_pnlU_5u_80pct"], item["eta_tradesPerDay"])
    else:
        raise ValueError(mode)
    if not candidates:
        return None
    return sorted(candidates, key=key, reverse=True)[0]


def find_case(report: dict, selected: dict) -> dict | None:
    key = case_key(selected)
    for case in report["cases"]:
        if (
            int(case["lookbackSec"]),
            float(case["tailPct"]),
            float(case["targetBps"]),
            int(case["maxWaitSec"]),
            bool(case["downOnly"]),
        ) == key:
            return case
    return None


def build_report(args: argparse.Namespace) -> dict:
    bars = load_bars(Path(args.old_csv), Path(args.shard_dir))
    days = sorted({ts.date().isoformat() for ts in bars.index})
    folds = []
    for i in range(2, len(days)):
        train_days = days[:i]
        test_day = days[i]
        day_index = pd.Index([ts.date().isoformat() for ts in bars.index])
        train = bars[day_index.isin(train_days)]
        test = bars[day_index == test_day]
        if len(train) < 7200 + 600 or len(test) < 7200 + 600:
            continue
        train_report = run_grid(train)
        test_report = run_grid(test)
        fold = {
            "trainDays": train_days,
            "testDay": test_day,
            "trainHours": round((train.index.max() - train.index.min()).total_seconds() / 3600.0, 2),
            "testHours": round((test.index.max() - test.index.min()).total_seconds() / 3600.0, 2),
        }
        for mode in ("balanced", "stable"):
            selected = select_candidate(train_report, mode)
            if selected is None:
                fold[mode] = {"selected": None, "test": None}
                continue
            test_case = find_case(test_report, selected)
            fold[mode] = {
                "selected": {
                    "lookbackSec": selected["lookbackSec"],
                    "tailPct": selected["tailPct"],
                    "targetBps": selected["targetBps"],
                    "maxWaitSec": selected["maxWaitSec"],
                    "downOnly": selected["downOnly"],
                    "trainEta": {
                        "trades": selected["eta_trades"],
                        "winRate": selected["eta_winRate"],
                        "pnlU_5u_80pct": selected["eta_pnlU_5u_80pct"],
                        "maxLoss": selected["eta_maxLoss"],
                        "tradesPerDay": selected["eta_tradesPerDay"],
                    },
                },
                "test": test_case["eta"] if test_case else None,
                "testBySide": test_case["etaBySide"] if test_case else None,
            }
        folds.append(fold)
    summary = {}
    for mode in ("balanced", "stable"):
        tests = [fold[mode]["test"] for fold in folds if fold[mode].get("test")]
        summary[mode] = metric_from_days(tests)
        summary[mode]["foldsWithTrades"] = sum(1 for item in tests if item["trades"] > 0)
    return {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "sample": {
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "hours": round((bars.index.max() - bars.index.min()).total_seconds() / 3600.0, 2),
            "rows": int(len(bars)),
            "observedPct": round(float(bars["observed"].mean() * 100), 2),
            "days": days,
        },
        "method": {
            "train": "For each fold, select params using only earlier days.",
            "test": "Apply selected params to the next day only.",
            "grid": "normal distribution signals only plus ETA; no chip/trend signals.",
        },
        "summary": summary,
        "folds": folds,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--old-csv", default=str(DEFAULT_OLD_CSV))
    p.add_argument("--shard-dir", default=str(DEFAULT_SHARD_DIR))
    p.add_argument("--prod-config", default=str(DEFAULT_PROD_CONFIG))
    p.add_argument("--out", default=str(DEFAULT_OUT))
    return p.parse_args()


def main() -> None:
    args = parse_args()
    report = build_report(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report["sample"], ensure_ascii=False))
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    for fold in report["folds"]:
        print(json.dumps(fold, ensure_ascii=False))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
