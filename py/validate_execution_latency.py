"""Validate selected BTC option strategies under execution latency.

Binary options settle from the actual order entry time, not from the model
signal timestamp. This script repeats strict walk-forward prediction and
settles selected candidates after delayed entry times using 1m BTC closes.
"""
import json
import os
import sys
import warnings
from collections import defaultdict

import pandas as pd

warnings.filterwarnings("ignore")

sys.path.insert(0, "E:/codex/py")
from backtest_enhanced import load_symbol
from validate_strategy_candidates import TRAIN_SIZE, TEST_SIZE, STEP, collect_predictions

OUT = "E:/codex/data"
BTC_1M_FILE = os.path.join(OUT, "btcusdt_1m.csv")
CONFIG_FILE = os.path.join(OUT, "prod_config.json")
REPORT_FILE = os.path.join(OUT, "execution_latency_validation.json")
PAYOUT = 0.85
STAKE = 5
DELAYS_MIN = [0, 1, 2, 3, 5]


def max_loss_streak(statuses):
    best = cur = 0
    for s in statuses:
        if s == "lost":
            cur += 1
            best = max(best, cur)
        elif s in ("won", "tie"):
            cur = 0
    return best


def metric(statuses):
    statuses = list(statuses)
    wins = statuses.count("won")
    losses = statuses.count("lost")
    ties = statuses.count("tie")
    pnl = wins * STAKE * PAYOUT - losses * STAKE
    return {
        "trades": wins + losses + ties,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "wr": round(wins / max(1, wins + losses) * 100, 2),
        "pnl_5u": round(float(pnl), 2),
        "max_loss": max_loss_streak(statuses),
    }


def load_1m():
    df = pd.read_csv(BTC_1M_FILE, parse_dates=["open_time"])
    df = df.sort_values("open_time").reset_index(drop=True)
    return pd.to_datetime(df["open_time"], utc=True), df["close"].astype(float).to_numpy()


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    raw = raw.get("strategies", raw)
    candidates = {}
    for label in ["BTC_10min", "BTC_30min"]:
        cfg = raw[label]
        candidates[label] = {
            "horizon": int(cfg["horizon"]),
            "duration": int(cfg.get("interval_min", int(cfg["horizon"]) * 5)),
            "threshold": float(cfg["threshold"]),
            "rsi": (float(cfg.get("rsi_lo", 30)), float(cfg.get("rsi_hi", 70))),
            "agree_mode": cfg.get("agree_mode", "all3"),
            "skip_hours_utc": sorted({int(h) for h in cfg.get("skip_hours_utc", [])}),
        }
    return candidates


def price_at(times, prices, ts):
    idx = times.searchsorted(ts, side="left")
    if idx >= len(prices):
        return None
    return float(prices[idx])


def status_for(direction, open_price, close_price):
    if close_price == open_price:
        return "tie"
    if direction == "UP":
        return "won" if close_price > open_price else "lost"
    if direction == "DOWN":
        return "won" if close_price < open_price else "lost"
    return "unknown"


def collect_opportunities(df5, label, cfg):
    preds = collect_predictions(df5, cfg["horizon"], label)
    avg = preds["avg"].astype(float)
    vote_sum = preds["vote_sum"].astype(int)
    agree_all = preds["agree_all"].astype(bool)
    rsi = preds["rsi14"].astype(float)
    times = pd.to_datetime(preds["time"], utc=True)

    if cfg["agree_mode"] == "all3":
        agree_ok = agree_all
        direction_up = avg >= 0.5
    else:
        agree_ok = pd.Series(True, index=range(len(avg))).to_numpy()
        direction_up = vote_sum >= 2

    th = cfg["threshold"]
    high_conf = (avg >= th) | (avg <= (1 - th))
    lo, hi = cfg["rsi"]
    rsi_ok = (rsi < lo) | (rsi > hi)
    mask = agree_ok & high_conf & rsi_ok
    if cfg.get("skip_hours_utc"):
        mask &= ~pd.Series(times.hour).isin(cfg["skip_hours_utc"]).to_numpy()

    opps = []
    for j in mask.nonzero()[0]:
        opps.append({
            "strategy": label,
            "time": times[j],
            "direction": "UP" if bool(direction_up[j]) else "DOWN",
            "avg_prob": float(avg[j]),
            "confidence": round(abs(float(avg[j]) - 0.5) * 2 * 100, 1),
            "rsi": float(rsi[j]),
        })
    print(f"{label} latency opportunities from cached walk-forward preds: opps={len(opps)}")
    return opps


def evaluate_latency(opps, cfg, times_1m, prices_1m):
    by_delay = {}
    examples = defaultdict(list)
    for delay in DELAYS_MIN:
        statuses = []
        for opp in opps:
            # df5["time"] is the 5m candle start. The model can only act after
            # that candle closes, so execution delay is measured from +5m.
            entry_time = opp["time"] + pd.Timedelta(minutes=5 + delay)
            expiry_time = entry_time + pd.Timedelta(minutes=cfg["duration"])
            open_price = price_at(times_1m, prices_1m, entry_time)
            close_price = price_at(times_1m, prices_1m, expiry_time)
            if open_price is None or close_price is None:
                continue
            s = status_for(opp["direction"], open_price, close_price)
            statuses.append(s)
            if len(examples[str(delay)]) < 10:
                examples[str(delay)].append({
                    "signalTime": str(opp["time"]),
                    "entryTime": str(entry_time),
                    "expiryTime": str(expiry_time),
                    "direction": opp["direction"],
                    "openPrice": open_price,
                    "closePrice": close_price,
                    "status": s,
                    "confidence": opp["confidence"],
                    "rsi": round(opp["rsi"], 2),
                })
        by_delay[str(delay)] = metric(statuses)
    return by_delay, examples


def main():
    df5 = load_symbol("btcusdt")
    times_1m, prices_1m = load_1m()
    candidates = load_config()
    report = {
        "method": {
            "train_size": TRAIN_SIZE,
            "test_size": TEST_SIZE,
            "step": STEP,
            "delays_min": DELAYS_MIN,
            "note": "Entry and expiry are settled from delayed 1m close prices. Production skip_hours_utc filters are applied.",
        },
        "results": {},
    }
    for label, cfg in candidates.items():
        opps = collect_opportunities(df5, label, cfg)
        by_delay, examples = evaluate_latency(opps, cfg, times_1m, prices_1m)
        report["results"][label] = {
            "candidate": cfg,
            "opportunities": len(opps),
            "by_delay_min": by_delay,
            "examples": examples,
        }
        print(f"\n{label} latency:")
        for d, m in by_delay.items():
            print(f"  delay={d}m WR={m['wr']}% n={m['trades']} pnl={m['pnl_5u']} maxLoss={m['max_loss']}")
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\nSaved {REPORT_FILE}")


if __name__ == "__main__":
    main()
