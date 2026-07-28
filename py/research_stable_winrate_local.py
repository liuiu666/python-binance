"""Local-only stability audit for the live BTC 30-minute strategy.

The script resumes the existing walk-forward cache, trains only missing local
windows, applies the live 30-minute cooldown, and compares a small fixed set of
candidate policies. It never reads from or writes to the production server.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from xgboost import XGBClassifier


ROOT = Path(__file__).resolve().parents[1]
PY_DIR = ROOT / "py"
DATA_DIR = ROOT / "data" / "server_latest"
CACHE_DIR = ROOT / "data" / "cache"
OUT_DIR = ROOT / "tmp" / "stable_winrate_local"
OUT_DIR.mkdir(parents=True, exist_ok=True)

os.environ["APP_DIR"] = str(ROOT)
os.environ["DATA_DIR"] = str(DATA_DIR)
sys.path.insert(0, str(PY_DIR))

import backtest_enhanced as enhanced  # noqa: E402


STRATEGY_ID = "BTC_30min"
HORIZON = 6
COOLDOWN_BARS = 6
TRAIN_SIZE = 8000
TEST_SIZE = 500
STEP = 500
PAYOUT = 0.85
TREND_EPS = 0.00005
BREAKEVEN_WR = 100.0 / (1.0 + PAYOUT)


def make_models() -> list:
    return [
        XGBClassifier(
            n_estimators=200,
            max_depth=4,
            learning_rate=0.05,
            subsample=0.7,
            colsample_bytree=0.6,
            reg_alpha=1.0,
            reg_lambda=2.0,
            min_child_weight=30,
            tree_method="hist",
            eval_metric="logloss",
            use_label_encoder=False,
            verbosity=0,
            random_state=42,
        ),
        XGBClassifier(
            n_estimators=250,
            max_depth=3,
            learning_rate=0.03,
            subsample=0.8,
            colsample_bytree=0.7,
            reg_alpha=0.5,
            reg_lambda=1.5,
            min_child_weight=25,
            tree_method="hist",
            eval_metric="logloss",
            use_label_encoder=False,
            verbosity=0,
            random_state=123,
        ),
        LGBMClassifier(
            n_estimators=240,
            max_depth=4,
            learning_rate=0.04,
            subsample=0.75,
            colsample_bytree=0.65,
            reg_alpha=0.8,
            reg_lambda=1.8,
            min_child_samples=35,
            random_state=77,
            verbose=-1,
        ),
    ]


def feature_hash(frame: pd.DataFrame) -> str:
    return hashlib.sha1("|".join(enhanced.fcols(frame)).encode("utf-8")).hexdigest()[:10]


def compatible_cache(feature_frame: pd.DataFrame) -> tuple[Path, dict[str, np.ndarray]]:
    expected_hash = feature_hash(feature_frame)
    candidates = sorted(
        CACHE_DIR.glob(f"walkforward_{STRATEGY_ID}_h{HORIZON}_tr{TRAIN_SIZE}_te{TEST_SIZE}_st{STEP}_*_c{expected_hash}_*.npz"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        raw = np.load(path, allow_pickle=False)
        count = len(raw["time"])
        start = TRAIN_SIZE
        end = start + count
        if end > len(feature_frame):
            continue
        expected_times = pd.to_datetime(feature_frame["time"].iloc[start:end], utc=True).astype(str).to_numpy()
        cached_times = pd.to_datetime(raw["time"].astype(str), utc=True).astype(str).to_numpy()
        if not np.array_equal(expected_times, cached_times):
            continue
        expected_y = (feature_frame["target"].iloc[start:end].to_numpy() == 1).astype(int)
        if not np.array_equal(expected_y, raw["y"].astype(int)):
            continue
        return path, {key: raw[key].copy() for key in raw.files}
    raise RuntimeError("No compatible BTC_30min walk-forward cache found")


def resume_predictions(df5: pd.DataFrame) -> tuple[dict[str, np.ndarray], dict[str, object]]:
    feature_frame = enhanced.build_features(df5, HORIZON)
    feature_frame = feature_frame[feature_frame["target"] != 0].reset_index(drop=True)
    cache_path, cached = compatible_cache(feature_frame)
    columns = enhanced.fcols(feature_frame)
    x_all = feature_frame[columns].to_numpy()
    y_all = (feature_frame["target"].to_numpy() == 1).astype(int)

    chunks: dict[str, list] = {
        "time": cached["time"].astype(str).tolist(),
        "y": cached["y"].astype(int).tolist(),
        "avg": cached["avg"].astype(float).tolist(),
        "vote_sum": cached["vote_sum"].astype(int).tolist(),
        "agree_all": cached["agree_all"].astype(bool).tolist(),
        "rsi14": cached["rsi14"].astype(float).tolist(),
        "atrp": cached["atrp"].astype(float).tolist(),
    }
    next_start = TRAIN_SIZE + len(chunks["time"])
    trained_windows = 0
    while next_start + TEST_SIZE <= len(feature_frame):
        started = time.time()
        models = make_models()
        train = slice(next_start - TRAIN_SIZE, next_start)
        test = slice(next_start, next_start + TEST_SIZE)
        probabilities = []
        for model in models:
            model.fit(x_all[train], y_all[train])
            probabilities.append(model.predict_proba(x_all[test])[:, 1])
        probs = np.vstack(probabilities).T
        votes = (probs >= 0.5).astype(int)
        chunks["time"].extend(feature_frame["time"].iloc[test].astype(str).tolist())
        chunks["y"].extend(y_all[test].tolist())
        chunks["avg"].extend(probs.mean(axis=1).tolist())
        chunks["vote_sum"].extend(votes.sum(axis=1).tolist())
        chunks["agree_all"].extend(((votes[:, 0] == votes[:, 1]) & (votes[:, 1] == votes[:, 2])).tolist())
        chunks["rsi14"].extend(feature_frame["rsi14"].iloc[test].astype(float).tolist())
        chunks["atrp"].extend(feature_frame["atrp"].iloc[test].astype(float).tolist())
        trained_windows += 1
        print(
            f"local window {next_start}-{next_start + TEST_SIZE} "
            f"finished in {time.time() - started:.1f}s",
            flush=True,
        )
        next_start += STEP

    merged = {
        "time": np.asarray(chunks["time"], dtype=str),
        "y": np.asarray(chunks["y"], dtype=int),
        "avg": np.asarray(chunks["avg"], dtype=float),
        "vote_sum": np.asarray(chunks["vote_sum"], dtype=int),
        "agree_all": np.asarray(chunks["agree_all"], dtype=bool),
        "rsi14": np.asarray(chunks["rsi14"], dtype=float),
        "atrp": np.asarray(chunks["atrp"], dtype=float),
    }
    resumed_path = OUT_DIR / "walkforward_BTC_30min_resumed.npz"
    np.savez_compressed(resumed_path, **merged)
    metadata = {
        "sourceCache": str(cache_path),
        "sourceCacheEnd": str(pd.to_datetime(cached["time"][-1], utc=True)),
        "trainedWindows": trained_windows,
        "predictionRows": len(merged["time"]),
        "featureRows": len(feature_frame),
        "featureHash": feature_hash(feature_frame),
        "resumedCache": str(resumed_path),
    }
    return merged, metadata


def build_frame(df5: pd.DataFrame, predictions: dict[str, np.ndarray]) -> pd.DataFrame:
    features = enhanced.build_features(df5, HORIZON)
    features = features[features["target"] != 0].reset_index(drop=True)
    keep = [
        "time", "rsi14", "bbp", "bbw", "atrp", "atr_exp", "pre20", "pre50",
        "roc5", "roc10", "mom_6", "mom_12", "hlp20", "hlp50", "trend6",
        "trend12", "trend30", "ema_stack", "vr", "taker_ratio", "ls_ratio",
    ]
    available = [column for column in keep if column in features.columns]
    feature_rows = features[available].copy()
    feature_rows["time_key"] = pd.to_datetime(feature_rows["time"], utc=True)
    prediction_rows = pd.DataFrame(
        {
            "time": pd.to_datetime(predictions["time"], utc=True),
            "target": predictions["y"].astype(int),
            "avg": predictions["avg"].astype(float),
            "vote_sum": predictions["vote_sum"].astype(int),
            "agree_all": predictions["agree_all"].astype(bool),
        }
    )
    frame = prediction_rows.merge(feature_rows.drop(columns=["time"]), left_on="time", right_on="time_key", how="left")
    frame = frame.drop(columns=["time_key"]).sort_values("time").reset_index(drop=True)
    score = np.zeros(len(frame), dtype=int)
    for column in ["trend6", "trend12", "trend30", "pre50"]:
        values = frame[column].astype(float).to_numpy()
        score += (values > TREND_EPS).astype(int)
        score -= (values < -TREND_EPS).astype(int)
    stack = frame["ema_stack"].astype(float).to_numpy()
    score += (stack > 0).astype(int)
    score -= (stack < 0).astype(int)
    frame["trend_score"] = score
    frame["hour_utc"] = frame["time"].dt.hour
    frame["strength"] = np.abs(frame["avg"] - 0.5) * 200.0
    frame["ml_dir_all3"] = (frame["avg"] >= 0.5).astype(int)
    frame["ml_dir_majority"] = (frame["vote_sum"] >= 2).astype(int)
    return frame


def apply_cooldown(mask: np.ndarray) -> np.ndarray:
    selected = np.zeros(len(mask), dtype=bool)
    last = -10**9
    for index in np.flatnonzero(mask):
        if index - last >= COOLDOWN_BARS:
            selected[index] = True
            last = index
    return selected


def max_loss_streak(wins: np.ndarray) -> int:
    best = current = 0
    for won in wins:
        current = 0 if won else current + 1
        best = max(best, current)
    return int(best)


def metrics(frame: pd.DataFrame, direction: np.ndarray, mask: np.ndarray) -> dict[str, object]:
    selected = apply_cooldown(mask)
    indices = np.flatnonzero(selected)
    wins = direction[indices] == frame["target"].to_numpy(int)[indices]
    trades = len(indices)
    won = int(wins.sum())
    lost = trades - won
    return {
        "trades": trades,
        "wins": won,
        "losses": lost,
        "winRate": round(won / trades * 100.0, 2) if trades else None,
        "edgeOverBreakeven": round(won / trades * 100.0 - BREAKEVEN_WR, 2) if trades else None,
        "pnl5U": round(won * 4.25 - lost * 5.0, 2),
        "maxLossStreak": max_loss_streak(wins),
    }


def signal_candidate(
    frame: pd.DataFrame,
    threshold: float = 0.55,
    agree_mode: str = "majority",
    trend_gate: str = "none",
    direction_only: str | None = None,
    confidence_max: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    average = frame["avg"].astype(float).to_numpy()
    rsi = frame["rsi14"].astype(float).to_numpy()
    score = frame["trend_score"].astype(int).to_numpy()
    if agree_mode == "all3":
        agree = frame["agree_all"].to_numpy(bool)
        direction = frame["ml_dir_all3"].to_numpy(int)
    else:
        agree = np.ones(len(frame), dtype=bool)
        direction = frame["ml_dir_majority"].to_numpy(int)
    mask = agree & ((average >= threshold) | (average <= 1.0 - threshold))
    mask &= (rsi < 30.0) | (rsi > 70.0)
    align = np.where(direction == 1, score, -score)
    if trend_gate == "align_or_neutral":
        mask &= align >= 0
    elif trend_gate == "no_strong_trend_score3":
        mask &= np.abs(score) < 3
    elif trend_gate != "none":
        raise ValueError(f"Unsupported trend gate: {trend_gate}")
    if direction_only == "UP":
        mask &= direction == 1
    elif direction_only == "DOWN":
        mask &= direction == 0
    if confidence_max is not None:
        mask &= frame["strength"].to_numpy(float) < confidence_max
    return direction, mask


def combine(frame: pd.DataFrame, parts: list[tuple[np.ndarray, np.ndarray]]) -> tuple[np.ndarray, np.ndarray]:
    direction = np.full(len(frame), -1, dtype=int)
    mask = np.zeros(len(frame), dtype=bool)
    for part_direction, part_mask in parts:
        take = part_mask & ~mask
        direction[take] = part_direction[take]
        mask |= part_mask
    return direction, mask


def evaluate_policy(
    frame: pd.DataFrame,
    direction: np.ndarray,
    mask: np.ndarray,
    untouched_start: pd.Timestamp,
) -> dict[str, object]:
    overall = metrics(frame, direction, mask)
    blocks = []
    for block_index, indices in enumerate(np.array_split(np.arange(len(frame)), 10), start=1):
        block_mask = np.zeros(len(frame), dtype=bool)
        block_mask[indices] = mask[indices]
        blocks.append({"block": block_index, **metrics(frame, direction, block_mask)})
    untouched_mask = mask & (frame["time"] > untouched_start).to_numpy()
    second_half_mask = mask.copy()
    second_half_mask[: len(frame) // 2] = False
    recent_start = int(len(frame) * 0.8)
    recent_mask = mask.copy()
    recent_mask[:recent_start] = False
    overall.update(
        {
            "secondHalf": metrics(frame, direction, second_half_mask),
            "recent20Pct": metrics(frame, direction, recent_mask),
            "untouchedAfterCache": metrics(frame, direction, untouched_mask),
            "positiveBlocks": sum(float(block["pnl5U"]) > 0 for block in blocks),
            "minBlockWinRate": min(
                (float(block["winRate"]) for block in blocks if block["trades"] >= 10 and block["winRate"] is not None),
                default=None,
            ),
            "blocks": blocks,
        }
    )
    return overall


def main() -> None:
    enhanced.OUT = str(DATA_DIR)
    print(f"Loading local data from {DATA_DIR}", flush=True)
    df5 = enhanced.load_symbol("btcusdt")
    if df5 is None or df5.empty:
        raise RuntimeError("Local BTC minute data is unavailable")
    predictions, resume_meta = resume_predictions(df5)
    frame = build_frame(df5, predictions)
    untouched_start = pd.to_datetime(resume_meta["sourceCacheEnd"], utc=True)

    policies: dict[str, tuple[np.ndarray, np.ndarray]] = {
        "current_majority_055": signal_candidate(frame),
        "down_only_majority_055": signal_candidate(frame, direction_only="DOWN"),
        "up_only_majority_055": signal_candidate(frame, direction_only="UP"),
        "majority_058": signal_candidate(frame, threshold=0.58),
        "majority_060": signal_candidate(frame, threshold=0.60),
        "majority_065": signal_candidate(frame, threshold=0.65),
        "all3_055": signal_candidate(frame, agree_mode="all3"),
        "all3_058": signal_candidate(frame, threshold=0.58, agree_mode="all3"),
        "all3_065": signal_candidate(frame, threshold=0.65, agree_mode="all3"),
        "majority_055_conf_lt40": signal_candidate(frame, confidence_max=40),
        "majority_055_conf_lt50": signal_candidate(frame, confidence_max=50),
        "majority_055_align_or_neutral": signal_candidate(frame, trend_gate="align_or_neutral"),
        "majority_055_no_strong_trend": signal_candidate(frame, trend_gate="no_strong_trend_score3"),
    }
    policies["down055_plus_up_majority065"] = combine(
        frame,
        [
            signal_candidate(frame, direction_only="DOWN"),
            signal_candidate(frame, threshold=0.65, direction_only="UP"),
        ],
    )
    policies["down055_plus_up_all3_065"] = combine(
        frame,
        [
            signal_candidate(frame, direction_only="DOWN"),
            signal_candidate(frame, threshold=0.65, agree_mode="all3", direction_only="UP"),
        ],
    )

    results = {}
    for name, (direction, mask) in policies.items():
        results[name] = evaluate_policy(frame, direction, mask, untouched_start)
        summary = results[name]
        print(
            name,
            json.dumps(
                {
                    "trades": summary["trades"],
                    "winRate": summary["winRate"],
                    "pnl5U": summary["pnl5U"],
                    "maxLossStreak": summary["maxLossStreak"],
                    "positiveBlocks": summary["positiveBlocks"],
                    "untouched": summary["untouchedAfterCache"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    report = {
        "method": {
            "execution": "local_only",
            "dataDir": str(DATA_DIR),
            "dataStart": str(df5["time"].min()),
            "dataEnd": str(df5["time"].max()),
            "walkForward": True,
            "trainSize": TRAIN_SIZE,
            "testSize": TEST_SIZE,
            "step": STEP,
            "horizonBars": HORIZON,
            "cooldownBars": COOLDOWN_BARS,
            "payoutRate": PAYOUT,
            "breakevenWinRate": round(BREAKEVEN_WR, 2),
            **resume_meta,
        },
        "results": results,
    }
    report_path = OUT_DIR / "stable_winrate_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {report_path}", flush=True)


if __name__ == "__main__":
    main()
