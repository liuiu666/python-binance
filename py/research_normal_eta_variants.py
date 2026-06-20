from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from research_arrival_forecast import DEFAULT_OLD_CSV, DEFAULT_PROD_CONFIG, DEFAULT_SHARD_DIR, load_bars
from research_normal_eta import day_metrics, direct_rows, eta_rows, metrics, side_metrics
from second_backtest.execution import execute_signals
from second_backtest.strategies import SecondNormalConfig, generate_normal_signals, normal_cdf, settle_signal


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "tmp" / "normal_eta_variants_combined.json"


def _apply_gap(rows: list[dict], gap_sec: int) -> list[dict]:
    if gap_sec <= 0:
        return rows
    out: list[dict] = []
    last_idx = -10**12
    for row in rows:
        idx = int(row["idx"])
        if idx - last_idx < gap_sec:
            continue
        out.append(row)
        last_idx = idx
    return out


def generate_robust_return_signals(bars: pd.DataFrame, cfg: SecondNormalConfig) -> list[dict]:
    close = bars["close"].to_numpy(float)
    if len(close) <= cfg.lookback_sec + cfg.horizon_sec:
        return []
    logp = np.log(close)
    lr = pd.Series(np.diff(logp, prepend=np.nan), index=bars.index)
    min_periods = max(120, min(cfg.lookback_sec, cfg.lookback_sec // 3))
    med = lr.rolling(cfg.lookback_sec, min_periods=min_periods).median()
    abs_dev = (lr - med).abs()
    mad = abs_dev.rolling(cfg.lookback_sec, min_periods=min_periods).median().to_numpy(float)
    med_np = med.to_numpy(float)
    sigma = 1.4826 * mad
    threshold_hi = 1.0 - cfg.tail_pct
    rows: list[dict] = []
    for i in range(cfg.lookback_sec, len(close) - cfg.horizon_sec):
        if not np.isfinite(med_np[i]) or not np.isfinite(sigma[i]) or sigma[i] < 1e-12:
            continue
        z = float(cfg.horizon_sec * med_np[i] / (math.sqrt(cfg.horizon_sec) * sigma[i]))
        p_up = normal_cdf(z)
        signal = "DOWN" if p_up >= threshold_hi else "UP" if p_up <= cfg.tail_pct else None
        if not signal:
            continue
        rows.append(
            settle_signal(
                bars=bars,
                idx=i,
                strategy_id=cfg.strategy_id,
                model_type="second_normal_robust_return",
                signal=signal,
                horizon_sec=cfg.horizon_sec,
                amount=cfg.amount,
                extra={
                    "p_up": round(float(p_up), 6),
                    "z_score": round(float(z), 6),
                    "tail_pct": float(cfg.tail_pct),
                    "lookback_sec": int(cfg.lookback_sec),
                    "normal_variant": "return_median_mad",
                },
            )
        )
    return _apply_gap(rows, cfg.signal_gap_sec)


def generate_price_level_signals(bars: pd.DataFrame, cfg: SecondNormalConfig) -> list[dict]:
    close = bars["close"].to_numpy(float)
    if len(close) <= cfg.lookback_sec + cfg.horizon_sec:
        return []
    logp = pd.Series(np.log(close), index=bars.index)
    min_periods = max(120, min(cfg.lookback_sec, cfg.lookback_sec // 3))
    mu = logp.rolling(cfg.lookback_sec, min_periods=min_periods).mean().to_numpy(float)
    sigma = logp.rolling(cfg.lookback_sec, min_periods=min_periods).std(ddof=1).to_numpy(float)
    threshold_hi = 1.0 - cfg.tail_pct
    rows: list[dict] = []
    for i in range(cfg.lookback_sec, len(close) - cfg.horizon_sec):
        if not np.isfinite(mu[i]) or not np.isfinite(sigma[i]) or sigma[i] < 1e-12:
            continue
        z = float((math.log(close[i]) - mu[i]) / sigma[i])
        p_low_to_high = normal_cdf(z)
        signal = "DOWN" if p_low_to_high >= threshold_hi else "UP" if p_low_to_high <= cfg.tail_pct else None
        if not signal:
            continue
        rows.append(
            settle_signal(
                bars=bars,
                idx=i,
                strategy_id=cfg.strategy_id,
                model_type="second_normal_price_level",
                signal=signal,
                horizon_sec=cfg.horizon_sec,
                amount=cfg.amount,
                extra={
                    "p_level": round(float(p_low_to_high), 6),
                    "z_score": round(float(z), 6),
                    "tail_pct": float(cfg.tail_pct),
                    "lookback_sec": int(cfg.lookback_sec),
                    "normal_variant": "price_level_z",
                },
            )
        )
    return _apply_gap(rows, cfg.signal_gap_sec)


def generate_variant_signals(variant: str, bars: pd.DataFrame, cfg: SecondNormalConfig) -> list[dict]:
    if variant == "return_mean_std":
        return generate_normal_signals(bars, cfg, apply_config_gap=True)
    if variant == "return_median_mad":
        return generate_robust_return_signals(bars, cfg)
    if variant == "price_level_z":
        return generate_price_level_signals(bars, cfg)
    raise ValueError(f"unknown variant: {variant}")


FAST_CANDIDATES = (
    {"lookback": 2700, "tail": 0.27, "target": 2.0, "wait": 45, "downOnly": False},
    {"lookback": 2700, "tail": 0.27, "target": 3.0, "wait": 45, "downOnly": False},
    {"lookback": 4200, "tail": 0.20, "target": 2.0, "wait": 45, "downOnly": False},
    {"lookback": 1800, "tail": 0.20, "target": 1.0, "wait": 20, "downOnly": True},
    {"lookback": 3600, "tail": 0.27, "target": 1.0, "wait": 20, "downOnly": False},
    {"lookback": 2700, "tail": 0.20, "target": 2.0, "wait": 45, "downOnly": False},
)


def run_grid(bars: pd.DataFrame, *, fast: bool = False) -> dict:
    start, end = bars.index.min(), bars.index.max()
    cases = []
    variants = ("return_mean_std", "return_median_mad", "price_level_z")
    for variant in variants:
        if fast:
            normal_params = sorted({(c["lookback"], c["tail"]) for c in FAST_CANDIDATES})
        else:
            normal_params = [(lookback, tail) for lookback in (1800, 2700, 3600, 4200, 5400, 7200) for tail in (0.18, 0.20, 0.22, 0.25, 0.27)]
        for lookback, tail in normal_params:
                cfg = SecondNormalConfig(
                    strategy_id=f"NORMAL_{variant}_{lookback}_{int(tail * 100)}",
                    lookback_sec=lookback,
                    horizon_sec=600,
                    signal_gap_sec=600,
                    tail_pct=tail,
                    second_filter="none",
                    amount=5,
                    label="normal_eta_variants",
                )
                raw = generate_variant_signals(variant, bars, cfg)
                signals, _ = execute_signals(raw, per_strategy_lock=True, cooldown_sec=600, use_horizon_as_lock=True)
                direct = direct_rows(signals, bars)
                if fast:
                    eta_params = sorted(
                        {
                            (c["target"], c["wait"], c["downOnly"])
                            for c in FAST_CANDIDATES
                            if c["lookback"] == lookback and c["tail"] == tail
                        }
                    )
                else:
                    eta_params = [
                        (target_bps, wait_sec, down_only)
                        for target_bps, wait_sec in ((1.0, 20), (1.0, 45), (1.0, 90), (2.0, 45), (3.0, 45))
                        for down_only in (False, True)
                    ]
                for target_bps, wait_sec, down_only in eta_params:
                        rows, forecast = eta_rows(
                            signals,
                            bars,
                            target_bps=target_bps,
                            max_wait_sec=wait_sec,
                            down_only=down_only,
                        )
                        cases.append(
                            {
                                "variant": variant,
                                "lookbackSec": lookback,
                                "tailPct": tail,
                                "targetBps": target_bps,
                                "maxWaitSec": wait_sec,
                                "downOnly": down_only,
                                "rawSignals": len(raw),
                                "executableSignals": len(signals),
                                "direct": metrics(direct, start, end),
                                "eta": metrics(rows, start, end),
                                "forecast": forecast,
                                "etaBySide": side_metrics(rows, start, end),
                                "etaByDay": day_metrics(rows),
                            }
                        )
    ranked = sorted(
        (
            {
                "variant": c["variant"],
                "lookbackSec": c["lookbackSec"],
                "tailPct": c["tailPct"],
                "targetBps": c["targetBps"],
                "maxWaitSec": c["maxWaitSec"],
                "downOnly": c["downOnly"],
                "directWinRate": c["direct"]["winRate"],
                "directTradesPerDay": c["direct"]["tradesPerDay"],
                **{f"eta_{k}": v for k, v in c["eta"].items()},
                **c["forecast"],
            }
            for c in cases
            if c["eta"]["trades"] >= 20
        ),
        key=lambda item: (item["eta_pnlU_5u_80pct"], item["eta_winRate"], -item["eta_maxLoss"], item["eta_trades"]),
        reverse=True,
    )
    stable = sorted(
        (
            item
            for item in ranked
            if item["eta_winRate"] >= 65 and item["eta_tradesPerDay"] >= 8 and item["eta_maxLoss"] <= 3
        ),
        key=lambda item: (item["eta_winRate"], item["eta_pnlU_5u_80pct"], item["eta_tradesPerDay"]),
        reverse=True,
    )
    by_variant = {}
    for variant in variants:
        subset = [item for item in ranked if item["variant"] == variant]
        by_variant[variant] = subset[:10]
    return {"cases": cases, "ranked": ranked, "stable": stable, "byVariant": by_variant}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--old-csv", default=str(DEFAULT_OLD_CSV))
    p.add_argument("--shard-dir", default=str(DEFAULT_SHARD_DIR))
    p.add_argument("--prod-config", default=str(DEFAULT_PROD_CONFIG))
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--fast", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    bars = load_bars(Path(args.old_csv), Path(args.shard_dir))
    result = run_grid(bars, fast=bool(args.fast))
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "sample": {
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "hours": round((bars.index.max() - bars.index.min()).total_seconds() / 3600.0, 2),
            "rows": int(len(bars)),
            "observedPct": round(float(bars["observed"].mean() * 100), 2),
        },
        "method": (
            "Compare normal variants using the same 10m settlement and the same ETA entry filter. "
            "All features use data at or before signal second; entry search starts after the signal second."
        ),
        "fastMode": bool(args.fast),
        **result,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report["sample"], ensure_ascii=False))
    print("RANKED")
    for item in report["ranked"][:30]:
        print(json.dumps(item, ensure_ascii=False))
    print("STABLE")
    for item in report["stable"][:30]:
        print(json.dumps(item, ensure_ascii=False))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
