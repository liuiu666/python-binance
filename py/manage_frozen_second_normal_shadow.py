"""Audit or safely activate the frozen V13 second-normal shadow strategy.

The default command is read-only.  ``--activate`` replaces any older V13
variant, posts the frozen shadow configuration, verifies every research field,
and rolls the previous strategy list back if the server normalizes or drops a
field.  It never enables real trading.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "data" / "trade_config.json"
MANIFEST_PATH = ROOT / "data" / "frozen_second_normal_reversal_v13.json"
DEFAULT_BASE_URL = "http://115.190.218.128:3000"

IGNORED_COMPARISON_FIELDS = {"backtest", "label", "role", "observationMode"}


def request_json(url: str, *, method: str = "GET", body: Any = None, token: str | None = None) -> Any:
    payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def load_frozen() -> tuple[dict[str, Any], dict[str, Any]]:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    target = next(
        (row for row in config.get("strategyVariants", []) if row.get("id") == manifest["strategyId"]),
        None,
    )
    if target is None:
        raise RuntimeError("Frozen strategy is missing from local trade_config.json")
    if target.get("enabled") is not True or target.get("tradeEnabled") is not False:
        raise RuntimeError("Frozen local strategy must be enabled for observation and disabled for real trading")
    return target, manifest


def deployment_variant(target: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    deployed = dict(target)
    deployed["id"] = manifest.get("serverStrategyId") or target["id"]
    if manifest.get("serverLabel"):
        deployed["label"] = manifest["serverLabel"]
    return deployed


def comparable_fields(target: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in target.items()
        if key not in IGNORED_COMPARISON_FIELDS
    }


def compare_variant(target: dict[str, Any], remote: dict[str, Any] | None) -> list[str]:
    if remote is None:
        return ["strategy_missing"]
    mismatches = []
    for key, expected in comparable_fields(target).items():
        actual = remote.get(key)
        if actual != expected:
            mismatches.append(f"{key}: expected={expected!r}, actual={actual!r}")
    return mismatches


def v13_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        row for row in config.get("strategyVariants", [])
        if "NORMAL_LIQ_OB_V2_AUGMENTED_V13" in str(row.get("id", ""))
    ]


def audit(base_url: str) -> dict[str, Any]:
    target, manifest = load_frozen()
    deployed = deployment_variant(target, manifest)
    remote = request_json(f"{base_url.rstrip('/')}/api/config")
    exact = next((row for row in remote.get("strategyVariants", []) if row.get("id") == deployed["id"]), None)
    mismatches = compare_variant(deployed, exact)
    legacy = [row.get("id") for row in v13_rows(remote) if row.get("id") != deployed["id"]]
    safe = remote.get("realTradingEnabled") is False
    trade_enabled_ids = [
        row.get("id") for row in remote.get("strategyVariants", [])
        if row.get("enabled") is not False and row.get("tradeEnabled") is not False
    ]
    return {
        "strategyId": target["id"],
        "serverStrategyId": deployed["id"],
        "ready": safe and not trade_enabled_ids and not mismatches and exact.get("enabled") is True and exact.get("tradeEnabled") is False,
        "realTradingEnabled": remote.get("realTradingEnabled"),
        "tradeEnabledStrategyIds": trade_enabled_ids,
        "mismatches": mismatches,
        "legacyV13Ids": legacy,
        "manifestStatus": manifest.get("status"),
    }


def login_token(base_url: str) -> str:
    direct = os.environ.get("SHADOW_API_TOKEN") or os.environ.get("API_TOKEN")
    if direct:
        return direct
    username = os.environ.get("SHADOW_API_USERNAME", "sl")
    password = os.environ.get("SHADOW_API_PASSWORD")
    if not password:
        raise RuntimeError("Set SHADOW_API_PASSWORD or SHADOW_API_TOKEN before --activate")
    result = request_json(
        f"{base_url.rstrip('/')}/api/login",
        method="POST",
        body={"username": username, "password": password},
    )
    token = result.get("token")
    if not token:
        raise RuntimeError("Login did not return an API token")
    return str(token)


def post_variants(base_url: str, variants: list[dict[str, Any]], token: str) -> dict[str, Any]:
    return request_json(
        f"{base_url.rstrip('/')}/api/config",
        method="POST",
        body={"strategyVariants": variants},
        token=token,
    )


def activate(base_url: str) -> dict[str, Any]:
    target, manifest = load_frozen()
    deployed = deployment_variant(target, manifest)
    base_url = base_url.rstrip("/")
    before = request_json(f"{base_url}/api/config")
    if before.get("realTradingEnabled") is not False:
        raise RuntimeError("Refusing activation because server realTradingEnabled is not false")
    previous_variants = list(before.get("strategyVariants") or [])
    preserved = [
        {**row, "tradeEnabled": False}
        for row in previous_variants if row not in v13_rows(before)
    ]
    proposed = [deployed, *preserved]
    token = login_token(base_url)
    post_variants(base_url, proposed, token)
    after = request_json(f"{base_url}/api/config")
    exact = next((row for row in after.get("strategyVariants", []) if row.get("id") == deployed["id"]), None)
    mismatches = compare_variant(deployed, exact)
    trade_enabled_ids = [
        row.get("id") for row in after.get("strategyVariants", [])
        if row.get("enabled") is not False and row.get("tradeEnabled") is not False
    ]
    unsafe = after.get("realTradingEnabled") is not False or trade_enabled_ids or mismatches
    if unsafe:
        post_variants(base_url, previous_variants, token)
        rolled_back = request_json(f"{base_url}/api/config")
        return {
            "activated": False,
            "rolledBack": True,
            "mismatches": mismatches,
            "realTradingEnabled": rolled_back.get("realTradingEnabled"),
        }
    return {
        "activated": True,
        "rolledBack": False,
        "mismatches": [],
        "realTradingEnabled": after.get("realTradingEnabled"),
        "strategyId": target["id"],
        "serverStrategyId": deployed["id"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--activate", action="store_true")
    args = parser.parse_args()
    try:
        result = activate(args.base_url) if args.activate else audit(args.base_url)
    except (RuntimeError, urllib.error.URLError, TimeoutError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    if args.activate:
        return 0 if result.get("activated") else 1
    return 0 if result.get("ready") else 1


if __name__ == "__main__":
    sys.exit(main())
