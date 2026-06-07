"""Diagnose countertrend losses and test skip-vs-flip policies.

This report is research-only. It asks whether the live loss cluster is caused
by repeatedly taking reversal signals against a strong trend, and compares:
- current reversal model
- skipping strong-countertrend reversal trades
- flipping strong-countertrend reversal trades into trend-follow trades

No production config is changed and real auto trading must remain disabled.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "E:/codex/py")
from analyze_live_backtest_gap import live_signal_trades  # noqa: E402
from search_htf_regime_filters import build_frame  # noqa: E402
from validate_strategy_candidates import PAYOUT, STAKE  # noqa: E402
from backtest_enhanced import load_symbol  # noqa: E402

OUT = "E:/codex/data"
CONFIG_FILE = os.path.join(OUT, "prod_config.json")
TRADE_CONFIG_FILE = os.path.join(OUT, "trade_config.json")
REPORT_FILE = os.path.join(OUT, "countertrend_failure_report.json")
BREAKEVEN_WR = 100 / (1 + PAYOUT)
HORIZONS = {"BTC_10min": 2, "BTC_30min": 6}


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def max_loss_streak(wins):
    best = cur = 0
    for ok in wins:
        if ok:
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return int(best)


def metric(wins):
    wins = np.asarray(wins, dtype=bool)
    trades = int(len(wins))
    won = int(wins.sum()) if trades else 0
    lost = trades - won
    wr = won / trades * 100 if trades else 0.0
    return {
        "trades": trades,
        "wins": won,
        "losses": lost,
        "wr": round(float(wr), 2),
        "edge_over_breakeven": round(float(wr - BREAKEVEN_WR), 2),
        "pnl_5u": round(float(won * STAKE * PAYOUT - lost * STAKE), 2),
        "max_loss": max_loss_streak(wins.tolist()),
    }


def block_summary(frame, direction, mask):
    target = frame["target"].astype(int).to_numpy()
    rows = []
    for i, idx in enumerate(np.array_split(np.arange(len(frame)), 10), start=1):
        use = mask[idx]
        wins = direction[idx][use] == target[idx][use]
        rows.append({
            "slice": f"block_{i:02d}",
            "start": str(frame["time"].iloc[idx[0]]),
            "end": str(frame["time"].iloc[idx[-1]]),
            **metric(wins),
        })
    active = [r for r in rows if r["trades"] >= 5]
    if not active:
        return rows, {"active_blocks": 0, "positive_blocks": 0, "min_block_wr": None, "worst_block": None}
    worst = min(active, key=lambda r: r["wr"])
    return rows, {
        "active_blocks": len(active),
        "positive_blocks": sum(1 for r in active if r["pnl_5u"] > 0),
        "min_block_wr": worst["wr"],
        "worst_block": worst["slice"],
    }


def base_direction_mask(frame, cfg):
    avg = frame["avg"].astype(float).to_numpy()
    rsi = frame["rsi14"].astype(float).to_numpy()
    th = float(cfg.get("threshold", 0.55))
    if cfg.get("agree_mode", "majority") == "all3":
        direction = frame["ml_dir_all3"].astype(int).to_numpy()
        agree = frame["agree_all"].astype(bool).to_numpy()
    else:
        direction = frame["ml_dir_majority"].astype(int).to_numpy()
        agree = np.ones(len(frame), dtype=bool)
    lo = float(cfg.get("rsi_lo", 30))
    hi = float(cfg.get("rsi_hi", 70))
    mask = agree & ((avg >= th) | (avg <= 1 - th)) & ((rsi < lo) | (rsi > hi))
    skip_hours = sorted({int(h) for h in cfg.get("skip_hours_utc", [])})
    if skip_hours:
        mask &= ~frame["hour_utc"].isin(skip_hours).to_numpy()
    return direction, mask


def trend_direction(score):
    return np.where(score >= 3, 1, np.where(score <= -3, 0, -1))


def add_short_trend_score(frame):
    if "trend_score" in frame.columns:
        return frame
    score = np.zeros(len(frame), dtype=int)
    eps = 0.00005
    for col in ["trend6", "trend12", "trend30", "pre50"]:
        vals = frame[col].astype(float).to_numpy()
        score += (vals > eps).astype(int)
        score -= (vals < -eps).astype(int)
    stack = frame["ema_stack"].astype(float).to_numpy()
    score += (stack > 0).astype(int)
    score -= (stack < 0).astype(int)
    out = frame.copy()
    out["trend_score"] = score
    return out


def apply_policy(frame, base_direction, base_mask, policy):
    direction = base_direction.copy()
    mask = base_mask.copy()
    short_score = frame["trend_score"].astype(int).to_numpy()
    htf_score = frame["htf_score"].astype(int).to_numpy()
    short_trend_dir = trend_direction(short_score)
    htf_trend_dir = trend_direction(htf_score)
    short_counter = (short_trend_dir >= 0) & (direction != short_trend_dir)
    htf_counter = (htf_trend_dir >= 0) & (direction != htf_trend_dir)
    both_counter = short_counter & htf_counter & (short_trend_dir == htf_trend_dir)

    name = policy["name"]
    if name == "current":
        return direction, mask
    if name == "skip_short_strong_countertrend":
        mask &= ~short_counter
    elif name == "skip_htf_strong_countertrend":
        mask &= ~htf_counter
    elif name == "skip_both_short_htf_countertrend":
        mask &= ~both_counter
    elif name == "flip_short_strong_countertrend":
        direction = np.where(short_counter, short_trend_dir, direction)
    elif name == "flip_htf_strong_countertrend":
        direction = np.where(htf_counter, htf_trend_dir, direction)
    elif name == "flip_both_short_htf_countertrend":
        direction = np.where(both_counter, short_trend_dir, direction)
    elif name == "trend_follow_only_short_strong":
        mask &= short_trend_dir >= 0
        direction = np.where(short_trend_dir >= 0, short_trend_dir, direction)
    elif name == "trend_follow_only_htf_strong":
        mask &= htf_trend_dir >= 0
        direction = np.where(htf_trend_dir >= 0, htf_trend_dir, direction)
    elif name == "countertrend_only_cooling":
        trend6 = np.abs(frame["trend6"].astype(float).to_numpy())
        strength = frame["strength"].astype(float).to_numpy()
        cooling = (trend6 <= float(policy["max_abs_trend6"])) & (strength <= float(policy["max_strength"]))
        mask &= ~(short_counter & ~cooling)
    else:
        raise ValueError(f"unknown policy: {name}")
    return direction, mask


def grouped_current(frame, direction, mask):
    selected = frame.loc[mask].copy()
    selected["direction_num"] = direction[mask]
    selected["win"] = selected["direction_num"].to_numpy() == selected["target"].astype(int).to_numpy()
    selected["short_align"] = np.where(selected["direction_num"] == 1, selected["trend_score"], -selected["trend_score"])
    selected["htf_align"] = np.where(selected["direction_num"] == 1, selected["htf_score"], -selected["htf_score"])
    selected["direction"] = np.where(selected["direction_num"] == 1, "UP", "DOWN")
    out = {}
    groups = {
        "direction": ["direction"],
        "short_align_bucket": [pd.cut(
            selected["short_align"],
            bins=[-10, -3, -1, 0, 1, 3, 10],
            labels=["strong_counter", "mild_counter", "neutral", "mild_aligned", "aligned", "strong_aligned"],
        )],
        "htf_label": ["htf_label"],
        "hour_utc": ["hour_utc"],
    }
    for name, cols in groups.items():
        if isinstance(cols[0], pd.Series):
            tmp = selected.copy()
            tmp["_bucket"] = cols[0].astype(str)
            key_cols = ["_bucket"]
        else:
            tmp = selected
            key_cols = cols
        rows = []
        for key, part in tmp.groupby(key_cols, sort=True):
            key_value = key[0] if isinstance(key, tuple) else key
            if len(part) < 3:
                continue
            rows.append({"bucket": str(key_value), **metric(part["win"].to_numpy())})
        rows.sort(key=lambda r: (r["wr"], r["trades"]), reverse=True)
        out[name] = rows
    return out


def evaluate_offline(strategy_id, cfg, df5):
    frame = add_short_trend_score(build_frame(df5, strategy_id, int(cfg["horizon"])))
    base_direction, base_mask = base_direction_mask(frame, cfg)
    policies = [
        {"name": "current"},
        {"name": "skip_short_strong_countertrend"},
        {"name": "skip_htf_strong_countertrend"},
        {"name": "skip_both_short_htf_countertrend"},
        {"name": "flip_short_strong_countertrend"},
        {"name": "flip_htf_strong_countertrend"},
        {"name": "flip_both_short_htf_countertrend"},
        {"name": "trend_follow_only_short_strong"},
        {"name": "trend_follow_only_htf_strong"},
        {"name": "countertrend_only_cooling", "max_abs_trend6": 0.0025, "max_strength": 30},
        {"name": "countertrend_only_cooling", "max_abs_trend6": 0.0030, "max_strength": 30},
    ]
    target = frame["target"].astype(int).to_numpy()
    current_direction, current_mask = apply_policy(frame, base_direction, base_mask, policies[0])
    current = metric(current_direction[current_mask] == target[current_mask])
    rows = []
    for policy in policies:
        direction, mask = apply_policy(frame, base_direction, base_mask, policy)
        wins = direction[mask] == target[mask]
        blocks, bsum = block_summary(frame, direction, mask)
        name = policy["name"]
        if name == "countertrend_only_cooling":
            name += f"_t6{int(policy['max_abs_trend6'] * 10000)}_str{int(policy['max_strength'])}"
        rows.append({
            "name": name,
            "overall": metric(wins),
            "wr_delta_pp": round(metric(wins)["wr"] - current["wr"], 2),
            "trade_retention_pct": round(len(wins) / max(1, current["trades"]) * 100, 2),
            "time_block_summary": bsum,
            "time_blocks": blocks,
        })
    rows.sort(
        key=lambda r: (
            r["overall"]["wr"],
            r["time_block_summary"]["min_block_wr"] or 0,
            -r["overall"]["max_loss"],
            r["overall"]["trades"],
        ),
        reverse=True,
    )
    return {
        "oos_range": {
            "start": str(frame["time"].iloc[0]),
            "end": str(frame["time"].iloc[-1]),
            "rows": int(len(frame)),
        },
        "current": current,
        "current_groups": grouped_current(frame, current_direction, current_mask),
        "policies_ranked": rows,
    }


def live_metric(rows, direction_getter):
    wins = []
    selected = 0
    for row in rows:
        direction = direction_getter(row)
        if direction is None:
            continue
        selected += 1
        open_price = float(row.get("open_price"))
        close_price = float(row.get("close_price"))
        if close_price == open_price:
            continue
        target = 1 if close_price > open_price else 0
        wins.append(int(direction) == target)
    return metric(wins), selected


def evaluate_live(strategy_id):
    rows = [r for r in live_signal_trades() if r.get("strategy") == strategy_id]
    if not rows:
        return {"sample": "none", "current": metric([]), "policies": []}

    def base_dir(row):
        return 1 if row.get("direction") == "UP" else 0

    current, _ = live_metric(rows, base_dir)

    def short_trend_dir(row):
        score = int(row.get("trend_score") or 0)
        if score >= 3:
            return 1
        if score <= -3:
            return 0
        return None

    policies = []
    for name in ["skip_short_strong_countertrend", "flip_short_strong_countertrend", "trend_follow_only_short_strong"]:
        def getter(row, policy_name=name):
            bdir = base_dir(row)
            tdir = short_trend_dir(row)
            counter = tdir is not None and bdir != tdir
            if policy_name == "skip_short_strong_countertrend":
                return None if counter else bdir
            if policy_name == "flip_short_strong_countertrend":
                return tdir if counter else bdir
            if policy_name == "trend_follow_only_short_strong":
                return tdir
            return bdir

        m, selected = live_metric(rows, getter)
        policies.append({
            "name": name,
            "overall": m,
            "wr_delta_pp": round(m["wr"] - current["wr"], 2) if m["trades"] else None,
            "trade_retention_pct": round(selected / max(1, len(rows)) * 100, 2),
        })
    return {
        "sample": "diagnostic_small_sample" if current["trades"] < 50 else "readable",
        "current": current,
        "policies": policies,
    }


def main():
    cfg = read_json(CONFIG_FILE, {})
    trade_cfg = read_json(TRADE_CONFIG_FILE, {})
    df5 = load_symbol("btcusdt")
    if df5 is None:
        raise SystemExit("No BTC data found")

    report = {
        "method": {
            "type": "countertrend_skip_vs_flip_diagnostic",
            "payout": PAYOUT,
            "stake": STAKE,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "note": "Research only. Skip and flip policies are compared offline and on live replay; no trading config is changed.",
        },
        "safety": {
            "autoTrade": trade_cfg.get("autoTrade"),
            "verdict": "do_not_resume_real_auto_trading",
        },
        "strategies": {},
        "conclusions": [],
    }
    for strategy_id in ["BTC_10min", "BTC_30min"]:
        offline = evaluate_offline(strategy_id, cfg[strategy_id], df5)
        live = evaluate_live(strategy_id)
        best = offline["policies_ranked"][0]
        current = offline["current"]
        flip = next((r for r in offline["policies_ranked"] if r["name"] == "flip_short_strong_countertrend"), None)
        skip = next((r for r in offline["policies_ranked"] if r["name"] == "skip_short_strong_countertrend"), None)
        report["strategies"][strategy_id] = {
            "offline": offline,
            "live_replay": live,
        }
        report["conclusions"].append(
            f"{strategy_id}: current offline WR {current['wr']}%/{current['trades']} trades; "
            f"best countertrend policy {best['name']} WR {best['overall']['wr']}%/"
            f"{best['overall']['trades']} trades ({best['wr_delta_pp']:+.2f}pp). "
            f"short-skip WR {skip['overall']['wr'] if skip else None}%, "
            f"short-flip WR {flip['overall']['wr'] if flip else None}%. "
            f"live current WR {live['current']['wr']}% over {live['current']['trades']} settled signals."
        )

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(json.dumps({
        "safety": report["safety"],
        "conclusions": report["conclusions"],
        "top": {
            sid: [
                {
                    "name": row["name"],
                    "wr": row["overall"]["wr"],
                    "trades": row["overall"]["trades"],
                    "delta": row["wr_delta_pp"],
                    "retention": row["trade_retention_pct"],
                    "max_loss": row["overall"]["max_loss"],
                }
                for row in payload["offline"]["policies_ranked"][:5]
            ]
            for sid, payload in report["strategies"].items()
        },
    }, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
