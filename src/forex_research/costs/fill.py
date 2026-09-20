"""Fill equation — COST-015.

One fill equation. Price movement during latency and residual execution
slippage are separate terms, and each is counted once::

    arrival_quote = quote on the fill side at (decision_time + latency)
    fill_price    = arrival_quote + residual_slippage        # adverse sign
    latency_move  = arrival_quote − quote at decision_time   # reported, never sampled
    residual      = fill_price − arrival_quote               # the only sampled term

Buy fills at ask plus adverse slippage; sell fills at bid minus adverse
slippage. **Spread is charged once.** A zero-slippage fixture must return
exactly the quoted ask (entry) — never ask plus half a spread again.

Residual slippage is conditioned on information available **at submission**:
realised volatility over bars closed before decision_time, spread at
submission, session, calendar proximity. The realised range of the bar
being traded is not known when the order is sent and must not be used.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from random import Random

from ..strategy.base import OrderIntent, Side


@dataclass(frozen=True)
class FillReport:
    fill_price: Decimal
    arrival_quote: Decimal
    quote_at_decision: Decimal
    latency_move: Decimal   # reported, never sampled (COST-015)
    residual: Decimal       # the only sampled term
    side: str
    filled_at: datetime
    rule: str  # which BT-010 fill rule produced this


class ZeroSlippageFixtureError(AssertionError):
    """A zero-slippage run must return exactly the quoted side price."""


class FillEngine:
    """Implements the COST-015 equation over a tick replay (BT-010)."""

    def __init__(
        self,
        *,
        quote_at: Callable[[datetime], tuple[Decimal, Decimal] | None],
        latency: timedelta,
        slippage_pips_sampler: Callable[[OrderIntent, datetime], Decimal] | None = None,
        rng: Random | None = None,
    ) -> None:
        self._quote_at = quote_at
        self._latency = latency
        self._slippage_sampler = slippage_pips_sampler or self._zero_residual
        self._rng = rng or Random()
        self._pip_size: Decimal = Decimal("0.0001")

    def set_instrument(self, *, pip_size: Decimal) -> None:
        self._pip_size = Decimal(pip_size)

    @staticmethod
    def _zero_residual(intent: OrderIntent, at: datetime) -> Decimal:
        return Decimal(0)

    def fill_market(
        self,
        intent: OrderIntent,
        *,
        decision_time: datetime,
        pip_size: Decimal | None = None,
    ) -> FillReport | None:
        """Canonical BT-010 rule: first eligible quote at or after
        ``decision_time + latency``. Returns None if no quote is eligible."""
        pip = pip_size or self._pip_size
        arrival = decision_time + self._latency
        quote = self._quote_at(arrival)
        if quote is None:
            return None
        bid, ask = quote
        quote_at_decision = self._quote_at(decision_time)
        decision_ref = (
            (quote_at_decision[1] if intent.side is Side.BUY else quote_at_decision[0])
            if quote_at_decision
            else (ask if intent.side is Side.BUY else bid)
        )
        arrival_quote = ask if intent.side is Side.BUY else bid

        residual_pips = self._slippage_sampler(intent, decision_time)
        residual = residual_pips * pip
        if intent.side is Side.BUY:
            fill_price = arrival_quote + residual  # adverse = pay more
        else:
            fill_price = arrival_quote - residual  # adverse = receive less

        return FillReport(
            fill_price=fill_price,
            arrival_quote=arrival_quote,
            quote_at_decision=decision_ref,
            latency_move=arrival_quote - decision_ref,
            residual=residual if intent.side is Side.BUY else -residual,
            side=intent.side.value,
            filled_at=arrival,
            rule="BT-010 canonical: first quote at or after decision_time + latency",
        )

    def fill_limit(
        self,
        intent: OrderIntent,
        *,
        decision_time: datetime,
    ) -> FillReport | None:
        """Take profit / limit: no favourable slippage; fills at the level or
        not at all (COST-015 table)."""
        arrival = decision_time + self._latency
        quote = self._quote_at(arrival)
        if quote is None or intent.limit_or_stop_price is None:
            return None
        bid, ask = quote
        level = intent.limit_or_stop_price
        if intent.side is Side.BUY and ask <= level:
            price = level
        elif intent.side is Side.SELL and bid >= level:
            price = level
        else:
            return None
        return FillReport(
            fill_price=price,
            arrival_quote=ask if intent.side is Side.BUY else bid,
            quote_at_decision=price,
            latency_move=Decimal(0),
            residual=Decimal(0),
            side=intent.side.value,
            filled_at=arrival,
            rule="COST-015 limit: at the level or not at all",
        )

    def fill_stop(
        self,
        intent: OrderIntent,
        *,
        decision_time: datetime,
        stop_exit_spread_sampler=None,
    ) -> FillReport | None:
        """Stop loss / stop entry: adverse, fat-tailed; gaps through the stop
        are the tail. Stop exits use the stop-exit spread model (COST-012)."""
        arrival = decision_time + self._latency
        quote = self._quote_at(arrival)
        if quote is None:
            return None
        bid, ask = quote
        level = intent.limit_or_stop_price
        if level is None:
            return None
        # For a SELL-side stop ENTRY, fill once the bid falls to the level
        # (sell stops trigger on the bid); for a BUY-side stop ENTRY, once
        # the ask rises to it. Comment names the trigger quote, not the
        # position being stopped out.
        if intent.side is Side.SELL and bid <= level:  # sell stop triggered
            base = bid
        elif intent.side is Side.BUY and ask >= level:  # buy stop triggered
            base = ask
        else:
            return None
        residual = (
            stop_exit_spread_sampler(intent, decision_time)
            if stop_exit_spread_sampler
            else self._slippage_sampler(intent, decision_time)
        ) * self._pip_size
        if intent.side is Side.SELL:
            # Adverse for a sell = a lower fill price.
            fill_price = base - residual
        else:
            fill_price = base + residual
        return FillReport(
            fill_price=fill_price,
            arrival_quote=base,
            quote_at_decision=base,
            latency_move=Decimal(0),
            residual=residual if intent.side is Side.BUY else -residual,
            side=intent.side.value,
            filled_at=arrival,
            rule="COST-015 stop: adverse draw, stop-exit spread model",
        )
