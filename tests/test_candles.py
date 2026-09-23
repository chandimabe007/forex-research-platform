"""Candle module tests — format invariants, resume cache, closed-market days."""

from __future__ import annotations

import datetime as dt
import lzma
import struct

import polars as pl
import pytest

from forex_research.data.candles import (
    CANDLE_SCHEMA,
    _cache_name,
    day_url,
    fetch_candle_range,
    fetch_day_candles,
    parse_day_candles,
)

_DAY = dt.date(2025, 9, 15)  # a Monday
_SCALE = 100_000


def _encode_day(rows: list[tuple[int, int, int, int, int, float]]) -> bytes:
    return lzma.compress(b"".join(struct.pack(">IIIIIf", *r) for r in rows))


def _m1_rows(start_s: int, count: int, base: int = 117_250) -> list[tuple]:
    """count consecutive M1 candles starting at ``start_s`` seconds of the day."""
    out = []
    price = base
    for k in range(count):
        o = price
        c = price + 10
        h = price + 20
        low = price - 5
        out.append((start_s + 60 * k, o, c, low, h, 1.5))
        price += 5
    return out


class _FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_day_url_zero_indexed_month():
    # September (9) must appear as 08 in the URL.
    assert day_url("EURUSD", _DAY) == (
        "https://datafeed.dukascopy.com/datafeed/EURUSD/2025/08/15/BID_candles_min_1.bi5"
    )


def test_parse_full_day_1440_rows_with_minute_spacing():
    payload = _encode_day(_m1_rows(0, 1440))
    df = parse_day_candles(payload, _DAY, _SCALE, symbol="EURUSD")
    assert df.height == 1440
    assert df["ts"][1] - df["ts"][0] == dt.timedelta(minutes=1)
    assert df["ts"][-1] == dt.datetime(2025, 9, 15, 23, 59)
    # OHLC ordering sanity and scale: open near 1.17
    assert 1.0 < df["open"][0] < 2.0
    assert df["high"].max() >= df["close"].max()


def test_parse_empty_payload_is_closed_market():
    df = parse_day_candles(b"", _DAY, _SCALE, symbol="EURUSD")
    assert df.height == 0
    assert df.schema == pl.Schema(CANDLE_SCHEMA)


def test_parse_rejects_corrupt_payload():
    with pytest.raises(ValueError, match="not LZMA"):
        parse_day_candles(b"garbage-not-lzma", _DAY, _SCALE, symbol="EURUSD")


def test_parse_rejects_bad_ohlc_from_wrong_layout():
    # A record whose high is below the day's other prices violates the OHLC
    # invariant — exactly what a wrong field layout produces.
    raw = struct.pack(">IIIIIf", 0, 117_000, 117_000, 117_000, 116_000, 0.0)
    with pytest.raises(ValueError, match="OHLC ordering"):
        parse_day_candles(lzma.compress(raw * 4), _DAY, _SCALE, symbol="EURUSD")


def test_parse_rejects_double_scaled_prices():
    # Prices already divided by scale produce ~0.001 — implausible.
    rows = [(s, 1, 2, 1, 2, 1.0) for s in range(0, 3600, 60)]
    with pytest.raises(ValueError, match="implausible"):
        parse_day_candles(_encode_day(rows), _DAY, 100_000, symbol="EURUSD")


def test_fetch_404_is_closed_market():
    def opener(req, timeout=None):
        import urllib.error

        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", None, None)

    df = fetch_day_candles("EURUSD", _DAY, attempts=1, opener=opener)
    assert df.height == 0


def test_fetch_retries_then_succeeds(monkeypatch):
    import urllib.error

    calls = {"n": 0}

    def flaky_opener(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("temporary")
        return _FakeResp(_encode_day(_m1_rows(0, 10)))

    df = fetch_day_candles(
        "EURUSD", _DAY, attempts=3, backoff=0.0, sleep=lambda _s: None, opener=flaky_opener
    )
    assert calls["n"] == 3
    assert df.height == 10


def test_fetch_candle_range_resume_cache_and_closed_marker(tmp_path):
    """Second run re-downloads only failed days; 404 days persist as markers."""
    fetched: list[dt.date] = []
    closed = {dt.date(2025, 9, 13)}  # Saturday

    def opener(req, timeout=None):
        day = _day_from_url(req.full_url)
        if day in closed:
            import urllib.error

            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", None, None)
        fetched.append(day)
        return _FakeResp(_encode_day(_m1_rows(0, 5)))

    def _day_from_url(url: str) -> dt.date:
        # .../SYMBOL/2025/08/15/BID_candles_min_1.bi5 with zero-indexed month
        parts = url.split("/")
        y, m, d = int(parts[-4]), int(parts[-3]) + 1, int(parts[-2])
        return dt.date(y, m, d)

    start, end = dt.date(2025, 9, 12), dt.date(2025, 9, 16)  # Fri, Sat, Sun, Mon
    first = fetch_candle_range(
        "EURUSD",
        start,
        end,
        attempts=1,
        backoff=0.0,
        sleep=lambda _s: None,
        opener=opener,
        workers=1,
        cache_dir=tmp_path,
    )
    # Sat is 404-closed; Sun returns a real (partial-session) file on Dukascopy.
    assert first.height == 15  # Fri, Sun, Mon x 5 candles
    assert sorted(set(fetched)) == [
        dt.date(2025, 9, 12),
        dt.date(2025, 9, 14),
        dt.date(2025, 9, 15),
    ]

    fetched.clear()
    second = fetch_candle_range(
        "EURUSD",
        start,
        end,
        attempts=1,
        backoff=0.0,
        sleep=lambda _s: None,
        opener=opener,
        workers=1,
        cache_dir=tmp_path,
    )
    assert second.height == 15
    assert fetched == []  # nothing re-downloaded: full resume
    assert (tmp_path / _cache_name("EURUSD", dt.date(2025, 9, 13))).stat().st_size == 0
