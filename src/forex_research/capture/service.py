"""Capture service — MILE-022, COST-013.

Captures every available quote into ``data/raw/venue_quotes/`` (partitioned
by symbol-month, DATA-004 shape). Resumes from the files on disk (crash-safe:
no replay duplication), distinguishes suspected quote gaps from confirmed
capture downtime, retries/reconnects on feed failure, and keeps only bounded
state in memory.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

from .config import CaptureConfig
from .lock import CaptureLock, CaptureLocked
from .monitor import CaptureMonitor
from .store import TickStore
from .tick_source import PollingQuoteSource


def _default_quote_fn(symbols: list[str]):
    """MT5-backed quote poller for the shipped CLI path (EXEC-001, Windows).

    Returns the platform's ``symbol_info_tick`` bound to the running,
    logged-in terminal. Raises ``AdapterError`` when no terminal is
    reachable, so the CLI fails fast instead of polling a dead source.
    """
    from ..execution.mt5_adapter import MT5Adapter

    adapter = MT5Adapter()
    adapter.connect()
    return adapter.subscribe_quotes(symbols)


class CaptureService:
    def __init__(
        self,
        config: CaptureConfig,
        *,
        quote_fn=None,
        root: Path | None = None,
    ) -> None:
        self.config = config
        root = Path(root) if root else Path(config.output_dir)
        self.store = TickStore(root, flush_interval_s=config.flush_interval_s)
        self.monitor = CaptureMonitor(thresholds_ms=config.continuity_threshold_ms)
        # No injected source means the MT5 terminal (the shipped path).
        self._quote_fn = quote_fn or _default_quote_fn(list(config.symbols))
        self._source = PollingQuoteSource(list(config.symbols), self._quote_fn)
        self._resume_points: dict[str, datetime | None] = {}
        self._cycles = 0
        self._stop = False
        self.started_at = datetime.now(UTC)

    # -- lifecycle ----------------------------------------------------------
    def set_resume_points(self, points: dict[str, datetime | None]) -> None:
        """Skip stored ticks at or before these timestamps (crash-safe resume)."""
        self._resume_points = dict(points)

    def poll_once(self) -> int:
        """One poll cycle; returns the number of new ticks stored."""
        ticks = self._source.poll()
        stored = 0
        for tick in ticks:
            if tick.sequence_gap:
                self.monitor.record_sequence_gap(tick.symbol)
            resume = self._resume_points.get(tick.symbol)
            if resume is not None and tick.ts <= resume:
                continue  # already on disk: no replay duplication
            self.monitor.record_tick(tick.symbol, tick.ts)
            self.store.append(tick)
            stored += 1
        self.store.flush_if_due()
        self._cycles += 1
        return stored

    def run(self, *, max_cycles: int | None = None, initial_backoff_s: float = 1.0) -> None:
        """Run continuously. ``max_cycles`` bounds the run for smoke tests.

        Every poll *attempt* counts toward the bound — including failed
        ones — so a bounded smoke run against a dead feed terminates
        instead of retrying forever. ``initial_backoff_s`` scales the retry
        backoff (tests use a small value to stay fast).
        """
        cycles = 0
        backoff_s = initial_backoff_s
        while not self._stop and (max_cycles is None or cycles < max_cycles):
            cycles += 1
            try:
                self.poll_once()
                backoff_s = initial_backoff_s
            except Exception as exc:  # noqa: BLE001 - retry/reconnect on feed failure
                # Downtime during the backoff is recorded via open_downtime on
                # the next successful poll (monitor distinguishes it from a
                # suspected feed gap). Per-symbol keys, not a placeholder:
                # record_tick closes only the window of the symbol it
                # observes, so a "*" window would never close.
                now = datetime.now(UTC)
                for symbol in self.config.symbols:
                    self.monitor.open_downtime(symbol, now)
                print(f"capture poll failed ({exc}); retrying in {backoff_s:.0f}s")
                time.sleep(backoff_s)
                backoff_s = min(backoff_s * 2, 60.0)
                continue
            time.sleep(self.config.poll_interval_s)
        # Normal exit (bounded run): persist whatever is still buffered.
        self.store.flush()

    def stop(self) -> None:
        self._stop = True

    # -- reporting ----------------------------------------------------------
    def status(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(),
            "cycles": self._cycles,
            "symbols": self.config.symbols,
            "monitor": self.monitor.summary(),
        }


def run_capture(config_path: Path, *, max_cycles: int | None = None) -> int:
    """CLI entry: acquire the lock, resume from disk, run until stopped."""
    from .config import load_capture_config

    config = load_capture_config(config_path)
    lock = CaptureLock(config.output_dir / ".capture.lock")
    try:
        lock.acquire()
    except CaptureLocked as exc:
        print(f"refusing to start: {exc}")
        return 2
    service = None
    try:
        service = CaptureService(config)
        # Resume: skip anything at or before the newest stored timestamp.
        resume_points = {s: service.store.last_ts(s) for s in config.symbols}
        service.set_resume_points(resume_points)
        for symbol, ts in resume_points.items():
            print(f"resume {symbol}: from {ts.isoformat() if ts else 'beginning'}")
        service.run(max_cycles=max_cycles)
    except Exception as exc:  # noqa: BLE001 - start-up failure is an operator message
        print(f"capture failed to start: {exc}")
        return 1
    finally:
        # Ctrl+C or exceptions must not discard buffered ticks either.
        if service is not None:
            try:
                service.store.flush()
            except Exception:  # noqa: BLE001 - best-effort flush on shutdown
                pass
        lock.release()
    return 0
