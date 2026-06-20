from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from second_backtest.execution import execute_signals
from second_backtest.strategies import (
    generate_chip_signals,
    generate_normal_signals,
    prod_configs_to_second_configs,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OLD_CSV = ROOT / "tmp" / "latest_server_recheck_20260618_015135" / "btcusdt_1s_trades.csv"
DEFAULT_SHARD_DIR = ROOT / "tmp" / "latest_second_pull_20260620_131022" / "data" / "second" / "BTCUSDT" / "futures"
DEFAULT_PROD_CONFIG = ROOT / "tmp" / "latest_second_pull_20260620_131022" / "data" / "prod_config.json"
DEFAULT_OUT = ROOT / "tmp" / "dynamic_entry_research_latest.json"


def load_bars(old_csv: Path, shard_dir: Path) -> pd.DataFrame:
    cols = [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "taker_buy_volume",
        "taker_sell_volume",
    ]
    files = []
    if old_csv.exists():
        files.append(old_csv)
    if shard_dir.exists():
        files.extend(sorted(shard_dir.glob("*.csv")))
    if not files:
        raise FileNotFoundError("no second data found")
    parts = []
    for path in files:
        df = pd.read_csv(path, usecols=cols)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.floor("s")
        parts.append(df)
    raw = (
        pd.concat(parts, ignore_index=True)
        .sort_values("timestamp")
        .drop_duplicates("timestamp", keep="last")
        .set_index("timestamp")
    )
    idx = pd.date_range(raw.index.min(), raw.index.max(), freq="s", tz="UTC")
    bars = raw.reindex(idx)
    bars["close"] = bars["close"].ffill()
    bars["open"] = bars["open"].fillna(bars["close"])
    bars["high"] = bars["high"].fillna(bars["close"])
    bars["low"] = bars["low"].fillna(bars["close"])
    for col in ("volume", "taker_buy_volume", "taker_sell_volume"):
        bars[col] = bars[col].fillna(0.0)
    bars = bars.rename(
        columns={"taker_buy_volume": "buy_qty", "taker_sell_volume": "sell_qty"}
    )
    bars["observed"] = bars.index.isin(raw.index)
    return bars.dropna(subset=["close"])


def load_current_signals(bars: pd.DataFrame, prod_config: Path) -> list[dict]:
    config = json.loads(prod_config.read_text(encoding="utf-8"))
    rows = []
    for cfg in prod_configs_to_second_configs(config):
        if cfg.__class__.__name__ == "SecondNormalConfig":
            signals = generate_normal_signals(bars, cfg)
        else:
            signals = generate_chip_signals(bars, cfg)
        rows.extend(signals)
    accepted, _ = execute_signals(
        rows,
        per_strategy_lock=True,
        cooldown_sec=600,
        use_horizon_as_lock=True,
    )
    return accepted


def metrics(rows: list[dict], start: pd.Timestamp, end: pd.Timestamp) -> dict:
    ordered = sorted(rows, key=lambda row: row["entry_time"])
    n = len(ordered)
    wins = sum(bool(row["won"]) for row in ordered)
    pnl = sum(4 if row["won"] else -5 for row in ordered)
    max_loss = 0
    loss = 0
    for row in ordered:
        if row["won"]:
            loss = 0
        else:
            loss += 1
            max_loss = max(max_loss, loss)
    days = max((end - start).total_seconds() / 86400.0, 1e-12)
    return {
        "trades": n,
        "winRate": round(wins / n * 100, 2) if n else 0.0,
        "pnlU_5u_80pct": round(float(pnl), 2),
        "maxConsecutiveLoss": int(max_loss),
        "tradesPerDay": round(n / days, 2),
        "avgDelaySec": round(float(np.mean([r["delay_sec"] for r in ordered])), 2) if n else 0.0,
        "avgImproveBps": round(float(np.mean([r["improve_bps"] for r in ordered])), 4) if n else 0.0,
    }


def day_metrics(rows: list[dict], start: pd.Timestamp, end: pd.Timestamp) -> dict:
    out = {}
    for day in pd.date_range(start.floor("D"), end.floor("D"), freq="D", tz="UTC"):
        subset = [row for row in rows if day <= row["entry_time"] < day + pd.Timedelta(days=1)]
        if subset:
            out[str(day.date())] = metrics(subset, start, end)
    return out


def base_rows(signals: list[dict], bars: pd.DataFrame) -> list[dict]:
    close = bars["close"].to_numpy(float)
    rows = []
    for row in signals:
        idx = int(row["idx"])
        if idx + 600 >= len(close):
            continue
        signal = row["signal"]
        entry = close[idx]
        settle = close[idx + 600]
        rows.append(
            {
                "strategy_id": row["strategy_id"],
                "signal": signal,
                "signal_time": row["time"],
                "entry_time": bars.index[idx],
                "entry_idx": idx,
                "entry": float(entry),
                "settle": float(settle),
                "won": bool(settle > entry if signal == "UP" else settle < entry),
                "delay_sec": 0,
                "improve_bps": 0.0,
                "entry_reason": "immediate",
            }
        )
    return rows


def dynamic_entry_rows(
    signals: list[dict],
    bars: pd.DataFrame,
    *,
    max_wait_sec: int,
    min_improve_bps: float,
    min_reversal_score: int,
    skip_without_confirm: bool,
) -> list[dict]:
    close = bars["close"].to_numpy(float)
    high = bars["high"].to_numpy(float)
    low = bars["low"].to_numpy(float)
    buy = bars["buy_qty"].to_numpy(float)
    sell = bars["sell_qty"].to_numpy(float)
    ret1 = np.diff(np.log(close), prepend=np.nan)
    flow = buy - sell
    flow5 = pd.Series(flow).rolling(5, min_periods=1).sum().to_numpy(float)
    flow20 = pd.Series(flow).rolling(20, min_periods=1).sum().to_numpy(float)
    abs_ret60 = pd.Series(np.abs(ret1)).rolling(60, min_periods=10).median().to_numpy(float)

    rows = []
    for row in signals:
        idx = int(row["idx"])
        if idx + max_wait_sec + 600 >= len(close):
            continue
        signal = row["signal"]
        start_price = close[idx]
        chosen = None
        fallback = None
        for j in range(idx, idx + max_wait_sec + 1):
            if j < 3:
                continue
            price = low[j] if signal == "UP" else high[j]
            improve_bps = (
                (start_price - price) / start_price * 10000
                if signal == "UP"
                else (price - start_price) / start_price * 10000
            )
            if improve_bps < min_improve_bps:
                continue
            v1 = ret1[j]
            v2 = ret1[j - 1]
            local_vol = max(float(abs_ret60[j]) if np.isfinite(abs_ret60[j]) else 0.0, 1e-8)
            adverse_speed = -v1 / local_vol if signal == "UP" else v1 / local_vol
            prev_adverse_speed = -v2 / local_vol if signal == "UP" else v2 / local_vol
            speed_decay = adverse_speed < prev_adverse_speed
            flow_turn = flow5[j] > flow20[j] * 0.25 if signal == "UP" else flow5[j] < flow20[j] * 0.25
            micro_bounce = close[j] > close[j - 1] if signal == "UP" else close[j] < close[j - 1]
            score = int(speed_decay) + int(flow_turn) + int(micro_bounce)
            fallback = (j, improve_bps, score, "fallback_better_price")
            if score >= min_reversal_score:
                chosen = (j, improve_bps, score, "dynamic_confirm")
                break
        if chosen is None:
            if skip_without_confirm:
                continue
            if fallback is None:
                chosen = (idx + max_wait_sec, 0.0, 0, "deadline")
            else:
                chosen = fallback
        entry_idx, improve_bps, score, reason = chosen
        entry = low[entry_idx] if signal == "UP" else high[entry_idx]
        settle = close[entry_idx + 600]
        rows.append(
            {
                "strategy_id": row["strategy_id"],
                "signal": signal,
                "signal_time": row["time"],
                "entry_time": bars.index[entry_idx],
                "entry_idx": int(entry_idx),
                "entry": float(entry),
                "settle": float(settle),
                "won": bool(settle > entry if signal == "UP" else settle < entry),
                "delay_sec": int(entry_idx - idx),
                "improve_bps": round(float(improve_bps), 6),
                "entry_score": int(score),
                "entry_reason": reason,
            }
        )
    return rows


def build_report(args: argparse.Namespace) -> dict:
    bars = load_bars(Path(args.old_csv), Path(args.shard_dir))
    signals = load_current_signals(bars, Path(args.prod_config))
    start = bars.index.min()
    end = bars.index.max()
    baseline = base_rows(signals, bars)
    cases = {
        "immediate": baseline,
    }
    for wait in (20, 30, 45, 60, 90):
        for improve_bps in (1.0, 2.0, 3.0):
            for score in (1, 2, 3):
                key = f"wait{wait}_bps{improve_bps:g}_score{score}_skip"
                cases[key] = dynamic_entry_rows(
                    signals,
                    bars,
                    max_wait_sec=wait,
                    min_improve_bps=improve_bps,
                    min_reversal_score=score,
                    skip_without_confirm=True,
                )
    summaries = {
        name: {
            "all": metrics(rows, start, end),
            "byDay": day_metrics(rows, start, end),
        }
        for name, rows in cases.items()
    }
    ranked = sorted(
        (
            {"case": name, **section["all"]}
            for name, section in summaries.items()
        ),
        key=lambda item: (
            item["pnlU_5u_80pct"],
            item["winRate"],
            -item["maxConsecutiveLoss"],
        ),
        reverse=True,
    )
    return {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "sample": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "hours": round((end - start).total_seconds() / 3600.0, 2),
            "rows": int(len(bars)),
            "observedPct": round(float(bars["observed"].mean() * 100), 2),
            "signals": len(signals),
        },
        "method": {
            "causal": "Entry features use only seconds from signal time to candidate entry time; settlement is entry+600s.",
            "dynamicEntry": "For UP waits for a lower price and fading sell efficiency; for DOWN waits for a higher price and fading buy efficiency.",
            "payout": "5U stake, 80% 10m payout.",
        },
        "ranked": ranked,
        "summaries": summaries,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-csv", default=str(DEFAULT_OLD_CSV))
    parser.add_argument("--shard-dir", default=str(DEFAULT_SHARD_DIR))
    parser.add_argument("--prod-config", default=str(DEFAULT_PROD_CONFIG))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = build_report(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["sample"], ensure_ascii=False))
    for item in report["ranked"][:15]:
        print(json.dumps(item, ensure_ascii=False))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
