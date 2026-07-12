from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from research_normal_liquidity_orderbook import load_local_data  # noqa: E402
import research_regime_ob_strategy_backtest as base  # noqa: E402


OUT_JSON = ROOT / "tmp" / "detailed_trend_state_research.json"
OUT_TRADES = ROOT / "tmp" / "detailed_trend_state_trades.csv"


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def payout(won: bool) -> int:
    return 4 if bool(won) else -5


def max_drawdown(pnls: list[int]) -> tuple[int, int]:
    equity = peak = drawdown = loss_streak = max_loss = 0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        if pnl < 0:
            loss_streak += 1
            max_loss = max(max_loss, loss_streak)
        else:
            loss_streak = 0
    return int(drawdown), int(max_loss)


def metrics(rows: pd.DataFrame, hours: float) -> dict[str, Any]:
    if rows.empty:
        return {
            "trades": 0,
            "wins": 0,
            "winRate": 0.0,
            "pnlU": 0,
            "maxDrawdownU": 0,
            "maxLoss": 0,
            "tradesPerDay": 0.0,
        }
    ordered = rows.sort_values("time")
    pnls = [payout(bool(won)) for won in ordered["won"]]
    drawdown, max_loss = max_drawdown(pnls)
    wins = int(ordered["won"].astype(bool).sum())
    sign = ordered["signal"].map({"UP": 1.0, "DOWN": -1.0}).astype(float)
    signed_usdt = sign * (ordered["settle"].astype(float) - ordered["entry"].astype(float))
    return {
        "trades": int(len(ordered)),
        "wins": wins,
        "winRate": round(wins / len(ordered) * 100.0, 2),
        "pnlU": int(sum(pnls)),
        "maxDrawdownU": drawdown,
        "maxLoss": max_loss,
        "tradesPerDay": round(len(ordered) / hours * 24.0, 2) if hours > 0 else 0.0,
        "medianSignedUSDT": round(float(signed_usdt.median()), 2),
        "meanSignedUSDT": round(float(signed_usdt.mean()), 2),
    }


def discover_sources() -> list[dict[str, Path | str]]:
    sources: list[dict[str, Path | str]] = []
    seen: set[tuple[int, int]] = set()
    for seconds in ROOT.glob("tmp/**/btcusdt_1s_trades.csv"):
        orderbook = seconds.parent / "btcusdt_orderbook_1s.csv"
        if not orderbook.exists() or seconds.stat().st_size < 5_000_000:
            continue
        sig = (seconds.stat().st_size, orderbook.stat().st_size)
        # Keep one copy of duplicated pulls such as smoke/audit folders.
        if sig in seen:
            continue
        seen.add(sig)
        sources.append(
            {
                "name": str(seconds.parent.relative_to(ROOT / "tmp")),
                "seconds": seconds,
                "orderbook": orderbook,
            }
        )
    return sorted(sources, key=lambda item: str(item["name"]))


def log_return(close: pd.Series, seconds: int) -> pd.Series:
    return np.log(close / close.shift(seconds)) * 10000.0


def enrich(rows: pd.DataFrame, data: pd.DataFrame) -> pd.DataFrame:
    if rows.empty:
        return rows
    close = data["close"].astype(float)
    high = data["high"].astype(float)
    low = data["low"].astype(float)
    ret = {sec: log_return(close, sec) for sec in (60, 300, 900, 1800, 3600)}
    range15 = (high.rolling(900, min_periods=300).max() / low.rolling(900, min_periods=300).min() - 1.0) * 10000.0
    range60 = (high.rolling(3600, min_periods=1200).max() / low.rolling(3600, min_periods=1200).min() - 1.0) * 10000.0

    out = rows.copy()
    for sec, series in ret.items():
        values = []
        signed = []
        for _, row in out.iterrows():
            idx = int(row["idx"])
            signal_sign = 1.0 if row["signal"] == "UP" else -1.0
            value = float(series.iloc[idx]) if idx < len(series) else float("nan")
            values.append(value)
            signed.append(signal_sign * value)
        out[f"ret{sec}_bps"] = values
        out[f"signed_ret{sec}_bps"] = signed
    out["range15_bps"] = [float(range15.iloc[int(row["idx"])]) for _, row in out.iterrows()]
    out["range60_bps"] = [float(range60.iloc[int(row["idx"])]) for _, row in out.iterrows()]
    out["trend_state"] = [classify_trend(row) for _, row in out.iterrows()]
    out["normal_state"] = [classify_normal(row) for _, row in out.iterrows()]
    out["price_speed_state"] = [classify_speed(row) for _, row in out.iterrows()]
    out["market_case"] = out["trend_state"] + "|" + out["normal_state"]
    sign = out["signal"].map({"UP": 1.0, "DOWN": -1.0}).astype(float)
    out["signed_usdt"] = sign * (out["settle"].astype(float) - out["entry"].astype(float))
    return out


def classify_trend(row: pd.Series) -> str:
    r5 = float(row.get("ret300_bps", np.nan))
    r15 = float(row.get("ret900_bps", np.nan))
    r30 = float(row.get("ret1800_bps", np.nan))
    r60 = float(row.get("ret3600_bps", np.nan))
    rg15 = float(row.get("range15_bps", np.nan))
    rg60 = float(row.get("range60_bps", np.nan))

    if not all(math.isfinite(v) for v in (r5, r15, r30, r60, rg15, rg60)):
        return "unknown"
    if rg15 >= 90.0 or abs(r5) >= 25.0:
        if r5 <= -15.0 or r15 <= -30.0:
            return "shock_down"
        if r5 >= 15.0 or r15 >= 30.0:
            return "shock_up"
        return "shock_chop"
    if r15 <= -12.0 and r30 <= -15.0 and r60 <= -20.0:
        return "stacked_down"
    if r15 >= 12.0 and r30 >= 15.0 and r60 >= 20.0:
        return "stacked_up"
    if r30 <= -18.0 or (r60 <= -25.0 and r15 <= 5.0):
        return "drift_down"
    if r30 >= 18.0 or (r60 >= 25.0 and r15 >= -5.0):
        return "drift_up"
    if abs(r15) <= 10.0 and abs(r30) <= 14.0 and abs(r60) <= 24.0:
        return "balanced_flat"
    if r60 <= -20.0 and r15 >= 8.0:
        return "drop_rebound"
    if r60 >= 20.0 and r15 <= -8.0:
        return "rise_pullback"
    return "mixed_transition"


def classify_normal(row: pd.Series) -> str:
    slope = float(row.get("slope_bps", np.nan))
    z = float(row.get("z", np.nan))
    sigma_expand = float(row.get("sigma_expand", np.nan))
    if not all(math.isfinite(v) for v in (slope, z, sigma_expand)):
        return "normal_unknown"
    if sigma_expand > 1.05:
        expand = "expanding"
    elif sigma_expand < 0.75:
        expand = "compressing"
    else:
        expand = "stable"
    if slope <= -8.0:
        move = "center_down"
    elif slope >= 8.0:
        move = "center_up"
    elif slope <= -4.0:
        move = "center_weak_down"
    elif slope >= 4.0:
        move = "center_weak_up"
    else:
        move = "center_flat"
    if z >= 0.8:
        pos = "upper"
    elif z <= -0.8:
        pos = "lower"
    elif z >= 0:
        pos = "upper_reclaim"
    else:
        pos = "lower_reclaim"
    return f"{move}_{expand}_{pos}"


def classify_speed(row: pd.Series) -> str:
    signed60 = float(row.get("signed_ret60_bps", np.nan))
    if not math.isfinite(signed60):
        return "speed_unknown"
    if signed60 > 10.0:
        return "too_fast_same"
    if signed60 > 5.0:
        return "fast_same"
    if signed60 >= 0.0:
        return "mild_same"
    if signed60 >= -10.0:
        return "mild_opposite"
    return "fast_opposite"


def base_candidates(data: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for row in base.build_candidates(data, 600):
        if row["window"] != 600:
            continue
        if row["regime"] == "mid_flat_fade":
            pass
        elif row["regime"] == "mid_weakup_trend" and row["signal"] == "DOWN":
            pass
        else:
            continue
        if 40 <= pd.Timestamp(row["time"]).tz_convert("Asia/Shanghai").minute <= 59:
            continue
        if not (-0.5 < float(row["support_chg60"]) < 3.0):
            continue
        if float(row["sigma_expand"]) > 1.05:
            continue
        rows.append(row)
    return pd.DataFrame(rows)


def apply_cooldown(rows: pd.DataFrame) -> pd.DataFrame:
    if rows.empty:
        return rows
    accepted: list[dict[str, Any]] = []
    last_time: pd.Timestamp | None = None
    for row in rows.sort_values("time").to_dict("records"):
        timestamp = pd.Timestamp(row["time"])
        if last_time is not None and (timestamp - last_time).total_seconds() < 600:
            continue
        accepted.append(row)
        last_time = timestamp
    return pd.DataFrame(accepted)


def variant_filter(rows: pd.DataFrame, variant: str) -> pd.DataFrame:
    if rows.empty:
        return rows
    if variant == "baseline":
        return rows.copy()
    if variant == "speed_guard":
        return rows[rows["signed_ret60_bps"] <= 5.0].copy()
    if variant == "block_downtrend_up":
        bad_up = rows["signal"].eq("UP") & rows["trend_state"].isin(
            ["stacked_down", "drift_down", "shock_down", "mixed_transition"]
        )
        return rows[~bad_up].copy()
    if variant == "state_v2":
        bad_up = rows["signal"].eq("UP") & rows["trend_state"].isin(
            ["stacked_down", "drift_down", "shock_down", "mixed_transition"]
        )
        bad_down = rows["signal"].eq("DOWN") & rows["trend_state"].isin(["shock_up", "stacked_up"])
        fast_same = rows["signed_ret60_bps"] > 5.0
        unstable_center = rows["normal_state"].str.contains("expanding", na=False)
        return rows[~bad_up & ~bad_down & ~fast_same & ~unstable_center].copy()
    if variant == "state_v2_keep_expansion":
        bad_up = rows["signal"].eq("UP") & rows["trend_state"].isin(
            ["stacked_down", "drift_down", "shock_down", "mixed_transition"]
        )
        bad_down = rows["signal"].eq("DOWN") & rows["trend_state"].isin(["shock_up", "stacked_up"])
        fast_same = rows["signed_ret60_bps"] > 5.0
        return rows[~bad_up & ~bad_down & ~fast_same].copy()
    if variant == "state_v3_quality_cases":
        regime_signal = rows["regime"].astype(str) + "_" + rows["signal"].astype(str)
        center_move = rows["normal_state"].str.extract(r"^(center_(?:weak_)?(?:down|up)|center_flat)")[0]
        good_case = (
            rows["trend_state"].eq("balanced_flat")
            & regime_signal.isin(["mid_flat_fade_DOWN", "mid_flat_fade_UP"])
        )
        good_case |= rows["trend_state"].eq("drop_rebound") & regime_signal.eq("mid_flat_fade_DOWN")
        good_case |= rows["trend_state"].eq("mixed_transition") & regime_signal.eq("mid_weakup_trend_DOWN")
        good_case |= rows["trend_state"].eq("drift_up") & regime_signal.eq("mid_weakup_trend_DOWN")
        good_case |= rows["trend_state"].isin(["shock_down", "shock_up"]) & rows["signal"].eq("DOWN")
        good_case |= rows["trend_state"].eq("rise_pullback") & rows["signal"].eq("UP")
        bad_center = rows["signal"].eq("DOWN") & center_move.eq("center_up")
        bad_center |= rows["signal"].eq("UP") & center_move.eq("center_weak_up")
        bad_speed = rows["price_speed_state"].eq("too_fast_same")
        return rows[good_case & ~bad_center & ~bad_speed].copy()
    if variant == "state_v4_execution_guard":
        v3 = variant_filter(rows, "state_v3_quality_cases")
        center_move = v3["normal_state"].str.extract(r"^(center_(?:weak_)?(?:down|up)|center_flat)")[0]
        up_chase = v3["signal"].eq("UP") & (v3["signed_ret60_bps"] > 0.0)
        up_center_rising = v3["signal"].eq("UP") & center_move.isin(["center_up", "center_weak_up"])
        down_fake_wall = v3["signal"].eq("DOWN") & (
            (v3["support_chg60"] > 1.2)
            | (v3["support_ratio"].fillna(0.0) > 3.0)
        )
        down_extreme_chase = v3["signal"].eq("DOWN") & (v3["signed_ret60_bps"] > 8.0)
        return v3[~up_chase & ~up_center_rising & ~down_fake_wall & ~down_extreme_chase].copy()
    if variant == "state_v5_conservative":
        v4 = variant_filter(rows, "state_v4_execution_guard")
        regime_signal = v4["regime"].astype(str) + "_" + v4["signal"].astype(str)
        keep = (
            v4["trend_state"].eq("balanced_flat")
            & regime_signal.eq("mid_flat_fade_UP")
            & (v4["ret3600_bps"] >= -20.0)
        )
        keep |= v4["trend_state"].eq("mixed_transition") & regime_signal.eq("mid_weakup_trend_DOWN")
        keep |= (
            v4["trend_state"].eq("drop_rebound")
            & regime_signal.eq("mid_flat_fade_DOWN")
            & (v4["z"] >= 1.2)
        )
        keep |= v4["trend_state"].eq("shock_up") & regime_signal.eq("mid_flat_fade_DOWN")
        return v4[keep].copy()
    raise ValueError(f"unknown variant: {variant}")


def split_summary(rows: pd.DataFrame, hours: float, key: str) -> dict[str, Any]:
    if rows.empty or key not in rows:
        return {}
    return {
        str(name): metrics(group, hours)
        for name, group in rows.groupby(key, sort=True)
    }


def run() -> dict[str, Any]:
    reports = []
    all_rows = []
    for source in discover_sources():
        data = load_local_data(Path(source["seconds"]), Path(source["orderbook"]))
        if len(data) < 7200:
            continue
        hours = (data.index.max() - data.index.min()).total_seconds() / 3600.0
        candidates = enrich(base_candidates(data), data)
        source_report = {
            "source": source["name"],
            "start": data.index.min(),
            "end": data.index.max(),
            "hours": round(hours, 4),
            "rows": len(data),
            "variants": {},
            "by_trend_state_baseline": {},
            "by_regime_signal_baseline": {},
        }
        for variant in (
            "baseline",
            "speed_guard",
            "block_downtrend_up",
            "state_v2_keep_expansion",
            "state_v2",
            "state_v3_quality_cases",
            "state_v4_execution_guard",
            "state_v5_conservative",
        ):
            trades = apply_cooldown(variant_filter(candidates, variant))
            trades["variant"] = variant
            trades["source"] = source["name"]
            if not trades.empty:
                trades["regime_signal"] = trades["regime"].astype(str) + "_" + trades["signal"].astype(str)
            source_report["variants"][variant] = metrics(trades, hours)
            if variant == "baseline":
                source_report["by_trend_state_baseline"] = split_summary(trades, hours, "trend_state")
                source_report["by_regime_signal_baseline"] = split_summary(trades, hours, "regime_signal")
            all_rows.append(trades)
        reports.append(source_report)

    trades_out = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
    if not trades_out.empty:
        trades_out["regime_signal"] = trades_out["regime"].astype(str) + "_" + trades_out["signal"].astype(str)
        trades_out.to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    output = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "rules": {
            "trend_state": {
                "balanced_flat": "15m/30m/60m returns all close to flat",
                "stacked_down/up": "15m, 30m and 60m align in the same direction",
                "drift_down/up": "30m or 60m has directional drift but not full alignment",
                "shock_down/up": "15m range >=90bp or 5m impulse >=25bp",
                "drop_rebound/rise_pullback": "longer trend and 15m move conflict after a sharp leg",
                "mixed_transition": "state is neither flat nor clean trend",
            },
            "variants": {
                "baseline": "previous stable candidate before detailed state filter",
                "speed_guard": "baseline + skip signed_ret60 > 5bp",
                "block_downtrend_up": "skip UP in stacked/drift/shock down or mixed transition",
                "state_v2_keep_expansion": "block bad trend-side trades + speed guard",
                "state_v2": "state_v2_keep_expansion + skip expanding normal band",
                "state_v3_quality_cases": "only keep detailed cases that stayed positive across sources: true-flat reversion, drop-rebound short, weak-up/transition short, shock short, rise-pullback long; then remove center-up shorts and too-fast same-direction entries",
                "state_v4_execution_guard": "state_v3 + execution guards: UP must not chase a positive 60s move/rising center; DOWN skips sudden thick same-side wall and extreme same-direction speed",
                "state_v5_conservative": "state_v4 conservative subset: balanced-flat lower reclaim long, mixed-transition weak-up short, drop-rebound upper short with z>=1.2, and shock-up upper short",
            },
        },
        "reports": reports,
        "tradesCsv": str(OUT_TRADES),
    }
    OUT_JSON.write_text(json.dumps(clean(output), ensure_ascii=False, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    result = run()
    print(json.dumps(clean(result), ensure_ascii=False, indent=2))
