from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR / "py"))

from second_backtest.data import load_second_bars  # noqa: E402
from second_backtest.metrics import payout_for_horizon, summarize_trades  # noqa: E402


DATA_ROOT = APP_DIR / "tmp" / "latest_pull_20260706_2130" / "data"
DEFAULT_SECONDS = DATA_ROOT / "btcusdt_1s_trades.csv"
DEFAULT_ORDERBOOK = DATA_ROOT / "btcusdt_orderbook_1s.csv"
OUT_JSON = APP_DIR / "tmp" / "normal_liquidity_orderbook_research.json"
OUT_TRADES = APP_DIR / "tmp" / "normal_liquidity_orderbook_trades.csv"
OUT_GRID = APP_DIR / "tmp" / "normal_liquidity_orderbook_grid.csv"


@dataclass(frozen=True)
class LiquidityNormalConfig:
    normal_window_sec: int = 900
    z_entry: float = 1.2
    z_reclaim: float = 0.85
    mode: str = "hybrid"  # edge, reclaim, hybrid
    retest_sec: int = 120
    inside_min: float = 0.58
    observed_min_pct: float = 88.0
    center_slope_sec: int = 300
    center_slope_max_bps: float = 8.0
    sigma_min_bps: float = 5.0
    sigma_max_bps: float = 55.0
    sigma_expand_max: float = 1.9
    ob_imbalance_min: float = 0.12
    micro_min_bps: float = 0.001
    wall_ratio_min: float = 1.0
    flow_guard: float = 0.12
    true_break_flow: float = 0.28
    true_break_imbalance: float = 0.28
    signal_gap_sec: int = 600
    horizon_sec: int = 600
    amount: float = 5.0

    @property
    def strategy_id(self) -> str:
        mode = self.mode.upper()
        return (
            f"NL_OB_{mode}_W{self.normal_window_sec}"
            f"_Z{int(self.z_entry * 100)}_OB{int(self.ob_imbalance_min * 100)}"
            f"_IN{int(self.inside_min * 100)}"
        )


def read_orderbook(path: Path, target_index: pd.DatetimeIndex) -> pd.DataFrame:
    usecols = [
        "timestamp",
        "mid",
        "spread_bps",
        "bid_qty_20",
        "ask_qty_20",
        "imbalance_5",
        "imbalance_20",
        "microprice_edge_bps",
        "bid_wall_bps",
        "ask_wall_bps",
        "bid_wall_qty",
        "ask_wall_qty",
    ]
    ob = pd.read_csv(path, usecols=lambda col: col in set(usecols))
    ob["time"] = pd.to_datetime(ob["timestamp"], utc=True, errors="coerce").dt.floor("s")
    ob = ob.dropna(subset=["time"]).drop(columns=["timestamp"], errors="ignore")
    for col in ob.columns:
        if col != "time":
            ob[col] = pd.to_numeric(ob[col], errors="coerce")
    ob = ob.sort_values("time").drop_duplicates("time", keep="last").set_index("time")
    aligned = ob.reindex(target_index, method="ffill", limit=3)
    aligned["ob_available"] = aligned["mid"].notna()
    for col in aligned.columns:
        if col != "ob_available":
            aligned[col] = aligned[col].astype(float)
    return aligned


def load_local_data(seconds_path: Path, orderbook_path: Path) -> pd.DataFrame:
    # The main CSV and the order-book CSV share the same recent local pull.
    # Do not include older shards here, because those dates have no order book.
    bars = load_second_bars(seconds_path, include_shards=False)
    ob = read_orderbook(orderbook_path, bars.index)
    data = bars.join(ob, how="left")
    data = data[data["ob_available"].fillna(False)].copy()
    data = data[~data.index.duplicated(keep="last")].sort_index()
    return data


def rolling_sum(arr: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    return arr.rolling(window, min_periods=min_periods or max(30, window // 3)).sum()


def build_features(data: pd.DataFrame, window: int, cfg: LiquidityNormalConfig) -> pd.DataFrame:
    close = data["close"].astype(float)
    volume = data["volume"].astype(float).clip(lower=0.0)
    observed = data["observed"].astype(bool).astype(float)

    sw = rolling_sum(volume, window)
    sx = rolling_sum(close * volume, window)
    sx2 = rolling_sum(close * close * volume, window)
    mean = close.rolling(window, min_periods=max(120, window // 3)).mean()
    std = close.rolling(window, min_periods=max(120, window // 3)).std(ddof=1)

    vwap = sx / sw.replace(0, np.nan)
    var = sx2 / sw.replace(0, np.nan) - vwap * vwap
    vw_sigma = np.sqrt(var.clip(lower=0.0))
    center = vwap.fillna(mean)
    sigma = vw_sigma.where(vw_sigma > 1e-9, std)
    z = (close - center) / sigma.replace(0, np.nan)

    inside1 = z.abs().le(1.0).astype(float)
    sigma_bps = sigma / close * 10000.0
    sigma_median = sigma_bps.rolling(max(window, 900), min_periods=max(120, window // 3)).median()
    center_slope = (center / center.shift(cfg.center_slope_sec) - 1.0) * 10000.0

    buy_60 = data["buy_qty"].rolling(60, min_periods=10).sum()
    sell_60 = data["sell_qty"].rolling(60, min_periods=10).sum()
    flow_60 = (buy_60 - sell_60) / (buy_60 + sell_60).replace(0, np.nan)
    slope_30 = (close / close.shift(30) - 1.0) * 10000.0
    slope_90 = (close / close.shift(90) - 1.0) * 10000.0

    bid20 = data["bid_qty_20"].astype(float)
    ask20 = data["ask_qty_20"].astype(float)
    bid_wall = data["bid_wall_qty"].astype(float)
    ask_wall = data["ask_wall_qty"].astype(float)
    wall_balance = (bid_wall - ask_wall) / (bid_wall + ask_wall).replace(0, np.nan)
    bid20_chg_30 = bid20 / bid20.shift(30).replace(0, np.nan) - 1.0
    ask20_chg_30 = ask20 / ask20.shift(30).replace(0, np.nan) - 1.0

    out = pd.DataFrame(index=data.index)
    out["close"] = close
    out["center"] = center
    out["sigma"] = sigma
    out["z"] = z
    out["normal_low"] = center - sigma
    out["normal_high"] = center + sigma
    out["inside1_ratio"] = inside1.rolling(window, min_periods=max(120, window // 3)).mean()
    out["observed_pct"] = observed.rolling(min(600, window), min_periods=120).mean() * 100.0
    out["center_slope_bps"] = center_slope
    out["sigma_bps"] = sigma_bps
    out["sigma_expand"] = sigma_bps / sigma_median.replace(0, np.nan)
    out["flow_60"] = flow_60
    out["slope_30_bps"] = slope_30
    out["slope_90_bps"] = slope_90
    out["imbalance_5"] = data["imbalance_5"].astype(float)
    out["imbalance_20"] = data["imbalance_20"].astype(float)
    out["micro_bps"] = data["microprice_edge_bps"].astype(float)
    out["spread_bps"] = data["spread_bps"].astype(float)
    out["bid_qty_20"] = bid20
    out["ask_qty_20"] = ask20
    out["bid20_chg_30"] = bid20_chg_30
    out["ask20_chg_30"] = ask20_chg_30
    out["wall_balance"] = wall_balance
    out["z_max_retest"] = z.rolling(cfg.retest_sec, min_periods=10).max()
    out["z_min_retest"] = z.rolling(cfg.retest_sec, min_periods=10).min()
    return out


def normal_ready(row: pd.Series, cfg: LiquidityNormalConfig) -> bool:
    values = [
        row.get("z"),
        row.get("inside1_ratio"),
        row.get("observed_pct"),
        row.get("center_slope_bps"),
        row.get("sigma_bps"),
        row.get("sigma_expand"),
    ]
    if any(not np.isfinite(float(x)) for x in values):
        return False
    return (
        float(row["inside1_ratio"]) >= cfg.inside_min
        and float(row["observed_pct"]) >= cfg.observed_min_pct
        and abs(float(row["center_slope_bps"])) <= cfg.center_slope_max_bps
        and cfg.sigma_min_bps <= float(row["sigma_bps"]) <= cfg.sigma_max_bps
        and float(row["sigma_expand"]) <= cfg.sigma_expand_max
    )


def passive_resistance(row: pd.Series, cfg: LiquidityNormalConfig) -> bool:
    ask = float(row.get("ask_qty_20", np.nan))
    bid = float(row.get("bid_qty_20", np.nan))
    return (
        np.isfinite(ask)
        and np.isfinite(bid)
        and float(row["imbalance_20"]) <= -cfg.ob_imbalance_min
        and float(row["micro_bps"]) <= -cfg.micro_min_bps
        and ask >= max(1e-9, bid * cfg.wall_ratio_min)
        and float(row.get("ask20_chg_30", 0.0)) > -0.55
    )


def passive_support(row: pd.Series, cfg: LiquidityNormalConfig) -> bool:
    ask = float(row.get("ask_qty_20", np.nan))
    bid = float(row.get("bid_qty_20", np.nan))
    return (
        np.isfinite(ask)
        and np.isfinite(bid)
        and float(row["imbalance_20"]) >= cfg.ob_imbalance_min
        and float(row["micro_bps"]) >= cfg.micro_min_bps
        and bid >= max(1e-9, ask * cfg.wall_ratio_min)
        and float(row.get("bid20_chg_30", 0.0)) > -0.55
    )


def true_break_up(row: pd.Series, cfg: LiquidityNormalConfig) -> bool:
    return (
        float(row["flow_60"]) >= cfg.true_break_flow
        or float(row["imbalance_20"]) >= cfg.true_break_imbalance
        or float(row["micro_bps"]) >= cfg.micro_min_bps * 4.0
    )


def true_break_down(row: pd.Series, cfg: LiquidityNormalConfig) -> bool:
    return (
        float(row["flow_60"]) <= -cfg.true_break_flow
        or float(row["imbalance_20"]) <= -cfg.true_break_imbalance
        or float(row["micro_bps"]) <= -cfg.micro_min_bps * 4.0
    )


def edge_signal(row: pd.Series, cfg: LiquidityNormalConfig) -> tuple[str | None, str | None]:
    z = float(row["z"])
    flow = float(row["flow_60"])
    if z >= cfg.z_entry and passive_resistance(row, cfg) and flow <= cfg.flow_guard and not true_break_up(row, cfg):
        return "DOWN", "upper_passive_resistance_fade"
    if z <= -cfg.z_entry and passive_support(row, cfg) and flow >= -cfg.flow_guard and not true_break_down(row, cfg):
        return "UP", "lower_passive_support_fade"
    return None, None


def reclaim_signal(row: pd.Series, cfg: LiquidityNormalConfig) -> tuple[str | None, str | None]:
    z = float(row["z"])
    flow = float(row["flow_60"])
    if (
        float(row["z_max_retest"]) >= cfg.z_entry
        and 0.0 <= z <= cfg.z_reclaim
        and passive_resistance(row, cfg)
        and flow <= cfg.flow_guard
    ):
        return "DOWN", "upper_fake_break_reclaim"
    if (
        float(row["z_min_retest"]) <= -cfg.z_entry
        and -cfg.z_reclaim <= z <= 0.0
        and passive_support(row, cfg)
        and flow >= -cfg.flow_guard
    ):
        return "UP", "lower_fake_break_reclaim"
    return None, None


def generate_signals(data: pd.DataFrame, features: pd.DataFrame, cfg: LiquidityNormalConfig) -> list[dict[str, Any]]:
    close = data["close"].to_numpy(float)
    z = features["z"].to_numpy(float)
    inside1 = features["inside1_ratio"].to_numpy(float)
    observed = features["observed_pct"].to_numpy(float)
    center_slope = features["center_slope_bps"].to_numpy(float)
    sigma_bps = features["sigma_bps"].to_numpy(float)
    sigma_expand = features["sigma_expand"].to_numpy(float)
    flow = features["flow_60"].to_numpy(float)
    imb20 = features["imbalance_20"].to_numpy(float)
    micro = features["micro_bps"].to_numpy(float)
    bid20 = features["bid_qty_20"].to_numpy(float)
    ask20 = features["ask_qty_20"].to_numpy(float)
    bid_chg = features["bid20_chg_30"].to_numpy(float)
    ask_chg = features["ask20_chg_30"].to_numpy(float)
    zmax = features["z_max_retest"].to_numpy(float)
    zmin = features["z_min_retest"].to_numpy(float)

    normal_mask = (
        np.isfinite(z)
        & np.isfinite(inside1)
        & np.isfinite(observed)
        & np.isfinite(center_slope)
        & np.isfinite(sigma_bps)
        & np.isfinite(sigma_expand)
        & (inside1 >= cfg.inside_min)
        & (observed >= cfg.observed_min_pct)
        & (np.abs(center_slope) <= cfg.center_slope_max_bps)
        & (sigma_bps >= cfg.sigma_min_bps)
        & (sigma_bps <= cfg.sigma_max_bps)
        & (sigma_expand <= cfg.sigma_expand_max)
    )
    resistance = (
        np.isfinite(ask20)
        & np.isfinite(bid20)
        & (imb20 <= -cfg.ob_imbalance_min)
        & (micro <= -cfg.micro_min_bps)
        & (ask20 >= np.maximum(1e-9, bid20 * cfg.wall_ratio_min))
        & (np.nan_to_num(ask_chg, nan=0.0) > -0.55)
    )
    support = (
        np.isfinite(ask20)
        & np.isfinite(bid20)
        & (imb20 >= cfg.ob_imbalance_min)
        & (micro >= cfg.micro_min_bps)
        & (bid20 >= np.maximum(1e-9, ask20 * cfg.wall_ratio_min))
        & (np.nan_to_num(bid_chg, nan=0.0) > -0.55)
    )
    true_up = (flow >= cfg.true_break_flow) | (imb20 >= cfg.true_break_imbalance) | (micro >= cfg.micro_min_bps * 4.0)
    true_down = (flow <= -cfg.true_break_flow) | (imb20 <= -cfg.true_break_imbalance) | (micro <= -cfg.micro_min_bps * 4.0)

    edge_down = (z >= cfg.z_entry) & resistance & (flow <= cfg.flow_guard) & (~true_up)
    edge_up = (z <= -cfg.z_entry) & support & (flow >= -cfg.flow_guard) & (~true_down)
    reclaim_down = (zmax >= cfg.z_entry) & (z >= 0.0) & (z <= cfg.z_reclaim) & resistance & (flow <= cfg.flow_guard)
    reclaim_up = (zmin <= -cfg.z_entry) & (z <= 0.0) & (z >= -cfg.z_reclaim) & support & (flow >= -cfg.flow_guard)

    if cfg.mode == "edge":
        down_mask = edge_down
        up_mask = edge_up
    elif cfg.mode == "reclaim":
        down_mask = reclaim_down
        up_mask = reclaim_up
    else:
        down_mask = edge_down | reclaim_down
        up_mask = edge_up | reclaim_up

    warmup = max(cfg.normal_window_sec, cfg.center_slope_sec, cfg.retest_sec, 600) + 5
    limit = len(data) - cfg.horizon_sec
    valid_idx = np.zeros(len(data), dtype=bool)
    valid_idx[warmup:limit] = True
    candidate_idx = np.flatnonzero(valid_idx & normal_mask & (down_mask | up_mask))

    rows: list[dict[str, Any]] = []
    last_idx = -10**12
    for i in candidate_idx:
        if i - last_idx < cfg.signal_gap_sec:
            continue
        if down_mask[i]:
            signal = "DOWN"
            reason = "upper_passive_resistance_fade" if edge_down[i] else "upper_fake_break_reclaim"
        else:
            signal = "UP"
            reason = "lower_passive_support_fade" if edge_up[i] else "lower_fake_break_reclaim"
        entry = float(close[i])
        settle = float(close[i + cfg.horizon_sec])
        won = bool(settle > entry if signal == "UP" else settle < entry)
        last_idx = i
        row = features.iloc[i]
        rows.append(
            {
                "strategy_id": cfg.strategy_id,
                "model_type": "normal_liquidity_orderbook",
                "idx": int(i),
                "time": data.index[i],
                "signal": signal,
                "reason": reason,
                "entry": entry,
                "settle_time": data.index[i + cfg.horizon_sec],
                "settle": settle,
                "won": won,
                "horizon_sec": cfg.horizon_sec,
                "amount": cfg.amount,
                "normal_window_sec": cfg.normal_window_sec,
                "z": round(float(row["z"]), 5),
                "inside1_ratio": round(float(row["inside1_ratio"]), 5),
                "observed_pct": round(float(row["observed_pct"]), 4),
                "center_slope_bps": round(float(row["center_slope_bps"]), 4),
                "sigma_bps": round(float(row["sigma_bps"]), 4),
                "flow_60": round(float(row["flow_60"]), 6),
                "imbalance_20": round(float(row["imbalance_20"]), 6),
                "micro_bps": round(float(row["micro_bps"]), 6),
                "bid_qty_20": round(float(row["bid_qty_20"]), 6),
                "ask_qty_20": round(float(row["ask_qty_20"]), 6),
                "bid20_chg_30": round(float(row["bid20_chg_30"]), 6),
                "ask20_chg_30": round(float(row["ask20_chg_30"]), 6),
            }
        )
    return rows


def max_drawdown_u(trades: list[dict[str, Any]], amount: float, payout_rate: float) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for row in sorted(trades, key=lambda x: x["time"]):
        equity += amount * payout_rate if row["won"] else -amount
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return round(float(max_dd), 2)


def metrics_for(trades: list[dict[str, Any]], start: pd.Timestamp, end: pd.Timestamp, cfg: LiquidityNormalConfig) -> dict:
    payout = payout_for_horizon(cfg.horizon_sec)
    m = summarize_trades(trades, start, end, amount=cfg.amount, payout_rate=payout)
    m["maxDrawdownU"] = max_drawdown_u(trades, cfg.amount, payout)
    return m


def split_by_day(trades: list[dict[str, Any]], cfg: LiquidityNormalConfig) -> list[dict[str, Any]]:
    out = []
    for day, group in pd.DataFrame(trades).groupby(pd.DataFrame(trades)["time"].dt.strftime("%Y-%m-%d")) if trades else []:
        start = pd.Timestamp(day, tz="UTC")
        end = start + pd.Timedelta(days=1)
        out.append({"day": day, **metrics_for(group.to_dict("records"), start, end, cfg)})
    return out


def score_candidate(train: dict, test: dict) -> float:
    if train["trades"] < 4 or test["trades"] < 3:
        return -9999.0
    train_wr = train["winRate"] or 0.0
    test_wr = test["winRate"] or 0.0
    count_bonus = min(test["tradesPerDay"], 20.0) * 0.3
    dd_penalty = max(train["maxDrawdownU"], test["maxDrawdownU"]) * 0.35
    streak_penalty = max(train["maxLoss"], test["maxLoss"]) * 2.0
    return round(train_wr * 0.25 + test_wr * 0.45 + min(train_wr, test_wr) * 0.25 + count_bonus - dd_penalty - streak_penalty, 4)


def config_grid() -> list[LiquidityNormalConfig]:
    configs = []
    for window in (600, 900, 1200):
        for z_entry in (1.0, 1.2):
            for mode in ("edge", "reclaim", "hybrid"):
                for inside_min in (0.55,):
                    for ob_min in (0.08, 0.12):
                        configs.append(
                            LiquidityNormalConfig(
                                normal_window_sec=window,
                                z_entry=z_entry,
                                mode=mode,
                                inside_min=inside_min,
                                ob_imbalance_min=ob_min,
                            )
                        )
    return configs


def run(seconds_path: Path, orderbook_path: Path) -> dict[str, Any]:
    data = load_local_data(seconds_path, orderbook_path)
    start = data.index.min()
    end = data.index.max()
    split_time = pd.Timestamp("2026-07-06T00:00:00Z")

    feature_cache: dict[int, pd.DataFrame] = {}
    results = []
    best_rows: list[dict[str, Any]] = []
    best_cfg: LiquidityNormalConfig | None = None
    best_score = -9999.0

    for cfg in config_grid():
        features = feature_cache.get(cfg.normal_window_sec)
        if features is None:
            features = build_features(data, cfg.normal_window_sec, cfg)
            feature_cache[cfg.normal_window_sec] = features
        rows = generate_signals(data, features, cfg)
        train_rows = [row for row in rows if row["time"] < split_time]
        test_rows = [row for row in rows if row["time"] >= split_time]
        train = metrics_for(train_rows, start, min(split_time, end), cfg)
        test = metrics_for(test_rows, max(split_time, start), end, cfg)
        all_m = metrics_for(rows, start, end, cfg)
        score = score_candidate(train, test)
        item = {
            **asdict(cfg),
            "strategy_id": cfg.strategy_id,
            "score": score,
            "all_trades": all_m["trades"],
            "all_winRate": all_m["winRate"],
            "all_tradesPerDay": all_m["tradesPerDay"],
            "all_pnl": all_m["pnl"],
            "all_maxLoss": all_m["maxLoss"],
            "all_maxDrawdownU": all_m["maxDrawdownU"],
            "train_trades": train["trades"],
            "train_winRate": train["winRate"],
            "train_pnl": train["pnl"],
            "train_maxLoss": train["maxLoss"],
            "train_maxDrawdownU": train["maxDrawdownU"],
            "test_trades": test["trades"],
            "test_winRate": test["winRate"],
            "test_pnl": test["pnl"],
            "test_maxLoss": test["maxLoss"],
            "test_maxDrawdownU": test["maxDrawdownU"],
        }
        results.append(item)
        if score > best_score:
            best_score = score
            best_cfg = cfg
            best_rows = rows

    assert best_cfg is not None
    payout = payout_for_horizon(best_cfg.horizon_sec)
    train_rows = [row for row in best_rows if row["time"] < split_time]
    test_rows = [row for row in best_rows if row["time"] >= split_time]
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "method": {
            "idea": "Rolling VWAP/mean normal band + passive order-book support/resistance. Fade only when price is at/reclaims a normal edge and book pressure is opposite; skip likely true breaks.",
            "causal": "Every feature uses data at or before the signal second. Outcome only uses signal + 600s settlement.",
            "dataLimit": "Order-book data exists only for the recent local pull, so this is a short order-book test, not a deployment-grade validation.",
            "split": "Train/check before 2026-07-06 UTC; forward test from 2026-07-06 UTC.",
            "payout": f"{best_cfg.amount}U stake, {payout:.2f} payout rate for 10m binary option.",
        },
        "data": {
            "seconds": str(seconds_path),
            "orderbook": str(orderbook_path),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "hours": round((end - start).total_seconds() / 3600.0, 2),
            "rows": int(len(data)),
            "observedPct": round(float(data["observed"].mean() * 100.0), 4),
            "orderbookRows": int(data["ob_available"].sum()),
        },
        "best": {
            "config": asdict(best_cfg) | {"strategy_id": best_cfg.strategy_id},
            "score": best_score,
            "all": metrics_for(best_rows, start, end, best_cfg),
            "train": metrics_for(train_rows, start, min(split_time, end), best_cfg),
            "forwardTest": metrics_for(test_rows, max(split_time, start), end, best_cfg),
            "byUtcDay": split_by_day(best_rows, best_cfg),
            "byReason": summarize_group(best_rows, "reason", best_cfg),
            "bySide": summarize_group(best_rows, "signal", best_cfg),
        },
        "top": sorted(results, key=lambda row: row["score"], reverse=True)[:20],
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    pd.DataFrame(results).sort_values("score", ascending=False).to_csv(OUT_GRID, index=False, encoding="utf-8")
    if best_rows:
        pd.DataFrame(best_rows).to_csv(OUT_TRADES, index=False, encoding="utf-8")
    return report


def summarize_group(trades: list[dict[str, Any]], key: str, cfg: LiquidityNormalConfig) -> list[dict[str, Any]]:
    if not trades:
        return []
    frame = pd.DataFrame(trades)
    out = []
    for value, group in frame.groupby(key):
        out.append({"key": value, **metrics_for(group.to_dict("records"), frame["time"].min(), frame["time"].max(), cfg)})
    return sorted(out, key=lambda row: row["trades"], reverse=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", default=str(DEFAULT_SECONDS))
    parser.add_argument("--orderbook", default=str(DEFAULT_ORDERBOOK))
    args = parser.parse_args()
    report = run(Path(args.seconds), Path(args.orderbook))
    best = report["best"]
    print(json.dumps({
        "data": report["data"],
        "bestConfig": best["config"],
        "all": best["all"],
        "forwardTest": best["forwardTest"],
        "byReason": best["byReason"],
        "outputs": {
            "json": str(OUT_JSON),
            "trades": str(OUT_TRADES),
            "grid": str(OUT_GRID),
        },
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
