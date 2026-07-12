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


OUT_JSON = ROOT / "tmp" / "up_sprint_precursors.json"
OUT_CSV = ROOT / "tmp" / "up_sprint_precursors_events.csv"
OUT_RULES = ROOT / "tmp" / "up_sprint_precursor_rules.csv"


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


def build_all_snapshots() -> pd.DataFrame:
    frames = []
    for source_name, seconds, orderbook in matrix.SOURCES:
        data = matrix.load_local_data(seconds, orderbook)
        frame = matrix.build_minute_snapshots(data, source_name)
        frames.append(frame)
    snapshots = pd.concat(frames, ignore_index=True)
    snapshots["time"] = pd.to_datetime(snapshots["time"], utc=True)
    return snapshots.sort_values(["source", "time"]).reset_index(drop=True)


def add_lags(snapshots: pd.DataFrame) -> pd.DataFrame:
    out = snapshots.copy()
    lag_cols = [
        "trend",
        "volatility",
        "normal_pos",
        "sprint",
        "flow",
        "book",
        "volume",
        "ret10_bps",
        "ret30_bps",
        "ret60_bps",
        "range10_bps",
        "range30_bps",
        "sigma10_bps",
        "vol_ratio30",
        "flow5",
        "imb20",
        "z",
        "sigma_expand",
        "future10_bps",
    ]
    for lag in (1, 3, 5, 10):
        for col in lag_cols:
            if col in out.columns:
                out[f"lag{lag}_{col}"] = out.groupby("source")[col].shift(lag)
    return out


def summarize_events(events: pd.DataFrame, name: str) -> dict[str, Any]:
    if events.empty:
        return {"name": name, "events": 0}
    output: dict[str, Any] = {
        "name": name,
        "events": int(len(events)),
        "sources": int(events["source"].nunique()),
        "continueUpRate": round(float((events["future10_bps"] > 0).mean() * 100.0), 2),
        "fadeDownRate": round(float((events["future10_bps"] < 0).mean() * 100.0), 2),
        "future10MedianBps": round(float(events["future10_bps"].median()), 4),
    }
    numeric = [
        "lag1_ret10_bps",
        "lag3_ret10_bps",
        "lag5_ret10_bps",
        "lag10_ret10_bps",
        "lag1_sigma10_bps",
        "lag3_sigma10_bps",
        "lag5_sigma10_bps",
        "lag10_sigma10_bps",
        "lag1_vol_ratio30",
        "lag3_vol_ratio30",
        "lag5_vol_ratio30",
        "lag10_vol_ratio30",
        "lag1_flow5",
        "lag3_flow5",
        "lag5_flow5",
        "lag10_flow5",
        "lag1_imb20",
        "lag3_imb20",
        "lag5_imb20",
        "lag10_imb20",
        "lag1_z",
        "lag3_z",
        "lag5_z",
        "lag10_z",
        "lag1_sigma_expand",
        "lag3_sigma_expand",
        "lag5_sigma_expand",
        "lag10_sigma_expand",
    ]
    for col in numeric:
        if col not in events.columns:
            continue
        vals = pd.to_numeric(events[col], errors="coerce").dropna()
        if len(vals):
            output[col] = round(float(vals.median()), 6)
    output["bySource"] = {
        str(source): {
            "events": int(len(group)),
            "continueUpRate": round(float((group["future10_bps"] > 0).mean() * 100.0), 2),
            "future10MedianBps": round(float(group["future10_bps"].median()), 4),
        }
        for source, group in events.groupby("source")
    }
    return output


def compare_continue_vs_fade(events: pd.DataFrame) -> dict[str, Any]:
    cont = events[events["future10_bps"] > 0]
    fade = events[events["future10_bps"] < 0]
    output: dict[str, Any] = {
        "continueEvents": int(len(cont)),
        "fadeEvents": int(len(fade)),
    }
    features = [
        col
        for col in events.columns
        if col.startswith("lag")
        and (
            col.endswith("_ret10_bps")
            or col.endswith("_ret30_bps")
            or col.endswith("_sigma10_bps")
            or col.endswith("_vol_ratio30")
            or col.endswith("_flow5")
            or col.endswith("_imb20")
            or col.endswith("_z")
            or col.endswith("_sigma_expand")
        )
    ]
    diffs = []
    for col in features:
        c = pd.to_numeric(cont[col], errors="coerce").dropna()
        f = pd.to_numeric(fade[col], errors="coerce").dropna()
        if len(c) == 0 or len(f) == 0:
            continue
        diffs.append(
            {
                "feature": col,
                "continueMedian": round(float(c.median()), 6),
                "fadeMedian": round(float(f.median()), 6),
                "deltaContinueMinusFade": round(float(c.median() - f.median()), 6),
            }
        )
    output["diffs"] = sorted(diffs, key=lambda row: abs(row["deltaContinueMinusFade"]), reverse=True)[:40]
    return output


def rule_search(events: pd.DataFrame) -> list[dict[str, Any]]:
    features = [
        col
        for col in events.columns
        if col.startswith("lag")
        and (
            col.endswith("_ret10_bps")
            or col.endswith("_ret30_bps")
            or col.endswith("_sigma10_bps")
            or col.endswith("_vol_ratio30")
            or col.endswith("_flow5")
            or col.endswith("_imb20")
            or col.endswith("_z")
            or col.endswith("_sigma_expand")
        )
    ]
    rules = []
    base_n = len(events)
    if base_n == 0:
        return []
    for feature in features:
        values = pd.to_numeric(events[feature], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
        if len(values) < 12:
            continue
        for threshold in sorted(set(float(values.quantile(q)) for q in (0.2, 0.35, 0.5, 0.65, 0.8))):
            for op in ("<=", ">="):
                selected = events[pd.to_numeric(events[feature], errors="coerce") <= threshold] if op == "<=" else events[pd.to_numeric(events[feature], errors="coerce") >= threshold]
                if len(selected) < max(8, int(base_n * 0.15)):
                    continue
                up_rate = float((selected["future10_bps"] > 0).mean() * 100.0)
                down_rate = float((selected["future10_bps"] < 0).mean() * 100.0)
                rules.append(
                    {
                        "rule": f"{feature} {op} {threshold:.6g}",
                        "events": int(len(selected)),
                        "continueUpRate": round(up_rate, 2),
                        "fadeDownRate": round(down_rate, 2),
                        "future10MedianBps": round(float(selected["future10_bps"].median()), 4),
                        "sources": int(selected["source"].nunique()),
                    }
                )
    return sorted(rules, key=lambda item: (max(item["continueUpRate"], item["fadeDownRate"]), item["events"]), reverse=True)[:60]


def run() -> dict[str, Any]:
    snapshots = add_lags(build_all_snapshots())
    up_sprint = snapshots[
        snapshots["trend"].eq("trend_up")
        & snapshots["normal_pos"].eq("above_upper")
        & snapshots["sprint"].eq("up_sprint")
    ].copy()
    # One event per 10 minutes per source, to avoid counting the same sprint every minute.
    events = []
    for _, group in up_sprint.groupby("source"):
        last_time: pd.Timestamp | None = None
        for row in group.sort_values("time").to_dict("records"):
            timestamp = pd.Timestamp(row["time"])
            if last_time is not None and (timestamp - last_time).total_seconds() < 600:
                continue
            events.append(row)
            last_time = timestamp
    events_df = pd.DataFrame(events)
    events_df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    pre_center_or_upper = events_df[
        events_df["lag5_normal_pos"].isin(["center", "upper_inside", "upper_edge"])
    ].copy()
    pre_from_lower = events_df[
        events_df["lag10_normal_pos"].isin(["below_lower", "lower_edge", "lower_inside"])
    ].copy()
    rules = rule_search(events_df)
    pd.DataFrame(rules).to_csv(OUT_RULES, index=False, encoding="utf-8-sig")
    output = {
        "method": "Find precursors before trend_up + above_upper + up_sprint events. Events are 10-minute deduped.",
        "allUpSprint": summarize_events(events_df, "all_up_sprint"),
        "preCenterOrUpper": summarize_events(pre_center_or_upper, "lag5_center_or_upper"),
        "preFromLower": summarize_events(pre_from_lower, "lag10_from_lower_side"),
        "continueVsFadeDiffs": compare_continue_vs_fade(events_df),
        "candidateRules": rules[:40],
        "files": {
            "events": str(OUT_CSV),
            "rules": str(OUT_RULES),
        },
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
