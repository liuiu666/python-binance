from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "py" / "audit_v2_live_backtest_parity.py"
OUT_JSON = ROOT / "tmp" / "v2_trend_mode_filter_research.json"
OUT_TRADES = ROOT / "tmp" / "v2_trend_mode_filter_trades.csv"


DATASETS = {
    "2026-07-05_2026-07-06": {
        "dir": ROOT / "tmp" / "latest_pull_20260706_2130" / "data",
        "start": "2026-07-05T00:00:00Z",
        "end": "2026-07-07T00:00:00Z",
    },
    "2026-07-07_2026-07-08": {
        "dir": ROOT / "tmp" / "latest_pull_20260708_204204" / "data",
        "start": "2026-07-07T00:00:00Z",
        "end": "2026-07-09T00:00:00Z",
    },
    "2026-07-09_today": {
        "dir": ROOT / "tmp" / "latest_live_pull_20260709_220834" / "data",
        "start": "2026-07-09T00:00:00Z",
        "end": "2026-07-10T00:00:00Z",
    },
}


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
    return {
        "trades": n,
        "wins": int(wins),
        "winRate": round(wins / n * 100.0, 2) if n else 0.0,
        "pnl": int(sum(payout(bool(row["won"])) for row in rows)),
        "maxDrawdownU": int(max_dd),
        "maxLoss": int(max_loss),
        "avgFavBps": round(float(np.mean(fav_bps)), 2) if fav_bps else None,
        "medianFavBps": round(float(np.median(fav_bps)), 2) if fav_bps else None,
        "thinAbsLe5bp": int(sum(1 for x in fav_bps if abs(x) <= 5.0)),
        "bigAbsGe10bp": int(sum(1 for x in fav_bps if abs(x) >= 10.0)),
    }


def split_metrics(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    if not rows:
        return {}
    frame = pd.DataFrame(rows)
    return {str(name): metrics(group.to_dict("records")) for name, group in frame.groupby(key, sort=True)}


def trend_mode(row: pd.Series, cfg: dict[str, Any]) -> str:
    ret = float(row.get("ret_1800s_bps", np.nan))
    pos = float(row.get("pos_1800s", np.nan))
    if not np.isfinite(ret) or not np.isfinite(pos):
        return "unknown"
    trend_bps = float(cfg.get("trendRet1800Bps", 15.0))
    up_pos = float(cfg.get("upPos1800Min", 0.72))
    down_pos = float(cfg.get("downPos1800Max", 0.28))
    if ret >= trend_bps and pos >= up_pos:
        return "uptrend"
    if ret <= -trend_bps and pos <= down_pos:
        return "downtrend"
    return "range"


def filter_reason(row: pd.Series, cfg: dict[str, Any]) -> str | None:
    signal = str(row.get("signal"))
    mode = trend_mode(row, cfg)
    if cfg.get("space", False):
        if float(row["sigma_expand"]) > float(cfg.get("sigmaExpandMax", 1.6)):
            return "space_sigma_expand_high"
        if abs(float(row["center_slope_bps"])) > float(cfg.get("centerSlopeAbsMaxBps", 6.0)):
            return "space_center_slope_high"
        if float(row["inside1_ratio"]) > float(cfg.get("insideMax", 0.75)):
            return "space_inside_too_high"
    if cfg.get("blockCounterTrend", True):
        if signal == "DOWN" and mode == "uptrend":
            return "trend_block_down_in_uptrend"
        if signal == "UP" and mode == "downtrend":
            return "trend_block_up_in_downtrend"
    if cfg.get("blockUpperFadeWhenShortPullbackUp", False):
        if (
            signal == "DOWN"
            and str(row.get("reason")) == "upper_fake_break_reclaim"
            and float(row.get("ret_600s_bps", 0.0)) > float(cfg.get("shortRet600UpBps", 12.0))
            and float(row.get("pos_600s", 0.0)) > float(cfg.get("shortPos600Min", 0.65))
        ):
            return "trend_block_short_pullback_up"
    return None


def collect_events(audit: Any, dataset: str, spec: dict[str, Any]) -> tuple[list[dict[str, Any]], float]:
    c = audit.cfg()
    data = audit.load_data(Path(spec["dir"]))
    start = pd.Timestamp(spec["start"])
    end = pd.Timestamp(spec["end"])
    data = data[(data.index >= start) & (data.index < end)].copy()
    hours = (data.index.max() - data.index.min()).total_seconds() / 3600.0 if len(data) else 0.0
    features = audit.build_features(data, c.normal_window_sec, c)
    close = data["close"].astype(float)
    close_np = close.to_numpy(float)
    for sec in (60, 180, 300, 600, 900, 1800, 3600):
        features[f"ret_{sec}s_bps"] = np.log(close / close.shift(sec)) * 10000.0
    for window in (600, 1800, 3600):
        high = close.rolling(window, min_periods=max(60, window // 3)).max()
        low = close.rolling(window, min_periods=max(60, window // 3)).min()
        features[f"pos_{window}s"] = (close - low) / (high - low).replace(0, np.nan)
        features[f"range_{window}s_bps"] = (high / low - 1.0) * 10000.0
    features["bid20_chg_60"] = features["bid_qty_20"] / features["bid_qty_20"].shift(60).replace(0, np.nan) - 1.0

    events: list[dict[str, Any]] = []
    warmup = max(c.normal_window_sec, c.center_slope_sec, c.retest_sec, 900) + 10
    limit = len(data) - c.horizon_sec
    last_emit_idx = -10**12
    for idx in range(warmup, max(warmup, limit)):
        if idx - last_emit_idx < c.horizon_sec:
            continue
        row = features.iloc[idx]
        if not bool(data["ob_available"].iloc[idx]) or not audit.normal_ready(row, c):
            continue
        signal, reason = audit.signal_from_row(row, c)
        if not signal:
            continue
        raw_signal, raw_reason = signal, reason
        trap = audit.bidwall_trap(signal, reason, row)
        if trap:
            ret600 = float(row.get("ret_600s_bps", np.nan))
            if np.isfinite(ret600) and ret600 < -20.0:
                events.append({
                    "event": "bidwall_trap_extreme_drop_skip",
                    "dataset": dataset,
                    "idx": int(idx),
                    "time": data.index[idx],
                    "raw_signal": raw_signal,
                    "raw_reason": raw_reason,
                    "blocked_signal": "DOWN",
                    "ret_600s_bps": round(float(ret600), 6),
                })
                last_emit_idx = idx
                continue
            signal = "DOWN"
            reason = "lower_reclaim_bidwall_trap_flip_down"
        veto = audit.quality_v2_veto(signal, row)
        if veto:
            events.append({"event": "v2_veto", "dataset": dataset, "idx": int(idx), "time": data.index[idx]})
            last_emit_idx = idx
            continue
        entry = float(close_np[idx])
        settle = float(close_np[idx + c.horizon_sec])
        won = bool(settle > entry if signal == "UP" else settle < entry)
        fav_diff = settle - entry if signal == "UP" else entry - settle
        rec = {
            "event": "candidate",
            "dataset": dataset,
            "idx": int(idx),
            "time": data.index[idx],
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
        }
        for col in (
            "z",
            "inside1_ratio",
            "center_slope_bps",
            "sigma_bps",
            "sigma_expand",
            "flow_60",
            "imbalance_20",
            "micro_bps",
            "bid20_chg_60",
            "ret_300s_bps",
            "ret_600s_bps",
            "ret_900s_bps",
            "ret_1800s_bps",
            "ret_3600s_bps",
            "pos_600s",
            "pos_1800s",
            "pos_3600s",
            "range_600s_bps",
            "range_1800s_bps",
            "range_3600s_bps",
        ):
            value = row.get(col, np.nan)
            rec[col] = None if not np.isfinite(value) else round(float(value), 6)
        events.append(rec)
        last_emit_idx = idx
    return events, hours


def apply_variant(events: list[dict[str, Any]], variant: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    last_by_dataset: dict[str, int] = {}
    for event in sorted(events, key=lambda item: (str(item["dataset"]), int(item["idx"]))):
        dataset = str(event["dataset"])
        idx = int(event["idx"])
        if event.get("event") != "candidate":
            last_by_dataset[dataset] = idx
            continue
        if idx - last_by_dataset.get(dataset, -10**12) < 600:
            continue
        reason = filter_reason(pd.Series(event), variant)
        if reason:
            skipped.append(
                {
                    "variant": variant["name"],
                    "dataset": dataset,
                    "idx": idx,
                    "time": event["time"],
                    "signal": event["signal"],
                    "reason": event["reason"],
                    "skipReason": reason,
                    "wonIfTaken": event["won"],
                    "favBpsIfTaken": event["fav_bps"],
                }
            )
            last_by_dataset[dataset] = idx
            continue
        row = dict(event)
        row["variant"] = variant["name"]
        row["trend_mode"] = trend_mode(pd.Series(event), variant)
        rows.append(row)
        last_by_dataset[dataset] = idx
    return rows, skipped


def run() -> dict[str, Any]:
    audit = load_audit_module()
    variants = [
        {"name": "base_v2", "blockCounterTrend": False},
        {
            "name": "space_v2",
            "space": True,
            "sigmaExpandMax": 1.6,
            "centerSlopeAbsMaxBps": 6.0,
            "insideMax": 0.75,
            "blockCounterTrend": False,
        },
        {
            "name": "trend_v1_block_counter",
            "trendRet1800Bps": 15.0,
            "upPos1800Min": 0.72,
            "downPos1800Max": 0.28,
            "blockCounterTrend": True,
        },
        {
            "name": "trend_space_v2",
            "space": True,
            "sigmaExpandMax": 1.6,
            "centerSlopeAbsMaxBps": 6.0,
            "insideMax": 0.75,
            "trendRet1800Bps": 15.0,
            "upPos1800Min": 0.72,
            "downPos1800Max": 0.28,
            "blockCounterTrend": True,
        },
        {
            "name": "trend_space_v3_pullback",
            "space": True,
            "sigmaExpandMax": 1.6,
            "centerSlopeAbsMaxBps": 6.0,
            "insideMax": 0.75,
            "trendRet1800Bps": 15.0,
            "upPos1800Min": 0.72,
            "downPos1800Max": 0.28,
            "blockCounterTrend": True,
            "blockUpperFadeWhenShortPullbackUp": True,
            "shortRet600UpBps": 12.0,
            "shortPos600Min": 0.65,
        },
    ]
    events: list[dict[str, Any]] = []
    hours_by_dataset: dict[str, float] = {}
    for name, spec in DATASETS.items():
        if not Path(spec["dir"]).exists():
            continue
        dataset_events, hours = collect_events(audit, name, spec)
        events.extend(dataset_events)
        hours_by_dataset[name] = round(hours, 2)

    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "hoursByDataset": hours_by_dataset,
        "variants": {},
    }
    all_rows: list[dict[str, Any]] = []
    for variant in variants:
        rows, skipped = apply_variant(events, variant)
        all_rows.extend(rows)
        total_hours = sum(hours_by_dataset.get(name, 0.0) for name in {str(row["dataset"]) for row in rows})
        output["variants"][variant["name"]] = {
            "filters": {k: v for k, v in variant.items() if k != "name"},
            "overall": metrics(rows),
            "tradesPerDay": round(len(rows) / max(total_hours, 1e-9) * 24.0, 2),
            "byDataset": split_metrics(rows, "dataset"),
            "bySide": split_metrics(rows, "signal"),
            "byTrendMode": split_metrics(rows, "trend_mode"),
            "skipped": {
                "count": len(skipped),
                "byReason": {str(k): len(v) for k, v in pd.DataFrame(skipped).groupby("skipReason")} if skipped else {},
                "skippedNetPnlIfTaken": int(sum(payout(bool(item["wonIfTaken"])) for item in skipped)),
            },
        }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(clean(all_rows)).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
