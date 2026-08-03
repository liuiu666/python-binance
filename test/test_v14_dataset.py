from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from v14_dataset import PairedSource, discover_paired_sources, load_canonical_dataset


def _write_pair(
    folder: Path,
    trade_rows: list[dict],
    book_rows: list[dict],
) -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    trades = folder / "btcusdt_1s_trades.csv"
    orderbook = folder / "btcusdt_orderbook_1s.csv"
    pd.DataFrame(trade_rows).to_csv(trades, index=False)
    pd.DataFrame(book_rows).to_csv(orderbook, index=False)
    return trades, orderbook


def _trades(seconds: list[int], *, base_price: float) -> list[dict]:
    start = pd.Timestamp("2026-01-01T00:00:00Z")
    return [
        {
            "timestamp": start + pd.Timedelta(seconds=second),
            "last_trade_time": start + pd.Timedelta(seconds=second, milliseconds=900),
            "open": base_price + second,
            "high": base_price + second + 1,
            "low": base_price + second - 1,
            "close": base_price + second,
            "volume": 1.0,
            "taker_buy_volume": 0.6,
            "taker_sell_volume": 0.4,
        }
        for second in seconds
    ]


def _books(seconds: list[int], *, base_mid: float) -> list[dict]:
    start = pd.Timestamp("2026-01-01T00:00:00Z")
    return [
        {
            "timestamp": start + pd.Timedelta(seconds=second),
            "event_time": start + pd.Timedelta(seconds=second, milliseconds=500),
            "mid": base_mid + second,
            "imbalance_20": second / 10.0,
        }
        for second in seconds
    ]


def test_overlapping_sources_choose_priority_once_per_second(tmp_path: Path):
    first_trades, first_book = _write_pair(
        tmp_path / "first",
        _trades([0, 1, 2], base_price=100.0),
        _books([0, 1, 2], base_mid=100.0),
    )
    second_trades, second_book = _write_pair(
        tmp_path / "second",
        _trades([1, 2, 3], base_price=1000.0),
        _books([1, 2, 3], base_mid=1000.0),
    )

    result = load_canonical_dataset([
        PairedSource("preferred", first_trades, first_book, priority=0),
        PairedSource("fallback", second_trades, second_book, priority=10),
    ])

    assert result.frame.index.is_unique
    assert len(result.frame) == 4
    assert result.frame.iloc[1]["source"] == "preferred"
    assert result.frame.iloc[1]["close"] == 101.0
    assert result.frame.iloc[3]["source"] == "fallback"
    assert result.audit["overlapSecondsAcrossSources"] == 2
    assert result.audit["duplicateRowsDroppedAcrossSources"] == 2
    assert result.audit["selectedRowsBySource"] == {"preferred": 3, "fallback": 1}
    assert len(result.blocks) == 1


def test_only_exact_pairs_survive_and_missing_seconds_split_blocks(tmp_path: Path):
    trades, book = _write_pair(
        tmp_path / "partial",
        _trades([0, 1, 2, 4], base_price=100.0),
        _books([0, 2, 3, 4], base_mid=100.0),
    )
    result = load_canonical_dataset([PairedSource("partial", trades, book, priority=0)])

    expected = pd.DatetimeIndex([
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:02Z",
        "2026-01-01T00:00:04Z",
    ])
    pd.testing.assert_index_equal(result.frame.index, expected.rename("time"))
    source_audit = result.audit["sources"][0]
    assert source_audit["tradeOnlySeconds"] == 1
    assert source_audit["orderbookOnlySeconds"] == 1
    assert result.audit["coverage"]["missingSeconds"] == 2
    assert result.audit["coverage"]["gapCount"] == 2
    assert result.audit["coverage"]["maxMissingRunSec"] == 1
    assert len(result.blocks) == 3


def test_same_second_uses_latest_event_then_last_file_row(tmp_path: Path):
    start = pd.Timestamp("2026-01-01T00:00:00Z")
    trade_rows = [
        {
            "timestamp": start + pd.Timedelta(milliseconds=100),
            "last_trade_time": start + pd.Timedelta(milliseconds=400),
            "close": 100.0,
            "volume": 1.0,
        },
        {
            "timestamp": start + pd.Timedelta(milliseconds=200),
            "last_trade_time": start + pd.Timedelta(milliseconds=900),
            "close": 101.0,
            "volume": 1.0,
        },
    ]
    book_rows = [
        {"timestamp": start, "event_time": start + pd.Timedelta(milliseconds=200), "mid": 100.0},
        {"timestamp": start, "event_time": start + pd.Timedelta(milliseconds=800), "mid": 101.0},
    ]
    trades, book = _write_pair(tmp_path / "duplicates", trade_rows, book_rows)
    result = load_canonical_dataset([PairedSource("duplicates", trades, book, priority=0)])

    assert len(result.frame) == 1
    assert result.frame.iloc[0]["close"] == 101.0
    assert result.frame.iloc[0]["mid"] == 101.0
    assert result.audit["sources"][0]["trade"]["duplicateRowsWithinSource"] == 1
    assert result.audit["sources"][0]["orderbook"]["duplicateRowsWithinSource"] == 1


def test_duplicate_snapshot_cannot_add_opportunity_seconds(tmp_path: Path):
    first = _write_pair(
        tmp_path / "snapshot_a",
        _trades([0, 1, 2, 3], base_price=100.0),
        _books([0, 1, 2, 3], base_mid=100.0),
    )
    duplicate = _write_pair(
        tmp_path / "snapshot_b",
        _trades([0, 1, 2, 3], base_price=100.0),
        _books([0, 1, 2, 3], base_mid=100.0),
    )
    one = load_canonical_dataset([PairedSource("a", *first, priority=0)])
    two = load_canonical_dataset([
        PairedSource("a", *first, priority=0),
        PairedSource("b", *duplicate, priority=1),
    ])

    assert list(one.frame.index) == list(two.frame.index)
    assert len(one.frame) == len(two.frame) == 4
    assert two.audit["candidatePairedRows"] == 8
    assert two.audit["canonicalRows"] == 4
    assert two.audit["duplicateRowsDroppedAcrossSources"] == 4


def test_discovery_order_freezes_root_then_lexical_priority(tmp_path: Path):
    data_root = tmp_path / "data"
    tmp_root = tmp_path / "tmp"
    _write_pair(data_root / "z", _trades([0], base_price=100.0), _books([0], base_mid=100.0))
    _write_pair(tmp_root / "a", _trades([1], base_price=100.0), _books([1], base_mid=100.0))

    sources = discover_paired_sources([data_root, tmp_root])

    assert [source.priority for source in sources] == [0, 1]
    assert sources[0].name.startswith("data/")
    assert sources[1].name.startswith("tmp/")
