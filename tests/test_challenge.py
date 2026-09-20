"""Challenge rules evaluated against hand-worked examples (MILE-033/055)."""

import datetime as dt
from decimal import Decimal

from forex_research.challenge.evaluator import AccountPoint, evaluate_phase
from forex_research.config.schemas import (
    AtomicRule,
    BreachSemantics,
    BreachSeverity,
    FloorBasis,
    Provenance,
    RuleStatus,
    TypedValue,
)

UTC = dt.UTC
PRAGUE = "Europe/Prague"
PROV = Provenance(source_url="https://example.com/terms",
                  retrieved_at=dt.datetime(2026, 9, 20, tzinfo=UTC))


def _rule(**kw):
    defaults = dict(
        status=RuleStatus.VERIFIED,
        provenance=PROV,
        breach_semantics=BreachSemantics.FALLS_BELOW,
        breach_severity=BreachSeverity.HARD,
        ratchet_on_equity=False,
    )
    defaults.update(kw)
    return AtomicRule(name="rule", **defaults)


def _rules(*, ratchet: bool):
    return {
        "maximum_daily_loss": _rule(
            value=TypedValue("percent_initial_capital", Decimal("5")),
            floor_basis=FloorBasis.BALANCE_AT_RESET_MINUS_AMOUNT,
            ratchet_on_equity=ratchet,
            reset_local_time="00:00:00",
        ),
        "maximum_loss": _rule(
            value=TypedValue("percent_initial_capital", Decimal("10")),
            floor_basis=FloorBasis.INITIAL_BALANCE_MINUS_AMOUNT,
        ),
        "profit_target": _rule(
            value=TypedValue("percent_initial_capital", Decimal("10")),
            breach_semantics=BreachSemantics.HITS,
            breach_severity=BreachSeverity.SOFT,
        ),
    }


def _pt(hour, minute, equity, balance=None, positions=0):
    return AccountPoint(
        ts=dt.datetime(2024, 1, 2, hour, minute, tzinfo=UTC),
        equity=Decimal(equity),
        balance=Decimal(balance if balance is not None else equity),
        positions_open=positions,
    )


CAPITAL = Decimal("100000")


def test_intrabar_breach_detected_even_though_close_recovers():
    # VAL-030b: equity falls to 94.5k intraday (below the 95k floor) and
    # recovers by the close — the breach stands, exactly once.
    path = [
        # 23:30 UTC Jan 1 = 00:30 local Jan 2: just after the reset.
        AccountPoint(ts=dt.datetime(2024, 1, 1, 23, 30, tzinfo=UTC),
                     equity=Decimal("100000"), balance=Decimal("100000"),
                     positions_open=0),
        _pt(9, 0, "96000"),
        _pt(11, 0, "94500"),  # breach moment
        _pt(12, 0, "96000"),  # recovered; must NOT un-breach
    ]
    breaches = evaluate_phase(
        phase_name="challenge", rules=_rules(ratchet=False),
        initial_capital=CAPITAL, path=path, venue_zone=PRAGUE,
    )
    daily = [b for b in breaches if b.rule == "maximum_daily_loss"]
    assert len(daily) == 1
    assert daily[0].ts == path[2].ts  # the 11:00 breach moment
    assert daily[0].floor_or_target == Decimal("95000")


def test_daily_reset_crossed_with_position_open():
    # Day 1 drifts down but does not breach; the reset recomputes the floor
    # from the new day's balance baseline while the position stays open.
    path = [
        _pt(9, 0, "98000", balance="100000", positions=1),
        _pt(23, 59, "97500", balance="100000", positions=1),  # still day 1
        _pt(0, 30, "98000", balance="97500", positions=1),    # day 2 after reset
    ]
    breaches = evaluate_phase(
        phase_name="challenge", rules=_rules(ratchet=False),
        initial_capital=CAPITAL, path=path, venue_zone=PRAGUE,
    )
    daily = [b for b in breaches if b.rule == "maximum_daily_loss"]
    assert daily == []  # day 1 floor 95k, day 2 baseline 97.5k -> floor 92.5k
    # And the maximum-loss floor (90k) never trips.
    assert not any(b.rule == "maximum_loss" for b in breaches)


def test_gate003_floating_profit_ratchet():
    # Carrying +3k floating through the reset: with the ratchet the next
    # day's floor rises to 103k - 5k = 98k; a retrace to 97.5k breaches.
    # Without the ratchet the floor stays 95k and 97.5k is safe.
    path = [
        _pt(9, 0, "100000", balance="100000", positions=1),
        _pt(23, 59, "103000", balance="100000", positions=1),  # floating +3k
        _pt(0, 30, "97500", balance="100000", positions=1),    # day 2 retrace
    ]
    with_ratchet = evaluate_phase(
        phase_name="challenge", rules=_rules(ratchet=True),
        initial_capital=CAPITAL, path=path, venue_zone=PRAGUE,
    )
    without_ratchet = evaluate_phase(
        phase_name="challenge", rules=_rules(ratchet=False),
        initial_capital=CAPITAL, path=path, venue_zone=PRAGUE,
    )
    assert any(b.rule == "maximum_daily_loss" for b in with_ratchet)
    assert not any(b.rule == "maximum_daily_loss" for b in without_ratchet)


def test_profit_target_hits_recorded_as_soft():
    path = [_pt(9, 0, "100000"), _pt(15, 0, "110000")]
    breaches = evaluate_phase(
        phase_name="challenge", rules=_rules(ratchet=False),
        initial_capital=CAPITAL, path=path, venue_zone=PRAGUE,
    )
    targets = [b for b in breaches if b.rule == "profit_target"]
    assert len(targets) == 1
    assert targets[0].severity is BreachSeverity.SOFT
    assert targets[0].floor_or_target == Decimal("110000")


def test_maximum_loss_hard_breach():
    path = [_pt(9, 0, "90500"), _pt(10, 0, "89500")]
    breaches = evaluate_phase(
        phase_name="challenge", rules=_rules(ratchet=False),
        initial_capital=CAPITAL, path=path, venue_zone=PRAGUE,
    )
    hard = [b for b in breaches if b.rule == "maximum_loss"]
    assert len(hard) == 1
    assert hard[0].severity is BreachSeverity.HARD
    assert hard[0].floor_or_target == Decimal("90000")
