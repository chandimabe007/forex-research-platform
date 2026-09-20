"""ARCH-002: canonical event priority, venue-configurable reset/swap position."""

import pytest

from forex_research.core.events import (
    EventKind,
    EventPositionError,
    VenueEventConfig,
    validate_venue_config,
)


def test_event_kinds_are_in_canonical_order():
    values = [int(k) for k in EventKind]
    assert values == sorted(values)
    assert int(EventKind.MARKET_STATUS_CHANGE) == 1
    assert int(EventKind.NEW_ORDER_SUBMISSION) == 9


def test_default_venue_config_places_reset_before_swap():
    config = VenueEventConfig()
    validate_venue_config(config)
    assert config.reset_priority < config.swap_priority


def test_swapped_venue_config_is_permitted():
    config = VenueEventConfig(reset_before_swap=False)
    validate_venue_config(config)
    assert config.swap_priority < config.reset_priority


def test_invalid_venue_config_colliding_with_fixed_events():
    class Bad:
        reset_priority = 4  # collides with QUOTE_ARRIVAL
        swap_priority = 5

    with pytest.raises(EventPositionError):
        validate_venue_config(Bad())
