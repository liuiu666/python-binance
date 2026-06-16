from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from second_backtest.data import audit_second_csv, load_second_bars
from second_backtest.research import build_research_configs, run_research_scan


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / "tmp" / "latest_1s_pull_20260616_224315" / "btcusdt_1s_trades.csv"
DEFAULT_OUT = ROOT / "tmp" / "second_algorithm_research_latest.json"


def build_report(args: argparse.Namespace) -> dict:
    bars = load_second_bars(args.csv)
    configs = build_research_configs()
    if args.limit:
        configs = configs[: args.limit]
    reports = run_research_scan(bars, configs, global_lock_sec=args.global_lock_sec)
    top = reports[: args.top]
    return {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "source": str(Path(args.csv).resolve()),
        "method": {
            "data": "Only 1-second BTC bars are used. Missing seconds carry forward close and set volume to zero.",
            "causal": "Every signal uses data at or before the signal second and settles 600 seconds later.",
            "selection": "Ranking uses split metrics, recent 24h behavior, trade count, loss streak, and variance across thirds.",
            "warning": "This is research output, not a live configuration change.",
        },
        "dataQuality": audit_second_csv(args.csv),
        "sample": {
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "hours": round((bars.index.max() - bars.index.min()).total_seconds() / 3600.0, 2),
            "rows": int(len(bars)),
            "observedRows": int(bars["observed"].sum()),
            "filledRows": int((~bars["observed"]).sum()),
        },
        "configCount": len(configs),
        "top": top,
        "all": reports,
        "families": summarize_families(reports),
    }


def summarize_families(reports: list[dict]) -> list[dict]:
    out = []
    for family in sorted({row["family"] for row in reports}):
        items = [row for row in reports if row["family"] == family]
        usable = [row for row in items if row["stability"]["usable"]]
        best = items[0]
        best_usable = usable[0] if usable else None
        out.append(
            {
                "family": family,
                "configs": len(items),
                "usableConfigs": len(usable),
                "best": compact_rank_row(best),
                "bestUsable": compact_rank_row(best_usable) if best_usable else None,
            }
        )
    out.sort(key=lambda row: row["best"]["score"], reverse=True)
    return out


def compact_rank_row(row: dict) -> dict:
    metrics = row["execution"]["metrics"]["all"]
    last = row["execution"]["metrics"]["last24h"]
    return {
        "strategyId": row["strategyId"],
        "label": row["label"],
        "score": row["score"],
        "winRate": metrics["winRate"],
        "trades": metrics["trades"],
        "tradesPerDay": metrics["tradesPerDay"],
        "last24WinRate": last["winRate"],
        "last24Trades": last["trades"],
        "maxLoss": metrics["maxLoss"],
        "warnings": row["stability"]["warnings"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Research 1-second BTC algorithms.")
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="Path to 1-second BTC CSV")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Output JSON report")
    parser.add_argument("--top", type=int, default=20, help="Number of top rows duplicated in top[]")
    parser.add_argument("--limit", type=int, default=0, help="Debug only: run first N configs")
    parser.add_argument(
        "--global-lock-sec",
        type=int,
        default=0,
        help="Research-only global lock. Leave 0 to match independent strategy execution.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = build_report(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
