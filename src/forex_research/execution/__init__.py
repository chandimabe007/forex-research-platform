"""Execution layer: adapter contract, allowlist, capability probe (EXEC-*)."""

from .adapter import (
    AccountState,
    BrokerCapabilities,
    FillingMode,
    MarginMode,
    OrderIntent,
    OrderResult,
    SessionStatus,
    SymbolInfo,
    TerminalInfo,
)
from .allowlist import AllowlistViolation, login_hash, verify_account
from .probe import compare_against_expected, persist_probe_record, run_probe

__all__ = [
    "AccountState",
    "AllowlistViolation",
    "BrokerCapabilities",
    "FillingMode",
    "MarginMode",
    "OrderIntent",
    "OrderResult",
    "SessionStatus",
    "SymbolInfo",
    "TerminalInfo",
    "compare_against_expected",
    "login_hash",
    "persist_probe_record",
    "run_probe",
    "verify_account",
]
