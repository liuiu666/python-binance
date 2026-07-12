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
from research_all_branch_router_strategy import metrics, payout


ROOT = Path(__file__).resolve().parents[1]
OUT_JSON = ROOT / "tmp" / "top_exhaustion_confirmation.json"
OUT_CSV = ROOT / "tmp" / "top_exhaustion_confirmation_trades.csv"

EXTRA_SOURCES = (
    (
        "extra_2026-07-08_day",
        ROOT / "tmp" / "latest_pull_20260708_204204" / "data" / "btcusdt_1s_trades.csv",
        ROOT
        / "tmp"
        / "latest_live_pull_20260709_220453"
        / "data_clean"
        / "btcusdt_orderbook_1s.csv",
    ),
)

EXTRA_TRAIN_EXCLUDES = {
    "extra_2026-07-08_day": {"2026-07-08_09"},
}


def finite(value: Any) -> float:
    try:
        number = float(value)
    except Exception:
        return float("nan")
    return number if math.isfinite(number) else float("nan")


def add_top_micro_features(data: pd.DataFrame, snapshots: pd.DataFrame) -> pd.DataFrame:
    out = snapshots.copy()
    close = data["close"].astype(float)
    buy = data["buy_qty"].astype(float).clip(lower=0.0)
    sell = data["sell_qty"].astype(float).clip(lower=0.0)
    bid20 = data["bid_qty_20"].astype(float)
    ask20 = data["ask_qty_20"].astype(float)
    imb20 = data["imbalance_20"].astype(float)

    rows: list[dict[str, float]] = []
    for timestamp in pd.to_datetime(out["time"], utc=True):
        target = timestamp + pd.Timedelta(seconds=59)
        idx = int(data.index.searchsorted(target, side="right") - 1)
        if idx < 120 or idx >= len(data) - 600 or abs((data.index[idx] - target).total_seconds()) > 3:
            rows.append(
                {
                    "ret15_bps": np.nan,
                    "ret30_bps_sec": np.nan,
                    "prev30_bps_sec": np.nan,
                    "decel30_bps": np.nan,
                    "flow30_now": np.nan,
                    "flow30_prev": np.nan,
                    "flow30_delta": np.nan,
                    "imb20_chg60": np.nan,
                    "ask20_chg60": np.nan,
                    "bid_ask20_ratio": np.nan,
                }
            )
            continue

        buy30 = float(buy.iloc[idx - 29 : idx + 1].sum())
        sell30 = float(sell.iloc[idx - 29 : idx + 1].sum())
        prev_buy30 = float(buy.iloc[idx - 59 : idx - 29].sum())
        prev_sell30 = float(sell.iloc[idx - 59 : idx - 29].sum())
        flow30 = (buy30 - sell30) / (buy30 + sell30) if buy30 + sell30 > 0 else np.nan
        prev_flow30 = (prev_buy30 - prev_sell30) / (prev_buy30 + prev_sell30) if prev_buy30 + prev_sell30 > 0 else np.nan

        previous_ask = float(ask20.iloc[idx - 60])
        rows.append(
            {
                "ret15_bps": float((close.iloc[idx] / close.iloc[idx - 15] - 1.0) * 10000.0),
                "ret30_bps_sec": float((close.iloc[idx] / close.iloc[idx - 30] - 1.0) * 10000.0),
                "prev30_bps_sec": float((close.iloc[idx - 30] / close.iloc[idx - 60] - 1.0) * 10000.0),
                "decel30_bps": float(
                    (close.iloc[idx] / close.iloc[idx - 30] - close.iloc[idx - 30] / close.iloc[idx - 60])
                    * 10000.0
                ),
                "flow30_now": float(flow30),
                "flow30_prev": float(prev_flow30),
                "flow30_delta": float(flow30 - prev_flow30) if math.isfinite(flow30) and math.isfinite(prev_flow30) else np.nan,
                "imb20_chg60": float(imb20.iloc[idx] - imb20.iloc[idx - 60]),
                "ask20_chg60": float(ask20.iloc[idx] / previous_ask - 1.0) if previous_ask > 0 else np.nan,
                "bid_ask20_ratio": float(bid20.iloc[idx] / ask20.iloc[idx]) if ask20.iloc[idx] > 0 else np.nan,
            }
        )
    return pd.concat([out.reset_index(drop=True), pd.DataFrame(rows)], axis=1)


def build_source_snapshots(source_name: str, seconds: Path, orderbook: Path) -> tuple[pd.DataFrame, float]:
    data = matrix.load_local_data(seconds, orderbook)
    hours = (data.index.max() - data.index.min()).total_seconds() / 3600.0
    snapshots = matrix.build_minute_snapshots(data, source_name)
    snapshots["time"] = pd.to_datetime(snapshots["time"], utc=True)
    snapshots = precursor.add_second_micro_features(data, snapshots)
    snapshots = add_top_micro_features(data, snapshots)
    return snapshots, hours


def is_up_sprint_short(row: dict[str, Any], signal: str) -> bool:
    return (
        signal == "DOWN"
        and row.get("trend") == "trend_up"
        and row.get("normal_pos") == "above_upper"
        and row.get("sprint") == "up_sprint"
    )


def top_exhaustion_score(row: dict[str, Any]) -> tuple[int, dict[str, bool]]:
    checks = {
        "速度降温": finite(row.get("decel30_bps")) <= -1.0 or finite(row.get("ret15_bps")) <= 1.0,
        "买盘降温": finite(row.get("flow30_now")) <= 0.12 or finite(row.get("flow30_delta")) <= -0.12,
        "盘口不再买强": finite(row.get("imb20_now")) <= 0.05 or finite(row.get("imb20_chg60")) <= -0.08,
        "卖压/上方流动性增强": finite(row.get("ask20_chg60")) >= 0.15 or finite(row.get("bid_ask20_ratio")) <= 0.9,
    }
    return sum(1 for ok in checks.values() if ok), checks


def confirm_with_top(row: dict[str, Any], signal: str, variant: str) -> tuple[bool, str]:
    ok, reason = precursor.confirm_signal(row, signal, "both_confirm")
    if not ok:
        return False, reason
    if not is_up_sprint_short(row, signal):
        return ok, reason

    score, checks = top_exhaustion_score(row)
    if variant == "top_soft":
        threshold = 1
    elif variant == "top_mid":
        threshold = 2
    elif variant == "top_strict":
        threshold = 3
    else:
        return ok, reason

    if score < threshold:
        return False, f"skip_top_not_confirmed_{score}of4"
    labels = ",".join(name for name, passed in checks.items() if passed)
    return True, f"top_exhaustion_{score}of4:{labels}"


def vote_signal(row: dict[str, Any], compiled: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any]]:
    signal, info = vote.vote_for(row, compiled)
    total_votes = int(info["upVotes"]) + int(info["downVotes"])
    if signal is None or total_votes < 2:
        return None, info
    return signal, info


def apply_strategy(test: pd.DataFrame, compiled: list[dict[str, Any]], variant: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    last_time: pd.Timestamp | None = None
    for row in test.sort_values("time").to_dict("records"):
        signal, vote_info = vote_signal(row, compiled)
        if signal is None:
            continue
        ok, reason = confirm_with_top(row, signal, variant)
        if not ok:
            continue
        timestamp = pd.Timestamp(row["time"])
        if last_time is not None and (timestamp - last_time).total_seconds() < 600:
            continue
        future = float(row["future10_bps"])
        won = future > 0 if signal == "UP" else future < 0
        record = dict(row)
        record["signal"] = signal
        record["won"] = bool(won)
        record["pnl"] = payout(won)
        record["reason"] = reason
        record["upVotes"] = int(vote_info["upVotes"])
        record["downVotes"] = int(vote_info["downVotes"])
        record["topScore"] = top_exhaustion_score(row)[0] if is_up_sprint_short(row, signal) else np.nan
        rows.append(record)
        last_time = timestamp
    return pd.DataFrame(rows)


def run_folded(sources: tuple[tuple[str, Path, Path], ...], mode: str) -> tuple[list[dict[str, Any]], list[pd.DataFrame], dict[str, Any]]:
    frames: list[pd.DataFrame] = []
    hours: dict[str, float] = {}
    info: dict[str, Any] = {}
    for source_name, seconds, orderbook in sources:
        frame, duration = build_source_snapshots(source_name, seconds, orderbook)
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
    variants = ("both_confirm", "top_soft", "top_mid", "top_strict")
    reports: list[dict[str, Any]] = []
    all_trades: list[pd.DataFrame] = []
    source_names = sorted(snapshots["source"].unique())
    for variant in variants:
        fold_reports = []
        variant_trades = []
        for test_source in source_names:
            if mode == "leave_one_out":
                train_names = set(source_names) - {test_source}
            else:
                train_names = set(source_names) - EXTRA_TRAIN_EXCLUDES.get(test_source, set()) - {test_source}
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
    return reports, all_trades, info


def run() -> dict[str, Any]:
    base_reports, base_trades, base_info = run_folded(tuple(matrix.SOURCES), "leave_one_out")
    extra_reports, extra_trades, extra_info = run_folded(tuple(matrix.SOURCES) + EXTRA_SOURCES, "extra")

    all_trades = base_trades + extra_trades
    if all_trades:
        pd.concat(all_trades, ignore_index=True).to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    output = {
        "method": "Top exhaustion confirmation on top of both_confirm. Features use only data available at signal time.",
        "baseSources": base_info,
        "baseLeaveOneOutReports": base_reports,
        "extraSources": extra_info,
        "extraReportsIncludingOriginalAndExtra": extra_reports,
        "csv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(precursor.clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    print(json.dumps(precursor.clean(run()), ensure_ascii=False, indent=2))
