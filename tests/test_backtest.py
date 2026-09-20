"""Backtest engine — BT-010/011/012 fills and exits, BT-021 budget, BT-030
realism, BT-040 golden path, BT-041 differential fixtures."""

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from forex_research.backtest import BacktestResult, TickBacktester
from forex_research.costs import FeeSchedule
from forex_research.data.instruments import InstrumentSpec
from forex_research.strategy.base import TIF, OrderIntent, OrderType, Side

UTC = dt.UTC
REPO = Path(__file__).resolve().parents[1]


def _spec() -> InstrumentSpec:
    return InstrumentSpec(
        symbol="EURUSD", venue_symbol="EURUSD",
        point_size=Decimal("0.00001"), pip_size=Decimal("0.0001"),
        contract_size=Decimal("100000"), tick_size=Decimal("0.00001"),
        tick_value=Decimal("1.0"), quote_currency="USD",
        volume_min=Decimal("0.01"), volume_step=Decimal("0.01"),
        volume_max=Decimal("100.0"), stops_level_points=0, freeze_level_points=0,
    )


def _fees() -> FeeSchedule:
    return FeeSchedule(
        symbol="EURUSD",
        commission_per_lot_round_trip=Decimal("7.00"),
        swap_long_per_lot_per_day=Decimal("0"),
        swap_short_per_lot_per_day=Decimal("0"),
    )


def _ticks(rows: list[tuple[dt.datetime, str, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts": [r[0] for r in rows],
            "bid": [float(r[1]) for r in rows],
            "ask": [float(r[2]) for r in rows],
        }
    )


def _empty_features() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "available_at": [dt.datetime(2024, 1, 1, tzinfo=UTC)],
            "ret_1": [None],
        }
    )


class ScriptedStrategy:
    """Emits a queued sequence of signals once each; no look-ahead inputs."""

    name = "scripted"
    version = "1"
    required_features: list[str] = []
    active_regimes: list[str] = []

    def __init__(self, signals: dict[int, OrderIntent]) -> None:
        self._signals = dict(signals)
        self._tick_index = 0

    def evaluate(self, state):
        idx = self._tick_index
        self._tick_index += 1
        intent = self._signals.get(idx)
        if intent is None or intent in ("emitted",):
            return None
        self._signals[idx] = "emitted"
        from forex_research.strategy.base import Signal

        return Signal(intent=intent)


def _t(minute: int, second: int = 0) -> dt.datetime:
    return dt.datetime(2024, 1, 2, 10, minute, second, tzinfo=UTC)


def _run(rows, strategy, *, volume_rounded_off=False):
    bt = TickBacktester(
        spec=_spec(), fees=_fees(), latency=dt.timedelta(0),
        value_per_point_per_lot=Decimal("10.0"),  # $10 per pip per lot on EURUSD
    )
    return bt.run(symbol="EURUSD", ticks=_ticks(rows), features=_empty_features(),
                  strategy=strategy)


def test_hand_worked_long_tp_winner():
    # BT-041 differential fixture, worked by hand:
    # Buy 0.10 at ask 1.10000; SL 1.09900 (10 pips -> $10 risk);
    # TP 1.10200 (20 pips -> $20 gross). Commission 0.70. Net 19.30. R = 1.93.
    rows = [
        (_t(0), "1.09995", "1.10000"),  # decision + entry fill at ask
        (_t(1), "1.10200", "1.10205"),  # bid reaches TP -> exit at level
    ]
    intent = OrderIntent(
        side=Side.BUY, order_type=OrderType.MARKET, limit_or_stop_price=None,
        stop_loss=Decimal("1.09900"), take_profit=Decimal("1.10200"),
        volume=Decimal("0.10"),
    )
    result = _run(rows, ScriptedStrategy({0: intent}))
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "closed_tp"
    assert trade.pnl == Decimal("19.30")
    assert trade.r_multiple == Decimal("1.93")


def test_hand_worked_long_stop_losser():
    # Stop: bid 1.09900 reached -> exit at the stop level (long closes at bid).
    # Gross -10.00, commission 0.70, net -10.70, R = -1.07.
    rows = [
        (_t(0), "1.09995", "1.10000"),
        (_t(1), "1.09900", "1.09905"),
    ]
    intent = OrderIntent(
        side=Side.BUY, order_type=OrderType.MARKET, limit_or_stop_price=None,
        stop_loss=Decimal("1.09900"), take_profit=Decimal("1.10200"),
        volume=Decimal("0.10"),
    )
    result = _run(rows, ScriptedStrategy({0: intent}))
    trade = result.trades[0]
    assert trade.exit_reason == "closed_sl"
    assert trade.pnl == Decimal("-10.70")
    assert trade.r_multiple == Decimal("-1.07")


def test_bt012_short_stop_fires_on_ask_spike_the_bid_never_reaches():
    # The spec's named fixture: a short's stop must be evaluated against the
    # ask. Here the ask spikes to 1.10105 while the bid stays at 1.10050 —
    # testing both sides against the bid would miss the stop entirely.
    rows = [
        (_t(0), "1.10000", "1.10005"),  # sell entry at bid 1.10000
        (_t(1), "1.10050", "1.10105"),  # ask spike: stop 1.10100 breached
    ]
    intent = OrderIntent(
        side=Side.SELL, order_type=OrderType.MARKET, limit_or_stop_price=None,
        stop_loss=Decimal("1.10100"), take_profit=Decimal("1.09800"),
        volume=Decimal("0.10"),
    )
    result = _run(rows, ScriptedStrategy({0: intent}))
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "closed_sl"
    assert trade.exit_price == Decimal("1.10105")  # filled at the spiked ask
    # Gross: (1.10000 - 1.10105) * 0.10 * 100000 = -10.50; net -11.20.
    assert trade.pnl == Decimal("-11.20")


def test_bt030_volume_rounds_down_and_risk_recomputes():
    # 0.237 lots rounds DOWN to 0.23; risk is recomputed from 0.23.
    rows = [
        (_t(0), "1.09995", "1.10000"),
        (_t(1), "1.10200", "1.10205"),
    ]
    intent = OrderIntent(
        side=Side.BUY, order_type=OrderType.MARKET, limit_or_stop_price=None,
        stop_loss=Decimal("1.09900"), take_profit=Decimal("1.10200"),
        volume=Decimal("0.237"),
    )
    result = _run(rows, ScriptedStrategy({0: intent}))
    trade = result.trades[0]
    assert trade.volume == Decimal("0.23")
    # Gross: 20 pips x $10/pip x 0.23 = 46.00; commission 1.61; net 44.39.
    assert trade.pnl == Decimal("44.39")
    # Risk recomputed from 0.23: 10 pips x $10 x 0.23 = 23.00 -> R = 44.39/23.00.
    assert trade.r_multiple == Decimal("1.93")


def test_bt030_rejects_rounded_volume_below_min():
    rows = [(_t(0), "1.09995", "1.10000")]
    intent = OrderIntent(
        side=Side.BUY, order_type=OrderType.MARKET, limit_or_stop_price=None,
        stop_loss=Decimal("1.09900"), take_profit=None, volume=Decimal("0.005"),
    )
    result = _run(rows, ScriptedStrategy({0: intent}))
    assert result.trades == []
    assert any("volume_min" in r for r in result.rejected_orders)


def test_pending_limit_fills_on_trigger_before_exits_bt011():
    # The pending limit fills when the trigger tick arrives, and the position
    # then exits normally — fill FIRST, exit SECOND (BT-011 intrabar order).
    rows = [
        (_t(0), "1.10000", "1.10005"),
        (_t(1), "1.09945", "1.09950"),  # ask <= limit level -> fill at level
        (_t(2), "1.09840", "1.09845"),  # bid <= SL 1.09850 -> stop exit
    ]
    intent = OrderIntent(
        side=Side.BUY, order_type=OrderType.LIMIT, limit_or_stop_price=Decimal("1.09950"),
        stop_loss=Decimal("1.09850"), take_profit=None, volume=Decimal("0.10"),
    )
    result = _run(rows, ScriptedStrategy({0: intent}))
    assert result.rejected_orders == []
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "closed_sl"
    # Entry was at the limit LEVEL, not the better trigger quote.
    assert trade.entry_price == Decimal("1.09950")


def test_bt021_ambiguity_budget_instrumented():
    # Tick replay resolves exactly (BT-020 default): even a tick that spans
    # both levels produces one deterministic resolution, not an ambiguity.
    rows = [
        (_t(0), "1.09995", "1.10000"),
        (_t(1), "1.09800", "1.10205"),
    ]
    intent = OrderIntent(
        side=Side.BUY, order_type=OrderType.MARKET, limit_or_stop_price=None,
        stop_loss=Decimal("1.09900"), take_profit=Decimal("1.10200"),
        volume=Decimal("0.10"),
    )
    result = _run(rows, ScriptedStrategy({0: intent}))
    assert result.ambiguous_exits == 0
    assert result.resolution_methods == {"tick": 1}

    # The budget arithmetic itself: ambiguous exits above 5% of all exits
    # flag stops as too tight for the available resolution (BT-021).
    result = BacktestResult()
    result.trades = [None] * 100
    result.ambiguous_exits = 3
    assert result.ambiguity_budget() == Decimal("3")
    result.ambiguous_exits = 6
    assert result.ambiguity_budget() > 5


def test_gtd_pending_order_expired_is_abandoned_not_left_resting():
    # A GTD limit whose expiry has passed must leave the book BEFORE its
    # trigger check: an expired order that later fills would resurrect a
    # cancelled order. Boundary: the order is still live AT the expiry
    # instant and dead strictly after it.
    rows = [
        (_t(0), "1.10000", "1.10005"),
        (_t(1), "1.09995", "1.10000"),  # == expiry: still live, not triggered
        (_t(2), "1.09940", "1.09945"),  # > expiry: dead, must NOT fill
    ]
    intent = OrderIntent(
        side=Side.BUY, order_type=OrderType.LIMIT, limit_or_stop_price=Decimal("1.09950"),
        stop_loss=Decimal("1.09850"), take_profit=None, volume=Decimal("0.10"),
        time_in_force=TIF.GTD, expiry=_t(1),
    )
    result = _run(rows, ScriptedStrategy({0: intent}))
    assert result.trades == []
    assert any("expired" in r for r in result.rejected_orders)


def test_stop_exits_use_stop_exit_spread_sampler_end_to_end():
    # COST-012: a stop is a market order in a fast market; its exit price
    # takes adverse slippage from the SEPARATE stop-exit model, and the
    # entry slippage model must never be consulted for it.
    calls = {"entry": [], "stop_exit": []}

    def entry_sampler(intent, at):
        calls["entry"].append((intent.order_type.value, at))
        return Decimal(0)

    def stop_exit_sampler(intent, at):
        calls["stop_exit"].append((intent.order_type.value, at))
        return Decimal("2")  # 2 pips adverse

    bt = TickBacktester(
        spec=_spec(), fees=_fees(), latency=dt.timedelta(0),
        value_per_point_per_lot=Decimal("10.0"),
        slippage_pips_sampler=entry_sampler,
        stop_exit_spread_sampler=stop_exit_sampler,
    )
    rows = [
        (_t(0), "1.09995", "1.10000"),
        (_t(1), "1.09900", "1.09905"),  # bid touches the stop -> stopped out
    ]
    intent = OrderIntent(
        side=Side.BUY, order_type=OrderType.MARKET, limit_or_stop_price=None,
        stop_loss=Decimal("1.09900"), take_profit=None, volume=Decimal("0.10"),
    )
    result = bt.run(symbol="EURUSD", ticks=_ticks(rows),
                    features=_empty_features(), strategy=ScriptedStrategy({0: intent}))
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason == "closed_sl"
    # Exit at the stop minus 2 pips of adverse stop-exit spread (long closes
    # at bid): 1.09900 - 0.00020 = 1.09880.
    assert trade.exit_price == Decimal("1.09880")
    # Hand-worked: (1.09880 - 1.10000) * 0.10 * 100000 = -12.00; net -12.70.
    assert trade.pnl == Decimal("-12.70")
    assert trade.r_multiple == Decimal("-1.27")
    # The stop-exit model was consulted exactly once, on the MARKET intent
    # synthesised from the position; the entry model only at submission.
    assert [c[0] for c in calls["stop_exit"]] == ["market"]
    assert len(calls["entry"]) == 1 and calls["entry"][0][0] == "market"


def test_weekend_gap_exit_counted_separately():
    # BT-030: an exit resolved at the first tick after the market's weekend
    # gap is counted in weekend_gap_exits — the gap carried the price away
    # from the stop and that tail must be visible.
    thu = dt.datetime(2024, 1, 4, 10, 0, tzinfo=UTC)   # Thursday
    mon = dt.datetime(2024, 1, 8, 9, 0, tzinfo=UTC)    # Monday after the gap
    intent = OrderIntent(
        side=Side.BUY, order_type=OrderType.MARKET, limit_or_stop_price=None,
        stop_loss=Decimal("1.09900"), take_profit=None, volume=Decimal("0.10"),
    )
    across = [
        (thu, "1.09995", "1.10000"),
        (mon, "1.09850", "1.09855"),  # gapped through the stop over the weekend
    ]
    result = _run(across, ScriptedStrategy({0: intent}))
    assert len(result.trades) == 1 and result.trades[0].exit_reason == "closed_sl"
    assert result.weekend_gap_exits == 1

    # Control: identical quotes within one session are NOT weekend gaps.
    intraday = [
        (thu, "1.09995", "1.10000"),
        (thu + dt.timedelta(minutes=1), "1.09850", "1.09855"),
    ]
    result2 = _run(intraday, ScriptedStrategy({0: intent}))
    assert len(result2.trades) == 1 and result2.trades[0].exit_reason == "closed_sl"
    assert result2.weekend_gap_exits == 0


def test_bt040_golden_path_regression(tmp_path):
    """One fixed dataset, one fixed strategy, a committed expected result."""
    rows = [
        (_t(0), "1.09995", "1.10000"),
        (_t(1), "1.09900", "1.09905"),
        (_t(2), "1.10000", "1.10005"),
    ]
    intent = OrderIntent(
        side=Side.BUY, order_type=OrderType.MARKET, limit_or_stop_price=None,
        stop_loss=Decimal("1.09900"), take_profit=Decimal("1.10200"),
        volume=Decimal("0.10"),
    )
    result = _run(rows, ScriptedStrategy({0: intent}))
    observed = [
        {
            "side": t.side,
            "volume": str(t.volume),
            "entry_price": str(t.entry_price),
            "exit_price": str(t.exit_price),
            "exit_reason": t.exit_reason,
            "pnl": str(t.pnl),
            "r_multiple": str(t.r_multiple),
            "resolution_method": t.resolution_method,
        }
        for t in result.trades
    ]
    golden = REPO / "tests" / "golden_path.json"
    if not golden.exists():
        golden.write_text(json.dumps(observed, indent=2), encoding="utf-8")
        pytest.fail("golden path written on first run; commit it and re-run (BT-040)")
    assert json.loads(golden.read_text()) == observed
