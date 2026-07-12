"""Explain wins and losses from the corrected regime router using entry-time data."""

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

import research_market_regime_classifier as regime_features  # noqa: E402
import research_regime_router_corrected as corrected  # noqa: E402
import research_v2_persistent_reclaim as source  # noqa: E402


INPUT = ROOT / "tmp" / "regime_router_corrected_trades.csv"
OUT_JSON = ROOT / "tmp" / "corrected_router_loss_analysis.json"
OUT_CSV = ROOT / "tmp" / "corrected_router_loss_features.csv"


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


def mapped_rows() -> pd.DataFrame:
    trades = pd.read_csv(INPUT, parse_dates=["time", "settle_time"])
    trades = trades[trades["dataset"].isin(source.DATASETS)].copy()
    output = []
    cache = {}
    for dataset, group in trades.groupby("dataset", sort=True):
        item = source.DATASETS[dataset]
        key = (str(item["seconds"]), str(item["orderbook"]))
        if key not in cache:
            cache[key] = source.load_market(Path(item["seconds"]), Path(item["orderbook"]))
        data = cache[key]
        features = regime_features.build_regime_features(data)
        features["ob_coverage_60"] = features["ob_available"].astype(float).rolling(60, min_periods=60).mean()
        states = corrected.build_states(features)
        close = data["close"].to_numpy(float)
        index_to_pos = pd.Series(np.arange(len(data)), index=data.index)
        for _, trade in group.iterrows():
            timestamp = pd.Timestamp(trade["time"])
            idx = int(index_to_pos.loc[timestamp])
            row = states.iloc[idx]
            sign = 1.0 if trade["signal"] == "UP" else -1.0
            entry = float(close[idx])
            future = close[idx : idx + 601]
            signed_path = (future / entry - 1.0) * 10000.0 * sign
            item_out = trade.to_dict()
            item_out.update(
                z=float(row["z"]),
                inside1_ratio=float(row["inside1_ratio"]),
                center_slope_bps=float(row["center_slope_bps"]),
                sigma_bps=float(row["sigma_bps"]),
                sigma_expand=float(row["sigma_expand"]),
                ret_60s_bps=float(row["ret_60s_bps"]),
                ret_300s_bps=float(row["ret_300s_bps"]),
                ret_600s_bps=float(row["ret_600s_bps"]),
                ret_1800s_bps=float(row["ret_1800s_bps"]),
                efficiency_600=float(row["efficiency_600"]),
                pos_600s=float(row["pos_600s"]),
                pos_1800s=float(row["pos_1800s"]),
                range_600s_bps=float(row["range_600s_bps"]),
                bandwalk_signed=float(row["bandwalk_signed"]),
                flow_60=float(row["flow_60"]),
                flow_120_mean=float(row["flow_120_mean"]),
                imbalance_20=float(row["imbalance_20"]),
                imbalance_60_mean=float(row["imbalance_60_mean"]),
                micro_bps=float(row["micro_bps"]),
                slope_30_bps=float(row["slope_30_bps"]),
                slope_90_bps=float(row["slope_90_bps"]),
                signal_ret60_bps=sign * float(row["ret_60s_bps"]),
                signal_ret300_bps=sign * float(row["ret_300s_bps"]),
                signal_ret600_bps=sign * float(row["ret_600s_bps"]),
                signal_ret1800_bps=sign * float(row["ret_1800s_bps"]),
                signal_center_slope_bps=sign * float(row["center_slope_bps"]),
                signal_flow60=sign * float(row["flow_60"]),
                signal_flow120=sign * float(row["flow_120_mean"]),
                signal_imb20=sign * float(row["imbalance_20"]),
                signal_imb60=sign * float(row["imbalance_60_mean"]),
                signal_micro_bps=sign * float(row["micro_bps"]),
                future_60_bps=float(signed_path[60]),
                future_120_bps=float(signed_path[120]),
                future_300_bps=float(signed_path[300]),
                future_600_bps=float(signed_path[600]),
                max_favorable_bps=float(np.max(signed_path)),
                max_adverse_bps=float(np.min(signed_path)),
                max_favorable_sec=int(np.argmax(signed_path)),
                max_adverse_sec=int(np.argmin(signed_path)),
            )
            output.append(item_out)
    return pd.DataFrame(output).sort_values(["dataset", "time"])


def stats(rows: pd.DataFrame) -> dict:
    cols = [
        "signed_outcome_bps", "signal_ret60_bps", "signal_ret300_bps", "signal_ret600_bps",
        "signal_ret1800_bps", "signal_center_slope_bps", "efficiency_600", "z",
        "inside1_ratio", "sigma_expand", "pos_600s", "bandwalk_signed",
        "signal_flow60", "signal_flow120", "signal_imb20", "signal_imb60",
        "signal_micro_bps", "future_60_bps", "future_120_bps", "future_300_bps",
        "max_favorable_bps", "max_adverse_bps",
    ]
    return {
        "n": int(len(rows)),
        "winRate": round(float(rows["won"].astype(bool).mean() * 100.0), 2) if len(rows) else 0.0,
        "median": {col: round(float(pd.to_numeric(rows[col], errors="coerce").median()), 4) for col in cols},
        "mean": {col: round(float(pd.to_numeric(rows[col], errors="coerce").mean()), 4) for col in cols},
    }


def run() -> dict:
    rows = mapped_rows()
    rows["won"] = rows["won"].astype(bool)
    normal_rows = rows[rows["kind"] == "normal"]
    trend_rows = rows[rows["kind"] == "trend"]
    normal_losses = normal_rows[~normal_rows["won"]]
    trend_losses = trend_rows[~trend_rows["won"]]
    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "overall": {"wins": stats(rows[rows["won"]]), "losses": stats(rows[~rows["won"]])},
        "normal": {"wins": stats(normal_rows[normal_rows["won"]]), "losses": stats(normal_losses)},
        "trend": {"wins": stats(trend_rows[trend_rows["won"]]), "losses": stats(trend_losses)},
        "normalLossBuckets": {
            "countertrend10m_le_minus5bp": int((normal_losses["signal_ret600_bps"] <= -5.0).sum()),
            "countertrend30m_le_minus10bp": int((normal_losses["signal_ret1800_bps"] <= -10.0).sum()),
            "currentBookOpposesSignal": int(((normal_losses["signal_imb20"] < 0.0) | (normal_losses["signal_micro_bps"] < 0.0)).sum()),
            "wentPositive5bpThenLost": int(((normal_losses["max_favorable_bps"] >= 5.0) & (normal_losses["future_600_bps"] <= 0.0)).sum()),
        },
        "trendLossBuckets": {
            "wentPositive5bpThenLost": int(((trend_losses["max_favorable_bps"] >= 5.0) & (trend_losses["future_600_bps"] <= 0.0)).sum()),
            "bookTurnedAgainstAtEntry": int(((trend_losses["signal_imb20"] < 0.0) | (trend_losses["signal_micro_bps"] < 0.0)).sum()),
        },
        "losses": clean(rows[~rows["won"]].to_dict("records")),
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    rows.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
