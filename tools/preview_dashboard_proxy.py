"""Serve the built dashboard locally while proxying read-only API calls.

Used for visual review of a local frontend build against a remote dashboard API.
Only GET/HEAD requests are proxied; write requests are rejected.
"""

from __future__ import annotations

import argparse
import http.server
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PreviewHandler(http.server.SimpleHTTPRequestHandler):
    target = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "public"), **kwargs)

    def _proxy(self, include_body: bool) -> None:
        request = urllib.request.Request(f"{self.target}{self.path}", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read()
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if include_body:
                    self.wfile.write(body)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Type", exc.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if include_body:
                self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
        if self.path.startswith("/api/"):
            self._proxy(include_body=True)
            return
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib hook name
        if self.path.startswith("/api/"):
            self._proxy(include_body=False)
            return
        super().do_HEAD()

    def do_POST(self) -> None:  # noqa: N802 - preview must be read-only
        self.send_error(405, "Local preview proxy is read-only")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--target", default="http://115.190.218.128:3000")
    args = parser.parse_args()
    PreviewHandler.target = args.target.rstrip("/")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), PreviewHandler)
    print(f"Preview: http://127.0.0.1:{args.port}/dashboard/ -> {PreviewHandler.target}")
    server.serve_forever()


if __name__ == "__main__":
    main()
