"""Risk engine — RISK-001, RISK-010, RISK-011, RISK-012, RISK-013, RISK-020.

Final authority, reduce-only (RISK-001): the risk engine can only reduce or
veto, never increase. Sizing is bounded by headroom, not just equity
(RISK-010); unreadable state is a veto (RISK-011); foreign positions are
vetoed, not sized around (RISK-012); pending and in-flight orders reserve
risk atomically at account level (RISK-013); stops are gap-adjusted
(RISK-020).

Volume rounds DOWN to volume_step and risk is RECOMPUTED from the rounded
volume (ARCH-001, RISK-010 step 6-7).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..core.units import D, round_volume_down
from ..data.instruments import InstrumentSpec


@dataclass(frozen=True)
class AccountSnapshot:
    """Everything sizing needs from the account. ``readable=False`` vetoes."""

    readable: bool
    equity: Decimal
    balance: Decimal
    margin_available: Decimal
    daily_loss_so_far: Decimal  # realised + floating against today's floor
    foreign_positions_present: bool  # RISK-012


@dataclass(frozen=True)
class RiskLimits:
    """Declared configuration with rationale (OPS-050, RISK-030 defaults)."""

    per_trade_risk_pct: Decimal = Decimal("0.005")  # 0.5%
    total_open_risk_pct: Decimal = Decimal("0.02")  # 2.0%
    daily_loss_soft_pct: Decimal = Decimal("0.60")  # of the firm's daily limit
    max_concurrent_positions: int = 4


@dataclass(frozen=True)
class RiskDecision:
    accepted: bool
    reason: str
    volume: Decimal | None = None
    risk_amount: Decimal | None = None
    effective_stop: Decimal | None = None


@dataclass
class Reservation:
    """A pending or in-flight order's reserved risk (RISK-013)."""

    key: str
    max_loss: Decimal
    margin: Decimal
    created_at: datetime


class ReservationLedger:
    """Account-level reservations, released only on confirmed outcomes.

    A timeout does NOT release a reservation: an order whose state is unknown
    is assumed live (RISK-013). Release happens on confirmed cancellation,
    confirmed rejection, or reconciliation showing the order no longer
    exists. On a fill the reservation converts into the open position's risk.
    """

    def __init__(self) -> None:
        self._reservations: dict[str, Reservation] = {}

    def reserve(self, reservation: Reservation) -> None:
        self._reservations[reservation.key] = reservation

    def release(self, key: str) -> bool:
        return self._reservations.pop(key, None) is not None

    def total_max_loss(self) -> Decimal:
        return sum((r.max_loss for r in self._reservations.values()), Decimal(0))

    def total_margin(self) -> Decimal:
        return sum((r.margin for r in self._reservations.values()), Decimal(0))

    def __len__(self) -> int:
        return len(self._reservations)


class RiskEngine:
    def __init__(self, *, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

    # -- sizing ---------------------------------------------------------------
    def size_position(
        self,
        *,
        account: AccountSnapshot,
        open_risk: Decimal,
        open_margin: Decimal,
        reservations: ReservationLedger,
        stop_distance: Decimal,
        spec: InstrumentSpec,
        value_per_price_unit_per_lot: Decimal,
        gap_95: Decimal,
        daily_limit: Decimal,
    ) -> RiskDecision:
        """RISK-010. Each step can only reduce; order matters."""
        # 1. VETO if account state could not be read (RISK-011): no override.
        if not account.readable:
            return RiskDecision(False, "RISK-011: account state unreadable — veto")
        # RISK-012: foreign positions veto rather than size around.
        if account.foreign_positions_present:
            return RiskDecision(
                False, "RISK-012: foreign positions present — veto, incident raised"
            )

        equity = D(account.equity)
        # 2. risk_budget = min of every headroom, net of reservations (RISK-013).
        configured = equity * D(self.limits.per_trade_risk_pct)
        daily_headroom = daily_limit * D(self.limits.daily_loss_soft_pct) - D(
            account.daily_loss_so_far
        )
        max_headroom = (
            equity * D(self.limits.total_open_risk_pct)
            - D(open_risk)
            - reservations.total_max_loss()
        )
        exposure_headroom = max_headroom  # total-open-risk basis
        margin_headroom = D(account.margin_available) - reservations.total_margin()
        risk_budget = min(
            configured, daily_headroom, max_headroom, exposure_headroom, margin_headroom
        )
        if risk_budget <= 0:
            return RiskDecision(False, "headroom exhausted — no capacity (RISK-010)")

        # 3-4. effective stop: the entry-to-stop distance inclusive of spread
        # at the stop (GATE-021 one basis), floored by the gap adjustment.
        effective_stop = max(D(stop_distance), D(gap_95))  # RISK-020

        # 5. raw volume from the gap-adjusted stop.
        raw_volume = risk_budget / (effective_stop * D(value_per_price_unit_per_lot))
        # 6. floor to volume_step.
        volume = round_volume_down(raw_volume, step=spec.volume_step)
        # 8. rejects, never round-ups.
        if volume < spec.volume_min:
            return RiskDecision(
                False,
                "rounded volume below volume_min — rejected, not rounded up (RISK-010)",
                effective_stop=effective_stop,
            )
        if volume > spec.volume_max:
            volume = spec.volume_max
        # 7. RECOMPUTE risk from the rounded volume.
        risk_amount = effective_stop * volume * D(value_per_price_unit_per_lot)
        if risk_amount > risk_budget:
            return RiskDecision(
                False,
                "recomputed risk exceeds headroom after rounding (RISK-010)",
                effective_stop=effective_stop,
            )
        return RiskDecision(
            True,
            "accepted (RISK-010)",
            volume=volume,
            risk_amount=risk_amount,
            effective_stop=effective_stop,
        )

    # -- reservations ----------------------------------------------------------
    def try_reserve(
        self,
        *,
        ledger: ReservationLedger,
        open_risk: Decimal,
        open_margin: Decimal,
        key: str,
        max_loss: Decimal,
        margin: Decimal,
        equity: Decimal,
        now: datetime,
    ) -> RiskDecision:
        """Atomic accept-or-refuse of a pending order's risk (RISK-013).

        The headroom check and the reservation are one operation: two pending
        entries that are individually within limits but jointly over them are
        refused at the second acceptance.
        """
        room = equity * D(self.limits.total_open_risk_pct) - D(open_risk) - ledger.total_max_loss()
        if max_loss > room:
            return RiskDecision(
                False,
                "RISK-013: reservation refused — pending orders would jointly exceed "
                "the account limit",
            )
        ledger.reserve(Reservation(key=key, max_loss=D(max_loss), margin=D(margin), created_at=now))
        return RiskDecision(True, "reservation accepted (RISK-013)")
