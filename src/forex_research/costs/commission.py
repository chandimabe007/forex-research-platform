"""Commission, swap, calendar — COST-016.

Commission per lot round trip from the fee schedule, applied both legs. Swap
at each rollover held through, with triple swap on the instrument's
documented day — maintained as a per-instrument calendar including market
holidays rather than assumed. The schedule records its retrieval date.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

TRIPLE_SWAP_WEEKDAY_DEFAULT = 2  # Wednesday for most FX majors (0=Monday)


@dataclass(frozen=True)
class FeeSchedule:
    """Per-instrument fee schedule with provenance (COST-016)."""

    symbol: str
    commission_per_lot_round_trip: Decimal  # applied both legs
    swap_long_per_lot_per_day: Decimal
    swap_short_per_lot_per_day: Decimal
    triple_swap_weekdays: tuple[int, ...] = (TRIPLE_SWAP_WEEKDAY_DEFAULT,)
    holidays: tuple[date, ...] = field(default_factory=tuple)
    retrieved_at: datetime | None = None  # the schedule's retrieval date

    def commission(self, volume: Decimal) -> Decimal:
        """Full round-trip commission, booked on entry (conservative: cost up)."""
        return Decimal(volume) * self.commission_per_lot_round_trip

    def swap_for_night(
        self,
        *,
        side: str,
        volume: Decimal,
        night: date,
    ) -> Decimal:
        """Swap for holding through one rollover; triple on the documented day.

        The instrument's documented day governs — Wednesday for most majors,
        maintained per instrument, shifted for market holidays by the
        operator's calendar rather than assumed.
        """
        is_triple = night.weekday() in self.triple_swap_weekdays and night not in self.holidays
        rate = (
            self.swap_long_per_lot_per_day
            if side == "buy"
            else self.swap_short_per_lot_per_day
        )
        multiplier = Decimal(3) if is_triple else Decimal(1)
        return Decimal(volume) * rate * multiplier
