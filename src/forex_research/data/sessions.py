"""DST-aware session buckets — COST-011.

London and New York shift with their own local DST, on different dates. A
bucket defined as a fixed UTC range is wrong for several weeks a year, and
the error lands exactly where spread behaviour changes. Sessions are defined
in exchange local time and converted per date via IANA zones; both DST
transitions are tested in tests/test_sessions.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

SESSIONS = ("asia", "london", "new_york", "overlap", "rollover")

# Exchange-local session windows (start, end) in each exchange's own zone,
# so each follows its own DST transition dates.
_EXCHANGE_LOCAL: dict[str, tuple[time, time]] = {
    "Asia/Tokyo": (time(9, 0), time(15, 0)),
    "Europe/London": (time(8, 0), time(16, 30)),
    "America/New_York": (time(8, 0), time(17, 0)),
}

ZONE_TO_BUCKET = {
    "Asia/Tokyo": "asia",
    "Europe/London": "london",
    "America/New_York": "new_york",
}

ROLLOVER_LOCAL = (time(23, 55), time(0, 5))  # evaluated in the venue zone


@dataclass(frozen=True)
class SessionResolver:
    """Resolves timestamps to session buckets, DST-aware per date.

    ``venue_zone`` is the server zone recorded at GATE-005 (an IANA zone,
    never a fixed offset).
    """

    venue_zone: str = "UTC"

    def bucket(self, ts: datetime) -> str | None:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ZoneInfo("UTC"))
        venue = ZoneInfo(self.venue_zone)
        local_venue = ts.astimezone(venue).timetz().replace(tzinfo=None)
        start, end = ROLLOVER_LOCAL
        if local_venue >= start or local_venue < end:
            return "rollover"

        active: set[str] = set()
        for zone, window in _EXCHANGE_LOCAL.items():
            local = ts.astimezone(ZoneInfo(zone)).timetz().replace(tzinfo=None)
            if window[0] <= local < window[1]:
                active.add(ZONE_TO_BUCKET[zone])
        if "london" in active and "new_york" in active:
            return "overlap"
        for candidate in ("london", "new_york", "asia"):
            if candidate in active:
                return candidate
        return None


def exchange_zones() -> tuple[str, ...]:
    return tuple(_EXCHANGE_LOCAL)
