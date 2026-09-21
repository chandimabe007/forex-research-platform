"""CLI for the capability probe — MILE-021.

Usage::

    python -m forex_research.execution.cli [--symbols EURUSD GBPUSD ...]

The probe places no orders. It refuses to run until the demo account is on
the allowlist (EXEC-060): add the sha256 of your login to
``config/accounts.yaml``. A redacted record for every outcome persists to
``data/probe/``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from ..config import load_allowlist
from ..config.loader import ConfigError
from .allowlist import verify_account
from .probe import persist_probe_record, run_probe

DEFAULT_SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]


def load_expected_symbols(path: Path) -> dict:
    """Documented instrument values the observed ones are compared against."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("instruments", {})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MILE-021 capability probe (read-only)")
    parser.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    parser.add_argument("--out", type=Path, default=Path("data/probe"))
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config"),
        help="directory containing accounts.yaml and instruments.yaml",
    )
    args = parser.parse_args(argv)

    # EXEC-060 fail-closed, but as a REFUSAL with a persisted record, not a
    # crash: an empty or invalid allowlist is a configuration state the
    # operator must fix, and the refusal itself is evidence.
    try:
        allowlist = load_allowlist(args.config / "accounts.yaml")
    except ConfigError as exc:
        print(f"probe refused: {exc}")
        path = persist_probe_record(
            out_dir=args.out,
            outcome="refused",
            account_login=0,
            capabilities=None,
            discrepancies=[],
            errors=[f"allowlist refused: {exc}"],
        )
        print(f"record: {path}")
        return 2
    expected = load_expected_symbols(args.config / "instruments.yaml")

    from .mt5_adapter import MT5Adapter

    adapter = MT5Adapter()
    outcome, path = run_probe(
        adapter,
        allowlist=allowlist,
        verifier=verify_account,
        symbols=args.symbols,
        expected=expected,
        out_dir=args.out,
    )
    print(f"probe outcome: {outcome}")
    print(f"record: {path}")
    return 0 if outcome == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
