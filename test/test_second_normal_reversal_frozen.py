import importlib.util
import json
from pathlib import Path

import pandas as pd


MODULE_PATH = Path(__file__).resolve().parents[1] / "py" / "research_second_normal_reversal_frozen.py"
SPEC = importlib.util.spec_from_file_location("research_second_normal_reversal_frozen", MODULE_PATH)
research = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(research)


def test_union_hours_deduplicates_overlapping_snapshots():
    intervals = [
        (pd.Timestamp("2026-07-01T00:00:00Z"), pd.Timestamp("2026-07-01T02:00:00Z")),
        (pd.Timestamp("2026-07-01T01:00:00Z"), pd.Timestamp("2026-07-01T03:00:00Z")),
        (pd.Timestamp("2026-07-02T00:00:00Z"), pd.Timestamp("2026-07-02T01:00:00Z")),
    ]
    assert research.union_hours(intervals) == 4.0


def test_find_variant_requires_exact_frozen_strategy_id():
    config = {
        "strategyVariants": [
            {"id": "BTC_30min_SHADOW_CANDIDATE"},
            {"id": research.STRATEGY_ID, "tradeEnabled": False},
        ]
    }
    assert research.find_variant(config)["tradeEnabled"] is False


def test_frozen_manifest_matches_current_shadow_variant_and_core():
    root = MODULE_PATH.parents[1]
    config = json.loads((root / "data" / "trade_config.json").read_text(encoding="utf-8-sig"))
    manifest = json.loads(research.MANIFEST_PATH.read_text(encoding="utf-8"))
    variant = research.find_variant(config)

    assert variant["tradeEnabled"] is False
    assert research.object_sha256(variant) == manifest["variantSha256"]
    assert research.sha256(root / "py" / "current_v2_augmented_v9_core.py") == manifest["sharedCoreSha256"]
