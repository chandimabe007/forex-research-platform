"""Strategy interface — STRAT-001, STRAT-002.

``None`` is the expected return most of the time: a strategy signalling on a
large fraction of bars has no selectivity. Strategies read only features
whose ``available_at <= state.now`` — no raw data access, no I/O, no mutable
state between calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

import polars as pl


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class TIF(StrEnum):
    GTC = "gtc"
    DAY = "day"
    GTD = "gtd"


class CancelRule(StrEnum):
    NONE = "none"
    ON_OPPOSITE_SIGNAL = "on_opposite_signal"
    ON_SESSION_CLOSE = "on_session_close"


@dataclass(frozen=True)
class OrderIntent:
    """An intent carries what it takes to simulate a pullback, a retest and a
    breakout consistently (STRAT-002)."""

    side: Side
    order_type: OrderType
    limit_or_stop_price: Decimal | None
    stop_loss: Decimal
    take_profit: Decimal | None
    volume: Decimal
    time_in_force: TIF = TIF.GTC
    expiry: datetime | None = None
    max_slippage_points: int = 0  # abandon rather than fill worse
    cancel_rule: CancelRule = CancelRule.NONE


@dataclass(frozen=True)
class MarketState:
    """What a strategy may see: features keyed by available_at, the current
    quote, and the time now. Nothing else."""

    now: datetime
    symbol: str
    bid: Decimal
    ask: Decimal
    features: pl.DataFrame  # rows with available_at <= now only


@dataclass(frozen=True)
class Signal:
    intent: OrderIntent


@runtime_checkable
class Strategy(Protocol):
    name: str
    version: str
    required_features: list[str]
    active_regimes: list[str]  # empty means all

    def evaluate(self, state: MarketState) -> Signal | None: ...
