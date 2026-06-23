from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from run_second_backtest import _amount_map_from_trade_config, _load_json, _signals_for_config
from second_backtest.data import audit_second_sources, load_second_bars
from second_backtest.execution import apply_signal_gap, execute_signals
from second_backtest.incident_filter import apply_incident_filter_to_signals, incident_config_from_dict
from second_backtest.metrics import compact_metrics, payout_for_horizon, split_metrics
from second_backtest.strategies import prod_configs_to_second_configs
from research_poc_normal_10m import PocNormalConfig, _poc_normal_features


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / "tmp" / "latest_second_pull_20260622_upfix" / "btcusdt_1s_trades.csv"
DEFAULT_PROD_CONFIG = ROOT / "tmp" / "latest_server_pull_20260622" / "prod_config.json"
DEFAULT_TRADE_CONFIG = ROOT / "data" / "trade_config.json"
DEFAULT_OUT = ROOT / "tmp" / "online_overlay_filter_research_latest.json"


@dataclass(frozen=True)
class OverlayConfig:
    name: str
    global_lock_sec: int = 0
    poc_mode: str = "none"
    poc_z: float = 2.0
    poc_lookback_sec: int = 3600
    poc_bin_size: float = 50.0
    trend_veto: bool = False
    trend_10m_bps: float = 20.0
    trend_30m_bps: float = 35.0
    loss_pause_after: int = 0
    loss_pause_sec: int = 3600


def _make_overlays() -> list[OverlayConfig]:
    return [
        OverlayConfig("baseline_incident"),
        OverlayConfig("global_dedupe_600", global_lock_sec=600),
        OverlayConfig("poc_confirm_z2", poc_mode="confirm", poc_z=2.0),
        OverlayConfig("poc_confirm_z15", poc_mode="confirm", poc_z=1.5),
        OverlayConfig("poc_veto_opposite_z2", poc_mode="veto_opposite", poc_z=2.0),
        OverlayConfig("trend_veto", trend_veto=True),
        OverlayConfig("loss_pause_3_60m", loss_pause_after=3, loss_pause_sec=3600),
        OverlayConfig(
            "dedupe_poc_confirm_z2",
            global_lock_sec=600,
            poc_mode="confirm",
            poc_z=2.0,
        ),
        OverlayConfig(
            "dedupe_poc_veto_z2",
            global_lock_sec=600,
            poc_mode="veto_opposite",
            poc_z=2.0,
        ),
        OverlayConfig(
            "dedupe_trend_veto",
            global_lock_sec=600,
            trend_veto=True,
        ),
        OverlayConfig(
            "dedupe_loss_pause",
            global_lock_sec=600,
            loss_pause_after=3,
            loss_pause_sec=3600,
        ),
        OverlayConfig(
            "dedupe_poc_veto_trend",
            global_lock_sec=600,
            poc_mode="veto_opposite",
            poc_z=2.0,
            trend_veto=True,
        ),
        OverlayConfig(
            "dedupe_poc_veto_trend_loss",
            global_lock_sec=600,
            poc_mode="veto_opposite",
            poc_z=2.0,
            trend_veto=True,
            loss_pause_after=3,
            loss_pause_sec=3600,
        ),
    ]


def _build_online_incident_signals(bars: pd.DataFrame, prod_config: dict, trade_config_path: Path) -> tuple[list[dict], dict]:
    amount_map = _amount_map_from_trade_config(trade_config_path)
    configs = prod_configs_to_second_configs(prod_config, amount_map)
    all_signals: list[dict] = []
    diagnostics: dict[str, Any] = {}
    for cfg in configs:
        raw = _signals_for_config(bars, cfg, apply_config_gap=False)
        gap = apply_signal_gap(raw, cfg.signal_gap_sec)
        incident_cfg = incident_config_from_dict(getattr(cfg, "incident_filter", {}))
        filtered, blocked, diag = apply_incident_filter_to_signals(bars, gap, incident_cfg)
        all_signals.extend(filtered)
        diagnostics[cfg.strategy_id] = {
            "raw": len(raw),
            "afterGap": len(gap),
            "afterIncident": len(filtered),
            "incidentBlocked": len(blocked),
            "incidentDiagnostics": diag,
        }
    return sorted(all_signals, key=lambda row: row["time"]), diagnostics


def _index_by_time(bars: pd.DataFrame) -> dict[pd.Timestamp, int]:
    return {pd.Timestamp(t): i for i, t in enumerate(bars.index)}


def _apply_overlay_pre_execution(
    bars: pd.DataFrame,
    signals: list[dict],
    overlay: OverlayConfig,
) -> tuple[list[dict], list[dict], dict[str, int]]:
    close = bars["close"].to_numpy(float)
    volume = bars["volume"].to_numpy(float)
    idx_by_time = _index_by_time(bars)
    poc_cfg = PocNormalConfig(
        lookback_sec=overlay.poc_lookback_sec,
        bin_size=overlay.poc_bin_size,
        z_entry=overlay.poc_z,
        flow_mode="none",
    )
    accepted: list[dict] = []
    rejected: list[dict] = []
    reasons: dict[str, int] = {}

    for row in signals:
        idx = row.get("idx")
        if idx is None:
            idx = idx_by_time.get(pd.Timestamp(row["time"]))
        if idx is None:
            skipped = dict(row)
            skipped["skipReason"] = "missing_index"
            rejected.append(skipped)
            reasons["missing_index"] = reasons.get("missing_index", 0) + 1
            continue
        idx = int(idx)
        reason = None

        if overlay.poc_mode != "none":
            features = _poc_normal_features(close, volume, idx, poc_cfg)
            if not features:
                reason = "poc_unavailable"
            else:
                z = float(features["poc_z"])
                signal = row.get("signal")
                if overlay.poc_mode == "confirm":
                    if signal == "UP" and z > -overlay.poc_z:
                        reason = "poc_not_confirm_up"
                    elif signal == "DOWN" and z < overlay.poc_z:
                        reason = "poc_not_confirm_down"
                elif overlay.poc_mode == "veto_opposite":
                    if signal == "UP" and z >= overlay.poc_z:
                        reason = "poc_opposes_up"
                    elif signal == "DOWN" and z <= -overlay.poc_z:
                        reason = "poc_opposes_down"
                row = {**row, **{f"overlay_{k}": v for k, v in features.items()}}

        if reason is None and overlay.trend_veto:
            if idx >= 1800:
                move_10m = (close[idx] / close[idx - 600] - 1.0) * 10000.0
                move_30m = (close[idx] / close[idx - 1800] - 1.0) * 10000.0
                signal = row.get("signal")
                if signal == "UP" and move_10m <= -overlay.trend_10m_bps and move_30m <= -overlay.trend_30m_bps:
                    reason = "trend_down_blocks_up"
                elif signal == "DOWN" and move_10m >= overlay.trend_10m_bps and move_30m >= overlay.trend_30m_bps:
                    reason = "trend_up_blocks_down"
                row = {
                    **row,
                    "overlay_trend_10m_bps": float(move_10m),
                    "overlay_trend_30m_bps": float(move_30m),
                }

        if reason is None:
            accepted.append(row)
            continue
        skipped = dict(row)
        skipped["skipReason"] = reason
        rejected.append(skipped)
        reasons[reason] = reasons.get(reason, 0) + 1

    return accepted, rejected, reasons


def _apply_loss_pause(trades: list[dict], after_losses: int, pause_sec: int) -> tuple[list[dict], list[dict]]:
    if after_losses <= 0 or pause_sec <= 0:
        return trades, []
    accepted: list[dict] = []
    rejected: list[dict] = []
    pending: list[tuple[pd.Timestamp, bool]] = []
    consecutive_losses = 0
    pause_until: pd.Timestamp | None = None

    for row in sorted(trades, key=lambda item: item["time"]):
        now = pd.Timestamp(row["time"])
        still_pending = []
        for settle_time, won in pending:
            if settle_time <= now:
                consecutive_losses = 0 if won else consecutive_losses + 1
                if consecutive_losses >= after_losses:
                    pause_until = settle_time + pd.Timedelta(seconds=int(pause_sec))
                    consecutive_losses = 0
            else:
                still_pending.append((settle_time, won))
        pending = still_pending

        if pause_until is not None and now < pause_until:
            skipped = dict(row)
            skipped["skipReason"] = "loss_pause"
            skipped["pauseUntil"] = pause_until.isoformat()
            rejected.append(skipped)
            continue
        accepted.append(row)
        pending.append((pd.Timestamp(row["settle_time"]), bool(row["won"])))
    return accepted, rejected


def _run_overlay(bars: pd.DataFrame, signals: list[dict], overlay: OverlayConfig) -> dict:
    pre_signals, pre_rejected, pre_reasons = _apply_overlay_pre_execution(bars, signals, overlay)
    executed, execution_rejected = execute_signals(
        pre_signals,
        per_strategy_lock=True,
        global_lock_sec=int(overlay.global_lock_sec),
        cooldown_sec=600,
        use_horizon_as_lock=True,
    )
    final_trades, pause_rejected = _apply_loss_pause(
        executed,
        overlay.loss_pause_after,
        overlay.loss_pause_sec,
    )
    metrics = split_metrics(
        final_trades,
        bars.index.min(),
        bars.index.max(),
        amount=5.0,
        payout_rate=payout_for_horizon(600),
    )
    return {
        "name": overlay.name,
        "config": asdict(overlay),
        "metrics": compact_metrics(metrics),
        "policy": {
            "inputSignals": len(signals),
            "afterOverlay": len(pre_signals),
            "overlayRejected": len(pre_rejected),
            "overlayRejectReasons": pre_reasons,
            "executedBeforePause": len(executed),
            "executionRejected": len(execution_rejected),
            "lossPauseRejected": len(pause_rejected),
            "finalTrades": len(final_trades),
        },
    }


def build_report(args: argparse.Namespace) -> dict:
    bars = load_second_bars(args.csv, include_shards=not args.no_shards)
    prod_config = _load_json(Path(args.prod_config))
    signals, signal_diag = _build_online_incident_signals(
        bars,
        prod_config,
        Path(args.trade_config),
    )
    overlays = [_run_overlay(bars, signals, overlay) for overlay in _make_overlays()]
    overlays.sort(
        key=lambda item: (
            item["metrics"]["all"]["pnl"],
            item["metrics"]["all"]["winRate"] or 0.0,
            -item["metrics"]["all"]["maxLoss"],
        ),
        reverse=True,
    )
    return {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "source": str(Path(args.csv).resolve()),
        "prodConfig": str(Path(args.prod_config).resolve()),
        "method": {
            "base": "Current online second strategies with their incident filters applied first.",
            "overlays": "Research-only overlays: global dedupe, POC confirmation/veto, trend veto, and loss pause.",
            "payout": "80% payout, 5U amount.",
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
        "baseSignalDiagnostics": signal_diag,
        "overlays": overlays,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test research overlays on current online strategies.")
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--prod-config", default=str(DEFAULT_PROD_CONFIG))
    parser.add_argument("--trade-config", default=str(DEFAULT_TRADE_CONFIG))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--no-shards", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = build_report(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    summary = [
        {
            "name": row["name"],
            "trades": row["metrics"]["all"]["trades"],
            "winRate": row["metrics"]["all"]["winRate"],
            "pnl": row["metrics"]["all"]["pnl"],
            "maxLoss": row["metrics"]["all"]["maxLoss"],
            "policy": row["policy"],
        }
        for row in report["overlays"]
    ]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
