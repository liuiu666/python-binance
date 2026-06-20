from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from research_arrival_forecast import (
    DEFAULT_OLD_CSV,
    DEFAULT_PROD_CONFIG,
    DEFAULT_SHARD_DIR,
    first_hit,
    forecast_eta,
    load_bars,
    signal_side,
)
from second_backtest.execution import execute_signals
from second_backtest.strategies import SecondNormalConfig, generate_normal_signals


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "tmp" / "normal_eta_research_latest.json"


def max_loss(rows: list[dict]) -> int:
    cur = 0
    out = 0
    for row in sorted(rows, key=lambda item: item["entry_time"]):
        if row["won"]:
            cur = 0
        else:
            cur += 1
            out = max(out, cur)
    return out


def metrics(rows: list[dict], start: pd.Timestamp, end: pd.Timestamp) -> dict:
    n = len(rows)
    wins = sum(bool(row["won"]) for row in rows)
    pnl = sum(4 if row["won"] else -5 for row in rows)
    days = max((end - start).total_seconds() / 86400.0, 1e-12)
    return {
        "trades": n,
        "winRate": round(wins / n * 100, 2) if n else 0.0,
        "pnlU_5u_80pct": int(pnl),
        "maxLoss": max_loss(rows),
        "tradesPerDay": round(n / days, 2),
        "avgDelaySec": round(float(np.mean([r["delay_sec"] for r in rows])), 2) if rows else 0.0,
    }


def day_metrics(rows: list[dict]) -> dict:
    out = {}
    for day in sorted({row["entry_time"].date().isoformat() for row in rows}):
        subset = [row for row in rows if row["entry_time"].date().isoformat() == day]
        out[day] = metrics(subset, min(r["entry_time"] for r in subset), max(r["entry_time"] for r in subset))
    return out


def side_metrics(rows: list[dict], start: pd.Timestamp, end: pd.Timestamp) -> dict:
    return {
        side: metrics([row for row in rows if row["signal"] == side], start, end)
        for side in ("UP", "DOWN")
    }


def direct_rows(signals: list[dict], bars: pd.DataFrame) -> list[dict]:
    close = bars["close"].to_numpy(float)
    rows = []
    for row in signals:
        idx = int(row["idx"])
        horizon = int(row.get("horizon_sec") or 600)
        if idx + horizon >= len(close):
            continue
        entry = close[idx]
        settle = close[idx + horizon]
        won = bool(settle > entry if row["signal"] == "UP" else settle < entry)
        rows.append(
            {
                "entry_time": bars.index[idx],
                "strategy_id": row.get("strategy_id"),
                "signal": row["signal"],
                "won": won,
                "delay_sec": 0,
            }
        )
    return rows


def eta_rows(
    signals: list[dict],
    bars: pd.DataFrame,
    *,
    target_bps: float,
    max_wait_sec: int,
    down_only: bool,
) -> tuple[list[dict], dict]:
    close = bars["close"].to_numpy(float)
    high = bars["high"].to_numpy(float)
    low = bars["low"].to_numpy(float)
    buy = bars["buy_qty"].to_numpy(float)
    sell = bars["sell_qty"].to_numpy(float)
    rows = []
    tested = 0
    predicted_count = 0
    false_predicted = 0
    actual_hits = 0
    for sig in signals:
        if down_only and sig.get("signal") != "DOWN":
            continue
        idx = int(sig["idx"])
        if idx + max_wait_sec + 600 >= len(close):
            continue
        tested += 1
        side = signal_side(str(sig["signal"]))
        fc = forecast_eta(
            close,
            buy,
            sell,
            idx,
            side,
            target_bps,
            speed_window=30,
            accel_window=10,
            min_speed_bps=0.005,
        )
        if not fc.get("ok"):
            continue
        predicted = fc["eta_sec"] <= max_wait_sec
        if predicted:
            predicted_count += 1
        hit_idx, entry = first_hit(high, low, close, idx, side, target_bps, max_wait_sec)
        if hit_idx is None:
            if predicted:
                false_predicted += 1
            continue
        actual_hits += 1
        if not predicted:
            continue
        settle = close[hit_idx + 600]
        won = bool(settle > entry if sig["signal"] == "UP" else settle < entry)
        rows.append(
            {
                "entry_time": bars.index[hit_idx],
                "strategy_id": sig.get("strategy_id"),
                "signal": sig["signal"],
                "won": won,
                "delay_sec": int(hit_idx - idx),
                "eta_sec": float(fc["eta_sec"]),
            }
        )
    return rows, {
        "testedSignals": tested,
        "actualHitCount": actual_hits,
        "actualHitRate": round(actual_hits / tested * 100, 2) if tested else 0.0,
        "predictedHitCount": predicted_count,
        "predictedHitPrecision": round((predicted_count - false_predicted) / predicted_count * 100, 2)
        if predicted_count
        else 0.0,
        "falsePredictedHitCount": false_predicted,
    }


def run_grid(bars: pd.DataFrame) -> dict:
    start, end = bars.index.min(), bars.index.max()
    cases = []
    for lookback in (1800, 2700, 3600, 4200, 5400, 7200):
        for tail in (0.18, 0.20, 0.22, 0.25, 0.27):
            cfg = SecondNormalConfig(
                strategy_id=f"NORMAL_{lookback}_{int(tail * 100)}",
                lookback_sec=lookback,
                horizon_sec=600,
                signal_gap_sec=600,
                tail_pct=tail,
                second_filter="none",
                amount=5,
                label="normal_eta_research",
            )
            raw = generate_normal_signals(bars, cfg, apply_config_gap=True)
            signals, _ = execute_signals(raw, per_strategy_lock=True, cooldown_sec=600, use_horizon_as_lock=True)
            direct = direct_rows(signals, bars)
            for target_bps, wait_sec in ((1.0, 20), (1.0, 45), (1.0, 90), (2.0, 45), (3.0, 45)):
                for down_only in (False, True):
                    rows, forecast = eta_rows(
                        signals,
                        bars,
                        target_bps=target_bps,
                        max_wait_sec=wait_sec,
                        down_only=down_only,
                    )
                    cases.append(
                        {
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
            item for item in ranked if item["eta_winRate"] >= 65 and item["eta_tradesPerDay"] >= 8 and item["eta_maxLoss"] <= 3
        ),
        key=lambda item: (item["eta_winRate"], item["eta_pnlU_5u_80pct"], item["eta_tradesPerDay"]),
        reverse=True,
    )
    return {"cases": cases, "ranked": ranked, "stable": stable}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--old-csv", default=str(DEFAULT_OLD_CSV))
    p.add_argument("--shard-dir", default=str(DEFAULT_SHARD_DIR))
    p.add_argument("--prod-config", default=str(DEFAULT_PROD_CONFIG))
    p.add_argument("--out", default=str(DEFAULT_OUT))
    return p.parse_args()


def main() -> None:
    args = parse_args()
    bars = load_bars(Path(args.old_csv), Path(args.shard_dir))
    result = run_grid(bars)
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "sample": {
            "start": bars.index.min().isoformat(),
            "end": bars.index.max().isoformat(),
            "hours": round((bars.index.max() - bars.index.min()).total_seconds() / 3600.0, 2),
            "rows": int(len(bars)),
            "observedPct": round(float(bars["observed"].mean() * 100), 2),
        },
        **result,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report["sample"], ensure_ascii=False))
    print("RANKED")
    for item in report["ranked"][:20]:
        print(json.dumps(item, ensure_ascii=False))
    print("STABLE")
    for item in report["stable"][:20]:
        print(json.dumps(item, ensure_ascii=False))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
