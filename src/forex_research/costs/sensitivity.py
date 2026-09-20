"""Sensitivity surface — COST-017.

Multipliers 0.75x to 2.0x in 0.25 steps, run in both directions because
non-venue spread may be pessimistic as well as optimistic.

Rejection: 1.25x removing more than half the net expectancy marks a cost
artefact.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

MULTIPLIERS = (Decimal("0.75"), Decimal("1.0"), Decimal("1.25"), Decimal("1.5"),
               Decimal("1.75"), Decimal("2.0"))


@dataclass(frozen=True)
class SensitivityPoint:
    multiplier: Decimal
    net_expectancy: Decimal


@dataclass(frozen=True)
class SensitivitySurface:
    points: tuple[SensitivityPoint, ...]

    def is_cost_artefact(self) -> bool:
        """COST-017 rejection: 1.25x removing more than half the expectancy."""
        base = next((p for p in self.points if p.multiplier == Decimal("1.0")), None)
        at_125 = next((p for p in self.points if p.multiplier == Decimal("1.25")), None)
        if base is None or at_125 is None or base.net_expectancy <= 0:
            return False
        removed = base.net_expectancy - at_125.net_expectancy
        return removed > base.net_expectancy / Decimal(2)


def run_sensitivity(net_expectancy_at) -> SensitivitySurface:
    """``net_expectancy_at(multiplier) -> Decimal`` is supplied by the caller
    (a full backtest per multiplier)."""
    points = tuple(
        SensitivityPoint(multiplier=m, net_expectancy=net_expectancy_at(m))
        for m in MULTIPLIERS
    )
    return SensitivitySurface(points=points)
