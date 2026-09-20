"""Quote source — COST-013 requirement 1: capture every available quote, not
periodic snapshots. The source polls the platform's last tick per symbol at a
short interval, records sequence gaps (DATA-004 ``sequence_gap``), and
distinguishes suspected quote gaps from capture downtime (the service layer
records its own downtime separately).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class TickRecord:
    """DATA-004 tick schema (venue-quote variant)."""

    symbol: str
    ts: datetime  # UTC
    bid: float
    ask: float
    bid_volume: float | None
    ask_volume: float | None
    sequence_gap: bool  # true where the feed indicates missing ticks


class PollingQuoteSource:
    """Polls ``quote_fn(symbol)`` per symbol; emits TickRecords with gap flags."""

    def __init__(self, symbols: list[str], quote_fn) -> None:
        self._symbols = list(symbols)
        self._quote_fn = quote_fn
        self._last_ts: dict[str, datetime] = {}

    def poll(self) -> list[TickRecord]:
        out: list[TickRecord] = []
        for symbol in self._symbols:
            tick = self._quote_fn(symbol)
            if tick is None:
                continue  # no quote yet; the monitor judges staleness, not the source
            ts = datetime.fromtimestamp(tick.time, tz=UTC)
            prev = self._last_ts.get(symbol)
            sequence_gap = prev is not None and ts < prev
            self._last_ts[symbol] = max(prev, ts) if prev else ts
            out.append(
                TickRecord(
                    symbol=symbol,
                    ts=ts,
                    bid=float(tick.bid),
                    ask=float(tick.ask),
                    bid_volume=getattr(tick, "bid_volume", None),
                    ask_volume=getattr(tick, "ask_volume", None),
                    sequence_gap=sequence_gap,
                )
            )
        return out
