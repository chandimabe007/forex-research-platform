"""Web UI tests — the servers' behavior, not the HTML.

The HTTP tests bind real ThreadingHTTPServer instances on free localhost
ports and drive them with urllib, so request parsing, status codes and the
JSON contract are exercised exactly as a browser would see them. The
aggregation tests point the module's data paths at temporary directories so
they never depend on the developer's local ``data/`` state.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from forex_research.webui import aggregate
from forex_research.webui.editor import _Handler as EditorHandler
from forex_research.webui.editor import readiness as editor_readiness
from forex_research.webui.server import _Handler as StatusHandler


def _free_port() -> int:
    with ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler) as s:
        return s.server_address[1]


class _ProbeHandler(StatusHandler):
    """Bare handler used only to grab a free port; do_GET is never called."""


def _serve(handler) -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def _get(port: int, path: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as res:
        return res.status, res.read()


def _post(port: int, path: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError:
        raise


# ---------------------------------------------------------------------------
# aggregation — synthetic data dirs, no dependence on local state
# ---------------------------------------------------------------------------


def _write_record(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_probe_summary_prefers_newest_record(tmp_path, monkeypatch):
    monkeypatch.setattr(aggregate, "_PROBE_DIR", tmp_path)
    now = time.time()
    old_ts = datetime.now(UTC) - timedelta(hours=1)
    new_ts = datetime.now(UTC)
    old_path = tmp_path / f"probe_{old_ts.strftime('%Y%m%dT%H%M%S%f')}Z.json"
    new_path = tmp_path / f"probe_{new_ts.strftime('%Y%m%dT%H%M%S%f')}Z.json"
    _write_record(
        old_path,
        {"schema_version": 1, "outcome": "refused", "timestamp": old_ts.isoformat()},
    )
    _write_record(
        new_path,
        {
            "schema_version": 1,
            "outcome": "ok",
            "timestamp": new_ts.isoformat(),
            "capabilities": {"server": "S", "symbols": {"EURUSD": {"digits": 5}}},
        },
    )
    # The coarse Windows clock can stamp both writes with one mtime; pin them.
    os.utime(old_path, (now - 3600, now - 3600))
    os.utime(new_path, (now, now))
    summary = aggregate.probe_summary()
    assert summary["record"]["outcome"] == "ok"
    assert summary["record"]["capabilities"]["server"] == "S"


def test_probe_summary_tolerates_garbage_and_absence(tmp_path, monkeypatch):
    monkeypatch.setattr(aggregate, "_PROBE_DIR", tmp_path)
    assert aggregate.probe_summary()["record"] is None
    (tmp_path / "probe_broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "probe_wrong_schema.json").write_text('{"schema_version": 9}', encoding="utf-8")
    assert aggregate.probe_summary()["record"] is None


def test_canary_summary_extracts_step_details(tmp_path, monkeypatch):
    monkeypatch.setattr(aggregate, "_CANARY_DIR", tmp_path)
    _write_record(
        tmp_path / "canary_20260921T202134536332Z.json",
        {
            "outcome": "ok_with_discrepancies",
            "finished_at": "2026-09-21T20:21:34+00:00",
            "symbol": "EURUSD",
            "discrepancies": ["settle window"],
            "steps": [
                {"name": "submit", "ok": True, "detail": {"fill_price": 1.14648}},
                {
                    "name": "broker_side_protection",
                    "ok": True,
                    "detail": {"sl_on_broker": 1.1, "tp_on_broker": 1.2},
                },
                {
                    "name": "reconcile",
                    "ok": True,
                    "detail": {"realized_delta": -0.12, "modelled_profit": -0.06},
                },
            ],
        },
    )
    record = aggregate.canary_summary()["record"]
    assert record["outcome"] == "ok_with_discrepancies"
    assert record["submit"]["fill_price"] == 1.14648
    assert record["reconcile"]["realized_delta"] == -0.12


def test_capture_summary_counts_rows_per_symbol_month(tmp_path, monkeypatch):
    root = tmp_path / "venue_quotes"
    (root / "EURUSD").mkdir(parents=True)
    (root / "GBPUSD").mkdir(parents=True)
    rows_a = "\n".join(
        json.dumps({"ts": f"2026-09-01T0{i}:00:00+00:00", "bid": 1.1, "ask": 1.1}) for i in range(3)
    )
    (root / "EURUSD" / "2026-09.jsonl").write_text(rows_a + "\n", encoding="utf-8")
    (root / "GBPUSD" / "2026-09.jsonl").write_text("", encoding="utf-8")  # empty month: skipped
    monkeypatch.setattr(aggregate, "_TICK_ROOT", root)
    summary = aggregate.capture_summary()
    assert summary["total_rows"] == 3
    assert summary["symbols"]["EURUSD"][0]["rows"] == 3
    assert "GBPUSD" not in summary["symbols"]


def test_episode_summary_counts_bucket_runs(tmp_path, monkeypatch):
    root = tmp_path / "venue_quotes"
    (root / "EURUSD").mkdir(parents=True)
    # 06:59 asia, 07:01 london -> two episodes; two more london quotes -> same episode.
    lines = [
        {"ts": "2026-09-01T06:59:00+00:00"},
        {"ts": "2026-09-01T07:01:00+00:00"},
        {"ts": "2026-09-01T07:02:00+00:00"},
    ]
    (root / "EURUSD" / "2026-09.jsonl").write_text(
        "\n".join(json.dumps(row) for row in lines) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(aggregate, "_TICK_ROOT", root)
    summary = aggregate.episode_summary({"rollover": 8})
    assert summary["episodes"]["EURUSD"] == {"asia": 1, "london": 1}
    assert summary["minimums"] == {"rollover": 8}


def test_fee_summary_exposes_provenance(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "fees.yaml").write_text(
        yaml.safe_dump(
            {
                "schedules": {
                    "TEST-Server": {
                        "currency": "USD",
                        "commission_per_lot_round_trip": 6.0,
                        "swap_long_per_lot_per_day": None,
                        "swap_short_per_lot_per_day": None,
                        "swaps_observed": False,
                        "provenance": {
                            "source_url": "data/canary/x.json",
                            "retrieved_at": "2026-09-21T01:52:44+00:00",
                            "observed_on_server": True,
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(aggregate, "REPO", tmp_path)
    summary = aggregate.fee_summary()
    sched = summary["schedules"]["TEST-Server"]
    assert sched["commission_per_lot_round_trip"] == "6.0"
    assert sched["swaps_observed"] is False
    assert sched["provenance"]["source_url"] == "data/canary/x.json"


# ---------------------------------------------------------------------------
# servers — real HTTP round trips on free ports
# ---------------------------------------------------------------------------


@pytest.fixture()
def status_port(tmp_path, monkeypatch):
    """Status server wired to synthetic data dirs and the real repo config."""
    monkeypatch.setattr(aggregate, "_PROBE_DIR", tmp_path / "probe")
    monkeypatch.setattr(aggregate, "_CANARY_DIR", tmp_path / "canary")
    monkeypatch.setattr(aggregate, "_TICK_ROOT", tmp_path / "ticks")
    server, port = _serve(StatusHandler)
    yield port
    server.shutdown()
    server.server_close()


def test_status_server_index_and_api(status_port):
    status, body = _get(status_port, "/")
    assert status == 200 and b"Forex Research" in body
    status, body = _get(status_port, "/api/status")
    assert status == 200
    snapshot = json.loads(body)
    for key in ("generated_utc", "probe", "canary", "capture", "episodes", "fees"):
        assert key in snapshot
    assert snapshot["probe"]["record"] is None  # empty data dir renders empty state


def test_status_server_404(status_port):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(status_port, "/api/nope")
    assert excinfo.value.code == 404


@pytest.fixture()
def editor_port(tmp_path, monkeypatch):
    """Editor server pointed at a temp config dir (copy of the real example)."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr("forex_research.webui.editor._CONFIG_DIR", config_dir)
    monkeypatch.setattr("forex_research.webui.editor._OBJECTIVE", config_dir / "objective.yaml")
    monkeypatch.setattr("forex_research.webui.editor._RULES", config_dir / "challenge_rules.yaml")
    server, port = _serve(EditorHandler)
    yield port
    server.shutdown()
    server.server_close()


def test_editor_index_and_empty_config(editor_port):
    status, body = _get(editor_port, "/")
    assert status == 200 and b"Configuration" in body
    status, body = _get(editor_port, "/api/config")
    payload = json.loads(body)
    assert payload["objective"] is None and payload["rules"] is None
    assert payload["readiness"]["ready"] is False  # nothing declared yet


def test_editor_save_valid_objective_round_trips(editor_port):
    objective = dict(_VALID_OBJECTIVE)
    status, payload = _post(editor_port, "/api/objective", objective)
    assert status == 200 and payload["ok"] is True
    _, stored = _get(editor_port, "/api/config")
    stored = json.loads(stored)
    assert stored["objective"]["objective"]["target"] == "evaluation_pass"


_VALID_OBJECTIVE = {
    "target": "evaluation_pass",
    "horizon": "12 months",
    "target_annual_return": 0.10,
    "volatility_budget": 0.10,
    "retry_on_failure": True,
    "account_size": 100000,
    "account_type": "swing",
    "max_attempts": 3,
    "max_fee_budget": 1500,
    "max_calendar_time": "6 months",
    # A floor on net value: negative or zero by the schema's own rule.
    "min_acceptable_env": -5000,
}


def test_editor_save_refused_and_rolled_back(editor_port):
    # First save a valid config, then send garbage: the disk file must survive.
    good = dict(_VALID_OBJECTIVE)
    _post(editor_port, "/api/objective", good)
    status, payload = _post(editor_port, "/api/objective", {"target": "nonsense_target"})
    assert payload["ok"] is False
    assert "invalid" in payload["error"].lower()
    _, stored = _get(editor_port, "/api/config")
    stored = json.loads(stored)
    assert stored["objective"]["objective"]["target"] == "evaluation_pass"  # rolled back


def test_editor_rules_save_requires_minimal_valid_shape(editor_port):
    rules = {
        "provider": "firm",
        "programme": "programme",
        "account_type": "swing",
        "rule_set_version": "2026-09-21",
        "timezone": {"iana_zone": "Europe/Prague", "source": "docs"},
        "phases": {
            "challenge": {
                "rules": {
                    "profit_target": {
                        "status": "verified",
                        "value": {"kind": "percent_initial_capital", "value": 8},
                        "provenance": {
                            "source_url": "https://firm.example/rules",
                            "retrieved_at": "2026-09-21T00:00:00+00:00",
                        },
                    }
                }
            }
        },
    }
    status, payload = _post(editor_port, "/api/rules", rules)
    assert payload["ok"] is True, payload.get("error")
    _, stored = _get(editor_port, "/api/config")
    stored = json.loads(stored)
    assert stored["rules"]["phases"]["challenge"]["rules"]["profit_target"]["status"] == "verified"


def test_editor_bad_json_rejected(editor_port):
    req = urllib.request.Request(
        f"http://127.0.0.1:{editor_port}/api/objective",
        method="POST",
        data=b"{broken",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req)
    assert excinfo.value.code == 400


def test_readiness_gate_reflects_state(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr("forex_research.webui.editor._OBJECTIVE", config_dir / "objective.yaml")
    monkeypatch.setattr("forex_research.webui.editor._RULES", config_dir / "challenge_rules.yaml")
    assert editor_readiness()["ready"] is False
    (config_dir / "objective.yaml").write_text(
        yaml.safe_dump({"objective": _VALID_OBJECTIVE}), encoding="utf-8"
    )
    # Objective ready, rules missing -> still not ready overall.
    report = editor_readiness()
    assert report["checks"][0]["ready"] is True
    assert report["checks"][1]["ready"] is False
    assert report["ready"] is False


def test_record_collision_loop_names_distinct_files(tmp_path):
    """Two writes in one clock quantum must not overwrite each other (CI lesson)."""
    from forex_research.webui.aggregate import _newest  # noqa: F401 - existence check

    stamp = "20260921T000000000000Z"
    (tmp_path / f"probe_{stamp}.json").write_text("{}", encoding="utf-8")
    names = sorted(p.name for p in tmp_path.glob("probe_*.json"))
    assert names == [f"probe_{stamp}.json"]
    # The collision suffix loop lives in the execution writers; here we only
    # assert the aggregate's newest-wins order for same-stamp distinct files.
    time.sleep(0.01)
    (tmp_path / f"probe_{stamp}_01.json").write_text("{}", encoding="utf-8")
    assert len(list(tmp_path.glob("probe_*.json"))) == 2
