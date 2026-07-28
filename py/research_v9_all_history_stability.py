"""Run the current online V9 strategy across local historical snapshots.

The local tmp/ folder contains many overlapping pulls. This script keeps each
snapshot separate, applies one replay path, and writes an audit table so unstable
periods are visible instead of hidden by aggregate numbers.
"""

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

from backtest_io import read_orderbook  # noqa: E402
from backtest_online_strategies_latest import replay_liquidity, variant  # noqa: E402
from current_v2_augmented_v9_core import (  # noqa: E402
    AugmentedV9Rules,
    build_confirmed_supplement_candidates,
    trailing_book_confirmation,
)
from second_backtest.data import load_second_bars  # noqa: E402


STRATEGY_ID = "BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V9"
DELAYS = (0, 5, 6, 10)
OUT_JSON = ROOT / "tmp" / "v9_all_history_stability_20260716.json"
OUT_TRADES = ROOT / "tmp" / "v9_all_history_stability_20260716_trades.csv"
OUT_INVENTORY = ROOT / "tmp" / "v9_all_history_inventory_20260716.csv"


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if pd.isna(value):
        return None
    return value


def find_config() -> dict[str, Any]:
    for path in (
        ROOT / "tmp" / "latest_server_tail_20260716" / "trade_config.json",
        ROOT / "data" / "server_latest" / "trade_config.json",
        ROOT / "data" / "trade_config.json",
    ):
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    raise FileNotFoundError("trade_config.json not found")


def parent_dataset_root(seconds: Path) -> Path:
    if seconds.parent.name == "data" or seconds.parent.name.startswith("latest_"):
        return seconds.parent
    return seconds.parent


def find_datasets() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for seconds in sorted((ROOT / "tmp").rglob("btcusdt_1s_trades.csv")):
        folder = seconds.parent
        orderbook = folder / "btcusdt_orderbook_1s.csv"
        if not orderbook.exists():
            continue
        rows.append({
            "name": str(folder.relative_to(ROOT)),
            "folder": folder,
            "seconds": seconds,
            "orderbook": orderbook,
            "secondBytes": seconds.stat().st_size,
            "orderbookBytes": orderbook.stat().st_size,
        })
    server_seconds = ROOT / "data" / "server_latest" / "btcusdt_1s_trades.csv"
    server_orderbook = ROOT / "data" / "server_latest" / "btcusdt_orderbook_1s.csv"
    if server_seconds.exists() and server_orderbook.exists():
        rows.append({
            "name": "data/server_latest",
            "folder": server_seconds.parent,
            "seconds": server_seconds,
            "orderbook": server_orderbook,
            "secondBytes": server_seconds.stat().st_size,
            "orderbookBytes": server_orderbook.stat().st_size,
        })
    return rows


def time_range(path: Path) -> tuple[pd.Timestamp | None, pd.Timestamp | None, int]:
    try:
        frame = pd.read_csv(path, usecols=lambda col: col in {"timestamp", "open_time", "time", "ts"})
        if frame.empty:
            return None, None, 0
        column = next((c for c in ("timestamp", "open_time", "time", "ts") if c in frame.columns), frame.columns[0])
        ts = pd.to_datetime(frame[column], utc=True, errors="coerce").dropna()
        if ts.empty:
            return None, None, len(frame)
        return pd.Timestamp(ts.min()), pd.Timestamp(ts.max()), len(frame)
    except Exception:
        return None, None, 0


def price_at(data: pd.DataFrame, target: pd.Timestamp) -> float | None:
    pos = int(data.index.searchsorted(target, side="left"))
    if pos >= len(data) or abs((data.index[pos] - target).total_seconds()) > 2:
        return None
    return float(data.close.iloc[pos])


def supplement_outcomes(data: pd.DataFrame, detected: pd.Timestamp, signal: str) -> dict[str, Any] | None:
    direction = 1.0 if signal == "UP" else -1.0
    out: dict[str, Any] = {}
    for delay in DELAYS:
        entry_time = detected + pd.Timedelta(seconds=1 + delay)
        settle_time = entry_time + pd.Timedelta(seconds=600)
        entry = price_at(data, entry_time)
        settle = price_at(data, settle_time)
        if entry is None or settle is None or entry <= 0.0:
            return None
        out[f"signed_bps_d{delay}"] = (settle / entry - 1.0) * 10000.0 * direction
        out[f"entry_d{delay}"] = entry
        out[f"settle_d{delay}"] = settle
    return out


def shared_cooldown(candidates: pd.DataFrame, gap_sec: int = 600) -> pd.DataFrame:
    if candidates.empty:
        return candidates
    rows: list[dict[str, Any]] = []
    last: pd.Timestamp | None = None
    for row in candidates.sort_values(["time", "priority"]).to_dict("records"):
        timestamp = pd.Timestamp(row["time"])
        if last is not None and (timestamp - last).total_seconds() < gap_sec:
            continue
        rows.append(row)
        last = timestamp
    return pd.DataFrame(rows)


def metric(frame: pd.DataFrame, delay: int, hours: float) -> dict[str, Any]:
    if frame.empty:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "winRate": None,
            "pnlU": 0.0,
            "maxDrawdownU": 0.0,
            "maxLossStreak": 0,
            "tradesPerDay": 0.0,
        }
    signed = pd.to_numeric(frame[f"signed_bps_d{delay}"], errors="coerce")
    frame = frame[signed.notna()].copy()
    signed = pd.to_numeric(frame[f"signed_bps_d{delay}"], errors="coerce")
    wins = signed > 0.0
    pnl = np.where(wins, 4.0, -5.0)
    equity = np.r_[0.0, np.cumsum(pnl)]
    drawdown = np.maximum.accumulate(equity) - equity
    streak = max_streak = 0
    for won in wins:
        streak = 0 if won else streak + 1
        max_streak = max(max_streak, streak)
    return {
        "trades": int(len(frame)),
        "wins": int(wins.sum()),
        "losses": int((~wins).sum()),
        "winRate": round(float(wins.mean()) * 100.0, 2),
        "pnlU": round(float(pnl.sum()), 2),
        "maxDrawdownU": round(float(drawdown.max()), 2),
        "maxLossStreak": int(max_streak),
        "tradesPerDay": round(len(frame) / max(hours, 1e-9) * 24.0, 2),
        "medianSignedBps": round(float(signed.median()), 3),
        "thinAbsLe3bpPct": round(float(signed.abs().le(3.0).mean()) * 100.0, 2),
    }


def replay_dataset(item: dict[str, Any], cfg_row: dict[str, Any], rules: AugmentedV9Rules) -> tuple[pd.DataFrame, dict[str, Any]]:
    bars = load_second_bars(item["seconds"], include_shards=False)
    data = bars.join(read_orderbook(item["orderbook"], bars.index, max_age_sec=3), how="left").sort_index()
    # Keep only the overlap with valid orderbook and enough forward settlement.
    ob_ok = data["ob_available"].fillna(False).astype(bool)
    if not ob_ok.any():
        return pd.DataFrame(), {"error": "no_orderbook_overlap"}
    start = max(pd.Timestamp(data.index.min()), pd.Timestamp(data.index[ob_ok.argmax()]))
    end = pd.Timestamp(data.index.max()) - pd.Timedelta(seconds=610)
    if end <= start:
        return pd.DataFrame(), {"error": "not_enough_forward_data", "start": start, "end": end}

    rows: list[dict[str, Any]] = []
    current_trades, current_counts = replay_liquidity(data, cfg_row)
    current_confirmed = 0
    for trade in current_trades:
        timestamp = pd.Timestamp(trade["time"])
        if not start <= timestamp <= end:
            continue
        book = trailing_book_confirmation(data, str(trade["signal"]), timestamp, rules)
        if not book["ok"]:
            continue
        rows.append({
            "dataset": item["name"],
            "time": timestamp,
            "signal": trade["signal"],
            "branch": "current_v2_original",
            "priority": 0,
            "reason": trade.get("reason"),
            **book,
            **{f"signed_bps_d{delay}": trade[f"signed_bps_d{delay}"] for delay in DELAYS},
        })
        current_confirmed += 1

    supplement = build_confirmed_supplement_candidates(data, rules)
    supplement_confirmed = 0
    for candidate in supplement.to_dict("records"):
        detected = pd.Timestamp(candidate["detected_time"])
        if not start <= detected <= end:
            continue
        outcome = supplement_outcomes(data, detected, str(candidate["signal"]))
        if outcome is None:
            continue
        rows.append({
            "dataset": item["name"],
            "time": detected,
            "signal": candidate["signal"],
            "branch": "exhaustion_orderbook_supplement",
            "priority": 1,
            "reason": candidate["reason"],
            "votes": candidate.get("votes"),
            **outcome,
        })
        supplement_confirmed += 1

    candidates = pd.DataFrame(rows)
    trades = shared_cooldown(candidates)
    if not trades.empty:
        trades["beijing_day"] = trades["time"].dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    audit = {
        "name": item["name"],
        "start": start,
        "end": end,
        "hours": round((end - start).total_seconds() / 3600.0, 3),
        "secondRows": int(len(data)),
        "orderbookCoveragePct": round(float(data.loc[start:end, "ob_available"].mean()) * 100.0, 4),
        "currentCounts": current_counts,
        "currentConfirmed": current_confirmed,
        "supplementConfirmed": supplement_confirmed,
        "candidateCount": int(len(candidates)),
        "tradeCount": int(len(trades)),
    }
    return trades, audit


def run() -> dict[str, Any]:
    config = find_config()
    cfg_row = variant(config, STRATEGY_ID)
    rules = AugmentedV9Rules.from_config(cfg_row)

    datasets = find_datasets()
    inventory_rows = []
    for item in datasets:
        second_start, second_end, second_rows = time_range(item["seconds"])
        ob_start, ob_end, ob_rows = time_range(item["orderbook"])
        overlap_start = max(x for x in (second_start, ob_start) if x is not None) if second_start and ob_start else None
        overlap_end = min(x for x in (second_end, ob_end) if x is not None) if second_end and ob_end else None
        overlap_hours = (overlap_end - overlap_start).total_seconds() / 3600.0 if overlap_start and overlap_end and overlap_end > overlap_start else 0.0
        inventory_rows.append({
            **{k: item[k] for k in ("name", "secondBytes", "orderbookBytes")},
            "secondRowsRaw": second_rows,
            "orderbookRowsRaw": ob_rows,
            "secondStart": second_start,
            "secondEnd": second_end,
            "orderbookStart": ob_start,
            "orderbookEnd": ob_end,
            "overlapHoursRaw": round(overlap_hours, 3),
        })
    inventory = pd.DataFrame(inventory_rows).sort_values(["overlapHoursRaw", "orderbookBytes"], ascending=False)
    inventory.to_csv(OUT_INVENTORY, index=False, encoding="utf-8-sig")

    selected = [
        item for item in datasets
        if any(row["name"] == item["name"] and row["overlapHoursRaw"] >= 6.0 for row in inventory_rows)
    ]
    all_trades: list[pd.DataFrame] = []
    audits: list[dict[str, Any]] = []
    for item in selected:
        try:
            trades, audit = replay_dataset(item, cfg_row, rules)
        except Exception as exc:
            trades, audit = pd.DataFrame(), {"name": item["name"], "error": str(exc)}
        audits.append(audit)
        if not trades.empty:
            all_trades.append(trades)

    trades_all = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    if not trades_all.empty:
        trades_all.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")

    # Aggregate per snapshot, not deduped. Overlapping pulls are diagnostics, not
    # a single all-history PnL curve.
    by_dataset = {}
    for dataset, group in trades_all.groupby("dataset") if not trades_all.empty else []:
        hours = next((a.get("hours", 1.0) for a in audits if a.get("name") == dataset), 1.0)
        by_dataset[str(dataset)] = metric(group, 6, float(hours))
    by_day = {
        str(day): metric(group, 6, 24.0)
        for day, group in trades_all.groupby("beijing_day")
    } if not trades_all.empty else {}
    by_branch = {
        str(branch): metric(group, 6, sum(float(a.get("hours", 0.0)) for a in audits if not a.get("error")))
        for branch, group in trades_all.groupby("branch")
    } if not trades_all.empty else {}

    report = {
        "method": {
            "strategyId": STRATEGY_ID,
            "serverBacktest": False,
            "amountU": 5,
            "delaySec": 6,
            "note": "Snapshots may overlap; aggregate allTrades is diagnostic, not a deduped equity curve.",
        },
        "inventoryCsv": str(OUT_INVENTORY),
        "tradesCsv": str(OUT_TRADES),
        "datasetsFound": len(datasets),
        "datasetsSelectedOverlapGe6h": len(selected),
        "audits": audits,
        "snapshotMetricsDelay6": by_dataset,
        "byDayDelay6OverlappingDiagnostic": by_day,
        "byBranchDelay6OverlappingDiagnostic": by_branch,
        "allSnapshotRowsDiagnostic": metric(
            trades_all,
            6,
            sum(float(a.get("hours", 0.0)) for a in audits if not a.get("error")),
        ) if not trades_all.empty else metric(pd.DataFrame(), 6, 1.0),
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
