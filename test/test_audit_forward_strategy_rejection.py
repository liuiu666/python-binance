import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "py" / "audit_forward_strategy_rejection.py"
SPEC = importlib.util.spec_from_file_location("audit_forward_strategy_rejection", MODULE_PATH)
audit = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(audit)


def test_signal_snapshot_excludes_unrelated_strategies():
    payload = {
        "ok": True,
        "data": {
            "BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V9": {"signal": "UP"},
            "BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V13_FREQ": {"signal": None},
            "BTC_30min_SHADOW_CANDIDATE": {"signal": "DOWN"},
            "_snapshot_time": "ignored metadata",
        },
    }

    target = audit.signal_snapshot(payload)
    all_strategies = audit.signal_snapshot(payload, target_only=False)

    assert [row["strategyId"] for row in target] == [
        "BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V9",
        "BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V13_FREQ",
    ]
    assert {row["strategyId"] for row in all_strategies} == {
        "BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V9",
        "BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V13_FREQ",
        "BTC_30min_SHADOW_CANDIDATE",
    }


def test_target_strategy_ids_do_not_include_30_minute_candidate():
    assert "BTC_30min_SHADOW_CANDIDATE" not in audit.TARGET_STRATEGY_IDS
