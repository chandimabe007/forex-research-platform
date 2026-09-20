"""Canonical event priority — ARCH-002.

One ordering, used identically by the backtest engine and the live system.
The exact position of the rule-reset boundary (2) relative to swap/commission
posting (3) is taken from the selected venue's observed behaviour and recorded
at GATE-011; the rest is fixed. The venue-dependent positions arrive as
configuration (``VenueEventConfig``), not as code.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class EventKind(IntEnum):
    """Event kinds in canonical priority order (ARCH-002 table).

    The numeric value IS the priority: lower runs first. Do not renumber —
    downstream engines sort by this value.
    """

    MARKET_STATUS_CHANGE = 1
    RULE_RESET_BOUNDARY = 2
    SWAP_AND_COMMISSION_POSTING = 3
    QUOTE_ARRIVAL = 4
    PENDING_ORDER_TRIGGER = 5
    FILL_OR_REJECTION = 6
    BROKER_SIDE_SLTP_TRIGGER = 7
    STRATEGY_DECISION = 8
    NEW_ORDER_SUBMISSION = 9


class EventPositionError(ValueError):
    """Raised when a venue configuration would break the canonical ordering."""


@dataclass(frozen=True)
class VenueEventConfig:
    """Venue-specific parts of ARCH-002, supplied as configuration.

    ``reset_before_swap`` records the observed reset/swap ordering from the
    venue canary (MILE-023 settles it from observation, per ARCH-002).
    """

    reset_before_swap: bool = True
    venue_name: str = "unverified"

    @property
    def reset_priority(self) -> int:
        return (
            int(EventKind.RULE_RESET_BOUNDARY)
            if self.reset_before_swap
            else int(EventKind.SWAP_AND_COMMISSION_POSTING)
        )

    @property
    def swap_priority(self) -> int:
        return (
            int(EventKind.SWAP_AND_COMMISSION_POSTING)
            if self.reset_before_swap
            else int(EventKind.RULE_RESET_BOUNDARY)
        )


def validate_venue_config(config: VenueEventConfig) -> None:
    """Refuse a configuration that contradicts the fixed parts of ARCH-002."""
    fixed = {int(k) for k in EventKind}
    fixed.discard(int(EventKind.RULE_RESET_BOUNDARY))
    fixed.discard(int(EventKind.SWAP_AND_COMMISSION_POSTING))
    movable = {config.reset_priority, config.swap_priority}
    collision = movable & fixed
    if collision:
        raise EventPositionError(
            f"venue event positions {sorted(collision)} collide with fixed priorities "
            f"{sorted(fixed)} — the venue-specific reset/swap ordering must stay "
            "inside positions 2-3 (ARCH-002)"
        )
    if config.reset_priority == config.swap_priority:
        raise EventPositionError("reset and swap priorities must differ (ARCH-002)")
