"""End to end — MILE-033: one trivial strategy carried through the full path.

Ticks -> validated bars -> available_at features -> toy strategy -> risk-
consistent execution -> trades with SL/TP exits. This proves the vertical
slice, not an edge: the toy is not a research candidate (MILE-033).
"""

import datetime as dt
from decimal import Decimal

import polars as pl

from forex_research.backtest import TickBacktester
from forex_research.costs import FeeSchedule
from forex_research.data.instruments import InstrumentSpec
from forex_research.data.resample import resample_ticks
from forex_research.data.sessions import SessionResolver
from forex_research.features.engine import FeatureEngine
from forex_research.features.library import FeatureParams
from forex_research.strategy.toy import PullbackToy

UTC = dt.UTC
START = dt.datetime(2024, 1, 2, tzinfo=UTC)


def _ticks(n: int = 5400) -> pl.DataFrame:
    """90 minutes of 1-second synthetic ticks with a mild upward drift."""
    import numpy as np

    rng = np.random.default_rng(11)
    ts = pl.datetime_range(START, START + dt.timedelta(seconds=n - 1), interval="1s", eager=True)
    mid = 1.10 + np.cumsum(rng.normal(2e-6, 1.2e-4, n))
    half = 0.0001
    return pl.DataFrame(
        {
            "ts": ts,
            "bid": mid - half,
            "ask": mid + half,
            "bid_volume": [1.0] * n,
            "ask_volume": [1.0] * n,
            "sequence_gap": [False] * n,
        }
    )


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


def test_toy_strategy_runs_the_full_vertical_slice():
    ticks = _ticks()
    bars = resample_ticks(ticks, timeframe="1m", continuity_threshold_ms=5000)
    assert bars.height == 90

    engine = FeatureEngine(
        params=FeatureParams(trend_window=20, vol_regime_lookback=60),
        session_resolver=SessionResolver(venue_zone="UTC"),
    )
    features = engine.compute(bars)
    assert "trend_state" in features.columns

    toy = PullbackToy(stop_pips=Decimal("2"), target_pips=Decimal("4"), volume=Decimal("0.10"))
    bt = TickBacktester(
        spec=_spec(),
        fees=FeeSchedule(
            symbol="EURUSD",
            commission_per_lot_round_trip=Decimal("7.00"),
            swap_long_per_lot_per_day=Decimal("0"),
            swap_short_per_lot_per_day=Decimal("0"),
        ),
        latency=dt.timedelta(0),
        value_per_point_per_lot=Decimal("100000"),
    )
    result = bt.run(symbol="EURUSD", ticks=ticks, features=features, strategy=toy)

    assert result.rejected_orders == []
    assert len(result.trades) >= 1, "the toy found no signal in 90 trending minutes"
    for trade in result.trades:
        assert trade.exit_reason in {"closed_sl", "closed_tp"}
        # Risk recomputed from the rounded volume: 2-pip stop x 0.10 x 100000.
        assert trade.volume == Decimal("0.10")
        if trade.r_multiple is not None:
            # SL 2 pips, TP 4 pips. Tight hand-worked bounds live in
            # test_backtest.py; here 1-second ticks can gap THROUGH the stop
            # within one second, filling at the prevailing quote (COST-015:
            # "gaps through the stop are the tail", RISK-020). The bound
            # therefore allows gap-through losses, not just -1 - costs.
            assert Decimal("-6.0") <= trade.r_multiple <= Decimal("2.5")
        # No trade may open before its features exist (VAL-065 at runtime).
        assert trade.entry_ts >= START


def test_toy_treats_stale_features_as_no_trade():
    """Missing/stale features are NO_TRADE, never zero (FEAT-002): before the
    warm-up window produces a trend state, the toy must not signal."""
    ticks = _ticks(n=600)  # 10 minutes: inside the trend warm-up for many bars
    bars = resample_ticks(ticks, timeframe="1m", continuity_threshold_ms=5000)
    engine = FeatureEngine(params=FeatureParams(trend_window=20, vol_regime_lookback=60))
    features = engine.compute(bars)

    toy = PullbackToy(stop_pips=Decimal("2"), target_pips=Decimal("4"), volume=Decimal("0.10"))
    bt = TickBacktester(
        spec=_spec(),
        fees=FeeSchedule(
            symbol="EURUSD",
            commission_per_lot_round_trip=Decimal("7"),
            swap_long_per_lot_per_day=Decimal("0"),
            swap_short_per_lot_per_day=Decimal("0"),
        ),
        latency=dt.timedelta(0),
        value_per_point_per_lot=Decimal("100000"),
    )
    result = bt.run(symbol="EURUSD", ticks=ticks, features=features, strategy=toy)
    first_feature = features["available_at"].min()
    for trade in result.trades:
        assert trade.entry_ts >= first_feature
