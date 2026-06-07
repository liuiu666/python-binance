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
PRICE_TICKS_FILE = os.path.join(OUT, "price_ticks.jsonl")
REPORT_FILE = os.path.join(OUT, "signal_audit_report.json")
PAYOUT = 0.85
DEFAULT_STAKE = 5
TREND_EPS = 0.00005
DERIVED_MODEL_REPLAY_CANDIDATES = [
    {
        "id": "SHADOW_10m_ctcool_t630_str30",
        "base_strategy": "BTC_10min",
        "threshold": 0.55,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "agree_mode": "majority",
        "countertrend_max_abs_trend6": 0.0030,
        "countertrend_max_strength": 30,
    },
    {
        "id": "SHADOW_30m_ctcool_t625_str30",
        "base_strategy": "BTC_30min",
        "threshold": 0.58,
        "rsi_lo": 30,
        "rsi_hi": 70,
        "agree_mode": "majority",
        "countertrend_max_abs_trend6": 0.0025,
        "countertrend_max_strength": 30,
    },
]


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
    parts = []
    if os.path.exists(BTC_1M_FILE):
        df = pd.read_csv(BTC_1M_FILE, parse_dates=["open_time"])
        hist = pd.DataFrame({
            "time": pd.to_datetime(df["open_time"], utc=True),
            "price": df["close"].astype(float),
            "source": "history_1m",
        })
        parts.append(hist)
    if os.path.exists(PRICE_TICKS_FILE):
        tick_rows = []
        with open(PRICE_TICKS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    if row.get("time") is None or row.get("price") is None:
                        continue
                    tick_rows.append({
                        "time": pd.to_datetime(int(row["time"]), unit="ms", utc=True),
                        "price": float(row["price"]),
                        "source": "live_tick",
                    })
                except Exception:
                    continue
        if tick_rows:
            parts.append(pd.DataFrame(tick_rows))
    if not parts:
        raise FileNotFoundError("No BTC price data found")
    df = pd.concat(parts, ignore_index=True).dropna(subset=["time", "price"])
    # Live ticks are more precise than the historical 1m close when timestamps
    # overlap, so keep the latest row after sorting by source priority.
    df["source_priority"] = (df["source"] == "live_tick").astype(int)
    df = df.sort_values(["time", "source_priority"]).drop_duplicates("time", keep="last")
    df = df.sort_values("time").reset_index(drop=True)
    times = pd.to_datetime(df["time"], utc=True)
    prices = df["price"].astype(float).to_numpy()
    return times, prices


def trend_label(score):
    if score >= 3:
        return "strong_uptrend"
    if score <= -3:
        return "strong_downtrend"
    if score > 0:
        return "mild_uptrend"
    if score < 0:
        return "mild_downtrend"
    return "neutral"


def build_trend_lookup(times, prices):
    """Approximate the signal service trend score from historical/live prices."""
    if len(times) == 0:
        return {}
    frame = pd.DataFrame({
        "time": pd.to_datetime(times, utc=True),
        "price": prices,
    }).dropna()
    if frame.empty:
        return {}
    frame["period"] = frame["time"].dt.floor("5min")
    bars = frame.groupby("period", as_index=False).agg(close=("price", "last"))
    c = bars["close"].astype(float)
    for span in [5, 10, 20, 50]:
        bars[f"ema{span}"] = c.ewm(span=span, adjust=False).mean()
    bars["pre50"] = c / bars["ema50"] - 1
    for span in [6, 12, 30]:
        bars[f"trend{span}"] = c / c.shift(span) - 1
    up_stack = (bars["ema5"] >= bars["ema10"]) & (bars["ema10"] >= bars["ema20"]) & (bars["ema20"] >= bars["ema50"])
    down_stack = (bars["ema5"] <= bars["ema10"]) & (bars["ema10"] <= bars["ema20"]) & (bars["ema20"] <= bars["ema50"])
    bars["ema_stack"] = 0
    bars.loc[up_stack, "ema_stack"] = 1
    bars.loc[down_stack, "ema_stack"] = -1

    lookup = {}
    for row in bars.dropna(subset=["pre50", "trend6", "trend12", "trend30"]).itertuples(index=False):
        score = 0
        for name in ["trend6", "trend12", "trend30", "pre50"]:
            value = float(getattr(row, name) or 0)
            if value > TREND_EPS:
                score += 1
            elif value < -TREND_EPS:
                score -= 1
        stack = int(getattr(row, "ema_stack"))
        if stack > 0:
            score += 1
        elif stack < 0:
            score -= 1
        lookup[pd.to_datetime(row.period, utc=True)] = {
            "trend_score": int(score),
            "trend_label": trend_label(int(score)),
            "trend6": float(getattr(row, "trend6") or 0),
        }
    return lookup


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


def confidence_bins(items):
    bins = [
        ("0_20", 0, 20),
        ("20_30", 20, 30),
        ("30_40", 30, 40),
        ("40_50", 40, 50),
        ("50_plus", 50, 10**9),
    ]
    out = {}
    for name, lo, hi in bins:
        part = []
        for x in items:
            conf = x.get("confidence")
            if conf is None:
                continue
            conf = float(conf)
            if lo <= conf < hi:
                part.append(x)
        out[name] = metrics(part)
    return out


def filter_non_overlapping(trades, min_confidence=None, loss_cooldown_mult=0):
    """Simulate a safer executor policy on already-generated signal rows."""
    selected = []
    active_until_by_strategy = {}
    cooldown_until_by_strategy = {}
    for trade in sorted(trades, key=lambda x: (x.get("entryTime") or "", x.get("strategyId") or "")):
        strategy = trade.get("strategyId") or "unknown"
        entry = pd.to_datetime(trade.get("entryTime"), utc=True)
        expiry = pd.to_datetime(trade.get("expiryTime"), utc=True)
        confidence = trade.get("confidence")
        if min_confidence is not None and (confidence is None or float(confidence) < float(min_confidence)):
            continue
        if strategy in active_until_by_strategy and entry < active_until_by_strategy[strategy]:
            continue
        if strategy in cooldown_until_by_strategy and entry < cooldown_until_by_strategy[strategy]:
            continue
        selected.append(trade)
        active_until_by_strategy[strategy] = expiry
        if trade.get("status") == "lost" and loss_cooldown_mult:
            duration = pd.Timedelta(minutes=int(float(trade.get("duration") or 0)))
            cooldown_until_by_strategy[strategy] = expiry + duration * int(loss_cooldown_mult)
    return selected


def policy_metrics(trades):
    variants = {
        "all_signals": trades,
        "non_overlapping_by_strategy": filter_non_overlapping(trades),
        "non_overlap_min_conf_20": filter_non_overlapping(trades, min_confidence=20),
        "non_overlap_min_conf_30": filter_non_overlapping(trades, min_confidence=30),
        "non_overlap_min_conf_35": filter_non_overlapping(trades, min_confidence=35),
        "non_overlap_loss_cooldown_1x": filter_non_overlapping(trades, loss_cooldown_mult=1),
    }
    out = {}
    for name, items in variants.items():
        by_strategy = defaultdict(list)
        for item in items:
            by_strategy[item["strategyId"]].append(item)
        out[name] = {
            "overall": metrics(items),
            "by_strategy": {k: metrics(v) for k, v in sorted(by_strategy.items())},
        }
    return out


def grouped_metrics(trades, key_fields):
    out = {}
    for field in key_fields:
        buckets = defaultdict(list)
        for item in trades:
            buckets[str(item.get(field) or "unknown")].append(item)
        out[field] = {k: metrics(v) for k, v in sorted(buckets.items())}
    return out


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
    if row.get("countertrend_guard_ok") is False:
        reasons.append("countertrend_not_cooled")
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


def replay_model_candidate_rows(rows, trend_lookup):
    by_base = defaultdict(list)
    for row in rows:
        strategy = row.get("strategy_id") or row.get("label")
        if strategy:
            by_base[strategy].append(row)

    replay_rows = []
    for cand in DERIVED_MODEL_REPLAY_CANDIDATES:
        for row in by_base.get(cand["base_strategy"], []):
            signal_time = pd.to_datetime(row.get("time"), utc=True)
            trend = trend_lookup.get(signal_time.floor("5min"), {})
            probs = row.get("probs") or []
            if probs and len(probs) >= 3:
                probs = [float(p) for p in probs[:3]]
                up_votes = sum(1 for p in probs if p >= 0.5)
                majority_up = up_votes >= 2
                agree_all = (probs[0] >= 0.5) == (probs[1] >= 0.5) == (probs[2] >= 0.5)
            else:
                avg = float(row.get("avg_prob") or 0.5)
                majority_up = avg >= 0.5
                agree_all = bool(row.get("agree_all", row.get("agree", True)))
            avg = float(row.get("avg_prob") or 0.5)
            agree = agree_all if cand["agree_mode"] == "all3" else bool(row.get("agree", True))
            high_conf = avg >= cand["threshold"] or avg <= (1 - cand["threshold"])
            rsi_value = float(row.get("rsi_value") or 50)
            rsi_extreme = rsi_value < cand["rsi_lo"] or rsi_value > cand["rsi_hi"]
            vol_ok = row.get("vol_ok", True) is not False
            session_ok = row.get("session_ok", True) is not False

            sig = None
            confidence = None
            countertrend_guard_ok = True
            if agree and high_conf and rsi_extreme and vol_ok and session_ok:
                sig = "UP" if majority_up else "DOWN"
                confidence = round(abs(avg - 0.5) * 2 * 100, 1)
                trend_score_value = int(trend.get("trend_score") or row.get("trend_score") or 0)
                trend6_value = float(trend.get("trend6") or row.get("trend6") or 0)
                countertrend = (sig == "UP" and trend_score_value <= -3) or (sig == "DOWN" and trend_score_value >= 3)
                if countertrend:
                    if abs(trend6_value) > float(cand["countertrend_max_abs_trend6"]):
                        countertrend_guard_ok = False
                    if confidence > float(cand["countertrend_max_strength"]):
                        countertrend_guard_ok = False
                if not countertrend_guard_ok:
                    sig = None
                    confidence = None

            out = dict(row)
            out.update({
                "event": "derived_shadow_candidate",
                "strategy_id": cand["id"],
                "label": cand["id"],
                "shadow": True,
                "shadow_type": "model_replay",
                "shadow_base_strategy": cand["base_strategy"],
                "replay_source": "signal_snapshot",
                "agree_mode": cand["agree_mode"],
                "threshold": cand["threshold"],
                "rsi_extreme": rsi_extreme,
                "rsi_value": round(rsi_value, 1),
                "trend_score": trend.get("trend_score", row.get("trend_score")),
                "trend_label": trend.get("trend_label", row.get("trend_label")),
                "trend6": round(float(trend.get("trend6") or row.get("trend6") or 0), 6),
                "countertrend_guard_ok": countertrend_guard_ok,
                "countertrend_max_abs_trend6": cand["countertrend_max_abs_trend6"],
                "countertrend_max_strength": cand["countertrend_max_strength"],
                "signal": sig,
                "confidence": confidence,
                "fixed_amount": True,
                "amount": str(DEFAULT_STAKE),
            })
            replay_rows.append(out)
    return replay_rows


def analyze_rows(rows, times, prices, trend_lookup=None):
    trades = []
    reason_counts = defaultdict(Counter)
    trend_lookup = trend_lookup or {}

    for row in rows:
        strategy = row.get("strategy_id") or row.get("label") or "unknown"
        reason_counts[strategy][reason_for(row)] += 1
        if not row.get("signal"):
            continue
        signal_time = pd.to_datetime(row.get("time"), utc=True)
        trend_score_value = row.get("trend_score")
        trend_label_value = row.get("trend_label")
        if trend_score_value is None or trend_label_value is None:
            trend = trend_lookup.get(signal_time.floor("5min"))
            if trend:
                trend_score_value = trend.get("trend_score")
                trend_label_value = trend.get("trend_label")
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
            "shadowType": row.get("shadow_type") or ("model" if row.get("shadow") else None),
            "shadowBaseStrategy": row.get("shadow_base_strategy"),
            "ruleKind": row.get("rule_kind"),
            "trendScore": trend_score_value,
            "trendLabel": trend_label_value,
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
        "confidence_bins": confidence_bins(trades),
        "policy_metrics": policy_metrics(trades),
        "grouped_metrics": grouped_metrics(trades, ["shadowType", "shadowBaseStrategy", "ruleKind", "trendLabel"]),
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
    trend_lookup = build_trend_lookup(times, prices)
    report = analyze_rows(rows, times, prices, trend_lookup)
    report["raw_signal_snapshots"] = len(raw_rows)
    report["deduped_snapshots"] = len(raw_rows) - len(rows)
    shadow_report = analyze_rows(shadow_rows, times, prices, trend_lookup)
    shadow_report["raw_signal_snapshots"] = len(shadow_raw_rows)
    shadow_report["deduped_snapshots"] = len(shadow_raw_rows) - len(shadow_rows)
    report["shadow_candidates"] = shadow_report
    derived_rows = replay_model_candidate_rows(rows, trend_lookup)
    derived_report = analyze_rows(derived_rows, times, prices, trend_lookup)
    derived_report["method"] = {
        "type": "derived_shadow_replay",
        "source": "historical signal_snapshot rows",
        "note": "Replay uses already-recorded live snapshots; it is useful for screening but is not a substitute for real live shadow samples.",
    }
    derived_report["raw_signal_snapshots"] = len(derived_rows)
    report["derived_shadow_candidates"] = derived_report
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
