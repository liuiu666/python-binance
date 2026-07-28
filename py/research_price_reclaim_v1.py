"""Price-only first reclaim study after a rolling Gaussian-band excursion."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from research_price_only_deceleration_v1 import metrics, select_non_overlapping
from second_backtest.data import load_second_bars


ROOT = Path(__file__).resolve().parents[1]
WINDOW_SEC = 600
ENTRY_DELAY_SEC = 5
HORIZON_SEC = 600
EVENT_SPACING_SEC = 60
TOUCH_Z = 1.0
RECLAIM_Z = 0.85
MIN_COVERAGE = 0.95
FREEZE_AT = pd.Timestamp("2026-07-13T03:45:00Z")


def level_at(bars: pd.DataFrame, event: pd.Timestamp) -> dict | None:
    frame = bars.loc[event - pd.Timedelta(seconds=WINDOW_SEC - 1):event]
    if len(frame) != WINDOW_SEC or float(frame.observed.mean()) < MIN_COVERAGE:
        return None
    observed = frame[frame.observed]
    if len(observed) < int(WINDOW_SEC * MIN_COVERAGE):
        return None
    values = np.log(observed.close.to_numpy(float))
    center = float(values.mean())
    sigma = float(values.std(ddof=0))
    if sigma <= 0.0 or not math.isfinite(sigma):
        return None
    elapsed = (observed.index - observed.index[0]).total_seconds().to_numpy(float)
    z = (values - center) / sigma
    lower = z <= -TOUCH_Z
    upper = z >= TOUCH_Z
    lower_positions = np.flatnonzero(lower)
    upper_positions = np.flatnonzero(upper)
    current_z = float(z[-1])
    candidates = []
    if current_z >= -RECLAIM_Z and len(lower_positions):
        candidates.append((elapsed[lower_positions[-1]], "LOWER", float(z[lower_positions[-1]])))
    if current_z <= RECLAIM_Z and len(upper_positions):
        candidates.append((elapsed[upper_positions[-1]], "UPPER", float(z[upper_positions[-1]])))
    if not candidates:
        return None
    _, side, touch_z = max(candidates, key=lambda item: item[0])
    touch_positions = lower_positions if side == "LOWER" else upper_positions
    touch_elapsed = elapsed[touch_positions[-1]]
    side_sign = 1.0 if side == "LOWER" else -1.0
    def ret(seconds: int) -> float:
        pos = int(np.searchsorted(elapsed, elapsed[-1] - seconds, side="left"))
        return float((values[-1] - values[pos]) * 10000.0)
    return {
        "side": side,
        "currentZ": current_z,
        "touchZ": touch_z,
        "touchAgeSec": float(elapsed[-1] - touch_elapsed),
        "reclaimDepthZ": (current_z + TOUCH_Z) if side == "LOWER" else (TOUCH_Z - current_z),
        "signedRet10Bps": side_sign * ret(10),
        "signedRet30Bps": side_sign * ret(30),
        "signedRet60Bps": side_sign * ret(60),
        "signedSpeed10Minus60": side_sign * ret(10) / 10.0 - side_sign * ret(60) / 60.0,
        "sigmaBps": sigma * 10000.0,
        "inside1": float(np.mean(np.abs(z) <= 1.0)),
        "rangeBps": float((values.max() - values.min()) * 10000.0),
    }


def outcome(bars: pd.DataFrame, event: pd.Timestamp, side: str) -> dict | None:
    entry_time = event + pd.Timedelta(seconds=ENTRY_DELAY_SEC)
    settle_time = entry_time + pd.Timedelta(seconds=HORIZON_SEC)
    frame = bars.loc[entry_time:settle_time]
    if len(frame) != HORIZON_SEC + 1 or float(frame.observed.mean()) < MIN_COVERAGE:
        return None
    observed = frame[frame.observed]
    if len(observed) < int(HORIZON_SEC * MIN_COVERAGE):
        return None
    entry = float(observed.close.iloc[0])
    settle = float(observed.close.iloc[-1])
    move = (settle / entry - 1.0) * 10000.0
    sign = 1.0 if side == "LOWER" else -1.0
    signed = sign * move
    return {
        "settleMoveBps": move,
        "signedMoveBps": signed,
        "won": bool(signed > 0.0),
        "pnlU": 4.0 if signed > 0.0 else -5.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(ROOT / "data" / "server_latest" / "btcusdt_1s_trades.csv"))
    parser.add_argument("--output", default=str(ROOT / "tmp" / "price_reclaim_v1.json"))
    args = parser.parse_args()
    bars = load_second_bars(args.input, include_shards=False).sort_index()
    start = bars.index.min() + pd.Timedelta(seconds=WINDOW_SEC)
    end = bars.index.max() - pd.Timedelta(seconds=ENTRY_DELAY_SEC + HORIZON_SEC)
    rows = []
    for event in pd.date_range(start.ceil(f"{EVENT_SPACING_SEC}s"), end, freq=f"{EVENT_SPACING_SEC}s", tz="UTC"):
        level = level_at(bars, event)
        if level is None:
            continue
        result = outcome(bars, event, level["side"])
        if result is None:
            continue
        rows.append({"eventTime": event, **level, **result})
    data = pd.DataFrame(rows)
    selected = select_non_overlapping(data)
    selected_with_signal = selected.assign(signal=lambda x: np.where(x.side == "LOWER", "UP", "DOWN"))
    report = {
        "status": "price_only_reclaim_measurement",
        "input": str(Path(args.input).resolve()),
        "freezeAt": FREEZE_AT.isoformat(),
        "windowSec": WINDOW_SEC,
        "entryDelaySec": ENTRY_DELAY_SEC,
        "horizonSec": HORIZON_SEC,
        "touchZ": TOUCH_Z,
        "reclaimZ": RECLAIM_Z,
        "rawCandidates": int(len(data)),
        "nonOverlappingCandidates": int(len(selected)),
        "bySide": {
            side: metrics(selected_with_signal[selected_with_signal.side == side])
            for side in ("LOWER", "UPPER")
        },
        "byTouchAge": {
            str(label): metrics(group.assign(signal=lambda x: np.where(x.side == "LOWER", "UP", "DOWN")))
                for label, group in selected.assign(
                ageBand=pd.cut(selected.touchAgeSec, [-np.inf, 30, 60, 120, 300, np.inf], right=False)
            ).groupby("ageBand", observed=False)
        },
        "withoutLateReclaim": metrics(selected_with_signal[selected_with_signal.touchAgeSec < 300]),
        "lateReclaim": metrics(selected_with_signal[selected_with_signal.touchAgeSec >= 300]),
        "forward": metrics(selected_with_signal[selected_with_signal.eventTime >= FREEZE_AT]),
        "forwardBySide": {
            side: metrics(selected_with_signal[(selected_with_signal.eventTime >= FREEZE_AT) & (selected_with_signal.side == side)])
            for side in ("LOWER", "UPPER")
        },
        "warning": "Reclaim candidates overlap before the 600-second de-duplication; this is measurement, not deployment approval.",
    }
    output = Path(args.output)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    data.to_csv(output.with_name(output.stem + "_samples.csv"), index=False, encoding="utf-8-sig")
    selected.to_csv(output.with_name(output.stem + "_nonoverlap.csv"), index=False, encoding="utf-8-sig")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
