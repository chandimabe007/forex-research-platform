"""Run the full vertical slice offline on synthetic data — no MT5 needed.

    .venv/Scripts/python scripts/demo_backtest.py

Ticks -> validated bars -> available_at features -> toy strategy -> risk-
consistent fills -> trade list with PnL and R multiples. This proves the
pipeline runs; it is NOT evidence of an edge (MILE-033: the toy is not a
research candidate).
"""

from __future__ import annotations

import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
import polars as pl

from forex_research.backtest import TickBacktester
from forex_research.config import load_fee_schedule_for_server
from forex_research.data.instruments import InstrumentSpec
from forex_research.data.resample import resample_ticks
from forex_research.features.engine import FeatureEngine
from forex_research.features.library import FeatureParams
from forex_research.strategy.toy import PullbackToy

UTC = dt.UTC
START = dt.datetime(2024, 1, 2, tzinfo=UTC)


def synthetic_ticks(n: int = 5400) -> pl.DataFrame:
    """90 minutes of 1-second ticks with a mild upward drift."""
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


def main() -> int:
    ticks = synthetic_ticks()
    print(f"ticks: {ticks.height} (1-second synthetic EURUSD)")

    bars = resample_ticks(ticks, timeframe="1m", continuity_threshold_ms=5000)
    print(f"bars:  {bars.height} x 1m (M1 from ticks)")

    engine = FeatureEngine(
        params=FeatureParams(trend_window=20, vol_regime_lookback=60),
    )
    features = engine.compute(bars)
    print(
        f"features: {features.height} rows; columns: {[c for c in features.columns if c != 'available_at']}"
    )

    toy = PullbackToy(stop_pips=Decimal("2"), target_pips=Decimal("4"), volume=Decimal("0.10"))
    # Fees come from config with canary evidence attached (COST-016); the
    # demo holds nothing overnight, so unobserved swaps are acknowledged.
    fees = load_fee_schedule_for_server(
        Path(__file__).resolve().parents[1] / "config" / "fees.yaml",
        "LHFXSA-Trade",
        allow_unobserved_swaps=True,
    )
    print(
        f"fees: {fees.symbol} commission {fees.commission_per_lot_round_trip}/lot "
        f"(observed {fees.retrieved_at})"
    )
    bt = TickBacktester(
        spec=InstrumentSpec(
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
        ),
        fees=fees,
        latency=dt.timedelta(0),
        value_per_point_per_lot=Decimal("100000"),
    )
    result = bt.run(symbol="EURUSD", ticks=ticks, features=features, strategy=toy)

    print(f"\ntrades: {len(result.trades)}   rejected orders: {len(result.rejected_orders)}")
    wins = [t for t in result.trades if t.pnl > 0]
    total_r = sum((t.r_multiple or Decimal(0)) for t in result.trades)
    for t in result.trades:
        print(
            f"  {t.side:4s} {t.volume} @ {t.entry_price} -> {t.exit_price} "
            f"({t.exit_reason}, R={t.r_multiple})"
        )
    if result.trades:
        print(f"win rate: {len(wins)}/{len(result.trades)}   total R: {total_r:.2f}")
    print("\nThis is the plumbing demo, not an edge (MILE-033).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
