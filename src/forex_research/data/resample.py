"""Resampling — DATA-003, DATA-013.

Bars labelled by open time, closed left: the bar whose ``ts_open`` is 10:00
covers [10:00, 11:00) and is knowable at 11:00. Aggregate both quote sides
separately. ``available_at = ts_open + duration`` is written here so no
downstream consumer has to remember the rule (DATA-013).

A bar is **incomplete** when ``coverage_ok`` is false or ``known_outage`` is
true: it generates no signals and is excluded from feature computation
(DATA-013). Tick count does not decide this — a quiet complete bar can have
few ticks, and a busy bar with a feed gap can have many.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import polars as pl

BAR_COLUMNS = [
    "ts_open",
    "available_at",
    "bid_open",
    "bid_high",
    "bid_low",
    "bid_close",
    "ask_open",
    "ask_high",
    "ask_low",
    "ask_close",
    "volume",
    "tick_count",
    "max_quote_gap_ms",
    "coverage_ok",
    "known_outage",
    "tick_derived",
]


def resample_ticks(
    ticks: pl.DataFrame,
    *,
    timeframe: str,
    continuity_threshold_ms: int,
    outages: list[tuple[str, str]] | None = None,
) -> pl.DataFrame:
    """Resample DATA-004 ticks into DATA-003 bars.

    ``timeframe`` is a Polars interval like ``"1m"``/``"5m"``/``"1h"``.
    ``outages`` is a list of (start, end) ISO strings from the outage
    register (vendor, broker or capture outages) — bars overlapping any
    entry carry ``known_outage=True``.
    """
    if ticks.is_empty():
        return pl.DataFrame(schema=BAR_COLUMNS)
    outages = outages or []

    ticks = ticks.sort("ts")
    duration = _parse_timeframe(timeframe)

    # Global inter-tick gap BEFORE grouping, so a hole that begins in one bar
    # and ends in the next is visible: the bar after a gap inherits the
    # cross-group interval in its max_quote_gap_ms (DATA-003).
    ticks = ticks.with_columns(pl.col("ts").diff().alias("_gap"))

    grouped = ticks.group_by_dynamic(
        "ts",
        every=timeframe,
        closed="left",
        label="left",
    ).agg(
        [
            pl.col("bid").first().alias("bid_open"),
            pl.col("bid").max().alias("bid_high"),
            pl.col("bid").min().alias("bid_low"),
            pl.col("bid").last().alias("bid_close"),
            pl.col("ask").first().alias("ask_open"),
            pl.col("ask").max().alias("ask_high"),
            pl.col("ask").min().alias("ask_low"),
            pl.col("ask").last().alias("ask_close"),
            pl.col("bid_volume").sum().alias("volume"),
            pl.len().alias("tick_count"),
            pl.col("_gap").max().alias("_max_gap"),
        ]
    )

    gap_ms = pl.col("_max_gap").dt.total_microseconds().fill_null(0) // 1000
    bars = grouped.with_columns(
        [
            pl.col("ts").alias("ts_open"),
            (pl.col("ts") + duration).alias("available_at"),
            gap_ms.alias("max_quote_gap_ms"),
            (gap_ms <= continuity_threshold_ms).alias("coverage_ok"),
            pl.lit(False).alias("known_outage"),
            pl.lit(True).alias("tick_derived"),
        ]
    ).drop("_max_gap")

    if outages:
        outage_mask = pl.lit(False)
        for start, end in outages:
            start_ts = _to_datetime(start)
            end_ts = _to_datetime(end)
            outage_mask = outage_mask | (
                (pl.col("ts_open") < end_ts) & (pl.col("available_at") > start_ts)
            )
        bars = bars.with_columns(outage_mask.alias("known_outage"))

    return bars.select(BAR_COLUMNS)


def _parse_timeframe(timeframe: str) -> timedelta:
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
    unit = timeframe[-1]
    if unit not in units:
        raise ValueError(f"unsupported timeframe '{timeframe}'")
    amount = int(timeframe[:-1])
    if amount <= 0:
        raise ValueError(f"timeframe amount must be positive: '{timeframe}'")
    return timedelta(**{units[unit]: amount})


def _to_datetime(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
