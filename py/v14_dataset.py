"""Canonical paired second/order-book timeline for V14 research.

The historical data directory contains many overlapping server pulls.  Loading
each pull independently makes the same market second available more than once
and can therefore inflate the number of apparent trading opportunities.  This
module builds one deterministic UTC timeline instead:

* a source contributes a second only when both its trade bar and order-book
  snapshot are valid for that exact second;
* duplicate rows inside a source use the latest event in that second (then the
  last file row as a deterministic tie-breaker);
* duplicate seconds across sources use the lowest numeric source priority, then
  the source name;
* missing paired seconds split the output into contiguous coverage blocks.

Downstream research should iterate :meth:`CanonicalDataset.iter_blocks` so a
rolling feature window never crosses an uncovered gap.  The canonical frame has
one row per timestamp by construction, so overlapping snapshot files cannot add
trading opportunities.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import numpy as np
import pandas as pd


TRADE_FILENAME = "btcusdt_1s_trades.csv"
ORDERBOOK_FILENAME = "btcusdt_orderbook_1s.csv"
TIMESTAMP_COLUMNS = ("timestamp", "ts", "time", "open_time")
TRADE_EVENT_TIME_COLUMNS = ("last_trade_time", "event_time", "timestamp", "ts", "time")
BOOK_EVENT_TIME_COLUMNS = ("event_time", "timestamp", "ts", "time")
RESEARCH_COLUMNS = {
    # Timestamp/provenance fields needed for deterministic same-second choice.
    "timestamp", "ts", "time", "open_time", "last_trade_time", "event_time",
    "symbol", "market",
    # Futures second bars.
    "open", "high", "low", "close", "price", "volume", "qty",
    "taker_buy_volume", "taker_sell_volume",
    # Order-book features consumed by the V14 candidates.
    "bid", "ask", "mid", "spread_bps", "bid_qty_20", "ask_qty_20",
    "imbalance_5", "imbalance_20", "microprice_edge_bps",
    "bid_wall_qty", "ask_wall_qty",
}


@dataclass(frozen=True)
class PairedSource:
    """One trade/order-book snapshot pair.

    Lower ``priority`` values win when two sources contain the same UTC second.
    Callers should assign priority before inspecting strategy outcomes.  Ties are
    resolved by ``name`` so filesystem enumeration order never changes results.
    """

    name: str
    trades: Path
    orderbook: Path
    priority: int = 100

    def __post_init__(self) -> None:
        object.__setattr__(self, "trades", Path(self.trades))
        object.__setattr__(self, "orderbook", Path(self.orderbook))


@dataclass(frozen=True)
class CoverageBlock:
    """A gap-free run of exact paired seconds in the canonical timeline."""

    block_id: int
    start: pd.Timestamp
    end: pd.Timestamp
    rows: int
    gap_from_previous_sec: int | None
    source_counts: dict[str, int]

    @property
    def duration_sec(self) -> int:
        return int((self.end - self.start).total_seconds()) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "blockId": self.block_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "rows": self.rows,
            "durationSec": self.duration_sec,
            "gapFromPreviousSec": self.gap_from_previous_sec,
            "sourceCounts": dict(self.source_counts),
        }


@dataclass
class CanonicalDataset:
    """Canonical paired frame plus provenance and coverage diagnostics."""

    frame: pd.DataFrame
    blocks: tuple[CoverageBlock, ...]
    audit: dict[str, Any]

    def iter_blocks(self, min_rows: int = 1) -> Iterator[pd.DataFrame]:
        """Yield independent contiguous blocks without crossing data gaps."""

        threshold = max(1, int(min_rows))
        for block in self.blocks:
            if block.rows >= threshold:
                yield self.frame.loc[self.frame["block_id"] == block.block_id].copy()


def _first_existing(columns: Iterable[str], candidates: Iterable[str]) -> str | None:
    available = set(columns)
    return next((name for name in candidates if name in available), None)


def _finite_positive(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    return numeric.notna() & np.isfinite(numeric) & numeric.gt(0.0)


def _safe_read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    # ``low_memory=False`` avoids chunk-dependent dtype choices.  NUL padding
    # found in one archived order-book file is ignored by pandas' C parser.
    # The archived files contain many exchange sequence/id columns that are not
    # used by any V14 feature.  Limiting columns here keeps loading all unique
    # historical pulls bounded without changing timestamp/value selection.
    return pd.read_csv(
        path,
        low_memory=False,
        usecols=lambda column: column in RESEARCH_COLUMNS,
    )


def _normalize_timestamp_rows(
    raw: pd.DataFrame,
    *,
    path: Path,
    event_time_candidates: Sequence[str],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    timestamp_column = _first_existing(raw.columns, TIMESTAMP_COLUMNS)
    if timestamp_column is None:
        raise ValueError(f"{path} has no timestamp column; columns={list(raw.columns)}")

    out = raw.copy()
    out["_raw_timestamp"] = pd.to_datetime(out[timestamp_column], utc=True, errors="coerce")
    out["_second"] = out["_raw_timestamp"].dt.floor("s")
    event_column = _first_existing(out.columns, event_time_candidates)
    if event_column is None:
        out["_event_timestamp"] = out["_raw_timestamp"]
    else:
        event_timestamp = pd.to_datetime(out[event_column], utc=True, errors="coerce")
        out["_event_timestamp"] = event_timestamp.fillna(out["_raw_timestamp"])
    out["_row_order"] = np.arange(len(out), dtype=np.int64)

    invalid_timestamp_rows = int(out["_second"].isna().sum())
    out = out.dropna(subset=["_second"])
    valid_rows_before_dedupe = len(out)
    out = out.sort_values(
        ["_second", "_event_timestamp", "_raw_timestamp", "_row_order"],
        kind="stable",
    )
    out = out.drop_duplicates(subset=["_second"], keep="last").set_index("_second")
    out.index = pd.DatetimeIndex(out.index, tz="UTC", name="time")
    out = out.sort_index()
    return out, {
        "rowsRead": int(len(raw)),
        "invalidTimestampRows": invalid_timestamp_rows,
        "validTimestampRows": int(valid_rows_before_dedupe),
        "duplicateRowsWithinSource": int(valid_rows_before_dedupe - len(out)),
        "uniqueSecondsBeforeValueValidation": int(len(out)),
        "timestampColumn": timestamp_column,
        "eventTimeColumn": event_column,
    }


def _normalize_trades(path: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    rows, stats = _normalize_timestamp_rows(
        _safe_read_csv(path),
        path=path,
        event_time_candidates=TRADE_EVENT_TIME_COLUMNS,
    )
    price_column = _first_existing(rows.columns, ("close", "price"))
    if price_column is None:
        raise ValueError(f"{path} has no close/price column")
    valid = _finite_positive(rows[price_column])
    invalid_value_rows = int((~valid).sum())
    rows = rows.loc[valid].copy()
    rows["close"] = pd.to_numeric(rows[price_column], errors="coerce").astype(float)

    for column in ("open", "high", "low"):
        if column in rows.columns:
            value = pd.to_numeric(rows[column], errors="coerce")
            rows[column] = value.where(_finite_positive(value), rows["close"]).astype(float)
        else:
            rows[column] = rows["close"]
    if "volume" in rows.columns:
        rows["volume"] = pd.to_numeric(rows["volume"], errors="coerce").fillna(0.0).clip(lower=0.0)
    elif "qty" in rows.columns:
        rows["volume"] = pd.to_numeric(rows["qty"], errors="coerce").fillna(0.0).clip(lower=0.0)
    else:
        rows["volume"] = 0.0

    if "taker_buy_volume" in rows.columns or "taker_sell_volume" in rows.columns:
        buy_source = (
            rows["taker_buy_volume"]
            if "taker_buy_volume" in rows.columns
            else pd.Series(0.0, index=rows.index)
        )
        sell_source = (
            rows["taker_sell_volume"]
            if "taker_sell_volume" in rows.columns
            else pd.Series(0.0, index=rows.index)
        )
        rows["buy_qty"] = pd.to_numeric(buy_source, errors="coerce").fillna(0.0)
        rows["sell_qty"] = pd.to_numeric(sell_source, errors="coerce").fillna(0.0)
    else:
        rows["buy_qty"] = rows["volume"] * 0.5
        rows["sell_qty"] = rows["volume"] * 0.5

    rows = rows.drop(columns=["_event_timestamp", "_row_order"], errors="ignore")
    stats.update({
        "invalidValueRows": invalid_value_rows,
        "usableSeconds": int(len(rows)),
        "priceColumn": price_column,
    })
    return rows, stats


def _normalize_orderbook(path: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    rows, stats = _normalize_timestamp_rows(
        _safe_read_csv(path),
        path=path,
        event_time_candidates=BOOK_EVENT_TIME_COLUMNS,
    )
    if "mid" in rows.columns:
        mid = pd.to_numeric(rows["mid"], errors="coerce")
    elif "bid" in rows.columns and "ask" in rows.columns:
        bid = pd.to_numeric(rows["bid"], errors="coerce")
        ask = pd.to_numeric(rows["ask"], errors="coerce")
        mid = (bid + ask) / 2.0
    else:
        raise ValueError(f"{path} has no mid or bid/ask columns")
    valid = _finite_positive(mid)
    invalid_value_rows = int((~valid).sum())
    rows = rows.loc[valid].copy()
    rows["mid"] = mid.loc[valid].astype(float)
    rows = rows.drop(columns=["_event_timestamp", "_row_order"], errors="ignore")
    stats.update({
        "invalidValueRows": invalid_value_rows,
        "usableSeconds": int(len(rows)),
    })
    return rows, stats


def _gap_stats(index: pd.DatetimeIndex) -> dict[str, Any]:
    if index.empty:
        return {
            "start": None,
            "end": None,
            "rows": 0,
            "expectedSeconds": 0,
            "missingSeconds": 0,
            "coveragePct": None,
            "gapCount": 0,
            "maxMissingRunSec": 0,
            "maxStepSec": 0,
        }
    ordered = pd.DatetimeIndex(index).sort_values().unique()
    steps = pd.Series(ordered).diff().dt.total_seconds().dropna().astype(int)
    missing_runs = (steps[steps > 1] - 1).astype(int)
    expected = int((ordered[-1] - ordered[0]).total_seconds()) + 1
    missing = max(0, expected - len(ordered))
    return {
        "start": ordered[0].isoformat(),
        "end": ordered[-1].isoformat(),
        "rows": int(len(ordered)),
        "expectedSeconds": expected,
        "missingSeconds": int(missing),
        "coveragePct": round(100.0 * len(ordered) / max(1, expected), 6),
        "gapCount": int(len(missing_runs)),
        "maxMissingRunSec": int(missing_runs.max()) if len(missing_runs) else 0,
        "maxStepSec": int(steps.max()) if len(steps) else 0,
    }


def _load_source(source: PairedSource) -> tuple[pd.DataFrame, dict[str, Any]]:
    trades, trade_stats = _normalize_trades(source.trades)
    orderbook, book_stats = _normalize_orderbook(source.orderbook)
    paired_index = trades.index.intersection(orderbook.index).sort_values()
    trade_only = trades.index.difference(orderbook.index)
    book_only = orderbook.index.difference(trades.index)

    paired_trades = trades.loc[paired_index].copy()
    paired_book = orderbook.loc[paired_index].copy()
    paired_trades = paired_trades.rename(columns={"_raw_timestamp": "trade_raw_timestamp"})
    paired_book = paired_book.rename(columns={"_raw_timestamp": "book_raw_timestamp"})
    paired = paired_trades.join(paired_book, how="inner", rsuffix="_book")
    paired["source"] = source.name
    paired["source_priority"] = int(source.priority)

    stats = {
        "name": source.name,
        "priority": int(source.priority),
        "tradesPath": str(source.trades),
        "orderbookPath": str(source.orderbook),
        "trade": trade_stats,
        "orderbook": book_stats,
        "pairedSeconds": int(len(paired)),
        "tradeOnlySeconds": int(len(trade_only)),
        "orderbookOnlySeconds": int(len(book_only)),
        "pairedCoverage": _gap_stats(paired.index),
    }
    return paired, stats


def _coverage_blocks(frame: pd.DataFrame) -> tuple[CoverageBlock, ...]:
    if frame.empty:
        return ()
    steps = frame.index.to_series().diff().dt.total_seconds()
    block_ids = steps.ne(1.0).cumsum().astype(int) - 1
    frame["block_id"] = block_ids.to_numpy()
    blocks: list[CoverageBlock] = []
    previous_end: pd.Timestamp | None = None
    for block_id, part in frame.groupby("block_id", sort=True):
        start = pd.Timestamp(part.index[0])
        end = pd.Timestamp(part.index[-1])
        gap = None if previous_end is None else max(0, int((start - previous_end).total_seconds()) - 1)
        blocks.append(CoverageBlock(
            block_id=int(block_id),
            start=start,
            end=end,
            rows=int(len(part)),
            gap_from_previous_sec=gap,
            source_counts={str(key): int(value) for key, value in part["source"].value_counts().items()},
        ))
        previous_end = end
    return tuple(blocks)


def load_canonical_dataset(
    sources: Sequence[PairedSource],
    *,
    min_block_seconds: int = 1,
) -> CanonicalDataset:
    """Load one non-overlapping exact-pair timeline from overlapping pulls.

    ``sources`` may overlap arbitrarily.  A lower source priority wins an exact
    timestamp collision.  The result never forward-fills either side.
    ``min_block_seconds`` can remove fragments that are too short for a research
    warm-up; removed rows and blocks remain visible in the audit.
    """

    if not sources:
        raise ValueError("at least one PairedSource is required")
    names = [source.name for source in sources]
    if len(names) != len(set(names)):
        raise ValueError("PairedSource names must be unique")

    parts: list[pd.DataFrame] = []
    source_audits: list[dict[str, Any]] = []
    for source in sources:
        part, audit = _load_source(source)
        source_audits.append(audit)
        if not part.empty:
            parts.append(part)
    if not parts:
        empty = pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="time"))
        empty["block_id"] = pd.Series(dtype="int64", index=empty.index)
        return CanonicalDataset(empty, (), {
            "sourceCount": len(sources),
            "sources": source_audits,
            "candidatePairedRows": 0,
            "canonicalRows": 0,
            "duplicateRowsDroppedAcrossSources": 0,
            "overlapSecondsAcrossSources": 0,
            "droppedShortBlockRows": 0,
            "coverage": _gap_stats(empty.index),
            "blocks": [],
        })

    candidates = pd.concat(parts, axis=0, sort=False)
    candidates = candidates.reset_index(names="time")
    candidates = candidates.sort_values(
        ["time", "source_priority", "source"],
        kind="stable",
    )
    per_second_counts = candidates.groupby("time", sort=False).size()
    overlap_seconds = int(per_second_counts.gt(1).sum())
    duplicate_rows = int((per_second_counts - 1).clip(lower=0).sum())
    canonical = candidates.drop_duplicates(subset=["time"], keep="first")
    canonical = canonical.set_index("time").sort_index()
    canonical.index = pd.DatetimeIndex(canonical.index, tz="UTC", name="time")

    initial_blocks = _coverage_blocks(canonical)
    threshold = max(1, int(min_block_seconds))
    keep_ids = {block.block_id for block in initial_blocks if block.rows >= threshold}
    dropped_short_rows = int(sum(block.rows for block in initial_blocks if block.block_id not in keep_ids))
    if dropped_short_rows:
        canonical = canonical.loc[canonical["block_id"].isin(keep_ids)].drop(columns=["block_id"]).copy()
    else:
        canonical = canonical.drop(columns=["block_id"]).copy()
    blocks = _coverage_blocks(canonical)

    selected_counts = {str(key): int(value) for key, value in canonical["source"].value_counts().items()}
    candidate_counts = {str(key): int(value) for key, value in candidates["source"].value_counts().items()}
    audit = {
        "sourceCount": len(sources),
        "priorityRule": "lowest numeric priority, then lexical source name",
        "withinSourceSameSecondRule": "latest event timestamp, then latest raw timestamp, then last file row",
        "pairingRule": "exact UTC second intersection; no forward fill",
        "sources": source_audits,
        "candidatePairedRows": int(len(candidates)),
        "canonicalRowsBeforeBlockFilter": int(len(candidates) - duplicate_rows),
        "canonicalRows": int(len(canonical)),
        "duplicateRowsDroppedAcrossSources": duplicate_rows,
        "overlapSecondsAcrossSources": overlap_seconds,
        "droppedShortBlockRows": dropped_short_rows,
        "minBlockSeconds": threshold,
        "candidateRowsBySource": candidate_counts,
        "selectedRowsBySource": selected_counts,
        "coverage": _gap_stats(canonical.index),
        "blocks": [block.to_dict() for block in blocks],
    }
    return CanonicalDataset(canonical, blocks, audit)


def discover_paired_sources(
    roots: Sequence[str | Path],
    *,
    trade_filename: str = TRADE_FILENAME,
    orderbook_filename: str = ORDERBOOK_FILENAME,
) -> list[PairedSource]:
    """Discover sibling trade/order-book files with deterministic priorities.

    Roots listed earlier have precedence.  Within one root, relative paths are
    ordered lexically.  Callers may replace the returned priorities with a
    separately frozen source policy before loading a research dataset.
    """

    discovered: list[tuple[int, str, Path, Path]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for root_rank, raw_root in enumerate(roots):
        root = Path(raw_root)
        if not root.exists():
            continue
        for trade_path in sorted(root.rglob(trade_filename), key=lambda path: str(path).lower()):
            orderbook_path = trade_path.with_name(orderbook_filename)
            if not orderbook_path.exists():
                continue
            key = (str(trade_path.resolve()), str(orderbook_path.resolve()))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            try:
                relative_parent = trade_path.parent.relative_to(root)
                relative = str(relative_parent) if str(relative_parent) != "." else "root"
            except ValueError:
                relative = str(trade_path.parent)
            name = f"{root.name}/{relative}".replace("\\", "/")
            discovered.append((root_rank, name, trade_path, orderbook_path))

    discovered.sort(key=lambda item: (item[0], item[1].lower(), str(item[2]).lower()))
    return [
        PairedSource(name=name, trades=trades, orderbook=book, priority=priority)
        for priority, (_, name, trades, book) in enumerate(discovered)
    ]


def inventory_paired_sources(sources: Sequence[PairedSource]) -> list[dict[str, Any]]:
    """Return a cheap file-level inventory without loading CSV contents."""

    rows = []
    for source in sorted(sources, key=lambda item: (item.priority, item.name)):
        rows.append({
            "name": source.name,
            "priority": int(source.priority),
            "tradesPath": str(source.trades),
            "orderbookPath": str(source.orderbook),
            "tradesBytes": source.trades.stat().st_size if source.trades.exists() else None,
            "orderbookBytes": source.orderbook.stat().st_size if source.orderbook.exists() else None,
        })
    return rows


def _json_clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_clean(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        action="append",
        dest="roots",
        help="Root to scan; may be repeated. Defaults to data then tmp.",
    )
    parser.add_argument(
        "--load",
        action="store_true",
        help="Load every discovered pair and print canonical coverage statistics.",
    )
    parser.add_argument("--min-block-seconds", type=int, default=1)
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    roots = [Path(value) for value in args.roots] if args.roots else [project_root / "data", project_root / "tmp"]
    sources = discover_paired_sources(roots)
    if args.load:
        output: dict[str, Any] = load_canonical_dataset(
            sources,
            min_block_seconds=args.min_block_seconds,
        ).audit
    else:
        output = {
            "sourceCount": len(sources),
            "priorityRule": "root argument order, then lexical relative path",
            "sources": inventory_paired_sources(sources),
        }
    print(json.dumps(_json_clean(output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
