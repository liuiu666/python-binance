from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_frozen_reverse_history_v27 as subject  # noqa: E402


def test_status_and_pnl_matches_binary_contract() -> None:
    assert subject._status_and_pnl(0.1) == ("won", 4.0)
    assert subject._status_and_pnl(-0.1) == ("lost", -5.0)
    assert subject._status_and_pnl(0.0) == ("tie", 0.0)


def test_verify_input_checks_hash_and_continuity(tmp_path: Path) -> None:
    data = tmp_path / "minutes.csv"
    data.write_text("frozen\n", encoding="utf-8")
    digest = hashlib.sha256(data.read_bytes()).hexdigest()
    manifest = tmp_path / "minutes.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "sha256": digest,
                "audit": {
                    "rows": 2_103_840,
                    "missingMinutes": 0,
                    "duplicateMinutes": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    result = subject.verify_input(data, manifest)
    assert result["sha256"] == digest


def _winning_frame() -> pd.DataFrame:
    rows = []
    for month in pd.period_range("2020-01", "2023-12", freq="M"):
        base = month.start_time.tz_localize("UTC")
        for offset in range(3):
            signal_time = base + pd.Timedelta(days=offset)
            rows.append(
                {
                    "profile": "synthetic",
                    "signal_time": signal_time,
                    "signal": "DOWN",
                    "status_h10_d0": "won",
                    "pnl_u_h10_d0": 4.0,
                    "status_h10_d1": "won",
                    "pnl_u_h10_d1": 4.0,
                    "status_h10_fixed_d1": "won",
                    "pnl_u_h10_fixed_d1": 4.0,
                    "entry_time_h10_fixed_d1": signal_time
                    + pd.Timedelta(minutes=1),
                    "settle_time_h10_fixed_d1": signal_time
                    + pd.Timedelta(minutes=10),
                }
            )
    return pd.DataFrame(rows)


def test_summary_requires_all_three_execution_modes() -> None:
    frame = _winning_frame()
    passed = subject.summarize(frame, "synthetic")
    assert passed["passed"] is True
    broken = frame.copy()
    broken["status_h10_d1"] = "lost"
    broken["pnl_u_h10_d1"] = -5.0
    failed = subject.summarize(broken, "synthetic")
    assert failed["executions"]["exact"]["passed"] is True
    assert failed["executions"]["delayed"]["passed"] is False
    assert failed["passed"] is False


def test_h2_entry_and_settlement_alignment(monkeypatch) -> None:
    index = pd.date_range("2020-01-01T00:00:00Z", periods=100, freq="min")
    opens = np.arange(100, dtype=float) + 100.0
    minutes = pd.DataFrame(
        {
            "open": opens,
            "high": opens + 1.0,
            "low": opens - 1.0,
            "close": opens + 0.5,
            "volume": 1.0,
        },
        index=index,
    )
    volatility = pd.DataFrame({"vol_state": "mid"}, index=index)

    boundary = np.zeros(len(index), dtype=bool)
    boundary[20] = True
    monkeypatch.setattr(subject, "_boundary_mask", lambda _: boundary)

    def fake_arrays(minutes_arg, lookback, positions):
        assert lookback == 10
        assert positions.tolist() == [20]
        return {
            f"{subject.EXHAUSTION_REVERSAL}|{subject.H2_THRESHOLD}": (
                np.array([False]),
                np.array([True]),
                np.array([2.1]),
            )
        }

    monkeypatch.setattr(subject, "_family_signal_arrays", fake_arrays)
    result = subject.generate_h2_candidates(minutes, volatility)
    assert len(result) == 1
    row = result.iloc[0]
    assert row["signal"] == "DOWN"
    assert row["signal_time"] == index[21]
    assert row["entry_time_h10_d0"] == index[21]
    assert row["settle_time_h10_d0"] == index[31]
    assert row["entry_time_h10_d1"] == index[22]
    assert row["settle_time_h10_d1"] == index[32]
    assert row["settle_time_h10_fixed_d1"] == index[31]
