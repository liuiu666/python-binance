from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

import research_orderflow_confirmation_v30 as subject  # noqa: E402


def _zip(path: Path, *, header: bool) -> None:
    columns = [
        "agg_trade_id",
        "price",
        "quantity",
        "first_trade_id",
        "last_trade_id",
        "transact_time",
        "is_buyer_maker",
    ]
    lines = []
    if header:
        lines.append(",".join(columns))
    lines.extend(
        [
            "1,100,2,1,1,1577836800000,true",
            "2,99,1,2,2,1577836805000,false",
        ]
    )
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("ticks.csv", "\n".join(lines) + "\n")


@pytest.mark.parametrize("header", [False, True])
def test_orderflow_archive_parser_keeps_quantity_and_side(
    tmp_path: Path, header: bool
) -> None:
    path = tmp_path / f"flow-{header}.zip"
    _zip(path, header=header)
    rows = subject._read_orderflow_archive(
        {"path": str(path), "date": "2020-01-01"},
        [(1577836800000, 1577836805000)],
    )
    assert rows["quantity"].astype(float).tolist() == [2.0, 1.0]
    assert rows["is_buyer_maker"].astype(str).str.lower().tolist() == [
        "true",
        "false",
    ]


def test_features_compute_taker_imbalance() -> None:
    start = pd.Timestamp("2020-01-01T00:00:00Z")
    signals = pd.DataFrame(
        {
            "candidate_id": ["h1"],
            "signal_time": [start],
            "period": ["development_2020_2022"],
        }
    )
    flow = pd.DataFrame(
        {
            "time": [start, start + pd.Timedelta(seconds=1)],
            "quantity": [3.0, 1.0],
            "is_buyer_maker": [True, False],
        }
    )
    price_ticks = pd.DataFrame(
        {
            "time": pd.date_range(start, periods=7, freq="s"),
            "price": [100.0, 100.0, 100.0, 100.0, 100.0, 99.0, 99.0],
        }
    )
    result = subject.build_features(signals, flow, price_ticks).iloc[0]
    assert result["sell_taker_qty_5s"] == 3.0
    assert result["buy_taker_qty_5s"] == 1.0
    assert result["flow_imbalance_5s"] == -0.5
    assert result["price_change_5s_bps"] == pytest.approx(-100.0)


def test_development_selector_requires_every_execution_slice() -> None:
    good = {
        "pnlU": 10.0,
        "winRatePct": 65.0,
        "expectedValueU": 0.5,
        "wilson95LowerPct": 56.0,
        "bootstrap": {"lower90EvU": 0.1},
        "maxDrawdownU": 20.0,
        "maxLossStreak": 2,
    }
    development = {
        "commonCoverageSignals": 60,
        "slices": {f"s{i}": dict(good) for i in range(6)},
    }
    results = {
        rule: {"development_2020_2022": development}
        for rule, _, _ in subject.RULES
    }
    selection = subject.select_development_rule(results)
    assert selection is not None
    assert selection["rule"] == "flow_le_0"
    bad = {
        **development,
        "slices": {**development["slices"], "s0": {**good, "pnlU": -1.0}},
    }
    results = {
        rule: {"development_2020_2022": bad}
        for rule, _, _ in subject.RULES
    }
    assert subject.select_development_rule(results) is None

