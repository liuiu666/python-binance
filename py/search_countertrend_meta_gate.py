"""Walk-forward second-stage gate for countertrend reversal signals.

The first-stage model decides UP/DOWN. This script trains a second-stage
LightGBM gate that predicts whether an already-triggered reversal signal will
win, using only signal-time features. It is research-only and does not change
production config.
"""
import json
import os
import sys

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier

sys.path.insert(0, "E:/codex/py")
from analyze_countertrend_failures import (  # noqa: E402
    add_short_trend_score,
    base_direction_mask,
    metric,
    trend_direction,
)
from backtest_enhanced import load_symbol  # noqa: E402
from search_htf_regime_filters import build_frame  # noqa: E402
from validate_strategy_candidates import PAYOUT, STAKE  # noqa: E402

OUT = "E:/codex/data"
CONFIG_FILE = os.path.join(OUT, "prod_config.json")
TRADE_CONFIG_FILE = os.path.join(OUT, "trade_config.json")
REPORT_FILE = os.path.join(OUT, "countertrend_meta_gate_report.json")
HORIZONS = {"BTC_10min": 2, "BTC_30min": 6}
BREAKEVEN_WR = 100 / (1 + PAYOUT)
THRESHOLDS = [0.52, 0.55, 0.58, 0.60, 0.62, 0.65, 0.70]


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def make_model(seed):
    return LGBMClassifier(
        n_estimators=160,
        max_depth=3,
        learning_rate=0.04,
        subsample=0.75,
        colsample_bytree=0.70,
        reg_alpha=1.2,
        reg_lambda=2.2,
        min_child_samples=35,
        random_state=seed,
        verbose=-1,
    )


def block_summary(trades, mask):
    rows = []
    for i, idx in enumerate(np.array_split(np.arange(len(trades)), 10), start=1):
        use = mask[idx]
        wins = trades["win"].to_numpy(bool)[idx][use]
        rows.append({
            "slice": f"block_{i:02d}",
            "start": str(trades["time"].iloc[idx[0]]),
            "end": str(trades["time"].iloc[idx[-1]]),
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


def build_signal_trades(df5, strategy_id, cfg):
    frame = add_short_trend_score(build_frame(df5, strategy_id, int(cfg["horizon"])))
    direction, mask = base_direction_mask(frame, cfg)
    selected = frame.loc[mask].copy().reset_index(drop=True)
    selected["direction_num"] = direction[mask]
    selected["direction"] = np.where(selected["direction_num"] == 1, "UP", "DOWN")
    selected["win"] = selected["direction_num"].to_numpy() == selected["target"].astype(int).to_numpy()

    short_trend_dir = trend_direction(selected["trend_score"].astype(int).to_numpy())
    htf_trend_dir = trend_direction(selected["htf_score"].astype(int).to_numpy())
    selected["short_countertrend"] = (short_trend_dir >= 0) & (selected["direction_num"].to_numpy() != short_trend_dir)
    selected["htf_countertrend"] = (htf_trend_dir >= 0) & (selected["direction_num"].to_numpy() != htf_trend_dir)
    selected["both_countertrend"] = (
        selected["short_countertrend"].to_numpy()
        & selected["htf_countertrend"].to_numpy()
        & (short_trend_dir == htf_trend_dir)
    )
    selected["short_align"] = np.where(
        selected["direction_num"].to_numpy() == 1,
        selected["trend_score"].astype(int).to_numpy(),
        -selected["trend_score"].astype(int).to_numpy(),
    )
    selected["htf_align"] = np.where(
        selected["direction_num"].to_numpy() == 1,
        selected["htf_score"].astype(int).to_numpy(),
        -selected["htf_score"].astype(int).to_numpy(),
    )
    selected["is_down"] = (selected["direction_num"] == 0).astype(int)
    selected["hour_sin"] = np.sin(2 * np.pi * selected["hour_utc"].astype(float) / 24)
    selected["hour_cos"] = np.cos(2 * np.pi * selected["hour_utc"].astype(float) / 24)
    selected["signal_win_label"] = selected["win"].astype(int)
    return selected


def feature_cols(trades):
    candidates = [
        "avg", "strength", "rsi14", "bbp", "bbw", "atrp", "atr_exp", "vr",
        "trend6", "trend12", "trend30", "pre50", "ema_stack", "trend_score",
        "htf_score", "htf_ret_1h", "htf_ret_4h", "htf_ret_24h",
        "htf_pos_1h", "htf_pos_4h", "htf_pos_24h", "htf_rng_1h",
        "htf_rng_4h", "htf_rng_24h", "taker_ratio", "ls_ratio", "fund_rate",
        "short_align", "htf_align", "is_down", "short_countertrend",
        "htf_countertrend", "both_countertrend", "hour_sin", "hour_cos",
    ]
    return [c for c in candidates if c in trades.columns]


def walkforward_meta_predictions(trades, strategy_id):
    cols = feature_cols(trades)
    if len(trades) < 260:
        return None
    train_size = 300 if strategy_id == "BTC_10min" else 400
    test_size = 75 if strategy_id == "BTC_10min" else 100
    step = test_size
    if len(trades) < train_size + test_size:
        train_size = max(120, len(trades) // 2)
        test_size = max(30, min(80, (len(trades) - train_size) // 2 or len(trades) - train_size))
        step = test_size

    X = trades[cols].astype(float).replace([np.inf, -np.inf], np.nan).fillna(0).to_numpy()
    y = trades["signal_win_label"].astype(int).to_numpy()
    chunks = []
    i = train_size
    while i + test_size <= len(trades):
        train_y = y[i - train_size:i]
        if len(np.unique(train_y)) < 2:
            i += step
            continue
        model = make_model(7000 + i)
        model.fit(X[i - train_size:i], train_y)
        probs = model.predict_proba(X[i:i + test_size])[:, 1]
        part = trades.iloc[i:i + test_size].copy()
        part["meta_prob"] = probs
        chunks.append(part)
        i += step

    if not chunks:
        return None
    out = pd.concat(chunks, ignore_index=True)
    return {
        "rows": out,
        "feature_cols": cols,
        "train_size": train_size,
        "test_size": test_size,
        "step": step,
    }


def evaluate_policy(meta_rows, policy):
    keep = np.ones(len(meta_rows), dtype=bool)
    prob = meta_rows["meta_prob"].astype(float).to_numpy()
    th = float(policy.get("threshold", 0.55))
    name = policy["name"]

    if name == "current_meta_oos":
        keep = np.ones(len(meta_rows), dtype=bool)
    elif name == "meta_gate_all":
        keep = prob >= th
    elif name == "meta_gate_short_countertrend":
        counter = meta_rows["short_countertrend"].to_numpy(bool)
        keep = ~counter | (prob >= th)
    elif name == "meta_gate_htf_countertrend":
        counter = meta_rows["htf_countertrend"].to_numpy(bool)
        keep = ~counter | (prob >= th)
    elif name == "meta_gate_both_countertrend":
        counter = meta_rows["both_countertrend"].to_numpy(bool)
        keep = ~counter | (prob >= th)
    elif name == "meta_gate_down_countertrend":
        counter = meta_rows["short_countertrend"].to_numpy(bool) & (meta_rows["direction"] == "DOWN").to_numpy()
        keep = ~counter | (prob >= th)
    else:
        raise ValueError(f"unknown policy: {name}")

    wins = meta_rows["win"].to_numpy(bool)[keep]
    blocks, bsum = block_summary(meta_rows, keep)
    return {
        "name": name if name == "current_meta_oos" else f"{name}_th{int(round(th * 100))}",
        "policy": policy,
        "overall": metric(wins),
        "trade_retention_pct": round(float(keep.sum() / max(1, len(meta_rows)) * 100), 2),
        "time_block_summary": bsum,
        "time_blocks": blocks,
        "avg_meta_prob_selected": round(float(prob[keep].mean()), 4) if keep.any() else None,
    }


def evaluate_strategy(df5, strategy_id, cfg):
    trades = build_signal_trades(df5, strategy_id, cfg)
    meta = walkforward_meta_predictions(trades, strategy_id)
    if meta is None:
        return {
            "status": "insufficient_rows",
            "current_full": metric(trades["win"].to_numpy(bool)),
            "signal_trades": int(len(trades)),
        }
    rows = meta["rows"]
    baseline = evaluate_policy(rows, {"name": "current_meta_oos"})
    policies = [baseline]
    for th in THRESHOLDS:
        for name in [
            "meta_gate_all",
            "meta_gate_short_countertrend",
            "meta_gate_htf_countertrend",
            "meta_gate_both_countertrend",
            "meta_gate_down_countertrend",
        ]:
            policies.append(evaluate_policy(rows, {"name": name, "threshold": th}))
    base_wr = baseline["overall"]["wr"]
    for row in policies:
        row["wr_delta_pp"] = round(float(row["overall"]["wr"] or 0) - float(base_wr or 0), 2)
        row["trade_delta"] = int(row["overall"]["trades"] or 0) - int(baseline["overall"]["trades"] or 0)

    ranked = sorted(
        policies,
        key=lambda r: (
            r["overall"]["wr"],
            r["time_block_summary"]["min_block_wr"] or 0,
            -r["overall"]["max_loss"],
            r["overall"]["trades"],
        ),
        reverse=True,
    )
    usable = [
        r for r in ranked
        if r["overall"]["trades"] >= (80 if strategy_id == "BTC_10min" else 100)
        and r["overall"]["wr"] >= base_wr
        and r["trade_retention_pct"] >= 35
        and (r["time_block_summary"]["active_blocks"] or 0) >= 4
    ]
    return {
        "status": "ready",
        "method": {
            "train_size": meta["train_size"],
            "test_size": meta["test_size"],
            "step": meta["step"],
            "features": meta["feature_cols"],
            "meta_oos_rows": int(len(rows)),
        },
        "current_full": metric(trades["win"].to_numpy(bool)),
        "current_meta_oos": baseline,
        "countertrend_counts": {
            "signal_trades": int(len(trades)),
            "meta_oos_rows": int(len(rows)),
            "short_countertrend": int(rows["short_countertrend"].sum()),
            "htf_countertrend": int(rows["htf_countertrend"].sum()),
            "both_countertrend": int(rows["both_countertrend"].sum()),
            "down_short_countertrend": int((rows["short_countertrend"] & (rows["direction"] == "DOWN")).sum()),
        },
        "top_by_wr": ranked[:20],
        "top_usable": usable[:20],
    }


def main():
    cfg = read_json(CONFIG_FILE, {})
    trade_cfg = read_json(TRADE_CONFIG_FILE, {})
    df5 = load_symbol("btcusdt")
    if df5 is None:
        raise SystemExit("No BTC data found")

    report = {
        "method": {
            "type": "second_stage_signal_win_gate_walkforward",
            "payout": PAYOUT,
            "stake": STAKE,
            "breakeven_wr": round(BREAKEVEN_WR, 2),
            "note": "Research only. The gate predicts whether an existing first-stage signal wins; it does not change production config.",
        },
        "safety": {
            "autoTrade": trade_cfg.get("autoTrade"),
            "verdict": "research_only_do_not_resume_real_auto_trading",
        },
        "strategies": {},
        "conclusions": [],
    }
    for strategy_id in ["BTC_10min", "BTC_30min"]:
        result = evaluate_strategy(df5, strategy_id, cfg[strategy_id])
        report["strategies"][strategy_id] = result
        if result.get("status") == "ready":
            current = result["current_meta_oos"]["overall"]
            best = (result.get("top_usable") or result.get("top_by_wr") or [result["current_meta_oos"]])[0]
            report["conclusions"].append(
                f"{strategy_id}: meta-gate baseline WR {current.get('wr')}%/"
                f"{current.get('trades')} meta-OOS trades; best usable gate {best.get('name')} "
                f"WR {best['overall'].get('wr')}%/{best['overall'].get('trades')} trades "
                f"({best.get('wr_delta_pp'):+.2f}pp, retention {best.get('trade_retention_pct')}%)."
            )
        else:
            report["conclusions"].append(
                f"{strategy_id}: insufficient signal rows for a second-stage meta gate."
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
                    "delta": row.get("wr_delta_pp"),
                    "retention": row.get("trade_retention_pct"),
                    "max_loss": row["overall"]["max_loss"],
                }
                for row in (payload.get("top_usable") or payload.get("top_by_wr") or [])[:6]
            ]
            for sid, payload in report["strategies"].items()
        },
    }, indent=2, ensure_ascii=False))
    print(f"Saved {REPORT_FILE}")


if __name__ == "__main__":
    main()
