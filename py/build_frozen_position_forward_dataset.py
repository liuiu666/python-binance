"""Build post-freeze causal events locally from downloaded raw files."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from auction_event_dataset import build_event_samples  # noqa: E402
from backtest_io import read_orderbook  # noqa: E402
from second_backtest.data import load_second_bars  # noqa: E402


FOLDER = ROOT / "tmp" / "frozen_position_forward"
CONFIG = ROOT / "data" / "frozen_position_build_up_v1.json"
OUT = FOLDER / "events_10m.csv"
REPORT = FOLDER / "dataset_report.json"


def main() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    cutoff = pd.Timestamp(config["frozenAt"])
    bars = load_second_bars(FOLDER / "btcusdt_1s_trades.csv", include_shards=False)
    orderbook = read_orderbook(FOLDER / "btcusdt_orderbook_1s.csv", bars.index, max_age_sec=3)
    data = bars.join(orderbook, how="left").sort_index()
    source = SimpleNamespace(
        spec=SimpleNamespace(name="frozen_forward", role="frozen_forward"),
        data=data,
        test_start=cutoff,
        test_end=pd.Timestamp(data.index.max()),
    )
    events = build_event_samples(source)
    if events.empty:
        events = pd.DataFrame(columns=["time", "entry_time", "settle_time"])
    else:
        events = events[events.time >= cutoff].copy()
    events.to_csv(OUT, index=False, encoding="utf-8-sig")
    report = {
        "frozenAt": config["frozenAt"],
        "rawStart": data.index.min().isoformat() if not data.empty else None,
        "rawEnd": data.index.max().isoformat() if not data.empty else None,
        "completedPostFreezeEvents": len(events),
        "firstEvent": events.time.min().isoformat() if not events.empty else None,
        "lastEvent": events.time.max().isoformat() if not events.empty else None,
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
