from __future__ import annotations

from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


TIMESTAMP_CANDIDATES = ("timestamp", "ts", "time", "open_time")
PRICE_CANDIDATES = ("close", "price")


def _first_existing(columns: Iterable[str], candidates: Iterable[str]) -> str | None:
    cols = set(columns)
    return next((name for name in candidates if name in cols), None)


def _read_csv_header(path: Path) -> list[str]:
    return list(pd.read_csv(path, nrows=0).columns)


def audit_second_csv(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {"exists": False, "file": str(path)}
    try:
        columns = _read_csv_header(path)
        ts_col = _first_existing(columns, TIMESTAMP_CANDIDATES)
        if not ts_col:
            return {
                "exists": True,
                "file": str(path),
                "error": f"no timestamp column in {columns}",
            }
        full = pd.read_csv(path, usecols=[ts_col])
        ts = pd.to_datetime(full[ts_col], utc=True, errors="coerce").dropna()
        if ts.empty:
            return {
                "exists": True,
                "file": str(path),
                "rows": int(len(full)),
                "error": "empty timestamp",
            }
        rounded = ts.dt.floor("s").sort_values()
        unique = rounded.drop_duplicates()
        expected = pd.date_range(unique.iloc[0], unique.iloc[-1], freq="s", tz="UTC")
        missing = max(len(expected) - len(unique), 0)
        gaps = unique.diff().dt.total_seconds().dropna()
        return {
            "exists": True,
            "file": str(path),
            "rows": int(len(full)),
            "uniqueSlots": int(len(unique)),
            "start": unique.iloc[0].isoformat(),
            "end": unique.iloc[-1].isoformat(),
            "missingSlots": int(missing),
            "missingPct": round(100.0 * missing / max(len(expected), 1), 4),
            "duplicateSlots": int(len(rounded) - len(unique)),
            "maxGapSec": int(gaps.max()) if len(gaps) else 0,
        }
    except Exception as exc:
        return {"exists": True, "file": str(path), "error": str(exc)}


def load_second_bars(path: str | Path) -> pd.DataFrame:
    """Load trade/1s CSV into a dense UTC second index.

    Missing seconds keep the last close and zero volume. The `observed` column
    marks whether that second existed in the source CSV before reindexing.
    """

    path = Path(path)
    df = pd.read_csv(path)
    ts_col = _first_existing(df.columns, TIMESTAMP_CANDIDATES)
    price_col = _first_existing(df.columns, PRICE_CANDIDATES)
    if not ts_col or not price_col:
        raise ValueError(f"{path} must contain timestamp/price columns")

    df = df.rename(columns={ts_col: "time", price_col: "price"})
    df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")

    if "qty" in df.columns:
        df["qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0.0)
    elif "volume" in df.columns:
        df["qty"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    else:
        df["qty"] = 0.0

    if "taker_buy_volume" in df.columns or "taker_sell_volume" in df.columns:
        df["buy_qty"] = pd.to_numeric(
            df.get("taker_buy_volume", 0.0), errors="coerce"
        ).fillna(0.0)
        df["sell_qty"] = pd.to_numeric(
            df.get("taker_sell_volume", 0.0), errors="coerce"
        ).fillna(0.0)
    elif "isBuyerMaker" in df.columns:
        maker_sell = df["isBuyerMaker"].astype(str).str.lower().isin(("true", "1"))
        df["sell_qty"] = df["qty"].where(maker_sell, 0.0)
        df["buy_qty"] = df["qty"].where(~maker_sell, 0.0)
    else:
        df["buy_qty"] = df["qty"] * 0.5
        df["sell_qty"] = df["qty"] * 0.5

    raw = df.dropna(subset=["time", "price"]).sort_values("time")
    if raw.empty:
        raise ValueError(f"{path} has no usable rows")

    raw["second"] = raw["time"].dt.floor("s")
    bars = raw.groupby("second").agg(
        close=("price", "last"),
        volume=("qty", "sum"),
        buy_qty=("buy_qty", "sum"),
        sell_qty=("sell_qty", "sum"),
    )
    bars["observed"] = True

    idx = pd.date_range(bars.index.min(), bars.index.max(), freq="s", tz="UTC")
    dense = bars.reindex(idx)
    dense["observed"] = dense["observed"].fillna(False).astype(bool)
    dense["close"] = dense["close"].ffill()
    for col in ("volume", "buy_qty", "sell_qty"):
        dense[col] = dense[col].fillna(0.0)
    dense = dense.dropna(subset=["close"])
    dense.index.name = "time"

    for col in ("close", "volume", "buy_qty", "sell_qty"):
        dense[col] = dense[col].astype(float)
    dense["net_buy_qty"] = dense["buy_qty"] - dense["sell_qty"]
    buy = dense["buy_qty"].to_numpy(float)
    sell = dense["sell_qty"].to_numpy(float)
    ratio = np.full(len(dense), np.inf, dtype=float)
    np.divide(buy, sell, out=ratio, where=sell > 0)
    dense["buy_sell_ratio"] = ratio
    return dense
