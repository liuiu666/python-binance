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
    settle_signal,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OLD_CSV = ROOT / "tmp" / "latest_server_recheck_20260618_015135" / "btcusdt_1s_trades.csv"
DEFAULT_SHARD_DIR = ROOT / "tmp" / "latest_second_pull_20260620_131022" / "data" / "second" / "BTCUSDT" / "futures"
DEFAULT_PROD_CONFIG = ROOT / "tmp" / "latest_second_pull_20260620_131022" / "data" / "prod_config.json"
DEFAULT_OUT = ROOT / "tmp" / "smart_switch_research_latest.json"


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
        raise FileNotFoundError("no second csv files found")

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


def metrics(rows: list[dict], start: pd.Timestamp, end: pd.Timestamp) -> dict:
    ordered = sorted(rows, key=lambda row: row["time"])
    n = len(ordered)
    wins = sum(bool(row["won"]) for row in ordered)
    pnl = sum(4 if row["won"] else -5 for row in ordered)
    max_loss = 0
    current_loss = 0
    for row in ordered:
        if row["won"]:
            current_loss = 0
        else:
            current_loss += 1
            max_loss = max(max_loss, current_loss)
    days = max((end - start).total_seconds() / 86400.0, 1e-12)
    return {
        "trades": n,
        "winRate": round(wins / n * 100, 2) if n else 0.0,
        "pnlU_5u_80pct": round(float(pnl), 2),
        "maxConsecutiveLoss": int(max_loss),
        "tradesPerDay": round(n / days, 2),
    }


def day_metrics(rows: list[dict], start: pd.Timestamp, end: pd.Timestamp) -> dict:
    out = {}
    for day in pd.date_range(start.floor("D"), end.floor("D"), freq="D", tz="UTC"):
        subset = [row for row in rows if day <= row["time"] < day + pd.Timedelta(days=1)]
        if subset:
            out[str(day.date())] = metrics(subset, start, end)
    return out


def build_features(bars: pd.DataFrame) -> pd.DataFrame:
    close = bars["close"].astype(float)
    volume = bars["volume"].astype(float)
    buy = bars["buy_qty"].astype(float)
    sell = bars["sell_qty"].astype(float)
    feat = pd.DataFrame(index=bars.index)
    for seconds, name in (
        (10800, "r3h"),
        (7200, "r2h"),
        (5400, "r90m"),
        (3600, "r1h"),
        (1800, "r30m"),
        (600, "r10m"),
        (300, "r5m"),
        (180, "r3m"),
        (60, "r1m"),
    ):
        feat[name] = close / close.shift(seconds) - 1.0

    roll_min = close.rolling(7200, min_periods=60).min()
    roll_max = close.rolling(7200, min_periods=60).max()
    roll_mean = close.rolling(7200, min_periods=60).mean()
    feat["pos2h"] = (close - roll_min) / (roll_max - roll_min + 1e-12)
    feat["mean_gap_2h"] = close / roll_mean - 1.0
    feat["flow300"] = (buy - sell).rolling(300, min_periods=1).sum()
    ret1 = np.log(close / close.shift(1)).replace([np.inf, -np.inf], np.nan)
    feat["vol_2h"] = ret1.rolling(7200, min_periods=600).std(ddof=1) * np.sqrt(7200)
    feat["vol_5m"] = ret1.rolling(300, min_periods=60).std(ddof=1) * np.sqrt(300)
    feat["down_z_2h"] = -feat["r2h"] / feat["vol_2h"].clip(lower=1e-12)
    feat["pullback_z_5m"] = feat["r5m"] / feat["vol_5m"].clip(lower=1e-12)
    feat["v60"] = volume.rolling(60, min_periods=1).sum()
    feat["v60_mean_30m"] = feat["v60"].rolling(1800, min_periods=60).mean()
    feat["volume_burst"] = feat["v60"] / feat["v60_mean_30m"].clip(lower=1e-12)
    feat["poc_2h"] = rolling_poc(close.to_numpy(float), 7200, 100.0)
    feat["poc_shift_30m"] = feat["poc_2h"] / feat["poc_2h"].shift(1800) - 1.0
    return feat


def rolling_poc(close: np.ndarray, window: int, bin_size: float) -> pd.Series:
    # Research-only implementation. It is intentionally simple and runs fast enough
    # for a few days of 1-second data.
    out = np.full(len(close), np.nan)
    bins = np.rint(close / bin_size).astype(int)
    offset = int(np.nanmin(bins))
    counts = np.zeros(int(np.nanmax(bins) - offset + 1), dtype=np.int32)
    for i, bucket_raw in enumerate(bins):
        bucket = bucket_raw - offset
        counts[bucket] += 1
        if i >= window:
            counts[bins[i - window] - offset] -= 1
        if i >= window:
            out[i] = (int(np.argmax(counts)) + offset) * bin_size
    return pd.Series(out)


def current_signals(bars: pd.DataFrame, prod_config: Path) -> list[dict]:
    config = json.loads(prod_config.read_text(encoding="utf-8"))
    rows = []
    for cfg in prod_configs_to_second_configs(config):
        if cfg.__class__.__name__ == "SecondNormalConfig":
            signals = generate_normal_signals(bars, cfg)
        else:
            signals = generate_chip_signals(bars, cfg)
        for row in signals:
            row["strategy_id"] = "CUR_" + row["strategy_id"]
            row["origin"] = "current"
        rows.extend(signals)
    accepted, _ = execute_signals(
        rows,
        per_strategy_lock=True,
        cooldown_sec=600,
        use_horizon_as_lock=True,
    )
    return accepted


def make_down_signal(bars: pd.DataFrame, idx: int, strategy_id: str, extra: dict) -> dict:
    row = settle_signal(
        bars=bars,
        idx=idx,
        strategy_id=strategy_id,
        model_type=strategy_id.lower(),
        signal="DOWN",
        horizon_sec=600,
        amount=5,
        extra=extra,
    )
    row["origin"] = "smart"
    return row


def fixed_stable_policy(bars: pd.DataFrame, current: list[dict], feat: pd.DataFrame) -> list[dict]:
    active = (
        ((feat["r2h"] <= -0.004) | (feat["r90m"] <= -0.003))
        & (feat["pos2h"] < 0.6)
        & (feat["r30m"] <= 0.001)
        & (feat["mean_gap_2h"] <= 0)
    ).fillna(False)
    kept = [row for row in current if not bool(active.loc[row["time"]])]
    signals = []
    last = -10**12
    for i, time in enumerate(bars.index[:-600]):
        if i < 7200 or i - last < 600 or not bool(active.iloc[i]):
            continue
        f = feat.iloc[i]
        if f["r5m"] >= 0.001 and f["pos2h"] < 0.4 and f["r30m"] <= 0.001:
            signals.append(
                make_down_signal(
                    bars,
                    i,
                    "SMART_FIXED_DOWN",
                    {
                        "switch_score": 4,
                        "r2h": round(float(f["r2h"]), 6),
                        "r5m": round(float(f["r5m"]), 6),
                        "pos2h": round(float(f["pos2h"]), 6),
                    },
                )
            )
            last = i
    accepted, _ = execute_signals(
        kept + signals,
        per_strategy_lock=True,
        cooldown_sec=600,
        use_horizon_as_lock=True,
    )
    return accepted


def adaptive_auction_policy(
    bars: pd.DataFrame,
    current: list[dict],
    feat: pd.DataFrame,
    *,
    active_score: int = 5,
    entry_score_needed: int = 4,
    down_z: float = 0.9,
    pullback_z: float = 0.55,
) -> list[dict]:
    score = pd.Series(0, index=feat.index, dtype=int)
    score += (feat["down_z_2h"] >= down_z).astype(int)
    score += (feat["pos2h"] < 0.45).astype(int)
    score += (feat["mean_gap_2h"] <= 0).astype(int)
    score += (feat["r30m"] <= 0.001).astype(int)
    score += (feat["poc_shift_30m"] <= 0).astype(int)
    score += (feat["volume_burst"] >= 0.75).astype(int)
    active = (score >= active_score).fillna(False)
    kept = [row for row in current if not bool(active.loc[row["time"]])]
    signals = []
    last = -10**12
    for i, time in enumerate(bars.index[:-600]):
        if i < 7200 or i - last < 600 or not bool(active.iloc[i]):
            continue
        f = feat.iloc[i]
        entry_score = 0
        entry_score += int(f["pullback_z_5m"] >= pullback_z)
        entry_score += int(f["r5m"] >= 0.0008)
        entry_score += int(f["r30m"] <= 0.001)
        entry_score += int(f["pos2h"] < 0.45)
        entry_score += int(f["flow300"] > 0)
        if entry_score >= entry_score_needed:
            signals.append(
                make_down_signal(
                    bars,
                    i,
                    "SMART_ADAPTIVE_AUCTION_DOWN",
                    {
                        "switch_score": int(score.iloc[i]),
                        "entry_score": int(entry_score),
                        "down_z_2h": round(float(f["down_z_2h"]), 4),
                        "pullback_z_5m": round(float(f["pullback_z_5m"]), 4),
                        "poc_shift_30m": round(float(f["poc_shift_30m"]), 6)
                        if pd.notna(f["poc_shift_30m"])
                        else None,
                    },
                )
            )
            last = i
    accepted, _ = execute_signals(
        kept + signals,
        per_strategy_lock=True,
        cooldown_sec=600,
        use_horizon_as_lock=True,
    )
    return accepted


def build_report(args: argparse.Namespace) -> dict:
    bars = load_bars(Path(args.old_csv), Path(args.shard_dir))
    feat = build_features(bars)
    current = current_signals(bars, Path(args.prod_config))
    fixed = fixed_stable_policy(bars, current, feat)
    adaptive = adaptive_auction_policy(bars, current, feat)
    adaptive_grid = []
    for active_score in (5, 6):
        for entry_score in (4, 5):
            for down_z in (0.7, 0.9):
                for pullback_z in (0.45, 0.55):
                    rows = adaptive_auction_policy(
                        bars,
                        current,
                        feat,
                        active_score=active_score,
                        entry_score_needed=entry_score,
                        down_z=down_z,
                        pullback_z=pullback_z,
                    )
                    item = metrics(rows, start=bars.index.min(), end=bars.index.max())
                    item.update(
                        {
                            "activeScore": active_score,
                            "entryScore": entry_score,
                            "downZ": down_z,
                            "pullbackZ": pullback_z,
                        }
                    )
                    adaptive_grid.append(item)
    adaptive_grid.sort(
        key=lambda item: (
            item["pnlU_5u_80pct"],
            item["winRate"],
            -item["maxConsecutiveLoss"],
        ),
        reverse=True,
    )
    start = bars.index.min()
    end = bars.index.max()
    return {
        "generatedAt": pd.Timestamp.now(tz="UTC").isoformat(),
        "sample": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "hours": round((end - start).total_seconds() / 3600.0, 2),
            "rows": int(len(bars)),
            "observedPct": round(float(bars["observed"].mean() * 100), 2),
        },
        "method": {
            "causal": "All features use bars at or before the signal second; settlement uses +600 seconds.",
            "payout": "PnL assumes 5U stake and 80% payout for 10m binary options.",
        },
        "results": {
            "current": {
                "all": metrics(current, start, end),
                "byDay": day_metrics(current, start, end),
            },
            "fixedStableSwitch": {
                "all": metrics(fixed, start, end),
                "byDay": day_metrics(fixed, start, end),
            },
            "adaptiveAuctionSwitch": {
                "all": metrics(adaptive, start, end),
                "byDay": day_metrics(adaptive, start, end),
            },
            "adaptiveAuctionGridTop": adaptive_grid[:20],
        },
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
    for name, section in report["results"].items():
        if isinstance(section, dict) and "all" in section:
            print(name, json.dumps(section["all"], ensure_ascii=False))
        else:
            print(name, json.dumps(section[:5], ensure_ascii=False))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
