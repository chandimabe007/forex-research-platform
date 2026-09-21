"""Look-ahead enforcement — VAL-060, VAL-061, VAL-065.

The invariant (VAL-065): no feature value may depend on any input whose
``available_at`` is later than the decision time of the row it contributes
to. The truncation test (VAL-060) establishes this by property, not by
design: for any cutoff ``T``, features computed on inputs truncated **by
``available_at``** at ``T`` must be identical to features computed on full
data and sliced at ``T`` — keyed rows, null masks compared.

Leaking fixtures (VAL-061) deliberately violate the invariant; the test MUST
fail on each. A fixture that stops failing means the test was weakened.

Warm-up note: rows inside the trailing warm-up of a truncated run are
excluded from comparison. A null there is a warm-up artefact of trailing
windows, not look-ahead; every leak below manifests in interior rows.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

import polars as pl

UTC = dt.UTC


def base_bars(n: int = 300, start: dt.datetime | None = None) -> pl.DataFrame:
    """Deterministic synthetic M1 bars in the DATA-003 schema."""
    import numpy as np

    start = start or dt.datetime(2024, 1, 1, tzinfo=UTC)
    ts = pl.datetime_range(start, start + dt.timedelta(minutes=n - 1), interval="1m", eager=True)
    rng = np.random.default_rng(7)
    mid = 1.10 + np.cumsum(rng.normal(0, 1e-4, n))
    half = 0.0001
    wiggle = np.abs(rng.normal(0, 5e-5, n))
    return pl.DataFrame(
        {
            "ts_open": ts,
            "available_at": ts + dt.timedelta(minutes=1),
            "bid_open": mid - half,
            "bid_high": mid - half + wiggle,
            "bid_low": mid - half - wiggle,
            "bid_close": mid - half,
            "ask_open": mid + half,
            "ask_high": mid + half + wiggle,
            "ask_low": mid + half - wiggle,
            "ask_close": mid + half,
            "volume": rng.integers(1, 100, n),
            "tick_count": rng.integers(1, 10, n),
            "max_quote_gap_ms": [0] * n,
            "coverage_ok": [True] * n,
            "known_outage": [False] * n,
            "tick_derived": [True] * n,
        }
    )


def _frames_agree(a: pl.DataFrame, b: pl.DataFrame) -> bool:
    """Keyed-row comparison with null masks (VAL-060)."""
    if a.height != b.height or a.columns != b.columns:
        return False
    for col in a.columns:
        if not bool((a[col].is_null() == b[col].is_null()).all()):
            return False
        a_vals, b_vals = a[col], b[col]
        if a_vals.dtype.is_numeric():
            both = pl.DataFrame({"x": a_vals, "y": b_vals}).drop_nulls()
            if both.is_empty():
                continue
            if not bool((both["x"] - both["y"]).abs().max() <= 1e-12):
                return False
        else:
            if not bool(a_vals.equals(b_vals)):
                return False
    return True


def run_truncation_test(
    pipeline: Callable[[dict[str, pl.DataFrame]], pl.DataFrame],
    inputs: dict[str, pl.DataFrame],
    cutoff: dt.datetime,
    *,
    warmup: int = 0,
    key: str = "available_at",
) -> bool:
    """VAL-060: truncate every input by ``available_at`` <= cutoff, run the
    pipeline, compare against the full run sliced at the same cutoff."""
    full = pipeline(inputs).filter(pl.col(key) <= cutoff)
    truncated_inputs = {name: df.filter(pl.col(key) <= cutoff) for name, df in inputs.items()}
    truncated = pipeline(truncated_inputs)
    if warmup and truncated.height >= warmup:
        truncated = truncated[warmup:]
    keys = set(truncated[key].to_list())
    full_aligned = full.filter(pl.col(key).is_in(list(keys)))
    return _frames_agree(full_aligned, truncated)


# ---------------------------------------------------------------------------
# Honest pipeline and leaking fixtures (VAL-061)
# ---------------------------------------------------------------------------


def honest_pipeline(inputs: dict[str, pl.DataFrame]) -> pl.DataFrame:
    from .library import FeatureParams, compute_features

    return compute_features(
        inputs["bars"], params=FeatureParams(trend_window=20, vol_regime_lookback=60)
    )


def _leak_centred_window(inputs: dict[str, pl.DataFrame]) -> pl.DataFrame:
    return (
        inputs["bars"]
        .with_columns(pl.col("bid_close").rolling_mean(window_size=5, center=True).alias("leak"))
        .select(["available_at", "leak"])
    )


def _leak_full_series_rank(inputs: dict[str, pl.DataFrame]) -> pl.DataFrame:
    return (
        inputs["bars"]
        .with_columns(pl.col("bid_close").rank().alias("leak"))
        .select(["available_at", "leak"])
    )


def _h1_inputs(bars: pl.DataFrame) -> dict[str, pl.DataFrame]:
    h1 = bars.group_by_dynamic("ts_open", every="1h", closed="left", label="left").agg(
        [pl.col("bid_close").last().alias("h1_close")]
    )
    h1 = h1.with_columns((pl.col("ts_open") + dt.timedelta(hours=1)).alias("available_at"))
    return {"bars": bars, "h1": h1}


def _leak_higher_tf_join_on_ts_open(inputs: dict[str, pl.DataFrame]) -> pl.DataFrame:
    # The leak: join the H1 close on bar-open time instead of available_at —
    # rows inside the hour receive a close that does not exist yet (FEAT-001).
    return (
        inputs["bars"]
        .join(inputs["h1"].select(["ts_open", "h1_close"]), on="ts_open", how="left")
        .select(["available_at", "h1_close"])
    )


def _honest_higher_tf_join(inputs: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """The FEAT-002-correct version: asof join on available_at with tolerance."""
    return (
        inputs["bars"]
        .select(["available_at"])
        .sort("available_at")
        .join_asof(
            inputs["h1"].select(["available_at", "h1_close"]).sort("available_at"),
            on="available_at",
            strategy="backward",
            tolerance="2h",
        )
        .select(["available_at", "h1_close"])
    )


def _calendar_inputs(bars: pl.DataFrame) -> dict[str, pl.DataFrame]:
    events = pl.DataFrame(
        {
            "event_id": ["NFP"],
            "scheduled_time": [dt.datetime(2024, 1, 1, 4, 30, tzinfo=UTC)],
            "actual": [1.1],  # readable only at release
            "available_at": [dt.datetime(2024, 1, 1, 6, 0, tzinfo=UTC)],
        },
        schema={
            "event_id": pl.String,
            "scheduled_time": pl.Datetime("us", time_zone="UTC"),
            "actual": pl.Float64,
            "available_at": pl.Datetime("us", time_zone="UTC"),
        },
    )
    return {"bars": bars, "events": events}


def _leak_calendar_actual_before_release(inputs: dict[str, pl.DataFrame]) -> pl.DataFrame:
    # The leak: the ``actual`` value joins as of scheduled_time, ignoring the
    # event's own available_at (VAL-060).
    return (
        inputs["bars"]
        .select(["available_at"])
        .sort("available_at")
        .join_asof(
            inputs["events"].select(["scheduled_time", "actual"]).sort("scheduled_time"),
            left_on="available_at",
            right_on="scheduled_time",
            strategy="backward",
        )
        .select(["available_at", "actual"])
    )


def _honest_calendar(inputs: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """Correct: the event row becomes usable only at its available_at, and it
    carries the scheduled value, not the actual, until release (FEAT-003)."""
    events = inputs["events"].rename({"available_at": "_evt_available"})
    return (
        inputs["bars"]
        .select(["available_at"])
        .sort("available_at")
        .join_asof(
            events.select(["_evt_available", "scheduled_time"]).sort("_evt_available"),
            left_on="available_at",
            right_on="_evt_available",
            strategy="backward",
            tolerance="1d",
        )
        .select(["available_at", "scheduled_time"])
    )


LEAKING_PIPELINES: list[tuple[str, Callable, Callable[[], dict[str, pl.DataFrame]], int]] = [
    # (name, leaking pipeline, inputs builder, cutoff hour-of-day that exposes it)
    ("centred_window", _leak_centred_window, lambda: {"bars": base_bars(n=900)}, 720),
    ("full_series_rank", _leak_full_series_rank, lambda: {"bars": base_bars(n=900)}, 720),
    (
        "higher_tf_join_on_ts_open",
        _leak_higher_tf_join_on_ts_open,
        lambda: _h1_inputs(base_bars(n=900)),
        750,  # mid-hour: the in-progress H1 group exposes the ts_open join
    ),
    (
        "calendar_actual_before_release",
        _leak_calendar_actual_before_release,
        lambda: _calendar_inputs(base_bars(n=900)),
        300,  # cutoff 05:00: between scheduled (04:30) and release (06:00)
    ),
]
