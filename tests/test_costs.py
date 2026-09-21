"""Cost model — COST-015 one fill equation, COST-016 fees, COST-017 sensitivity."""

import datetime as dt
from decimal import Decimal
from random import Random

from forex_research.costs import (
    FeeSchedule,
    FillEngine,
    SpreadCellKey,
    SpreadModel,
    run_sensitivity,
)
from forex_research.strategy.base import OrderIntent, OrderType, Side

UTC = dt.UTC


def _quote_series(quotes: dict[dt.datetime, tuple[Decimal, Decimal]]):
    def quote_at(t: dt.datetime):
        return quotes.get(t)

    return quote_at


def test_zero_slippage_returns_exactly_the_quoted_ask():
    # COST-015: a zero-slippage fixture must return exactly the quoted ask.
    t0 = dt.datetime(2024, 1, 2, 10, 0, tzinfo=UTC)
    quotes = {t0: (Decimal("1.09995"), Decimal("1.10000"))}
    engine = FillEngine(quote_at=_quote_series(quotes), latency=dt.timedelta(0))
    intent = OrderIntent(
        side=Side.BUY,
        order_type=OrderType.MARKET,
        limit_or_stop_price=None,
        stop_loss=Decimal("1.09900"),
        take_profit=None,
        volume=Decimal("0.10"),
    )
    report = engine.fill_market(intent, decision_time=t0)
    assert report is not None
    assert report.fill_price == Decimal("1.10000")  # the ask, no half-spread again
    assert report.residual == 0
    assert report.latency_move == 0


def test_latency_move_reported_and_residual_sampled_separately():
    # One tick at decision, a moved quote at arrival: the latency move is
    # reported, never sampled; residual stays the only sampled term.
    t0 = dt.datetime(2024, 1, 2, 10, 0, tzinfo=UTC)
    t_arrival = t0 + dt.timedelta(milliseconds=50)
    quotes = {
        t0: (Decimal("1.09995"), Decimal("1.10000")),
        t_arrival: (Decimal("1.10005"), Decimal("1.10010")),
    }
    engine = FillEngine(
        quote_at=_quote_series(quotes),
        latency=dt.timedelta(milliseconds=50),
        slippage_pips_sampler=lambda intent, at: Decimal("0.2"),
    )
    engine.set_instrument(pip_size=Decimal("0.0001"))
    intent = OrderIntent(
        side=Side.BUY,
        order_type=OrderType.MARKET,
        limit_or_stop_price=None,
        stop_loss=Decimal("1.09900"),
        take_profit=None,
        volume=Decimal("0.10"),
    )
    report = engine.fill_market(intent, decision_time=t0)
    assert report.arrival_quote == Decimal("1.10010")
    assert report.latency_move == Decimal("0.00010")  # 1 pip, reported
    assert report.residual == Decimal("0.00002")  # 0.2 pip, the only sample
    assert report.fill_price == Decimal("1.10012")  # arrival + residual, once


def test_sell_fills_at_bid_minus_adverse():
    t0 = dt.datetime(2024, 1, 2, 10, 0, tzinfo=UTC)
    quotes = {t0: (Decimal("1.09995"), Decimal("1.10000"))}
    engine = FillEngine(
        quote_at=_quote_series(quotes),
        latency=dt.timedelta(0),
        slippage_pips_sampler=lambda intent, at: Decimal("0.5"),
    )
    engine.set_instrument(pip_size=Decimal("0.0001"))
    intent = OrderIntent(
        side=Side.SELL,
        order_type=OrderType.MARKET,
        limit_or_stop_price=None,
        stop_loss=Decimal("1.10100"),
        take_profit=None,
        volume=Decimal("0.10"),
    )
    report = engine.fill_market(intent, decision_time=t0)
    assert report.fill_price == Decimal("1.09990")  # bid 1.09995 minus 0.5 pip


def test_limit_fills_at_level_or_not_at_all():
    t0 = dt.datetime(2024, 1, 2, 10, 0, tzinfo=UTC)
    t1 = t0 + dt.timedelta(seconds=1)
    quotes = {
        t0: (Decimal("1.10000"), Decimal("1.10005")),
        t1: (Decimal("1.09940"), Decimal("1.09945")),
    }
    engine = FillEngine(quote_at=_quote_series(quotes), latency=dt.timedelta(0))
    intent = OrderIntent(
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        limit_or_stop_price=Decimal("1.09950"),
        stop_loss=Decimal("1.09850"),
        take_profit=None,
        volume=Decimal("0.10"),
    )
    # t0: ask 1.10005 above the level -> no fill, no favourable slippage.
    assert engine.fill_limit(intent, decision_time=t0) is None
    # At t1 the ask 1.09945 is at or below the level -> fills AT the level,
    # never at the better quote (no favourable slippage, COST-015).
    report = engine.fill_limit(intent, decision_time=t1)
    assert report is not None
    assert report.fill_price == Decimal("1.09950")


def test_commission_both_legs_and_triple_swap_wednesday():
    fees = FeeSchedule(
        symbol="EURUSD",
        commission_per_lot_round_trip=Decimal("7.00"),
        swap_long_per_lot_per_day=Decimal("-0.50"),
        swap_short_per_lot_per_day=Decimal("0.20"),
        triple_swap_weekdays=(2,),
    )
    assert fees.commission(Decimal("0.10")) == Decimal("0.70")
    wednesday = dt.date(2024, 1, 3)  # a Wednesday
    monday = dt.date(2024, 1, 1)
    assert fees.swap_for_night(side="buy", volume=Decimal("1"), night=monday) == Decimal("-0.50")
    assert fees.swap_for_night(side="buy", volume=Decimal("1"), night=wednesday) == Decimal("-1.50")


def test_sensitivity_surface_rejects_cost_artefact():
    # 1.25x removing more than half the expectancy is a cost artefact.
    def artefact(m):
        table = {
            "0.75": Decimal("10"),
            "1.0": Decimal("8"),
            "1.25": Decimal("2"),
            "1.5": Decimal("0"),
            "1.75": Decimal("-2"),
            "2.0": Decimal("-4"),
        }
        return table[str(m)]

    surface = run_sensitivity(artefact)
    assert surface.is_cost_artefact()

    def robust(m):
        return Decimal("8") - (Decimal("1") * (m - Decimal("1")))

    surface = run_sensitivity(robust)
    assert not surface.is_cost_artefact()


def test_spread_model_fallback_and_episode_counts():
    model = SpreadModel(min_episodes=8, global_bound_pips=5.0)
    key = SpreadCellKey("EURUSD", "london", "normal", False, 1)
    for i in range(10):
        model.add_episode(key, entry_spread_pips=0.2 + i * 0.01)
    draw, provenance = model.draw(key, kind="entry", rng=Random(7))
    assert provenance["level"].startswith("symbol")  # exact cell matched
    assert provenance["episodes"] == 10
    assert draw in model.entry_cells[key].samples_pips  # drawn, not averaged

    # A sparse cell with no matches anywhere falls to the conservative bound.
    empty_key = SpreadCellKey("AUDUSD", "asia", "high", True, 4)
    draw, provenance = model.draw(empty_key, kind="entry", rng=Random(1))
    assert provenance["level"] == "global_bound"
    assert draw == Decimal("5.0")


def test_spread_fallback_does_not_pool_news_cells_into_news_free_keys():
    # COST-014: a query with news_proximity=False at the exact level must
    # not silently pool news=True episodes into the same cell — the match on
    # a field the level includes is exact, not wildcarded.
    model = SpreadModel(min_episodes=8, global_bound_pips=5.0)
    calm = SpreadCellKey("EURUSD", "london", "normal", False, 1)
    news = SpreadCellKey("EURUSD", "london", "normal", True, 1)
    for _ in range(8):
        model.add_episode(calm, entry_spread_pips=0.2)
    for _ in range(8):
        model.add_episode(news, entry_spread_pips=3.0)

    draw, provenance = model.draw(calm, kind="entry", rng=Random(1))
    assert provenance["level"].startswith("symbol")
    assert draw == Decimal("0.2")  # calm draws stay calm; no 3.0-pip episodes
    assert provenance["episodes"] == 8  # pooled-with-news would report 16

    # Dropping news from the key's level (news=True here) still matches the
    # exact cell first — and a news-free-only universe would fall through to
    # the global bound rather than pool, which is covered by empty_key above.
    draw, provenance = model.draw(news, kind="entry", rng=Random(1))
    assert draw == Decimal("3.0")
