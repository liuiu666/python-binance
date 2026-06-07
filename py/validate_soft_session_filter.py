"""Validate hard time filters versus soft session-risk gating.

This keeps time-of-day as a small risk input instead of a hard production ban.
The live implementation in signal_btc.py uses the same score thresholds.
"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, "E:/codex/py")
from search_htf_regime_filters import build_frame, metric  # noqa: E402
from backtest_enhanced import load_symbol  # noqa: E402

OUT = "E:/codex/data"
CONFIG_FILE = os.path.join(OUT, "prod_config.json")
REPORT_FILE = os.path.join(OUT, "soft_session_filter_validation.json")
HORIZONS = {"BTC_10min": 2, "BTC_30min": 6}
TREND_EPS = 0.00005


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def trend_score_frame(frame):
    score = np.zeros(len(frame), dtype=int)
    for col in ["trend6", "trend12", "trend30", "pre50"]:
        vals = frame[col].astype(float).to_numpy()
        score += (vals > TREND_EPS).astype(int)
        score -= (vals < -TREND_EPS).astype(int)
    stack = frame["ema_stack"].astype(float).to_numpy()
    score += (stack > 0).astype(int)
    score -= (stack < 0).astype(int)
    return score


def direction_sign(direction):
    return np.where(direction == 1, 1, -1)


def market_confirm_score(frame, direction):
    sign = direction_sign(direction)
    trend = frame["trend_score"].astype(int).to_numpy()
    htf = frame["htf_score"].astype(int).to_numpy()
    taker = frame.get("taker_ratio", 1).astype(float).to_numpy()
    atr_exp = frame.get("atr_exp", 0).astype(float).to_numpy()

    short_align = trend * sign
    htf_align = htf * sign
    score = np.zeros(len(frame), dtype=int)

    score += np.where(short_align >= 3, 2, np.where(short_align > 0, 1, 0))
    score -= np.where(short_align <= -3, 2, np.where(short_align < 0, 1, 0))
    score += np.where(htf_align >= 3, 2, np.where(htf_align > 0, 1, 0))
    score -= np.where(htf_align <= -3, 2, np.where(htf_align < 0, 1, 0))

    taker_align = np.where(taker >= 1.05, 1, np.where(taker <= 0.95, -1, 0))
    score += np.where(taker_align == sign, 1, 0)
    score -= np.where((taker_align != 0) & (taker_align != sign), 1, 0)

    score += np.where((atr_exp >= 0.65) & (atr_exp <= 2.25), 1, 0)
    score -= np.where(atr_exp > 2.8, 1, 0)
    return score, short_align, htf_align


def base_ml_signals(frame, cfg):
    avg = frame["avg"].astype(float).to_numpy()
    rsi = frame["rsi14"].astype(float).to_numpy()
    if cfg.get("agree_mode", "majority") == "all3":
        agree = frame["agree_all"].astype(bool).to_numpy()
        direction = (avg >= 0.5).astype(int)
    else:
        agree = np.ones(len(frame), dtype=bool)
        direction = (frame["vote_sum"].astype(int).to_numpy() >= 2).astype(int)
    th = float(cfg.get("threshold", 0.55))
    lo = float(cfg.get("rsi_lo", 30))
    hi = float(cfg.get("rsi_hi", 70))
    strength = np.abs(avg - 0.5) * 200
    mask = agree & ((avg >= th) | (avg <= 1 - th)) & ((rsi < lo) | (rsi > hi))
    return direction, mask, strength


def summarize(frame, direction, mask):
    target = frame["target"].astype(int).to_numpy()
    wins = direction[mask] == target[mask]
    days = max(1e-9, (frame["time"].iloc[-1] - frame["time"].iloc[0]).total_seconds() / 86400)
    out = metric(wins)
    out["trades_per_day"] = round(float(out["trades"] / days), 2)
    return out


def validate_strategy(df5, strategy_id, cfg):
    frame = build_frame(df5, strategy_id, HORIZONS[strategy_id])
    frame["trend_score"] = trend_score_frame(frame)
    direction, base_mask, strength = base_ml_signals(frame, cfg)

    skip_hours = sorted({int(h) for h in cfg.get("skip_hours_utc", [])})
    session_risk = frame["hour_utc"].isin(skip_hours).to_numpy()
    hard_mask = base_mask & ~session_risk

    score, short_align, htf_align = market_confirm_score(frame, direction)
    min_score = int(cfg.get("session_min_market_score", 2))
    bump = float(cfg.get("session_confidence_bump", 8))
    base_strength_min = abs(float(cfg.get("threshold", 0.55)) - 0.5) * 200
    strong_counter = (short_align <= -3) & (htf_align <= 0)
    soft_risk_ok = (
        (strength >= base_strength_min + bump)
        & (score >= min_score)
        & ~strong_counter
    )
    soft_mask = base_mask & (~session_risk | soft_risk_ok)

    risk_base = base_mask & session_risk
    risk_soft = soft_mask & session_risk
    return {
        "strategy_id": strategy_id,
        "skip_hours_utc": skip_hours,
        "settings": {
            "mode": "soft",
            "session_confidence_bump": bump,
            "session_min_market_score": min_score,
            "strong_countertrend_block": True,
        },
        "no_session_filter": summarize(frame, direction, base_mask),
        "old_hard_session_filter": summarize(frame, direction, hard_mask),
        "new_soft_session_filter": summarize(frame, direction, soft_mask),
        "risk_hours_before_soft_gate": summarize(frame, direction, risk_base),
        "risk_hours_allowed_by_soft_gate": summarize(frame, direction, risk_soft),
        "soft_gate_risk_retention_pct": round(float(risk_soft.sum() / max(1, risk_base.sum()) * 100), 2),
    }


def main():
    cfg = read_json(CONFIG_FILE, {})
    df5 = load_symbol("btcusdt")
    if df5 is None:
        raise SystemExit("No BTC data found")
    report = {
        "method": {
            "type": "soft_session_filter_validation",
            "note": (
                "Compares no time filter, the old hard skip-hours filter, and the new soft "
                "session-risk gate. Entry-timing confirmation is not included in this report."
            ),
        },
        "strategies": {},
    }
    for strategy_id in HORIZONS:
        report["strategies"][strategy_id] = validate_strategy(df5, strategy_id, cfg[strategy_id])
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
