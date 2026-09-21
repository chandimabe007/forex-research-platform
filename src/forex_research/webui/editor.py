"""Local configuration editor — stdlib only, localhost-bound.

A form-based editor for the declared objective (PROD-010) and the firm
challenge rules (CHAL-010). On save it writes **only** the two fixed files
``config/objective.yaml`` and ``config/challenge_rules.yaml``, validates
through the same loaders the rest of the platform uses (so a save that
produces an invalid configuration is refused with the loader's own errors),
and reports research readiness live.

    python -m forex_research.webui.editor             # http://127.0.0.1:8788
    python -m forex_research.webui.editor --port 9001

It never asks for or stores account credentials, and binds to 127.0.0.1.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from forex_research.config.loader import ConfigError, load_challenge_rules, load_objective
from forex_research.config.schemas import RuleStatus

_INDEX = Path(__file__).resolve().parent / "editor.html"
_REPO = Path(__file__).resolve().parents[3]
_CONFIG_DIR = _REPO / "config"
_OBJECTIVE = _CONFIG_DIR / "objective.yaml"
_RULES = _CONFIG_DIR / "challenge_rules.yaml"
_MAX_BODY_BYTES = 1_048_576  # 1 MiB is far beyond any legitimate form payload


def readiness() -> dict:
    """Research readiness: a complete objective (GATE-031) and no unknown rules (CHAL-011)."""
    checks: list[dict] = []
    try:
        objective = load_objective(_OBJECTIVE)
        checks.append(
            {
                "name": "objective declared and valid (PROD-010)",
                "ready": objective.is_complete(),
                "detail": "complete"
                if objective.is_complete()
                else "template incomplete (GATE-031)",
            }
        )
    except Exception as exc:  # noqa: BLE001 — readiness is a diagnostic; report, never raise
        checks.append(
            {"name": "objective declared and valid (PROD-010)", "ready": False, "detail": str(exc)}
        )
    try:
        rules = load_challenge_rules(_RULES)
        unknown = [
            f"{phase.name}.{rule.name}"
            for phase in rules.phases.values()
            for rule in phase.rules.values()
            if rule.status is RuleStatus.UNKNOWN
        ]
        checks.append(
            {
                "name": "all challenge rules verified (CHAL-011)",
                "ready": not unknown,
                "detail": "all verified" if not unknown else f"unknown: {', '.join(unknown)}",
            }
        )
    except Exception as exc:  # noqa: BLE001 — readiness is a diagnostic; report, never raise
        checks.append(
            {"name": "all challenge rules verified (CHAL-011)", "ready": False, "detail": str(exc)}
        )
    return {"ready": all(c["ready"] for c in checks), "checks": checks}


def _load_yaml(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


_NUMERIC_OBJECTIVE_FIELDS = (
    "target_annual_return",
    "volatility_budget",
    "account_size",
    "max_fee_budget",
    "min_acceptable_env",
)


def _save_objective(body: dict) -> dict:
    # The loader validates these as numbers; text that cannot parse is
    # refused with a named-field message rather than coerced or crashed on.
    for key in _NUMERIC_OBJECTIVE_FIELDS:
        value = body.get(key)
        if isinstance(value, str):
            try:
                float(value)
            except ValueError:
                return {"ok": False, "error": f"{key}: '{value}' is not a number"}
    return _write_validated(_OBJECTIVE, {"objective": body}, load_objective)


def _save_rules(body: dict) -> dict:
    return _write_validated(_RULES, body, load_challenge_rules)


def _write_validated(path: Path, data: dict, validator) -> dict:
    """Write, validate through the platform's loader, roll back on failure.

    The save is only kept if the same loader the research entry points use
    accepts it — a rejected save must never leave an invalid configuration
    on disk. A loader crash (say, a TypeError deep in validation) rolls back
    too and surfaces as a refused save: the server must never die mid-request
    or persist a configuration the engine itself cannot load.
    """
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    _write_yaml(path, data)
    try:
        validator(path)
    except ConfigError as exc:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(previous, encoding="utf-8")
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — roll back and report, never persist
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(previous, encoding="utf-8")
        return {"ok": False, "error": f"validation crashed ({type(exc).__name__}: {exc})"}
    return {"ok": True, "wrote": str(path)}


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: object) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802 — stdlib handler name
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, _INDEX.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/config":
            self._json(
                200,
                {
                    "objective": _load_yaml(_OBJECTIVE),
                    "rules": _load_yaml(_RULES),
                    "readiness": readiness(),
                },
            )
        elif path == "/api/readiness":
            self._json(200, readiness())
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 — stdlib handler name
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", 0))
        if length > _MAX_BODY_BYTES:
            self._json(413, {"ok": False, "error": "payload too large"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "invalid JSON"})
            return
        if path == "/api/objective":
            result = _save_objective(body)
        elif path == "/api/rules":
            result = _save_rules(body)
        else:
            self._send(404, b"not found", "text/plain")
            return
        result["readiness"] = readiness()
        self._json(200, result)

    def log_message(self, *args) -> None:  # quiet
        return


def serve(host: str = "127.0.0.1", port: int = 8788) -> None:
    httpd = ThreadingHTTPServer((host, port), _Handler)
    print(f"config editor: http://{host}:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Forex research configuration editor")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args(argv)
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
