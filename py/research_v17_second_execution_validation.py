"""Validate V17 retrospective candidates on available futures second bars.

The minute study discovers structure.  This script keeps its profile frozen,
uses one representative futures shard per UTC day, and resolves entry plus
600-second settlement with the first observed second bar at/after each target.
It is an execution diagnostic only; the covered seconds are already historical.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from research_minute_volatility_normal_v15 import clean
from research_volatility_window_sensitivity_v17 import (
    CANDIDATES,
    INPUT,
    OUT_TABLE,
    build_volatility_states,
    load_candidates,
    remap_states,
)
from v14_validation import (
    apply_family_cooldown,
    metrics_by_delay,
    normalize_candidates,
    normalize_futures_ticks,
    resolve_candidate_trades,
    summarize_metrics,
)


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "tmp" / "minute_second_inventory_raw_20260730.json"
OUT_JSON = ROOT / "tmp" / "v17_second_execution_validation_20260730.json"
OUT_TRADES = ROOT / "tmp" / "v17_second_execution_validation_20260730_trades.csv"

DELAYS_SEC = (0, 5, 10)
HORIZON_SEC = 600
MAX_TICK_LAG_SEC = 5


def _as_utc(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def load_retrospective_candidates(path: str | Path) -> pd.DataFrame:
    table = pd.read_csv(path)
    required = {"vol_window_min", "vol_state", "profile", "retrospective_candidate"}
    missing = sorted(required - set(table.columns))
    if missing:
        raise ValueError(f"V17 stability table missing columns: {missing}")
    selected = table.loc[
        table["retrospective_candidate"].astype(str).str.lower().eq("true")
    ].copy()
    if selected.empty:
        return selected
    return selected.sort_values(
        ["vol_window_min", "vol_state", "profile"], kind="stable"
    ).reset_index(drop=True)


def _target_seconds(signal_times: Iterable[pd.Timestamp]) -> pd.DatetimeIndex:
    needed: set[pd.Timestamp] = set()
    for raw_time in signal_times:
        signal_time = _as_utc(raw_time).floor("s")
        for delay in DELAYS_SEC:
            nominal_entry = signal_time + pd.Timedelta(seconds=delay)
            for entry_lag in range(MAX_TICK_LAG_SEC + 1):
                entry_time = nominal_entry + pd.Timedelta(seconds=entry_lag)
                needed.add(entry_time)
                settle_target = entry_time + pd.Timedelta(seconds=HORIZON_SEC)
                for settle_lag in range(MAX_TICK_LAG_SEC + 1):
                    needed.add(settle_target + pd.Timedelta(seconds=settle_lag))
    return pd.DatetimeIndex(sorted(needed), tz="UTC")


def select_daily_sources(
    inventory_path: str | Path,
    target_dates: set[str],
) -> tuple[list[Path], dict[str, Any]]:
    payload = json.loads(Path(inventory_path).read_text(encoding="utf-8"))
    groups = []
    for index, group in enumerate(payload.get("groups", [])):
        representative = str(group.get("representative", ""))
        if "::" in representative:
            continue
        path = Path(representative)
        if not path.exists():
            continue
        try:
            start = _as_utc(group["start"])
            end = _as_utc(group["end"])
        except Exception:
            continue
        groups.append((index, group, path, start, end))

    selected: dict[str, tuple[tuple[float, ...], Path, dict[str, Any]]] = {}
    for date_text in sorted(target_dates):
        day_start = pd.Timestamp(f"{date_text}T00:00:00Z")
        day_end = day_start + pd.Timedelta(days=1)
        candidates = []
        for index, group, path, start, end in groups:
            if end < day_start or start >= day_end:
                continue
            exact_daily = path.stem == date_text
            overlap_start = max(start, day_start)
            overlap_end = min(end + pd.Timedelta(seconds=1), day_end)
            overlap_seconds = max(0.0, (overlap_end - overlap_start).total_seconds())
            expected = max(float(group.get("expectedSeconds") or 0.0), 1.0)
            density = float(group.get("uniqueSeconds") or 0.0) / expected
            score = (
                1.0 if exact_daily else 0.0,
                overlap_seconds,
                density,
                float(group.get("uniqueSeconds") or 0.0),
                -float(index),
            )
            candidates.append((score, path, group))
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            selected[date_text] = candidates[0]

    paths = sorted({item[1] for item in selected.values()}, key=lambda path: str(path).lower())
    audit = {
        "targetDates": sorted(target_dates),
        "coveredTargetDates": sorted(selected),
        "missingTargetDates": sorted(target_dates - set(selected)),
        "selectedSources": {
            date: {
                "path": str(item[1]),
                "start": item[2].get("start"),
                "end": item[2].get("end"),
                "uniqueSeconds": item[2].get("uniqueSeconds"),
                "sha256": item[2].get("sha256"),
            }
            for date, item in selected.items()
        },
    }
    return paths, audit


def load_needed_second_rows(
    sources: list[Path],
    needed: pd.DatetimeIndex,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    # Pandas 3 may preserve a microsecond DatetimeIndex unit.  Force both
    # sides to nanoseconds before integer membership checks.
    needed_ns = set(
        needed.to_numpy(dtype="datetime64[ns]").astype(np.int64).tolist()
    )
    retained: list[pd.DataFrame] = []
    rows_read = 0
    for source_rank, source in enumerate(sources):
        columns = list(pd.read_csv(source, nrows=0).columns)
        timestamp_column = next(
            (column for column in ("timestamp", "time", "ts", "open_time") if column in columns),
            None,
        )
        if timestamp_column is None or "close" not in columns:
            continue
        usecols = [timestamp_column, "close"]
        for optional in ("open", "market", "last_trade_time", "last_agg_trade_id"):
            if optional in columns:
                usecols.append(optional)
        row_offset = 0
        for chunk in pd.read_csv(source, usecols=usecols, chunksize=200_000, low_memory=False):
            rows_read += len(chunk)
            raw_time = pd.to_datetime(chunk[timestamp_column], utc=True, errors="coerce")
            second = raw_time.dt.floor("s")
            values_ns = second.to_numpy(dtype="datetime64[ns]").astype(np.int64)
            mask = np.fromiter((int(value) in needed_ns for value in values_ns), dtype=bool)
            if not mask.any():
                row_offset += len(chunk)
                continue
            part = chunk.loc[mask].copy()
            part["time"] = second.loc[mask].to_numpy()
            part["_raw_time"] = raw_time.loc[mask].to_numpy()
            if "last_trade_time" in part.columns:
                part["_event_time"] = pd.to_datetime(
                    part["last_trade_time"], utc=True, errors="coerce"
                ).fillna(pd.Series(part["_raw_time"], index=part.index))
            else:
                part["_event_time"] = part["_raw_time"]
            part["_source_rank"] = source_rank
            part["_row_order"] = row_offset + np.flatnonzero(mask)
            part["_source"] = str(source)
            retained.append(part)
            row_offset += len(chunk)
    if not retained:
        return pd.DataFrame(), {"rowsRead": rows_read, "retainedRows": 0}
    full = pd.concat(retained, ignore_index=True)
    full["close"] = pd.to_numeric(full["close"], errors="coerce")
    if "open" in full.columns:
        full["open"] = pd.to_numeric(full["open"], errors="coerce")
    else:
        full["open"] = full["close"]
    full = full.dropna(subset=["time", "close"])
    full = full.loc[np.isfinite(full["close"]) & full["close"].gt(0.0)]
    if "market" in full.columns:
        full = full.loc[full["market"].astype(str).str.lower().eq("futures")]
    conflicts = full.groupby("time")["close"].agg(["min", "max", "count"])
    conflicts["spread_bps"] = (conflicts["max"] / conflicts["min"] - 1.0) * 10_000.0
    full = full.sort_values(
        ["time", "_event_time", "_raw_time", "_source_rank", "_row_order"],
        kind="stable",
    ).drop_duplicates("time", keep="last")
    full = full.sort_values("time", kind="stable").reset_index(drop=True)
    audit = {
        "rowsRead": int(rows_read),
        "retainedRowsBeforeDedupe": int(sum(len(frame) for frame in retained)),
        "uniqueNeededSecondsFound": int(len(full)),
        "duplicateRowsRemoved": int(sum(len(frame) for frame in retained) - len(full)),
        "priceConflictSeconds": int(conflicts["spread_bps"].gt(0.0).sum()),
        "maxSourceCloseConflictBps": round(float(conflicts["spread_bps"].max()), 6)
        if len(conflicts)
        else 0.0,
    }
    return full, audit


def _evaluate_price_mode(
    candidates: pd.DataFrame,
    second_rows: pd.DataFrame,
    price_column: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    ticks = normalize_futures_ticks(
        second_rows,
        time_col="time",
        price_col=price_column,
        market_col=None,
        require_futures=False,
    )
    normalized = normalize_candidates(
        candidates.rename(columns={"signal_time": "time"}),
        time_col="time",
        signal_col="signal",
        family_col="profile",
        branch_col="vol_state",
    )
    cooldown = apply_family_cooldown(normalized, cooldown_sec=HORIZON_SEC)
    trades = resolve_candidate_trades(
        cooldown,
        ticks,
        delays_sec=DELAYS_SEC,
        execution_base_lag_sec=0,
        horizon_sec=HORIZON_SEC,
        amount_u=5.0,
        payout_rate=0.8,
        max_tick_lag_sec=MAX_TICK_LAG_SEC,
    )
    if trades.empty:
        return trades, {"byDelay": {}, "commonCoverageByDelay": {}}
    settled_delays = (
        trades.loc[trades["status"].isin(("won", "lost", "tie"))]
        .groupby("candidate_key")["delay_sec"]
        .nunique()
    )
    common_keys = set(settled_delays.loc[settled_delays.eq(len(DELAYS_SEC))].index)
    common = trades.loc[trades["candidate_key"].isin(common_keys)].copy()
    report = {
        "priceColumn": price_column,
        "signalsAfterCooldown": int(cooldown["time"].nunique()),
        "byDelay": metrics_by_delay(trades),
        "commonCoverageSignals": int(len(common_keys)),
        "commonCoverageByDelay": metrics_by_delay(common),
        "byUtcMonthAndDelay": {
            f"{month}|{int(delay)}": summarize_metrics(group)
            for (month, delay), group in trades.assign(
                utc_month=pd.to_datetime(trades["signal_time"], utc=True).dt.strftime("%Y-%m")
            ).groupby(["utc_month", "delay_sec"], sort=True)
        },
    }
    return trades, report


def run(
    candidate_path: str | Path,
    stability_path: str | Path,
    inventory_path: str | Path,
) -> dict[str, Any]:
    selected_profiles = load_retrospective_candidates(stability_path)
    if selected_profiles.empty:
        report = {
            "status": "NO_RETROSPECTIVE_CANDIDATE",
            "safety": {"researchOnly": True, "deploymentPerformed": False},
        }
        OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report
    if len(selected_profiles) != 1:
        raise ValueError(
            "second validation expects one frozen retrospective candidate; "
            f"got {len(selected_profiles)}"
        )
    selected = selected_profiles.iloc[0]
    window = int(selected["vol_window_min"])
    state = str(selected["vol_state"])
    profile = str(selected["profile"])

    minutes = pd.read_csv(INPUT)
    minutes["open_time"] = pd.to_datetime(minutes["open_time"], utc=True, errors="coerce")
    minutes = minutes.set_index("open_time").sort_index()
    volatility = build_volatility_states(minutes, window)
    candidates = remap_states(load_candidates(candidate_path), volatility)
    frozen = candidates.loc[
        candidates["profile"].eq(profile) & candidates["vol_state"].eq(state)
    ].copy()
    needed = _target_seconds(frozen["signal_time"])
    sources, source_audit = select_daily_sources(
        inventory_path, {timestamp.strftime("%Y-%m-%d") for timestamp in needed}
    )
    second_rows, tick_audit = load_needed_second_rows(sources, needed)
    if second_rows.empty:
        raise ValueError("no needed futures seconds were found")

    all_trades = []
    mode_reports: dict[str, Any] = {}
    for price_column in ("open", "close"):
        trades, mode_report = _evaluate_price_mode(frozen, second_rows, price_column)
        if not trades.empty:
            tagged = trades.copy()
            tagged["price_mode"] = price_column
            all_trades.append(tagged)
        mode_reports[price_column] = mode_report
    trade_output = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    trade_output.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V17_RETROSPECTIVE_CANDIDATE_SECOND_EXECUTION_DIAGNOSTIC",
        "safety": {
            "researchOnly": True,
            "tradeEnabled": False,
            "deploymentPerformed": False,
            "realTradingAllowed": False,
        },
        "candidate": {
            "volatilityWindowMin": window,
            "volatilityState": state,
            "profile": profile,
            "minuteCandidateSignals": int(len(frozen)),
            "selectionWarning": "Chosen after inspecting April-June; not a sealed holdout.",
        },
        "execution": {
            "delaysSec": list(DELAYS_SEC),
            "horizonSecAfterResolvedEntry": HORIZON_SEC,
            "maxFirstTickLagSec": MAX_TICK_LAG_SEC,
            "priceModes": ["first-second open", "first-second close"],
        },
        "sources": source_audit,
        "ticks": tick_audit,
        "results": mode_reports,
        "decision": {
            "deployment": "none",
            "note": "Sparse historical second coverage and retrospective selection permit diagnosis only.",
        },
        "outputs": {"json": str(OUT_JSON), "trades": str(OUT_TRADES)},
    }
    OUT_JSON.write_text(
        json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", default=str(CANDIDATES))
    parser.add_argument("--stability", default=str(OUT_TABLE))
    parser.add_argument("--inventory", default=str(INVENTORY))
    args = parser.parse_args()
    report = run(args.candidates, args.stability, args.inventory)
    print(
        json.dumps(
            clean(
                {
                    "candidate": report.get("candidate"),
                    "results": report.get("results"),
                    "decision": report.get("decision"),
                    "outputs": report.get("outputs"),
                }
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
