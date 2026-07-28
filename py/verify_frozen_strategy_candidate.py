"""Verify that a frozen research candidate still matches its source hashes."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "manifest",
        nargs="?",
        default=str(ROOT / "data" / "frozen_exhaustion_orderbook_v6.json"),
    )
    args = parser.parse_args()
    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = []
    valid = True
    for relative, expected in manifest["sourceHashesSha256"].items():
        path = ROOT / relative
        actual = sha256(path) if path.exists() else None
        matched = actual == expected
        valid = valid and matched
        rows.append({"file": relative, "expected": expected, "actual": actual, "matched": matched})
    output = {
        "strategyId": manifest["strategyId"],
        "status": manifest["status"],
        "manifest": str(manifest_path),
        "valid": valid,
        "files": rows,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if not valid:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
