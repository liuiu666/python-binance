"""Robustness profile for the current production BTC strategies.

This is a diagnostic pass, not an optimizer. It evaluates only the current
production configs and slices the walk-forward out-of-sample predictions by
time block, hour, weekday, RSI side, and volatility quartile.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "E:/codex/py")
from backtest_enhanced import load_symbol
from validate_strategy_candidates import STAKE, PAYOUT, collect_predictions, metric

OUT = "E:/codex/data"
CONFIG_FILE = os.path.join(OUT, "prod_config.json")
REPORT_FILE = os.path.join(OUT, "strategy_robustness_profile.json")


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def strategy_candidate(strategy_id, cfg):
    return {
        "name": strategy_id,
        "threshold": float(cfg["threshold"]),
        "rsi": (float(cfg["rsi_lo"]), float(cfg["rsi_hi"])),
        "vol_min_rank": cfg.get("vol_min_rank"),
        "agree_mode": cfg.get("agree_mode", "all3"),
        "skip_hours_utc": sorted({int(h) for h in cfg.get("skip_hours_utc", [])}),
    }


def prediction_frame(preds, candidate):
    y = preds["y"].astype(int)
    avg = preds["avg"].astype(float)
    vote_sum = preds["vote_sum"].astype(int)
    agree_all = preds["agree_all"].astype(bool)

    if candidate["agree_mode"] == "all3":
        agree_ok = agree_all
        direction = (avg >= 0.5).astype(int)
    else:
        agree_ok = np.ones(len(y), dtype=bool)
        direction = (vote_sum >= 2).astype(int)

    th = candidate["threshold"]
    mask = agree_ok & ((avg >= th) | (avg <= (1 - th)))

    rsi = preds["rsi14"].astype(float)
    if candidate.get("rsi"):
        lo, hi = candidate["rsi"]
        mask &= (rsi < lo) | (rsi > hi)

    atr = preds["atrp"].astype(float)
    if candidate.get("vol_min_rank") is not None:
        rank = pd.Series(atr).rank(pct=True).to_numpy()
        mask &= rank >= float(candidate["vol_min_rank"])

    if candidate.get("skip_hours_utc"):
        hours = pd.to_datetime(preds["time"]).hour
        mask &= ~pd.Series(hours).isin(candidate["skip_hours_utc"]).to_numpy()

    df = pd.DataFrame({
        "time": pd.to_datetime(preds["time"]),
        "target": y,
        "direction": direction,
        "avg": avg,
        "rsi14": rsi,
        "atrp": atr,
        "trade": mask,
    })
    df["win"] = df["direction"] == df["target"]
    return df[df["trade"]].reset_index(drop=True)


def slice_metrics(df, by):
    rows = []
    for name, part in df.groupby(by, sort=True):
        m = metric(part["win"].to_numpy())
        rows.append({"slice": str(name), **m})
    return rows


def chronological_blocks(df, blocks=10):
    rows = []
    if df.empty:
        return rows
    cuts = np.array_split(np.arange(len(df)), blocks)
    for i, idx in enumerate(cuts, start=1):
        part = df.iloc[idx]
        m = metric(part["win"].to_numpy())
        rows.append({
            "slice": f"block_{i:02d}",
            "start": str(part["time"].iloc[0]),
            "end": str(part["time"].iloc[-1]),
            **m,
        })
    return rows


def compact_risk(rows, min_trades):
    active = [r for r in rows if int(r.get("trades") or 0) >= min_trades]
    if not active:
        return {
            "min_wr": None,
            "worst_slice": None,
            "positive_slices": 0,
            "total_slices": 0,
        }
    worst = min(active, key=lambda r: float(r.get("wr") or 0))
    return {
        "min_wr": worst["wr"],
        "worst_slice": worst["slice"],
        "positive_slices": sum(1 for r in active if float(r.get("pnl_5u") or 0) > 0),
        "total_slices": len(active),
    }


def trade_frequency(trades):
    if trades.empty:
        return {
            "start": None,
            "end": None,
            "calendar_days": 0,
            "trades_per_day": 0,
            "trades_per_week": 0,
        }
    start = trades["time"].min()
    end = trades["time"].max()
    days = max((end - start).total_seconds() / 86400, 1 / 24)
    total = int(len(trades))
    return {
        "start": str(start),
        "end": str(end),
        "calendar_days": round(days, 2),
        "trades_per_day": round(total / days, 2),
        "trades_per_week": round(total / days * 7, 2),
    }


def profile_strategy(df5, strategy_id, cfg):
    horizon = int(cfg["horizon"])
    candidate = strategy_candidate(strategy_id, cfg)
    preds = collect_predictions(df5, horizon, strategy_id)
    trades = prediction_frame(preds, candidate)

    if trades.empty:
        return {"candidate": candidate, "overall": metric([]), "warning": "no trades"}

    trades["hour_utc"] = trades["time"].dt.hour
    trades["weekday"] = trades["time"].dt.day_name()
    lo, hi = candidate["rsi"]
    trades["rsi_side"] = np.where(trades["rsi14"] < lo, "oversold", np.where(trades["rsi14"] > hi, "overbought", "neutral"))
    trades["vol_quartile"] = pd.qcut(trades["atrp"].rank(method="first"), 4, labels=["q1_low", "q2", "q3", "q4_high"])

    time_blocks = chronological_blocks(trades, 10)
    by_hour = slice_metrics(trades, "hour_utc")
    by_weekday = slice_metrics(trades, "weekday")
    by_rsi_side = slice_metrics(trades, "rsi_side")
    by_vol_quartile = slice_metrics(trades, "vol_quartile")

    return {
        "candidate": candidate,
        "overall": metric(trades["win"].to_numpy()),
        "frequency": trade_frequency(trades),
        "time_blocks_10": time_blocks,
        "by_hour_utc": by_hour,
        "by_weekday": by_weekday,
        "by_rsi_side": by_rsi_side,
        "by_vol_quartile": by_vol_quartile,
        "risk_summary": {
            "time_blocks": compact_risk(time_blocks, 20),
            "hour_utc": compact_risk(by_hour, 20),
            "weekday": compact_risk(by_weekday, 20),
            "rsi_side": compact_risk(by_rsi_side, 20),
            "vol_quartile": compact_risk(by_vol_quartile, 20),
        },
    }


def main():
    config = read_json(CONFIG_FILE)
    df5 = load_symbol("btcusdt")
    if df5 is None:
        raise SystemExit("No BTC data found")
    report = {
        "method": {
            "type": "diagnostic_only",
            "stake": STAKE,
            "payout": PAYOUT,
            "note": "Only current production configs are evaluated; no parameter search is performed.",
        },
        "results": {},
    }
    for strategy_id in ["BTC_10min", "BTC_30min"]:
        report["results"][strategy_id] = profile_strategy(df5, strategy_id, config[strategy_id])

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
