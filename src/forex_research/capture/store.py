"""Tick store — DATA-004 partitioning (symbol-month) with crash-safe resume.

Append-only JSONL per ``{symbol}/{YYYY-MM}.jsonl``. On startup the service
reads the last timestamp per symbol from disk and skips ticks at or before
it: a restart never replays duplicates, and the downtime between the last
record and the restart is visible as a gap rather than being silently
papered over (DATA-011 spirit: gaps are recorded, never filled).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .tick_source import TickRecord


class TickStore:
    def __init__(self, root: Path, *, flush_interval_s: float = 5.0) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._flush_interval_s = flush_interval_s
        self._buffers: dict[tuple[str, str], list[str]] = {}
        self._dirty_since_flush = False
        self._last_flush = datetime.now(UTC)

    def last_ts(self, symbol: str) -> datetime | None:
        """Resume point: the newest timestamp already on disk for ``symbol``."""
        symbol_dir = self._root / symbol
        if not symbol_dir.exists():
            return None
        newest: datetime | None = None
        for month_file in sorted(symbol_dir.glob("*.jsonl")):
            with month_file.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    ts = datetime.fromisoformat(json.loads(line)["ts"])
                    if newest is None or ts > newest:
                        newest = ts
        return newest

    def append(self, record: TickRecord) -> None:
        month = record.ts.strftime("%Y-%m")
        payload = {
            "ts": record.ts.isoformat(),
            "bid": record.bid,
            "ask": record.ask,
            "bid_volume": record.bid_volume,
            "ask_volume": record.ask_volume,
            "sequence_gap": record.sequence_gap,
        }
        key = (record.symbol, month)
        self._buffers.setdefault(key, []).append(
            json.dumps(payload, separators=(",", ":"))
        )
        self._dirty_since_flush = True

    def flush_if_due(self, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        if not self._dirty_since_flush:
            return False
        if (now - self._last_flush).total_seconds() < self._flush_interval_s:
            return False
        self.flush()
        return True

    def flush(self) -> None:
        for (symbol, month), lines in self._buffers.items():
            target = self._root / symbol / f"{month}.jsonl"
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as fh:
                for line in lines:
                    fh.write(line + "\n")
        self._buffers.clear()
        self._dirty_since_flush = False
        self._last_flush = datetime.now(UTC)
