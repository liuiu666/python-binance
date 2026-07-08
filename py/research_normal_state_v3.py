from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_normal_state_v1 as v1
import research_yellow_revert_filters as yellow


OUT_JSON = ROOT / "tmp" / "normal_state_v3_research.json"
OUT_CSV = ROOT / "tmp" / "normal_state_v3_scan.csv"
OUT_TRADES = ROOT / "tmp" / "normal_state_v3_selected_trades.csv"

HORIZON_SEC = 600
WIN_PAY = 0.8
LOSS_PAY = -1.0
BREAKEVEN_WR = abs(LOSS_PAY) / (WIN_PAY + abs(LOSS_PAY)) * 100.0


@dataclass(frozen=True)
class V3Config:
    name: str
    lookback_min: int
    reentry_z: float
    max_outside_sec: int
    side_mode: str
    min_score_ratio: float
    min_width_ratio: float
    max_width_ratio: float
    max_slope_side_bps: float
    max_bandwalk10: float
    max_half_life_min: float
    max_flow60_side: float
    max_ob_imb_side: float
    max_ob_micro_side: float
    max_peak_abs_z: float
    cooldown_sec: int = 600


def _unique_existing(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for path in paths:
        if not path.exists() or path.stat().st_size <= 128:
            continue
        key = str(path.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return sorted(out, key=lambda p: (p.stat().st_mtime, str(p)))


def discover_second_sources() -> list[Path]:
    paths = list(yellow.discover_sources())
    patterns = [
        "tmp/latest_data_pull_*/second/BTCUSDT/futures/*.csv",
        "tmp/latest_data_pull_*/*/second/BTCUSDT/futures/*.csv",
        "tmp/normal_multiday_20260630/second/*.csv",
    ]
    for pattern in patterns:
        paths.extend(ROOT.glob(pattern))
    return _unique_existing(paths)


def load_merged_bars_v3() -> tuple[pd.DataFrame, list[dict]]:
    frames: list[pd.DataFrame] = []
    source_info: list[dict] = []
    for rank, path in enumerate(discover_second_sources()):
        try:
            part = yellow.read_source(path)
        except Exception as exc:
            source_info.append({"file": str(path), "error": str(exc)})
            continue
        if part.empty:
            continue
        per = (
            part.groupby("second")
            .agg(
                close=("price", "last"),
                volume=("volume", "sum"),
                buy_qty=("buy_qty", "sum"),
                sell_qty=("sell_qty", "sum"),
            )
            .reset_index()
        )
        per["source_rank"] = rank
        frames.append(per)
        source_info.append(
            {
                "file": str(path),
                "rows": int(len(part)),
                "seconds": int(len(per)),
                "first": part["second"].min().isoformat() if len(part) else None,
                "last": part["second"].max().isoformat() if len(part) else None,
            }
        )
    if not frames:
        raise RuntimeError("no second data sources found")
    raw = pd.concat(frames, ignore_index=True).sort_values(["second", "source_rank"])
    agg = raw.groupby("second").agg(
        close=("close", "last"),
        volume=("volume", "last"),
        buy_qty=("buy_qty", "last"),
        sell_qty=("sell_qty", "last"),
    )
    agg["observed"] = True
    idx = pd.date_range(agg.index.min(), agg.index.max(), freq="s", tz="UTC")
    bars = agg.reindex(idx)
    bars["observed"] = bars["observed"].fillna(False).astype(bool)
    bars["close"] = bars["close"].ffill()
    for col in ("volume", "buy_qty", "sell_qty"):
        bars[col] = bars[col].fillna(0.0)
    bars = bars.dropna(subset=["close"])
    bars.index.name = "time"
    return bars, source_info


def _num_col(df: pd.DataFrame, name: str, default: float = np.nan) -> pd.Series:
    if name not in df.columns:
        return pd.Series(default, index=df.index, dtype=float)
    return pd.to_numeric(df[name], errors="coerce")


def discover_orderbook_sources() -> list[Path]:
    patterns = [
        "tmp/latest_market_pull_*/btcusdt_orderbook_1s.csv",
        "tmp/latest_data_pull_*/btcusdt_orderbook_1s.csv",
        "tmp/latest_data_pull_*/*/btcusdt_orderbook_1s.csv",
        "tmp/latest_pull_*/btcusdt_orderbook_1s.csv",
        "tmp/latest_pull_*/data/btcusdt_orderbook_1s.csv",
        "tmp/latest_smart_test_pull_*/data/btcusdt_orderbook_1s.csv",
        "tmp/normal_research_latest_*/btcusdt_orderbook_1s.csv",
        "tmp/*/orderbook/BTCUSDT/futures/*.csv",
    ]
    paths: list[Path] = []
    for pattern in patterns:
        paths.extend(ROOT.glob(pattern))
    return _unique_existing(paths)


def load_orderbook_features_v3(second_index: pd.DatetimeIndex) -> tuple[pd.DataFrame, list[dict]]:
    out = pd.DataFrame(index=second_index)
    out["ob_available"] = False
    out["ob_imb20"] = np.nan
    out["ob_micro_bps"] = np.nan
    out["ob_spread_bps"] = np.nan
    frames: list[pd.DataFrame] = []
    source_info: list[dict] = []
    for rank, path in enumerate(discover_orderbook_sources()):
        try:
            df = pd.read_csv(path)
            ts = v1.parse_time_series(df).dt.floor("s")
        except Exception as exc:
            source_info.append({"file": str(path), "error": str(exc)})
            continue
        if df.empty:
            continue
        per = pd.DataFrame(
            {
                "time": ts,
                "ob_imb20": _num_col(df, "imbalance_20"),
                "ob_micro_bps": _num_col(df, "microprice_edge_bps"),
                "ob_spread_bps": _num_col(df, "spread_bps"),
                "ob_available": True,
                "source_rank": rank,
            }
        ).dropna(subset=["time"])
        per = per.drop_duplicates("time", keep="last")
        frames.append(per)
        source_info.append(
            {
                "file": str(path),
                "rows": int(len(df)),
                "seconds": int(len(per)),
                "first": per["time"].min().isoformat() if len(per) else None,
                "last": per["time"].max().isoformat() if len(per) else None,
            }
        )
    if not frames:
        out["orderbook_sources"] = ""
        return out, source_info
    raw = pd.concat(frames, ignore_index=True).sort_values(["time", "source_rank"])
    merged = raw.groupby("time").agg(
        ob_imb20=("ob_imb20", "last"),
        ob_micro_bps=("ob_micro_bps", "last"),
        ob_spread_bps=("ob_spread_bps", "last"),
        ob_available=("ob_available", "last"),
    )
    aligned = merged.reindex(second_index, method="ffill", limit=5)
    for col in ("ob_imb20", "ob_micro_bps", "ob_spread_bps"):
        out[col] = aligned[col]
    out["ob_available"] = aligned["ob_available"].fillna(False).astype(bool)
    out["orderbook_sources"] = str(len(source_info))
    return out, source_info


def summarize(rows: list[dict]) -> dict:
    return v1.summarize(rows)


def _finite_float(value: object) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return out if np.isfinite(out) else float("nan")


def _vote(score: dict, ok: bool, reason: str) -> None:
    score["total"] += 1
    if ok:
        score["ok"] += 1
    else:
        score["failed"].append(reason)


def config_allows_v3(row: dict, cfg: V3Config) -> tuple[bool, dict]:
    side = _finite_float(row.get("breakout_side"))
    if not np.isfinite(side):
        return False, {"score_ratio": 0.0, "failed": ["bad_side"]}
    if cfg.side_mode == "upper_only" and side <= 0:
        return False, {"score_ratio": 0.0, "failed": ["lower_disabled"]}
    if cfg.side_mode == "lower_only" and side >= 0:
        return False, {"score_ratio": 0.0, "failed": ["upper_disabled"]}
    outside_sec = int(row.get("outside_sec", 10**9))
    peak_abs_z = _finite_float(row.get("peak_abs_z"))
    if outside_sec > cfg.max_outside_sec:
        return False, {"score_ratio": 0.0, "failed": ["late_reentry"]}
    if not np.isfinite(peak_abs_z) or peak_abs_z > cfg.max_peak_abs_z:
        return False, {"score_ratio": 0.0, "failed": ["tail_too_far"]}

    width = _finite_float(row.get("m_width_ratio"))
    slope = _finite_float(row.get("m_slope60_bps"))
    bandwalk = _finite_float(row.get("m_bandwalk10"))
    cover = _finite_float(row.get("m_cover2_120"))
    half_life = _finite_float(row.get("m_half_life_min"))
    sigma10 = _finite_float(row.get("sigma10_bps"))
    flow60 = _finite_float(row.get("flow60"))
    if not all(np.isfinite(x) for x in (width, slope, bandwalk, cover, half_life, sigma10, flow60)):
        return False, {"score_ratio": 0.0, "failed": ["feature_nan"]}

    side_slope = side * slope
    side_flow = side * flow60
    ob_available = bool(row.get("ob_available"))
    ob_imb = _finite_float(row.get("ob_imb20"))
    ob_micro = _finite_float(row.get("ob_micro_bps"))

    # Hard risk controls: do not fade into obvious continuation pressure.
    if width < cfg.min_width_ratio * 0.85:
        return False, {"score_ratio": 0.0, "failed": ["squeeze_risk"]}
    if side_slope > cfg.max_slope_side_bps:
        return False, {"score_ratio": 0.0, "failed": ["slope_continuation"]}
    if bandwalk > cfg.max_bandwalk10:
        return False, {"score_ratio": 0.0, "failed": ["bandwalk_continuation"]}
    if side_flow > cfg.max_flow60_side * 1.8:
        return False, {"score_ratio": 0.0, "failed": ["flow_continuation"]}
    if ob_available and np.isfinite(ob_imb) and side * ob_imb > cfg.max_ob_imb_side * 2.0:
        return False, {"score_ratio": 0.0, "failed": ["ob_imb_continuation"]}
    if ob_available and np.isfinite(ob_micro) and side * ob_micro > cfg.max_ob_micro_side * 2.0:
        return False, {"score_ratio": 0.0, "failed": ["ob_micro_continuation"]}

    score = {"ok": 0, "total": 0, "failed": []}
    _vote(score, 0.82 <= cover <= 0.99, "empirical_cover")
    _vote(score, cfg.min_width_ratio <= width <= cfg.max_width_ratio, "band_width")
    _vote(score, side_slope <= cfg.max_slope_side_bps, "slope_continuation")
    _vote(score, bandwalk <= cfg.max_bandwalk10, "bandwalk")
    _vote(score, half_life <= cfg.max_half_life_min, "half_life")
    _vote(score, 6.0 <= sigma10 <= 80.0, "sigma10")
    _vote(score, side_flow <= cfg.max_flow60_side, "trade_flow")
    if ob_available and np.isfinite(ob_imb):
        _vote(score, side * ob_imb <= cfg.max_ob_imb_side, "orderbook_imb")
    if ob_available and np.isfinite(ob_micro):
        _vote(score, side * ob_micro <= cfg.max_ob_micro_side, "orderbook_micro")
    ratio = score["ok"] / max(score["total"], 1)
    return ratio >= cfg.min_score_ratio, {
        "score_ratio": round(float(ratio), 4),
        "score_ok": int(score["ok"]),
        "score_total": int(score["total"]),
        "failed": score["failed"],
    }


def apply_v3(candidates: list[dict], cfg: V3Config) -> list[dict]:
    rows: list[dict] = []
    last_signal = -10**9
    for row in candidates:
        idx = int(row["idx"])
        if idx - last_signal < cfg.cooldown_sec:
            continue
        ok, meta = config_allows_v3(row, cfg)
        if not ok:
            continue
        out = dict(row)
        out["strategy"] = cfg.name
        out["v3_score_ratio"] = meta["score_ratio"]
        out["v3_score_ok"] = meta["score_ok"]
        out["v3_score_total"] = meta["score_total"]
        out["v3_failed_votes"] = ",".join(meta["failed"])
        rows.append(out)
        last_signal = idx
    return rows


def split_summary(rows: list[dict]) -> dict:
    df = pd.DataFrame(rows)
    days = sorted(df["day_cn"].unique().tolist()) if not df.empty else []
    return {
        "summary": summarize(rows),
        "train": summarize([r for r in rows if r["day_cn"] <= "2026-06-30"]),
        "recent": summarize([r for r in rows if r["day_cn"] >= "2026-07-01"]),
        "d1": summarize([r for r in rows if r["day_cn"] == "2026-07-01"]),
        "d2": summarize([r for r in rows if r["day_cn"] == "2026-07-02"]),
        "d3": summarize([r for r in rows if r["day_cn"] == "2026-07-03"]),
        "days_present": days,
    }


def build_config_grid() -> list[V3Config]:
    configs: list[V3Config] = []
    for lookback_min in (180,):
        for reentry_z in (1.95, 1.96):
            for max_outside_sec in (15, 30, 60):
                for side_mode in ("upper_only", "both"):
                    for min_score_ratio in (0.72, 0.78, 0.84):
                        for width_pair in ((0.45, 2.2), (0.45, 3.0), (0.60, 2.2)):
                            for max_slope_side_bps in (70, 120):
                                for max_bandwalk10 in (6, 8):
                                    for max_half_life_min in (20, 40, 999):
                                        name = (
                                            f"NSV3_W{lookback_min}_R{reentry_z}_O{max_outside_sec}"
                                            f"_{side_mode}_S{min_score_ratio}_BW{width_pair[0]}-{width_pair[1]}"
                                            f"_SL{max_slope_side_bps}_BWALK{max_bandwalk10}_HL{max_half_life_min}"
                                        )
                                        configs.append(
                                            V3Config(
                                                name=name,
                                                lookback_min=lookback_min,
                                                reentry_z=reentry_z,
                                                max_outside_sec=max_outside_sec,
                                                side_mode=side_mode,
                                                min_score_ratio=min_score_ratio,
                                                min_width_ratio=width_pair[0],
                                                max_width_ratio=width_pair[1],
                                                max_slope_side_bps=max_slope_side_bps,
                                                max_bandwalk10=max_bandwalk10,
                                                max_half_life_min=max_half_life_min,
                                                max_flow60_side=0.10,
                                                max_ob_imb_side=0.10,
                                                max_ob_micro_side=0.001,
                                                max_peak_abs_z=3.2,
                                            )
                                        )
    return configs


def score_scan_row(row: dict) -> float:
    if row["train_n"] < 20 or row["recent_n"] < 8:
        return -9999.0
    if row["train_wr"] < BREAKEVEN_WR or row["recent_wr"] < 52.0:
        return -9999.0
    day_penalty = 0.0
    for key in ("d1_pnl", "d2_pnl", "d3_pnl"):
        day_penalty += max(0.0, -float(row.get(key, 0.0))) * 0.7
    return (
        float(row["train_pnl"]) * 0.7
        + float(row["recent_pnl"]) * 1.8
        + min(float(row["n"]), 180.0) * 0.03
        - abs(float(row["max_dd"])) * 0.35
        - day_penalty
    )


def run() -> dict:
    bars, second_sources = load_merged_bars_v3()
    minute = v1.load_minute_features(bars.index)
    orderbook, orderbook_sources = load_orderbook_features_v3(bars.index)
    features = pd.concat(
        [
            minute.drop(columns=["minute_source"], errors="ignore"),
            orderbook.drop(columns=["orderbook_sources"], errors="ignore"),
        ],
        axis=1,
    )

    configs = build_config_grid()
    wanted_contexts = sorted({cfg.lookback_min for cfg in configs})
    wanted_reentry = sorted({cfg.reentry_z for cfg in configs})
    candidates_by_key: dict[tuple[int, float], list[dict]] = {}
    for lookback_min in wanted_contexts:
        ctx = v1.build_second_context(bars, lookback_min * 60)
        for reentry_z in wanted_reentry:
            candidates_by_key[(lookback_min, reentry_z)] = v1.generate_reversion_rows(
                bars,
                features,
                lookback_sec=lookback_min * 60,
                second_context=ctx,
                reentry_z=reentry_z,
                max_outside_sec=900,
                state_filter="none",
                ob_filter="none",
                cooldown_sec=0,
            )

    scan_rows: list[dict] = []
    selected_rows: list[dict] = []
    selected_cfg: V3Config | None = None
    selected_parts: dict | None = None
    results_by_name: dict[str, tuple[V3Config, dict, list[dict]]] = {}
    for cfg in configs:
        rows = apply_v3(candidates_by_key[(cfg.lookback_min, cfg.reentry_z)], cfg)
        parts = split_summary(rows)
        summary = parts["summary"]
        train = parts["train"]
        recent = parts["recent"]
        d1 = parts["d1"]
        d2 = parts["d2"]
        d3 = parts["d3"]
        row = {
            **asdict(cfg),
            "n": summary["n"],
            "wr": summary["wr"],
            "pnl": summary["pnl"],
            "max_dd": summary["max_dd"],
            "train_n": train["n"],
            "train_wr": train["wr"],
            "train_pnl": train["pnl"],
            "recent_n": recent["n"],
            "recent_wr": recent["wr"],
            "recent_pnl": recent["pnl"],
            "d1_n": d1["n"],
            "d1_wr": d1["wr"],
            "d1_pnl": d1["pnl"],
            "d2_n": d2["n"],
            "d2_wr": d2["wr"],
            "d2_pnl": d2["pnl"],
            "d3_n": d3["n"],
            "d3_wr": d3["wr"],
            "d3_pnl": d3["pnl"],
            "up_n": summary["up"].get("n", 0),
            "up_wr": summary["up"].get("wr", 0.0),
            "down_n": summary["down"].get("n", 0),
            "down_wr": summary["down"].get("wr", 0.0),
        }
        row["score"] = score_scan_row(row)
        scan_rows.append(row)
        results_by_name[cfg.name] = (cfg, parts, rows)

    scan = pd.DataFrame(scan_rows).sort_values(["score", "recent_pnl", "train_pnl", "n"], ascending=[False, False, False, False])
    if not scan.empty:
        selected_cfg, selected_parts, selected_rows = results_by_name[str(scan.iloc[0]["name"])]
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    scan.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    if selected_rows:
        pd.DataFrame(selected_rows).to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")
    else:
        pd.DataFrame().to_csv(OUT_TRADES, index=False, encoding="utf-8-sig")

    report = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "data": {
            "rows_dense": int(len(bars)),
            "rows_observed": int(bars["observed"].sum()),
            "observed_pct": round(float(bars["observed"].mean() * 100.0), 4),
            "first": bars.index.min().isoformat(),
            "last": bars.index.max().isoformat(),
            "second_sources": second_sources,
            "minute_source": minute["minute_source"].iloc[0] if "minute_source" in minute else "",
            "orderbook_sources": orderbook_sources,
        },
        "rule": (
            "Normal-state V3: treat band tags as continuation first; only fade a fast false breakout "
            "after re-entry, and require rolling normal-state votes from empirical coverage, band width, "
            "bandwalk, slope, half-life, trade flow, and order book pressure."
        ),
        "payoff": {"win": WIN_PAY, "loss": LOSS_PAY, "breakeven_wr_pct": round(BREAKEVEN_WR, 2)},
        "selected_config": asdict(selected_cfg) if selected_cfg else None,
        "selected": selected_parts,
        "selected_sample": selected_rows[-40:],
        "top": scan.head(40).to_dict("records"),
        "outputs": {
            "json": str(OUT_JSON),
            "scan_csv": str(OUT_CSV),
            "selected_trades_csv": str(OUT_TRADES),
        },
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    result = run()
    print(
        json.dumps(
            {
                "data": {k: result["data"][k] for k in ("rows_dense", "rows_observed", "observed_pct", "first", "last")},
                "selected_config": result["selected_config"],
                "selected": result["selected"],
                "top": result["top"][:15],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
