"""Evaluate only post-freeze samples for the position build-up candidate."""

from __future__ import annotations

import json
import math
import hashlib
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from position_build_up_core import evaluate_snapshot  # noqa: E402
from research_position_auction_v1 import merge_context, metrics  # noqa: E402
from second_backtest.data import load_second_bars  # noqa: E402


CONFIG = ROOT / "data" / "frozen_position_build_up_v1.json"
FORWARD_FOLDER = ROOT / "tmp" / "frozen_position_forward"
EVENTS = FORWARD_FOLDER / "events_10m.csv"
OUT_JSON = ROOT / "tmp" / "frozen_position_build_up_v1_forward.json"
OUT_CSV = ROOT / "tmp" / "frozen_position_build_up_v1_forward_trades.csv"


def wilson_interval(wins: int, trades: int, z: float = 1.96) -> tuple[float, float]:
    if trades <= 0:
        return 0.0, 1.0
    probability = wins / trades
    denominator = 1.0 + z * z / trades
    center = probability + z * z / (2.0 * trades)
    spread = z * math.sqrt(probability * (1.0 - probability) / trades + z * z / (4.0 * trades * trades))
    return (center - spread) / denominator, (center + spread) / denominator


def verify_rule_fingerprint(config: dict) -> str:
    payload = {
        "strategyId": config["strategyId"],
        "frozenAt": config["frozenAt"],
        "rule": config["rule"],
        "forwardAcceptance": config["forwardAcceptance"],
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    actual = hashlib.sha256(encoded).hexdigest()
    expected = str(config.get("ruleFingerprintSha256") or "")
    if actual != expected:
        raise RuntimeError(f"frozen rule fingerprint mismatch: expected={expected} actual={actual}")
    return actual


def latest_window_status() -> dict:
    seconds_path = FORWARD_FOLDER / "btcusdt_1s_trades.csv"
    open_interest_path = FORWARD_FOLDER / "btcusdt_open_interest.csv"
    if not seconds_path.exists() or not open_interest_path.exists():
        return {"available": False, "reason": "forward_files_missing"}
    bars = load_second_bars(seconds_path, include_shards=False).sort_index()
    open_interest = pd.read_csv(open_interest_path)
    open_interest["timestamp"] = pd.to_datetime(open_interest.timestamp, utc=True, format="mixed", errors="coerce")
    open_interest = open_interest.dropna(subset=["timestamp"]).sort_values("timestamp").set_index("timestamp")
    if bars.empty or open_interest.empty:
        return {"available": False, "reason": "forward_files_empty"}
    raw_end = pd.Timestamp(bars.index.max())
    event_time = raw_end.floor("10min")
    if event_time - pd.Timedelta(seconds=300) < bars.index.min():
        return {"available": False, "reason": "price_warmup_missing"}
    close = bars["close"].astype(float)
    current = float(close.asof(event_time))
    previous = float(close.asof(event_time - pd.Timedelta(seconds=300)))
    ret_300_bps = (current / previous - 1.0) * 10000.0
    completed_bucket_time = event_time - pd.Timedelta(minutes=5)
    eligible = open_interest.loc[:completed_bucket_time]
    if eligible.empty:
        return {"available": False, "reason": "open_interest_bucket_missing"}
    latest_time = pd.Timestamp(eligible.index[-1])
    latest_value = float(eligible.iloc[-1].sumOpenInterest)
    baseline = open_interest.loc[:latest_time - pd.Timedelta(minutes=15)]
    if baseline.empty:
        return {"available": False, "reason": "open_interest_warmup_missing"}
    baseline_value = float(baseline.iloc[-1].sumOpenInterest)
    oi_change = latest_value / baseline_value - 1.0 if baseline_value > 0.0 else 0.0
    decision = evaluate_snapshot(ret_300_bps, oi_change)
    entry_time = event_time + pd.Timedelta(seconds=6)
    settle_time = entry_time + pd.Timedelta(seconds=600)
    return {
        "available": True,
        "eventTime": event_time.isoformat(),
        "rawDataEnd": raw_end.isoformat(),
        "ret300Bps": round(ret_300_bps, 6),
        "openInterestBucket": latest_time.isoformat(),
        "openInterestChange15mPct": round(oi_change * 100.0, 6),
        "signal": decision["signal"],
        "reason": decision["reason"],
        "entryTime": entry_time.isoformat(),
        "settleTime": settle_time.isoformat(),
        "settledDataAvailable": raw_end >= settle_time,
    }


def main() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    fingerprint = verify_rule_fingerprint(config)
    cutoff = pd.Timestamp(config["frozenAt"])
    if EVENTS.exists() and EVENTS.stat().st_size > 3:
        events = pd.read_csv(EVENTS, parse_dates=["time", "entry_time", "settle_time"])
    else:
        events = pd.DataFrame(columns=["time", "entry_time", "settle_time"])
    if events.empty:
        data = events.copy()
    else:
        data = merge_context(events, FORWARD_FOLDER)
    forward = data[data.time >= cutoff].copy()
    if forward.empty:
        forward["signal"] = pd.Series(dtype=object)
        forward["reason"] = pd.Series(dtype=object)
    else:
        decisions = forward.apply(
            lambda row: evaluate_snapshot(row.ret_300, row.oi_sumOpenInterest_change_15m),
            axis=1,
        )
        forward["signal"] = [item["signal"] for item in decisions]
        forward["reason"] = [item["reason"] for item in decisions]
    trades = forward[forward.signal == "UP"].copy()
    results = {f"delay{delay}s": metrics(trades, delay) for delay in (0, 5, 6, 10)}
    if trades.empty:
        robust_wins = robust_losses = latency_sensitive = 0
    else:
        outcomes = pd.DataFrame({
            f"delay{delay}s": trades[f"raw_move_bps_d{delay}"] > 0.0
            for delay in (0, 5, 6, 10)
        })
        robust_wins = int(outcomes.all(axis=1).sum())
        robust_losses = int((~outcomes).all(axis=1).sum())
        latency_sensitive = int((outcomes.nunique(axis=1) > 1).sum())
    primary = results["delay6s"]
    lower, upper = wilson_interval(primary["wins"], primary["trades"])
    gate = config["forwardAcceptance"]
    passed = (
        primary["trades"] >= gate["minTrades"]
        and primary["winRate"] >= gate["minWinRate"]
        and primary["pnlU"] >= gate["minPnlU"]
        and primary["maxDrawdownU"] <= gate["maxDrawdownU"]
        and primary["maxLossStreak"] <= gate["maxLossStreak"]
        and all(item["pnlU"] > 0.0 for item in results.values())
    )
    report = {
        "strategyId": config["strategyId"],
        "frozenAt": config["frozenAt"],
        "ruleFingerprintSha256": fingerprint,
        "postFreezeEvents": len(forward),
        "latestWindow": latest_window_status(),
        "validationProgressPct": round(min(100.0, primary["trades"] / gate["minTrades"] * 100.0), 2),
        "winRateWilson95": {"lower": round(lower * 100.0, 2), "upper": round(upper * 100.0, 2)},
        "results": results,
        "executionRobustness": {
            "robustWins": robust_wins,
            "robustLosses": robust_losses,
            "latencySensitiveTrades": latency_sensitive,
            "latencySensitivePct": round(latency_sensitive / len(trades) * 100.0, 2) if len(trades) else 0.0,
        },
        "acceptance": gate,
        "passed": passed,
        "realTradingAllowed": bool(passed and gate.get("realTradingAllowed")),
        "note": "Pre-freeze discovery trades are intentionally excluded.",
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    trades.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
