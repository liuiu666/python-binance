"""Backtest entry-price optimization for the 2m BTC 10m binary model.

Research-only. This script keeps the primary signal fixed:

    base2m generic model, threshold 0.65, block_flow_opposes

Then it simulates entry execution with 1m OHLC data:
- immediate entry at the close of the current 2m bar;
- wait for a better pullback/rebound price and enter on touch;
- wait for a better close;
- wait for a better price plus 1m candle confirmation;
- wait for a better price, otherwise enter at the timeout close;
- an oracle upper-bound that chooses the best favorable price inside the wait
  window. The oracle is not production-usable; it shows the theoretical ceiling.

All metrics use a 10-minute non-overlap execution model.
"""
import glob
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "E:/codex/py")
from research_2m_10min_binary import OUT, SYMBOL, BAR_MIN, OPTION_MIN, HORIZON, BREAKEVEN_WR, load_1m, metric
from research_regime_models_2m import prepare_frame

REPORT_FILE = os.path.join(OUT, "entry_price_optimization_2m_report.json")
THRESHOLD = 0.65
STAKE = 5


def latest_signal_cache():
    paths = sorted(
        glob.glob(os.path.join(OUT, "cache", "regime_models_2m_10m_tr12000_te1500_st1500_*.npz")),
        key=os.path.getmtime,
        reverse=True,
    )
    if not paths:
        raise FileNotFoundError("No 2m model cache found. Run py/research_regime_models_2m.py first.")
    return paths[0]


def max_loss_streak(wins):
    best = cur = 0
    for ok in wins:
        if ok:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return best


def load_context():
    cache = latest_signal_cache()
    data = np.load(cache, allow_pickle=True)
    pred = {
        "time": data["time"].astype(str),
        "prob": data["generic_prob"].astype(float),
        "regime": data["regime"].astype(str),
    }

    one = load_1m(SYMBOL)
    one["open_time"] = pd.to_datetime(one["open_time"], utc=True)
    one = one.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    one["time_key"] = one["open_time"].astype(str)
    m1_index = {t: i for i, t in enumerate(one["time_key"])}

    _, _, frame = prepare_frame()
    frame = frame.copy()
    frame["time"] = pd.to_datetime(frame["time"], utc=True)
    frame["time_key"] = frame["time"].astype(str)
    aligned = frame.set_index("time_key").loc[pred["time"]].reset_index(drop=True)
    return cache, pred, one, m1_index, aligned


def prediction_direction(prob, threshold):
    return np.where(prob >= threshold, 1, np.where(prob <= 1 - threshold, 0, -1))


def flow_opposes(direction, aligned):
    taker = aligned["taker_ratio"].astype(float).to_numpy()
    return ((direction == 1) & (taker < 0.85)) | ((direction == 0) & (taker > 1.15))


def build_candidates(pred, one, m1_index, aligned):
    prob = pred["prob"]
    direction = prediction_direction(prob, THRESHOLD)
    signal_ok = direction >= 0
    signal_ok &= ~flow_opposes(direction, aligned)

    candidates = []
    for pred_idx in np.where(signal_ok)[0]:
        signal_time = pd.to_datetime(pred["time"][pred_idx], utc=True)
        # 2m aggregate timestamps are period starts. The model has seen the
        # close of the second 1m candle, so immediate execution starts there.
        base_m1_time = signal_time + pd.Timedelta(minutes=BAR_MIN - 1)
        base_key = str(base_m1_time)
        base_idx = m1_index.get(base_key)
        if base_idx is None:
            continue
        if base_idx + OPTION_MIN >= len(one):
            continue
        candidates.append({
            "pred_idx": int(pred_idx),
            "base_idx": int(base_idx),
            "signal_time": str(signal_time),
            "base_time": str(base_m1_time),
            "direction": int(direction[pred_idx]),
            "direction_text": "UP" if direction[pred_idx] == 1 else "DOWN",
            "prob": float(prob[pred_idx]),
            "regime": str(pred["regime"][pred_idx]),
            "base_price": float(one.loc[base_idx, "close"]),
        })
    return candidates


def touch_entry(one, cand, wait_min, pullback_bps):
    base = cand["base_idx"]
    base_price = cand["base_price"]
    direction = cand["direction"]
    if wait_min <= 0:
        return base, base_price, "immediate"
    target = base_price * (1 - pullback_bps / 10000) if direction == 1 else base_price * (1 + pullback_bps / 10000)
    end = min(base + wait_min, len(one) - OPTION_MIN - 1)
    for idx in range(base + 1, end + 1):
        if direction == 1 and float(one.loc[idx, "low"]) <= target:
            return idx, float(target), "touch"
        if direction == 0 and float(one.loc[idx, "high"]) >= target:
            return idx, float(target), "touch"
    return None, None, "no_touch"


def close_better_entry(one, cand, wait_min, pullback_bps):
    base = cand["base_idx"]
    base_price = cand["base_price"]
    direction = cand["direction"]
    if wait_min <= 0:
        return base, base_price, "immediate"
    target = base_price * (1 - pullback_bps / 10000) if direction == 1 else base_price * (1 + pullback_bps / 10000)
    end = min(base + wait_min, len(one) - OPTION_MIN - 1)
    for idx in range(base + 1, end + 1):
        close = float(one.loc[idx, "close"])
        if direction == 1 and close <= target:
            return idx, close, "close_better"
        if direction == 0 and close >= target:
            return idx, close, "close_better"
    return None, None, "no_close_better"


def touch_confirm_entry(one, cand, wait_min, pullback_bps):
    base = cand["base_idx"]
    base_price = cand["base_price"]
    direction = cand["direction"]
    if wait_min <= 0:
        return base, base_price, "immediate"
    target = base_price * (1 - pullback_bps / 10000) if direction == 1 else base_price * (1 + pullback_bps / 10000)
    end = min(base + wait_min, len(one) - OPTION_MIN - 1)
    for idx in range(base + 1, end + 1):
        open_px = float(one.loc[idx, "open"])
        close_px = float(one.loc[idx, "close"])
        if direction == 1 and float(one.loc[idx, "low"]) <= target and close_px > open_px:
            return idx, close_px, "touch_confirm"
        if direction == 0 and float(one.loc[idx, "high"]) >= target and close_px < open_px:
            return idx, close_px, "touch_confirm"
    return None, None, "no_confirm"


def oracle_best_touch_entry(one, cand, wait_min, pullback_bps):
    base = cand["base_idx"]
    base_price = cand["base_price"]
    direction = cand["direction"]
    if wait_min <= 0:
        return base, base_price, "immediate"
    target = base_price * (1 - pullback_bps / 10000) if direction == 1 else base_price * (1 + pullback_bps / 10000)
    end = min(base + wait_min, len(one) - OPTION_MIN - 1)
    best_idx = None
    best_price = None
    for idx in range(base + 1, end + 1):
        if direction == 1:
            low = float(one.loc[idx, "low"])
            if low <= target and (best_price is None or low < best_price):
                best_idx, best_price = idx, low
        else:
            high = float(one.loc[idx, "high"])
            if high >= target and (best_price is None or high > best_price):
                best_idx, best_price = idx, high
    if best_idx is None:
        return None, None, "no_oracle_touch"
    return best_idx, float(best_price), "oracle_best"


def policy_entry(one, cand, policy):
    kind = policy["kind"]
    wait_min = int(policy.get("wait_min", 0))
    bps = float(policy.get("pullback_bps", 0))
    if kind == "immediate":
        return cand["base_idx"], cand["base_price"], "immediate"
    if kind == "touch_skip":
        return touch_entry(one, cand, wait_min, bps)
    if kind == "touch_timeout_close":
        idx, price, reason = touch_entry(one, cand, wait_min, bps)
        if idx is not None:
            return idx, price, reason
        timeout_idx = min(cand["base_idx"] + wait_min, len(one) - OPTION_MIN - 1)
        return timeout_idx, float(one.loc[timeout_idx, "close"]), "timeout_close"
    if kind == "close_better_skip":
        return close_better_entry(one, cand, wait_min, bps)
    if kind == "touch_confirm_skip":
        return touch_confirm_entry(one, cand, wait_min, bps)
    if kind == "oracle_best_touch_skip":
        return oracle_best_touch_entry(one, cand, wait_min, bps)
    raise ValueError(kind)


def evaluate_policy(one, candidates, policy):
    entries = []
    next_allowed = 0
    skipped_overlap = 0
    skipped_no_entry = 0
    considered = 0

    for cand in candidates:
        if cand["base_idx"] < next_allowed:
            skipped_overlap += 1
            continue
        considered += 1
        entry_idx, entry_price, reason = policy_entry(one, cand, policy)
        if entry_idx is None:
            skipped_no_entry += 1
            continue
        if entry_idx < next_allowed or entry_idx + OPTION_MIN >= len(one):
            skipped_overlap += 1
            continue
        expire_idx = entry_idx + OPTION_MIN
        expiry_close = float(one.loc[expire_idx, "close"])
        direction = cand["direction"]
        win = (expiry_close > entry_price) if direction == 1 else (expiry_close < entry_price)
        if direction == 1:
            improvement_bps = (cand["base_price"] - entry_price) / cand["base_price"] * 10000
        else:
            improvement_bps = (entry_price - cand["base_price"]) / cand["base_price"] * 10000
        entries.append({
            "signal_time": cand["signal_time"],
            "entry_time": str(one.loc[entry_idx, "open_time"]),
            "expiry_time": str(one.loc[expire_idx, "open_time"]),
            "direction": cand["direction_text"],
            "prob": cand["prob"],
            "regime": cand["regime"],
            "base_price": cand["base_price"],
            "entry_price": float(entry_price),
            "expiry_close": expiry_close,
            "entry_delay_min": int(entry_idx - cand["base_idx"]),
            "improvement_bps": float(improvement_bps),
            "win": bool(win),
            "reason": reason,
        })
        next_allowed = entry_idx + OPTION_MIN

    wins = np.asarray([e["win"] for e in entries], dtype=bool)
    times = np.asarray([e["entry_time"] for e in entries], dtype=str)
    overall = metric(wins, times[0] if len(times) else None, times[-1] if len(times) else None)
    improvements = np.asarray([e["improvement_bps"] for e in entries], dtype=float) if entries else np.asarray([])
    delays = np.asarray([e["entry_delay_min"] for e in entries], dtype=float) if entries else np.asarray([])
    by_regime = {}
    if entries:
        df = pd.DataFrame(entries)
        for reg, part in df.groupby("regime"):
            by_regime[reg] = metric(part["win"].to_numpy(bool), part["entry_time"].iloc[0], part["entry_time"].iloc[-1])
    return {
        "name": policy["name"],
        "kind": policy["kind"],
        "params": {k: v for k, v in policy.items() if k not in ["name", "kind"]},
        "overall": overall,
        "max_loss": max_loss_streak(wins),
        "considered_signals": int(considered),
        "entered": int(len(entries)),
        "skipped_no_entry": int(skipped_no_entry),
        "skipped_overlap": int(skipped_overlap),
        "avg_improvement_bps": round(float(improvements.mean()), 4) if len(improvements) else 0.0,
        "p50_improvement_bps": round(float(np.quantile(improvements, 0.5)), 4) if len(improvements) else 0.0,
        "p10_improvement_bps": round(float(np.quantile(improvements, 0.1)), 4) if len(improvements) else 0.0,
        "avg_delay_min": round(float(delays.mean()), 4) if len(delays) else 0.0,
        "by_regime": by_regime,
        "sample_entries": entries[:20],
    }


def policy_grid():
    policies = [{"name": "immediate", "kind": "immediate"}]
    for wait in [1, 2, 3, 5]:
        for bps in [2, 3, 5, 8, 12]:
            policies.append({"name": f"touch_skip_w{wait}_b{bps}", "kind": "touch_skip", "wait_min": wait, "pullback_bps": bps})
            policies.append({"name": f"touch_timeout_w{wait}_b{bps}", "kind": "touch_timeout_close", "wait_min": wait, "pullback_bps": bps})
            policies.append({"name": f"close_better_skip_w{wait}_b{bps}", "kind": "close_better_skip", "wait_min": wait, "pullback_bps": bps})
            policies.append({"name": f"touch_confirm_skip_w{wait}_b{bps}", "kind": "touch_confirm_skip", "wait_min": wait, "pullback_bps": bps})
    for wait in [1, 2, 3, 5]:
        for bps in [3, 5, 8]:
            policies.append({"name": f"oracle_best_touch_w{wait}_b{bps}", "kind": "oracle_best_touch_skip", "wait_min": wait, "pullback_bps": bps})
    return policies


def top(rows, min_trades=80, limit=15, key=None):
    use = [r for r in rows if r["overall"]["trades"] >= min_trades]
    return sorted(use, key=key or (lambda r: (r["overall"]["pnl_5u"], r["overall"]["wr"])), reverse=True)[:limit]


def main():
    cache, pred, one, m1_index, aligned = load_context()
    candidates = build_candidates(pred, one, m1_index, aligned)
    rows = [evaluate_policy(one, candidates, p) for p in policy_grid()]
    rows.sort(key=lambda r: (r["overall"]["pnl_5u"], r["overall"]["wr"]), reverse=True)
    report = {
        "method": {
            "type": "entry_price_optimization_2m_10m_binary",
            "symbol": SYMBOL.upper(),
            "primary_signal": "generic_th65_all + block_flow_opposes",
            "signal_cache": cache,
            "threshold": THRESHOLD,
            "bar_min": BAR_MIN,
            "option_min": OPTION_MIN,
            "horizon_bars": HORIZON,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "execution_assumption": "Immediate entry uses the close of the second 1m candle in the 2m signal bar. Touch entries use 1m OHLC high/low and are approximate because tick order inside a minute is unknown.",
            "oracle_note": "oracle_best_touch policies are not production-usable; they choose the best favorable intrawindow price after the fact.",
            "production_note": "Production-feasible policies cannot backdate an immediate entry after waiting. They either enter on touch/confirmation, enter at timeout close, or skip.",
        },
        "data": {
            "one_min_rows": int(len(one)),
            "raw_signal_candidates": int(len(candidates)),
            "one_min_start": str(one["open_time"].iloc[0]),
            "one_min_end": str(one["open_time"].iloc[-1]),
        },
        "results": {
            "top_pnl": top(rows, min_trades=80, limit=20),
            "top_wr": top(rows, min_trades=80, limit=20, key=lambda r: (r["overall"]["wr"], r["overall"]["trades"], r["overall"]["pnl_5u"])),
            "top_trade_count_profitable": top([r for r in rows if r["overall"]["pnl_5u"] > 0], min_trades=80, limit=20, key=lambda r: (r["overall"]["trades"], r["overall"]["wr"])),
            "non_oracle_top_pnl": top([r for r in rows if not r["kind"].startswith("oracle")], min_trades=80, limit=20),
            "production_top_pnl": top([r for r in rows if not r["kind"].startswith("oracle")], min_trades=80, limit=20),
            "oracle_top": top([r for r in rows if r["kind"].startswith("oracle")], min_trades=80, limit=12),
        },
        "all_policy_count": int(len(rows)),
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps({
        "saved": REPORT_FILE,
        "raw_signal_candidates": len(candidates),
        "top_production": [
            {
                "name": r["name"],
                "wr": r["overall"]["wr"],
                "trades": r["overall"]["trades"],
                "trades_per_day": r["overall"]["trades_per_day"],
                "pnl_5u": r["overall"]["pnl_5u"],
                "max_loss": r["max_loss"],
                "avg_improve_bps": r["avg_improvement_bps"],
                "avg_delay_min": r["avg_delay_min"],
                "skipped_no_entry": r["skipped_no_entry"],
            }
            for r in report["results"]["production_top_pnl"][:10]
        ],
        "top_oracle": [
            {
                "name": r["name"],
                "wr": r["overall"]["wr"],
                "trades": r["overall"]["trades"],
                "pnl_5u": r["overall"]["pnl_5u"],
                "max_loss": r["max_loss"],
                "avg_improve_bps": r["avg_improvement_bps"],
            }
            for r in report["results"]["oracle_top"][:5]
        ],
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
