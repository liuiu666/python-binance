"""Walk-forward audit of dynamic minute-volatility regimes.

States use only trailing data: fast five-minute volatility relative to a
60-minute baseline is classified by its prior 24-hour 20/80 percentiles.
Candidate direction rules are selected on the first 60% of time only and
evaluated unchanged on the remaining 40%.
"""

from __future__ import annotations

import argparse
import json
from math import sqrt
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = (1, 3, 5, 10, 15, 30)
STATES = ("compression", "normal", "expansion")
BREAKEVEN = 5.0 / 9.0


def score(frame: pd.DataFrame, rule: str) -> dict:
    frame = frame[frame[rule] != 0]
    n = len(frame)
    if not n:
        return {"rule": rule, "trades": 0, "winRate": None, "wilsonLower": None}
    win_rate = float((frame[rule] * frame.outBps > 0.0).mean())
    z = 1.96
    denominator = 1.0 + z * z / n
    center = (win_rate + z * z / (2.0 * n)) / denominator
    half = z * sqrt((win_rate * (1.0 - win_rate) + z * z / (4.0 * n)) / n) / denominator
    return {
        "rule": rule,
        "trades": int(n),
        "winRate": round(win_rate * 100.0, 2),
        "wilsonLower": round((center - half) * 100.0, 2),
    }


def outcome(frame: pd.DataFrame) -> dict:
    if frame.empty:
        return {"trades": 0, "wins": 0, "winRate": None, "pnlU": 0.0}
    frame = frame.copy()
    frame["won"] = frame.prediction * frame.outBps > 0.0
    frame["pnlU"] = np.where(frame.won, 4.0, -5.0)
    return {
        "trades": int(len(frame)),
        "wins": int(frame.won.sum()),
        "winRate": round(float(frame.won.mean() * 100.0), 2),
        "pnlU": int(frame.pnlU.sum()),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(ROOT / "data" / "server_latest" / "btcusdt_1m.csv"))
    parser.add_argument("--output", default=str(ROOT / "tmp" / "dynamic_regime_v1.json"))
    args = parser.parse_args()
    raw = pd.read_csv(args.input)
    raw["open_time"] = pd.to_datetime(raw.open_time, utc=True)
    raw = raw.drop_duplicates("open_time").sort_values("open_time").set_index("open_time")
    close = raw.close.astype(float)
    entry = raw.open.astype(float).shift(-1)
    settle = close.shift(-11)
    out_bps = (settle / entry - 1.0) * 10000.0
    log_return = np.log(close / close.shift(1))
    fast = log_return.rolling(5, min_periods=5).std(ddof=0)
    slow = log_return.rolling(60, min_periods=60).std(ddof=0)
    ratio = fast / slow
    low = ratio.shift(1).rolling(1440, min_periods=720).quantile(0.20)
    high = ratio.shift(1).rolling(1440, min_periods=720).quantile(0.80)
    state = np.select([ratio <= low, ratio >= high], ["compression", "expansion"], default="normal")
    data = pd.DataFrame({"time": raw.index, "outBps": out_bps, "state": state}, index=raw.index)
    for width in WINDOWS:
        direction = np.sign(close / close.shift(width) - 1.0)
        data[f"mom_{width}"] = direction
        data[f"rev_{width}"] = -direction
    data = data[(raw.index.minute % 10 == 0) & out_bps.notna() & low.notna() & high.notna()].dropna()
    split_at = data.time.quantile(0.60)
    train = data[data.time <= split_at]
    test = data[data.time > split_at]
    rules = [f"{kind}_{width}" for kind in ("mom", "rev") for width in WINDOWS]
    selection = {}
    strict_frames = []
    relaxed_frames = []
    for state_name in STATES:
        train_state = train[train.state == state_name]
        candidates = sorted((score(train_state, rule) for rule in rules), key=lambda item: item["winRate"] or -1, reverse=True)
        viable = [item for item in candidates if item["trades"] >= 100 and item["wilsonLower"] is not None and item["wilsonLower"] >= BREAKEVEN * 100.0]
        selected = viable[0] if viable else None
        selection[state_name] = {"selected": selected, "topFive": candidates[:5]}
        test_state = test[test.state == state_name]
        if selected:
            frame = test_state[test_state[selected["rule"]] != 0].copy()
            frame["prediction"] = frame[selected["rule"]]
            frame["rule"] = selected["rule"]
            strict_frames.append(frame)
        relaxed = candidates[0]
        frame = test_state[test_state[relaxed["rule"]] != 0].copy()
        frame["prediction"] = frame[relaxed["rule"]]
        frame["rule"] = relaxed["rule"]
        relaxed_frames.append(frame)
    strict = pd.concat(strict_frames, ignore_index=True) if strict_frames else pd.DataFrame()
    relaxed = pd.concat(relaxed_frames, ignore_index=True)
    report = {
        "status": "dynamic_regime_walkforward_audit",
        "input": str(Path(args.input).resolve()),
        "entryModel": "signal minute close, next minute open entry, close after 10 further minutes settlement",
        "stateDefinition": "fast 5-minute volatility / slow 60-minute volatility, classified by prior 24-hour 20/80 percentiles",
        "selectionRule": "first 60% of time only; require >=100 trades and 95% Wilson lower confidence >=55.56%",
        "splitAt": split_at.isoformat(),
        "trainRows": int(len(train)),
        "testRows": int(len(test)),
        "selection": selection,
        "strictHoldout": outcome(strict),
        "relaxedHoldout": outcome(relaxed),
        "relaxedByState": {
            state_name: outcome(relaxed[relaxed.state == state_name])
            for state_name in STATES
        },
        "conclusion": "No dynamic branch passed the predeclared evidence gate. The relaxed best-in-train mapping also failed on holdout.",
    }
    output = Path(args.output)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
