from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_all_branch_matrix as matrix  # noqa: E402
import research_all_branch_vote_router as vote  # noqa: E402
from research_all_branch_router_strategy import metrics, payout  # noqa: E402


OUT_JSON = ROOT / "tmp" / "precursor_confirmed_vote_strategy.json"
OUT_CSV = ROOT / "tmp" / "precursor_confirmed_vote_strategy_trades.csv"


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def add_lag_features(snapshots: pd.DataFrame) -> pd.DataFrame:
    out = snapshots.sort_values(["source", "time"]).copy()
    lag_cols = [
        "normal_pos",
        "ret10_bps",
        "z",
        "sigma_expand",
        "vol_ratio30",
    ]
    for lag in (3, 5, 10):
        for col in lag_cols:
            out[f"lag{lag}_{col}"] = out.groupby("source")[col].shift(lag)
    return out


def add_second_micro_features(data: pd.DataFrame, snapshots: pd.DataFrame) -> pd.DataFrame:
    out = snapshots.copy()
    close = data["close"].astype(float)
    bid20 = data["bid_qty_20"].astype(float)
    imbalance = data["imbalance_20"].astype(float)
    values: list[dict[str, float]] = []
    for timestamp in pd.to_datetime(out["time"], utc=True):
        target = timestamp + pd.Timedelta(seconds=59)
        idx = int(data.index.searchsorted(target, side="right") - 1)
        if idx < 60 or idx >= len(data) - 600 or abs((data.index[idx] - target).total_seconds()) > 3:
            values.append(
                {
                    "sec_ret30_bps": np.nan,
                    "sec_ret60_bps": np.nan,
                    "bid20_chg60": np.nan,
                    "imb20_now": np.nan,
                }
            )
            continue
        previous_bid = float(bid20.iloc[idx - 60])
        values.append(
            {
                "sec_ret30_bps": float((close.iloc[idx] / close.iloc[idx - 30] - 1.0) * 10000.0),
                "sec_ret60_bps": float((close.iloc[idx] / close.iloc[idx - 60] - 1.0) * 10000.0),
                "bid20_chg60": float(bid20.iloc[idx] / previous_bid - 1.0) if previous_bid > 0 else np.nan,
                "imb20_now": float(imbalance.iloc[idx]),
            }
        )
    return pd.concat([out.reset_index(drop=True), pd.DataFrame(values)], axis=1)


def build_source_snapshots(source_name: str, seconds: Path, orderbook: Path) -> tuple[pd.DataFrame, float]:
    data = matrix.load_local_data(seconds, orderbook)
    hours = (data.index.max() - data.index.min()).total_seconds() / 3600.0
    snapshots = matrix.build_minute_snapshots(data, source_name)
    snapshots["time"] = pd.to_datetime(snapshots["time"], utc=True)
    snapshots = add_second_micro_features(data, snapshots)
    return snapshots, hours


def vote_signal(row: dict[str, Any], compiled: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any]]:
    signal, info = vote.vote_for(row, compiled)
    total_votes = int(info["upVotes"]) + int(info["downVotes"])
    if signal is None or total_votes < 2:
        return None, info
    return signal, info


def enough(values: list[bool], count: int) -> bool:
    return sum(1 for value in values if bool(value)) >= count


def confirm_signal(row: dict[str, Any], signal: str, variant: str) -> tuple[bool, str]:
    apply_up = variant in {"up_confirm", "both_confirm"}
    apply_down = variant in {"down_confirm", "both_confirm"}

    is_up_sprint_short = (
        signal == "DOWN"
        and row.get("trend") == "trend_up"
        and row.get("normal_pos") == "above_upper"
        and row.get("sprint") == "up_sprint"
    )
    if apply_up and is_up_sprint_short:
        if row.get("lag10_normal_pos") in {"below_lower", "lower_edge", "lower_inside"}:
            return False, "skip_up_sprint_from_lower"
        checks = [
            float(row.get("lag5_ret10_bps", np.nan)) >= 8.0,
            float(row.get("lag10_z", np.nan)) >= 0.0,
            float(row.get("lag5_sigma_expand", np.nan)) >= 0.9,
            float(row.get("lag3_vol_ratio30", np.nan)) >= 0.7,
        ]
        if not enough(checks, 2):
            return False, "skip_up_sprint_not_mature"
        return True, "up_sprint_mature_fade"

    is_down_rebound_long = (
        signal == "UP"
        and row.get("trend") == "trend_down"
        and row.get("normal_pos") in {"below_lower", "lower_edge", "lower_inside"}
    )
    if apply_down and is_down_rebound_long:
        if row.get("normal_pos") == "lower_inside":
            return False, "skip_down_rebound_lower_inside"
        checks = [
            float(row.get("sec_ret30_bps", np.nan)) >= 0.0,
            float(row.get("sec_ret60_bps", np.nan)) >= 0.0,
            float(row.get("bid20_chg60", np.nan)) >= 0.0,
            float(row.get("imb20_now", np.nan)) >= -0.1,
            float(row.get("sigma10_bps", np.nan)) <= 4.8,
        ]
        if not enough(checks, 2):
            return False, "skip_down_rebound_no_stopfall"
        return True, "down_rebound_confirmed"

    return True, "base_vote"


def apply_strategy(test: pd.DataFrame, compiled: list[dict[str, Any]], variant: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    last_time: pd.Timestamp | None = None
    for row in test.sort_values("time").to_dict("records"):
        signal, vote_info = vote_signal(row, compiled)
        if signal is None:
            continue
        ok, reason = confirm_signal(row, signal, variant)
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
        rows.append(record)
        last_time = timestamp
    return pd.DataFrame(rows)


def run() -> dict[str, Any]:
    source_frames = []
    source_hours: dict[str, float] = {}
    source_ranges: dict[str, Any] = {}
    for source_name, seconds, orderbook in matrix.SOURCES:
        frame, hours = build_source_snapshots(source_name, seconds, orderbook)
        source_frames.append(frame)
        source_hours[source_name] = hours
        source_ranges[source_name] = {
            "seconds": str(seconds),
            "orderbook": str(orderbook),
            "hours": round(hours, 4),
        }
    snapshots = add_lag_features(pd.concat(source_frames, ignore_index=True))
    sources = sorted(snapshots["source"].unique())
    variants = ("base", "up_confirm", "down_confirm", "both_confirm")
    reports = []
    all_trades = []
    for variant in variants:
        fold_reports = []
        variant_trades = []
        for test_source in sources:
            train = snapshots[snapshots["source"] != test_source].copy()
            test = snapshots[snapshots["source"] == test_source].copy()
            compiled = vote.compile_rules(train, "balanced")
            trades = apply_strategy(test, compiled, variant)
            if not trades.empty:
                trades["variant"] = variant
                trades["testSource"] = test_source
                variant_trades.append(trades)
                all_trades.append(trades)
            fold_reports.append(
                {
                    "testSource": str(test_source),
                    "result": metrics(trades, source_hours[str(test_source)]),
                    "byReason": {
                        str(reason): metrics(group, source_hours[str(test_source)])
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
                "total": metrics(combined, sum(source_hours.values())),
                "folds": fold_reports,
                "byReason": {
                    str(reason): metrics(group, sum(source_hours.values()))
                    for reason, group in combined.groupby("reason")
                }
                if not combined.empty
                else {},
            }
        )
    if all_trades:
        pd.concat(all_trades, ignore_index=True).to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    output = {
        "method": "Leave-one-source-out balanced-2 vote strategy with precursor confirmations for up-sprint fades and down-trend rebounds.",
        "sources": source_ranges,
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
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
