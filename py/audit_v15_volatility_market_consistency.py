"""Audit spot/futures agreement of the causal V15 volatility states."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from research_minute_volatility_normal_v15 import STATES, build_volatility_states, load_minutes


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tmp" / "v15_spot_futures_volatility_consistency_20260730.json"


def load_spot() -> pd.DataFrame:
    parts = []
    for path in (
        ROOT / "data" / "btcusdt_1m_180d.csv",
        ROOT / "data" / "server_latest" / "btcusdt_1m.csv",
    ):
        frame = pd.read_csv(path)
        frame["open_time"] = pd.to_datetime(frame["open_time"], utc=True, errors="coerce")
        parts.append(frame)
    spot = pd.concat(parts, ignore_index=True).dropna(subset=["open_time"])
    spot = spot.sort_values("open_time").drop_duplicates("open_time", keep="last")
    return spot.set_index("open_time")


def main() -> int:
    spot = load_spot()
    futures = load_minutes(ROOT / "data" / "btcusdt_futures_1m_20260131_20260730.csv")
    spot_state = build_volatility_states(spot).add_prefix("spot_")
    futures_state = build_volatility_states(futures).add_prefix("futures_")
    joined = spot_state.join(futures_state, how="inner")
    joined = joined.loc[
        joined["spot_vol_state"].isin(STATES)
        & joined["futures_vol_state"].isin(STATES)
    ]
    confusion = pd.crosstab(
        joined["spot_vol_state"],
        joined["futures_vol_state"],
        normalize="index",
    ) * 100.0
    report = {
        "purpose": "volatility-state consistency only; no strategy outcome or direction labels",
        "overlapReadyMinutes": int(len(joined)),
        "start": joined.index.min().isoformat(),
        "end": joined.index.max().isoformat(),
        "sameStatePct": round(float(joined["spot_vol_state"].eq(joined["futures_vol_state"]).mean()) * 100.0, 4),
        "rv10mCorrelation": round(float(joined["spot_rv10m_bps"].corr(joined["futures_rv10m_bps"])), 6),
        "futuresMinusSpotMedianRvBps": round(float((joined["futures_rv10m_bps"] - joined["spot_rv10m_bps"]).median()), 6),
        "confusionPctBySpotState": {
            str(index): {str(column): round(float(value), 4) for column, value in row.items()}
            for index, row in confusion.to_dict("index").items()
        },
        "method": "both markets use their own prior-seven-day shifted 33/67 percentile thresholds",
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
