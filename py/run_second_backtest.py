from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path

import pandas as pd

from second_backtest.data import audit_second_csv, load_second_bars
from second_backtest.execution import apply_signal_gap, execute_signals
from second_backtest.metrics import compact_metrics, payout_for_horizon, robust_score, split_metrics
from second_backtest.strategies import (
    SecondChipConfig,
    SecondNormalConfig,
    generate_chip_signals,
    generate_normal_signals,
    prod_configs_to_second_configs,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = ROOT / "tmp" / "latest_1s_pull_20260616_224315" / "btcusdt_1s_trades.csv"
DEFAULT_PROD_CONFIG = ROOT / "data" / "prod_config.json"
DEFAULT_TRADE_CONFIG = ROOT / "data" / "trade_config.json"
DEFAULT_OUT = ROOT / "tmp" / "second_backtest_report_latest.json"


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _amount_map_from_trade_config(path: Path) -> dict:
    config = _load_json(path)
    out = {}
    for item in config.get("strategyVariants") or []:
        if item.get("id") and item.get("amount") is not None:
            out[item["id"]] = item.get("amount")
    for key, value in (config.get("strategyAmounts") or {}).items():
        out.setdefault(key, value)
    return out


def _default_research_configs() -> list:
    return [
        SecondNormalConfig(
            strategy_id="BTC_10min_SECOND_3600_20",
            lookback_sec=3600,
            horizon_sec=600,
            tail_pct=0.20,
            amount=5,
            label="秒级正态 3600s 20/80",
        ),
        SecondChipConfig(
            strategy_id="BTC_10min_SECOND_CHIP_3600_20",
            lookback_sec=3600,
            horizon_sec=600,
            target_share=0.20,
            bin_mode="fixed",
            bin_size=20,
            break_pct=0.0023,
            direction_filter="breakout_up_only",
            chip_filter="none",
            amount=5,
            label="秒级筹码区 60m 20% 上破反转",
        ),
    ]


def _config_to_dict(cfg) -> dict:
    if is_dataclass(cfg):
        return asdict(cfg)
    return dict(cfg)


def _signals_for_config(
    bars: pd.DataFrame,
    cfg,
    *,
    apply_config_gap: bool = True,
) -> list[dict]:
    if isinstance(cfg, SecondNormalConfig):
        return generate_normal_signals(bars, cfg, apply_config_gap=apply_config_gap)
    if isinstance(cfg, SecondChipConfig):
        return generate_chip_signals(bars, cfg, apply_config_gap=apply_config_gap)
    raise TypeError(f"unsupported config: {cfg!r}")


def _execute_and_measure(
    bars: pd.DataFrame,
    signals: list[dict],
    cfg,
    *,
    global_lock_sec: int,
) -> tuple[list[dict], list[dict], dict]:
    executed, rejected = execute_signals(
        signals,
        per_strategy_lock=True,
        global_lock_sec=global_lock_sec,
        cooldown_sec=cfg.horizon_sec,
        use_horizon_as_lock=True,
    )
    payout = payout_for_horizon(cfg.horizon_sec)
    metrics = split_metrics(
        executed,
        bars.index.min(),
        bars.index.max(),
        amount=cfg.amount,
        payout_rate=payout,
    )
    return executed, rejected, metrics


def _execution_policy(
    cfg,
    accepted: list[dict],
    rejected: list[dict],
    *,
    global_lock_sec: int,
    configured_gap_applied: bool,
) -> dict:
    policy = {
        "perStrategyLock": True,
        "globalLockSec": int(global_lock_sec),
        "cooldownSec": int(cfg.horizon_sec),
        "accepted": len(accepted),
        "rejectedByStrategyLock": sum(
            1 for row in rejected if row.get("skipReason") == "strategy_lock"
        ),
        "rejectedByGlobalLock": sum(
            1 for row in rejected if row.get("skipReason") == "global_lock"
        ),
    }
    if configured_gap_applied:
        policy["configuredGapSec"] = int(cfg.signal_gap_sec)
    else:
        policy["configuredGapSecIgnored"] = int(cfg.signal_gap_sec)
    return policy


def _run_one(bars: pd.DataFrame, cfg, *, global_lock_sec: int) -> dict:
    raw = _signals_for_config(bars, cfg, apply_config_gap=False)
    configured_gap = apply_signal_gap(raw, cfg.signal_gap_sec)
    live_executed, live_rejected, live_metrics = _execute_and_measure(
        bars,
        raw,
        cfg,
        global_lock_sec=global_lock_sec,
    )
    gap_executed, gap_rejected, gap_metrics = _execute_and_measure(
        bars,
        configured_gap,
        cfg,
        global_lock_sec=global_lock_sec,
    )
    payout = payout_for_horizon(cfg.horizon_sec)
    raw_metrics = split_metrics(
        raw,
        bars.index.min(),
        bars.index.max(),
        amount=cfg.amount,
        payout_rate=payout,
    )
    return {
        "strategyId": cfg.strategy_id,
        "label": cfg.label,
        "modelType": "second_normal" if isinstance(cfg, SecondNormalConfig) else "second_chip",
        "params": _config_to_dict(cfg),
        "score": robust_score(gap_metrics),
        "rawSignals": compact_metrics(raw_metrics),
        "liveExecution": {
            "metrics": compact_metrics(live_metrics),
            "policy": _execution_policy(
                cfg,
                live_executed,
                live_rejected,
                global_lock_sec=global_lock_sec,
                configured_gap_applied=False,
            ),
            "sampleTrades": [_compact_trade(row) for row in live_executed[-10:]],
        },
        "configuredGapExecution": {
            "metrics": compact_metrics(gap_metrics),
            "policy": _execution_policy(
                cfg,
                gap_executed,
                gap_rejected,
                global_lock_sec=global_lock_sec,
                configured_gap_applied=True,
            ),
            "sampleTrades": [_compact_trade(row) for row in gap_executed[-10:]],
        },
    }


def _compact_trade(row: dict) -> dict:
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
        "breakout",
        "poc",
        "zone_low",
        "zone_high",
        "flow300",
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


def _portfolio_report(bars: pd.DataFrame, configs: list, *, global_lock_sec: int) -> dict:
    all_signals = []
    for cfg in configs:
        raw = _signals_for_config(bars, cfg, apply_config_gap=False)
        all_signals.extend(apply_signal_gap(raw, cfg.signal_gap_sec))
    executed, rejected = execute_signals(
        all_signals,
        per_strategy_lock=True,
        global_lock_sec=global_lock_sec,
        cooldown_sec=600,
        use_horizon_as_lock=True,
    )
    # Portfolio rows can have different amounts, so compute counts with a neutral
    # 5U/80% summary and report per-strategy money in the strategy sections.
    metrics = split_metrics(
        executed,
        bars.index.min(),
        bars.index.max(),
        amount=5,
        payout_rate=0.80,
    )
    return {
        "execution": {
            "perStrategyLock": True,
            "globalLockSec": int(global_lock_sec),
            "accepted": len(executed),
            "rejectedByStrategyLock": sum(
                1 for row in rejected if row.get("skipReason") == "strategy_lock"
            ),
            "rejectedByGlobalLock": sum(
                1 for row in rejected if row.get("skipReason") == "global_lock"
            ),
            "note": "Default is independent per-strategy locking. globalLockSec is only for research.",
        },
        "metrics": compact_metrics(metrics),
        "sampleTrades": [_compact_trade(row) for row in executed[-15:]],
    }


def build_report(args) -> dict:
    bars = load_second_bars(args.csv)
    amount_map = _amount_map_from_trade_config(Path(args.trade_config))
    prod_config = _load_json(Path(args.prod_config))
    configs = prod_configs_to_second_configs(prod_config, amount_map)
    if not configs or args.defaults:
        configs = _default_research_configs()

    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        configs = [cfg for cfg in configs if cfg.strategy_id in wanted]

    strategy_reports = [
        _run_one(bars, cfg, global_lock_sec=args.global_lock_sec)
        for cfg in configs
    ]
    strategy_reports.sort(
        key=lambda item: (
            item["score"],
            item["configuredGapExecution"]["metrics"]["all"]["trades"],
        ),
        reverse=True,
    )

    return {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "source": str(Path(args.csv).resolve()),
        "method": {
            "data": "1-second BTC bars only; missing seconds are forward-filled close with zero volume.",
            "causal": "Each signal uses data at or before its signal second and settles horizon_sec later.",
            "execution": "Reports liveExecution and configuredGapExecution; no global dedupe unless requested.",
        },
        "dataQuality": audit_second_csv(args.csv),
        "sample": {
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "hours": round(
                (bars.index.max() - bars.index.min()).total_seconds() / 3600.0,
                2,
            ),
            "rows": int(len(bars)),
            "observedRows": int(bars["observed"].sum()),
            "filledRows": int((~bars["observed"]).sum()),
        },
        "strategies": strategy_reports,
        "portfolio": _portfolio_report(
            bars,
            configs,
            global_lock_sec=args.global_lock_sec,
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run stable 1-second BTC strategy backtests.")
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="Path to btcusdt_1s_trades.csv")
    parser.add_argument("--prod-config", default=str(DEFAULT_PROD_CONFIG))
    parser.add_argument("--trade-config", default=str(DEFAULT_TRADE_CONFIG))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--only", default="", help="Comma-separated strategy ids")
    parser.add_argument("--defaults", action="store_true", help="Ignore prod config and run default research pair")
    parser.add_argument(
        "--global-lock-sec",
        type=int,
        default=0,
        help="Research-only global lock. Leave 0 to match independent strategy execution.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = build_report(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
