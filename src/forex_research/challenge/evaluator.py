"""Challenge simulator - rule evaluation core (CHAL-001, CHAL-010..013).

Given an account's equity path and a rule set, which rules breach and when?
That evaluation feeds the pass-probability simulator (MILE-055); it is not
itself the objective (CHAL-001). The simulator refuses to run while any rule
is ``unknown`` (CHAL-011) - enforced upstream by ``require_challenge_rules``.

Breaches are detected on the intraday path, not at daily close: a fall below
the floor that recovers by the close is still a breach (VAL-030b). The
floating-profit ratchet (GATE-003) is a per-rule configuration field.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..config.schemas import AtomicRule, BreachSemantics, BreachSeverity, RuleStatus


@dataclass(frozen=True)
class AccountPoint:
    """One synchronous mark of the account (CHAL-020 carries these in blocks)."""

    ts: datetime  # UTC; reset boundaries evaluated in the venue zone
    equity: Decimal
    balance: Decimal
    positions_open: int


@dataclass(frozen=True)
class Breach:
    rule: str
    phase: str
    ts: datetime
    severity: BreachSeverity
    monitored_value: Decimal
    floor_or_target: Decimal


@dataclass
class _DayState:
    anchor: datetime
    baseline: Decimal
    ratchet_high: Decimal


def _amount(rule: AtomicRule, initial_capital: Decimal) -> Decimal:
    kind = rule.value.kind if rule.value else ""
    value = Decimal(str(rule.value.value)) if rule.value else Decimal(0)
    if kind == "percent_initial_capital":
        return initial_capital * value / Decimal(100)
    return value


def _anchor_for(ts: datetime, zone: ZoneInfo, reset_time: time) -> datetime:
    """The venue-local reset instant at or before ``ts`` (GATE-005 zone)."""
    local = ts.astimezone(zone).replace(tzinfo=None)
    candidate = local.replace(hour=reset_time.hour, minute=reset_time.minute,
                              second=0, microsecond=0)
    if local < candidate:
        candidate = candidate - _dt.timedelta(days=1)
    return candidate.replace(tzinfo=zone).astimezone(_dt.UTC)


def evaluate_phase(
    *,
    phase_name: str,
    rules: dict[str, AtomicRule],
    initial_capital: Decimal,
    path: list[AccountPoint],
    venue_zone: str,
) -> list[Breach]:
    """Evaluate one phase's loss/target rules over an intraday marked path."""
    breaches: list[Breach] = []

    daily_rule = rules.get("maximum_daily_loss")
    max_rule = rules.get("maximum_loss")
    target_rule = rules.get("profit_target")

    max_floor = None
    if max_rule and max_rule.status is RuleStatus.VERIFIED:
        max_floor = initial_capital - _amount(max_rule, initial_capital)

    target_level = None
    if target_rule and target_rule.status is RuleStatus.VERIFIED:
        target_level = initial_capital + _amount(target_rule, initial_capital)

    zone = ZoneInfo(venue_zone)
    reset_time = time(0, 0)
    if daily_rule and daily_rule.reset_local_time:
        # Accept both "HH:MM" and "HH:MM:SS"; the loader does not police the
        # format, and a crash here would surface as a runtime ValueError.
        parts = [int(x) for x in daily_rule.reset_local_time.split(":")]
        hh, mm = parts[0], parts[1]
        ss = parts[2] if len(parts) > 2 else 0
        reset_time = time(hh, mm, ss)

    day: _DayState | None = None

    for point in path:
        anchor = _anchor_for(point.ts, zone, reset_time)
        if day is None or anchor > day.anchor:
            # A new reset period begins: baseline is the balance at the reset
            # (floor_basis balance_at_reset_minus_amount), per CHAL-010.
            day = _DayState(anchor=anchor, baseline=point.balance,
                            ratchet_high=max(point.balance, point.equity))
        else:
            day.ratchet_high = max(day.ratchet_high, point.equity)

        if daily_rule and daily_rule.status is RuleStatus.VERIFIED:
            # GATE-003: with the ratchet, the baseline is the equity at the
            # reset — floating profit held through the reset raises the next
            # floor. day.ratchet_high at the reset is exactly max(balance,
            # equity) at that instant; later post-reset balances must not
            # re-raise it (the ratchet is a high-water mark of equity, not of
            # balance).
            baseline = day.ratchet_high if daily_rule.ratchet_on_equity else day.baseline
            floor = baseline - _amount(daily_rule, initial_capital)
            semantics = daily_rule.breach_semantics or BreachSemantics.FALLS_BELOW
            breached = (point.equity < floor if semantics is BreachSemantics.FALLS_BELOW
                        else point.equity <= floor)
            if breached:
                breaches.append(Breach("maximum_daily_loss", phase_name, point.ts,
                                       daily_rule.breach_severity or BreachSeverity.HARD,
                                       point.equity, floor))

        if max_floor is not None and point.equity < max_floor:
            breaches.append(Breach("maximum_loss", phase_name, point.ts,
                                   (max_rule.breach_severity or BreachSeverity.HARD)
                                   if max_rule else BreachSeverity.HARD,
                                   point.equity, max_floor))

        if target_level is not None and point.equity >= target_level:
            breaches.append(Breach("profit_target", phase_name, point.ts,
                                   (target_rule.breach_severity or BreachSeverity.SOFT)
                                   if target_rule else BreachSeverity.SOFT,
                                   point.equity, target_level))

    return breaches
