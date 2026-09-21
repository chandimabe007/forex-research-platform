"""Aggregate the platform's local state into one snapshot for the dashboard.

Reads only; tolerates missing artifacts (nothing probed/captured/canaried yet),
because a fresh install has an empty ``data/`` and the dashboard must still
render. The newest probe and canary records are located by their timestamped
filenames (probe_*: newest mtime). Tick coverage is counted per symbol-month
without loading file contents into memory beyond the last line.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
_PROBE_DIR = REPO / "data" / "probe"
_CANARY_DIR = REPO / "data" / "canary"
_TICK_ROOT = REPO / "data" / "raw" / "venue_quotes"

_PROBE_SCHEMA_VERSION = 1


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _newest(dir_path: Path, prefix: str) -> Path | None:
    if not dir_path.exists():
        return None
    candidates = sorted(
        dir_path.glob(f"{prefix}_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def probe_summary() -> dict:
    path = _newest(_PROBE_DIR, "probe")
    record = _read_json(path) if path else None
    if record is None or record.get("schema_version") != _PROBE_SCHEMA_VERSION:
        return {"record": None, "record_file": str(path.name) if path else None}
    caps = record.get("capabilities") or {}
    return {
        "record": {
            "timestamp": record.get("timestamp"),
            "outcome": record.get("outcome"),
            "discrepancies": record.get("discrepancies") or [],
            "errors": record.get("errors") or [],
            "capabilities": caps,
        },
        "record_file": str(path.name),
    }


def canary_summary() -> dict:
    path = _newest(_CANARY_DIR, "canary")
    record = _read_json(path) if path else None
    if record is None or "outcome" not in record:
        return {"record": None, "record_file": str(path.name) if path else None}
    steps = record.get("steps") or []
    reconcile = next((s for s in steps if s.get("name") == "reconcile"), None)
    protection = next((s for s in steps if s.get("name") == "broker_side_protection"), None)
    submit = next((s for s in steps if s.get("name") == "submit"), None)
    return {
        "record": {
            "finished_at": record.get("finished_at"),
            "outcome": record.get("outcome"),
            "symbol": record.get("symbol"),
            "discrepancies": record.get("discrepancies") or [],
            "reconcile": (reconcile or {}).get("detail"),
            "protection": (protection or {}).get("detail"),
            "submit": (submit or {}).get("detail"),
        },
        "record_file": str(path.name),
    }


def capture_summary() -> dict:
    """Per symbol-month tick coverage, counted without loading files fully."""
    symbols: dict[str, dict] = {}
    total_rows = 0
    if _TICK_ROOT.exists():
        for month_file in sorted(_TICK_ROOT.glob("*/*.jsonl")):
            symbol = month_file.parent.name
            rows = 0
            last_ts: str | None = None
            first_ts: str | None = None
            with month_file.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rows += 1
                    if first_ts is None:
                        first_ts = json.loads(line).get("ts")
                    if last_ts is None or rows % 5000 == 1:
                        last_ts = json.loads(line).get("ts")
            if rows == 0:
                continue
            symbols.setdefault(symbol, {"months": []})["months"].append(
                {
                    "file": month_file.name,
                    "rows": rows,
                    "first_ts": first_ts,
                    "last_ts": last_ts,
                }
            )
            total_rows += rows
    return {
        "symbols": {k: v["months"] for k, v in symbols.items()},
        "total_rows": total_rows,
    }


def episode_summary(minimums: dict[str, int]) -> dict:
    """Episode counts against the capture.yaml minimums (COST-013)."""
    from forex_research.capture.report import episode_counts

    counts = episode_counts(_TICK_ROOT)
    return {"minimums": minimums, "episodes": counts["episodes"], "rows": counts["rows"]}


def fee_summary() -> dict:
    """Fee schedules with their provenance — the evidence, not just the number."""
    from forex_research.config.loader import ConfigError, load_fee_schedules

    path = REPO / "config" / "fees.yaml"
    try:
        schedules = load_fee_schedules(path, allow_unobserved_swaps=True)
    except (ConfigError, OSError):
        return {"schedules": {}, "error": "config/fees.yaml not loadable"}
    out: dict[str, dict] = {}
    for server, source in schedules.items():
        out[server] = {
            "currency": source.currency,
            "commission_per_lot_round_trip": str(source.commission_per_lot_round_trip),
            "swap_long_per_lot_per_day": (
                str(source.swap_long_per_lot_per_day)
                if source.swap_long_per_lot_per_day is not None
                else None
            ),
            "swap_short_per_lot_per_day": (
                str(source.swap_short_per_lot_per_day)
                if source.swap_short_per_lot_per_day is not None
                else None
            ),
            "swaps_observed": source.swaps_observed,
            "provenance": {
                "source_url": source.provenance.source_url,
                "retrieved_at": (
                    source.provenance.retrieved_at.isoformat()
                    if source.provenance.retrieved_at
                    else None
                ),
                "evidence_sha256": source.provenance.evidence_sha256,
                "observed_on_server": source.provenance.observed_on_server,
            },
        }
    return {"schedules": out}


def build_status_snapshot() -> dict:
    """Everything the dashboard renders, in one call. Pure reads."""
    episode_minimums: dict[str, int] = {}
    capture_config = REPO / "config" / "capture.yaml"
    if capture_config.exists():
        try:
            import yaml

            with capture_config.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            episode_minimums = data.get("episode_minimums") or {}
        except (OSError, ValueError):
            episode_minimums = {}
    return {
        "generated_utc": datetime.now(UTC).isoformat(),
        "probe": probe_summary(),
        "canary": canary_summary(),
        "capture": capture_summary(),
        "episodes": episode_summary(episode_minimums),
        "fees": fee_summary(),
    }
