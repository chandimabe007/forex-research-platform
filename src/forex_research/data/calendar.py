"""Economic calendar — DATA-014.

Required by the news filter hypothesis and by funded-stage compliance
(CHAL-013). A generic vendor "high impact" label is NOT equivalent to the
firm's own restricted-event list; compliance uses the firm's.

Per event: event_id, scheduled_time, revision_history, rescheduled_from,
affected_currencies, affected_instruments, restriction_status, source,
available_at. The ``actual`` value has its own ``available_at`` at release
and is never readable before it (VAL-060).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import polars as pl


@dataclass(frozen=True)
class CalendarEvent:
    event_id: str
    scheduled_time: datetime
    affected_currencies: tuple[str, ...]
    affected_instruments: tuple[str, ...]
    restriction_status: str  # the FIRM's list, not a vendor label
    source: str
    available_at: datetime
    revision_history: tuple[dict, ...] = field(default_factory=tuple)
    rescheduled_from: datetime | None = None


EVENT_SCHEMA = {
    "event_id": pl.String,
    "scheduled_time": pl.Datetime("us", time_zone="UTC"),
    "affected_currencies": pl.String,  # comma-joined for parquet friendliness
    "affected_instruments": pl.String,
    "restriction_status": pl.String,
    "source": pl.String,
    "available_at": pl.Datetime("us", time_zone="UTC"),
}


def events_frame(events: list[CalendarEvent]) -> pl.DataFrame:
    rows = [
        {
            "event_id": e.event_id,
            "scheduled_time": e.scheduled_time,
            "affected_currencies": ",".join(e.affected_currencies),
            "affected_instruments": ",".join(e.affected_instruments),
            "restriction_status": e.restriction_status,
            "source": e.source,
            "available_at": e.available_at,
        }
        for e in events
    ]
    return pl.DataFrame(rows, schema=EVENT_SCHEMA)


def is_news_restricted(
    events: pl.DataFrame,
    *,
    instrument: str,
    at: datetime,
    window_before,
    window_after,
) -> bool:
    """True when ``at`` falls inside a restricted event window for the instrument.

    Only events whose ``available_at`` <= ``at`` are considered — the actual
    value and even the restriction itself are never readable before release
    (VAL-060).
    """
    relevant = events.filter(
        (pl.col("available_at") <= at) & (pl.col("affected_instruments").str.contains(instrument))
    )
    if relevant.is_empty():
        return False
    for row in relevant.iter_rows(named=True):
        scheduled = row["scheduled_time"]
        if scheduled - window_before <= at <= scheduled + window_after:
            return True
    return False
