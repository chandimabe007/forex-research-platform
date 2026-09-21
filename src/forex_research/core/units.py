"""Typed units, not floats — ARCH-001.

Money, price, points, pips, volume and R are distinct types. Prices and money
use integer minor units or Decimal — never ``float`` at a comparison that
decides a breach. Pip vs point conversion always goes through an instrument
definition (``data.instruments.InstrumentSpec``, DATA-002), never a constant.

Rounding direction is specified at every broker boundary and always
conservative: volume down, cost up, stop distance up (ARCH-001).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal


class UnitError(ValueError):
    """Raised where a unit conversion or rounding would be ambiguous."""


def D(value: str | int | Decimal) -> Decimal:
    """Construct a Decimal exactly. Floats are rejected by design (ARCH-001)."""
    if isinstance(value, float):
        raise UnitError(
            "float is not accepted at broker boundaries (ARCH-001); pass a string, int or Decimal"
        )
    return Decimal(value)


@dataclass(frozen=True, slots=True)
class Pip:
    """One pip for a specific instrument — 0.0001 on EURUSD, 0.01 on USDJPY."""

    value: Decimal


@dataclass(frozen=True, slots=True)
class Point:
    """One point for a specific instrument — 0.00001 on EURUSD, 0.001 on USDJPY."""

    value: Decimal


def pips_to_price(pips: Decimal, *, pip_size: Decimal) -> Decimal:
    """Convert pips to a price distance through the instrument's pip size."""
    return D(pips) * D(pip_size)


def price_to_pips(distance: Decimal, *, pip_size: Decimal) -> Decimal:
    """Convert a price distance to pips through the instrument's pip size."""
    if D(pip_size) <= 0:
        raise UnitError("pip_size must be positive")
    return D(distance) / D(pip_size)


def round_volume_down(volume: Decimal, *, step: Decimal) -> Decimal:
    """Round volume DOWN to volume_step (ARCH-001: conservative direction)."""
    if D(step) <= 0:
        raise UnitError("volume step must be positive")
    steps = (D(volume) / D(step)).to_integral_value(rounding=ROUND_FLOOR)
    return steps * D(step)


def round_cost_up(value: Decimal, *, quantum: Decimal) -> Decimal:
    """Round a cost UP to the instrument's tick size (ARCH-001: conservative)."""
    if D(quantum) <= 0:
        raise UnitError("quantum must be positive")
    units = (D(value) / D(quantum)).to_integral_value(rounding=ROUND_CEILING)
    return units * D(quantum)


def round_stop_distance_up(distance: Decimal, *, point_size: Decimal) -> Decimal:
    """Round a stop distance UP to whole points (ARCH-001: conservative)."""
    if D(point_size) <= 0:
        raise UnitError("point_size must be positive")
    points = (D(distance) / D(point_size)).to_integral_value(rounding=ROUND_CEILING)
    return points * D(point_size)


def round_half_even(value: Decimal, *, places: int) -> Decimal:
    """Neutral rounding for presentation only — never for a breach comparison."""
    if places < 0:
        raise UnitError("places must be >= 0")
    quantum = Decimal(1).scaleb(-places)
    return D(value).quantize(quantum, rounding=ROUND_HALF_EVEN)
