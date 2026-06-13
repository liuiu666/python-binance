"""Analyze live AutoJS trade audit logs.

The report settles order_done events using server-recorded price ticks. It is
designed to measure execution reality: signal -> tablet order -> expiry result.
"""
import json
import os
from bisect import bisect_left
from collections import Counter, defaultdict

OUT = "E:/codex/data"
AUDIT_FILE = os.path.join(OUT, "trade_audit.jsonl")
TICKS_FILE = os.path.join(OUT, "price_ticks.jsonl")
REPORT_FILE = os.path.join(OUT, "live_trade_audit_report.json")
DEFAULT_PAYOUT = 0.85


def payout_for_duration(duration):
    try:
        minutes = float(duration)
    except Exception:
        return DEFAULT_PAYOUT
    if minutes >= 30:
        return 0.85
    if minutes >= 10:
        return 0.80
    return DEFAULT_PAYOUT


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


def settle_status(direction, open_price, close_price):
    if close_price == open_price:
        return "tie"
    if direction == "UP":
        return "won" if close_price > open_price else "lost"
    if direction == "DOWN":
        return "won" if close_price < open_price else "lost"
    return "unknown"


def metrics(items):
    settled = [x for x in items if x.get("status") in ("won", "lost", "tie")]
    wins = [x for x in settled if x["status"] == "won"]
    losses = [x for x in settled if x["status"] == "lost"]
    ties = [x for x in settled if x["status"] == "tie"]
    pnl = 0.0
    max_loss = cur = 0
    for x in settled:
        amount = float(x.get("amount") or 0)
        if x["status"] == "won":
            pnl += amount * payout_for_duration(x.get("duration"))
            cur = 0
        elif x["status"] == "lost":
            pnl -= amount
            cur += 1
            max_loss = max(max_loss, cur)
        else:
            cur = 0
    return {
        "settled": len(settled),
        "wins": len(wins),
        "losses": len(losses),
        "ties": len(ties),
        "wr": round(len(wins) / max(1, len(wins) + len(losses)) * 100, 2),
        "pnl": round(pnl, 2),
        "max_loss": max_loss,
        "pending": len(items) - len(settled),
    }


def event_funnel(audit):
    events_by_strategy = defaultdict(Counter)
    abort_reasons = defaultdict(Counter)
    skipped_reasons = defaultdict(Counter)
    autojs_events = []
    tablet_page_pings = []
    autojs_event_names = {
        "autojs_loader_start",
        "autojs_loader_exec",
        "autojs_loader_error",
        "autojs_start",
        "autojs_heartbeat",
        "signal_tradeable",
        "signal_skipped",
        "order_attempt",
        "order_abort",
        "order_done",
    }

    for row in audit:
        event = row.get("event") or "unknown"
        if event == "tablet_page_ping" and row.get("source") != "codex_local_probe":
            tablet_page_pings.append(row)
        if event not in autojs_event_names:
            continue
        autojs_events.append(row)
        strategy = row.get("strategyId") or "unknown"
        events_by_strategy[strategy][event] += 1
        if event == "order_abort":
            abort_reasons[strategy][row.get("reason") or "unknown"] += 1
        elif event == "signal_skipped":
            skipped_reasons[strategy][row.get("reason") or "unknown"] += 1

    funnel = {}
    strategies = sorted(set(events_by_strategy) | set(abort_reasons) | set(skipped_reasons))
    for strategy in strategies:
        counts = events_by_strategy[strategy]
        tradeable = counts.get("signal_tradeable", 0)
        attempts = counts.get("order_attempt", 0)
        done = counts.get("order_done", 0)
        aborts = counts.get("order_abort", 0)
        skipped = counts.get("signal_skipped", 0)
        funnel[strategy] = {
            "signal_tradeable": tradeable,
            "signal_skipped": skipped,
            "order_attempt": attempts,
            "order_abort": aborts,
            "order_done": done,
            "attempt_rate_from_tradeable": round(attempts / tradeable * 100, 2) if tradeable else None,
            "done_rate_from_attempt": round(done / attempts * 100, 2) if attempts else None,
            "abort_reasons": dict(abort_reasons[strategy]),
            "skipped_reasons": dict(skipped_reasons[strategy]),
        }

    latest_heartbeat = None
    for row in reversed(autojs_events):
        if row.get("event") == "autojs_heartbeat":
            latest_heartbeat = row
            break

    return {
        "autojs_event_rows": len(autojs_events),
        "tablet_page_ping_rows": len(tablet_page_pings),
        "latest_tablet_page_ping": tablet_page_pings[-1] if tablet_page_pings else None,
        "funnel_by_strategy": funnel,
        "latest_heartbeat": latest_heartbeat,
        "recent_autojs_events": autojs_events[-50:],
    }


def queue_execution_summary(audit):
    attempts = [
        r for r in audit
        if r.get("event") == "order_attempt" and r.get("queueBatchId")
    ]
    done = [
        r for r in audit
        if r.get("event") == "order_done" and r.get("queueBatchId")
    ]
    batches = defaultdict(list)
    for row in attempts:
        batches[row.get("queueBatchId")].append(row)

    multi_attempt_batches = {
        k: sorted(v, key=lambda r: int(r.get("queuePosition") or 0))
        for k, v in batches.items()
        if len(v) > 1
    }
    second_rows = [
        row for rows in multi_attempt_batches.values()
        for row in rows
        if int(row.get("queuePosition") or 0) > 1
    ]
    delays_ms = [
        float(row.get("sincePreviousDoneMs"))
        for row in second_rows
        if row.get("sincePreviousDoneMs") is not None
    ]

    def pct(values, q):
        if not values:
            return None
        values = sorted(values)
        idx = min(len(values) - 1, max(0, round((len(values) - 1) * q)))
        return round(values[idx], 2)

    policies = Counter(row.get("queueOrderPolicy") or "unknown" for row in attempts)
    second_delay_sec = [v / 1000 for v in delays_ms]
    return {
        "attempt_batches": len(batches),
        "multi_order_batches": len(multi_attempt_batches),
        "attempt_rows": len(attempts),
        "done_rows": len(done),
        "second_order_attempts": len(second_rows),
        "queue_order_policies": dict(policies),
        "second_order_delay_ms": {
            "count": len(delays_ms),
            "min": round(min(delays_ms), 2) if delays_ms else None,
            "p50": pct(delays_ms, 0.5),
            "p90": pct(delays_ms, 0.9),
            "max": round(max(delays_ms), 2) if delays_ms else None,
        },
        "second_order_delay_sec": {
            "min": round(min(second_delay_sec), 2) if second_delay_sec else None,
            "p50": pct(second_delay_sec, 0.5),
            "p90": pct(second_delay_sec, 0.9),
            "max": round(max(second_delay_sec), 2) if second_delay_sec else None,
        },
        "recent_multi_order_attempts": [
            {
                "serverTime": row.get("serverTime"),
                "strategyId": row.get("strategyId"),
                "direction": row.get("direction"),
                "queueBatchId": row.get("queueBatchId"),
                "queuePosition": row.get("queuePosition"),
                "queueLength": row.get("queueLength"),
                "queueOrderPolicy": row.get("queueOrderPolicy"),
                "sincePreviousDoneMs": row.get("sincePreviousDoneMs"),
                "confidence": row.get("confidence"),
                "duration": row.get("duration"),
            }
            for row in second_rows[-20:]
        ],
    }


def main():
    audit = read_jsonl(AUDIT_FILE)
    ticks = read_jsonl(TICKS_FILE)
    ticks = [t for t in ticks if isinstance(t.get("time"), (int, float)) and isinstance(t.get("price"), (int, float))]
    ticks.sort(key=lambda x: x["time"])
    tick_times = [t["time"] for t in ticks]

    trades = []
    for row in audit:
        if row.get("event") != "order_done":
            continue
        duration = int(float(row.get("duration") or 0))
        open_time = int(row.get("serverTime") or row.get("clientTime") or 0)
        if duration <= 0 or open_time <= 0:
            continue
        open_price = row.get("price")
        if open_price is None:
            i0 = bisect_left(tick_times, open_time)
            if i0 < len(ticks):
                open_price = ticks[i0]["price"]
        settle_time = open_time + duration * 60 * 1000
        i1 = bisect_left(tick_times, settle_time)
        status = "pending"
        close_price = None
        if open_price is not None and i1 < len(ticks):
            close_price = ticks[i1]["price"]
            status = settle_status(row.get("direction"), float(open_price), float(close_price))
        item = {
            "strategyId": row.get("strategyId"),
            "direction": row.get("direction"),
            "amount": row.get("amount"),
            "duration": str(duration),
            "queueBatchId": row.get("queueBatchId"),
            "queuePosition": row.get("queuePosition"),
            "queueLength": row.get("queueLength"),
            "queueOrderPolicy": row.get("queueOrderPolicy"),
            "sincePreviousDoneMs": row.get("sincePreviousDoneMs"),
            "sinceQueueBatchStartMs": row.get("sinceQueueBatchStartMs"),
            "signalTime": row.get("signalTime"),
            "openTime": open_time,
            "settleTime": settle_time,
            "openPrice": open_price,
            "closePrice": close_price,
            "status": status,
            "confidence": row.get("confidence"),
            "rsi_value": row.get("rsi_value"),
            "avg_prob": row.get("avg_prob"),
            "threshold": row.get("threshold"),
        }
        trades.append(item)

    by_strategy = defaultdict(list)
    for t in trades:
        by_strategy[t.get("strategyId") or "unknown"].append(t)

    funnel = event_funnel(audit)
    if trades:
        readiness = "has_order_done"
    elif any(r.get("event") == "autojs_loader_error" for r in funnel["recent_autojs_events"]):
        readiness = "autojs_loader_error"
    elif any(str(r.get("event") or "").startswith("autojs_loader_") for r in funnel["recent_autojs_events"]):
        readiness = "loader_seen_waiting_for_autojs"
    elif funnel["autojs_event_rows"]:
        readiness = "has_autojs_events"
    elif funnel.get("tablet_page_ping_rows"):
        readiness = "tablet_page_seen_waiting_for_autojs"
    else:
        readiness = "waiting_for_autojs_events"
    report = {
        "audit_rows": len(audit),
        "price_ticks": len(ticks),
        "trades": len(trades),
        "overall": metrics(trades),
        "by_strategy": {k: metrics(v) for k, v in sorted(by_strategy.items())},
        "execution_funnel": funnel,
        "queue_execution": queue_execution_summary(audit),
        "readiness": readiness,
        "recent": trades[-50:],
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
