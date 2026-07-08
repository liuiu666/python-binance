from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_normal_liquidity_orderbook import (  # noqa: E402
    LiquidityNormalConfig,
    build_features,
    generate_signals,
    read_orderbook,
)
from research_second_normal_drawdown_router import max_drawdown, max_loss_streak  # noqa: E402
from second_backtest.data import load_second_bars  # noqa: E402


DEFAULT_DATA = ROOT / "tmp" / "latest_pull_20260708_204204" / "data"
OUT_JSON = ROOT / "tmp" / "v1_orderbook_up_quality_research.json"
OUT_TRADES = ROOT / "tmp" / "v1_orderbook_up_quality_trades.csv"
OUT_BUCKETS = ROOT / "tmp" / "v1_orderbook_up_quality_buckets.csv"


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, tuple):
        return [clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        value = float(value)
        return value if math.isfinite(value) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def payout(won: bool) -> float:
    return 4.0 if bool(won) else -5.0


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(rows, key=lambda r: int(r["idx"]))
    n = len(rows)
    wins = sum(1 for row in rows if row["won"])
    pnls = [payout(row["won"]) for row in rows]
    by_day = []
    if rows:
        frame = pd.DataFrame(rows)
        for day, group in frame.groupby(frame["time"].dt.strftime("%Y-%m-%d"), sort=True):
            items = group.to_dict("records")
            day_pnls = [payout(row["won"]) for row in items]
            day_wins = sum(1 for row in items if row["won"])
            by_day.append(
                {
                    "day": str(day),
                    "trades": int(len(items)),
                    "winRate": round(day_wins / len(items) * 100.0, 2),
                    "pnl": round(sum(day_pnls), 4),
                    "maxDrawdownU": max_drawdown(day_pnls),
                    "maxLoss": max_loss_streak(items),
                }
            )
    return {
        "trades": n,
        "wins": int(wins),
        "winRate": round(wins / n * 100.0, 2) if n else 0.0,
        "pnl": round(sum(pnls), 4),
        "maxDrawdownU": max_drawdown(pnls),
        "maxLoss": max_loss_streak(rows),
        "activeDays": len(by_day),
        "tradesPerActiveDay": round(n / len(by_day), 2) if by_day else 0.0,
        "losingDays": sum(1 for row in by_day if float(row["pnl"]) < 0),
        "worstDay": min(by_day, key=lambda row: float(row["pnl"])) if by_day else None,
        "byDay": by_day,
    }


def load_data(data_dir: Path) -> pd.DataFrame:
    seconds = data_dir / "btcusdt_1s_trades.csv"
    orderbook = data_dir / "btcusdt_orderbook_1s.csv"
    bars = load_second_bars(seconds, include_shards=True)
    ob = read_orderbook(orderbook, bars.index)
    data = bars.join(ob, how="left")
    data = data[data["ob_available"].fillna(False)].copy()
    data = data[~data.index.duplicated(keep="last")].sort_index()
    return data


def bps(now: float, prev: float) -> float:
    if not math.isfinite(now) or not math.isfinite(prev) or prev <= 0:
        return float("nan")
    return math.log(now / prev) * 10000.0


def add_quality_features(rows: list[dict[str, Any]], data: pd.DataFrame, features: pd.DataFrame) -> list[dict[str, Any]]:
    close = data["close"].to_numpy(float)
    low = data["low"].to_numpy(float)
    high = data["high"].to_numpy(float)
    buy = data["buy_qty"].to_numpy(float)
    sell = data["sell_qty"].to_numpy(float)
    imb = features["imbalance_20"].to_numpy(float)
    micro = features["micro_bps"].to_numpy(float)
    bid20 = features["bid_qty_20"].to_numpy(float)
    ask20 = features["ask_qty_20"].to_numpy(float)
    out: list[dict[str, Any]] = []
    for row in rows:
        idx = int(row["idx"])
        if idx < 1800 or idx + 600 >= len(close):
            continue
        item = dict(row)
        price = float(close[idx])
        for sec in (30, 60, 120, 300, 600, 1800):
            item[f"ret_{sec}s_bps"] = bps(price, float(close[idx - sec]))
        for sec in (120, 300, 600):
            start = max(0, idx - sec)
            if item["signal"] == "UP":
                seg = low[start : idx + 1]
                pos = int(np.nanargmin(seg))
                extreme_price = float(seg[pos])
            else:
                seg = high[start : idx + 1]
                pos = int(np.nanargmax(seg))
                extreme_price = float(seg[pos])
            extreme_idx = start + pos
            item[f"extreme_age_{sec}s"] = int(idx - extreme_idx)
            if item["signal"] == "UP":
                item[f"bounce_from_extreme_{sec}s_bps"] = (price / extreme_price - 1.0) * 10000.0
            else:
                item[f"bounce_from_extreme_{sec}s_bps"] = (extreme_price / price - 1.0) * 10000.0
        for sec in (30, 60, 120):
            start = max(0, idx - sec + 1)
            b = float(np.nansum(buy[start : idx + 1]))
            s = float(np.nansum(sell[start : idx + 1]))
            item[f"flow_{sec}s"] = (b - s) / (b + s) if b + s > 0 else 0.0
            item[f"imb20_mean_{sec}s"] = float(np.nanmean(imb[start : idx + 1]))
            item[f"micro_mean_{sec}s"] = float(np.nanmean(micro[start : idx + 1]))
        item["bid20_60s_chg"] = bid20[idx] / bid20[idx - 60] - 1.0 if bid20[idx - 60] > 0 else float("nan")
        item["ask20_60s_chg"] = ask20[idx] / ask20[idx - 60] - 1.0 if ask20[idx - 60] > 0 else float("nan")
        item["book_pressure_ratio"] = bid20[idx] / ask20[idx] if ask20[idx] > 0 else float("nan")
        out.append(item)
    return out


def bucketize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    up = pd.DataFrame([row for row in rows if row["signal"] == "UP"])
    if up.empty:
        return []
    specs = {
        "ret_300s_bps": [-999, -30, -15, -5, 0, 5, 15, 999],
        "ret_600s_bps": [-999, -40, -20, -5, 0, 10, 999],
        "flow_60s": [-2, -0.5, -0.2, 0, 0.2, 0.5, 2],
        "imbalance_20": [-2, 0.1, 0.3, 0.5, 0.7, 2],
        "imb20_mean_60s": [-2, -0.1, 0.0, 0.1, 0.25, 0.5, 2],
        "micro_bps": [-1, 0.001, 0.003, 0.005, 0.008, 1],
        "book_pressure_ratio": [0, 1, 1.5, 2.5, 4, 8, 999],
        "bid20_60s_chg": [-2, -0.5, -0.2, 0, 0.2, 0.5, 2, 999],
        "bounce_from_extreme_120s_bps": [-1, 0.5, 1, 2, 4, 8, 999],
        "extreme_age_120s": [-1, 5, 15, 30, 60, 120, 9999],
    }
    out: list[dict[str, Any]] = []
    for feature, bins in specs.items():
        up[f"{feature}_bucket"] = pd.cut(up[feature].astype(float), bins=bins, include_lowest=True)
        for bucket, group in up.groupby(f"{feature}_bucket", observed=True, sort=True):
            s = summarize(group.to_dict("records"))
            out.append(
                {
                    "feature": feature,
                    "bucket": str(bucket),
                    "trades": s["trades"],
                    "winRate": s["winRate"],
                    "pnl": s["pnl"],
                    "maxDrawdownU": s["maxDrawdownU"],
                    "maxLoss": s["maxLoss"],
                }
            )
    return out


def apply_gap(rows: list[dict[str, Any]], gap_sec: int = 600) -> list[dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    last_idx = -10**12
    for row in sorted(rows, key=lambda r: int(r["idx"])):
        idx = int(row["idx"])
        if idx - last_idx < gap_sec:
            continue
        accepted.append(row)
        last_idx = idx
    return accepted


def test_rules(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rules: dict[str, Callable[[dict[str, Any]], bool]] = {
        "baseline": lambda r: False,
        "up_ret300_pos": lambda r: r["signal"] == "UP" and float(r["ret_300s_bps"]) > 0,
        "up_imb60_too_positive": lambda r: r["signal"] == "UP" and float(r["imb20_mean_60s"]) > 0.25,
        "up_flow60_hot": lambda r: r["signal"] == "UP" and float(r["flow_60s"]) > 0.2,
        "up_book_too_one_sided": lambda r: r["signal"] == "UP" and float(r["book_pressure_ratio"]) > 4.0,
        "up_bid_fading": lambda r: r["signal"] == "UP" and float(r["bid20_60s_chg"]) < -0.2,
        "up_chase_or_hotbook": lambda r: r["signal"] == "UP" and (float(r["ret_300s_bps"]) > 0 or float(r["imb20_mean_60s"]) > 0.25),
        "up_chase_hotbook_or_fading": lambda r: r["signal"] == "UP" and (
            float(r["ret_300s_bps"]) > 0
            or float(r["imb20_mean_60s"]) > 0.25
            or float(r["bid20_60s_chg"]) < -0.2
        ),
    }
    results = []
    for name, pred in rules.items():
        candidate = [row for row in rows if not pred(row)]
        kept = apply_gap(candidate)
        removed = [row for row in rows if pred(row)]
        results.append(
            {
                "rule": name,
                "kept": summarize(kept),
                "removedRaw": summarize(removed),
                "bySide": [
                    {"side": side, **summarize([row for row in kept if row["signal"] == side])}
                    for side in ("UP", "DOWN")
                ],
            }
        )
    return results


def run(data_dir: Path = DEFAULT_DATA) -> dict[str, Any]:
    data = load_data(data_dir)
    cfg = LiquidityNormalConfig(
        normal_window_sec=600,
        z_entry=1.2,
        z_reclaim=0.85,
        mode="reclaim",
        retest_sec=120,
        inside_min=0.55,
        observed_min_pct=88.0,
        center_slope_sec=300,
        center_slope_max_bps=8.0,
        sigma_min_bps=5.0,
        sigma_max_bps=55.0,
        sigma_expand_max=1.9,
        ob_imbalance_min=0.08,
        micro_min_bps=0.001,
        wall_ratio_min=1.0,
        flow_guard=0.12,
        true_break_flow=0.28,
        true_break_imbalance=0.28,
        signal_gap_sec=600,
        horizon_sec=600,
        amount=5.0,
    )
    features = build_features(data, cfg.normal_window_sec, cfg)
    base_rows = generate_signals(data, features, cfg)
    rows = add_quality_features(base_rows, data, features)
    buckets = bucketize(rows)
    rule_results = test_rules(rows)
    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "data": {
            "dir": str(data_dir),
            "rows": int(len(data)),
            "start": data.index.min().isoformat(),
            "end": data.index.max().isoformat(),
            "hours": round((data.index.max() - data.index.min()).total_seconds() / 3600.0, 2),
            "observedPct": round(float(data["observed"].mean() * 100.0), 4),
        },
        "config": clean(cfg.__dict__),
        "baseline": summarize(rows),
        "baselineBySide": [
            {"side": side, **summarize([row for row in rows if row["signal"] == side])}
            for side in ("UP", "DOWN")
        ],
        "baselineByReason": [
            {"reason": reason, **summarize(group.to_dict("records"))}
            for reason, group in pd.DataFrame(rows).groupby("reason", sort=True)
        ] if rows else [],
        "ruleResults": rule_results,
        "bucketCsv": str(OUT_BUCKETS),
        "tradesCsv": str(OUT_TRADES),
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(rows).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    pd.DataFrame(buckets).to_csv(OUT_BUCKETS, index=False, encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    result = run()
    print(json.dumps(clean({
        "data": result["data"],
        "baseline": result["baseline"],
        "baselineBySide": result["baselineBySide"],
        "baselineByReason": result["baselineByReason"],
        "ruleResults": [
            {
                "rule": row["rule"],
                "kept": {
                    k: row["kept"][k]
                    for k in ("trades", "winRate", "pnl", "maxDrawdownU", "maxLoss", "byDay")
                },
                "removedRaw": {
                    k: row["removedRaw"][k]
                    for k in ("trades", "winRate", "pnl", "maxDrawdownU", "maxLoss")
                },
            }
            for row in result["ruleResults"]
        ],
    }), ensure_ascii=False))
