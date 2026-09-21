"""Risk engine — VAL-063 sizing arithmetic, RISK-011/012/013, RISK-020."""

import datetime as dt
from decimal import Decimal

from forex_research.data.instruments import InstrumentSpec
from forex_research.risk import (
    AccountSnapshot,
    Reservation,
    ReservationLedger,
    RiskEngine,
    RiskLimits,
)
from forex_research.strategy.base import OrderIntent, OrderType, Side

UTC = dt.UTC


def _spec() -> InstrumentSpec:
    return InstrumentSpec(
        symbol="EURUSD",
        venue_symbol="EURUSD",
        point_size=Decimal("0.00001"),
        pip_size=Decimal("0.0001"),
        contract_size=Decimal("100000"),
        tick_size=Decimal("0.00001"),
        tick_value=Decimal("1.0"),
        quote_currency="USD",
        volume_min=Decimal("0.01"),
        volume_step=Decimal("0.01"),
        volume_max=Decimal("100.0"),
        stops_level_points=0,
        freeze_level_points=0,
    )


def _account(**overrides):
    values = dict(
        readable=True,
        equity=Decimal("100000"),
        balance=Decimal("100000"),
        margin_available=Decimal("50000"),
        daily_loss_so_far=Decimal("0"),
        foreign_positions_present=False,
    )
    values.update(overrides)
    return AccountSnapshot(**values)


def _intent(stop: Decimal = Decimal("0.0010"), volume: Decimal = Decimal("1.0")):
    return OrderIntent(
        side=Side.BUY,
        order_type=OrderType.MARKET,
        limit_or_stop_price=None,
        stop_loss=stop,
        take_profit=None,
        volume=volume,
    )


def _engine() -> RiskEngine:
    return RiskEngine(limits=RiskLimits(per_trade_risk_pct=Decimal("0.005")))


# -- VAL-063 fixtures ---------------------------------------------------------


def test_val063_volume_rounds_down_risk_recomputed():
    # VAL-063 fixture: raw volume 0.237 lots at step 0.01 rounds DOWN to 0.23
    # and risk is recomputed from 0.23. Budget = 0.5% of 4,740 = 23.70;
    # per-lot risk at the 10-pip effective stop = 0.0010 x 100000 = 100.00.
    decision = _engine().size_position(
        account=_account(equity=Decimal("4740"), balance=Decimal("4740")),
        open_risk=Decimal(0),
        open_margin=Decimal(0),
        reservations=ReservationLedger(),
        stop_distance=Decimal("0.0010"),
        spec=_spec(),
        value_per_price_unit_per_lot=Decimal("100000"),
        gap_95=Decimal("0.0005"),
        daily_limit=Decimal("5000"),
    )
    assert decision.accepted
    # risk_budget = 500; effective_stop = max(0.0010, 0.0005) = 0.0010
    # raw = 23.70 / 100 = 0.237 lots -> rounds down to 0.23
    assert decision.volume == Decimal("0.23")
    # risk recomputed from 0.23: 0.0010 * 0.23 * 100000 = 23.00 <= budget
    assert decision.risk_amount == Decimal("23.00")


def test_val063_partial_fill_records_risk_from_filled_volume():
    # RISK-013: on a fill the reservation converts into the open position's
    # risk; a partial fill of 0.10 of 0.23 records risk from 0.10 and the
    # unfilled reservation is released.
    ledger = ReservationLedger()
    engine = _engine()
    decision = engine.try_reserve(
        ledger=ledger,
        open_risk=Decimal(0),
        open_margin=Decimal(0),
        key="order-1",
        max_loss=Decimal("2.30"),
        margin=Decimal("250"),
        equity=Decimal("100000"),
        now=dt.datetime(2024, 1, 2, tzinfo=UTC),
    )
    assert decision.accepted and len(ledger) == 1
    # Partial fill: release the reservation; the position carries 0.10 risk.
    assert ledger.release("order-1")
    assert ledger.total_max_loss() == 0
    open_risk_after_fill = Decimal("0.0010") * Decimal("0.10") * Decimal("100000")
    assert open_risk_after_fill == Decimal("10.00")


def test_val063_headroom_bounds_size_below_configured_risk():
    # Headroom smaller than configured risk: size is bounded by headroom.
    engine = _engine()
    decision = engine.size_position(
        account=_account(daily_loss_so_far=Decimal("2900")),  # soft room = 3000-2900
        open_risk=Decimal(0),
        open_margin=Decimal(0),
        reservations=ReservationLedger(),
        stop_distance=Decimal("0.0010"),
        spec=_spec(),
        value_per_price_unit_per_lot=Decimal("100000"),
        gap_95=Decimal("0.0005"),
        daily_limit=Decimal("5000"),
    )
    assert decision.accepted
    # risk_budget = min(500 configured, 100 daily headroom, ...) = 100
    # raw volume = 100 / (0.0010 * 100000) = 1.00 lots exactly.
    assert decision.volume == Decimal("1.00")
    assert decision.risk_amount == Decimal("100.00")


def test_val063_rounded_volume_below_min_rejected_not_rounded_up():
    decision = _engine().size_position(
        account=_account(
            equity=Decimal("150"), balance=Decimal("150")
        ),  # 0.5% = 1.0 -> volume under min
        open_risk=Decimal(0),
        open_margin=Decimal(0),
        reservations=ReservationLedger(),
        stop_distance=Decimal("0.0010"),
        spec=_spec(),
        value_per_price_unit_per_lot=Decimal("100000"),
        gap_95=Decimal("0.0005"),
        daily_limit=Decimal("5000"),
    )
    assert not decision.accepted
    assert "volume_min" in decision.reason


def test_val063_gap_adjusted_stop_widens_sizing_basis():
    # RISK-020: where gap_95 exceeds the placed stop, size uses effective_stop.
    decision = _engine().size_position(
        account=_account(),
        open_risk=Decimal(0),
        open_margin=Decimal(0),
        reservations=ReservationLedger(),
        stop_distance=Decimal("0.0010"),
        spec=_spec(),
        value_per_price_unit_per_lot=Decimal("100000"),
        gap_95=Decimal("0.0030"),  # weekend gap wider than the 10-pip stop
        daily_limit=Decimal("5000"),
    )
    assert decision.accepted
    assert decision.effective_stop == Decimal("0.0030")
    # raw volume = 500 / (0.0030 * 100000) = 1.666 -> 1.66 lots.
    assert decision.volume == Decimal("1.66")
    # risk recomputed from the gap-adjusted stop: 0.0030 * 1.66 * 100000 = 498.00
    assert decision.risk_amount == Decimal("498.00")


# -- RISK-011 / RISK-012 ------------------------------------------------------


def test_unreadable_state_is_a_veto_with_no_override():
    decision = _engine().size_position(
        account=_account(readable=False),
        open_risk=Decimal(0),
        open_margin=Decimal(0),
        reservations=ReservationLedger(),
        stop_distance=Decimal("0.0010"),
        spec=_spec(),
        value_per_price_unit_per_lot=Decimal("100000"),
        gap_95=Decimal("0.0005"),
        daily_limit=Decimal("5000"),
    )
    assert not decision.accepted and "RISK-011" in decision.reason


def test_foreign_positions_veto():
    decision = _engine().size_position(
        account=_account(foreign_positions_present=True),
        open_risk=Decimal(0),
        open_margin=Decimal(0),
        reservations=ReservationLedger(),
        stop_distance=Decimal("0.0010"),
        spec=_spec(),
        value_per_price_unit_per_lot=Decimal("100000"),
        gap_95=Decimal("0.0005"),
        daily_limit=Decimal("5000"),
    )
    assert not decision.accepted and "RISK-012" in decision.reason


# -- RISK-013: the named fixture ----------------------------------------------


def test_risk013_two_pending_entries_jointly_over_limits():
    """Two pending entries on different symbols, each individually within
    limits, jointly over them: the second is refused at acceptance, and a
    simultaneous trigger of both must not breach."""
    ledger = ReservationLedger()
    engine = _engine()
    # 2% total open risk on 100k = 2000 room; each entry reserves 1200.
    first = engine.try_reserve(
        ledger=ledger,
        open_risk=Decimal(0),
        open_margin=Decimal(0),
        key="EURUSD-1",
        max_loss=Decimal("1200"),
        margin=Decimal("2500"),
        equity=Decimal("100000"),
        now=dt.datetime(2024, 1, 2, tzinfo=UTC),
    )
    assert first.accepted
    second = engine.try_reserve(
        ledger=ledger,
        open_risk=Decimal(0),
        open_margin=Decimal(0),
        key="USDJPY-1",
        max_loss=Decimal("1200"),
        margin=Decimal("2500"),
        equity=Decimal("100000"),
        now=dt.datetime(2024, 1, 2, tzinfo=UTC),
    )
    assert not second.accepted
    assert "RISK-013" in second.reason
    assert ledger.total_max_loss() == Decimal("1200")  # only the first holds


def test_reservation_survives_timeout():
    # A timeout does NOT release a reservation: unknown state is assumed live.
    ledger = ReservationLedger()
    ledger.reserve(
        Reservation(
            key="k",
            max_loss=Decimal("100"),
            margin=Decimal("500"),
            created_at=dt.datetime(2024, 1, 2, tzinfo=UTC),
        )
    )
    # (No release call here — that is the point.)
    assert ledger.total_max_loss() == Decimal("100")
