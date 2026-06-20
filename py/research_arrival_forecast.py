from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from research_dynamic_entry import load_bars, load_current_signals
from research_smart_switch import build_features, current_signals, fixed_stable_policy


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OLD_CSV = ROOT / "tmp" / "latest_server_recheck_20260618_015135" / "btcusdt_1s_trades.csv"
DEFAULT_SHARD_DIR = ROOT / "tmp" / "latest_second_pull_20260620_131022" / "data" / "second" / "BTCUSDT" / "futures"
DEFAULT_PROD_CONFIG = ROOT / "tmp" / "latest_second_pull_20260620_131022" / "data" / "prod_config.json"
DEFAULT_OUT = ROOT / "tmp" / "arrival_forecast_research_latest.json"


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    return round(float(np.quantile(np.asarray(values, dtype=float), q)), 4)


def payoff_metrics(rows: list[dict], start: pd.Timestamp, end: pd.Timestamp) -> dict:
    n = len(rows)
    wins = sum(bool(r["won_after_entry"]) for r in rows)
    pnl = sum(4 if r["won_after_entry"] else -5 for r in rows)
    max_loss = 0
    cur = 0
    for row in sorted(rows, key=lambda r: r["entry_time"]):
        if row["won_after_entry"]:
            cur = 0
        else:
            cur += 1
            max_loss = max(max_loss, cur)
    days = max((end - start).total_seconds() / 86400.0, 1e-12)
    return {
        "trades": n,
        "winRate": round(wins / n * 100, 2) if n else 0.0,
        "pnlU_5u_80pct": round(float(pnl), 2),
        "maxConsecutiveLoss": int(max_loss),
        "tradesPerDay": round(n / days, 2),
    }


def payoff_by_day(rows: list[dict]) -> dict:
    out = {}
    if not rows:
        return out
    for day in sorted({row["entry_time"].date().isoformat() for row in rows}):
        subset = [row for row in rows if row["entry_time"].date().isoformat() == day]
        out[day] = {
            **payoff_metrics(subset, min(r["entry_time"] for r in subset), max(r["entry_time"] for r in subset)),
            "avgDelaySec": round(float(np.mean([r["actual_delay_sec"] for r in subset])), 2),
        }
    return out


def signal_side(signal: str) -> int:
    # +1 means the better entry is above signal price; -1 means below.
    return 1 if signal == "DOWN" else -1


def forecast_eta(
    close: np.ndarray,
    buy: np.ndarray,
    sell: np.ndarray,
    idx: int,
    side: int,
    target_bps: float,
    *,
    speed_window: int,
    accel_window: int,
    min_speed_bps: float,
) -> dict:
    if idx < speed_window + accel_window + 2:
        return {"ok": False, "reason": "warmup"}

    # Directed returns: positive means price is moving toward the better entry.
    ret_bps = side * 10000.0 * np.diff(np.log(close), prepend=np.nan)
    recent = ret_bps[idx - speed_window + 1 : idx + 1]
    prev = ret_bps[idx - speed_window - accel_window + 1 : idx - accel_window + 1]
    if len(recent) < speed_window or len(prev) < speed_window:
        return {"ok": False, "reason": "warmup"}

    weights = np.linspace(1.0, 2.0, len(recent))
    pos_recent = np.clip(recent, 0.0, None)
    weighted_speed = float(np.average(pos_recent, weights=weights))

    net_move = float(np.nansum(recent))
    path = float(np.nansum(np.abs(recent)))
    efficiency = max(0.0, min(1.0, net_move / path)) if path > 1e-12 else 0.0

    volume = buy[idx - speed_window + 1 : idx + 1] + sell[idx - speed_window + 1 : idx + 1]
    flow = side * (buy[idx - speed_window + 1 : idx + 1] - sell[idx - speed_window + 1 : idx + 1])
    flow_eff = float(np.nansum(flow) / max(np.nansum(volume), 1e-12))
    flow_multiplier = max(0.25, min(1.5, 1.0 + flow_eff))

    v_now = float(np.nanmean(np.clip(recent[-accel_window:], 0.0, None)))
    v_prev = float(np.nanmean(np.clip(prev[-accel_window:], 0.0, None)))
    accel = (v_now - v_prev) / max(float(accel_window), 1.0)

    raw_speed = weighted_speed * max(efficiency, 0.15) * flow_multiplier
    if raw_speed < min_speed_bps:
        return {
            "ok": True,
            "eta_sec": 1_000_000_000.0,
            "speed_bps_sec": float(raw_speed),
            "weighted_speed": float(weighted_speed),
            "efficiency": float(efficiency),
            "flow_eff": float(flow_eff),
            "accel_bps_sec2": float(accel),
            "reason": "no_momentum",
        }
    v_eff = raw_speed
    eta_linear = target_bps / v_eff
    eta_accel = eta_linear
    if abs(accel) > 1e-9:
        # 0.5*a*t^2 + v*t - d = 0
        disc = v_eff * v_eff + 2.0 * accel * target_bps
        if disc > 0:
            root = (-v_eff + math.sqrt(disc)) / accel
            if root > 0 and np.isfinite(root):
                eta_accel = root
    eta = 0.65 * eta_linear + 0.35 * eta_accel
    return {
        "ok": True,
        "eta_sec": float(max(1.0, eta)),
        "speed_bps_sec": float(v_eff),
        "weighted_speed": float(weighted_speed),
        "efficiency": float(efficiency),
        "flow_eff": float(flow_eff),
        "accel_bps_sec2": float(accel),
    }


def first_hit(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    idx: int,
    side: int,
    target_bps: float,
    max_wait_sec: int,
) -> tuple[int | None, float | None]:
    p0 = close[idx]
    if side < 0:
        target = p0 * math.exp(-target_bps / 10000.0)
        for j in range(idx + 1, min(idx + max_wait_sec, len(low) - 601) + 1):
            if low[j] <= target:
                return j, float(target)
    else:
        target = p0 * math.exp(target_bps / 10000.0)
        for j in range(idx + 1, min(idx + max_wait_sec, len(high) - 601) + 1):
            if high[j] >= target:
                return j, float(target)
    return None, None


def evaluate_case(
    name: str,
    signals: list[dict],
    bars: pd.DataFrame,
    *,
    target_bps: float,
    max_wait_sec: int,
    speed_window: int,
    accel_window: int,
    eta_tolerance_sec: float,
) -> dict:
    close = bars["close"].to_numpy(float)
    high = bars["high"].to_numpy(float)
    low = bars["low"].to_numpy(float)
    buy = bars["buy_qty"].to_numpy(float)
    sell = bars["sell_qty"].to_numpy(float)
    start, end = bars.index.min(), bars.index.max()
    forecasts: list[dict] = []
    hits: list[dict] = []
    predicted_hits: list[dict] = []
    misses = 0
    skipped = 0
    for row in signals:
        idx = int(row["idx"])
        if idx + max_wait_sec + 600 >= len(close):
            skipped += 1
            continue
        side = signal_side(str(row["signal"]))
        fc = forecast_eta(
            close,
            buy,
            sell,
            idx,
            side,
            target_bps,
            speed_window=speed_window,
            accel_window=accel_window,
            min_speed_bps=0.005,
        )
        if not fc["ok"]:
            skipped += 1
            continue
        hit_idx, entry = first_hit(high, low, close, idx, side, target_bps, max_wait_sec)
        predicted_hit = fc["eta_sec"] <= max_wait_sec
        if hit_idx is None:
            misses += 1
            forecasts.append(
                {
                    "signal_time": bars.index[idx].isoformat(),
                    "strategy_id": row.get("strategy_id"),
                    "signal": row["signal"],
                    "predicted_hit": bool(predicted_hit),
                    "actual_hit": False,
                    "eta_sec": round(fc["eta_sec"], 4),
                }
            )
            continue
        actual_delay = hit_idx - idx
        settle = close[hit_idx + 600]
        won = settle > entry if row["signal"] == "UP" else settle < entry
        rec = {
            "signal_time": bars.index[idx],
            "entry_time": bars.index[hit_idx],
            "strategy_id": row.get("strategy_id"),
            "signal": row["signal"],
            "predicted_hit": bool(predicted_hit),
            "actual_hit": True,
            "eta_sec": float(fc["eta_sec"]),
            "actual_delay_sec": int(actual_delay),
            "eta_error_sec": float(fc["eta_sec"] - actual_delay),
            "entry": float(entry),
            "settle": float(settle),
            "won_after_entry": bool(won),
            "speed_bps_sec": fc["speed_bps_sec"],
            "efficiency": fc["efficiency"],
            "flow_eff": fc["flow_eff"],
            "accel_bps_sec2": fc["accel_bps_sec2"],
        }
        hits.append(rec)
        if predicted_hit:
            predicted_hits.append(rec)
        forecasts.append({**rec, "signal_time": rec["signal_time"].isoformat(), "entry_time": rec["entry_time"].isoformat()})

    tested = len(forecasts)
    pred_hit = [r for r in forecasts if r["predicted_hit"]]
    true_pred_hit = [r for r in pred_hit if r["actual_hit"]]
    false_pred_hit = [r for r in pred_hit if not r["actual_hit"]]
    eta_errors = [abs(r["eta_error_sec"]) for r in hits]
    within_tol = [r for r in hits if abs(r["eta_error_sec"]) <= eta_tolerance_sec]
    by_side = {}
    for side_name in ("UP", "DOWN"):
        side_hits = [r for r in hits if r["signal"] == side_name]
        by_side[side_name] = {
            "hitCount": len(side_hits),
            **payoff_metrics(side_hits, start, end),
            "medianDelaySec": quantile([r["actual_delay_sec"] for r in side_hits], 0.5),
            "medianAbsEtaErrorSec": quantile([abs(r["eta_error_sec"]) for r in side_hits], 0.5),
        }

    return {
        "case": name,
        "params": {
            "target_bps": target_bps,
            "max_wait_sec": max_wait_sec,
            "speed_window": speed_window,
            "accel_window": accel_window,
            "eta_tolerance_sec": eta_tolerance_sec,
        },
        "forecastQuality": {
            "testedSignals": tested,
            "skippedSignals": skipped,
            "actualHitRate": round(len(hits) / tested * 100, 2) if tested else 0.0,
            "predictedHitCount": len(pred_hit),
            "predictedHitPrecision": round(len(true_pred_hit) / len(pred_hit) * 100, 2) if pred_hit else 0.0,
            "predictedHitRecall": round(len(true_pred_hit) / len(hits) * 100, 2) if hits else 0.0,
            "falsePredictedHitCount": len(false_pred_hit),
            "actualMissCount": int(tested - len(hits)),
            "medianAbsEtaErrorSec": quantile(eta_errors, 0.5),
            "p75AbsEtaErrorSec": quantile(eta_errors, 0.75),
            "withinToleranceRate": round(len(within_tol) / len(hits) * 100, 2) if hits else 0.0,
            "medianActualDelaySec": quantile([r["actual_delay_sec"] for r in hits], 0.5),
        },
        "tradeAfterHit": payoff_metrics(hits, start, end),
        "tradeAfterPredictedHit": payoff_metrics(predicted_hits, start, end),
        "tradeAfterPredictedHitByDay": payoff_by_day(predicted_hits),
        "bySide": by_side,
        "examples": forecasts[:80],
    }


def build_report(args: argparse.Namespace) -> dict:
    bars = load_bars(Path(args.old_csv), Path(args.shard_dir))
    current = load_current_signals(bars, Path(args.prod_config))
    current_for_switch = current_signals(bars, Path(args.prod_config))
    feat = build_features(bars)
    smart = fixed_stable_policy(bars, current_for_switch, feat)
    smart_only = [r for r in smart if r.get("strategy_id") == "SMART_FIXED_DOWN"]
    cases = {
        "current_online": current,
        "smart_fixed_all": smart,
        "smart_fixed_down_only": smart_only,
    }
    results = []
    for name, signals in cases.items():
        for target_bps in (1.0, 2.0, 3.0, 5.0):
            for max_wait in (20, 45, 90):
                results.append(
                    evaluate_case(
                        name,
                        signals,
                        bars,
                        target_bps=target_bps,
                        max_wait_sec=max_wait,
                        speed_window=30,
                        accel_window=10,
                        eta_tolerance_sec=10.0,
                    )
                )
    ranked = sorted(
        (
            {
                "case": r["case"],
                **r["params"],
                **r["forecastQuality"],
                **{f"trade_{k}": v for k, v in r["tradeAfterHit"].items()},
                **{f"predTrade_{k}": v for k, v in r["tradeAfterPredictedHit"].items()},
            }
            for r in results
        ),
        key=lambda x: (x["trade_pnlU_5u_80pct"], x["trade_winRate"], x["actualHitRate"]),
        reverse=True,
    )
    ranked_executable = sorted(
        (
            {
                "case": r["case"],
                **r["params"],
                **r["forecastQuality"],
                **{f"predTrade_{k}": v for k, v in r["tradeAfterPredictedHit"].items()},
            }
            for r in results
        ),
        key=lambda x: (x["predTrade_pnlU_5u_80pct"], x["predTrade_winRate"], x["predTrade_trades"]),
        reverse=True,
    )
    return {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "sample": {
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "hours": round((bars.index.max() - bars.index.min()).total_seconds() / 3600.0, 2),
            "rows": int(len(bars)),
            "observedPct": round(float(bars["observed"].mean() * 100), 2),
            "currentSignals": len(current),
            "smartFixedSignals": len(smart),
            "smartFixedDownSignals": len(smart_only),
        },
        "formula": {
            "direction_coordinate": "side=+1 for DOWN because better entry is higher; side=-1 for UP because better entry is lower.",
            "target_price": "P*=P0*exp(side*target_bps/10000).",
            "directed_return": "r_t=side*10000*ln(close_t/close_{t-1}); positive r_t means price is moving toward P*.",
            "weighted_speed": "v_w=weighted_mean(max(r_t,0)) over the last speed_window seconds, recent seconds have larger weight.",
            "efficiency": "E=clip(sum(r_t)/sum(abs(r_t)),0,1), measuring whether the path is clean or choppy.",
            "flow": "F=clip(1 + side*sum(buy_qty-sell_qty)/sum(volume),0.25,1.5).",
            "effective_speed": "raw_v=v_w*max(E,0.15)*F; if raw_v < min_speed_bps then ETA is treated as unreachable in the wait window.",
            "acceleration": "a=(mean_positive_speed_last_10s - mean_positive_speed_previous_10s)/10.",
            "eta": "65% linear ETA target_bps/v + 35% accelerated ETA from 0.5*a*t^2+v*t-target_bps=0.",
            "no_future_leakage": "All forecast terms use data up to signal second only; future high/low is used only for validation.",
        },
        "ranked": ranked,
        "rankedExecutable": ranked_executable,
        "results": results,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--old-csv", default=str(DEFAULT_OLD_CSV))
    p.add_argument("--shard-dir", default=str(DEFAULT_SHARD_DIR))
    p.add_argument("--prod-config", default=str(DEFAULT_PROD_CONFIG))
    p.add_argument("--out", default=str(DEFAULT_OUT))
    return p.parse_args()


def main() -> None:
    args = parse_args()
    report = build_report(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report["sample"], ensure_ascii=False))
    for item in report["ranked"][:20]:
        print(json.dumps(item, ensure_ascii=False))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
