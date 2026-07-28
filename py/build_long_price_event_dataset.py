"""Extend causal event samples with older daily second-trade shards."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from auction_event_dataset import build_event_samples  # noqa: E402
from research_normal_shape_1m_10m import clean  # noqa: E402
from second_backtest.data import load_second_bars  # noqa: E402


SHARD_ROOT = ROOT / "tmp" / "latest_pull_20260706_2130" / "data" / "second" / "BTCUSDT" / "futures"
RECENT_EVENTS = ROOT / "tmp" / "unified_auction_events_10m.csv"
OUT_CSV = ROOT / "tmp" / "unified_long_price_events_10m.csv"
OUT_JSON = ROOT / "tmp" / "unified_long_price_events_report.json"
CUTOFF = pd.Timestamp("2026-07-05T00:00:00Z")


def load_shard(path: Path) -> pd.DataFrame:
    data = load_second_bars(path, include_shards=False).sort_index()
    if data.empty:
        return pd.DataFrame()
    spec = SimpleNamespace(name=path.stem, role="long_history")
    source = SimpleNamespace(
        spec=spec,
        data=data,
        test_start=pd.Timestamp(data.index.min()),
        test_end=pd.Timestamp(data.index.max()),
    )
    return build_event_samples(source, min_orderbook_pct=0.0)


def main() -> None:
    frames: list[pd.DataFrame] = []
    shard_rows: dict[str, int] = {}
    for path in sorted(SHARD_ROOT.glob("2026-*.csv")):
        day = pd.Timestamp(path.stem, tz="UTC")
        if day >= CUTOFF:
            continue
        frame = load_shard(path)
        shard_rows[path.stem] = len(frame)
        if not frame.empty:
            frames.append(frame)
    recent = pd.read_csv(RECENT_EVENTS, parse_dates=["time", "entry_time", "settle_time"])
    recent["price_history_tier"] = "recent_with_orderbook"
    for frame in frames:
        frame["price_history_tier"] = "older_without_orderbook"
    combined = pd.concat([*frames, recent], ignore_index=True, sort=False)
    combined = combined.sort_values("time").drop_duplicates("time", keep="last").reset_index(drop=True)
    combined.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    report = {
        "rows": len(combined),
        "start": combined.time.min(),
        "end": combined.time.max(),
        "olderRows": int((combined.price_history_tier == "older_without_orderbook").sum()),
        "recentRows": int((combined.price_history_tier == "recent_with_orderbook").sum()),
        "shards": shard_rows,
        "warning": "Older rows have second trades but no order-book snapshots; book features are intentionally missing.",
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(clean(report), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
