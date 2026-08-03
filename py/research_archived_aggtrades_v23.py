"""Stratified archived agg-trade validation for the frozen V22 candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

from research_actual_horizon_walkforward_v21 import load_candidates
from research_directional_candidate_v22 import CELL, PROFILE, SIGNAL, Z_THRESHOLD
from research_long_history_walkforward_v20 import OUT_CANDIDATES
from research_minute_volatility_normal_v15 import clean
from v14_validation import (
    apply_family_cooldown,
    metrics_by_delay,
    normalize_candidates,
    normalize_futures_ticks,
    resolve_candidate_trades,
    summarize_metrics,
)


ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "tmp" / "v23_aggtrades_daily_cache"
OUT_JSON = ROOT / "tmp" / "v23_archived_aggtrades_validation_20260730.json"
OUT_TICKS = ROOT / "tmp" / "v23_archived_aggtrades_filtered_ticks_20260730.csv"
OUT_TRADES = ROOT / "tmp" / "v23_archived_aggtrades_trades_20260730.csv"

BASE_URL = "https://data.binance.vision/data/futures/um/daily/aggTrades/BTCUSDT"
DELAYS_SEC = (0, 5, 10)
HORIZON_SEC = 600
MAX_TICK_LAG_SEC = 5
ARCHIVE_START = pd.Timestamp("2024-01-01T00:00:00Z")
ARCHIVE_END = pd.Timestamp("2026-01-01T00:00:00Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def select_first_signal_per_month(candidates: pd.DataFrame) -> pd.DataFrame:
    frozen = candidates.loc[
        candidates["cell"].eq(CELL)
        & candidates["profile"].eq(PROFILE)
        & candidates["signal"].eq(SIGNAL)
        & candidates["z"].ge(Z_THRESHOLD)
        & candidates["signal_time"].ge(ARCHIVE_START)
        & candidates["signal_time"].lt(ARCHIVE_END)
    ].sort_values("signal_time", kind="stable")
    selected = (
        frozen.assign(_month=frozen["signal_time"].dt.strftime("%Y-%m"))
        .drop_duplicates("_month", keep="first")
        .drop(columns="_month")
    )
    return selected.sort_values("signal_time", kind="stable").reset_index(drop=True)


def _archive_name(date_text: str) -> str:
    return f"BTCUSDT-aggTrades-{date_text}.zip"


def _download_one(date_text: str) -> dict[str, Any]:
    CACHE.mkdir(parents=True, exist_ok=True)
    name = _archive_name(date_text)
    path = CACHE / name
    url = f"{BASE_URL}/{name}"
    checksum_url = f"{url}.CHECKSUM"
    checksum_response = requests.get(checksum_url, timeout=30)
    checksum_response.raise_for_status()
    expected = checksum_response.text.strip().split()[0].lower()
    if path.exists() and sha256(path) == expected:
        return {
            "date": date_text,
            "url": url,
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": expected,
            "cached": True,
        }
    temporary = path.with_suffix(path.suffix + ".partial")
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with temporary.open("wb") as stream:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    stream.write(chunk)
    actual = sha256(temporary)
    if actual != expected:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"checksum mismatch for {date_text}: {actual} != {expected}")
    temporary.replace(path)
    return {
        "date": date_text,
        "url": url,
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": actual,
        "cached": False,
    }


def download_archives(dates: list[str], workers: int = 4) -> list[dict[str, Any]]:
    results = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(_download_one, date): date for date in dates}
        for future in as_completed(futures):
            result = future.result()
            print(
                f"archive {result['date']} {result['bytes']} "
                f"{'cached' if result['cached'] else 'downloaded'}",
                flush=True,
            )
            results.append(result)
    return sorted(results, key=lambda row: row["date"])


def _needed_ranges(selected: pd.DataFrame) -> dict[str, list[tuple[int, int]]]:
    ranges: dict[str, list[tuple[int, int]]] = {}
    for signal_time in pd.to_datetime(selected["signal_time"], utc=True):
        for delay in DELAYS_SEC:
            entry = signal_time + pd.Timedelta(seconds=delay)
            settlement = entry + pd.Timedelta(seconds=HORIZON_SEC)
            for target, extra_lag in ((entry, MAX_TICK_LAG_SEC), (settlement, 2 * MAX_TICK_LAG_SEC)):
                date_text = target.strftime("%Y-%m-%d")
                start_ms = int(target.timestamp() * 1000)
                end_ms = int((target + pd.Timedelta(seconds=extra_lag)).timestamp() * 1000)
                ranges.setdefault(date_text, []).append((start_ms, end_ms))
    return ranges


def _read_archive_ranges(
    archive: dict[str, Any],
    ranges: list[tuple[int, int]],
) -> pd.DataFrame:
    path = Path(archive["path"])
    retained = []
    with zipfile.ZipFile(path) as bundle:
        members = [name for name in bundle.namelist() if name.lower().endswith(".csv")]
        if len(members) != 1:
            raise ValueError(f"{path} expected one CSV, got {members}")
        member = members[0]
        with bundle.open(member) as stream:
            header = pd.read_csv(stream, nrows=0)
        columns = list(header.columns)
        if "transact_time" in columns:
            time_column = "transact_time"
            price_column = "price"
            id_column = "agg_trade_id" if "agg_trade_id" in columns else columns[0]
            read_kwargs: dict[str, Any] = {}
        else:
            # Older data-vision files may omit a header.
            time_column = "transact_time"
            price_column = "price"
            id_column = "agg_trade_id"
            read_kwargs = {
                "header": None,
                "names": [
                    "agg_trade_id",
                    "price",
                    "quantity",
                    "first_trade_id",
                    "last_trade_id",
                    "transact_time",
                    "is_buyer_maker",
                ],
            }
        with bundle.open(member) as stream:
            for chunk in pd.read_csv(
                stream,
                usecols=[id_column, price_column, time_column],
                chunksize=500_000,
                low_memory=False,
                **read_kwargs,
            ):
                timestamp = pd.to_numeric(chunk[time_column], errors="coerce")
                mask = pd.Series(False, index=chunk.index)
                for start_ms, end_ms in ranges:
                    mask |= timestamp.between(start_ms, end_ms, inclusive="both")
                if not mask.any():
                    continue
                part = chunk.loc[mask, [id_column, price_column, time_column]].copy()
                part.columns = ["agg_trade_id", "price", "time_ms"]
                part["archive_date"] = archive["date"]
                retained.append(part)
    return pd.concat(retained, ignore_index=True) if retained else pd.DataFrame(
        columns=["agg_trade_id", "price", "time_ms", "archive_date"]
    )


def load_filtered_ticks(
    archives: list[dict[str, Any]],
    ranges_by_date: dict[str, list[tuple[int, int]]],
) -> pd.DataFrame:
    parts = []
    for archive in archives:
        part = _read_archive_ranges(
            archive, ranges_by_date.get(str(archive["date"]), [])
        )
        print(f"filtered {archive['date']} {len(part)} agg trades", flush=True)
        if not part.empty:
            parts.append(part)
    if not parts:
        raise ValueError("no archived agg trades matched target windows")
    ticks = pd.concat(parts, ignore_index=True)
    ticks["time"] = pd.to_datetime(
        pd.to_numeric(ticks["time_ms"], errors="coerce"), unit="ms", utc=True
    )
    ticks["price"] = pd.to_numeric(ticks["price"], errors="coerce")
    ticks["agg_trade_id"] = pd.to_numeric(
        ticks["agg_trade_id"], errors="coerce"
    )
    ticks = ticks.dropna(subset=["time", "price", "agg_trade_id"])
    ticks = ticks.loc[np.isfinite(ticks["price"]) & ticks["price"].gt(0.0)]
    return ticks.sort_values(
        ["time", "agg_trade_id"], kind="stable"
    ).drop_duplicates("agg_trade_id", keep="last").reset_index(drop=True)


def run(candidate_path: str | Path, workers: int) -> dict[str, Any]:
    candidates = load_candidates(candidate_path)
    selected = select_first_signal_per_month(candidates)
    if len(selected) < 20:
        raise ValueError(f"monthly stratified sample too small: {len(selected)}")
    ranges = _needed_ranges(selected)
    dates = sorted(ranges)
    archives = download_archives(dates, workers=workers)
    ticks = load_filtered_ticks(archives, ranges)
    ticks.to_csv(OUT_TICKS, index=False, encoding="utf-8-sig")

    normalized_ticks = normalize_futures_ticks(
        ticks,
        time_col="time",
        price_col="price",
        market_col=None,
        require_futures=False,
    )
    normalized_candidates = normalize_candidates(
        selected.rename(columns={"signal_time": "time"}),
        time_col="time",
        signal_col="signal",
        family_col="profile",
        branch_col="cell",
    )
    cooldown = apply_family_cooldown(
        normalized_candidates, cooldown_sec=HORIZON_SEC
    )
    trades = resolve_candidate_trades(
        cooldown,
        normalized_ticks,
        delays_sec=DELAYS_SEC,
        execution_base_lag_sec=0,
        horizon_sec=HORIZON_SEC,
        amount_u=5.0,
        payout_rate=0.8,
        max_tick_lag_sec=MAX_TICK_LAG_SEC,
    )
    trades.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    settled_delays = (
        trades.loc[trades["status"].isin(("won", "lost", "tie"))]
        .groupby("candidate_key")["delay_sec"]
        .nunique()
    )
    common_keys = set(
        settled_delays.loc[settled_delays.eq(len(DELAYS_SEC))].index
    )
    common = trades.loc[trades["candidate_key"].isin(common_keys)].copy()
    by_delay = metrics_by_delay(trades)
    common_by_delay = metrics_by_delay(common)
    by_year_delay = {
        f"{year}|{int(delay)}": summarize_metrics(group)
        for (year, delay), group in trades.assign(
            year=pd.to_datetime(trades["signal_time"], utc=True).dt.year
        ).groupby(["year", "delay_sec"], sort=True)
    }
    delay_rows = [common_by_delay.get(str(delay)) for delay in DELAYS_SEC]
    passed = bool(
        len(common_keys) >= 20
        and all(row is not None for row in delay_rows)
        and all(
            row["settled"] >= 20
            and row["winRatePct"] is not None
            and row["winRatePct"] >= 63.0
            and row["pnlU"] > 0.0
            and row["maxDrawdownU"] <= 20.0
            and row["maxLossStreak"] <= 2
            for row in delay_rows
            if row is not None
        )
    )
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V23_STRATIFIED_ARCHIVED_AGGTRADES_VALIDATION",
        "safety": {
            "researchOnly": True,
            "tradeEnabled": False,
            "deploymentPerformed": False,
            "realTradingAllowed": False,
        },
        "candidate": {
            "cell": CELL,
            "profile": PROFILE,
            "signal": SIGNAL,
            "zThreshold": Z_THRESHOLD,
        },
        "sampling": {
            "policy": "earliest candidate in every 2024-2025 UTC calendar month, frozen before archived tick outcomes",
            "selectedSignals": int(len(selected)),
            "selectedTimes": [
                timestamp.isoformat()
                for timestamp in pd.to_datetime(selected["signal_time"], utc=True)
            ],
            "archiveDates": dates,
        },
        "archives": archives,
        "ticks": {
            "filteredAggTrades": int(len(ticks)),
            "start": ticks["time"].min(),
            "end": ticks["time"].max(),
        },
        "results": {
            "byDelay": by_delay,
            "commonCoverageSignals": int(len(common_keys)),
            "commonCoverageByDelay": common_by_delay,
            "byYearAndDelay": by_year_delay,
            "promotionStyleGatePassed": passed,
        },
        "decision": {
            "researchCandidate": passed,
            "deployment": "none",
            "realTradingAllowed": False,
        },
        "outputs": {
            "json": str(OUT_JSON),
            "ticks": str(OUT_TICKS),
            "trades": str(OUT_TRADES),
            "cache": str(CACHE),
        },
    }
    OUT_JSON.write_text(
        json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", default=str(OUT_CANDIDATES))
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    report = run(args.candidates, workers=args.workers)
    print(
        json.dumps(
            clean(
                {
                    "sampling": report["sampling"],
                    "ticks": report["ticks"],
                    "results": report["results"],
                    "decision": report["decision"],
                    "outputs": report["outputs"],
                }
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
