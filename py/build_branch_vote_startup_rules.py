"""Build fixed rules for the independent branch-vote startup strategy."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py"))

from branch_vote_startup_core import (  # noqa: E402
    BranchVoteStartupConfig,
    add_lag_features,
    clean_json,
    compile_rules,
    save_rules,
)
from research_normal_liquidity_orderbook import load_local_data  # noqa: E402
from research_parameter_stability_audit import SOURCES  # noqa: E402
from research_top_exhaustion_confirmation import EXTRA_SOURCES  # noqa: E402
from branch_vote_startup_core import build_minute_snapshots  # noqa: E402


DEFAULT_OUT = ROOT / "data" / "branch_vote_startup_rules.json"


def source_tuple(include_extra: bool):
    return tuple(SOURCES) + (tuple(EXTRA_SOURCES) if include_extra else tuple())


def run(args) -> dict:
    cfg = BranchVoteStartupConfig()
    frames = []
    info = {}
    for source_name, seconds, orderbook in source_tuple(args.include_extra):
        data = load_local_data(Path(seconds), Path(orderbook))
        frame = build_minute_snapshots(data, str(source_name), cfg, include_future=True)
        frames.append(frame)
        info[str(source_name)] = {
            "seconds": str(seconds),
            "orderbook": str(orderbook),
            "start": data.index.min(),
            "end": data.index.max(),
            "hours": round((data.index.max() - data.index.min()).total_seconds() / 3600.0, 4),
            "snapshots": int(len(frame)),
        }
    snapshots = add_lag_features(pd.concat(frames, ignore_index=True))
    compiled = compile_rules(snapshots)
    metadata = {
        "method": "fixed balanced branch-vote rules for second_branch_vote_startup_v1",
        "includeExtra": bool(args.include_extra),
        "sources": info,
        "snapshots": int(len(snapshots)),
        "layers": [
            {
                "layer": layer["layer"],
                "keys": layer["keys"],
                "rules": len(layer["rules"]),
            }
            for layer in compiled
        ],
    }
    save_rules(args.out, compiled, metadata)
    return {"out": str(Path(args.out).resolve()), "metadata": metadata}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--include-extra", action="store_true", help="include 2026-07-08 extra source")
    args = parser.parse_args()
    print(json.dumps(clean_json(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

