"""One state machine — ARCH-003.

The live system and the backtest use this same machine. Rejected orders are
logged with no retry within the bar; Unknown is resolved via EXEC-031 and is
never resubmitted from this layer.
"""

from __future__ import annotations

from enum import StrEnum


class OrderStatus(StrEnum):
    QUEUED = "queued"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    OPEN = "open"
    REJECTED = "rejected"
    REQUOTED = "requoted"
    UNKNOWN = "unknown"
    CLOSED_SL = "closed_sl"
    CLOSED_TP = "closed_tp"
    CLOSED_MANUAL = "closed_manual"


class InvalidTransition(RuntimeError):
    """A transition ARCH-003 does not permit."""


# Transitions permitted by the ARCH-003 diagram, as (from, to) pairs.
_ALLOWED: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.QUEUED: frozenset({OrderStatus.SUBMITTED}),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.FILLED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.REJECTED,
            OrderStatus.REQUOTED,
            OrderStatus.UNKNOWN,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {OrderStatus.OPEN, OrderStatus.UNKNOWN, OrderStatus.REJECTED}
    ),
    OrderStatus.FILLED: frozenset({OrderStatus.OPEN}),
    OrderStatus.OPEN: frozenset(
        {OrderStatus.CLOSED_SL, OrderStatus.CLOSED_TP, OrderStatus.CLOSED_MANUAL}
    ),
    OrderStatus.REQUOTED: frozenset({OrderStatus.SUBMITTED}),  # accept within tolerance, else abandon
    OrderStatus.UNKNOWN: frozenset(),  # resolve via EXEC-031; never resubmit here
    OrderStatus.REJECTED: frozenset(),  # logged, no retry within the bar
    OrderStatus.CLOSED_SL: frozenset(),
    OrderStatus.CLOSED_TP: frozenset(),
    OrderStatus.CLOSED_MANUAL: frozenset(),
}


def can_transition(current: OrderStatus, next_status: OrderStatus) -> bool:
    return next_status in _ALLOWED[current]


def transition(current: OrderStatus, next_status: OrderStatus) -> OrderStatus:
    """Apply a transition or raise ``InvalidTransition``."""
    if not can_transition(current, next_status):
        raise InvalidTransition(f"ARCH-003 forbids {current.value} -> {next_status.value}")
    return next_status
