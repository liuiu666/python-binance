from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "py" / "audit_v2_live_backtest_parity.py"
OUT_JSON = ROOT / "tmp" / "v2_space_filter_research.json"
OUT_TRADES = ROOT / "tmp" / "v2_space_filter_research_trades.csv"


def load_audit_module():
    spec = importlib.util.spec_from_file_location("audit_v2_live_backtest_parity", AUDIT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {AUDIT_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        value = float(value)
        return value if np.isfinite(value) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def payout(won: bool) -> int:
    return 4 if bool(won) else -5


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    wins = sum(1 for row in rows if row["won"])
    equity = peak = max_dd = max_loss = loss_streak = 0
    for row in rows:
        pnl = payout(bool(row["won"]))
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if row["won"]:
            loss_streak = 0
        else:
            loss_streak += 1
            max_loss = max(max_loss, loss_streak)
    fav_bps = [float(row["fav_bps"]) for row in rows if np.isfinite(float(row.get("fav_bps", np.nan)))]
    thin = [x for x in fav_bps if abs(x) <= 5.0]
    big = [x for x in fav_bps if abs(x) >= 10.0]
    return {
        "trades": n,
        "wins": int(wins),
        "winRate": round(wins / n * 100.0, 2) if n else 0.0,
        "pnl": int(sum(payout(bool(row["won"])) for row in rows)),
        "maxDrawdownU": int(max_dd),
        "maxLoss": int(max_loss),
        "avgFavBps": round(float(np.mean(fav_bps)), 2) if fav_bps else None,
        "medianFavBps": round(float(np.median(fav_bps)), 2) if fav_bps else None,
        "thinAbsLe5bp": len(thin),
        "bigAbsGe10bp": len(big),
    }


def split_metrics(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    if not rows:
        return {}
    frame = pd.DataFrame(rows)
    return {str(name): metrics(group.to_dict("records")) for name, group in frame.groupby(key, sort=True)}


def pass_filter(row: pd.Series, variant: dict[str, Any]) -> tuple[bool, str | None]:
    sigma_expand_max = variant.get("sigmaExpandMax")
    if sigma_expand_max is not None and float(row["sigma_expand"]) > float(sigma_expand_max):
        return False, "space_sigma_expand_high"

    center_slope_abs_max = variant.get("centerSlopeAbsMaxBps")
    if center_slope_abs_max is not None and abs(float(row["center_slope_bps"])) > float(center_slope_abs_max):
        return False, "space_center_slope_high"

    inside_max = variant.get("insideMax")
    if inside_max is not None and float(row["inside1_ratio"]) > float(inside_max):
        return False, "space_inside_too_high"

    return True, None


def collect_dataset_events(audit: Any, dataset: str, spec: dict[str, Any]) -> tuple[list[dict[str, Any]], float]:
    c = audit.cfg()
    data = audit.load_data(Path(spec["dir"]))
    start = pd.Timestamp(spec["start"])
    end = pd.Timestamp(spec["end"])
    data = data[(data.index >= start) & (data.index < end)].copy()
    hours = (data.index.max() - data.index.min()).total_seconds() / 3600.0 if len(data) else 0.0
    features = audit.build_features(data, c.normal_window_sec, c)
    close = data["close"].to_numpy(float)
    features["ret_300s_bps"] = np.log(features["close"] / features["close"].shift(300)) * 10000.0
    features["bid20_chg_60"] = features["bid_qty_20"] / features["bid_qty_20"].shift(60).replace(0, np.nan) - 1.0

    events: list[dict[str, Any]] = []
    warmup = max(c.normal_window_sec, c.center_slope_sec, c.retest_sec, 900) + 10
    limit = len(data) - c.horizon_sec
    last_emit_idx = -10**12

    for idx in range(warmup, max(warmup, limit)):
        row = features.iloc[idx]
        if not bool(data["ob_available"].iloc[idx]) or not audit.normal_ready(row, c):
            continue
        signal, reason = audit.signal_from_row(row, c)
        if not signal:
            continue
        raw_signal, raw_reason = signal, reason
        trap = audit.bidwall_trap(signal, reason, row)
        if trap:
            signal = "DOWN"
            reason = "lower_reclaim_bidwall_trap_flip_down"
        veto = audit.quality_v2_veto(signal, row)
        if veto:
            events.append(
                {
                    "event": "v2_veto",
                    "dataset": dataset,
                    "time": data.index[idx],
                    "idx": int(idx),
                    "signal": signal,
                    "reason": reason,
                    "vetoReason": veto,
                }
            )
            continue

        entry = float(close[idx])
        settle = float(close[idx + c.horizon_sec])
        won = bool(settle > entry if signal == "UP" else settle < entry)
        fav_diff = settle - entry if signal == "UP" else entry - settle
        events.append(
            {
                "event": "candidate",
                "dataset": dataset,
                "time": data.index[idx],
                "idx": int(idx),
                "signal": signal,
                "reason": reason,
                "raw_signal": raw_signal,
                "raw_reason": raw_reason,
                "bidwall_trap": bool(trap),
                "entry": entry,
                "settle": settle,
                "settle_time": data.index[idx + c.horizon_sec],
                "won": won,
                "fav_diff": round(float(fav_diff), 4),
                "fav_bps": round(float(fav_diff / entry * 10000.0), 6),
                "z": round(float(row["z"]), 5),
                "inside1_ratio": round(float(row["inside1_ratio"]), 5),
                "observed_pct": round(float(row["observed_pct"]), 4),
                "center_slope_bps": round(float(row["center_slope_bps"]), 4),
                "sigma_bps": round(float(row["sigma_bps"]), 4),
                "sigma_expand": round(float(row["sigma_expand"]), 4),
                "flow_60": round(float(row["flow_60"]), 6),
                "imbalance_20": round(float(row["imbalance_20"]), 6),
                "micro_bps": round(float(row["micro_bps"]), 6),
                "ret_300s_bps": round(float(row["ret_300s_bps"]), 4),
                "bid20_chg_60": round(float(row["bid20_chg_60"]), 6),
            }
        )
    return events, hours


def apply_variant(events: list[dict[str, Any]], variant: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    last_emit_by_dataset: dict[str, int] = {}
    for base_row in sorted(events, key=lambda item: (str(item["dataset"]), int(item["idx"]))):
        dataset = str(base_row["dataset"])
        idx = int(base_row["idx"])
        last_emit_idx = last_emit_by_dataset.get(dataset, -10**12)
        if base_row.get("event") == "v2_veto":
            last_emit_by_dataset[dataset] = idx
            continue
        if idx - last_emit_idx < 600:
            continue

        row = pd.Series(base_row)
        allowed, skip_reason = pass_filter(row, variant)
        if not allowed:
            skipped.append(
                {
                    "variant": variant["name"],
                    "dataset": dataset,
                    "time": base_row["time"],
                    "idx": idx,
                    "signal": base_row["signal"],
                    "reason": base_row["reason"],
                    "skipReason": skip_reason,
                    "inside1_ratio": base_row["inside1_ratio"],
                    "center_slope_bps": base_row["center_slope_bps"],
                    "sigma_expand": base_row["sigma_expand"],
                }
            )
            last_emit_by_dataset[dataset] = idx
            continue

        out = dict(base_row)
        out["variant"] = variant["name"]
        rows.append(out)
        last_emit_by_dataset[dataset] = idx
    return rows, skipped


def run() -> dict[str, Any]:
    audit = load_audit_module()
    variants = [
        {"name": "base_v2", "sigmaExpandMax": None, "centerSlopeAbsMaxBps": None, "insideMax": None},
        {"name": "space_v1_expand16_slope6_inside72", "sigmaExpandMax": 1.6, "centerSlopeAbsMaxBps": 6.0, "insideMax": 0.72},
        {"name": "space_v2_expand16_slope6_inside75", "sigmaExpandMax": 1.6, "centerSlopeAbsMaxBps": 6.0, "insideMax": 0.75},
        {"name": "space_v3_expand14_slope6_inside72", "sigmaExpandMax": 1.4, "centerSlopeAbsMaxBps": 6.0, "insideMax": 0.72},
        {"name": "space_v4_expand12_slope6_inside72", "sigmaExpandMax": 1.2, "centerSlopeAbsMaxBps": 6.0, "insideMax": 0.72},
    ]
    all_trades: list[dict[str, Any]] = []
    output: dict[str, Any] = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "basis": "Current online V2 signal replay; variants only add post-signal space/stability filters before accepting a trade.",
        "variants": {},
    }
    total_hours = 0.0

    base_candidates: list[dict[str, Any]] = []
    for dataset, spec in audit.DATASETS.items():
        dataset_events, dataset_hours = collect_dataset_events(audit, dataset, spec)
        base_candidates.extend(dataset_events)
        total_hours += dataset_hours

    for variant in variants:
        rows, skipped = apply_variant(base_candidates, variant)
        all_trades.extend(rows)
        output["variants"][variant["name"]] = {
            "filters": {k: v for k, v in variant.items() if k != "name"},
            "overall": metrics(rows),
            "tradesPerDay": round(len(rows) / max(total_hours, 1e-9) * 24.0, 2),
            "byDataset": split_metrics(rows, "dataset"),
            "bySide": split_metrics(rows, "signal"),
            "skipped": {
                "count": len(skipped),
                "byReason": {str(k): len(v) for k, v in pd.DataFrame(skipped).groupby("skipReason")} if skipped else {},
            },
        }

    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(clean(all_trades)).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
