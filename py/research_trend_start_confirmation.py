from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import research_all_branch_matrix as matrix
import research_all_branch_vote_router as vote
import research_precursor_confirmed_vote_strategy as precursor
import research_top_exhaustion_confirmation as top
from research_all_branch_router_strategy import metrics, payout


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "tmp" / "trend_start_confirmation.json"
OUT_CSV = ROOT / "tmp" / "trend_start_confirmation_trades.csv"


def f(value: Any) -> float:
    try:
        number = float(value)
    except Exception:
        return float("nan")
    return number if math.isfinite(number) else float("nan")


def is_up_sprint_short(row: dict[str, Any], signal: str) -> bool:
    return (
        signal == "DOWN"
        and row.get("trend") == "trend_up"
        and row.get("normal_pos") == "above_upper"
        and row.get("sprint") == "up_sprint"
    )


def trend_start_score(row: dict[str, Any]) -> tuple[int, dict[str, bool]]:
    checks = {
        "multi_period_up": f(row.get("ret10_bps")) >= 12.0
        and f(row.get("ret30_bps")) >= 18.0
        and f(row.get("ret60_bps")) >= 20.0,
        "short_accelerating": f(row.get("ret15_bps")) >= 1.0
        and f(row.get("ret30_bps_sec")) >= f(row.get("prev30_bps_sec")),
        "buy_flow_strong": f(row.get("flow30_now")) >= 0.35 or f(row.get("flow30_delta")) >= 0.15,
        "book_buy_strong": f(row.get("imb20_now")) >= 0.10 or f(row.get("bid_ask20_ratio")) >= 1.10,
        "not_low_volume": f(row.get("vol_ratio30")) >= 0.70,
        "not_from_mature_upper": row.get("lag10_normal_pos") not in {"above_upper", "upper_edge"},
    }
    return sum(1 for ok in checks.values() if ok), checks


def classify_start(score: int, variant: str) -> bool:
    if variant in {"startup_skip_3", "startup_follow_3"}:
        return score >= 3
    if variant in {"startup_skip_4", "startup_follow_4"}:
        return score >= 4
    return False


def vote_signal(row: dict[str, Any], compiled: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any]]:
    signal, info = vote.vote_for(row, compiled)
    total_votes = int(info["upVotes"]) + int(info["downVotes"])
    if signal is None or total_votes < 2:
        return None, info
    return signal, info


def decide_signal(row: dict[str, Any], raw_signal: str, variant: str) -> tuple[str | None, str, int]:
    ok, reason = precursor.confirm_signal(row, raw_signal, "both_confirm")
    if not ok:
        return None, reason, 0
    if not is_up_sprint_short(row, raw_signal):
        return raw_signal, reason, 0

    score, checks = trend_start_score(row)
    if not classify_start(score, variant):
        return raw_signal, f"{reason}|startup_score_{score}", score

    labels = ",".join(name for name, passed in checks.items() if passed)
    if variant.startswith("startup_skip"):
        return None, f"skip_trend_start_{score}of6:{labels}", score
    if variant.startswith("startup_follow"):
        return "UP", f"follow_trend_start_{score}of6:{labels}", score
    return raw_signal, f"{reason}|startup_score_{score}", score


def apply_strategy(test: pd.DataFrame, compiled: list[dict[str, Any]], variant: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    last_time: pd.Timestamp | None = None
    for row in test.sort_values("time").to_dict("records"):
        raw_signal, vote_info = vote_signal(row, compiled)
        if raw_signal is None:
            continue
        signal, reason, score = decide_signal(row, raw_signal, variant)
        if signal is None:
            continue
        timestamp = pd.Timestamp(row["time"])
        if last_time is not None and (timestamp - last_time).total_seconds() < 600:
            continue
        future = float(row["future10_bps"])
        won = future > 0 if signal == "UP" else future < 0
        record = dict(row)
        record["rawSignal"] = raw_signal
        record["signal"] = signal
        record["won"] = bool(won)
        record["pnl"] = payout(won)
        record["reason"] = reason
        record["startupScore"] = score if is_up_sprint_short(row, raw_signal) else np.nan
        record["upVotes"] = int(vote_info["upVotes"])
        record["downVotes"] = int(vote_info["downVotes"])
        rows.append(record)
        last_time = timestamp
    return pd.DataFrame(rows)


def build_sources(sources: tuple[tuple[str, Path, Path], ...]) -> tuple[pd.DataFrame, dict[str, float], dict[str, Any]]:
    frames = []
    hours: dict[str, float] = {}
    info: dict[str, Any] = {}
    for source_name, seconds, orderbook in sources:
        frame, duration = top.build_source_snapshots(source_name, seconds, orderbook)
        frames.append(frame)
        hours[source_name] = duration
        info[source_name] = {
            "seconds": str(seconds),
            "orderbook": str(orderbook),
            "hours": round(duration, 4),
            "usableMinuteSnapshots": int(len(frame)),
            "start": pd.to_datetime(frame["time"], utc=True).min() if not frame.empty else None,
            "end": pd.to_datetime(frame["time"], utc=True).max() if not frame.empty else None,
        }
    snapshots = precursor.add_lag_features(pd.concat(frames, ignore_index=True))
    return snapshots, hours, info


def run_folded(sources: tuple[tuple[str, Path, Path], ...], mode: str) -> tuple[list[dict[str, Any]], list[pd.DataFrame], dict[str, Any]]:
    snapshots, hours, source_info = build_sources(sources)
    source_names = sorted(snapshots["source"].unique())
    extra_excludes = getattr(top, "EXTRA_TRAIN_EXCLUDES", {})
    variants = (
        "both_confirm",
        "startup_skip_3",
        "startup_skip_4",
        "startup_follow_3",
        "startup_follow_4",
    )
    reports = []
    all_trades = []
    for variant in variants:
        fold_reports = []
        variant_trades = []
        for test_source in source_names:
            if mode == "leave_one_out":
                train_names = set(source_names) - {test_source}
            else:
                train_names = set(source_names) - set(extra_excludes.get(test_source, set())) - {test_source}
            train = snapshots[snapshots["source"].isin(train_names)].copy()
            test = snapshots[snapshots["source"] == test_source].copy()
            compiled = vote.compile_rules(train, "balanced")
            trades = apply_strategy(test, compiled, variant)
            if not trades.empty:
                trades["variant"] = variant
                trades["testSource"] = test_source
                trades["runMode"] = mode
                variant_trades.append(trades)
                all_trades.append(trades)
            fold_reports.append(
                {
                    "testSource": str(test_source),
                    "trainSources": sorted(str(name) for name in train_names),
                    "result": metrics(trades, hours[str(test_source)]),
                    "byReason": {
                        str(reason): metrics(group, hours[str(test_source)])
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
                "total": metrics(combined, sum(hours.values())),
                "folds": fold_reports,
                "byReason": {
                    str(reason): metrics(group, sum(hours.values()))
                    for reason, group in combined.groupby("reason")
                }
                if not combined.empty
                else {},
            }
        )
    return reports, all_trades, source_info


def startup_diagnostics(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty or "startupScore" not in trades.columns:
        return {}
    subset = trades[(trades["variant"] == "both_confirm") & trades["startupScore"].notna()].copy()
    if subset.empty:
        return {}
    rows = {}
    for score, group in subset.groupby("startupScore"):
        rows[str(int(score))] = metrics(group, 24.0)
        rows[str(int(score))]["avgFuture10Bps"] = round(float(group["future10_bps"].mean()), 4)
        rows[str(int(score))]["medianFuture10Bps"] = round(float(group["future10_bps"].median()), 4)
    return rows


def run() -> dict[str, Any]:
    base_reports, base_trades, base_info = run_folded(tuple(matrix.SOURCES), "leave_one_out")
    extra_sources = tuple(matrix.SOURCES) + tuple(top.EXTRA_SOURCES)
    extra_reports, extra_trades, extra_info = run_folded(extra_sources, "extra")
    all_trades = base_trades + extra_trades
    trades_df = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    if not trades_df.empty:
        trades_df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    output = {
        "method": "Trend-start detection on top of both_confirm. Startup handling is tested as skip vs follow, using signal-time data only.",
        "startupChecks": [
            "multi_period_up: ret10>=12bp and ret30>=18bp and ret60>=20bp",
            "short_accelerating: ret15>=1bp and last30s>=previous30s",
            "buy_flow_strong: flow30_now>=0.35 or flow30_delta>=0.15",
            "book_buy_strong: imb20_now>=0.10 or bid/ask20>=1.10",
            "not_low_volume: vol_ratio30>=0.70",
            "not_from_mature_upper: lag10 position is not above_upper/upper_edge",
        ],
        "baseSources": base_info,
        "baseLeaveOneOutReports": base_reports,
        "extraSources": extra_info,
        "extraReportsIncludingOriginalAndExtra": extra_reports,
        "startupScoreDiagnostics": startup_diagnostics(trades_df),
        "csv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(precursor.clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    print(json.dumps(precursor.clean(run()), ensure_ascii=False, indent=2))
