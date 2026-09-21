"""Load and validate configuration files.

The software builds and its tests run **without** any objective or firm
supplied (MILE-002); research entry points call :func:`require_objective` /
:func:`require_challenge_rules` and refuse without them (MILE-040).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from ..costs.commission import FeeSchedule
from .schemas import (
    AccountAllowlistEntry,
    AtomicRule,
    BreachSemantics,
    BreachSeverity,
    ChallengeRulesConfig,
    FloorBasis,
    ObjectiveConfig,
    PhaseRules,
    Provenance,
    RuleStatus,
    TimezoneConfig,
    TypedValue,
)


class ConfigError(ValueError):
    """Raised when a configuration file fails validation."""


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    return data


def load_objective(path: Path) -> ObjectiveConfig:
    """Load the PROD-010 objective. An all-null template is refused (MILE-002)."""
    data = _load_yaml(path)
    obj = data.get("objective", data)
    config = ObjectiveConfig(
        target=obj.get("target"),
        horizon=obj.get("horizon"),
        target_annual_return=obj.get("target_annual_return"),
        volatility_budget=obj.get("volatility_budget"),
        retry_on_failure=obj.get("retry_on_failure"),
        account_size=obj.get("account_size"),
        account_type=obj.get("account_type"),
        max_attempts=obj.get("max_attempts"),
        max_fee_budget=obj.get("max_fee_budget"),
        max_calendar_time=obj.get("max_calendar_time"),
        min_acceptable_env=obj.get("min_acceptable_env"),
    )
    errors = config.validate()
    if errors:
        raise ConfigError(f"objective invalid: {path}\n  - " + "\n  - ".join(errors))
    return config


def require_objective(path: Path) -> ObjectiveConfig:
    """Load the objective and additionally require a complete declaration (GATE-031)."""
    config = load_objective(path)
    if not config.is_complete():
        raise ConfigError(
            "objective must be fully declared before research runs (GATE-031, MILE-040)"
        )
    return config


def _parse_rule(name: str, raw: dict[str, Any]) -> AtomicRule:
    value = raw.get("value")
    typed = (
        TypedValue(kind=str(value.get("kind")), value=value.get("value"))
        if isinstance(value, dict)
        else None
    )
    prov_raw = raw.get("provenance", {}) or {}
    provenance = Provenance(
        source_url=str(prov_raw.get("source_url", "") or ""),
        retrieved_at=_parse_datetime(prov_raw.get("retrieved_at")),
        evidence_sha256=str(prov_raw.get("evidence_sha256", "") or ""),
        observed_on_server=bool(prov_raw.get("observed_on_server", False)),
    )
    return AtomicRule(
        name=name,
        status=RuleStatus(raw.get("status", "unknown")),
        value=typed,
        floor_basis=FloorBasis(raw["floor_basis"]) if raw.get("floor_basis") else None,
        monitored_value=raw.get("monitored_value"),
        ratchet_on_equity=raw.get("ratchet_on_equity"),
        breach_semantics=BreachSemantics(raw["breach_semantics"])
        if raw.get("breach_semantics")
        else None,
        breach_severity=BreachSeverity(raw["breach_severity"])
        if raw.get("breach_severity")
        else None,
        reset_local_time=raw.get("reset_local_time"),
        provenance=provenance,
    )


def load_challenge_rules(path: Path) -> ChallengeRulesConfig:
    """Load the CHAL-010 rule model. Unknown rule fields are preserved in phases."""
    data = _load_yaml(path)
    tz_raw = data.get("timezone", {}) or {}
    timezone = TimezoneConfig(
        iana_zone=str(tz_raw.get("iana_zone", "")),
        source=str(tz_raw.get("source", "") or ""),
    )
    phases: dict[str, PhaseRules] = {}
    for phase_name, phase_raw in (data.get("phases", {}) or {}).items():
        rules = {
            rule_name: _parse_rule(rule_name, rule_raw or {})
            for rule_name, rule_raw in ((phase_raw or {}).get("rules", {}) or {}).items()
        }
        phases[phase_name] = PhaseRules(name=phase_name, rules=rules)
    config = ChallengeRulesConfig(
        provider=str(data.get("provider", "")),
        programme=str(data.get("programme", "")),
        account_type=str(data.get("account_type", "")),
        rule_set_version=str(data.get("rule_set_version", "")),
        timezone=timezone,
        phases=phases,
    )
    errors = config.validate()
    if errors:
        raise ConfigError(f"challenge rules invalid: {path}\n  - " + "\n  - ".join(errors))
    return config


def require_challenge_rules(path: Path) -> ChallengeRulesConfig:
    """Refuse to run research until every field is verified or not_applicable (CHAL-011)."""
    config = load_challenge_rules(path)
    unknown = [
        f"{phase.name}.{rule.name}"
        for phase in config.phases.values()
        for rule in phase.rules.values()
        if rule.status is RuleStatus.UNKNOWN
    ]
    if unknown:
        raise ConfigError(
            "refusing to run: rules still 'unknown' (CHAL-011): " + ", ".join(unknown)
        )
    return config


@dataclass(frozen=True)
class Allowlist:
    """Explicit account allowlist — EXEC-060, SEC-030."""

    entries: tuple[AccountAllowlistEntry, ...]

    def contains(
        self, *, login_hash: str, server: str, account_type: str, currency: str, phase: str
    ) -> bool:
        for entry in self.entries:
            if (
                entry.login_hash == login_hash
                and entry.server == server
                and entry.account_type == account_type
                and entry.currency == currency
                and entry.phase == phase
            ):
                return True
        return False

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.entries:
            errors.append("allowlist is empty; connections must be refused (EXEC-060)")
        for entry in self.entries:
            errors.extend(f"allowlist entry '{entry.server}': {e}" for e in entry.validate())
        return errors


def load_allowlist(path: Path) -> Allowlist:
    data = _load_yaml(path)
    raw_entries = data.get("allowlist", data.get("accounts", [])) or []
    entries = tuple(
        AccountAllowlistEntry(
            login_hash=str(item.get("login_hash", "")),
            server=str(item.get("server", "")),
            account_type=str(item.get("account_type", "")),
            currency=str(item.get("currency", "")),
            phase=str(item.get("phase", "")),
        )
        for item in raw_entries
    )
    allowlist = Allowlist(entries=entries)
    errors = allowlist.validate()
    if errors:
        raise ConfigError(f"allowlist invalid: {path}\n  - " + "\n  - ".join(errors))
    return allowlist


@dataclass(frozen=True)
class FeeScheduleSource:
    """One venue's fee schedule as configured, with its evidence (COST-016)."""

    server: str
    currency: str
    commission_per_lot_round_trip: Decimal
    swap_long_per_lot_per_day: Decimal | None
    swap_short_per_lot_per_day: Decimal | None
    swaps_observed: bool
    triple_swap_weekdays: tuple[int, ...]
    holidays: tuple[Any, ...]
    provenance: Provenance

    def validate(self) -> list[str]:
        errors: list[str] = []
        errors.extend(self.provenance.validate())
        if not self.provenance.observed_on_server:
            errors.append(
                "fees must be observed on the server (canary) before use; "
                "a documented-only schedule is not evidence (COST-016)"
            )
        if self.swap_long_per_lot_per_day is None or self.swap_short_per_lot_per_day is None:
            if self.swaps_observed:
                errors.append("swaps_observed is true but swap values are missing")
            else:
                errors.append(
                    "swap rates not observed yet (swaps_observed: false) — run the "
                    "reset-boundary canary before any backtest that holds positions "
                    "across rollover"
                )
        return errors

    def to_fee_schedule(self, *, allow_unobserved_swaps: bool = False) -> FeeSchedule:
        """Build the execution FeeSchedule; refuses unobserved swaps (COST-016)
        unless explicitly acknowledged for plumbing demos — in which case swap
        rates enter as ZERO and the resulting trades must never hold overnight."""
        errors = [
            e
            for e in self.validate()
            if not (allow_unobserved_swaps and "swap rates not observed" in e)
        ]
        if errors:
            raise ConfigError(
                f"fee schedule '{self.server}' invalid:\n  - " + "\n  - ".join(errors)
            )
        if self.swap_long_per_lot_per_day is None or self.swap_short_per_lot_per_day is None:
            if not allow_unobserved_swaps:  # defensive; validate() already covers it
                raise ConfigError("unobserved swaps require the demo acknowledgement")
            swap_long, swap_short = Decimal(0), Decimal(0)
        else:
            swap_long, swap_short = (
                self.swap_long_per_lot_per_day,
                self.swap_short_per_lot_per_day,
            )
        return FeeSchedule(
            symbol=self.server,
            commission_per_lot_round_trip=self.commission_per_lot_round_trip,
            swap_long_per_lot_per_day=swap_long,
            swap_short_per_lot_per_day=swap_short,
            triple_swap_weekdays=self.triple_swap_weekdays,
            holidays=self.holidays,
            retrieved_at=self.provenance.retrieved_at,
        )


def load_fee_schedules(
    path: Path, *, allow_unobserved_swaps: bool = False
) -> dict[str, FeeScheduleSource]:
    """Load fee schedules. By default a schedule with unobserved swap rates is
    a validation ERROR (COST-016: never assume fees). ``allow_unobserved_swaps``
    exists solely for plumbing demos that hold no positions overnight."""
    data = _load_yaml(path)
    out: dict[str, FeeScheduleSource] = {}
    for server, raw in (data.get("schedules", {}) or {}).items():
        prov_raw = raw.get("provenance", {}) or {}
        swaps_long = raw.get("swap_long_per_lot_per_day")
        swaps_short = raw.get("swap_short_per_lot_per_day")
        source = FeeScheduleSource(
            server=str(server),
            currency=str(raw.get("currency", "")),
            commission_per_lot_round_trip=Decimal(str(raw.get("commission_per_lot_round_trip"))),
            swap_long_per_lot_per_day=(
                Decimal(str(swaps_long)) if swaps_long is not None else None
            ),
            swap_short_per_lot_per_day=(
                Decimal(str(swaps_short)) if swaps_short is not None else None
            ),
            swaps_observed=bool(raw.get("swaps_observed", False)),
            triple_swap_weekdays=tuple(raw.get("triple_swap_weekdays", (2,))),
            holidays=tuple(raw.get("holidays", ())),
            provenance=Provenance(
                source_url=str(prov_raw.get("source_url", "")),
                retrieved_at=_parse_datetime(prov_raw.get("retrieved_at")),
                evidence_sha256=str(prov_raw.get("evidence_sha256", "")),
                observed_on_server=bool(prov_raw.get("observed_on_server", False)),
            ),
        )
        if allow_unobserved_swaps:
            errors = [e for e in source.validate() if "swap rates not observed" not in e]
        else:
            errors = source.validate()
        if errors:
            raise ConfigError(
                f"fee schedule '{server}' invalid: {path}\n  - " + "\n  - ".join(errors)
            )
        out[str(server)] = source
    if not out:
        raise ConfigError(f"no fee schedules found in {path}")
    return out


def load_fee_schedule_for_server(
    path: Path, server: str, *, allow_unobserved_swaps: bool = False
) -> FeeSchedule:
    """The FeeSchedule for one venue server, evidence-validated."""
    schedules = load_fee_schedules(path, allow_unobserved_swaps=allow_unobserved_swaps)
    if server not in schedules:
        raise ConfigError(
            f"no fee schedule for server '{server}' in {path}; known: {sorted(schedules)}"
        )
    return schedules[server].to_fee_schedule(allow_unobserved_swaps=allow_unobserved_swaps)
