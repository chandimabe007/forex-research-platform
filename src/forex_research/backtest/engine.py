"""Backtest engine — BT-001, BT-010, BT-011, BT-012, BT-020, BT-021, BT-030.

Event-driven, single pass (BT-001): the engine holds no data beyond the
current timestamp. The tests (VAL-060..062), not the design, establish the
absence of look-ahead; ordering is ARCH-002, applied identically live:

    pending-order trigger (5) -> fill (6) -> broker-side SL/TP (7)
    -> strategy decision (8) -> new order submission (9)

Fill rule (BT-010): first eligible quote at or after ``decision_time +
latency`` — filling at a bar's open when the order arrives after that open is
not conservative, it is impossible.

Intrabar ordering (BT-011): a queued order filling at the start of a bar
whose stop is then touched in the same bar is filled FIRST and its exit
resolved SECOND, at tick granularity, before any new signal is computed.

Exits resolve on the side the position closes at (BT-012): a long closes by
selling at the bid; a short closes by buying at the ask. Spread widening is
modelled on both sides.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import polars as pl

from ..core.state_machine import OrderStatus, transition
from ..core.units import D, round_volume_down
from ..costs.commission import FeeSchedule
from ..costs.fill import FillEngine, FillReport
from ..data.instruments import InstrumentSpec
from ..strategy.base import (
    TIF,
    MarketState,
    OrderIntent,
    OrderType,
    Side,
)


@dataclass
class PendingOrder:
    intent: OrderIntent
    submitted_at: datetime
    status: OrderStatus


@dataclass
class Position:
    side: Side
    volume: Decimal
    entry_price: Decimal
    entry_ts: datetime
    stop_loss: Decimal
    take_profit: Decimal | None
    risk_amount: Decimal  # money at risk at entry (entry-to-stop x value)


@dataclass
class Trade:
    symbol: str
    side: str
    volume: Decimal
    entry_price: Decimal
    entry_ts: datetime
    exit_price: Decimal
    exit_ts: datetime
    pnl: Decimal
    r_multiple: Decimal | None
    exit_reason: str  # closed_sl | closed_tp | closed_manual
    resolution_method: str  # tick | m1 | bounds (BT-020)


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    rejected_orders: list[str] = field(default_factory=list)
    ambiguous_exits: int = 0
    resolution_methods: dict[str, int] = field(default_factory=dict)
    weekend_gap_exits: int = 0

    def ambiguity_budget(self) -> Decimal:
        """BT-021: ambiguous exits as a share of all exits; >5% means stops
        are too tight for the available resolution."""
        total = len(self.trades)
        if total == 0:
            return Decimal(0)
        return (D(self.ambiguous_exits) / D(total)) * 100


class TickBacktester:
    """Single-pass tick replay with the canonical fill rules."""

    def __init__(
        self,
        *,
        spec: InstrumentSpec,
        fees: FeeSchedule,
        latency: timedelta,
        value_per_point_per_lot: Decimal,
        slippage_pips_sampler: Callable[[OrderIntent, datetime], Decimal] | None = None,
        stop_exit_spread_sampler: Callable[[OrderIntent, datetime], Decimal] | None = None,
        no_entry_window_before_session_close: timedelta = timedelta(minutes=5),
        session_close_at: datetime | None = None,
        contract_size: Decimal | None = None,
    ) -> None:
        self.spec = spec
        self.fees = fees
        self.latency = latency
        self.value_per_point = D(value_per_point_per_lot)
        self._slippage = slippage_pips_sampler
        self._stop_exit_spread = stop_exit_spread_sampler
        self._no_entry_window = no_entry_window_before_session_close
        self._session_close_at = session_close_at
        self._contract = D(contract_size) if contract_size else spec.contract_size

    # -- helpers ------------------------------------------------------------
    def _quote_fn(self, ticks: pl.DataFrame):
        """BT-010 canonical quote lookup: first tick at or after t."""
        ts_col = ticks["ts"].to_list()
        bid_col = [D(str(b)) for b in ticks["bid"].to_list()]
        ask_col = [D(str(a)) for a in ticks["ask"].to_list()]
        import bisect

        def quote_at(t: datetime) -> tuple[Decimal, Decimal] | None:
            idx = bisect.bisect_left(ts_col, t)
            if idx >= len(ts_col):
                return None
            return bid_col[idx], ask_col[idx]

        return quote_at

    def _round_volume(self, volume: Decimal) -> Decimal:
        """BT-030: round to volume_step and RECOMPUTE risk from the rounded
        volume — ARCH-001 conservative direction (down)."""
        return round_volume_down(volume, step=self.spec.volume_step)

    def _pnl(self, side: Side, volume: Decimal, entry: Decimal, exit_: Decimal) -> Decimal:
        points = (exit_ - entry) if side is Side.BUY else (entry - exit_)
        lots_volume = volume * self._contract
        return points * lots_volume

    # -- main loop ----------------------------------------------------------
    def run(
        self,
        *,
        symbol: str,
        ticks: pl.DataFrame,
        features: pl.DataFrame,
        strategy,
    ) -> BacktestResult:
        result = BacktestResult()
        quote_at = self._quote_fn(ticks)
        fill_engine = FillEngine(
            quote_at=quote_at, latency=self.latency, slippage_pips_sampler=self._slippage
        )
        fill_engine.set_instrument(pip_size=self.spec.pip_size)

        ts_list = ticks["ts"].to_list()
        bid_list = [D(str(b)) for b in ticks["bid"].to_list()]
        ask_list = [D(str(a)) for a in ticks["ask"].to_list()]

        position: Position | None = None
        pending: list[PendingOrder] = []
        prev_ts: datetime | None = None
        feature_index = 0
        avail = features["available_at"].to_list() if not features.is_empty() else []

        for i, now in enumerate(ts_list):
            bid, ask = bid_list[i], ask_list[i]

            # (5) pending-order triggers, (6) fills — before SL/TP (BT-011).
            still_pending: list[PendingOrder] = []
            for po in pending:
                if (
                    po.intent.time_in_force is TIF.GTD
                    and po.intent.expiry is not None
                    and now > po.intent.expiry
                ):
                    # GTD expiry: the order leaves the book BEFORE any trigger
                    # check — it must never fill after its expiry.
                    result.rejected_orders.append("GTD order expired unfilled (abandoned)")
                    continue
                filled = self._try_fill_pending(po, now, fill_engine, result, symbol)
                if filled is not None and position is None:
                    position = filled
                elif filled is not None:
                    result.rejected_orders.append("position already open; intent abandoned")
                else:
                    still_pending.append(po)
            pending = still_pending

            # (7) broker-side SL/TP on the closing side (BT-012). An exit
            # resolved at the first tick after a weekend gap is counted
            # separately (BT-030): the gap carried the exit price away from
            # the stop level, and that tail must be visible in the results.
            if position is not None:
                exit_ = self._check_exits(
                    position,
                    bid,
                    ask,
                    now,
                    fill_engine,
                    result,
                    symbol,
                    after_weekend_gap=(prev_ts is not None and _spans_weekend(prev_ts, now)),
                )
                if exit_ is not None:
                    position = None

            # (8) strategy decision — features with available_at <= now only.
            while feature_index < len(avail) and avail[feature_index] <= now:
                feature_index += 1
            visible = features.slice(0, feature_index)
            state = MarketState(now=now, symbol=symbol, bid=bid, ask=ask, features=visible)
            signal = strategy.evaluate(state)

            # (9) new order submission.
            if signal is not None and position is None:
                intent = signal.intent
                if self._session_close_at and now >= self._session_close_at - self._no_entry_window:
                    result.rejected_orders.append(
                        "inside no-entry window of session close (BT-030)"
                    )
                elif intent.volume < self.spec.volume_min:
                    result.rejected_orders.append("volume below volume_min (BT-030)")
                else:
                    rounded = self._round_volume(intent.volume)
                    if rounded < self.spec.volume_min:
                        result.rejected_orders.append("rounded volume below volume_min (BT-030)")
                    else:
                        intent = _reprice_intent_volume(intent, rounded)
                        if intent.order_type is OrderType.MARKET:
                            # Market entries fill at the canonical rule immediately.
                            report = fill_engine.fill_market(
                                intent, decision_time=now, pip_size=self.spec.pip_size
                            )
                            if report is not None:
                                position = self._open_position(intent, report, now, symbol, result)
                        else:
                            # LIMIT/STOP rest as pending until their trigger
                            # (ARCH-002 position 5 precedes the exit check 7).
                            pending.append(
                                PendingOrder(
                                    intent=intent, submitted_at=now, status=OrderStatus.SUBMITTED
                                )
                            )
            prev_ts = now
        # Unresolved pending orders at stream end are abandoned (logged upstream).
        return result

    # -- fill/exit internals --------------------------------------------------
    def _try_fill_pending(
        self, po: PendingOrder, now, fill_engine, result, symbol
    ) -> Position | None:
        # A resting order is triggered by the CURRENT tick: ``now`` is the
        # arrival context, so the canonical rule (first quote at or after the
        # trigger) resolves against the tick being examined (BT-010/011).
        intent = po.intent
        if intent.order_type is OrderType.LIMIT:
            report = fill_engine.fill_limit(intent, decision_time=now)
        elif intent.order_type is OrderType.STOP:
            report = fill_engine.fill_stop(intent, decision_time=now)
        else:
            report = fill_engine.fill_market(intent, decision_time=now, pip_size=self.spec.pip_size)
        if report is None:
            return None  # not triggered on this tick; stays resting
        transition(OrderStatus.SUBMITTED, OrderStatus.FILLED)
        return self._open_position(intent, report, po.submitted_at, symbol, result)

    def _open_position(
        self,
        intent: OrderIntent,
        report: FillReport,
        decision_time,
        symbol: str,
        result: BacktestResult,
    ) -> Position:
        stop_distance = abs(report.fill_price - intent.stop_loss)
        risk = stop_distance * intent.volume * self._contract
        return Position(
            side=intent.side,
            volume=intent.volume,
            entry_price=report.fill_price,
            entry_ts=report.filled_at,
            stop_loss=intent.stop_loss,
            take_profit=intent.take_profit,
            risk_amount=risk,
        )

    def _check_exits(
        self,
        position: Position,
        bid: Decimal,
        ask: Decimal,
        now,
        fill_engine,
        result: BacktestResult,
        symbol: str,
        *,
        after_weekend_gap: bool = False,
    ) -> Trade | None:
        """Broker-side SL/TP evaluated against the side the position closes
        at (BT-012). Long exits at bid; short exits at ask."""
        close_quote = bid if position.side is Side.BUY else ask
        stopped = (position.side is Side.BUY and close_quote <= position.stop_loss) or (
            position.side is Side.SELL and close_quote >= position.stop_loss
        )
        target_hit = position.take_profit is not None and (
            (position.side is Side.BUY and close_quote >= position.take_profit)
            or (position.side is Side.SELL and close_quote <= position.take_profit)
        )
        if not stopped and not target_hit:
            return None

        exit_reason = "closed_sl" if stopped else "closed_tp"
        # Stop exits take adverse slippage from the stop-exit model (COST-012);
        # targets fill at the level or not at all (COST-015).
        exit_price = close_quote
        if stopped:
            adverse = (
                self._stop_exit_spread(  # type: ignore[misc]
                    _intent_from_position(position), now
                )
                if self._stop_exit_spread
                else Decimal(0)
            ) * self.spec.pip_size
            exit_price = (
                (close_quote - adverse) if position.side is Side.BUY else (close_quote + adverse)
            )
        elif position.take_profit is not None:
            exit_price = position.take_profit

        pnl = self._pnl(position.side, position.volume, position.entry_price, exit_price)
        commission = self.fees.commission(position.volume)
        net = pnl - commission
        r_multiple = (net / position.risk_amount) if position.risk_amount > 0 else None
        method = "tick"  # tick replay resolves exactly (BT-020 default)
        result.resolution_methods[method] = result.resolution_methods.get(method, 0) + 1
        # Ambiguity at tick granularity is impossible unless stop and target
        # are both inside one tick's range; the budget stays instrumented.
        if stopped and target_hit:
            result.ambiguous_exits += 1
        trade = Trade(
            symbol=symbol,
            side=position.side.value,
            volume=position.volume,
            entry_price=position.entry_price,
            entry_ts=position.entry_ts,
            exit_price=exit_price,
            exit_ts=now,
            pnl=net,
            r_multiple=r_multiple,
            exit_reason=exit_reason,
            resolution_method=method,
        )
        result.trades.append(trade)
        if after_weekend_gap:
            result.weekend_gap_exits += 1
        return trade


def _spans_weekend(a: datetime, b: datetime) -> bool:
    """True when the closed interval [a, b] contains a Saturday (UTC) — the
    FX market's weekly close. An exit resolved on the first tick after such
    a gap is a weekend-gap exit (BT-030)."""
    day = a.date()
    last = b.date()
    while day <= last:
        if day.weekday() == 5:  # Saturday
            return True
        day += timedelta(days=1)
    return False


def _intent_from_position(position: Position) -> OrderIntent:
    return OrderIntent(
        side=position.side,
        order_type=OrderType.MARKET,
        limit_or_stop_price=None,
        stop_loss=position.stop_loss,
        take_profit=position.take_profit,
        volume=position.volume,
    )


def _reprice_intent_volume(intent: OrderIntent, volume: Decimal) -> OrderIntent:
    return OrderIntent(
        side=intent.side,
        order_type=intent.order_type,
        limit_or_stop_price=intent.limit_or_stop_price,
        stop_loss=intent.stop_loss,
        take_profit=intent.take_profit,
        volume=volume,
        time_in_force=intent.time_in_force,
        expiry=intent.expiry,
        max_slippage_points=intent.max_slippage_points,
        cancel_rule=intent.cancel_rule,
    )
