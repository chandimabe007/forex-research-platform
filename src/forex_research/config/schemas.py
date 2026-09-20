"""Configuration schemas — PROD-010, CHAL-010, CHAL-011, GATE-005.

The objective and the firm/programme/account triple are **inputs, not
prerequisites for building** (MILE-002): the software builds and tests run
without them, and research entry points (MILE-040 onward) refuse to start
until they load and validate.

Every atomic rule is a tagged object with provenance. Every field is in one
of three states (CHAL-011):

- ``unknown`` — not checked; the simulator refuses to run.
- ``not_applicable`` — verified that the firm does not impose this; requires
  the same provenance as a value.
- ``verified`` + value — checked and recorded.

Time zone is an IANA zone, not an offset (GATE-005).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class RuleStatus(StrEnum):
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"
    VERIFIED = "verified"


class BreachSemantics(StrEnum):
    FALLS_BELOW = "falls_below"
    HITS = "hits"


class BreachSeverity(StrEnum):
    HARD = "hard"
    SOFT = "soft"


class FloorBasis(StrEnum):
    BALANCE_AT_RESET_MINUS_AMOUNT = "balance_at_reset_minus_amount"
    INITIAL_BALANCE_MINUS_AMOUNT = "initial_balance_minus_amount"
    EQUITY_RATCHET = "equity_ratchet"


@dataclass(frozen=True)
class Provenance:
    """Where a rule value came from (CHAL-010)."""

    source_url: str = ""
    retrieved_at: datetime | None = None
    evidence_sha256: str = ""
    observed_on_server: bool = False

    def validate(self, *, require_url: bool = True) -> list[str]:
        errors: list[str] = []
        if require_url and not self.source_url:
            errors.append("source_url is required for provenance")
        if self.retrieved_at is None:
            errors.append("retrieved_at is required for provenance")
        return errors


@dataclass(frozen=True)
class TypedValue:
    """A rule value tagged with its kind (CHAL-010 — no untyped scalars)."""

    kind: str  # percent_initial_capital | absolute | count | boolean | duration_days | ...
    value: Any


@dataclass(frozen=True)
class AtomicRule:
    """One rule with its status, value and provenance (CHAL-010, CHAL-011)."""

    name: str
    status: RuleStatus
    value: TypedValue | None = None
    floor_basis: FloorBasis | None = None
    monitored_value: str | None = None
    ratchet_on_equity: bool | None = None  # GATE-003 floating-profit ratchet
    breach_semantics: BreachSemantics | None = None
    breach_severity: BreachSeverity | None = None
    reset_local_time: str | None = None
    provenance: Provenance = field(default_factory=Provenance)

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.status is RuleStatus.UNKNOWN:
            return errors  # unknown is valid input state; consumers refuse (CHAL-011)
        if self.status is RuleStatus.NOT_APPLICABLE:
            # "No time limit" is a verified fact, not a gap: same provenance as a value.
            errors.extend(self.provenance.validate())
            if self.value is not None:
                errors.append("not_applicable rules must not carry a value")
            return errors
        # verified
        errors.extend(self.provenance.validate())
        if self.value is None:
            errors.append("verified rules must carry a value")
        return errors


@dataclass(frozen=True)
class PhaseRules:
    """Rules for one phase — evaluation, verification, funded (CHAL-010)."""

    name: str
    rules: dict[str, AtomicRule]

    def validate(self) -> list[str]:
        errors: list[str] = []
        for key, rule in self.rules.items():
            for err in rule.validate():
                errors.append(f"phase '{self.name}' rule '{key}': {err}")
        return errors


@dataclass(frozen=True)
class TimezoneConfig:
    """IANA zone, not an offset (GATE-005)."""

    iana_zone: str
    source: str = ""

    def validate(self) -> list[str]:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        errors: list[str] = []
        try:
            ZoneInfo(self.iana_zone)
        except (ZoneInfoNotFoundError, ValueError):
            errors.append(f"iana_zone '{self.iana_zone}' is not a valid IANA zone (GATE-005)")
        return errors


@dataclass(frozen=True)
class ChallengeRulesConfig:
    """The full CHAL-010 rule model, firm-agnostic by construction."""

    provider: str
    programme: str
    account_type: str
    rule_set_version: str
    timezone: TimezoneConfig
    phases: dict[str, PhaseRules]

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.provider:
            errors.append("provider is required")
        if not self.programme:
            errors.append("programme is required")
        if not self.account_type:
            errors.append("account_type is required")
        if not self.rule_set_version:
            errors.append("rule_set_version is required")
        errors.extend(self.timezone.validate())
        if not self.phases:
            errors.append("at least one phase must be present")
        for phase in self.phases.values():
            errors.extend(phase.validate())
        return errors


@dataclass(frozen=True)
class ObjectiveConfig:
    """The declared economic objective — PROD-010."""

    target: str | None  # evaluation_pass | first_payout | sustained_payouts_12m
    horizon: str | None  # e.g. "12 months"
    target_annual_return: float | None
    volatility_budget: float | None
    retry_on_failure: bool | None
    account_size: float | None
    account_type: str | None  # standard | swing | other
    max_attempts: int | None
    max_fee_budget: float | None
    max_calendar_time: str | None
    min_acceptable_env: float | None

    def validate(self) -> list[str]:
        """An all-null template is refused by the loader (MILE-002)."""
        required = [
            self.target,
            self.horizon,
            self.target_annual_return,
            self.volatility_budget,
            self.retry_on_failure,
            self.account_size,
            self.account_type,
            self.max_attempts,
            self.max_fee_budget,
            self.max_calendar_time,
            self.min_acceptable_env,
        ]
        if all(v is None for v in required):
            return ["objective is an all-null template; fill it in during MILE-010 (PROD-010)"]
        errors: list[str] = []
        allowed_targets = {"evaluation_pass", "first_payout", "sustained_payouts_12m"}
        if self.target is not None and self.target not in allowed_targets:
            errors.append(f"target must be one of {sorted(allowed_targets)}")
        if self.target_annual_return is not None and self.target_annual_return <= 0:
            errors.append("target_annual_return must be positive")
        if self.volatility_budget is not None and self.volatility_budget <= 0:
            errors.append("volatility_budget must be positive")
        if self.max_attempts is not None and self.max_attempts < 1:
            errors.append("max_attempts must be >= 1")
        if self.min_acceptable_env is not None and self.min_acceptable_env >= 0:
            errors.append("min_acceptable_env is a floor on net value; it must be negative or zero")
        return errors

    def is_complete(self) -> bool:
        """Research gates require a fully declared objective (GATE-031)."""
        return all(
            v is not None
            for v in (
                self.target,
                self.horizon,
                self.target_annual_return,
                self.volatility_budget,
                self.retry_on_failure,
                self.account_size,
                self.account_type,
                self.max_attempts,
                self.max_fee_budget,
                self.max_calendar_time,
                self.min_acceptable_env,
            )
        )


@dataclass(frozen=True)
class AccountAllowlistEntry:
    """One allowlisted account — EXEC-060, SEC-030."""

    login_hash: str  # sha256 of the login number, never the login itself
    server: str
    account_type: str  # demo | evaluation | funded
    currency: str
    phase: str

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.login_hash or len(self.login_hash) != 64:
            errors.append("login_hash must be a sha256 hex digest; the raw login never enters configuration (SEC-001)")
        if self.account_type not in {"demo", "evaluation", "funded"}:
            errors.append("account_type must be demo | evaluation | funded")
        if not self.server:
            errors.append("server is required")
        if not self.currency:
            errors.append("currency is required")
        return errors
