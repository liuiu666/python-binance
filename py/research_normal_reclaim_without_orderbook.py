from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR / "py"))

from second_backtest.data import audit_second_sources, load_second_bars  # noqa: E402
from second_backtest.metrics import payout_for_horizon, summarize_trades  # noqa: E402

OUT_JSON = APP_DIR / "tmp" / "normal_reclaim_without_orderbook_research.json"
OUT_CSV = APP_DIR / "tmp" / "normal_reclaim_without_orderbook_grid.csv"


DATASETS = [
    {
        "name": "old_local_20260613_0617",
        "path": APP_DIR / "data" / "btcusdt_1s_trades.csv",
        "include_shards": False,
    },
    {
        "name": "latest_pull_full_20260613_0706",
        "path": APP_DIR / "tmp" / "latest_pull_20260706_2130" / "data" / "btcusdt_1s_trades.csv",
        "include_shards": True,
    },
    {
        "name": "live_parity_20260705_0706",
        "path": APP_DIR / "tmp" / "live_parity_current" / "data" / "btcusdt_1s_trades.csv",
        "include_shards": False,
    },
]


@dataclass(frozen=True)
class NormalReclaimNoObConfig:
    normal_window_sec: int = 600
    z_entry: float = 1.2
    z_reclaim: float = 0.85
    retest_sec: int = 120
    inside_min: float = 0.55
    observed_min_pct: float = 88.0
    center_slope_sec: int = 300
    center_slope_max_bps: float = 8.0
    sigma_min_bps: float = 5.0
    sigma_max_bps: float = 55.0
    sigma_expand_max: float = 1.9
    flow_guard: float = 0.12
    true_break_flow: float = 0.28
    signal_gap_sec: int = 600
    horizon_sec: int = 600
    amount: float = 5.0

    @property
    def strategy_id(self) -> str:
        return (
            f"NOOB_RECLAIM_W{self.normal_window_sec}"
            f"_Z{int(self.z_entry * 100)}_FG{int(self.flow_guard * 100)}"
        )


def build_features(bars: pd.DataFrame, cfg: NormalReclaimNoObConfig) -> pd.DataFrame:
    close = bars["close"].astype(float)
    volume = bars["volume"].astype(float).clip(lower=0.0)
    observed = bars["observed"].astype(bool).astype(float)
    window = cfg.normal_window_sec
    min_periods = max(120, window // 3)

    sw = volume.rolling(window, min_periods=min_periods).sum()
    sx = (close * volume).rolling(window, min_periods=min_periods).sum()
    sx2 = (close * close * volume).rolling(window, min_periods=min_periods).sum()
    vwap = sx / sw.replace(0, np.nan)
    var = sx2 / sw.replace(0, np.nan) - vwap * vwap
    mean = close.rolling(window, min_periods=min_periods).mean()
    std = close.rolling(window, min_periods=min_periods).std(ddof=1)
    center = vwap.fillna(mean)
    sigma = np.sqrt(var.clip(lower=0.0)).where(lambda s: s > 1e-9, std)
    z = (close - center) / sigma.replace(0, np.nan)

    buy_60 = bars["buy_qty"].rolling(60, min_periods=10).sum()
    sell_60 = bars["sell_qty"].rolling(60, min_periods=10).sum()
    flow_60 = (buy_60 - sell_60) / (buy_60 + sell_60).replace(0, np.nan)

    sigma_bps = sigma / close * 10000.0
    sigma_median = sigma_bps.rolling(max(window, 900), min_periods=min_periods).median()
    out = pd.DataFrame(index=bars.index)
    out["close"] = close
    out["z"] = z
    out["inside1_ratio"] = z.abs().le(1.0).astype(float).rolling(window, min_periods=min_periods).mean()
    out["observed_pct"] = observed.rolling(min(600, window), min_periods=120).mean() * 100.0
    out["center_slope_bps"] = (center / center.shift(cfg.center_slope_sec) - 1.0) * 10000.0
    out["sigma_bps"] = sigma_bps
    out["sigma_expand"] = sigma_bps / sigma_median.replace(0, np.nan)
    out["flow_60"] = flow_60
    out["z_max_retest"] = z.rolling(cfg.retest_sec, min_periods=10).max()
    out["z_min_retest"] = z.rolling(cfg.retest_sec, min_periods=10).min()
    return out


def generate_signals(bars: pd.DataFrame, cfg: NormalReclaimNoObConfig) -> list[dict[str, Any]]:
    f = build_features(bars, cfg)
    close = bars["close"].to_numpy(float)
    z = f["z"].to_numpy(float)
    normal = (
        np.isfinite(z)
        & (f["inside1_ratio"].to_numpy(float) >= cfg.inside_min)
        & (f["observed_pct"].to_numpy(float) >= cfg.observed_min_pct)
        & (np.abs(f["center_slope_bps"].to_numpy(float)) <= cfg.center_slope_max_bps)
        & (f["sigma_bps"].to_numpy(float) >= cfg.sigma_min_bps)
        & (f["sigma_bps"].to_numpy(float) <= cfg.sigma_max_bps)
        & (f["sigma_expand"].to_numpy(float) <= cfg.sigma_expand_max)
    )
    flow = f["flow_60"].to_numpy(float)
    zmax = f["z_max_retest"].to_numpy(float)
    zmin = f["z_min_retest"].to_numpy(float)
    down = (zmax >= cfg.z_entry) & (z >= 0.0) & (z <= cfg.z_reclaim) & (flow <= cfg.flow_guard) & (flow < cfg.true_break_flow)
    up = (zmin <= -cfg.z_entry) & (z <= 0.0) & (z >= -cfg.z_reclaim) & (flow >= -cfg.flow_guard) & (flow > -cfg.true_break_flow)
    warmup = max(cfg.normal_window_sec, cfg.center_slope_sec, cfg.retest_sec, 600) + 5
    limit = len(bars) - cfg.horizon_sec
    valid = np.zeros(len(bars), dtype=bool)
    valid[warmup:limit] = True
    idxs = np.flatnonzero(valid & normal & (up | down))

    rows = []
    last_idx = -10**12
    for i in idxs:
        if i - last_idx < cfg.signal_gap_sec:
            continue
        signal = "DOWN" if down[i] else "UP"
        entry = float(close[i])
        settle = float(close[i + cfg.horizon_sec])
        rows.append(
            {
                "strategy_id": cfg.strategy_id,
                "idx": int(i),
                "time": bars.index[i],
                "signal": signal,
                "entry": entry,
                "settle_time": bars.index[i + cfg.horizon_sec],
                "settle": settle,
                "won": bool(settle > entry if signal == "UP" else settle < entry),
                "z": round(float(z[i]), 5),
                "flow_60": round(float(flow[i]), 6),
            }
        )
        last_idx = i
    return rows


def max_drawdown_u(rows: list[dict[str, Any]], amount: float, payout: float) -> float:
    eq = 0.0
    peak = 0.0
    dd = 0.0
    for row in sorted(rows, key=lambda x: x["time"]):
        eq += amount * payout if row["won"] else -amount
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return round(dd, 2)


def metrics(rows: list[dict[str, Any]], start: pd.Timestamp, end: pd.Timestamp, cfg: NormalReclaimNoObConfig) -> dict:
    payout = payout_for_horizon(cfg.horizon_sec)
    out = summarize_trades(rows, start, end, cfg.amount, payout)
    out["maxDrawdownU"] = max_drawdown_u(rows, cfg.amount, payout)
    return out


def cfg_grid() -> list[NormalReclaimNoObConfig]:
    out = []
    for window in (600, 900, 1200, 1800):
        for z_entry in (1.0, 1.2, 1.4):
            for inside in (0.55, 0.62):
                for flow_guard in (0.0, 0.08, 0.12):
                    out.append(
                        NormalReclaimNoObConfig(
                            normal_window_sec=window,
                            z_entry=z_entry,
                            inside_min=inside,
                            flow_guard=flow_guard,
                        )
                    )
    return out


def run() -> dict[str, Any]:
    all_rows = []
    dataset_reports = []
    for dataset in DATASETS:
        path = dataset["path"]
        if not path.exists():
            continue
        audit = audit_second_sources(path, include_shards=dataset["include_shards"])
        bars = load_second_bars(path, include_shards=dataset["include_shards"])
        start, end = bars.index.min(), bars.index.max()
        cfg_results = []
        for cfg in cfg_grid():
            rows = generate_signals(bars, cfg)
            m = metrics(rows, start, end, cfg)
            score = -9999.0
            if m["trades"] >= 8:
                score = (m["winRate"] or 0.0) + min(m["tradesPerDay"], 20) * 0.4 - m["maxDrawdownU"] * 0.25 - m["maxLoss"] * 2
            cfg_results.append({**asdict(cfg), "strategy_id": cfg.strategy_id, "score": round(score, 4), **m})
        best = sorted(cfg_results, key=lambda x: x["score"], reverse=True)[0]
        fixed = NormalReclaimNoObConfig(normal_window_sec=600, z_entry=1.2, inside_min=0.55, flow_guard=0.12)
        fixed_rows = generate_signals(bars, fixed)
        fixed_m = metrics(fixed_rows, start, end, fixed)
        dataset_reports.append(
            {
                "name": dataset["name"],
                "path": str(path),
                "audit": audit,
                "fixedSameAsObNoBook": {**asdict(fixed), "strategy_id": fixed.strategy_id, **fixed_m},
                "bestInSample": best,
            }
        )
        for item in cfg_results:
            all_rows.append({"dataset": dataset["name"], **item})

    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "method": {
            "purpose": "Test whether the same normal fake-break reclaim idea works without order-book support/resistance.",
            "conclusionUse": "If no-book results are unstable, the executable stable strategy should require live order-book for the new module.",
            "causal": "Every feature uses data at or before signal second; settlement uses +600s only for evaluation.",
        },
        "datasets": dataset_reports,
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    pd.DataFrame(all_rows).to_csv(OUT_CSV, index=False, encoding="utf-8")
    return report


if __name__ == "__main__":
    r = run()
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str)[:8000])
