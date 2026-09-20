"""Account allowlist enforcement — EXEC-060, SEC-030.

A mutable build flag is too weak a guard against trading the wrong account.
On every connection the account is verified — login hash, server, account
type, currency and phase — against an explicit allowlist, and any
unrecognised account is rejected **before requesting trade permission**.

The raw login number never enters configuration or logs (SEC-001): only its
SHA-256 digest.
"""

from __future__ import annotations

import hashlib


def login_hash(login: int | str) -> str:
    """SHA-256 of the decimal login string. The raw login is never stored."""
    return hashlib.sha256(str(login).strip().encode("utf-8")).hexdigest()


class AllowlistViolation(RuntimeError):
    """The connected account is not on the allowlist (EXEC-060)."""


def verify_account(allowlist, *, login, server, currency, account_type, phase) -> None:
    """Raise ``AllowlistViolation`` unless the account matches an allowlist entry.

    Called before any capability query or trade-permission request
    (EXEC-060). ``phase`` is the deployment phase recorded in configuration
    (e.g. ``demo`` until MILE-095).
    """
    digest = login_hash(login)
    if not allowlist.entries:
        raise AllowlistViolation(
            "refusing to connect: the account allowlist is empty (EXEC-060); "
            "add the demo account to config/accounts.yaml first"
        )
    if not allowlist.contains(
        login_hash=digest,
        server=server,
        account_type=account_type,
        currency=currency,
        phase=phase,
    ):
        raise AllowlistViolation(
            f"refusing to connect: account (sha256:{digest[:12]}…) on server "
            f"'{server}' as {account_type}/{currency}/{phase} is not on the "
            "allowlist (EXEC-060, SEC-030)"
        )
