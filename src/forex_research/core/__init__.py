"""Core primitives shared by every engine (ARCH-001, ARCH-002, ARCH-003)."""

from .events import EventKind, EventPositionError, VenueEventConfig, validate_venue_config
from .state_machine import InvalidTransition, OrderStatus, can_transition, transition
from .units import (
    D,
    Pip,
    Point,
    UnitError,
    pips_to_price,
    price_to_pips,
    round_cost_up,
    round_half_even,
    round_stop_distance_up,
    round_volume_down,
)

__all__ = [
    "D",
    "EventKind",
    "EventPositionError",
    "InvalidTransition",
    "OrderStatus",
    "Pip",
    "Point",
    "UnitError",
    "VenueEventConfig",
    "can_transition",
    "pips_to_price",
    "price_to_pips",
    "round_cost_up",
    "round_half_even",
    "round_stop_distance_up",
    "round_volume_down",
    "transition",
    "validate_venue_config",
]
