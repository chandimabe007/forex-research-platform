"""Toy strategy — MILE-033.

One deliberately trivial strategy — NOT a research candidate — carried
through sizing and rule evaluation to prove the path end to end. It reads
only registered features whose ``available_at <= state.now`` (STRAT-001),
returns None on most bars (``None`` is the expected return most of the
time), and registers its falsification criterion as a constant so nothing
downstream can invent one after a run (STRAT-021 spirit).
"""

from __future__ import annotations

from decimal import Decimal

from .base import MarketState, OrderIntent, OrderType, Side, Signal


class PullbackToy:
    """Buy a one-bar pullback inside an up-trend. Nothing more."""

    name = "pullback_toy"
    version = "1"
    required_features = ["ret_1", "trend_state"]
    active_regimes: list[str] = []  # empty means all

    #: Registered BEFORE any run (STRAT-020/STRAT-021): what would falsify it.
    FALSIFIED_IF = "expectancy at or below zero after costs across regimes"

    def __init__(
        self,
        *,
        stop_pips: Decimal = Decimal("10"),
        target_pips: Decimal = Decimal("20"),
        volume: Decimal = Decimal("0.10"),
        pip_size: Decimal = Decimal("0.0001"),
    ) -> None:
        self.stop_pips = Decimal(stop_pips)
        self.target_pips = Decimal(target_pips)
        self.volume = Decimal(volume)
        self.pip_size = Decimal(pip_size)

    def evaluate(self, state: MarketState) -> Signal | None:
        features = state.features
        if features.is_empty():
            return None
        latest = features.sort("available_at").row(-1, named=True)
        ret = latest.get("ret_1")
        trend = latest.get("trend_state")
        if ret is None or trend is None:
            return None  # missing features are NO_TRADE, never zero (FEAT-002)
        if trend == "up" and ret < 0:
            entry = state.ask
            stop = entry - self.stop_pips * self.pip_size
            target = entry + self.target_pips * self.pip_size
            intent = OrderIntent(
                side=Side.BUY,
                order_type=OrderType.MARKET,
                limit_or_stop_price=None,
                stop_loss=stop,
                take_profit=target,
                volume=self.volume,
            )
            return Signal(intent=intent)
        return None
