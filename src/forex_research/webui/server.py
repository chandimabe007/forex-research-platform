"""Local status dashboard — stdlib only, localhost-bound, read-only.

    python -m forex_research.webui.server             # http://127.0.0.1:8787
    python -m forex_research.webui.server --port 9000

Serves one HTML page and one JSON snapshot of the platform's local evidence:
probe and canary records, tick-capture coverage, episode counts, fee
schedules with provenance. It places no orders and binds to 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .aggregate import build_status_snapshot

_INDEX = Path(__file__).resolve().parent / "index.html"


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — stdlib handler name
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, _INDEX.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/status":
            try:
                payload = json.dumps(build_status_snapshot(), default=str).encode()
                self._send(200, payload, "application/json")
            except Exception as exc:  # noqa: BLE001 — report, never crash the server
                body = json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode()
                self._send(500, body, "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *args) -> None:  # quiet: no per-request stderr spam
        return


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    httpd = ThreadingHTTPServer((host, port), _Handler)
    print(f"status dashboard: http://{host}:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Forex research status dashboard (read-only)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
