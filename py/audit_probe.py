"""Probe the trade-audit endpoint without faking AutoJS liveness."""
import json
import time
import urllib.request

URL = "http://127.0.0.1:3000/api/trade-audit"


def main():
    payload = {
        "event": "codex_probe",
        "clientTime": int(time.time() * 1000),
        "note": "audit endpoint probe; not counted as AutoJS liveness",
    }
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = resp.read().decode("utf-8")
    print(body)


if __name__ == "__main__":
    main()
