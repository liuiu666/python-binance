from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from research_arrival_forecast import (
    DEFAULT_OLD_CSV,
    DEFAULT_PROD_CONFIG,
    DEFAULT_SHARD_DIR,
    first_hit,
    forecast_eta,
    load_bars,
    signal_side,
)
from research_smart_switch import build_features, current_signals, fixed_stable_policy


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "tmp" / "arrival_forecast_diagnostics_latest.json"


def streak_loss(rows: list[dict]) -> int:
    cur = 0
    out = 0
    for row in sorted(rows, key=lambda item: item["entry_time"]):
        if row["won"]:
            cur = 0
        else:
            cur += 1
            out = max(out, cur)
    return out


def metric(rows: list[dict], start: pd.Timestamp, end: pd.Timestamp) -> dict:
    n = len(rows)
    wins = sum(bool(row["won"]) for row in rows)
    pnl = sum(4 if row["won"] else -5 for row in rows)
    days = max((end - start).total_seconds() / 86400.0, 1e-12)
    return {
        "trades": n,
        "winRate": round(wins / n * 100, 2) if n else 0.0,
        "pnlU_5u_80pct": pnl,
        "maxLoss": streak_loss(rows),
        "tradesPerDay": round(n / days, 2),
        "avgDelaySec": round(float(np.mean([r["delay_sec"] for r in rows])), 2) if rows else 0.0,
    }


def group_metrics(rows: list[dict], key: str, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    out = {}
    for value in sorted({str(row.get(key)) for row in rows}):
        subset = [row for row in rows if str(row.get(key)) == value]
        out[value] = metric(subset, start, end)
    return out


def day_metrics(rows: list[dict]) -> dict:
    out = {}
    for day in sorted({row["entry_time"].date().isoformat() for row in rows}):
        subset = [row for row in rows if row["entry_time"].date().isoformat() == day]
        out[day] = metric(subset, min(r["entry_time"] for r in subset), max(r["entry_time"] for r in subset))
    return out


def build_rows(signals: list[dict], bars: pd.DataFrame, target_bps: float, max_wait_sec: int) -> dict:
    close = bars["close"].to_numpy(float)
    high = bars["high"].to_numpy(float)
    low = bars["low"].to_numpy(float)
    buy = bars["buy_qty"].to_numpy(float)
    sell = bars["sell_qty"].to_numpy(float)
    rows = []
    missed = []
    false_predicted = []
    not_predicted_but_hit = []
    for sig in signals:
        idx = int(sig["idx"])
        if idx + max_wait_sec + 600 >= len(close):
            continue
        side = signal_side(str(sig["signal"]))
        fc = forecast_eta(
            close,
            buy,
            sell,
            idx,
            side,
            target_bps,
            speed_window=30,
            accel_window=10,
            min_speed_bps=0.005,
        )
        if not fc.get("ok"):
            continue
        predicted = fc["eta_sec"] <= max_wait_sec
        hit_idx, entry = first_hit(high, low, close, idx, side, target_bps, max_wait_sec)
        if hit_idx is None:
            rec = {
                "signal_time": bars.index[idx],
                "strategy_id": sig.get("strategy_id"),
                "signal": sig.get("signal"),
                "predicted": predicted,
                "eta_sec": fc["eta_sec"],
                "speed": fc["speed_bps_sec"],
                "efficiency": fc["efficiency"],
                "flow_eff": fc["flow_eff"],
                "accel": fc["accel_bps_sec2"],
            }
            missed.append(rec)
            if predicted:
                false_predicted.append(rec)
            continue
        settle = close[hit_idx + 600]
        won = bool(settle > entry if sig["signal"] == "UP" else settle < entry)
        rec = {
            "signal_time": bars.index[idx],
            "entry_time": bars.index[hit_idx],
            "strategy_id": sig.get("strategy_id"),
            "origin": sig.get("origin") or ("smart" if sig.get("strategy_id") == "SMART_FIXED_DOWN" else "current"),
            "signal": sig.get("signal"),
            "predicted": predicted,
            "delay_sec": int(hit_idx - idx),
            "eta_sec": float(fc["eta_sec"]),
            "eta_error_sec": float(fc["eta_sec"] - (hit_idx - idx)),
            "entry": float(entry),
            "settle": float(settle),
            "won": won,
            "speed": fc["speed_bps_sec"],
            "efficiency": fc["efficiency"],
            "flow_eff": fc["flow_eff"],
            "accel": fc["accel_bps_sec2"],
        }
        rows.append(rec)
        if not predicted:
            not_predicted_but_hit.append(rec)
    return {
        "rows": rows,
        "predicted_rows": [row for row in rows if row["predicted"]],
        "missed": missed,
        "false_predicted": false_predicted,
        "not_predicted_but_hit": not_predicted_but_hit,
    }


def summarize_case(name: str, signals: list[dict], bars: pd.DataFrame, target_bps: float, max_wait_sec: int) -> dict:
    start, end = bars.index.min(), bars.index.max()
    built = build_rows(signals, bars, target_bps, max_wait_sec)
    rows = built["rows"]
    pred_rows = built["predicted_rows"]
    tested = len(signals)
    return {
        "name": name,
        "params": {"target_bps": target_bps, "max_wait_sec": max_wait_sec},
        "forecast": {
            "testedSignals": tested,
            "actualHitCount": len(rows),
            "actualHitRate": round(len(rows) / tested * 100, 2) if tested else 0.0,
            "predictedHitCount": len(pred_rows) + len(built["false_predicted"]),
            "predictedHitPrecision": round(len(pred_rows) / (len(pred_rows) + len(built["false_predicted"])) * 100, 2)
            if pred_rows or built["false_predicted"]
            else 0.0,
            "predictedHitRecall": round(len(pred_rows) / len(rows) * 100, 2) if rows else 0.0,
            "falsePredictedHitCount": len(built["false_predicted"]),
            "notPredictedButHitCount": len(built["not_predicted_but_hit"]),
        },
        "allHit": metric(rows, start, end),
        "predictedOnly": metric(pred_rows, start, end),
        "predictedOnlyByDay": day_metrics(pred_rows),
        "predictedOnlyBySignal": group_metrics(pred_rows, "signal", start, end),
        "predictedOnlyByStrategy": group_metrics(pred_rows, "strategy_id", start, end),
        "predictedOnlyByOrigin": group_metrics(pred_rows, "origin", start, end),
        "notPredictedButHit": metric(built["not_predicted_but_hit"], start, end),
        "falsePredictedExamples": [
            {
                **row,
                "signal_time": row["signal_time"].isoformat(),
            }
            for row in built["false_predicted"][:30]
        ],
        "losingPredictedExamples": [
            {
                **row,
                "signal_time": row["signal_time"].isoformat(),
                "entry_time": row["entry_time"].isoformat(),
            }
            for row in pred_rows
            if not row["won"]
        ][:30],
    }


def build_report(args: argparse.Namespace) -> dict:
    bars = load_bars(Path(args.old_csv), Path(args.shard_dir))
    current = current_signals(bars, Path(args.prod_config))
    smart = fixed_stable_policy(bars, current, build_features(bars))
    trend_only = [row for row in smart if row.get("strategy_id") == "SMART_FIXED_DOWN"]
    cases = []
    for name, signals in (("smart_fixed_all", smart), ("smart_fixed_down_only", trend_only), ("current_online", current)):
        for target_bps, max_wait_sec in ((1.0, 20), (1.0, 45), (1.0, 90), (2.0, 45), (3.0, 45)):
            cases.append(summarize_case(name, signals, bars, target_bps, max_wait_sec))
    ranked = sorted(
        (
            {
                "name": c["name"],
                **c["params"],
                **c["forecast"],
                **{f"pred_{k}": v for k, v in c["predictedOnly"].items()},
            }
            for c in cases
        ),
        key=lambda item: (item["pred_pnlU_5u_80pct"], item["pred_winRate"], item["pred_trades"]),
        reverse=True,
    )
    return {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "sample": {
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "hours": round((bars.index.max() - bars.index.min()).total_seconds() / 3600.0, 2),
            "rows": int(len(bars)),
            "observedPct": round(float(bars["observed"].mean() * 100), 2),
        },
        "rankedExecutable": ranked,
        "cases": cases,
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
    for row in report["rankedExecutable"][:15]:
        print(json.dumps(row, ensure_ascii=False))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
