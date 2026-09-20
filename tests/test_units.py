"""ARCH-001: typed units, conservative rounding at broker boundaries."""

from decimal import Decimal

import pytest

from forex_research.core.units import (
    D,
    UnitError,
    pips_to_price,
    price_to_pips,
    round_cost_up,
    round_half_even,
    round_stop_distance_up,
    round_volume_down,
)


def test_float_rejected_at_boundaries():
    with pytest.raises(UnitError):
        D(0.1)  # floats never enter a breach comparison (ARCH-001)


def test_pip_conversion_through_instrument_sizes():
    assert pips_to_price(Decimal(10), pip_size=Decimal("0.0001")) == Decimal("0.0010")
    assert pips_to_price(Decimal(10), pip_size=Decimal("0.01")) == Decimal("0.10")
    assert price_to_pips(Decimal("0.0050"), pip_size=Decimal("0.0001")) == Decimal(50)


def test_volume_rounds_down():
    assert round_volume_down(Decimal("0.237"), step=Decimal("0.01")) == Decimal("0.23")
    assert round_volume_down(Decimal("0.20"), step=Decimal("0.01")) == Decimal("0.20")


def test_cost_rounds_up_and_stop_distance_up():
    assert round_cost_up(Decimal("1.201"), quantum=Decimal("0.01")) == Decimal("1.21")
    assert round_stop_distance_up(Decimal("0.00042"), point_size=Decimal("0.00001")) == Decimal("0.00042")
    assert round_stop_distance_up(Decimal("0.000421"), point_size=Decimal("0.00001")) == Decimal("0.00043")


def test_round_half_even_is_presentation_only():
    assert round_half_even(Decimal("1.005"), places=2) == Decimal("1.00")
