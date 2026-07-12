from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_normal_liquidity_orderbook import (  # noqa: E402
    LiquidityNormalConfig,
    build_features,
    load_local_data,
)


DATA_DIR = ROOT / "tmp" / "latest_pull_20260710_203217" / "data"
SECONDS = DATA_DIR / "btcusdt_1s_trades.csv"
ORDERBOOK = DATA_DIR / "btcusdt_orderbook_1s.csv"
OUT_JSON = ROOT / "tmp" / "regime_ob_strategy_backtest.json"
OUT_TRADES = ROOT / "tmp" / "regime_ob_strategy_backtest_trades.csv"


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def vol_regime(value: float) -> str:
    if not math.isfinite(value):
        return "na"
    if value < 35.0:
        return "low"
    if value < 60.0:
        return "mid"
    if value < 90.0:
        return "high"
    return "extreme"


def trend_regime(value: float) -> str:
    if not math.isfinite(value):
        return "na"
    if value <= -30.0:
        return "down"
    if value < -12.0:
        return "weak_down"
    if value <= 12.0:
        return "flat"
    if value < 30.0:
        return "weak_up"
    return "up"


def slope_regime(value: float) -> str:
    if value <= -8.0:
        return "moving_down"
    if value < -4.0:
        return "weak_down"
    if value <= 4.0:
        return "normal"
    if value < 8.0:
        return "weak_up"
    return "moving_up"


def payout(won: bool) -> int:
    return 4 if bool(won) else -5


def metrics(rows: pd.DataFrame, hours: float) -> dict[str, Any]:
    if rows.empty:
        return {
            "trades": 0,
            "wins": 0,
            "winRate": 0.0,
            "pnlU": 0,
            "maxDrawdownU": 0,
            "maxLoss": 0,
            "tradesPerDay": 0.0,
        }
    ordered = rows.sort_values("time")
    pnls = [payout(won) for won in ordered["won"].astype(bool)]
    equity = peak = drawdown = loss_streak = max_loss = 0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        if pnl > 0:
            loss_streak = 0
        else:
            loss_streak += 1
            max_loss = max(max_loss, loss_streak)
    wins = int(ordered["won"].astype(bool).sum())
    return {
        "trades": int(len(ordered)),
        "wins": wins,
        "winRate": round(wins / len(ordered) * 100.0, 2),
        "pnlU": int(sum(pnls)),
        "maxDrawdownU": int(drawdown),
        "maxLoss": int(max_loss),
        "tradesPerDay": round(len(ordered) / hours * 24.0, 2) if hours > 0 else 0.0,
    }


def config(window: int) -> LiquidityNormalConfig:
    return LiquidityNormalConfig(
        normal_window_sec=window,
        z_entry=0.8,
        z_reclaim=0.8,
        mode="hybrid",
        retest_sec=120,
        inside_min=0.45,
        observed_min_pct=88.0,
        center_slope_sec=300,
        center_slope_max_bps=999.0,
        sigma_min_bps=1.0,
        sigma_max_bps=55.0,
        sigma_expand_max=1.9,
        signal_gap_sec=600,
        horizon_sec=600,
        amount=5.0,
    )


def strategy_decision(style: str, side: str, s: str, t30: str, v30: str) -> str | None:
    # Reversal is the default edge. Trend is allowed only in mid-vol weak-up.
    if style == "fade":
        if v30 == "mid" and t30 in {"weak_down", "down"}:
            return "mid_down_fade"
        if v30 == "mid" and t30 == "flat":
            return "mid_flat_fade"
        if v30 == "low" and t30 == "flat" and s in {"moving_down", "moving_up", "weak_up"}:
            return "low_flat_offset_fade"
        if v30 == "high" and t30 == "up" and s == "normal":
            return "high_up_normal_fade"
    if style == "trend":
        if v30 == "mid" and t30 == "weak_up":
            return "mid_weakup_trend"
    return None


def orderbook_ok(row: dict[str, Any], variant: str) -> bool:
    if variant == "core":
        return True
    if not (-0.5 < row["support_chg60"] < 3.0):
        return False
    if row["sigma_expand"] > 1.05:
        return False
    if variant == "ob_guard":
        return True
    if row["style"] == "trend":
        return (
            row["signed_imb20"] >= 0.08
            and row["signed_micro"] >= 0.001
            and row["signed_flow60"] >= 0.0
            and row["support_ratio"] >= 1.0
            and row["support_chg60"] > 0.0
        )
    # For reversal, do not require strong same-side book. Just avoid collapsing support.
    return True


def build_candidates(data: pd.DataFrame, window: int) -> list[dict[str, Any]]:
    cfg = config(window)
    features = build_features(data, window, cfg)
    close = data["close"].to_numpy(float)
    high = data["high"].astype(float)
    low = data["low"].astype(float)
    close_s = data["close"].astype(float)
    ret30 = np.log(close_s / close_s.shift(1800)) * 10000.0
    range30 = (high.rolling(1800, min_periods=900).max() / low.rolling(1800, min_periods=900).min() - 1.0) * 10000.0

    z = features["z"].to_numpy(float)
    zmax = features["z_max_retest"].to_numpy(float)
    zmin = features["z_min_retest"].to_numpy(float)
    valid = (np.arange(len(data)) >= max(window, 3600) + 5) & (np.arange(len(data)) < len(data) - 600)
    normal = (
        valid
        & np.isfinite(features["z"])
        & np.isfinite(features["center_slope_bps"])
        & (features["inside1_ratio"] >= 0.45)
        & (features["observed_pct"] >= 88.0)
        & (features["sigma_bps"] >= 1.0)
        & (features["sigma_bps"] <= 55.0)
        & (features["sigma_expand"] <= 1.9)
    )

    fade_down = (z >= 0.8) | ((zmax >= 0.8) & (z >= 0.0) & (z <= 0.8))
    fade_up = (z <= -0.8) | ((zmin <= -0.8) & (z <= 0.0) & (z >= -0.8))
    trend_up = fade_down
    trend_down = fade_up

    out: list[dict[str, Any]] = []
    for style, downs, ups in (
        ("fade", fade_down, fade_up),
        ("trend", trend_down, trend_up),
    ):
        indices = [(i, "DOWN") for i in np.flatnonzero(normal & downs)]
        indices += [(i, "UP") for i in np.flatnonzero(normal & ups)]
        for idx, signal in sorted(indices, key=lambda item: item[0]):
            entry = float(close[idx])
            settle = float(close[idx + 600])
            move = math.log(settle / entry) * 10000.0
            won = move > 0.0 if signal == "UP" else move < 0.0
            slope = float(features["center_slope_bps"].iloc[idx])
            current_ret30 = float(ret30.iloc[idx])
            current_range30 = float(range30.iloc[idx])
            s = slope_regime(slope)
            t30 = trend_regime(current_ret30)
            v30 = vol_regime(current_range30)
            regime = strategy_decision(style, signal, s, t30, v30)
            if regime is None:
                continue

            bid20 = float(features["bid_qty_20"].iloc[idx])
            ask20 = float(features["ask_qty_20"].iloc[idx])
            bid_chg60 = ask_chg60 = float("nan")
            if idx >= 60:
                prev_bid = float(features["bid_qty_20"].iloc[idx - 60])
                prev_ask = float(features["ask_qty_20"].iloc[idx - 60])
                bid_chg60 = bid20 / prev_bid - 1.0 if prev_bid > 0 else float("nan")
                ask_chg60 = ask20 / prev_ask - 1.0 if prev_ask > 0 else float("nan")

            sign = 1.0 if signal == "UP" else -1.0
            support = bid20 if signal == "UP" else ask20
            oppose = ask20 if signal == "UP" else bid20
            support_chg60 = bid_chg60 if signal == "UP" else ask_chg60
            out.append(
                {
                    "time": data.index[idx],
                    "time_shanghai": data.index[idx].tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S"),
                    "idx": int(idx),
                    "window": window,
                    "style": style,
                    "regime": regime,
                    "signal": signal,
                    "entry": round(entry, 4),
                    "settle": round(settle, 4),
                    "move_bps": round(float(move), 4),
                    "won": bool(won),
                    "pnl": payout(won),
                    "slope_regime": s,
                    "trend30_regime": t30,
                    "vol30_regime": v30,
                    "slope_bps": round(slope, 4),
                    "ret30_bps": round(current_ret30, 4),
                    "range30_bps": round(current_range30, 4),
                    "sigma_expand": round(float(features["sigma_expand"].iloc[idx]), 6),
                    "sigma_bps": round(float(features["sigma_bps"].iloc[idx]), 6),
                    "z": round(float(features["z"].iloc[idx]), 6),
                    "signed_imb20": round(sign * float(features["imbalance_20"].iloc[idx]), 6),
                    "signed_micro": round(sign * float(features["micro_bps"].iloc[idx]), 6),
                    "signed_flow60": round(sign * float(features["flow_60"].iloc[idx]), 6),
                    "support_ratio": round(support / oppose, 6) if oppose > 0 else None,
                    "support_chg60": round(float(support_chg60), 6),
                }
            )
    return out


def apply_cooldown(rows: list[dict[str, Any]], variant: str) -> pd.DataFrame:
    accepted: list[dict[str, Any]] = []
    last_time: pd.Timestamp | None = None
    # Prefer longer-window candidates when multiple windows fire at the same second.
    priority = {"mid_weakup_trend": 0, "mid_down_fade": 1, "mid_flat_fade": 2, "low_flat_offset_fade": 3}
    sorted_rows = sorted(rows, key=lambda row: (pd.Timestamp(row["time"]), priority.get(row["regime"], 9), -int(row["window"])))
    for row in sorted_rows:
        if not orderbook_ok(row, variant):
            continue
        timestamp = pd.Timestamp(row["time"])
        if last_time is not None and (timestamp - last_time).total_seconds() < 600:
            continue
        item = dict(row)
        item["variant"] = variant
        accepted.append(item)
        last_time = timestamp
    return pd.DataFrame(accepted)


def summarize_splits(rows: pd.DataFrame, hours: float) -> dict[str, Any]:
    result = {"overall": metrics(rows, hours)}
    for key in ("window", "style", "regime", "signal", "vol30_regime", "trend30_regime", "slope_regime"):
        result[f"by_{key}"] = {
            str(name): metrics(group, hours)
            for name, group in rows.groupby(key, sort=True)
        } if not rows.empty else {}
    return result


def run() -> dict[str, Any]:
    data = load_local_data(SECONDS, ORDERBOOK)
    start = pd.Timestamp("2026-07-09T16:00:00Z")
    end = pd.Timestamp("2026-07-10T16:00:00Z")
    data = data[(data.index >= start) & (data.index < end)].copy()
    hours = (data.index.max() - data.index.min()).total_seconds() / 3600.0

    candidates: list[dict[str, Any]] = []
    for window in (120, 600, 900):
        candidates.extend(build_candidates(data, window))

    all_trades = []
    reports = {}
    for variant in ("core", "ob_guard", "ob_strict"):
        trades = apply_cooldown(candidates, variant)
        reports[variant] = summarize_splits(trades, hours)
        all_trades.append(trades)

    trades_out = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    trades_out.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "data": {
            "seconds": str(SECONDS),
            "orderbook": str(ORDERBOOK),
            "start": data.index.min(),
            "end": data.index.max(),
            "hours": round(hours, 4),
            "rows": len(data),
        },
        "rules": {
            "volatility": "30m high-low range: low<35bp, mid 35-60bp, high 60-90bp, extreme>=90bp",
            "trend": "30m return: down<=-30bp, weak_down -30~-12bp, flat -12~12bp, weak_up 12~30bp, up>=30bp",
            "normal_slope": "5m center slope: moving_down<=-8bp, weak_down -8~-4bp, normal -4~4bp, weak_up 4~8bp, moving_up>=8bp",
            "core": "mid down/weak_down fade; mid flat fade; low flat offset fade; high up normal fade; mid weak_up trend",
            "ob_guard": "core + -0.5 < support_chg60 < 3 and sigma_expand <= 1.05",
            "ob_strict": "ob_guard + trend requires signed book/flow support",
        },
        "reports": reports,
        "tradesCsv": str(OUT_TRADES),
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    result = run()
    print(json.dumps(clean({"data": result["data"], "reports": result["reports"]}), ensure_ascii=False, indent=2))
