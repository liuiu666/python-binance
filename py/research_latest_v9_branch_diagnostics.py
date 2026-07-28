"""Diagnose the latest local V9 replay by branch and market state.

This script is intentionally read-only: it uses the downloaded local files in
data/server_latest and the already replayed V9 trades CSV, then writes a compact
diagnostic report under tmp/.
"""

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

from backtest_io import read_orderbook  # noqa: E402
from backtest_online_strategies_latest import liquidity_rules, metrics, variant  # noqa: E402
from current_v2_augmented_v9_core import AugmentedV9Rules, build_minute_features  # noqa: E402
from liquidity_v2_core import build_features  # noqa: E402
from second_backtest.data import load_second_bars  # noqa: E402


DATA = ROOT / "data" / "server_latest"
SECONDS = DATA / "btcusdt_1s_trades.csv"
ORDERBOOK = DATA / "btcusdt_orderbook_1s.csv"
CONFIG = DATA / "trade_config.json"
TRADES = ROOT / "tmp" / "latest_v9_local_backtest_20260716_trades.csv"
OUT_JSON = ROOT / "tmp" / "latest_v9_branch_diagnostics_20260716.json"
OUT_CSV = ROOT / "tmp" / "latest_v9_branch_diagnostics_20260716.csv"
STRATEGY_ID = "BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V9"


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
    if pd.isna(value):
        return None
    return value


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG.read_text(encoding="utf-8-sig"))


def summarize_signed(frame: pd.DataFrame, column: str, hours: float) -> dict[str, Any]:
    if frame.empty:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "winRate": None,
            "pnlU": 0.0,
            "maxDrawdownU": 0.0,
            "maxLossStreak": 0,
            "tradesPerDay": 0.0,
            "medianSignedBps": None,
            "thinAbsLe3bpPct": None,
        }
    wins = frame[column].astype(float) > 0.0
    pnl = np.where(wins, 4.0, -5.0)
    equity = np.r_[0.0, np.cumsum(pnl)]
    drawdown = np.maximum.accumulate(equity) - equity
    streak = max_streak = 0
    for won in wins:
        streak = 0 if won else streak + 1
        max_streak = max(max_streak, streak)
    return {
        "trades": int(len(frame)),
        "wins": int(wins.sum()),
        "losses": int((~wins).sum()),
        "winRate": round(float(wins.mean()) * 100.0, 2),
        "pnlU": round(float(pnl.sum()), 2),
        "maxDrawdownU": round(float(drawdown.max()), 2),
        "maxLossStreak": int(max_streak),
        "tradesPerDay": round(len(frame) / max(hours, 1e-9) * 24.0, 2),
        "medianSignedBps": round(float(frame[column].median()), 3),
        "thinAbsLe3bpPct": round(float(frame[column].abs().le(3.0).mean()) * 100.0, 2),
    }


def price_state(row: pd.Series) -> str:
    ret600 = float(row.get("ret_600s_bps", np.nan))
    ret1800 = float(row.get("ret_1800s_bps", np.nan))
    pos1800 = float(row.get("pos_1800s", np.nan))
    sigma = float(row.get("sigma_bps", np.nan))
    if not all(math.isfinite(v) for v in (ret600, ret1800, pos1800, sigma)):
        return "unknown"
    if ret1800 >= 15.0 and pos1800 >= 0.70:
        return "mature_uptrend"
    if ret1800 <= -15.0 and pos1800 <= 0.30:
        return "mature_downtrend"
    if abs(ret600) >= max(10.0, sigma * 0.75):
        return "short_migration_up" if ret600 > 0 else "short_migration_down"
    return "range_or_chop"


def side_alignment(signal: str, row: pd.Series) -> str:
    direction = 1.0 if signal == "UP" else -1.0
    ret300 = float(row.get("ret_300s_bps", np.nan))
    ret600 = float(row.get("ret_600s_bps", np.nan))
    if not math.isfinite(ret300) or not math.isfinite(ret600):
        return "unknown"
    if direction * ret300 > 0 and direction * ret600 > 0:
        return "with_5m_10m_trend"
    if direction * ret300 < 0 and direction * ret600 < 0:
        return "against_5m_10m_trend"
    return "mixed_trend"


def row_at(frame: pd.DataFrame, timestamp: pd.Timestamp) -> pd.Series | None:
    pos = int(frame.index.searchsorted(timestamp, side="right") - 1)
    if pos < 0:
        return None
    return frame.iloc[pos]


def annotate_trades(data: pd.DataFrame, trades: pd.DataFrame, cfg_row: dict[str, Any]) -> pd.DataFrame:
    liq_rules = liquidity_rules(cfg_row)
    liq_features = build_features(data, liq_rules)
    minute_features = build_minute_features(data, AugmentedV9Rules.from_config(cfg_row))

    close = data["close"].astype(float)
    second_features = pd.DataFrame(index=data.index)
    for sec in (10, 30, 60, 120, 300, 600, 900, 1800):
        second_features[f"px_ret_{sec}s_bps"] = np.log(close / close.shift(sec)) * 10000.0
    ret1 = close.pct_change().abs() * 10000.0
    second_features["path_600_bps"] = ret1.rolling(600, min_periods=120).sum()
    second_features["efficiency_600"] = second_features["px_ret_600s_bps"].abs() / second_features["path_600_bps"].replace(0.0, np.nan)
    second_features["vol_60s_bps"] = ret1.rolling(60, min_periods=30).std(ddof=0)
    second_features["vol_600s_bps"] = ret1.rolling(600, min_periods=120).std(ddof=0)
    second_features["buy_30"] = data["buy_qty"].astype(float).rolling(30, min_periods=10).sum()
    second_features["sell_30"] = data["sell_qty"].astype(float).rolling(30, min_periods=10).sum()
    second_features["flow_30"] = (
        (second_features["buy_30"] - second_features["sell_30"])
        / (second_features["buy_30"] + second_features["sell_30"]).replace(0.0, np.nan)
    )

    annotated: list[dict[str, Any]] = []
    for item in trades.sort_values("time").to_dict("records"):
        timestamp = pd.Timestamp(item["time"])
        liq = row_at(liq_features, timestamp)
        sec = row_at(second_features, timestamp)
        minute_time = timestamp.floor("min")
        minute = row_at(minute_features, minute_time)
        if liq is None or sec is None:
            continue
        row: dict[str, Any] = dict(item)
        row["won_d6"] = float(row.get("signed_bps_d6", 0.0)) > 0.0
        row["thin_d6"] = abs(float(row.get("signed_bps_d6", 0.0))) <= 3.0
        for key in (
            "z",
            "inside1_ratio",
            "observed_pct",
            "center_slope_bps",
            "sigma_bps",
            "sigma_expand",
            "flow_60",
            "slope_30_bps",
            "slope_90_bps",
            "ret_300s_bps",
            "ret_600s_bps",
            "ret_900s_bps",
            "ret_1800s_bps",
            "pos_600s",
            "pos_1800s",
            "range_600s_bps",
            "range_1800s_bps",
            "imbalance_20",
            "micro_bps",
            "spread_bps",
            "bid20_chg_30",
            "bid20_chg_60",
            "ask20_chg_30",
            "wall_balance",
        ):
            row[key] = liq.get(key)
        for key in (
            "px_ret_10s_bps",
            "px_ret_30s_bps",
            "px_ret_60s_bps",
            "px_ret_120s_bps",
            "px_ret_300s_bps",
            "px_ret_600s_bps",
            "px_ret_900s_bps",
            "px_ret_1800s_bps",
            "path_600_bps",
            "efficiency_600",
            "vol_60s_bps",
            "vol_600s_bps",
            "flow_30",
        ):
            row[key] = sec.get(key)
        if minute is not None:
            for key in ("efficiency_10", "trend_strength", "z_30", "ret_1", "ret_3", "ret_10", "volume_ratio"):
                row[f"minute_{key}"] = minute.get(key)
        row["price_state"] = price_state(liq)
        row["side_alignment"] = side_alignment(str(row["signal"]), liq)
        annotated.append(row)
    return pd.DataFrame(annotated)


def feature_compare(frame: pd.DataFrame) -> dict[str, Any]:
    keys = [
        "signed_bps_d6",
        "z",
        "sigma_bps",
        "sigma_expand",
        "inside1_ratio",
        "center_slope_bps",
        "ret_300s_bps",
        "ret_600s_bps",
        "ret_1800s_bps",
        "pos_1800s",
        "flow_60",
        "flow_30",
        "imbalance_20",
        "micro_bps",
        "bid20_chg_60",
        "ask20_chg_30",
        "efficiency_600",
        "minute_efficiency_10",
        "minute_trend_strength",
        "minute_z_30",
        "minute_volume_ratio",
    ]
    out: dict[str, Any] = {}
    for label, group in (("wins", frame[frame["won_d6"]]), ("losses", frame[~frame["won_d6"]]), ("thin", frame[frame["thin_d6"]])):
        out[label] = {
            key: round(float(pd.to_numeric(group[key], errors="coerce").median()), 4)
            for key in keys
            if key in group and pd.to_numeric(group[key], errors="coerce").notna().any()
        }
        out[label]["count"] = int(len(group))
    return out


def run() -> dict[str, Any]:
    cfg = load_config()
    cfg_row = variant(cfg, STRATEGY_ID)
    bars = load_second_bars(SECONDS, include_shards=False)
    data = bars.join(read_orderbook(ORDERBOOK, bars.index, max_age_sec=3), how="left").sort_index()
    trades = pd.read_csv(TRADES, parse_dates=["time"])
    annotated = annotate_trades(data, trades, cfg_row)
    annotated.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    start = pd.Timestamp(trades["time"].min())
    end = pd.Timestamp(trades["time"].max())
    span_hours = max((end - start).total_seconds() / 3600.0, 1.0)
    data_hours = max((data.index.max() - data.index.min()).total_seconds() / 3600.0, 1.0)

    by_branch = {
        str(branch): summarize_signed(group, "signed_bps_d6", data_hours)
        for branch, group in annotated.groupby("branch")
    }
    by_state = {
        str(state): summarize_signed(group, "signed_bps_d6", data_hours)
        for state, group in annotated.groupby("price_state")
    }
    by_alignment = {
        str(state): summarize_signed(group, "signed_bps_d6", data_hours)
        for state, group in annotated.groupby("side_alignment")
    }
    by_day = {
        str(day): summarize_signed(group, "signed_bps_d6", 24.0)
        for day, group in annotated.groupby("beijing_day")
    }
    supplement_only = annotated[annotated["branch"].eq("exhaustion_orderbook_supplement")]
    original_only = annotated[annotated["branch"].eq("current_v2_original")]
    report = {
        "data": {
            "seconds": str(SECONDS),
            "orderbook": str(ORDERBOOK),
            "start": data.index.min(),
            "end": data.index.max(),
            "hours": round(data_hours, 3),
            "orderbookCoveragePct": round(float(data["ob_available"].mean()) * 100.0, 4),
        },
        "tradeWindow": {"start": start, "end": end, "hoursBetweenFirstLastTrade": round(span_hours, 3)},
        "currentCombined": summarize_signed(annotated, "signed_bps_d6", data_hours),
        "supplementOnly": summarize_signed(supplement_only, "signed_bps_d6", data_hours),
        "originalOnly": summarize_signed(original_only, "signed_bps_d6", data_hours),
        "byDay": by_day,
        "byBranch": by_branch,
        "byPriceState": by_state,
        "bySideAlignment": by_alignment,
        "featureCompare": feature_compare(annotated),
        "losses": annotated[~annotated["won_d6"]][
            [
                "time",
                "beijing_day",
                "signal",
                "branch",
                "signed_bps_d6",
                "price_state",
                "side_alignment",
                "z",
                "sigma_bps",
                "ret_300s_bps",
                "ret_600s_bps",
                "ret_1800s_bps",
                "pos_1800s",
                "flow_60",
                "imbalance_20",
                "micro_bps",
                "bid20_chg_60",
                "minute_efficiency_10",
                "minute_trend_strength",
                "minute_z_30",
            ]
        ].to_dict("records"),
        "thinWinsOrLossesLe3bp": annotated[annotated["thin_d6"]][
            ["time", "beijing_day", "signal", "branch", "signed_bps_d6", "price_state", "side_alignment", "z", "ret_600s_bps", "flow_60"]
        ].to_dict("records"),
        "conclusion": [
            "Recent sample is small; do not fit new numeric thresholds from these 11 trades alone.",
            "Original V2 is the weak part in the latest pull; supplement branch contributes most recent edge.",
            "Next research should search for a separate high-frequency branch, not only filter the current one.",
        ],
        "csv": str(OUT_CSV),
    }
    OUT_JSON.write_text(json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
