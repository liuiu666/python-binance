"""Analyze persisted signal opportunities.

This reports the strategy's shadow performance independent of tablet execution:
when a signal appears, settle it after its binary-option duration using BTC 1m
close data. Compare this with live_trade_audit_report.json to separate model
edge from execution edge.
"""
import json
import os
from collections import Counter, defaultdict

import pandas as pd

OUT = "E:/codex/data"
SIGNAL_AUDIT_FILE = os.path.join(OUT, "signal_audit.jsonl")
BTC_1M_FILE = os.path.join(OUT, "btcusdt_1m.csv")
REPORT_FILE = os.path.join(OUT, "signal_audit_report.json")
PAYOUT = 0.85
DEFAULT_STAKE = 5


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                rows.append({"raw": line})
    return rows


def load_price_series():
    df = pd.read_csv(BTC_1M_FILE, parse_dates=["open_time"])
    df = df.sort_values("open_time").reset_index(drop=True)
    times = pd.to_datetime(df["open_time"], utc=True)
    prices = df["close"].astype(float).to_numpy()
    return times, prices


def settle(direction, open_price, close_price):
    if close_price == open_price:
        return "tie"
    if direction == "UP":
        return "won" if close_price > open_price else "lost"
    if direction == "DOWN":
        return "won" if close_price < open_price else "lost"
    return "unknown"


def max_loss_streak(statuses):
    best = cur = 0
    for s in statuses:
        if s == "lost":
            cur += 1
            best = max(best, cur)
        elif s in ("won", "tie"):
            cur = 0
    return best


def metrics(items):
    settled = [x for x in items if x.get("status") in ("won", "lost", "tie")]
    wins = sum(1 for x in settled if x["status"] == "won")
    losses = sum(1 for x in settled if x["status"] == "lost")
    ties = sum(1 for x in settled if x["status"] == "tie")
    pnl = 0.0
    for x in settled:
        amount = float(x.get("amount") or DEFAULT_STAKE)
        if x["status"] == "won":
            pnl += amount * PAYOUT
        elif x["status"] == "lost":
            pnl -= amount
    return {
        "settled": len(settled),
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "wr": round(wins / max(1, wins + losses) * 100, 2),
        "pnl": round(pnl, 2),
        "max_loss": max_loss_streak([x.get("status") for x in settled]),
        "pending": len(items) - len(settled),
    }


def reason_for(row):
    if row.get("signal"):
        return "signal"
    reasons = []
    if not row.get("agree", True):
        reasons.append("model_split")
    if not row.get("high_conf", True):
        reasons.append("low_conf")
    if not row.get("rsi_extreme", True):
        reasons.append("rsi_not_extreme")
    if row.get("vol_ok") is False:
        reasons.append("vol_filter")
    if row.get("session_ok") is False:
        reasons.append("session_blocked")
    return "+".join(reasons) or "no_signal"


def dedupe_rows(raw_rows):
    by_key = {}
    for r in raw_rows:
        key = (r.get("strategy_id") or r.get("label"), r.get("time"))
        old = by_key.get(key)
        if old is None:
            by_key[key] = r
            continue
        old_score = (1 if old.get("actionable_time") else 0, old.get("serverTime") or 0)
        new_score = (1 if r.get("actionable_time") else 0, r.get("serverTime") or 0)
        if new_score >= old_score:
            by_key[key] = r
    return list(by_key.values())


def analyze_rows(rows, times, prices):
    trades = []
    reason_counts = defaultdict(Counter)

    for row in rows:
        strategy = row.get("strategy_id") or row.get("label") or "unknown"
        reason_counts[strategy][reason_for(row)] += 1
        if not row.get("signal"):
            continue
        signal_time = pd.to_datetime(row.get("time"), utc=True)
        duration = int(float(row.get("duration") or row.get("interval_min") or 0))
        if duration <= 0:
            continue
        open_price = float(row.get("price"))
        # Prefer explicit actionable_time from the signal service. Older audit
        # rows do not have it, so fall back to candle start + 5m.
        entry_time = pd.to_datetime(row.get("actionable_time"), utc=True) if row.get("actionable_time") else signal_time + pd.Timedelta(minutes=5)
        expiry = entry_time + pd.Timedelta(minutes=duration)
        idx = times.searchsorted(expiry, side="left")
        status = "pending"
        close_price = None
        if idx < len(prices):
            close_price = float(prices[idx])
            status = settle(row.get("signal"), open_price, close_price)
        trades.append({
            "strategyId": strategy,
            "direction": row.get("signal"),
            "duration": str(duration),
            "signalTime": str(signal_time),
            "entryTime": str(entry_time),
            "expiryTime": str(expiry),
            "openPrice": open_price,
            "closePrice": close_price,
            "status": status,
            "confidence": row.get("confidence"),
            "avg_prob": row.get("avg_prob"),
            "rsi_value": row.get("rsi_value"),
            "threshold": row.get("threshold"),
            "amount": row.get("amount") or DEFAULT_STAKE,
        })

    by_strategy = defaultdict(list)
    for trade in trades:
        by_strategy[trade["strategyId"]].append(trade)

    return {
        "signal_snapshots": len(rows),
        "tradeable_signals": len(trades),
        "overall": metrics(trades),
        "by_strategy": {k: metrics(v) for k, v in sorted(by_strategy.items())},
        "reason_counts": {k: dict(v) for k, v in sorted(reason_counts.items())},
        "recent_tradeable": trades[-50:],
    }


def main():
    all_audit_rows = read_jsonl(SIGNAL_AUDIT_FILE)
    raw_rows = [r for r in all_audit_rows if r.get("event") == "signal_snapshot"]
    shadow_raw_rows = [r for r in all_audit_rows if r.get("event") == "shadow_candidate"]
    rows = dedupe_rows(raw_rows)
    shadow_rows = dedupe_rows(shadow_raw_rows)
    times, prices = load_price_series()
    report = analyze_rows(rows, times, prices)
    report["raw_signal_snapshots"] = len(raw_rows)
    report["deduped_snapshots"] = len(raw_rows) - len(rows)
    shadow_report = analyze_rows(shadow_rows, times, prices)
    shadow_report["raw_signal_snapshots"] = len(shadow_raw_rows)
    shadow_report["deduped_snapshots"] = len(shadow_raw_rows) - len(shadow_rows)
    report["shadow_candidates"] = shadow_report
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
