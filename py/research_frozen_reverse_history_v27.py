"""Validate two frozen hypotheses on unopened 2020-2023 futures minutes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pull_binance_futures_monthly_klines import sha256_file
from research_full_regime_action_matrix_v25 import (
    EXHAUSTION_REVERSAL,
    _family_signal_arrays,
)
from research_long_history_walkforward_v20 import build_volatility_states
from research_minute_volatility_normal_v15 import (
    AMOUNT_U,
    BREAKEVEN_WR,
    PAYOUT_RATE,
    _boundary_mask,
    clean,
    load_minutes,
)
from research_multiregime_strategy_v16 import apply_shared_cooldown, metrics
from research_stationarity_router_v19 import (
    PROFILES,
    _bootstrap_block_ev,
    fixed_metrics,
    generate_candidates,
)
from stationarity_features_v19 import build_stationarity_features


ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "data" / "btcusdt_futures_1m_20200101_20240101.csv"
MANIFEST = INPUT.with_suffix(".manifest.json")
OUT_JSON = ROOT / "tmp" / "v27_frozen_reverse_history_20260730.json"
OUT_H1 = ROOT / "tmp" / "v27_h1_direct_reversion_trades_20260730.csv"
OUT_H2 = ROOT / "tmp" / "v27_h2_exhaustion_trades_20260730.csv"

START = pd.Timestamp("2020-01-01T00:00:00Z")
END = pd.Timestamp("2024-01-01T00:00:00Z")
CALENDAR_MONTHS = pd.period_range("2020-01", "2023-12", freq="M").strftime(
    "%Y-%m"
).tolist()
YEARS = (2020, 2021, 2022, 2023)
H1_PROFILE = "v19_edge_w60_z2p0"
H1_Z_THRESHOLD = 2.5
H2_LOOKBACK_MIN = 10
H2_THRESHOLD = 2.0
HORIZON_MIN = 10


def verify_input(path: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    observed = sha256_file(path)
    if observed != str(manifest.get("sha256", "")).lower():
        raise ValueError("V27 frozen input SHA-256 mismatch")
    audit = manifest.get("audit", {})
    if (
        int(audit.get("missingMinutes", -1)) != 0
        or int(audit.get("duplicateMinutes", -1)) != 0
        or int(audit.get("rows", -1)) != 2_103_840
    ):
        raise ValueError("V27 frozen input continuity audit is not clean")
    return {"sha256": observed, "manifest": str(Path(manifest_path).resolve()), "audit": audit}


def _status_and_pnl(signed: float) -> tuple[str, float]:
    if signed > 0.0:
        return "won", AMOUNT_U * PAYOUT_RATE
    if signed < 0.0:
        return "lost", -AMOUNT_U
    return "tie", 0.0


def generate_h2_candidates(
    minutes: pd.DataFrame, volatility: pd.DataFrame
) -> pd.DataFrame:
    boundary = np.asarray(_boundary_mask(minutes.index), dtype=bool)
    known = volatility["vol_state"].isin(("low", "mid", "high")).to_numpy(bool)
    positions = np.flatnonzero(boundary & known)
    positions = positions[positions + 1 + 1 + 20 < len(minutes)]
    up, down, score = _family_signal_arrays(
        minutes, H2_LOOKBACK_MIN, positions
    )[f"{EXHAUSTION_REVERSAL}|{H2_THRESHOLD}"]
    state = volatility["vol_state"].iloc[positions].astype(str).to_numpy()
    chosen = np.flatnonzero((up | down) & (state == "mid"))
    opens = minutes["open"].to_numpy(float)
    rows: list[dict[str, Any]] = []
    for offset in chosen:
        position = int(positions[offset])
        signal = "UP" if bool(up[offset]) else "DOWN"
        direction = 1.0 if signal == "UP" else -1.0
        row: dict[str, Any] = {
            "profile": "v27_h2_mid_exhaustion_w10_s2p0",
            "family": EXHAUSTION_REVERSAL,
            "lookback_min": H2_LOOKBACK_MIN,
            "threshold": H2_THRESHOLD,
            "signal_bar_time": minutes.index[position],
            "signal_time": minutes.index[position] + pd.Timedelta(minutes=1),
            "signal": signal,
            "vol_state": "mid",
            "structure_score": float(score[offset]),
            "z": np.nan,
        }
        for horizon in (5, 10, 20):
            for delay in (0, 1):
                entry_position = position + 1 + delay
                settle_position = entry_position + horizon
                entry = float(opens[entry_position])
                settle = float(opens[settle_position])
                signed = (settle / entry - 1.0) * 10_000.0 * direction
                status, pnl = _status_and_pnl(signed)
                suffix = f"h{horizon}_d{delay}"
                row[f"entry_time_{suffix}"] = minutes.index[entry_position]
                row[f"settle_time_{suffix}"] = minutes.index[settle_position]
                row[f"entry_{suffix}"] = entry
                row[f"settle_{suffix}"] = settle
                row[f"signed_bps_{suffix}"] = signed
                row[f"status_{suffix}"] = status
                row[f"pnl_u_{suffix}"] = pnl
        entry_position = position + 2
        settle_position = position + 11
        signed = (
            float(opens[settle_position]) / float(opens[entry_position]) - 1.0
        ) * 10_000.0 * direction
        status, pnl = _status_and_pnl(signed)
        row["entry_time_h10_fixed_d1"] = minutes.index[entry_position]
        row["settle_time_h10_fixed_d1"] = minutes.index[settle_position]
        row["signed_bps_h10_fixed_d1"] = signed
        row["status_h10_fixed_d1"] = status
        row["pnl_u_h10_fixed_d1"] = pnl
        rows.append(row)
    return apply_shared_cooldown(pd.DataFrame(rows), cooldown_min=10)


def generate_h1_candidates(
    minutes: pd.DataFrame,
    volatility: pd.DataFrame,
    stationarity: pd.DataFrame,
) -> pd.DataFrame:
    profile = next(item for item in PROFILES if item.name == H1_PROFILE)
    raw = generate_candidates(minutes, volatility, stationarity, profile)
    selected = raw.loc[
        raw["cell"].eq("mid|revertible")
        & raw["signal"].eq("DOWN")
        & raw["z"].ge(H1_Z_THRESHOLD)
    ]
    return apply_shared_cooldown(selected, cooldown_min=10)


def _execution_metrics(frame: pd.DataFrame, mode: str) -> dict[str, Any]:
    if mode == "exact":
        return metrics(frame, 10, 0)
    if mode == "delayed":
        return metrics(frame, 10, 1)
    if mode == "fixed":
        return fixed_metrics(frame)
    raise ValueError(mode)


def _pnl_column(mode: str) -> str:
    return {
        "exact": "pnl_u_h10_d0",
        "delayed": "pnl_u_h10_d1",
        "fixed": "pnl_u_h10_fixed_d1",
    }[mode]


def summarize(frame: pd.DataFrame, hypothesis: str) -> dict[str, Any]:
    frame = frame.sort_values("signal_time", kind="stable").reset_index(drop=True)
    month = pd.to_datetime(frame["signal_time"], utc=True).dt.strftime("%Y-%m")
    year = pd.to_datetime(frame["signal_time"], utc=True).dt.year
    executions: dict[str, Any] = {}
    for mode in ("exact", "delayed", "fixed"):
        summary = _execution_metrics(frame, mode)
        pnl_column = _pnl_column(mode)
        pnl = pd.to_numeric(frame[pnl_column], errors="coerce").fillna(0.0)
        monthly_pnl = pnl.groupby(month).sum().reindex(CALENDAR_MONTHS, fill_value=0.0)
        yearly_pnl = pnl.groupby(year).sum().reindex(YEARS, fill_value=0.0)
        bootstrap = _bootstrap_block_ev(
            frame, pnl_column, seed_key=f"V27|{hypothesis}|{mode}"
        )
        year_nonnegative = bool(yearly_pnl.ge(0.0).all())
        positive_years = int(yearly_pnl.gt(0.0).sum())
        positive_month_pct = float(monthly_pnl.gt(0.0).mean()) * 100.0
        passed = bool(
            summary["trades"] >= 100
            and summary["winRatePct"] is not None
            and summary["winRatePct"] > BREAKEVEN_WR
            and summary["pnlU"] > 0.0
            and summary["wilson95LowerPct"] is not None
            and summary["wilson95LowerPct"] > BREAKEVEN_WR
            and year_nonnegative
            and positive_years >= 3
            and positive_month_pct >= 60.0
            and bootstrap["lower90EvU"] is not None
            and bootstrap["lower90EvU"] > 0.0
            and summary["maxDrawdownU"] <= 30.0
            and summary["maxLossStreak"] <= 3
        )
        executions[mode] = {
            **summary,
            "fixedCalendarMonths": CALENDAR_MONTHS,
            "positiveMonthPct": round(positive_month_pct, 4),
            "worstMonthPnlU": round(float(monthly_pnl.min()), 4),
            "monthlyPnlU": {str(key): float(value) for key, value in monthly_pnl.items()},
            "yearlyPnlU": {str(key): float(value) for key, value in yearly_pnl.items()},
            "nonnegativeAllYears": year_nonnegative,
            "positiveYears": positive_years,
            "bootstrap": bootstrap,
            "passed": passed,
        }
    direction = {
        str(name): {
            mode: _execution_metrics(group, mode)
            for mode in ("exact", "delayed", "fixed")
        }
        for name, group in frame.groupby("signal", sort=True)
    }
    return {
        "signals": int(len(frame)),
        "start": frame["signal_time"].min() if not frame.empty else None,
        "end": frame["signal_time"].max() if not frame.empty else None,
        "executions": executions,
        "directionDiagnostic": direction,
        "passed": all(row["passed"] for row in executions.values()),
    }


def run(input_path: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    frozen = verify_input(input_path, manifest_path)
    minutes = load_minutes(input_path)[["open", "high", "low", "close", "volume"]]
    if minutes.index[0] != START or minutes.index[-1] != END - pd.Timedelta(minutes=1):
        raise ValueError("V27 input does not match the frozen time interval")
    volatility = build_volatility_states(minutes, 120)
    stationarity = build_stationarity_features(minutes)
    h1 = generate_h1_candidates(minutes, volatility, stationarity)
    h2 = generate_h2_candidates(minutes, volatility)
    h1.to_csv(OUT_H1, index=False, encoding="utf-8-sig")
    h2.to_csv(OUT_H2, index=False, encoding="utf-8-sig")
    h1_summary = summarize(h1, "H1")
    h2_summary = summarize(h2, "H2")
    report = {
        "generatedAt": pd.Timestamp.now(tz="UTC"),
        "status": "V27_FROZEN_REVERSE_TIME_HISTORY_VALIDATION",
        "safety": {
            "researchOnly": True,
            "tradeEnabled": False,
            "deploymentPerformed": False,
            "onlineConfigurationChanged": False,
            "realTradingAllowed": False,
        },
        "data": {
            "input": str(Path(input_path).resolve()),
            "rows": int(len(minutes)),
            "start": minutes.index[0],
            "end": minutes.index[-1],
            **frozen,
        },
        "protocol": {
            "file": str(
                (ROOT / "docs" / "v27_frozen_reverse_history_protocol_20260730.md").resolve()
            ),
            "openedAfterProtocolFreeze": True,
            "reverseTimeHistoryWarning": "2020-2023 is older untouched history, not future chronological holdout.",
            "executionModes": ["exact", "delayed_full_horizon", "delayed_fixed_settlement"],
        },
        "hypotheses": {
            "H1": {
                "rule": "mid|revertible, w60 price z>=2.5, DOWN only, hold10m",
                "primary": True,
                "result": h1_summary,
            },
            "H2": {
                "rule": "mid volatility, 10m move score>=2.0 plus 3m 15% counter-move, both directions, hold10m",
                "primary": False,
                "multipleSelectionWarning": "H2 was discovered after V25's 540-cell matrix.",
                "result": h2_summary,
            },
        },
        "decision": {
            "H1": "historical_candidate" if h1_summary["passed"] else "no_trade",
            "H2": "historical_candidate" if h2_summary["passed"] else "no_trade",
            "deployment": "none",
            "realTradingAllowed": False,
        },
        "outputs": {
            "json": str(OUT_JSON.resolve()),
            "h1Trades": str(OUT_H1.resolve()),
            "h2Trades": str(OUT_H2.resolve()),
        },
    }
    OUT_JSON.write_text(
        json.dumps(clean(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=str(INPUT))
    parser.add_argument("--manifest", default=str(MANIFEST))
    args = parser.parse_args()
    report = run(args.input, args.manifest)
    print(
        json.dumps(
            clean(
                {
                    "data": report["data"],
                    "hypotheses": report["hypotheses"],
                    "decision": report["decision"],
                    "outputs": report["outputs"],
                }
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
