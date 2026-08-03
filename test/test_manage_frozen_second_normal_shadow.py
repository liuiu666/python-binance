import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "py" / "manage_frozen_second_normal_shadow.py"
SPEC = importlib.util.spec_from_file_location("manage_frozen_second_normal_shadow", MODULE_PATH)
shadow = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(shadow)


def test_compare_variant_reports_dropped_research_fields():
    target = {
        "id": "frozen",
        "enabled": True,
        "tradeEnabled": False,
        "v9SupplementMinAbsNormalZ": 0.7,
        "trendSpaceEnabled": True,
    }
    remote = {
        "id": "legacy",
        "enabled": True,
        "tradeEnabled": False,
        "v9SupplementMinAbsNormalZ": 0,
        "trendSpaceEnabled": False,
    }
    mismatches = shadow.compare_variant(target, remote)
    assert any("id:" in row for row in mismatches)
    assert any("v9SupplementMinAbsNormalZ" in row for row in mismatches)
    assert any("trendSpaceEnabled" in row for row in mismatches)


def test_deployment_variant_uses_frozen_server_alias_without_enabling_trading():
    target = {"id": "local-shadow", "label": "local", "enabled": True, "tradeEnabled": False}
    manifest = {"serverStrategyId": "server-v13", "serverLabel": "frozen alias"}

    deployed = shadow.deployment_variant(target, manifest)

    assert deployed["id"] == "server-v13"
    assert deployed["label"] == "frozen alias"
    assert deployed["enabled"] is True
    assert deployed["tradeEnabled"] is False


def test_retired_30m_candidate_is_detected_for_removal():
    config = {
        "strategyVariants": [
            {"id": "BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V13_FREQ"},
            {"id": "BTC_30min_SHADOW_CANDIDATE", "enabled": False},
        ]
    }
    assert [row["id"] for row in shadow.retired_rows(config)] == [
        "BTC_30min_SHADOW_CANDIDATE"
    ]


def test_activate_rolls_back_when_server_normalizes_frozen_variant(monkeypatch):
    target = {
        "id": "BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V13_SHADOW",
        "enabled": True,
        "tradeEnabled": False,
        "v9SupplementMinAbsNormalZ": 0.7,
    }
    before = {
        "realTradingEnabled": False,
        "strategyVariants": [
            {"id": "BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V13_FREQ", "enabled": False, "tradeEnabled": False},
            {"id": "other", "enabled": True, "tradeEnabled": False},
        ],
    }
    normalized = {
        "realTradingEnabled": False,
        "strategyVariants": [
            {"id": "BTC_10min_NORMAL_LIQ_OB_V2_AUGMENTED_V13_FREQ", "enabled": True, "tradeEnabled": False},
            {"id": "other", "enabled": True, "tradeEnabled": False},
        ],
    }
    responses = iter([before, normalized, before])
    posts = []
    monkeypatch.setattr(shadow, "load_frozen", lambda: (target, {}))
    monkeypatch.setattr(shadow, "request_json", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(shadow, "login_token", lambda _base: "token")
    monkeypatch.setattr(shadow, "post_variants", lambda _base, variants, _token: posts.append(variants) or {})

    result = shadow.activate("http://example.test")

    assert result["activated"] is False
    assert result["rolledBack"] is True
    assert len(posts) == 2
    assert posts[-1] == before["strategyVariants"]
