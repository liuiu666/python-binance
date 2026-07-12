"""Independent branch-vote + trend-start-skip strategy core.

The live strategy and the backtest both call this module. Keep research-only
training and future-return columns out of the live evaluation path.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from liquidity_v2_core import LiquidityV2Rules, build_features


RULE_SET_BALANCED = [
    ("trend_vol_sprint", ["trend", "volatility", "sprint"], {"min_total": 50, "min_per_source": 8, "min_rate": 60.0}),
    ("trend_vol_pos", ["trend", "volatility", "normal_pos"], {"min_total": 35, "min_per_source": 6, "min_rate": 60.0}),
    ("trend_pos_sprint", ["trend", "normal_pos", "sprint"], {"min_total": 35, "min_per_source": 6, "min_rate": 60.0}),
    ("trend_vol_pos_flow_book", ["trend", "volatility", "normal_pos", "flow", "book"], {"min_total": 25, "min_per_source": 4, "min_rate": 58.0}),
]


@dataclass(frozen=True)
class BranchVoteStartupConfig:
    normal_window_sec: int = 600
    horizon_sec: int = 600
    min_gap_sec: int = 600
    orderbook_max_age_sec: int = 3
    min_votes: int = 2
    startup_skip_threshold: int = 4
    rule_path: str = "data/branch_vote_startup_rules.json"

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "BranchVoteStartupConfig":
        horizon = int(cfg.get("branch_vote_horizon_sec", cfg.get("second_horizon_sec", 600)))
        return cls(
            normal_window_sec=int(cfg.get("branch_vote_normal_window_sec", 600)),
            horizon_sec=horizon,
            min_gap_sec=int(cfg.get("branch_vote_signal_gap_sec", cfg.get("second_min_gap_sec", horizon))),
            orderbook_max_age_sec=int(cfg.get("branch_vote_orderbook_max_age_sec", 3)),
            min_votes=int(cfg.get("branch_vote_min_votes", 2)),
            startup_skip_threshold=int(cfg.get("branch_vote_startup_skip_threshold", 4)),
            rule_path=str(cfg.get("branch_vote_rule_path", "data/branch_vote_startup_rules.json")),
        )

    def normal_rules(self) -> LiquidityV2Rules:
        return LiquidityV2Rules(
            normal_window_sec=self.normal_window_sec,
            horizon_sec=self.horizon_sec,
            min_gap_sec=self.min_gap_sec,
            inside_min=0.45,
            observed_min_pct=88.0,
            center_slope_sec=300,
            center_slope_max_bps=999.0,
            sigma_min_bps=1.0,
            sigma_max_bps=80.0,
            sigma_expand_max=3.0,
            orderbook_max_age_sec=self.orderbook_max_age_sec,
        )


def clean_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): clean_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_json(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def f(value: Any) -> float:
    try:
        number = float(value)
    except Exception:
        return float("nan")
    return number if math.isfinite(number) else float("nan")


def bucket(value: float, cuts: list[tuple[float, str]], default: str) -> str:
    if not math.isfinite(value):
        return "na"
    for limit, label in cuts:
        if value < limit:
            return label
    return default


def trend_bucket(ret10: float, ret30: float, ret60: float) -> str:
    up_votes = int(ret10 >= 8.0) + int(ret30 >= 18.0) + int(ret60 >= 28.0)
    down_votes = int(ret10 <= -8.0) + int(ret30 <= -18.0) + int(ret60 <= -28.0)
    if up_votes >= 2 and down_votes == 0:
        return "trend_up"
    if down_votes >= 2 and up_votes == 0:
        return "trend_down"
    if abs(ret10) <= 7.0 and abs(ret30) <= 18.0 and abs(ret60) <= 28.0:
        return "flat"
    if up_votes > down_votes:
        return "drift_up"
    if down_votes > up_votes:
        return "drift_down"
    return "transition"


def normal_pos(z: float) -> str:
    if not math.isfinite(z):
        return "z_na"
    if z >= 1.2:
        return "above_upper"
    if z >= 0.8:
        return "upper_edge"
    if z >= 0.25:
        return "upper_inside"
    if z > -0.25:
        return "center"
    if z > -0.8:
        return "lower_inside"
    if z > -1.2:
        return "lower_edge"
    return "below_lower"


def sprint_bucket(sign: str, run_len: int, run_move: float) -> str:
    if sign == "UP" and 2 <= run_len <= 4 and 7.0 <= run_move <= 28.0:
        return "up_sprint"
    if sign == "DOWN" and 2 <= run_len <= 4 and -28.0 <= run_move <= -7.0:
        return "down_sprint"
    if sign == "UP" and run_len >= 5:
        return "up_walk"
    if sign == "DOWN" and run_len >= 5:
        return "down_walk"
    return "none"


def side_bucket(value: float, pos: float, neg: float, prefix: str) -> str:
    if not math.isfinite(value):
        return f"{prefix}_na"
    if value >= pos:
        return f"{prefix}_up"
    if value <= neg:
        return f"{prefix}_down"
    return f"{prefix}_neutral"


def _safe_series(data: pd.DataFrame, column: str, default: float = np.nan) -> pd.Series:
    if column in data:
        return data[column].astype(float)
    return pd.Series(default, index=data.index, dtype="float64")


def build_minute_snapshots(
    data: pd.DataFrame,
    source: str,
    cfg: BranchVoteStartupConfig | None = None,
    include_future: bool = False,
) -> pd.DataFrame:
    cfg = cfg or BranchVoteStartupConfig()
    features = build_features(data, cfg.normal_rules())
    work = data.copy()
    for col in ("bid_qty_20", "ask_qty_20", "imbalance_20", "microprice_edge_bps", "spread_bps"):
        if col not in work:
            work[col] = np.nan
    agg = {
        "close": ["first", "max", "min", "last"],
        "volume": "sum",
        "buy_qty": "sum",
        "sell_qty": "sum",
        "bid_qty_20": "mean",
        "ask_qty_20": "mean",
        "imbalance_20": "mean",
        "microprice_edge_bps": "mean",
        "spread_bps": "mean",
    }
    minutes = work.resample("1min").agg(agg)
    minutes.columns = [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "buy_qty",
        "sell_qty",
        "bid20",
        "ask20",
        "imb20",
        "micro",
        "spread",
    ]
    minutes = minutes.dropna(subset=["open", "close"]).copy()
    close = minutes["close"].astype(float)
    for mins in (1, 3, 5, 10, 30, 60):
        minutes[f"ret{mins}_bps"] = (close / close.shift(mins) - 1.0) * 10000.0
    if include_future:
        horizon_min = max(1, int(round(cfg.horizon_sec / 60)))
        minutes["future_bps"] = (close.shift(-horizon_min) / close - 1.0) * 10000.0
    minutes["range10_bps"] = (
        minutes["high"].rolling(10, min_periods=5).max()
        / minutes["low"].rolling(10, min_periods=5).min()
        - 1.0
    ) * 10000.0
    minutes["range30_bps"] = (
        minutes["high"].rolling(30, min_periods=10).max()
        / minutes["low"].rolling(30, min_periods=10).min()
        - 1.0
    ) * 10000.0
    minutes["sigma10_bps"] = close.rolling(10, min_periods=5).std() / close * 10000.0
    minutes["sigma30_bps"] = close.rolling(30, min_periods=10).std() / close * 10000.0
    minutes["vol_ratio30"] = minutes["volume"] / minutes["volume"].rolling(30, min_periods=10).mean()
    flow = (minutes["buy_qty"] - minutes["sell_qty"]) / (
        minutes["buy_qty"] + minutes["sell_qty"]
    ).replace(0, np.nan)
    minutes["flow1"] = flow
    minutes["flow5"] = flow.rolling(5, min_periods=2).mean()
    minutes["bid20_chg5"] = minutes["bid20"] / minutes["bid20"].shift(5).replace(0, np.nan) - 1.0
    minutes["ask20_chg5"] = minutes["ask20"] / minutes["ask20"].shift(5).replace(0, np.nan) - 1.0

    sign = pd.Series("FLAT", index=minutes.index, dtype="object")
    sign[minutes["ret1_bps"] > 1.0] = "UP"
    sign[minutes["ret1_bps"] < -1.0] = "DOWN"
    minutes["minute_sign"] = sign

    run_lengths: list[int] = []
    run_moves: list[float] = []
    current_sign: str | None = None
    current_len = 0
    current_move = 0.0
    for row in minutes.itertuples():
        minute_sign = str(row.minute_sign)
        ret1 = f(row.ret1_bps)
        if not math.isfinite(ret1):
            ret1 = 0.0
        if minute_sign in {"UP", "DOWN"}:
            if minute_sign == current_sign:
                current_len += 1
                current_move += ret1
            else:
                current_sign = minute_sign
                current_len = 1
                current_move = ret1
        else:
            current_sign = None
            current_len = 0
            current_move = 0.0
        run_lengths.append(current_len)
        run_moves.append(current_move)
    minutes["run_len"] = run_lengths
    minutes["run_move_bps"] = run_moves

    feature_rows = []
    for minute_time in minutes.index:
        target = minute_time + pd.Timedelta(seconds=59)
        idx = int(data.index.searchsorted(target, side="right") - 1)
        if idx < 3605 or idx < 0 or abs((data.index[idx] - target).total_seconds()) > 3:
            feature_rows.append({})
            continue
        if include_future and idx >= len(data) - cfg.horizon_sec:
            feature_rows.append({})
            continue
        feature_rows.append(
            {
                "idx": idx,
                "normal_center": f(features["center"].iloc[idx]),
                "normal_sigma": f(features["sigma"].iloc[idx]),
                "normal_low": f(features["normal_low"].iloc[idx]),
                "normal_high": f(features["normal_high"].iloc[idx]),
                "z": f(features["z"].iloc[idx]),
                "inside1_ratio": f(features["inside1_ratio"].iloc[idx]),
                "observed_pct": f(features["observed_pct"].iloc[idx]),
                "center_slope_bps": f(features["center_slope_bps"].iloc[idx]),
                "sigma_bps": f(features["sigma_bps"].iloc[idx]),
                "sigma_expand": f(features["sigma_expand"].iloc[idx]),
            }
        )
    extra = pd.DataFrame(feature_rows, index=minutes.index)
    minutes = pd.concat([minutes, extra], axis=1)

    required = [
        "ret10_bps",
        "ret30_bps",
        "ret60_bps",
        "range10_bps",
        "range30_bps",
        "sigma10_bps",
        "vol_ratio30",
        "flow1",
        "flow5",
        "imb20",
        "micro",
        "z",
        "sigma_bps",
        "sigma_expand",
    ]
    if include_future:
        required.append("future_bps")
    minutes = minutes.replace([np.inf, -np.inf], np.nan).dropna(subset=required)

    out = pd.DataFrame(index=minutes.index)
    out["source"] = source
    out["time"] = minutes.index
    out["time_shanghai"] = [ts.tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S") for ts in minutes.index]
    out["price"] = minutes["close"]
    if include_future:
        out["future_bps"] = minutes["future_bps"]
        out["future10_bps"] = minutes["future_bps"]
        out["up_win"] = minutes["future_bps"] > 0
        out["down_win"] = minutes["future_bps"] < 0
    out["trend"] = [trend_bucket(f(row.ret10_bps), f(row.ret30_bps), f(row.ret60_bps)) for row in minutes.itertuples()]
    out["volatility"] = [
        bucket(f(value), [(3.0, "sigma_low"), (5.0, "sigma_midlow"), (8.0, "sigma_mid"), (12.0, "sigma_high")], "sigma_extreme")
        for value in minutes["sigma10_bps"]
    ]
    out["range"] = [
        bucket(f(value), [(16.0, "range_tight"), (30.0, "range_normal"), (45.0, "range_wide"), (70.0, "range_hot")], "range_extreme")
        for value in minutes["range10_bps"]
    ]
    out["normal_pos"] = [normal_pos(f(value)) for value in minutes["z"]]
    out["normal_quality"] = np.where(
        (minutes["inside1_ratio"] >= 0.45)
        & (minutes["observed_pct"] >= 88.0)
        & (minutes["sigma_expand"] <= 1.25),
        "normal_ready",
        "normal_weak",
    )
    out["sprint"] = [sprint_bucket(str(row.minute_sign), int(row.run_len), f(row.run_move_bps)) for row in minutes.itertuples()]
    out["flow"] = [side_bucket(f(value), 0.12, -0.12, "flow") for value in minutes["flow5"]]
    out["book"] = [side_bucket(f(value), 0.08, -0.08, "book") for value in minutes["imb20"]]
    out["volume"] = [
        bucket(f(value), [(0.7, "vol_low"), (1.25, "vol_normal"), (1.8, "vol_high")], "vol_extreme")
        for value in minutes["vol_ratio30"]
    ]
    out["branch"] = (
        out["trend"]
        + "|"
        + out["volatility"]
        + "|"
        + out["range"]
        + "|"
        + out["normal_quality"]
        + "|"
        + out["normal_pos"]
        + "|"
        + out["sprint"]
        + "|"
        + out["flow"]
        + "|"
        + out["book"]
        + "|"
        + out["volume"]
    )
    out["market_state"] = out["trend"] + "|" + out["volatility"] + "|" + out["normal_pos"] + "|" + out["sprint"]
    out["prev_market_state"] = out["market_state"].shift(1)
    out["transition"] = out["prev_market_state"] + "=>" + out["market_state"]

    for col in (
        "ret10_bps",
        "ret30_bps",
        "ret60_bps",
        "range10_bps",
        "range30_bps",
        "sigma10_bps",
        "vol_ratio30",
        "flow5",
        "imb20",
        "normal_center",
        "normal_sigma",
        "normal_low",
        "normal_high",
        "z",
        "inside1_ratio",
        "observed_pct",
        "center_slope_bps",
        "sigma_bps",
        "sigma_expand",
    ):
        out[col] = minutes[col]
    out = add_micro_features(data, out)
    return out.dropna(subset=["transition"])


def add_lag_features(snapshots: pd.DataFrame) -> pd.DataFrame:
    out = snapshots.sort_values(["source", "time"]).copy()
    for lag in (3, 5, 10):
        for col in ("normal_pos", "ret10_bps", "z", "sigma_expand", "vol_ratio30"):
            out[f"lag{lag}_{col}"] = out.groupby("source")[col].shift(lag)
    return out


def add_micro_features(data: pd.DataFrame, snapshots: pd.DataFrame) -> pd.DataFrame:
    out = snapshots.copy()
    close = data["close"].astype(float)
    buy = _safe_series(data, "buy_qty", 0.0).clip(lower=0.0)
    sell = _safe_series(data, "sell_qty", 0.0).clip(lower=0.0)
    bid20 = _safe_series(data, "bid_qty_20")
    ask20 = _safe_series(data, "ask_qty_20")
    imb20 = _safe_series(data, "imbalance_20")
    rows: list[dict[str, float]] = []
    for timestamp in pd.to_datetime(out["time"], utc=True):
        target = timestamp + pd.Timedelta(seconds=59)
        idx = int(data.index.searchsorted(target, side="right") - 1)
        if idx < 120 or idx < 0 or abs((data.index[idx] - target).total_seconds()) > 3:
            rows.append({})
            continue
        previous_bid = f(bid20.iloc[idx - 60])
        buy30 = float(buy.iloc[idx - 29 : idx + 1].sum())
        sell30 = float(sell.iloc[idx - 29 : idx + 1].sum())
        prev_buy30 = float(buy.iloc[idx - 59 : idx - 29].sum())
        prev_sell30 = float(sell.iloc[idx - 59 : idx - 29].sum())
        flow30 = (buy30 - sell30) / (buy30 + sell30) if buy30 + sell30 > 0 else np.nan
        prev_flow30 = (prev_buy30 - prev_sell30) / (prev_buy30 + prev_sell30) if prev_buy30 + prev_sell30 > 0 else np.nan
        rows.append(
            {
                "sec_ret30_bps": float((close.iloc[idx] / close.iloc[idx - 30] - 1.0) * 10000.0),
                "sec_ret60_bps": float((close.iloc[idx] / close.iloc[idx - 60] - 1.0) * 10000.0),
                "bid20_chg60": float(bid20.iloc[idx] / previous_bid - 1.0) if previous_bid > 0 else np.nan,
                "imb20_now": f(imb20.iloc[idx]),
                "ret15_bps": float((close.iloc[idx] / close.iloc[idx - 15] - 1.0) * 10000.0),
                "ret30_bps_sec": float((close.iloc[idx] / close.iloc[idx - 30] - 1.0) * 10000.0),
                "prev30_bps_sec": float((close.iloc[idx - 30] / close.iloc[idx - 60] - 1.0) * 10000.0),
                "flow30_now": float(flow30),
                "flow30_prev": float(prev_flow30),
                "flow30_delta": float(flow30 - prev_flow30) if math.isfinite(flow30) and math.isfinite(prev_flow30) else np.nan,
                "bid_ask20_ratio": float(bid20.iloc[idx] / ask20.iloc[idx]) if f(ask20.iloc[idx]) > 0 else np.nan,
            }
        )
    return pd.concat([out.reset_index(drop=True), pd.DataFrame(rows)], axis=1)


def summarize_direction(group: pd.DataFrame) -> tuple[str, float, int]:
    n = len(group)
    up_wins = int(group["up_win"].sum())
    down_wins = int(group["down_win"].sum())
    up_rate = up_wins / n * 100.0 if n else 0.0
    down_rate = down_wins / n * 100.0 if n else 0.0
    if up_rate >= down_rate:
        return "UP", up_rate, up_wins * 4 - (n - up_wins) * 5
    return "DOWN", down_rate, down_wins * 4 - (n - down_wins) * 5


def _group_key(name: Any) -> str:
    if isinstance(name, tuple):
        return "|".join(str(item) for item in name)
    return str(name)


def select_rules(train: pd.DataFrame, keys: list[str], min_total: int, min_per_source: int, min_rate: float) -> dict[str, dict[str, Any]]:
    rules: dict[str, dict[str, Any]] = {}
    for name, group in train.groupby(keys):
        key = _group_key(name)
        if len(group) < min_total:
            continue
        signal, rate, pnl = summarize_direction(group)
        if rate < min_rate or pnl <= 0:
            continue
        source_stats = {}
        ok = True
        for source_name, source_group in group.groupby("source"):
            source_signal, source_rate, source_pnl = summarize_direction(source_group)
            source_stats[str(source_name)] = {
                "n": int(len(source_group)),
                "signal": source_signal,
                "rate": round(source_rate, 2),
                "pnl": int(source_pnl),
            }
            if len(source_group) < min_per_source or source_signal != signal or source_pnl < 0:
                ok = False
        if ok:
            rules[key] = {
                "signal": signal,
                "trainSamples": int(len(group)),
                "trainWinRate": round(rate, 2),
                "trainPnlU": int(pnl),
                "sourceStats": source_stats,
            }
    return rules


def compile_rules(train: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {"layer": layer_name, "keys": keys, "rules": select_rules(train, keys, **params)}
        for layer_name, keys, params in RULE_SET_BALANCED
    ]


def save_rules(path: str | Path, compiled: list[dict[str, Any]], metadata: dict[str, Any] | None = None) -> None:
    payload = {"version": 1, "ruleSet": "balanced", "layers": compiled, "metadata": metadata or {}}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(clean_json(payload), ensure_ascii=False, indent=2), encoding="utf-8")


def load_rules(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    layers = payload.get("layers", payload)
    if not isinstance(layers, list):
        raise ValueError(f"invalid branch-vote rules: {path}")
    return layers


def vote_for(row: dict[str, Any], compiled: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any]]:
    votes = []
    for layer in compiled:
        key = "|".join(str(row.get(column)) for column in layer["keys"])
        rule = (layer.get("rules") or {}).get(key)
        if rule is None:
            continue
        votes.append({"layer": layer["layer"], "key": key, "signal": rule["signal"], "rate": rule.get("trainWinRate")})
    up = sum(1 for vote in votes if vote["signal"] == "UP")
    down = sum(1 for vote in votes if vote["signal"] == "DOWN")
    if up == down:
        return None, {"votes": votes, "upVotes": up, "downVotes": down}
    return ("UP" if up > down else "DOWN"), {"votes": votes, "upVotes": up, "downVotes": down}


def enough(values: list[bool], count: int) -> bool:
    return sum(1 for value in values if bool(value)) >= count


def is_up_sprint_short(row: dict[str, Any], signal: str) -> bool:
    return signal == "DOWN" and row.get("trend") == "trend_up" and row.get("normal_pos") == "above_upper" and row.get("sprint") == "up_sprint"


def confirm_signal(row: dict[str, Any], signal: str) -> tuple[bool, str]:
    if is_up_sprint_short(row, signal):
        if row.get("lag10_normal_pos") in {"below_lower", "lower_edge", "lower_inside"}:
            return False, "skip_up_sprint_from_lower"
        checks = [
            f(row.get("lag5_ret10_bps")) >= 8.0,
            f(row.get("lag10_z")) >= 0.0,
            f(row.get("lag5_sigma_expand")) >= 0.9,
            f(row.get("lag3_vol_ratio30")) >= 0.7,
        ]
        if not enough(checks, 2):
            return False, "skip_up_sprint_not_mature"
        return True, "up_sprint_mature_fade"

    is_down_rebound_long = signal == "UP" and row.get("trend") == "trend_down" and row.get("normal_pos") in {"below_lower", "lower_edge", "lower_inside"}
    if is_down_rebound_long:
        if row.get("normal_pos") == "lower_inside":
            return False, "skip_down_rebound_lower_inside"
        checks = [
            f(row.get("sec_ret30_bps")) >= 0.0,
            f(row.get("sec_ret60_bps")) >= 0.0,
            f(row.get("bid20_chg60")) >= 0.0,
            f(row.get("imb20_now")) >= -0.1,
            f(row.get("sigma10_bps")) <= 4.8,
        ]
        if not enough(checks, 2):
            return False, "skip_down_rebound_no_stopfall"
        return True, "down_rebound_confirmed"

    return True, "base_vote"


def trend_start_score(row: dict[str, Any]) -> tuple[int, dict[str, bool]]:
    checks = {
        "multi_period_up": f(row.get("ret10_bps")) >= 12.0 and f(row.get("ret30_bps")) >= 18.0 and f(row.get("ret60_bps")) >= 20.0,
        "short_accelerating": f(row.get("ret15_bps")) >= 1.0 and f(row.get("ret30_bps_sec")) >= f(row.get("prev30_bps_sec")),
        "buy_flow_strong": f(row.get("flow30_now")) >= 0.35 or f(row.get("flow30_delta")) >= 0.15,
        "book_buy_strong": f(row.get("imb20_now")) >= 0.10 or f(row.get("bid_ask20_ratio")) >= 1.10,
        "not_low_volume": f(row.get("vol_ratio30")) >= 0.70,
        "not_from_mature_upper": row.get("lag10_normal_pos") not in {"above_upper", "upper_edge"},
    }
    return sum(1 for ok in checks.values() if ok), checks


def decide_signal(row: dict[str, Any], raw_signal: str, startup_skip_threshold: int = 4) -> dict[str, Any]:
    ok, reason = confirm_signal(row, raw_signal)
    if not ok:
        return {"signal": None, "reason": reason, "raw_signal": raw_signal, "startupScore": 0}
    if not is_up_sprint_short(row, raw_signal):
        return {"signal": raw_signal, "reason": reason, "raw_signal": raw_signal, "startupScore": None}
    score, checks = trend_start_score(row)
    if score >= startup_skip_threshold:
        labels = ",".join(name for name, passed in checks.items() if passed)
        return {
            "signal": None,
            "reason": f"skip_trend_start_{score}of6:{labels}",
            "raw_signal": raw_signal,
            "startupScore": score,
            "startupChecks": checks,
        }
    return {
        "signal": raw_signal,
        "reason": f"{reason}|startup_score_{score}",
        "raw_signal": raw_signal,
        "startupScore": score,
        "startupChecks": checks,
    }


def evaluate_row(row: dict[str, Any], compiled: list[dict[str, Any]], cfg: BranchVoteStartupConfig | None = None) -> dict[str, Any]:
    cfg = cfg or BranchVoteStartupConfig()
    raw_signal, vote_info = vote_for(row, compiled)
    total_votes = int(vote_info["upVotes"]) + int(vote_info["downVotes"])
    base = {"raw_signal": raw_signal, "upVotes": int(vote_info["upVotes"]), "downVotes": int(vote_info["downVotes"]), "votes": vote_info["votes"]}
    if raw_signal is None or total_votes < cfg.min_votes:
        return {**base, "signal": None, "reason": "vote_not_enough"}
    decision = decide_signal(row, raw_signal, cfg.startup_skip_threshold)
    return {**base, **decision}


def evaluate_latest(snapshots: pd.DataFrame, compiled: list[dict[str, Any]], cfg: BranchVoteStartupConfig | None = None) -> dict[str, Any]:
    cfg = cfg or BranchVoteStartupConfig()
    if snapshots.empty:
        return {"signal": None, "reason": "no_snapshot"}
    with_lags = add_lag_features(snapshots)
    row = with_lags.sort_values("time").iloc[-1].to_dict()
    decision = evaluate_row(row, compiled, cfg)
    return {**row, **decision}
