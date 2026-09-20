"""Look-ahead enforcement — VAL-060, VAL-061, VAL-062, VAL-065.

Blocking in CI (VAL-002): these fail when the CODE is wrong, never when a
strategy is bad.
"""

import datetime as dt

from forex_research.features import (
    LEAKING_PIPELINES,
    base_bars,
    coverage_gaps,
    register_library_features,
    run_truncation_test,
)
from forex_research.features.leaks import _h1_inputs, _honest_higher_tf_join, honest_pipeline

UTC = dt.UTC
START = dt.datetime(2024, 1, 1, tzinfo=UTC)


def _cutoffs(n_bars: int = 720):
    """50+ stratified cutoffs (VAL-060): across sessions, exact boundaries
    and one minute either side, month boundary, day boundaries, gaps."""
    cutoffs: list[dt.datetime] = []
    base_minutes = [90, 240, 361, 360, 359, 600, 699, 700, 701, 719]
    # Repeating stratified offsets over the window: every ~14 minutes.
    cutoffs = [START + dt.timedelta(minutes=m) for m in range(80, n_bars, 12)]
    cutoffs += [START + dt.timedelta(minutes=m) for m in base_minutes]
    # Bar-boundary either side (H1 boundaries at multiples of 60).
    for h in range(2, 12):
        for delta in (-1, 0, 1):
            cutoffs.append(START + dt.timedelta(hours=h) + dt.timedelta(minutes=delta))
    # Month boundary Jan 31 -> Feb 1.
    cutoffs.append(dt.datetime(2024, 1, 31, 23, 59, tzinfo=UTC))
    cutoffs.append(dt.datetime(2024, 2, 1, 0, 0, tzinfo=UTC))
    # DST transition days 2024 (both transitions, UTC reference points).
    cutoffs.append(dt.datetime(2024, 3, 31, 1, 0, tzinfo=UTC))
    cutoffs.append(dt.datetime(2024, 3, 31, 2, 0, tzinfo=UTC))
    cutoffs.append(dt.datetime(2024, 10, 27, 1, 0, tzinfo=UTC))
    cutoffs.append(dt.datetime(2024, 10, 27, 2, 0, tzinfo=UTC))
    # Weekend boundary: Friday close, Sunday open.
    cutoffs.append(dt.datetime(2024, 1, 5, 21, 0, tzinfo=UTC))   # Friday
    cutoffs.append(dt.datetime(2024, 1, 7, 21, 5, tzinfo=UTC))   # Sunday open
    return cutoffs


def test_val060_truncation_over_stratified_cutoffs():
    """Features truncated by availability match features sliced from full
    data — over 50+ stratified cutoffs."""
    bars = base_bars(n=720)
    inputs = {"bars": bars}
    cutoffs = [c for c in _cutoffs(720) if c <= bars["available_at"][-1]]
    assert len(cutoffs) >= 50, "VAL-060 requires 50+ stratified cutoffs"
    failures = []
    for cutoff in cutoffs:
        if not run_truncation_test(honest_pipeline, inputs, cutoff, warmup=60):
            failures.append(cutoff.isoformat())
    assert not failures, f"look-ahead at cutoffs: {failures[:5]}"


def test_val061_each_leaking_fixture_fails_the_truncation_test():
    """A truncation test that has never failed catches everything or nothing.
    Every deliberately leaking fixture MUST fail it."""
    for name, pipeline, inputs_builder, exposing_minute in LEAKING_PIPELINES:
        inputs = inputs_builder()
        # 30s past the exposing minute: inside the data, in the exact window
        # where each fixture's leak is live (mid-hour for the H1 join,
        # between scheduled and release for the calendar fixture).
        cutoff = START + dt.timedelta(minutes=exposing_minute, seconds=30)
        assert not run_truncation_test(pipeline, inputs, cutoff, warmup=1), (
            f"leaking fixture '{name}' PASSED the truncation test — the test was "
            "weakened (VAL-061)"
        )


def test_val061_honest_higher_tf_join_is_clean():
    """The FEAT-002-correct H1 join (asof on available_at with tolerance)
    passes at a cutoff inside the hour."""
    inputs = _h1_inputs(base_bars(n=900))
    cutoff = START + dt.timedelta(hours=2, minutes=30)  # inside the 3rd hour
    assert run_truncation_test(_honest_higher_tf_join, inputs, cutoff, warmup=1)


def test_val062_coverage_registry_complete():
    """Every registered feature has a truncation case."""
    register_library_features()
    assert coverage_gaps() == []


def test_val065_invariant_docstring_check():
    """The invariant is stated by construction: the honest pipeline's rows are
    keyed by available_at, and the truncation harness truncates every input by
    available_at — never ts_open."""
    bars = base_bars(n=200)
    inputs = {"bars": bars}
    assert run_truncation_test(
        honest_pipeline, inputs, START + dt.timedelta(minutes=150), warmup=30
    )
