"""Reusable, execution-aware validation for the V14 strategy family.

The module deliberately separates signal generation from validation.  Callers
provide candidate signals and futures ticks; this module then applies one
family-wide cooldown, resolves executable entry/settlement ticks, and produces
the same risk metrics for every configured entry delay.

Expected candidate columns
--------------------------
``time`` (or a configured time column), ``signal`` (UP/DOWN), and optionally
``family``, ``branch``, ``priority``, ``strategy_id``, ``source`` and ``block``.

Expected tick columns
---------------------
``timestamp``/``time`` (or a DatetimeIndex), ``price``/``close`` and, by
default, ``market=futures``.  Both entry and settlement use the first tick at
or after their target.  The 600-second horizon starts at the *actual resolved
entry tick*, not at the nominal signal timestamp.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd


DEFAULT_DELAYS_SEC = (0, 5, 10, 15)
DEFAULT_EXECUTION_BASE_LAG_SEC = 1
DEFAULT_HORIZON_SEC = 600
DEFAULT_COOLDOWN_SEC = 600
DEFAULT_AMOUNT_U = 5.0
DEFAULT_PAYOUT_RATE = 0.8
DEFAULT_THIN_MARGIN_BPS = 3.0
WILSON_95_Z = 1.959963984540054

_TIME_COLUMNS = ("timestamp", "time", "trade_time", "open_time", "ts")
_PRICE_COLUMNS = ("price", "close")


def _as_utc(values: Any) -> pd.Series:
    return pd.to_datetime(values, utc=True, errors="coerce")


def _first_existing(columns: Iterable[str], candidates: Sequence[str]) -> str | None:
    available = set(columns)
    return next((name for name in candidates if name in available), None)


def normalize_futures_ticks(
    ticks: pd.DataFrame,
    *,
    time_col: str | None = None,
    price_col: str | None = None,
    market_col: str = "market",
    require_futures: bool = True,
) -> pd.DataFrame:
    """Return sorted UTC ticks with canonical ``time`` and ``price`` columns.

    Rows with invalid timestamps or non-positive prices are rejected.  A
    labelled non-futures feed is always rejected when ``require_futures`` is
    true so a spot settlement feed cannot silently enter a futures replay.
    """

    if not isinstance(ticks, pd.DataFrame) or ticks.empty:
        raise ValueError("ticks must be a non-empty pandas DataFrame")

    frame = ticks.copy()
    if require_futures:
        if market_col not in frame.columns:
            raise ValueError(
                f"ticks must contain {market_col!r} with value 'futures'; "
                "pass require_futures=False only for an already verified feed"
            )
        markets = {
            str(value).strip().lower()
            for value in frame[market_col].dropna().unique()
            if str(value).strip()
        }
        if not markets or not markets.issubset({"futures", "future", "fapi", "um"}):
            raise ValueError(f"non-futures tick market detected: {sorted(markets)!r}")

    resolved_time_col = time_col or _first_existing(frame.columns, _TIME_COLUMNS)
    if resolved_time_col is not None:
        times = _as_utc(frame[resolved_time_col])
    elif isinstance(frame.index, pd.DatetimeIndex):
        times = _as_utc(frame.index)
    else:
        raise ValueError(f"could not infer tick time column from {_TIME_COLUMNS!r}")

    resolved_price_col = price_col or _first_existing(frame.columns, _PRICE_COLUMNS)
    if resolved_price_col is None:
        raise ValueError(f"could not infer tick price column from {_PRICE_COLUMNS!r}")

    normalized = pd.DataFrame(
        {
            "time": times.to_numpy(),
            "price": pd.to_numeric(frame[resolved_price_col], errors="coerce").to_numpy(),
            "_tick_order": np.arange(len(frame), dtype=np.int64),
        }
    )
    normalized = normalized.dropna(subset=["time", "price"])
    normalized = normalized[np.isfinite(normalized["price"]) & normalized["price"].gt(0.0)]
    normalized = normalized.sort_values(["time", "_tick_order"], kind="stable").reset_index(drop=True)
    if normalized.empty:
        raise ValueError("ticks contain no valid positive-price rows")
    return normalized


def normalize_candidates(
    candidates: pd.DataFrame,
    *,
    time_col: str = "time",
    signal_col: str = "signal",
    family_col: str = "family",
    branch_col: str = "branch",
    default_family: str = "V14",
) -> pd.DataFrame:
    """Normalize candidate signals without applying dedupe or cooldown."""

    if not isinstance(candidates, pd.DataFrame) or candidates.empty:
        raise ValueError("candidates must be a non-empty pandas DataFrame")
    if time_col not in candidates.columns or signal_col not in candidates.columns:
        raise ValueError(f"candidates require {time_col!r} and {signal_col!r}")

    frame = candidates.copy().reset_index(drop=True)
    frame["time"] = _as_utc(frame[time_col])
    frame["signal"] = frame[signal_col].astype(str).str.upper().str.strip()
    invalid_signals = sorted(set(frame.loc[~frame["signal"].isin({"UP", "DOWN"}), "signal"]))
    if invalid_signals:
        raise ValueError(f"candidate signal must be UP or DOWN, got {invalid_signals!r}")
    if frame["time"].isna().any():
        raise ValueError("candidate timestamps contain invalid values")

    if family_col in frame.columns:
        family = frame[family_col].fillna(default_family).astype(str).str.strip()
        frame["family"] = family.mask(family.eq(""), default_family)
    else:
        frame["family"] = str(default_family)
    if branch_col in frame.columns:
        branch = frame[branch_col].fillna("unknown").astype(str).str.strip()
        frame["branch"] = branch.mask(branch.eq(""), "unknown")
    else:
        frame["branch"] = "unknown"

    if "priority" not in frame.columns:
        frame["priority"] = 0
    frame["priority"] = pd.to_numeric(frame["priority"], errors="coerce").fillna(0).astype(int)
    frame["_input_order"] = np.arange(len(frame), dtype=np.int64)
    return frame.sort_values(["time", "priority", "_input_order"], kind="stable").reset_index(drop=True)


def deduplicate_candidates(
    candidates: pd.DataFrame,
    *,
    dedupe_cols: Sequence[str] = ("family", "time", "signal", "branch"),
) -> pd.DataFrame:
    """Collapse overlapping pulls of the same family/time/direction/branch."""

    missing = [column for column in dedupe_cols if column not in candidates.columns]
    if missing:
        raise ValueError(f"candidate dedupe columns missing: {missing!r}")
    frame = candidates.copy()
    counts = frame.groupby(list(dedupe_cols), dropna=False)[dedupe_cols[0]].transform("size")
    frame["duplicate_count"] = counts.astype(int)
    return frame.drop_duplicates(list(dedupe_cols), keep="first").reset_index(drop=True)


def apply_family_cooldown(
    candidates: pd.DataFrame,
    *,
    cooldown_sec: int = DEFAULT_COOLDOWN_SEC,
) -> pd.DataFrame:
    """Keep at most one candidate per family in each cooldown window.

    The boundary is inclusive: a candidate exactly ``cooldown_sec`` after the
    previous kept candidate is allowed.  Cooldown is shared across strategy
    aliases and branches that carry the same ``family`` value.
    """

    if cooldown_sec < 0:
        raise ValueError("cooldown_sec must be non-negative")
    required = {"family", "time", "priority"}
    if not required.issubset(candidates.columns):
        raise ValueError(f"candidates missing cooldown columns: {sorted(required - set(candidates.columns))}")

    kept: list[dict[str, Any]] = []
    last_by_family: dict[str, pd.Timestamp] = {}
    ordered = candidates.sort_values(["time", "priority", "_input_order"], kind="stable")
    for row in ordered.to_dict("records"):
        family = str(row["family"])
        timestamp = pd.Timestamp(row["time"])
        last = last_by_family.get(family)
        if last is not None and (timestamp - last).total_seconds() < cooldown_sec:
            continue
        kept.append(row)
        last_by_family[family] = timestamp
    return pd.DataFrame(kept, columns=ordered.columns).reset_index(drop=True)


class TickResolver:
    """Efficient first-tick-at-or-after lookup over normalized futures ticks."""

    def __init__(self, ticks: pd.DataFrame):
        self._ticks = ticks.reset_index(drop=True)
        # Pandas 3 can preserve parsed timestamps as datetime64[us, UTC].
        # Converting that series directly to int64 would yield microseconds,
        # while Timestamp.value below is always nanoseconds.  Force one unit
        # here so searchsorted cannot silently classify every tick as missing.
        self._times_ns = self._ticks["time"].to_numpy(
            dtype="datetime64[ns]"
        ).astype(np.int64)
        self._prices = self._ticks["price"].to_numpy(float)

    def first_at_or_after(
        self,
        target: pd.Timestamp,
        *,
        max_lag_sec: float | None = None,
    ) -> dict[str, Any] | None:
        timestamp = pd.Timestamp(target)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        timestamp = timestamp.tz_convert("UTC")
        target_ns = int(timestamp.value)
        pos = int(np.searchsorted(self._times_ns, target_ns, side="left"))
        if pos >= len(self._times_ns):
            return None
        lag_sec = (int(self._times_ns[pos]) - target_ns) / 1_000_000_000.0
        if max_lag_sec is not None and lag_sec > float(max_lag_sec):
            return None
        return {
            "time": pd.Timestamp(int(self._times_ns[pos]), tz="UTC"),
            "price": float(self._prices[pos]),
            "lag_sec": float(lag_sec),
        }


def _candidate_key(row: dict[str, Any]) -> str:
    timestamp = pd.Timestamp(row["time"]).tz_convert("UTC").isoformat()
    return "|".join((str(row["family"]), timestamp, str(row["signal"]), str(row["branch"])))


def resolve_candidate_trades(
    candidates: pd.DataFrame,
    ticks: pd.DataFrame,
    *,
    delays_sec: Sequence[int] = DEFAULT_DELAYS_SEC,
    execution_base_lag_sec: int = DEFAULT_EXECUTION_BASE_LAG_SEC,
    horizon_sec: int = DEFAULT_HORIZON_SEC,
    amount_u: float = DEFAULT_AMOUNT_U,
    payout_rate: float = DEFAULT_PAYOUT_RATE,
    max_tick_lag_sec: float | None = None,
) -> pd.DataFrame:
    """Resolve entry and settlement ticks for cooldown-filtered candidates."""

    delays = tuple(dict.fromkeys(int(delay) for delay in delays_sec))
    if not delays or any(delay < 0 for delay in delays):
        raise ValueError("delays_sec must contain non-negative integers")
    if execution_base_lag_sec < 0:
        raise ValueError("execution_base_lag_sec must be non-negative")
    if horizon_sec <= 0:
        raise ValueError("horizon_sec must be positive")
    if amount_u <= 0 or not 0.0 < payout_rate <= 10.0:
        raise ValueError("amount_u and payout_rate must be positive")

    resolver = TickResolver(ticks)
    rows: list[dict[str, Any]] = []
    passthrough = (
        "strategy_id", "source", "block", "dataset", "reason", "regime",
        "priority", "duplicate_count",
    )
    for candidate in candidates.to_dict("records"):
        signal_time = pd.Timestamp(candidate["time"])
        candidate_key = _candidate_key(candidate)
        direction = 1.0 if candidate["signal"] == "UP" else -1.0
        for delay in delays:
            entry_target = signal_time + pd.Timedelta(
                seconds=int(execution_base_lag_sec) + delay
            )
            entry = resolver.first_at_or_after(entry_target, max_lag_sec=max_tick_lag_sec)
            base = {
                "trade_key": f"{candidate_key}|d{delay}",
                "candidate_key": candidate_key,
                "family": candidate["family"],
                "signal": candidate["signal"],
                "branch": candidate["branch"],
                "signal_time": signal_time,
                "delay_sec": int(delay),
                "execution_base_lag_sec": int(execution_base_lag_sec),
                "horizon_sec": int(horizon_sec),
                "entry_target_time": entry_target,
                **{key: candidate.get(key) for key in passthrough if key in candidate},
            }
            if entry is None:
                rows.append({
                    **base,
                    "status": "missing_entry",
                    "entry_time": pd.NaT,
                    "entry_price": np.nan,
                    "entry_lag_sec": np.nan,
                    "settle_target_time": pd.NaT,
                    "settle_time": pd.NaT,
                    "settle_price": np.nan,
                    "settle_lag_sec": np.nan,
                    "signed_bps": np.nan,
                    "pnl_u": np.nan,
                })
                continue

            settle_target = entry["time"] + pd.Timedelta(seconds=horizon_sec)
            settle = resolver.first_at_or_after(settle_target, max_lag_sec=max_tick_lag_sec)
            if settle is None:
                rows.append({
                    **base,
                    "status": "missing_settlement",
                    "entry_time": entry["time"],
                    "entry_price": entry["price"],
                    "entry_lag_sec": entry["lag_sec"],
                    "settle_target_time": settle_target,
                    "settle_time": pd.NaT,
                    "settle_price": np.nan,
                    "settle_lag_sec": np.nan,
                    "signed_bps": np.nan,
                    "pnl_u": np.nan,
                })
                continue

            signed_bps = (settle["price"] / entry["price"] - 1.0) * 10_000.0 * direction
            if signed_bps > 0.0:
                status = "won"
                pnl_u = amount_u * payout_rate
            elif signed_bps < 0.0:
                status = "lost"
                pnl_u = -amount_u
            else:
                status = "tie"
                pnl_u = 0.0
            rows.append({
                **base,
                "status": status,
                "entry_time": entry["time"],
                "entry_price": entry["price"],
                "entry_lag_sec": entry["lag_sec"],
                "settle_target_time": settle_target,
                "settle_time": settle["time"],
                "settle_price": settle["price"],
                "settle_lag_sec": settle["lag_sec"],
                "signed_bps": float(signed_bps),
                "pnl_u": float(pnl_u),
            })

    trades = pd.DataFrame(rows)
    if trades.empty:
        return trades
    return trades.drop_duplicates("trade_key", keep="first").sort_values(
        ["entry_target_time", "family", "delay_sec"], kind="stable"
    ).reset_index(drop=True)


def wilson_lower_bound(wins: int, decided: int, *, z: float = WILSON_95_Z) -> float | None:
    """Return the two-sided 95% Wilson lower bound as a 0..1 fraction."""

    if decided <= 0:
        return None
    wins = min(max(0, int(wins)), int(decided))
    n = float(decided)
    p = wins / n
    denominator = 1.0 + z * z / n
    center = p + z * z / (2.0 * n)
    radius = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return max(0.0, (center - radius) / denominator)


def summarize_metrics(
    trades: pd.DataFrame,
    *,
    thin_margin_bps: float = DEFAULT_THIN_MARGIN_BPS,
) -> dict[str, Any]:
    """Calculate binary-option performance and risk metrics for one slice."""

    if trades is None or trades.empty:
        return {
            "requested": 0,
            "settled": 0,
            "wins": 0,
            "losses": 0,
            "ties": 0,
            "missing": 0,
            "winRatePct": None,
            "wilson95LowerPct": None,
            "pnlU": 0.0,
            "expectedValueU": None,
            "maxDrawdownU": 0.0,
            "maxLossStreak": 0,
            "medianSignedBps": None,
            "thinMarginBps": float(thin_margin_bps),
            "thinMarginCount": 0,
            "thinMarginPct": None,
        }

    ordered = trades.sort_values(
        [column for column in ("entry_time", "signal_time") if column in trades.columns],
        kind="stable",
    )
    settled = ordered[ordered["status"].isin({"won", "lost", "tie"})].copy()
    wins = int(settled["status"].eq("won").sum())
    losses = int(settled["status"].eq("lost").sum())
    ties = int(settled["status"].eq("tie").sum())
    decided = wins + losses

    pnl = pd.to_numeric(settled.get("pnl_u"), errors="coerce").fillna(0.0).to_numpy(float)
    equity = np.r_[0.0, np.cumsum(pnl)]
    drawdown = np.maximum.accumulate(equity) - equity
    streak = max_streak = 0
    for status in settled["status"]:
        if status == "lost":
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    signed = pd.to_numeric(settled.get("signed_bps"), errors="coerce").dropna()
    thin = signed.abs().le(float(thin_margin_bps))
    lower = wilson_lower_bound(wins, decided)
    return {
        "requested": int(len(ordered)),
        "settled": int(len(settled)),
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "missing": int(len(ordered) - len(settled)),
        "winRatePct": round(wins / decided * 100.0, 4) if decided else None,
        "wilson95LowerPct": round(lower * 100.0, 4) if lower is not None else None,
        "pnlU": round(float(pnl.sum()), 4),
        "expectedValueU": round(float(pnl.sum()) / len(settled), 6) if len(settled) else None,
        "maxDrawdownU": round(float(drawdown.max()) if len(drawdown) else 0.0, 4),
        "maxLossStreak": int(max_streak),
        "medianSignedBps": round(float(signed.median()), 4) if not signed.empty else None,
        "thinMarginBps": float(thin_margin_bps),
        "thinMarginCount": int(thin.sum()),
        "thinMarginPct": round(float(thin.mean()) * 100.0, 4) if not thin.empty else None,
    }


def metrics_by_delay(
    trades: pd.DataFrame,
    *,
    thin_margin_bps: float = DEFAULT_THIN_MARGIN_BPS,
) -> dict[str, dict[str, Any]]:
    if trades is None or trades.empty:
        return {}
    return {
        str(int(delay)): summarize_metrics(group, thin_margin_bps=thin_margin_bps)
        for delay, group in trades.groupby("delay_sec", sort=True)
    }


def _with_blocks(
    trades: pd.DataFrame,
    *,
    block_col: str | None,
    block_freq: str,
    block_timezone: str,
    block_size: int | None,
) -> pd.DataFrame:
    frame = trades.copy()
    if block_col and block_col in frame.columns:
        frame["validation_block"] = frame[block_col].fillna("unknown").astype(str)
        return frame
    if block_size is not None:
        if block_size <= 0:
            raise ValueError("block_size must be positive")
        unique = (
            frame[["candidate_key", "signal_time"]]
            .drop_duplicates("candidate_key")
            .sort_values("signal_time", kind="stable")
            .reset_index(drop=True)
        )
        unique["validation_block"] = [f"block_{index // block_size:04d}" for index in range(len(unique))]
        return frame.merge(unique[["candidate_key", "validation_block"]], on="candidate_key", how="left")

    local_time = _as_utc(frame["signal_time"]).dt.tz_convert(block_timezone)
    try:
        frame["validation_block"] = local_time.dt.floor(block_freq).astype(str)
    except ValueError as exc:
        raise ValueError(f"block_freq must be a fixed pandas frequency, got {block_freq!r}") from exc
    return frame


def metrics_by_block(
    trades: pd.DataFrame,
    *,
    block_col: str | None = "block",
    block_freq: str = "1D",
    block_timezone: str = "Asia/Shanghai",
    block_size: int | None = None,
    thin_margin_bps: float = DEFAULT_THIN_MARGIN_BPS,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Return metrics nested as ``delay -> block -> metrics``."""

    if trades is None or trades.empty:
        return {}
    blocked = _with_blocks(
        trades,
        block_col=block_col,
        block_freq=block_freq,
        block_timezone=block_timezone,
        block_size=block_size,
    )
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for delay, delay_group in blocked.groupby("delay_sec", sort=True):
        out[str(int(delay))] = {
            str(block): summarize_metrics(group, thin_margin_bps=thin_margin_bps)
            for block, group in delay_group.groupby("validation_block", sort=True)
        }
    return out


def run_validation(
    candidates: pd.DataFrame,
    ticks: pd.DataFrame,
    *,
    candidate_time_col: str = "time",
    tick_time_col: str | None = None,
    tick_price_col: str | None = None,
    delays_sec: Sequence[int] = DEFAULT_DELAYS_SEC,
    execution_base_lag_sec: int = DEFAULT_EXECUTION_BASE_LAG_SEC,
    horizon_sec: int = DEFAULT_HORIZON_SEC,
    cooldown_sec: int = DEFAULT_COOLDOWN_SEC,
    amount_u: float = DEFAULT_AMOUNT_U,
    payout_rate: float = DEFAULT_PAYOUT_RATE,
    thin_margin_bps: float = DEFAULT_THIN_MARGIN_BPS,
    max_tick_lag_sec: float | None = None,
    require_futures: bool = True,
    block_col: str | None = "block",
    block_freq: str = "1D",
    block_timezone: str = "Asia/Shanghai",
    block_size: int | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Run the complete V14 execution-aware validation pipeline."""

    normalized_candidates = normalize_candidates(candidates, time_col=candidate_time_col)
    deduped = deduplicate_candidates(normalized_candidates)
    cooled = apply_family_cooldown(deduped, cooldown_sec=cooldown_sec)
    normalized_ticks = normalize_futures_ticks(
        ticks,
        time_col=tick_time_col,
        price_col=tick_price_col,
        require_futures=require_futures,
    )
    trades = resolve_candidate_trades(
        cooled,
        normalized_ticks,
        delays_sec=delays_sec,
        execution_base_lag_sec=execution_base_lag_sec,
        horizon_sec=horizon_sec,
        amount_u=amount_u,
        payout_rate=payout_rate,
        max_tick_lag_sec=max_tick_lag_sec,
    )
    report = {
        "method": {
            "market": "futures" if require_futures else "caller_verified",
            "horizonSecFromActualEntry": int(horizon_sec),
            "executionBaseLagSec": int(execution_base_lag_sec),
            "familyCooldownSec": int(cooldown_sec),
            "entryDelaysSec": [int(value) for value in delays_sec],
            "entryAndSettlement": "first_tick_at_or_after_target",
            "candidateDedupeKey": ["family", "time", "signal", "branch"],
            "tradeDedupeKey": "trade_key",
            "amountU": float(amount_u),
            "winPnlU": float(amount_u * payout_rate),
            "lossPnlU": float(-amount_u),
            "thinMarginBps": float(thin_margin_bps),
            "wilson": "two_sided_95_percent",
        },
        "audit": {
            "inputCandidates": int(len(normalized_candidates)),
            "dedupedCandidates": int(len(deduped)),
            "cooldownCandidates": int(len(cooled)),
            "duplicatesRemoved": int(len(normalized_candidates) - len(deduped)),
            "cooldownRemoved": int(len(deduped) - len(cooled)),
            "tickRows": int(len(normalized_ticks)),
            "resolvedTradeRows": int(len(trades)),
        },
        "metricsByDelay": metrics_by_delay(trades, thin_margin_bps=thin_margin_bps),
        "metricsByBlock": metrics_by_block(
            trades,
            block_col=block_col,
            block_freq=block_freq,
            block_timezone=block_timezone,
            block_size=block_size,
            thin_margin_bps=thin_margin_bps,
        ),
    }
    return trades, report


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if pd.isna(value):
        return None
    raise TypeError(f"cannot JSON encode {type(value).__name__}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", required=True, help="Candidate signal CSV")
    parser.add_argument("--ticks", required=True, help="Futures tick/second-bar CSV")
    parser.add_argument("--report", required=True, help="Output JSON report")
    parser.add_argument("--trades-out", help="Output resolved trade CSV")
    parser.add_argument("--candidate-time-col", default="time")
    parser.add_argument("--tick-time-col")
    parser.add_argument("--tick-price-col")
    parser.add_argument("--delays", default="0,5,10,15")
    parser.add_argument(
        "--execution-base-lag-sec",
        type=int,
        default=DEFAULT_EXECUTION_BASE_LAG_SEC,
        help="Minimum lag after the signal second before an entry can execute",
    )
    parser.add_argument("--horizon-sec", type=int, default=DEFAULT_HORIZON_SEC)
    parser.add_argument("--cooldown-sec", type=int, default=DEFAULT_COOLDOWN_SEC)
    parser.add_argument("--amount-u", type=float, default=DEFAULT_AMOUNT_U)
    parser.add_argument("--payout-rate", type=float, default=DEFAULT_PAYOUT_RATE)
    parser.add_argument("--thin-margin-bps", type=float, default=DEFAULT_THIN_MARGIN_BPS)
    parser.add_argument("--max-tick-lag-sec", type=float)
    parser.add_argument("--allow-unlabelled-market", action="store_true")
    parser.add_argument("--block-col", default="block")
    parser.add_argument("--block-freq", default="1D")
    parser.add_argument("--block-timezone", default="Asia/Shanghai")
    parser.add_argument("--block-size", type=int)
    args = parser.parse_args()

    delays = tuple(int(value.strip()) for value in args.delays.split(",") if value.strip())
    candidates = pd.read_csv(args.candidates)
    ticks = pd.read_csv(args.ticks)
    trades, report = run_validation(
        candidates,
        ticks,
        candidate_time_col=args.candidate_time_col,
        tick_time_col=args.tick_time_col,
        tick_price_col=args.tick_price_col,
        delays_sec=delays,
        execution_base_lag_sec=args.execution_base_lag_sec,
        horizon_sec=args.horizon_sec,
        cooldown_sec=args.cooldown_sec,
        amount_u=args.amount_u,
        payout_rate=args.payout_rate,
        thin_margin_bps=args.thin_margin_bps,
        max_tick_lag_sec=args.max_tick_lag_sec,
        require_futures=not args.allow_unlabelled_market,
        block_col=args.block_col or None,
        block_freq=args.block_freq,
        block_timezone=args.block_timezone,
        block_size=args.block_size,
    )
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    trades_path = Path(args.trades_out) if args.trades_out else report_path.with_name(report_path.stem + "_trades.csv")
    trades_path.parent.mkdir(parents=True, exist_ok=True)
    trades.to_csv(trades_path, index=False, encoding="utf-8-sig")
    print(json.dumps({"report": str(report_path), "trades": str(trades_path), **report["audit"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
