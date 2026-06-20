from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from research_arrival_forecast import DEFAULT_OLD_CSV, DEFAULT_PROD_CONFIG, DEFAULT_SHARD_DIR, load_bars
from research_normal_eta import (
    SecondNormalConfig,
    day_metrics,
    direct_rows,
    eta_rows,
    metrics,
)
from second_backtest.execution import execute_signals
from second_backtest.strategies import generate_normal_signals


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "tmp" / "normal_eta_walkforward_fast.json"


CANDIDATES = [
    {"lookback": 2700, "tail": 0.27, "target": 2.0, "wait": 45, "downOnly": False},
    {"lookback": 2700, "tail": 0.27, "target": 3.0, "wait": 45, "downOnly": False},
    {"lookback": 3600, "tail": 0.27, "target": 1.0, "wait": 20, "downOnly": False},
    {"lookback": 2700, "tail": 0.20, "target": 1.0, "wait": 20, "downOnly": False},
    {"lookback": 2700, "tail": 0.20, "target": 2.0, "wait": 45, "downOnly": False},
    {"lookback": 4200, "tail": 0.20, "target": 2.0, "wait": 45, "downOnly": False},
    {"lookback": 1800, "tail": 0.20, "target": 1.0, "wait": 20, "downOnly": True},
    {"lookback": 1800, "tail": 0.20, "target": 3.0, "wait": 45, "downOnly": False},
]


def run_candidate(bars: pd.DataFrame, candidate: dict) -> dict:
    cfg = SecondNormalConfig(
        strategy_id=f"NORMAL_{candidate['lookback']}_{int(candidate['tail'] * 100)}",
        lookback_sec=int(candidate["lookback"]),
        horizon_sec=600,
        signal_gap_sec=600,
        tail_pct=float(candidate["tail"]),
        second_filter="none",
        amount=5,
        label="normal_eta_walkforward_fast",
    )
    raw = generate_normal_signals(bars, cfg, apply_config_gap=True)
    signals, _ = execute_signals(raw, per_strategy_lock=True, cooldown_sec=600, use_horizon_as_lock=True)
    rows, forecast = eta_rows(
        signals,
        bars,
        target_bps=float(candidate["target"]),
        max_wait_sec=int(candidate["wait"]),
        down_only=bool(candidate["downOnly"]),
    )
    direct = direct_rows(signals, bars)
    start, end = bars.index.min(), bars.index.max()
    return {
        "candidate": candidate,
        "rawSignals": len(raw),
        "executableSignals": len(signals),
        "direct": metrics(direct, start, end),
        "eta": metrics(rows, start, end),
        "forecast": forecast,
        "etaByDay": day_metrics(rows),
    }


def pick(train_results: list[dict], mode: str) -> dict | None:
    if mode == "balanced":
        pool = [
            r for r in train_results
            if r["eta"]["trades"] >= 15 and r["eta"]["tradesPerDay"] >= 6 and r["eta"]["maxLoss"] <= 4
        ]
        key = lambda r: (r["eta"]["pnlU_5u_80pct"], r["eta"]["winRate"], -r["eta"]["maxLoss"])
    elif mode == "stable":
        pool = [
            r for r in train_results
            if r["eta"]["trades"] >= 15 and r["eta"]["winRate"] >= 62 and r["eta"]["maxLoss"] <= 3
        ]
        key = lambda r: (r["eta"]["winRate"], r["eta"]["pnlU_5u_80pct"], r["eta"]["tradesPerDay"])
    else:
        raise ValueError(mode)
    if not pool:
        return None
    return sorted(pool, key=key, reverse=True)[0]


def aggregate(rows: list[dict]) -> dict:
    trades = sum(r["trades"] for r in rows)
    pnl = sum(r["pnlU_5u_80pct"] for r in rows)
    wins = sum(round(r["trades"] * r["winRate"] / 100) for r in rows)
    return {
        "days": len(rows),
        "trades": int(trades),
        "winRate": round(wins / trades * 100, 2) if trades else 0,
        "pnlU_5u_80pct": int(pnl),
        "maxDailyLossStreak": max((r["maxLoss"] for r in rows), default=0),
        "tradesPerDay": round(trades / len(rows), 2) if rows else 0,
    }


def build_report(args: argparse.Namespace) -> dict:
    bars = load_bars(Path(args.old_csv), Path(args.shard_dir))
    day_index = pd.Index([ts.date().isoformat() for ts in bars.index])
    days = sorted(set(day_index))
    folds = []
    for i in range(2, len(days)):
        train_days = days[:i]
        test_day = days[i]
        train = bars[day_index.isin(train_days)]
        test = bars[day_index == test_day]
        if len(train) < 7800 or len(test) < 7800:
            continue
        train_results = [run_candidate(train, c) for c in CANDIDATES]
        test_results = [run_candidate(test, c) for c in CANDIDATES]
        fold = {"trainDays": train_days, "testDay": test_day}
        for mode in ("balanced", "stable"):
            selected = pick(train_results, mode)
            if not selected:
                fold[mode] = {"selected": None, "test": None}
                continue
            candidate = selected["candidate"]
            test_result = next(r for r in test_results if r["candidate"] == candidate)
            fold[mode] = {
                "selected": {
                    **candidate,
                    "trainEta": selected["eta"],
                    "trainForecast": selected["forecast"],
                },
                "test": test_result["eta"],
                "testForecast": test_result["forecast"],
                "testByDay": test_result["etaByDay"],
            }
        folds.append(fold)
    summary = {}
    for mode in ("balanced", "stable"):
        tests = [fold[mode]["test"] for fold in folds if fold[mode].get("test")]
        summary[mode] = aggregate(tests)
    return {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "sample": {
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "hours": round((bars.index.max() - bars.index.min()).total_seconds() / 3600, 2),
            "rows": int(len(bars)),
            "observedPct": round(float(bars["observed"].mean() * 100), 2),
            "days": days,
        },
        "method": "Candidate walk-forward: select only from pre-researched normal+ETA candidates using past days, test next day.",
        "candidates": CANDIDATES,
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
