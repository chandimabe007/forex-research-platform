"""Capture service — COST-013, DATA-004, MILE-022."""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from forex_research.capture.config import CaptureConfig
from forex_research.capture.lock import CaptureLock, CaptureLocked
from forex_research.capture.monitor import CaptureMonitor
from forex_research.capture.report import episode_counts, report
from forex_research.capture.service import CaptureService, run_capture


def _config(tmp_path: Path) -> CaptureConfig:
    return CaptureConfig(
        symbols=("EURUSD",),
        output_dir=tmp_path / "quotes",
        continuity_threshold_ms={"asia": 5000},
        episode_minimums={"rollover": 8},
        poll_interval_s=0.0,
        flush_interval_s=0.0,
    )


def test_bounded_run_terminates_against_dead_feed(tmp_path):
    """Every poll ATTEMPT counts toward max_cycles: a bounded smoke run
    against a permanently failing feed must terminate, not retry forever."""
    cfg = CaptureConfig(symbols=("EURUSD",), output_dir=tmp_path / "q",
                        continuity_threshold_ms={}, episode_minimums={})

    def dead_feed(symbol):
        raise ConnectionError("feed down")

    svc = CaptureService(cfg, quote_fn=dead_feed)
    t0 = time.perf_counter()
    svc.run(max_cycles=2, initial_backoff_s=0.01)
    assert time.perf_counter() - t0 < 5, "bounded run hung on a dead feed"
    # Both attempts failed: the status metric counts completed polls (0),
    # while the bound counted attempts — and the downtime window is open.
    assert svc._cycles == 0
    assert svc.monitor.state("EURUSD")._open_downtime_start is not None


def test_cli_without_injected_source_uses_mt5_source(tmp_path, monkeypatch):
    """The shipped CLI path wires the MT5-backed source when none is injected,
    and a start-up failure of that source is an operator message, not a crash."""
    import forex_research.capture.service as service_mod

    def fake_source(symbols):
        return lambda symbol: None  # "no quote yet" poller

    monkeypatch.setattr(service_mod, "_default_quote_fn", fake_source)
    config_path = tmp_path / "capture.yaml"
    config_path.write_text(
        "symbols:\n  - EURUSD\noutput_dir: " + str(tmp_path / "q").replace("\\\\", "/")
        + "\n",
        encoding="utf-8",
    )
    assert run_capture(config_path, max_cycles=1) == 0


class _FakeTick:
    def __init__(self, ts: float, bid: float, ask: float) -> None:
        self.time = ts
        self.bid = bid
        self.ask = ask
        self.bid_volume = 1.0
        self.ask_volume = 1.0


def _make_quote_fn():
    """A fresh quote source replaying the same two quotes; each service run
    gets its own instance so a restart replays the same market."""

    def factory():
        state = {"n": 0}

        def fn(symbol: str):
            state["n"] += 1
            if state["n"] == 1:
                return _FakeTick(1.0, 1.10, 1.10 + 0.0002)
            if state["n"] == 2:
                return _FakeTick(2.0, 1.10, 1.10 + 0.0002)
            return None

        return fn

    return factory


def test_capture_every_available_quote(tmp_path):
    # COST-013: capture every available quote, not periodic snapshots.
    service = CaptureService(_config(tmp_path), quote_fn=_make_quote_fn()())
    for _ in range(3):
        service.poll_once()
    service.store.flush()
    files = list((tmp_path / "quotes" / "EURUSD").glob("*.jsonl"))
    assert files, "no capture files written"
    lines = [json.loads(ln) for f in files for ln in f.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2


def test_resume_does_not_duplicate(tmp_path):
    factory = _make_quote_fn()
    service = CaptureService(_config(tmp_path), quote_fn=factory())
    for _ in range(3):
        service.poll_once()
    service.store.flush()

    # Restart: resume point is the newest stored ts; a replay of the same
    # market stores nothing new (crash-safe resume, no duplication).
    resumed = CaptureService(_config(tmp_path), quote_fn=factory())
    resume_ts = resumed.store.last_ts("EURUSD")
    assert resume_ts is not None
    resumed.set_resume_points({"EURUSD": resume_ts})
    for _ in range(3):
        resumed.poll_once()
    resumed.store.flush()
    lines = [
        json.loads(ln)
        for f in (tmp_path / "quotes" / "EURUSD").glob("*.jsonl")
        for ln in f.read_text().splitlines()
        if ln.strip()
    ]
    assert len(lines) == 2


def test_monitor_distinguishes_gap_from_downtime():
    monitor = CaptureMonitor(thresholds_ms={})
    t0 = datetime(2024, 1, 1, tzinfo=UTC)
    monitor.open_downtime("EURUSD", t0)
    monitor.record_tick("EURUSD", t0)
    monitor.record_tick("EURUSD", t0 + _td(seconds=30))
    summary = monitor.summary()["EURUSD"]
    assert summary["max_quote_gap_ms"] == 30_000  # a suspected feed gap
    # Downtime stays zero because a tick closed the window immediately.
    assert summary["downtime_ms"] == 0


def _td(**kw):
    from datetime import timedelta

    return timedelta(**kw)


def test_sequence_gap_counted():
    monitor = CaptureMonitor(thresholds_ms={})
    monitor.record_sequence_gap("EURUSD")
    assert monitor.summary()["EURUSD"]["sequence_gaps"] == 1


def test_lock_prevents_second_instance(tmp_path):
    lock_path = tmp_path / ".capture.lock"
    with CaptureLock(lock_path):
        with pytest.raises(CaptureLocked):
            CaptureLock(lock_path).acquire()


def test_episode_report_counts_visits_not_rows(tmp_path):
    root = tmp_path / "quotes" / "EURUSD"
    root.mkdir(parents=True)
    ts = [
        "2024-01-02T00:30:00+00:00",  # asia
        "2024-01-02T00:35:00+00:00",  # same visit
        "2024-01-02T08:00:00+00:00",  # london
        "2024-01-02T13:00:00+00:00",  # overlap
        "2024-01-02T13:30:00+00:00",  # same overlap visit
    ]
    with (root / "2024-01.jsonl").open("w", encoding="utf-8") as fh:
        for t in ts:
            fh.write(
                json.dumps({"ts": t, "bid": 1.1, "ask": 1.1, "sequence_gap": False}) + "\n"
            )
    counts = episode_counts(tmp_path / "quotes")
    assert counts["episodes"]["EURUSD"]["asia"] == 1
    assert counts["episodes"]["EURUSD"]["london"] == 1
    assert counts["episodes"]["EURUSD"]["overlap"] == 1


def test_report_names_deferred_buckets(tmp_path):
    result = report(tmp_path / "quotes", minimums={"rollover": 8})
    assert "MILE-031" in result["text"]
    assert "DATA-014" in result["text"]
