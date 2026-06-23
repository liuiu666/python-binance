from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from second_backtest.data import audit_second_sources, load_second_bars
from second_backtest.execution import execute_signals
from second_backtest.metrics import compact_metrics, payout_for_horizon, robust_score, split_metrics


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / "tmp" / "latest_second_pull_20260622_upfix" / "btcusdt_1s_trades.csv"
DEFAULT_OUT = ROOT / "tmp" / "auction_volume_10m_research_latest.json"


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass(frozen=True)
class AuctionConfig:
    lookback_sec: int
    bin_size: float
    value_area_pct: float = 0.70
    dist_bins: float = 2.0
    recent_sec: int = 180
    min_flow: float = 0.10
    min_volume_ratio: float = 1.20
    max_absorb_move_bps: float = 5.0
    top_node_pct: float = 0.25
    min_node_share: float = 0.035
    prob_floor: float = 0.52


def _window_pct(prefix: np.ndarray, start: int, end_exclusive: int) -> float:
    if end_exclusive <= start:
        return 0.0
    return 100.0 * float(prefix[end_exclusive] - prefix[start]) / float(end_exclusive - start)


def _settle(
    *,
    bars: pd.DataFrame,
    i: int,
    horizon_sec: int,
    strategy_id: str,
    model_type: str,
    signal: str,
    extra: dict[str, Any],
) -> dict[str, Any]:
    close = bars["close"].to_numpy(float)
    entry = float(close[i])
    settle = float(close[i + horizon_sec])
    return {
        "strategy_id": strategy_id,
        "model_type": model_type,
        "idx": int(i),
        "time": bars.index[i],
        "signal": signal,
        "entry": entry,
        "settle_time": bars.index[i + horizon_sec],
        "settle": settle,
        "won": bool(settle > entry if signal == "UP" else settle < entry),
        "horizon_sec": int(horizon_sec),
        "amount": 5.0,
        **extra,
    }


def _profile_features(
    close: np.ndarray,
    volume: np.ndarray,
    i: int,
    cfg: AuctionConfig,
) -> dict[str, Any] | None:
    start = i - cfg.lookback_sec + 1
    if start < 0:
        return None
    window_close = close[start : i + 1]
    window_volume = volume[start : i + 1]
    total_volume = float(window_volume.sum())
    if total_volume <= 0:
        return None

    bin_ids = np.floor(window_close / cfg.bin_size).astype(np.int64)
    uniq, inv = np.unique(bin_ids, return_inverse=True)
    vol_by_bin = np.bincount(inv, weights=window_volume)
    if len(uniq) == 0 or float(vol_by_bin.sum()) <= 0:
        return None

    order = np.argsort(vol_by_bin)[::-1]
    poc_bin = int(uniq[order[0]])
    threshold = total_volume * float(cfg.value_area_pct)
    cum = 0.0
    selected = []
    for pos in order:
        selected.append(int(uniq[pos]))
        cum += float(vol_by_bin[pos])
        if cum >= threshold:
            break
    val_bin = min(selected)
    vah_bin = max(selected)

    current_bin = int(math.floor(float(close[i]) / cfg.bin_size))
    curr_pos = np.searchsorted(uniq, current_bin)
    curr_volume = 0.0
    curr_rank_pct = 1.0
    if curr_pos < len(uniq) and int(uniq[curr_pos]) == current_bin:
        curr_volume = float(vol_by_bin[curr_pos])
        ranks = np.empty(len(order), dtype=int)
        ranks[order] = np.arange(len(order))
        curr_rank_pct = float(ranks[curr_pos] + 1) / float(len(uniq))

    val = val_bin * cfg.bin_size
    vah = (vah_bin + 1) * cfg.bin_size
    price = float(close[i])
    if price < val - cfg.dist_bins * cfg.bin_size:
        zone = "below_value"
        outside_bins = (val - price) / cfg.bin_size
    elif price > vah + cfg.dist_bins * cfg.bin_size:
        zone = "above_value"
        outside_bins = (price - vah) / cfg.bin_size
    else:
        zone = "inside_value"
        outside_bins = 0.0

    return {
        "poc": poc_bin * cfg.bin_size,
        "val": val,
        "vah": vah,
        "zone": zone,
        "outside_bins": float(outside_bins),
        "current_bin": current_bin * cfg.bin_size,
        "current_bin_share": curr_volume / total_volume,
        "current_bin_rank_pct": curr_rank_pct,
        "high_volume_node": bool(
            curr_rank_pct <= cfg.top_node_pct or curr_volume / total_volume >= cfg.min_node_share
        ),
    }


def _make_configs() -> list[AuctionConfig]:
    configs: list[AuctionConfig] = []
    for lookback_sec in (1800, 3600, 7200):
        for bin_size in (20.0, 50.0):
            for dist_bins in (1.0, 2.0, 3.0):
                configs.append(
                    AuctionConfig(
                        lookback_sec=lookback_sec,
                        bin_size=bin_size,
                        dist_bins=dist_bins,
                        recent_sec=180,
                        min_flow=0.10,
                        min_volume_ratio=1.20,
                        max_absorb_move_bps=5.0,
                    )
                )
    return configs


def generate_signals(
    bars: pd.DataFrame,
    cfg: AuctionConfig,
    *,
    horizon_sec: int,
    entry_step_sec: int,
    min_observed_pct: float,
) -> list[dict[str, Any]]:
    close = bars["close"].to_numpy(float)
    volume = bars["volume"].to_numpy(float)
    buy = bars["buy_qty"].to_numpy(float)
    sell = bars["sell_qty"].to_numpy(float)
    observed = bars["observed"].astype(bool).to_numpy()
    obs_prefix = np.concatenate([[0], np.cumsum(observed.astype(int))])

    close_s = pd.Series(close, index=bars.index)
    volume_s = pd.Series(volume, index=bars.index)
    buy_s = pd.Series(buy, index=bars.index)
    sell_s = pd.Series(sell, index=bars.index)
    log_ret = np.diff(np.log(close), prepend=np.nan)
    ret_s = pd.Series(log_ret, index=bars.index)

    mu1 = ret_s.rolling(cfg.lookback_sec, min_periods=max(300, cfg.lookback_sec // 3)).mean().to_numpy(float)
    sig1 = ret_s.rolling(cfg.lookback_sec, min_periods=max(300, cfg.lookback_sec // 3)).std(ddof=1).to_numpy(float)
    vol_recent = volume_s.rolling(cfg.recent_sec, min_periods=max(10, cfg.recent_sec // 3)).sum().to_numpy(float)
    buy_recent = buy_s.rolling(cfg.recent_sec, min_periods=1).sum().to_numpy(float)
    sell_recent = sell_s.rolling(cfg.recent_sec, min_periods=1).sum().to_numpy(float)
    vol_ref = (
        volume_s.rolling(cfg.lookback_sec, min_periods=max(300, cfg.lookback_sec // 3)).mean()
        * cfg.recent_sec
    ).to_numpy(float)

    rows: list[dict[str, Any]] = []
    first = max(cfg.lookback_sec, cfg.recent_sec)
    last = len(close) - horizon_sec - 1
    for i in range(first, last, max(1, int(entry_step_sec))):
        if _window_pct(obs_prefix, i - cfg.lookback_sec + 1, i + 1) < min_observed_pct:
            continue
        if _window_pct(obs_prefix, i + 1, i + horizon_sec + 1) < min_observed_pct:
            continue

        profile = _profile_features(close, volume, i, cfg)
        if not profile:
            continue

        h_mu = float(mu1[i]) * horizon_sec
        h_sig = float(sig1[i]) * math.sqrt(horizon_sec)
        p_up = normal_cdf(h_mu / h_sig) if np.isfinite(h_mu) and np.isfinite(h_sig) and h_sig > 0 else 0.5
        flow = float((buy_recent[i] - sell_recent[i]) / max(buy_recent[i] + sell_recent[i], 1e-12))
        volume_ratio = float(vol_recent[i] / max(vol_ref[i], 1e-12)) if np.isfinite(vol_ref[i]) else 0.0
        move_bps = float((close[i] / close[i - cfg.recent_sec] - 1.0) * 10000.0)

        common = {
            **profile,
            "lookback_sec": cfg.lookback_sec,
            "bin_size": cfg.bin_size,
            "dist_bins": cfg.dist_bins,
            "p_up": p_up,
            "flow": flow,
            "volume_ratio": volume_ratio,
            "move_bps": move_bps,
        }

        zone = str(profile["zone"])
        if zone in ("above_value", "below_value"):
            revert = "DOWN" if zone == "above_value" else "UP"
            cont = "UP" if zone == "above_value" else "DOWN"
            rows.append(
                _settle(
                    bars=bars,
                    i=i,
                    horizon_sec=horizon_sec,
                    strategy_id=f"AUCTION_REVERT_L{cfg.lookback_sec}_B{int(cfg.bin_size)}_D{cfg.dist_bins:g}",
                    model_type="auction_value_area_revert",
                    signal=revert,
                    extra=common,
                )
            )
            if (cont == "UP" and flow >= cfg.min_flow and move_bps > 0) or (
                cont == "DOWN" and flow <= -cfg.min_flow and move_bps < 0
            ):
                rows.append(
                    _settle(
                        bars=bars,
                        i=i,
                        horizon_sec=horizon_sec,
                        strategy_id=f"AUCTION_CONT_FLOW_L{cfg.lookback_sec}_B{int(cfg.bin_size)}_D{cfg.dist_bins:g}",
                        model_type="auction_breakout_continue_flow",
                        signal=cont,
                        extra=common,
                    )
                )
            if (revert == "UP" and p_up >= cfg.prob_floor) or (
                revert == "DOWN" and p_up <= 1.0 - cfg.prob_floor
            ):
                rows.append(
                    _settle(
                        bars=bars,
                        i=i,
                        horizon_sec=horizon_sec,
                        strategy_id=f"AUCTION_COMBO_REVERT_PROB_L{cfg.lookback_sec}_B{int(cfg.bin_size)}_D{cfg.dist_bins:g}",
                        model_type="auction_revert_plus_normal_prob",
                        signal=revert,
                        extra=common,
                    )
                )

        is_absorption = (
            bool(profile["high_volume_node"])
            and volume_ratio >= cfg.min_volume_ratio
            and abs(move_bps) <= cfg.max_absorb_move_bps
            and abs(flow) >= cfg.min_flow
        )
        if is_absorption:
            signal = "UP" if flow < 0 else "DOWN"
            rows.append(
                _settle(
                    bars=bars,
                    i=i,
                    horizon_sec=horizon_sec,
                    strategy_id=f"AUCTION_ABSORB_L{cfg.lookback_sec}_B{int(cfg.bin_size)}",
                    model_type="auction_absorption_reversal",
                    signal=signal,
                    extra=common,
                )
            )
            if (signal == "UP" and p_up >= 0.45) or (signal == "DOWN" and p_up <= 0.55):
                rows.append(
                    _settle(
                        bars=bars,
                        i=i,
                        horizon_sec=horizon_sec,
                        strategy_id=f"AUCTION_COMBO_ABSORB_PROB_L{cfg.lookback_sec}_B{int(cfg.bin_size)}",
                        model_type="auction_absorption_plus_prob",
                        signal=signal,
                        extra=common,
                    )
                )

    return rows


def _compact_trade(row: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "strategy_id",
        "model_type",
        "time",
        "signal",
        "entry",
        "settle_time",
        "settle",
        "won",
        "p_up",
        "zone",
        "poc",
        "val",
        "vah",
        "current_bin_share",
        "current_bin_rank_pct",
        "flow",
        "volume_ratio",
        "move_bps",
    ]
    out = {}
    for key in keys:
        if key not in row:
            continue
        value = row[key]
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        elif isinstance(value, float):
            value = round(value, 6)
        out[key] = value
    return out


def _variant_report(bars: pd.DataFrame, strategy_id: str, raw: list[dict[str, Any]], horizon_sec: int) -> dict:
    executed, rejected = execute_signals(
        raw,
        per_strategy_lock=True,
        global_lock_sec=0,
        cooldown_sec=horizon_sec,
        use_horizon_as_lock=True,
    )
    metrics = split_metrics(
        executed,
        bars.index.min(),
        bars.index.max(),
        amount=5.0,
        payout_rate=payout_for_horizon(horizon_sec),
    )
    return {
        "strategyId": strategy_id,
        "modelType": raw[0].get("model_type") if raw else "unknown",
        "rawSignals": len(raw),
        "rejectedByLock": len(rejected),
        "score": robust_score(metrics),
        "metrics": compact_metrics(metrics),
        "sampleTrades": [_compact_trade(row) for row in executed[-8:]],
    }


def build_report(args: argparse.Namespace) -> dict:
    bars = load_second_bars(args.csv, include_shards=not args.no_shards)
    configs = _make_configs()
    by_strategy: dict[str, list[dict[str, Any]]] = {}
    for cfg in configs:
        for row in generate_signals(
            bars,
            cfg,
            horizon_sec=args.horizon_sec,
            entry_step_sec=args.entry_step_sec,
            min_observed_pct=args.min_observed_pct,
        ):
            by_strategy.setdefault(row["strategy_id"], []).append(row)

    reports = [
        _variant_report(bars, strategy_id, raw, args.horizon_sec)
        for strategy_id, raw in by_strategy.items()
    ]
    reports.sort(
        key=lambda item: (
            item["score"],
            item["metrics"]["all"]["trades"],
            item["metrics"]["all"]["winRate"] or 0.0,
        ),
        reverse=True,
    )

    return {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "source": str(Path(args.csv).resolve()),
        "method": {
            "goal": "10-minute binary option direction test using auction volume profile, taker flow, absorption, and normal-probability confirmation.",
            "causal": "Every signal only uses seconds at or before entry; settlement is entry + horizon_sec.",
            "execution": "Per-strategy 10-minute lock after entry; no global dedupe.",
            "entryStepSec": int(args.entry_step_sec),
            "minObservedPct": float(args.min_observed_pct),
            "configs": [asdict(cfg) for cfg in configs],
        },
        "dataQuality": audit_second_sources(args.csv, include_shards=not args.no_shards),
        "sample": {
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "hours": round((bars.index.max() - bars.index.min()).total_seconds() / 3600.0, 2),
            "rows": int(len(bars)),
            "observedRows": int(bars["observed"].sum()),
            "filledRows": int((~bars["observed"]).sum()),
        },
        "variantCount": len(reports),
        "topByScore": reports[:30],
        "topByTradeCountPositive": [
            item
            for item in sorted(
                reports,
                key=lambda row: (
                    row["metrics"]["all"]["pnl"],
                    row["metrics"]["all"]["trades"],
                    row["metrics"]["all"]["winRate"] or 0.0,
                ),
                reverse=True,
            )
            if item["metrics"]["all"]["trades"] >= args.min_report_trades
        ][:30],
        "allVariants": reports,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Research 10m auction-volume signals on BTC 1s data.")
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--horizon-sec", type=int, default=600)
    parser.add_argument("--entry-step-sec", type=int, default=10)
    parser.add_argument("--min-observed-pct", type=float, default=95.0)
    parser.add_argument("--min-report-trades", type=int, default=8)
    parser.add_argument("--no-shards", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = build_report(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report["topByScore"][:10], ensure_ascii=False, indent=2, default=str))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
