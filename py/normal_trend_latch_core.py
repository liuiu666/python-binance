"""Shared normal/trend router with short-lived execution latching."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from liquidity_v2_core import LiquidityV2Rules, build_features, evaluate_candidate, normal_ready


@dataclass(frozen=True)
class BandParams:
    z_entry: float
    z_reclaim: float
    confirm_hits: int
    confirm_span_sec: int
    ret600_min_bps: float
    flow120_min: float
    enabled: bool = True


@dataclass(frozen=True)
class RouterRules:
    base: LiquidityV2Rules
    latch_sec: int = 6
    execution_interval_sec: int = 5
    execution_phase: int = 0
    data_observed_min_pct: float = 90.0
    orderbook_coverage_min: float = 0.90
    trend_confirm_sec: int = 20
    startup_skip_enabled: bool = False
    startup_skip_threshold: int = 4

    @classmethod
    def from_config(cls, cfg):
        return cls(
            base=LiquidityV2Rules.from_config(cfg),
            latch_sec=int(cfg.get("router_latch_sec", 6)),
            execution_interval_sec=int(cfg.get("router_execution_interval_sec", 5)),
            execution_phase=int(cfg.get("router_execution_phase", 0)),
            data_observed_min_pct=float(cfg.get("router_data_observed_min_pct", 90.0)),
            orderbook_coverage_min=float(cfg.get("router_orderbook_coverage_min", 0.90)),
            trend_confirm_sec=int(cfg.get("router_trend_confirm_sec", 20)),
            startup_skip_enabled=bool(cfg.get("router_startup_skip_enabled", False)),
            startup_skip_threshold=int(cfg.get("router_startup_skip_threshold", 4)),
        )


OFF = BandParams(1.5, 0.7, 4, 12, 0.0, 0.0, False)
DEFAULT_BANDS = {
    "ultra_low": BandParams(0.80, 0.80, 2, 5, -15.0, -0.12),
    "low": BandParams(0.90, 0.85, 2, 5, -15.0, -0.12),
    "mid": BandParams(1.00, 0.90, 2, 5, -12.0, -0.08),
    "elevated": BandParams(1.20, 0.85, 3, 8, -10.0, -0.08),
    "high": OFF,
}


def band_name(sigma_bps: float) -> str:
    if sigma_bps < 6.5:
        return "ultra_low"
    if sigma_bps < 8.0:
        return "low"
    if sigma_bps < 10.0:
        return "mid"
    if sigma_bps < 14.0:
        return "elevated"
    return "high"


def band_params(cfg, band: str) -> BandParams:
    default = DEFAULT_BANDS[band]
    prefix = f"router_band_{band}_"
    return BandParams(
        z_entry=float(cfg.get(prefix + "z_entry", default.z_entry)),
        z_reclaim=float(cfg.get(prefix + "z_reclaim", default.z_reclaim)),
        confirm_hits=int(cfg.get(prefix + "confirm_hits", default.confirm_hits)),
        confirm_span_sec=int(cfg.get(prefix + "confirm_span_sec", default.confirm_span_sec)),
        ret600_min_bps=float(cfg.get(prefix + "ret600_min_bps", default.ret600_min_bps)),
        flow120_min=float(cfg.get(prefix + "flow120_min", default.flow120_min)),
        enabled=bool(cfg.get(prefix + "enabled", default.enabled)),
    )


def build_router_features(data: pd.DataFrame, rules: RouterRules) -> pd.DataFrame:
    out = build_features(data, rules.base)
    close = data["close"].astype(float)
    volume = data["volume"].astype(float).clip(lower=0.0)
    buy = data["buy_qty"].astype(float).clip(lower=0.0)
    sell = data["sell_qty"].astype(float).clip(lower=0.0)
    bid20 = data["bid_qty_20"].astype(float)
    ask20 = data["ask_qty_20"].astype(float)
    one_sec_path = np.log(close / close.shift(1)).abs() * 10000.0
    path_600 = one_sec_path.rolling(600, min_periods=300).sum()
    out["ret_15s_bps"] = np.log(close / close.shift(15)) * 10000.0
    out["ret_30s_bps"] = np.log(close / close.shift(30)) * 10000.0
    out["prev_ret_30s_bps"] = np.log(close.shift(30) / close.shift(60)) * 10000.0
    out["ret_60s_bps"] = np.log(close / close.shift(60)) * 10000.0
    out["ret_600s_bps"] = np.log(close / close.shift(600)) * 10000.0
    out["ret_1800s_bps"] = np.log(close / close.shift(1800)) * 10000.0
    out["ret_3600s_bps"] = np.log(close / close.shift(3600)) * 10000.0
    out["efficiency_600"] = out["ret_600s_bps"].abs() / path_600.replace(0.0, np.nan)
    buy_30 = buy.rolling(30, min_periods=10).sum()
    sell_30 = sell.rolling(30, min_periods=10).sum()
    prev_buy_30 = buy.shift(30).rolling(30, min_periods=10).sum()
    prev_sell_30 = sell.shift(30).rolling(30, min_periods=10).sum()
    out["flow_30"] = (buy_30 - sell_30) / (buy_30 + sell_30).replace(0.0, np.nan)
    prev_flow_30 = (prev_buy_30 - prev_sell_30) / (prev_buy_30 + prev_sell_30).replace(0.0, np.nan)
    out["flow_30_delta"] = out["flow_30"] - prev_flow_30
    out["bid_ask20_ratio"] = bid20 / ask20.replace(0.0, np.nan)
    out["vol_ratio_30m"] = volume / volume.rolling(1800, min_periods=300).mean().replace(0.0, np.nan)
    upper_walk = out["z"].gt(1.0).astype(float).rolling(120, min_periods=60).mean()
    lower_walk = out["z"].lt(-1.0).astype(float).rolling(120, min_periods=60).mean()
    out["bandwalk_signed"] = upper_walk - lower_walk
    out["flow_120_mean"] = out["flow_60"].rolling(120, min_periods=30).mean()
    out["imbalance_60_mean"] = out["imbalance_20"].rolling(60, min_periods=20).mean()
    out["ob_coverage_60"] = out["ob_available"].astype(float).rolling(60, min_periods=60).mean()

    direction = np.sign(out["ret_600s_bps"]).replace(0.0, np.nan)
    data_quality = (
        (out["observed_pct"] >= rules.data_observed_min_pct)
        & out["ob_available"].fillna(False).astype(bool)
        & (out["ob_age_sec"] <= rules.base.orderbook_max_age_sec)
        & (out["ob_coverage_60"] >= rules.orderbook_coverage_min)
    )
    position_ok = np.where(direction > 0.0, out["pos_600s"] >= 0.80, out["pos_600s"] <= 0.20)
    aligned_bandwalk = direction * out["bandwalk_signed"]
    trend_core = (
        data_quality
        & position_ok
        & (out["ret_600s_bps"].abs() >= 20.0)
        & (out["efficiency_600"] >= 0.10)
        & (direction * out["ret_60s_bps"] >= 4.0)
        & (direction * out["imbalance_60_mean"] >= 0.16)
        & (aligned_bandwalk >= 0.0)
        & (aligned_bandwalk <= 0.60)
        & (out["sigma_expand"] <= 1.60)
    )
    trend_votes = pd.DataFrame(
        {
            "slope": direction * out["center_slope_bps"] >= 6.0,
            "long_alignment": direction * out["ret_1800s_bps"] >= 20.0,
            "flow": direction * out["flow_120_mean"] >= 0.04,
        },
        index=out.index,
    ).sum(axis=1)
    normal_slope_max = min(
        rules.base.center_slope_max_bps,
        rules.base.trend_space_center_slope_abs_max_bps,
    )
    normal_state = (
        data_quality
        & (out["inside1_ratio"] >= rules.base.inside_min)
        & (out["center_slope_bps"].abs() <= normal_slope_max)
        & (out["sigma_bps"] >= rules.base.sigma_min_bps)
        & (out["sigma_bps"] <= rules.base.sigma_max_bps)
        & (out["sigma_expand"] <= min(rules.base.sigma_expand_max, rules.base.trend_space_sigma_expand_max))
    )
    trend_formation = trend_core & (trend_votes >= 2)
    out["data_quality_ready"] = data_quality
    out["trend_direction"] = direction
    out["trend_votes"] = trend_votes
    out["state"] = "transition"
    out.loc[normal_state, "state"] = "normal"
    out.loc[trend_formation, "state"] = "trend_formation"
    return out


def passive_book_valid(row, signal: str, rules: LiquidityV2Rules) -> bool:
    sign = 1.0 if signal == "UP" else -1.0
    imbalance = sign * float(row["imbalance_20"])
    micro = sign * float(row["micro_bps"])
    bid = float(row["bid_qty_20"])
    ask = float(row["ask_qty_20"])
    supporting = bid if signal == "UP" else ask
    opposing = ask if signal == "UP" else bid
    wall_change = float(row["bid20_chg_30"] if signal == "UP" else row["ask20_chg_30"])
    values = [imbalance, micro, supporting, opposing, wall_change]
    return bool(
        np.isfinite(values).all()
        and imbalance >= rules.ob_imbalance_min
        and micro >= rules.micro_min_bps
        and supporting >= max(1e-9, opposing * rules.wall_ratio_min)
        and wall_change > -0.55
    )


def trend_entry_ready(row, direction: str) -> bool:
    sign = 1.0 if direction == "UP" else -1.0
    if sign * float(row["imbalance_20"]) < 0.08 or sign * float(row["micro_bps"]) < 0.001:
        return False
    hot_votes = sum(
        (
            sign * float(row["ret_60s_bps"]) >= 18.0,
            sign * float(row["ret_1800s_bps"]) >= 60.0,
            sign * float(row["flow_120_mean"]) >= 0.50,
            float(row["sigma_expand"]) >= 1.25,
        )
    )
    return hot_votes <= 1


def trend_start_score(row) -> dict:
    checks = {
        "multi_period_up": (
            float(row["ret_600s_bps"]) >= 12.0
            and float(row["ret_1800s_bps"]) >= 18.0
            and float(row["ret_3600s_bps"]) >= 20.0
        ),
        "short_accelerating": (
            float(row["ret_15s_bps"]) >= 1.0
            and float(row["ret_30s_bps"]) >= float(row["prev_ret_30s_bps"])
        ),
        "buy_flow_strong": (
            float(row["flow_30"]) >= 0.35
            or float(row["flow_30_delta"]) >= 0.15
        ),
        "book_buy_strong": (
            float(row["imbalance_20"]) >= 0.10
            or float(row["bid_ask20_ratio"]) >= 1.10
        ),
        "not_low_volume": float(row["vol_ratio_30m"]) >= 0.70,
        "not_mature_upper": float(row["pos_600s"]) < 0.95 or float(row["bandwalk_signed"]) < 0.50,
    }
    clean_checks = {key: bool(value) for key, value in checks.items()}
    return {
        "score": int(sum(1 for value in clean_checks.values() if value)),
        "checks": clean_checks,
    }


class NormalTrendLatchEngine:
    def __init__(self, cfg, last_emit_time=None):
        self.cfg = dict(cfg)
        self.rules = RouterRules.from_config(cfg)
        self.last_emit_time = pd.Timestamp(last_emit_time) if last_emit_time is not None else None
        if self.last_emit_time is not None:
            if self.last_emit_time.tzinfo is None:
                self.last_emit_time = self.last_emit_time.tz_localize("UTC")
            self.last_emit_time = self.last_emit_time.tz_convert("UTC")
        self.trend_direction = None
        self.trend_start_time = None
        self.trend_last_time = None
        self.normal_direction = None
        self.normal_band = None
        self.normal_hits = deque()
        self.latched = None

    @staticmethod
    def _timestamp(value):
        if not value:
            return None
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        return timestamp.tz_convert("UTC")

    def restore_state(self, state):
        if not isinstance(state, dict):
            return
        self.trend_direction = state.get("trend_direction") or None
        self.trend_start_time = self._timestamp(state.get("trend_start_time"))
        self.trend_last_time = self._timestamp(state.get("trend_last_time"))
        self.normal_direction = state.get("normal_direction") or None
        self.normal_band = state.get("normal_band") or None
        self.normal_hits = deque(
            timestamp
            for timestamp in (self._timestamp(value) for value in state.get("normal_hits", []))
            if timestamp is not None
        )
        latch = state.get("latched")
        if isinstance(latch, dict) and latch.get("signal"):
            created = self._timestamp(latch.get("created_time"))
            expires = self._timestamp(latch.get("expires_time"))
            if created is not None and expires is not None:
                self.latched = {
                    "kind": str(latch.get("kind") or "normal"),
                    "signal": str(latch["signal"]),
                    "reason": str(latch.get("reason") or "restored_latch"),
                    "band": str(latch.get("band") or "unknown"),
                    "created_time": created,
                    "expires_time": expires,
                }

    def export_state(self):
        def iso(value):
            return None if value is None else pd.Timestamp(value).strftime("%Y-%m-%dT%H:%M:%SZ")

        latch = None
        if self.latched is not None:
            latch = {
                **self.latched,
                "created_time": iso(self.latched["created_time"]),
                "expires_time": iso(self.latched["expires_time"]),
            }
        return {
            "trend_direction": self.trend_direction,
            "trend_start_time": iso(self.trend_start_time),
            "trend_last_time": iso(self.trend_last_time),
            "normal_direction": self.normal_direction,
            "normal_band": self.normal_band,
            "normal_hits": [iso(value) for value in self.normal_hits],
            "latched": latch,
        }

    def _reset_confirmations(self):
        self.trend_direction = None
        self.trend_start_time = None
        self.trend_last_time = None
        self.normal_direction = None
        self.normal_band = None
        self.normal_hits.clear()

    def _latch(self, timestamp, kind, signal, reason, band):
        self.latched = {
            "kind": kind,
            "signal": signal,
            "reason": reason,
            "band": band,
            "created_time": timestamp,
            "expires_time": timestamp + pd.Timedelta(seconds=self.rules.latch_sec),
        }

    def step(self, timestamp, row, *, allow_emit=True):
        timestamp = pd.Timestamp(timestamp)
        band = band_name(float(row["sigma_bps"]))
        params = band_params(self.cfg, band)
        state = str(row["state"])
        direction = "UP" if float(row["trend_direction"]) > 0.0 else "DOWN"
        event = None

        if state == "trend_formation" and band in {"mid", "elevated", "high"}:
            continuous = (
                self.trend_direction == direction
                and self.trend_last_time is not None
                and (timestamp - self.trend_last_time).total_seconds() == 1
            )
            if not continuous:
                self.trend_direction = direction
                self.trend_start_time = timestamp
            self.trend_last_time = timestamp
        else:
            self.trend_direction = None
            self.trend_start_time = None
            self.trend_last_time = None

        candidate = None
        if state != "normal" or not params.enabled:
            self.normal_direction = None
            self.normal_band = None
            self.normal_hits.clear()
        else:
            band_rules = replace(
                self.rules.base,
                z_entry=params.z_entry,
                z_reclaim=params.z_reclaim,
                sigma_min_bps=4.5 if band in {"ultra_low", "low"} else self.rules.base.sigma_min_bps,
            )
            if normal_ready(row, band_rules):
                decision = evaluate_candidate(row, band_rules)
                if decision["status"] == "accepted":
                    candidate = decision

        if candidate is not None:
            candidate_direction = str(candidate["signal"])
            if self.normal_direction is not None and candidate_direction != self.normal_direction:
                if self.latched is not None and self.latched["kind"] == "normal" and self.latched["signal"] != candidate_direction:
                    self.latched = None
                self.normal_hits.clear()
            if self.normal_band is not None and self.normal_band != band:
                self.normal_hits.clear()
            self.normal_direction = candidate_direction
            self.normal_band = band
            self.normal_hits.append(timestamp)
            while self.normal_hits and (timestamp - self.normal_hits[0]).total_seconds() > 30:
                self.normal_hits.popleft()
            confirmed = (
                len(self.normal_hits) >= params.confirm_hits
                and (timestamp - self.normal_hits[0]).total_seconds() >= params.confirm_span_sec
            )
            sign = 1.0 if candidate_direction == "UP" else -1.0
            filtered = (
                sign * float(row["ret_600s_bps"]) >= params.ret600_min_bps
                and sign * float(row["flow_120_mean"]) >= params.flow120_min
            )
            if confirmed and filtered and (self.latched is None or self.latched["kind"] != "trend"):
                startup = trend_start_score(row)
                if (
                    self.rules.startup_skip_enabled
                    and candidate_direction == "DOWN"
                    and startup["score"] >= self.rules.startup_skip_threshold
                ):
                    event = f"startup_skip_{startup['score']}of6"
                    self.normal_direction = None
                    self.normal_band = None
                    self.normal_hits.clear()
                    return {
                        "event": event,
                        "signal": None,
                        "latched": self.latched,
                        "startup_skip": {
                            **startup,
                            "threshold": self.rules.startup_skip_threshold,
                            "blocked_signal": candidate_direction,
                            "blocked_reason": str(candidate["reason"]),
                        },
                    }
                self._latch(timestamp, "normal", candidate_direction, str(candidate["reason"]), band)
                event = "latched"

        trend_confirmed = (
            self.trend_direction is not None
            and self.trend_start_time is not None
            and (timestamp - self.trend_start_time).total_seconds() >= self.rules.trend_confirm_sec
            and trend_entry_ready(row, self.trend_direction)
        )
        if trend_confirmed:
            if self.latched is not None and self.latched["signal"] != self.trend_direction:
                self.latched = None
            self._latch(timestamp, "trend", self.trend_direction, "fine_vol_trend_formation", band)
            event = "latched"

        if self.latched is not None and timestamp > self.latched["expires_time"]:
            self.latched = None
            event = "expired"

        interval = max(1, self.rules.execution_interval_sec)
        if int(timestamp.timestamp()) % interval != self.rules.execution_phase % interval:
            return {"event": event, "signal": None, "latched": self.latched}
        if self.latched is None or not allow_emit:
            return {"event": event, "signal": None, "latched": self.latched}
        if not bool(row["data_quality_ready"]):
            return {"event": "execution_data_not_ready", "signal": None, "latched": self.latched}
        if self.last_emit_time is not None:
            elapsed = (timestamp - self.last_emit_time).total_seconds()
            if elapsed < self.rules.base.min_gap_sec:
                return {"event": "cooldown", "signal": None, "latched": self.latched}
        signal = dict(self.latched)
        signal["time"] = timestamp
        signal["delay_sec"] = int((timestamp - signal["created_time"]).total_seconds())
        self.last_emit_time = timestamp
        self.latched = None
        self._reset_confirmations()
        return {"event": "emitted", "signal": signal, "latched": None}
