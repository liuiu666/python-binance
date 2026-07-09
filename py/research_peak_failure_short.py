from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_normal_liquidity_orderbook import LiquidityNormalConfig, build_features, read_orderbook  # noqa: E402
from second_backtest.data import load_second_bars  # noqa: E402


OUT_JSON = ROOT / "tmp" / "peak_failure_short_research.json"
OUT_TRADES = ROOT / "tmp" / "peak_failure_short_trades.csv"

DATASETS = {
    "2026-07-05_2026-07-06": {
        "dir": ROOT / "tmp" / "latest_pull_20260706_2130" / "data",
        "start": "2026-07-05T00:00:00Z",
        "end": "2026-07-07T00:00:00Z",
    },
    "2026-07-07_2026-07-08": {
        "dir": ROOT / "tmp" / "latest_pull_20260708_204204" / "data",
        "start": "2026-07-07T00:00:00Z",
        "end": "2026-07-09T00:00:00Z",
    },
    "2026-07-08_2026-07-09_clean": {
        "dir": ROOT / "tmp" / "latest_live_pull_20260709_220453" / "data_clean",
        "start": "2026-07-08T12:48:00Z",
        "end": "2026-07-09T14:04:00Z",
    },
}


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        value = float(value)
        return value if np.isfinite(value) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def cfg() -> LiquidityNormalConfig:
    return LiquidityNormalConfig(
        normal_window_sec=600,
        z_entry=1.2,
        z_reclaim=0.85,
        mode="reclaim",
        retest_sec=120,
        inside_min=0.55,
        observed_min_pct=88.0,
        center_slope_sec=300,
        center_slope_max_bps=8.0,
        sigma_min_bps=5.8,
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


def load_data(data_dir: Path) -> pd.DataFrame:
    bars = load_second_bars(data_dir / "btcusdt_1s_trades.csv", include_shards=True)
    ob = read_orderbook(data_dir / "btcusdt_orderbook_1s.csv", bars.index)
    return bars.join(ob, how="left").sort_index()


def add_context(data: pd.DataFrame, c: LiquidityNormalConfig) -> pd.DataFrame:
    f = build_features(data, c.normal_window_sec, c)
    close = data["close"].astype(float)
    volume = data["volume"].astype(float)
    for sec in (30, 60, 120, 180, 300, 600, 900, 1800):
        f[f"ret_{sec}s_bps"] = np.log(close / close.shift(sec)) * 10000.0
    for sec in (300, 600, 900, 1800):
        high = close.rolling(sec, min_periods=max(60, sec // 3)).max()
        low = close.rolling(sec, min_periods=max(60, sec // 3)).min()
        f[f"pos_{sec}s"] = (close - low) / (high - low).replace(0, np.nan)
        f[f"range_{sec}s_bps"] = (high / low - 1.0) * 10000.0
    f["bid20_chg_60"] = f["bid_qty_20"] / f["bid_qty_20"].shift(60).replace(0, np.nan) - 1.0
    f["ask20_chg_60"] = f["ask_qty_20"] / f["ask_qty_20"].shift(60).replace(0, np.nan) - 1.0
    f["flow60_delta"] = f["flow_60"] - f["flow_60"].shift(60)
    vol60 = volume.rolling(60, min_periods=10).sum()
    vol300 = volume.rolling(300, min_periods=60).sum()
    f["vol_ratio_60"] = vol60 / (vol300 / 5.0).replace(0, np.nan)
    return f


def payout(won: bool) -> int:
    return 4 if bool(won) else -5


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    wins = sum(1 for row in rows if row["won"])
    equity = peak = max_dd = loss_streak = max_loss = 0
    fav = []
    for row in sorted(rows, key=lambda r: (str(r["dataset"]), int(r["idx"]))):
        pnl = payout(bool(row["won"]))
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        if row["won"]:
            loss_streak = 0
        else:
            loss_streak += 1
            max_loss = max(max_loss, loss_streak)
        fav.append(float(row["fav_bps"]))
    return {
        "trades": n,
        "wins": wins,
        "winRate": round(wins / n * 100.0, 2) if n else 0.0,
        "pnl": sum(payout(bool(row["won"])) for row in rows),
        "maxDrawdownU": int(max_dd),
        "maxLoss": int(max_loss),
        "avgFavBps": round(float(np.mean(fav)), 2) if fav else None,
        "medianFavBps": round(float(np.median(fav)), 2) if fav else None,
    }


def split_metrics(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    if not rows:
        return {}
    frame = pd.DataFrame(rows)
    return {str(name): metrics(group.to_dict("records")) for name, group in frame.groupby(key, sort=True)}


def normal_ready(row: pd.Series, c: LiquidityNormalConfig, *, allow_slope_bps: float, max_inside: float | None) -> bool:
    checks = [
        row.get("z"),
        row.get("inside1_ratio"),
        row.get("observed_pct"),
        row.get("center_slope_bps"),
        row.get("sigma_bps"),
        row.get("sigma_expand"),
    ]
    if any(not np.isfinite(float(x)) for x in checks):
        return False
    if float(row["observed_pct"]) < c.observed_min_pct:
        return False
    if float(row["sigma_bps"]) < c.sigma_min_bps or float(row["sigma_bps"]) > c.sigma_max_bps:
        return False
    if float(row["sigma_expand"]) > c.sigma_expand_max:
        return False
    if abs(float(row["center_slope_bps"])) > allow_slope_bps:
        return False
    if max_inside is not None and float(row["inside1_ratio"]) > max_inside:
        return False
    return True


def run_variant(data: pd.DataFrame, features: pd.DataFrame, dataset: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    c = cfg()
    close = data["close"].to_numpy(float)
    mask = (
        data["ob_available"].astype(bool)
        & features["z"].ge(float(params["zMin"]))
        & features["pos_600s"].ge(float(params["pos600Min"]))
        & features["ret_180s_bps"].ge(float(params["preRet180MinBps"]))
        & features["ret_30s_bps"].le(float(params["turnRet30MaxBps"]))
        & features["imbalance_20"].le(-float(params["obImbMin"]))
        & features["micro_bps"].le(-float(params["microMinBps"]))
        & features["ask_qty_20"].ge(features["bid_qty_20"] * float(params["askBidRatioMin"]))
        & features["flow_60"].le(float(params["flow60Max"]))
        & features["flow60_delta"].le(float(params["flowDeltaMax"]))
        & features["bid20_chg_60"].le(float(params["bid20Chg60Max"]))
        & features["vol_ratio_60"].ge(float(params["volRatioMin"]))
        & features["observed_pct"].ge(c.observed_min_pct)
        & features["sigma_bps"].ge(c.sigma_min_bps)
        & features["sigma_bps"].le(c.sigma_max_bps)
        & features["sigma_expand"].le(c.sigma_expand_max)
        & features["center_slope_bps"].abs().le(float(params["centerSlopeAbsMaxBps"]))
    )
    if params.get("insideMax") is not None:
        mask &= features["inside1_ratio"].le(float(params["insideMax"]))
    candidate_idxs = np.flatnonzero(mask.fillna(False).to_numpy())
    rows: list[dict[str, Any]] = []
    last_idx = -10**12
    warmup = max(c.normal_window_sec, c.center_slope_sec, c.retest_sec, 1800) + 10
    limit = len(data) - c.horizon_sec
    for idx in candidate_idxs:
        if idx < warmup or idx >= limit:
            continue
        if idx - last_idx < c.signal_gap_sec:
            continue
        row = features.iloc[idx]
        entry = float(close[idx])
        settle = float(close[idx + c.horizon_sec])
        won = settle < entry
        fav = (entry - settle) / entry * 10000.0
        rows.append(
            {
                "dataset": dataset,
                "idx": idx,
                "time": data.index[idx],
                "signal": "DOWN",
                "reason": "peak_failure_short",
                "entry": entry,
                "settle": settle,
                "settle_time": data.index[idx + c.horizon_sec],
                "won": bool(won),
                "fav_bps": round(float(fav), 6),
                "z": round(float(row["z"]), 4),
                "pos600": round(float(row["pos_600s"]), 4),
                "ret180": round(float(row["ret_180s_bps"]), 4),
                "ret30": round(float(row["ret_30s_bps"]), 4),
                "flow60": round(float(row["flow_60"]), 6),
                "flowDelta": round(float(row["flow60_delta"]), 6),
                "imb20": round(float(row["imbalance_20"]), 6),
                "micro": round(float(row["micro_bps"]), 6),
                "bid20Chg60": round(float(row["bid20_chg_60"]), 6),
                "volRatio60": round(float(row["vol_ratio_60"]), 4),
            }
        )
        last_idx = idx
    return rows


def run() -> dict[str, Any]:
    variants = []
    for z_min in (1.8, 2.2, 2.6):
        for pos_min in (0.85, 0.93):
            for pre_ret in (5.0, 8.0):
                for turn_ret in (-0.5, -1.0):
                    for flow_max in (0.15, 0.0):
                        variants.append(
                            {
                                "name": f"PF_Z{z_min:g}_P{pos_min:g}_R{pre_ret:g}_T{turn_ret:g}_F{flow_max:g}",
                                "zMin": z_min,
                                "pos600Min": pos_min,
                                "preRet180MinBps": pre_ret,
                                "turnRet30MaxBps": turn_ret,
                                "flow60Max": flow_max,
                                "flowDeltaMax": 0.25,
                                "obImbMin": 0.08,
                                "microMinBps": 0.001,
                                "askBidRatioMin": 1.0,
                                "bid20Chg60Max": 0.5,
                                "volRatioMin": 0.55,
                                "centerSlopeAbsMaxBps": 18.0,
                                "insideMax": 0.78,
                            }
                        )

    loaded: dict[str, tuple[pd.DataFrame, pd.DataFrame, float]] = {}
    for name, spec in DATASETS.items():
        if not Path(spec["dir"]).exists():
            continue
        data = load_data(Path(spec["dir"]))
        data = data[(data.index >= pd.Timestamp(spec["start"])) & (data.index < pd.Timestamp(spec["end"]))].copy()
        if data.empty:
            continue
        features = add_context(data, cfg())
        hours = (data.index.max() - data.index.min()).total_seconds() / 3600.0
        loaded[name] = (data, features, hours)

    results = []
    all_trade_rows = []
    for variant in variants:
        rows = []
        for dataset, (data, features, _hours) in loaded.items():
            rows.extend(run_variant(data, features, dataset, variant))
        m = metrics(rows)
        total_hours = sum(hours for _data, _features, hours in loaded.values())
        trades_per_day = len(rows) / max(total_hours, 1e-9) * 24.0
        if m["trades"] >= 6:
            score = m["pnl"] + m["winRate"] * 0.35 + min(trades_per_day, 20) * 0.4 - m["maxDrawdownU"] * 0.8 - m["maxLoss"] * 3
            results.append(
                {
                    "name": variant["name"],
                    "params": variant,
                    "overall": m,
                    "tradesPerDay": round(trades_per_day, 2),
                    "byDataset": split_metrics(rows, "dataset"),
                    "score": round(float(score), 4),
                }
            )
            for row in rows:
                all_trade_rows.append({**row, "variant": variant["name"]})

    results = sorted(results, key=lambda item: item["score"], reverse=True)
    best_name = results[0]["name"] if results else None
    best_rows = [row for row in all_trade_rows if row["variant"] == best_name]
    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "method": {
            "rule": "DOWN only: normal high position + prior 180s rise + 30s turn down + orderbook sell pressure + weakening buy side.",
            "causal": "All features are known at signal second; settlement is +600s.",
            "note": "This is a separate peak-failure short module, not replacing V2 normal reclaim.",
        },
        "dataHours": {name: round(hours, 2) for name, (_data, _features, hours) in loaded.items()},
        "top": results[:20],
        "bestByDataset": split_metrics(best_rows, "dataset"),
        "bestTrades": best_rows,
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(clean(all_trade_rows)).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    return output


if __name__ == "__main__":
    print(json.dumps(clean(run()), ensure_ascii=False, indent=2))
