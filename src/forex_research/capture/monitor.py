"""Completeness monitor — bounded state, per COST-013.

Tracks per symbol: the longest interval without a quote (suspected feed gap),
confirmed capture downtime (sum of intervals where the service itself was not
running between two of its records), and the newest quote age for staleness
alerts. No unbounded per-tick accumulation: the monitor holds one window per
symbol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class SymbolMonitorState:
    last_quote_ts: datetime | None = None
    max_quote_gap: timedelta = timedelta(0)
    downtime: timedelta = timedelta(0)
    ticks_seen: int = 0
    sequence_gaps: int = 0
    _open_downtime_start: datetime | None = None


@dataclass
class CaptureMonitor:
    """One bounded state object per symbol; nothing per-tick is retained."""

    thresholds_ms: dict[str, int]
    states: dict[str, SymbolMonitorState] = field(default_factory=dict)

    def state(self, symbol: str) -> SymbolMonitorState:
        return self.states.setdefault(symbol, SymbolMonitorState())

    def record_tick(self, symbol: str, ts: datetime) -> None:
        state = self.state(symbol)
        if state.last_quote_ts is not None:
            delta = ts - state.last_quote_ts
            if delta > state.max_quote_gap:
                state.max_quote_gap = delta
        state.last_quote_ts = ts
        state.ticks_seen += 1
        # Close any open downtime window: we were not running until now.
        # Clamp at zero: the window opens on the wall clock but closes on the
        # feed's timestamp, and a stale or lagging feed stamp must never book
        # negative downtime (which would erase earlier outages from the
        # completeness picture, DATA-011).
        if state._open_downtime_start is not None:
            delta = ts - state._open_downtime_start
            state.downtime += delta if delta > timedelta(0) else timedelta(0)
            state._open_downtime_start = None

    def record_sequence_gap(self, symbol: str) -> None:
        self.state(symbol).sequence_gaps += 1

    def open_downtime(self, symbol: str, since: datetime) -> None:
        """Mark capture as down since ``since`` (service start after a restart)."""
        self.state(symbol)._open_downtime_start = since

    def stale_for(self, symbol: str, now: datetime, threshold_ms: int) -> bool:
        state = self.state(symbol)
        if state.last_quote_ts is None:
            return True
        return (now - state.last_quote_ts).total_seconds() * 1000.0 > threshold_ms

    def summary(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for symbol, state in self.states.items():
            out[symbol] = {
                "ticks_seen": state.ticks_seen,
                "sequence_gaps": state.sequence_gaps,
                "max_quote_gap_ms": int(state.max_quote_gap.total_seconds() * 1000),
                "downtime_ms": int(state.downtime.total_seconds() * 1000),
                "last_quote_ts": state.last_quote_ts.isoformat() if state.last_quote_ts else None,
            }
        return out
