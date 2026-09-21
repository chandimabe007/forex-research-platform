"""Capability probe — EXEC-020, GATE-010, GATE-011.

Runs on every connection, persists output, fails closed on anything missing
or unexpected. Fields at GATE-011 plus ``trade_allowed``, margin mode and
terminal build. The probe cannot settle DST (GATE-005).

Allowlist verification happens **before any capability query** (EXEC-060):
``run_probe`` takes a ``verifier`` callable and never touches the adapter if
it raises. Records persist redacted — login numbers are never written
(SEC-001).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .adapter import BrokerCapabilities
from .allowlist import login_hash

PROBE_SCHEMA_VERSION = 1


def persist_probe_record(
    *,
    out_dir: Path,
    outcome: str,
    account_login: int,
    capabilities: BrokerCapabilities | None,
    discrepancies: list[str],
    errors: list[str],
) -> Path:
    """Write a redacted JSON record for every outcome (OK or failure)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": PROBE_SCHEMA_VERSION,
        "timestamp": datetime.now(UTC).isoformat(),
        "outcome": outcome,  # ok | refused | failed
        "account_login_sha256": login_hash(account_login) if account_login else None,
        "capabilities": asdict(capabilities) if capabilities else None,
        "discrepancies": discrepancies,
        "errors": errors,
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = out_dir / f"probe_{stamp}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return path


def compare_against_expected(
    capabilities: BrokerCapabilities,
    expected: dict,
) -> list[str]:
    """Every documented value is compared against observed; discrepancies listed.

    ``expected`` comes from config/instruments.yaml (DATA-002 values marked
    OBSERVE) — the probe never silently accepts a drifted value.
    """
    discrepancies: list[str] = []
    for symbol, symbol_expected in (expected or {}).items():
        observed = capabilities.symbols.get(symbol)
        if observed is None:
            discrepancies.append(f"{symbol}: no observed capabilities")
            continue
        for field in (
            "point_size",
            "contract_size",
            "tick_size",
            "volume_min",
            "volume_step",
            "volume_max",
        ):
            want = symbol_expected.get(field)
            if want is None:
                continue
            got = getattr(observed, field if field != "point_size" else "point")
            if abs(float(want) - float(got)) > 1e-12:
                discrepancies.append(f"{symbol}.{field}: documented {want} vs observed {got}")
    return discrepancies


def run_probe(
    adapter,
    *,
    allowlist,
    verifier,
    symbols: list[str],
    expected: dict,
    out_dir: Path,
) -> tuple[str, Path]:
    """Run the probe: verify the allowlist first, probe second, persist always.

    Returns ``(outcome, record_path)`` where outcome is ok | refused | failed.
    """
    account = None
    try:
        account = adapter.connect()
    except Exception as exc:  # noqa: BLE001 - record every outcome
        path = persist_probe_record(
            out_dir=out_dir,
            outcome="failed",
            account_login=0,
            capabilities=None,
            discrepancies=[],
            errors=[f"connect failed: {exc}"],
        )
        return "failed", path

    try:
        # EXEC-060: allowlist check BEFORE any capability query.
        verifier(
            allowlist,
            login=account.login,
            server=account.server,
            currency=account.currency,
            account_type=account.account_type,
            phase="demo",
        )
    except Exception as exc:  # noqa: BLE001
        path = persist_probe_record(
            out_dir=out_dir,
            outcome="refused",
            account_login=account.login,
            capabilities=None,
            discrepancies=[],
            errors=[f"allowlist refused: {exc}"],
        )
        return "refused", path

    errors: list[str] = []
    discrepancies: list[str] = []
    try:
        capabilities = adapter.probe_capabilities(symbols)
        cap_errors = capabilities.validate()
        errors.extend(cap_errors)
        discrepancies = compare_against_expected(capabilities, expected)
        discrepancies.append(
            f"terminal trade_allowed={capabilities.trade_expert} "
            "(enable the Algo Trading toggle before order-placing milestones)"
        )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"probe failed: {exc}")
        capabilities = None

    outcome = "ok" if capabilities is not None and not errors else "failed"
    path = persist_probe_record(
        out_dir=out_dir,
        outcome=outcome,
        account_login=account.login,
        capabilities=capabilities,
        discrepancies=discrepancies,
        errors=errors,
    )
    return outcome, path
