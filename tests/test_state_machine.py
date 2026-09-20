"""ARCH-003: one state machine for live and backtest."""

import pytest

from forex_research.core.state_machine import (
    InvalidTransition,
    OrderStatus,
    can_transition,
    transition,
)


def test_happy_path():
    transition(OrderStatus.QUEUED, OrderStatus.SUBMITTED)
    transition(OrderStatus.SUBMITTED, OrderStatus.FILLED)
    transition(OrderStatus.FILLED, OrderStatus.OPEN)
    transition(OrderStatus.OPEN, OrderStatus.CLOSED_SL)


def test_rejected_is_terminal_no_retry_within_the_bar():
    transition(OrderStatus.SUBMITTED, OrderStatus.REJECTED)
    assert not can_transition(OrderStatus.REJECTED, OrderStatus.SUBMITTED)


def test_unknown_never_resubmits():
    transition(OrderStatus.SUBMITTED, OrderStatus.UNKNOWN)
    assert not can_transition(OrderStatus.UNKNOWN, OrderStatus.SUBMITTED)


def test_partial_fill_opens_at_filled_volume():
    transition(OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED)
    transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.OPEN)


def test_illegal_transition_raises():
    with pytest.raises(InvalidTransition):
        transition(OrderStatus.QUEUED, OrderStatus.OPEN)
