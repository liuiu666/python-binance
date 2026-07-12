"""Study rolling price-distribution morphology from 1 to 10 minutes.

Shape labels use only prices available at each completed minute. Future
10-minute returns are attached afterwards for validation and never influence
classification thresholds.
"""

from __future__ import annotations

import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from second_backtest.data import load_second_bars  # noqa: E402


OUT_JSON = ROOT / "tmp" / "normal_shape_1m_10m_latest.json"
OUT_CSV = ROOT / "tmp" / "normal_shape_1m_10m_samples.csv"
OUT_PNG = ROOT / "tmp" / "normal_shape_1m_10m_latest.png"
HORIZON_SEC = 600
WINDOW_MINUTES = tuple(range(1, 11))


SOURCES = (
    ("2026-07-05_06", ROOT / "tmp" / "latest_pull_20260706_2130" / "data" / "btcusdt_1s_trades.csv", None, None, "history"),
    ("2026-07-08_09", ROOT / "tmp" / "latest_live_pull_20260709_101331" / "data" / "btcusdt_1s_trades.csv", None, "2026-07-09T02:02:00Z", "history"),
    ("2026-07-09_10", ROOT / "tmp" / "latest_pull_20260710_203217" / "data" / "btcusdt_1s_trades.csv", "2026-07-09T02:14:00Z", None, "history"),
    ("2026-07-11_12", ROOT / "tmp" / "latest_pull_20260712_migration_fix" / "extracted" / "data" / "btcusdt_1s_trades.csv", None, None, "latest"),
)


SHAPE_LABELS = {
    "balanced_normal": "稳定钟形",
    "shift_up": "中心上移",
    "shift_down": "中心下移",
    "expanding": "波动扩张",
    "contracting": "波动收缩",
    "upper_escape": "上尾逃逸",
    "lower_escape": "下尾逃逸",
    "right_skew": "右偏分布",
    "left_skew": "左偏分布",
    "heavy_tail": "厚尾分布",
    "distorted": "畸变/换区",
}

SHAPE_LABELS_EN = {
    "balanced_normal": "Stable bell",
    "shift_up": "Center up",
    "shift_down": "Center down",
    "expanding": "Expanding",
    "contracting": "Contracting",
    "upper_escape": "Upper escape",
    "lower_escape": "Lower escape",
    "right_skew": "Right skew",
    "left_skew": "Left skew",
    "heavy_tail": "Heavy tail",
    "distorted": "Distorted",
}


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return value


def utc(value: str | pd.Timestamp | None) -> pd.Timestamp | None:
    if value is None:
        return None
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")


def classify_shape(
    z: float,
    inside1: float,
    skew: float,
    excess_kurtosis: float,
    slope_sigma: float,
    sigma_ratio: float,
) -> str:
    if z >= 2.0:
        return "upper_escape"
    if z <= -2.0:
        return "lower_escape"
    if slope_sigma >= 0.75:
        return "shift_up"
    if slope_sigma <= -0.75:
        return "shift_down"
    if sigma_ratio >= 1.5:
        return "expanding"
    if sigma_ratio <= 0.67:
        return "contracting"
    if 0.55 <= inside1 <= 0.80 and abs(skew) <= 0.75 and abs(excess_kurtosis) <= 1.5:
        return "balanced_normal"
    if skew > 0.75:
        return "right_skew"
    if skew < -0.75:
        return "left_skew"
    if excess_kurtosis > 1.5:
        return "heavy_tail"
    return "distorted"


def shape_features(values: np.ndarray, observed: np.ndarray) -> dict[str, Any] | None:
    if len(values) < 30 or not np.all(np.isfinite(values)):
        return None
    observed_pct = float(np.mean(observed) * 100.0)
    if observed_pct < 60.0:
        return None
    center = float(np.mean(values))
    sigma = float(np.std(values, ddof=0))
    if center <= 0.0 or sigma <= 0.0:
        return None
    deviations = (values - center) / sigma
    skew = float(np.mean(deviations**3))
    excess_kurtosis = float(np.mean(deviations**4) - 3.0)
    inside1 = float(np.mean(np.abs(deviations) <= 1.0))
    sigma_bps = sigma / center * 10000.0
    quarter = max(10, len(values) // 4)
    center_first = float(np.mean(values[:quarter]))
    center_last = float(np.mean(values[-quarter:]))
    slope_bps = (center_last / center_first - 1.0) * 10000.0 if center_first > 0.0 else 0.0
    slope_sigma = slope_bps / sigma_bps if sigma_bps > 0.0 else 0.0
    half = len(values) // 2
    sigma_first = float(np.std(values[:half], ddof=0))
    sigma_last = float(np.std(values[half:], ddof=0))
    sigma_ratio = sigma_last / sigma_first if sigma_first > 0.0 else 1.0
    path = float(np.abs(np.diff(values)).sum())
    efficiency = abs(float(values[-1] - values[0])) / path if path > 0.0 else 0.0
    z = float(deviations[-1])
    range_bps = (float(np.max(values)) / float(np.min(values)) - 1.0) * 10000.0
    return {
        "center": center,
        "sigma_price": sigma,
        "sigma_bps": sigma_bps,
        "z": z,
        "inside1_ratio": inside1,
        "skew": skew,
        "excess_kurtosis": excess_kurtosis,
        "center_slope_bps": slope_bps,
        "slope_sigma": slope_sigma,
        "sigma_ratio": sigma_ratio,
        "range_bps": range_bps,
        "efficiency": efficiency,
        "observed_pct": observed_pct,
        "shape": classify_shape(z, inside1, skew, excess_kurtosis, slope_sigma, sigma_ratio),
        "tail": "upper" if z >= 1.2 else "lower" if z <= -1.2 else "core",
    }


def sample_source(
    name: str,
    path: Path,
    start: str | None,
    end: str | None,
    role: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    data = load_second_bars(path, include_shards=False)
    start_time = utc(start) or data.index.min()
    end_time = utc(end) or data.index.max()
    data = data[(data.index >= start_time) & (data.index <= end_time)].copy()
    close = data["close"].to_numpy(float)
    observed = data["observed"].fillna(False).to_numpy(bool)
    minute_positions = np.flatnonzero(data.index.second.to_numpy() == 59)
    rows: list[dict[str, Any]] = []
    for index in minute_positions:
        if index + HORIZON_SEC >= len(data):
            continue
        current_time = data.index[index]
        future_price = float(close[index + HORIZON_SEC])
        current_price = float(close[index])
        future10_bps = (future_price / current_price - 1.0) * 10000.0
        for minutes in WINDOW_MINUTES:
            width = minutes * 60
            if index + 1 < width:
                continue
            feature = shape_features(close[index - width + 1 : index + 1], observed[index - width + 1 : index + 1])
            if not feature:
                continue
            z = float(feature["z"])
            slope = float(feature["center_slope_bps"])
            tail_reversion_bps = future10_bps * (-1.0 if z > 0.0 else 1.0) if abs(z) >= 1.2 else float("nan")
            shift_continuation_bps = future10_bps * (1.0 if slope > 0.0 else -1.0) if abs(slope) > 0.0 else float("nan")
            rows.append(
                {
                    "source": name,
                    "role": role,
                    "time": current_time,
                    "time_shanghai": current_time.tz_convert("Asia/Shanghai"),
                    "window_min": minutes,
                    "price": current_price,
                    "future10_bps": future10_bps,
                    "future10_abs_bps": abs(future10_bps),
                    "tail_reversion_bps": tail_reversion_bps,
                    "shift_continuation_bps": shift_continuation_bps,
                    **feature,
                }
            )
    frame = pd.DataFrame(rows)
    source_report = {
        "role": role,
        "path": str(path),
        "start": data.index.min(),
        "end": data.index.max(),
        "hours": round((data.index.max() - data.index.min()).total_seconds() / 3600.0, 4),
        "seconds": len(data),
        "observedPct": round(float(data["observed"].mean() * 100.0), 2),
        "samples": len(frame),
    }
    return frame, source_report


def run_lengths(frame: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (source, window), group in frame.sort_values("time").groupby(["source", "window_min"]):
        previous_shape = None
        previous_time = None
        run_shape = None
        run_length = 0
        for row in group.itertuples():
            contiguous = previous_time is not None and (row.time - previous_time).total_seconds() <= 61
            if contiguous and row.shape == run_shape:
                run_length += 1
            else:
                if run_shape is not None:
                    rows.append({"source": source, "window_min": window, "shape": run_shape, "duration_min": run_length})
                run_shape = row.shape
                run_length = 1
            previous_shape = row.shape
            previous_time = row.time
        if run_shape is not None:
            rows.append({"source": source, "window_min": window, "shape": run_shape, "duration_min": run_length})
    return pd.DataFrame(rows)


def transitions(frame: pd.DataFrame, window: int) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str]] = Counter()
    totals: Counter[str] = Counter()
    for _, group in frame[frame["window_min"] == window].sort_values("time").groupby("source"):
        values = group[["time", "shape"]].to_records(index=False)
        for left, right in zip(values[:-1], values[1:]):
            if (right[0] - left[0]).total_seconds() > 61:
                continue
            counts[(str(left[1]), str(right[1]))] += 1
            totals[str(left[1])] += 1
    top = []
    for (left, right), count in counts.most_common(15):
        top.append(
            {
                "from": left,
                "to": right,
                "count": count,
                "probabilityPct": round(count / totals[left] * 100.0, 2) if totals[left] else 0.0,
            }
        )
    return top


def summarize(frame: pd.DataFrame) -> dict[str, Any]:
    durations = run_lengths(frame)
    windows: dict[str, Any] = {}
    for window, group in frame.groupby("window_min"):
        shape_rows: dict[str, Any] = {}
        for shape, shaped in group.groupby("shape"):
            duration = durations[(durations["window_min"] == window) & (durations["shape"] == shape)]["duration_min"]
            tails = shaped[shaped["tail"] != "core"]
            shape_rows[str(shape)] = {
                "label": SHAPE_LABELS.get(str(shape), str(shape)),
                "samples": len(shaped),
                "sharePct": round(len(shaped) / len(group) * 100.0, 2),
                "medianDurationMin": round(float(duration.median()), 2) if len(duration) else None,
                "p90DurationMin": round(float(duration.quantile(0.9)), 2) if len(duration) else None,
                "avgFuture10Bps": round(float(shaped["future10_bps"].mean()), 3),
                "avgFuture10AbsBps": round(float(shaped["future10_abs_bps"].mean()), 3),
                "futureUpRatePct": round(float((shaped["future10_bps"] > 0.0).mean() * 100.0), 2),
                "tailSamples": len(tails),
                "tailReversionRatePct": round(float((tails["tail_reversion_bps"] > 0.0).mean() * 100.0), 2) if len(tails) else None,
            }
        windows[str(window)] = {
            "samples": len(group),
            "medianSigmaBps": round(float(group["sigma_bps"].median()), 3),
            "medianRangeBps": round(float(group["range_bps"].median()), 3),
            "balancedSharePct": round(float((group["shape"] == "balanced_normal").mean() * 100.0), 2),
            "tailSharePct": round(float((group["tail"] != "core").mean() * 100.0), 2),
            "shapes": shape_rows,
            "topTransitions": transitions(frame, int(window)),
        }
    return windows


def cross_window_states(frame: pd.DataFrame) -> dict[str, Any]:
    pivot = frame.pivot_table(index=["source", "time"], columns="window_min", values="shape", aggfunc="last")
    required = [1, 2, 3, 5, 10]
    pivot = pivot.dropna(subset=required)
    if pivot.empty:
        return {}
    balanced = pivot[required].eq("balanced_normal")
    shift_up = pivot[required].eq("shift_up")
    shift_down = pivot[required].eq("shift_down")
    return {
        "samples": len(pivot),
        "allSelectedBalancedPct": round(float(balanced.all(axis=1).mean() * 100.0), 2),
        "atLeastThreeBalancedPct": round(float((balanced.sum(axis=1) >= 3).mean() * 100.0), 2),
        "atLeastThreeShiftUpPct": round(float((shift_up.sum(axis=1) >= 3).mean() * 100.0), 2),
        "atLeastThreeShiftDownPct": round(float((shift_down.sum(axis=1) >= 3).mean() * 100.0), 2),
    }


def validation_by_role(frame: pd.DataFrame) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for window, window_group in frame.groupby("window_min"):
        role_rows: dict[str, Any] = {}
        for role, group in window_group.groupby("role"):
            tails = group[group["tail"] != "core"]
            escapes = group[group["shape"].isin(["upper_escape", "lower_escape"])]
            shifts = group[group["shape"].isin(["shift_up", "shift_down"])]
            balanced_tails = group[(group["shape"] == "balanced_normal") & (group["tail"] != "core")]
            role_rows[str(role)] = {
                "samples": len(group),
                "tailSamples": len(tails),
                "tailReversionRatePct": round(float((tails["tail_reversion_bps"] > 0.0).mean() * 100.0), 2) if len(tails) else None,
                "escapeSamples": len(escapes),
                "escapeReversionRatePct": round(float((escapes["tail_reversion_bps"] > 0.0).mean() * 100.0), 2) if len(escapes) else None,
                "shiftSamples": len(shifts),
                "shiftContinuationRatePct": round(float((shifts["shift_continuation_bps"] > 0.0).mean() * 100.0), 2) if len(shifts) else None,
                "balancedTailSamples": len(balanced_tails),
                "balancedTailReversionRatePct": round(float((balanced_tails["tail_reversion_bps"] > 0.0).mean() * 100.0), 2) if len(balanced_tails) else None,
            }
        report[str(window)] = role_rows
    return report


def source_validation_10m(frame: pd.DataFrame) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for source, group in frame[frame["window_min"] == 10].groupby("source"):
        escapes = group[group["shape"].isin(["upper_escape", "lower_escape"])]
        shifts = group[group["shape"].isin(["shift_up", "shift_down"])]
        balanced_tails = group[(group["shape"] == "balanced_normal") & (group["tail"] != "core")]
        report[str(source)] = {
            "escapeSamples": len(escapes),
            "escapeReversionRatePct": round(float((escapes["tail_reversion_bps"] > 0.0).mean() * 100.0), 2) if len(escapes) else None,
            "shiftSamples": len(shifts),
            "shiftContinuationRatePct": round(float((shifts["shift_continuation_bps"] > 0.0).mean() * 100.0), 2) if len(shifts) else None,
            "balancedTailSamples": len(balanced_tails),
            "balancedTailReversionRatePct": round(float((balanced_tails["tail_reversion_bps"] > 0.0).mean() * 100.0), 2) if len(balanced_tails) else None,
        }
    return report


def render_chart(frame: pd.DataFrame) -> None:
    try:
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except Exception:
        return
    latest = frame[frame["source"] == "2026-07-11_12"].copy()
    if latest.empty:
        return
    end = latest["time"].max()
    start = end - pd.Timedelta(hours=12)
    latest = latest[latest["time"] >= start]
    colors = {
        "balanced_normal": "#2a9d8f",
        "shift_up": "#277da1",
        "shift_down": "#f94144",
        "expanding": "#f8961e",
        "contracting": "#90be6d",
        "upper_escape": "#6a4c93",
        "lower_escape": "#9c6644",
        "right_skew": "#577590",
        "left_skew": "#bc6c25",
        "heavy_tail": "#8d99ae",
        "distorted": "#adb5bd",
    }
    fig, axes = plt.subplots(5, 1, figsize=(16, 10), sharex=True, gridspec_kw={"height_ratios": [3, 1, 1, 1, 1]})
    price = latest[latest["window_min"] == 10]
    axes[0].plot(price["time"], price["price"], color="#202124", linewidth=1.1)
    axes[0].set_ylabel("BTC price")
    axes[0].grid(alpha=0.2)
    for axis, window in zip(axes[1:], (1, 2, 5, 10)):
        group = latest[latest["window_min"] == window].sort_values("time")
        for shape, shaped in group.groupby("shape"):
            axis.scatter(shaped["time"], np.zeros(len(shaped)), s=12, marker="s", color=colors.get(shape, "#999999"), label=SHAPE_LABELS_EN.get(shape, shape))
        axis.set_yticks([])
        axis.set_ylabel(f"{window}m", rotation=0, labelpad=18)
        axis.grid(axis="x", alpha=0.15)
    handles, labels = axes[1].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.965), ncol=6, frameon=False)
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M", tz=end.tz))
    fig.suptitle("Rolling price-distribution morphology: 1m / 2m / 5m / 10m", y=0.995)
    fig.tight_layout(rect=(0, 0, 1, 0.90))
    fig.savefig(OUT_PNG, dpi=150)
    plt.close(fig)


def main() -> None:
    frames: list[pd.DataFrame] = []
    source_reports: dict[str, Any] = {}
    for source in SOURCES:
        frame, source_report = sample_source(*source)
        frames.append(frame)
        source_reports[source[0]] = source_report
    samples = pd.concat(frames, ignore_index=True).sort_values(["source", "time", "window_min"])
    report = {
        "method": {
            "object": "Rolling distribution of one-second BTC prices, not a Gaussian assumption about returns.",
            "windowsMin": list(WINDOW_MINUTES),
            "sampleAt": "Each completed minute ending at second 59.",
            "classification": "Fixed statistical morphology thresholds; no future outcome or parameter search is used to assign shape.",
            "futureValidation": "The price 600 seconds later is attached only after shape classification.",
            "shapeLabels": SHAPE_LABELS,
        },
        "sources": source_reports,
        "windows": summarize(samples),
        "crossWindow": cross_window_states(samples),
        "validationByRole": validation_by_role(samples),
        "sourceValidation10m": source_validation_10m(samples),
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    samples.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    render_chart(samples)
    print(json.dumps(clean(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
