"""Dukascopy tick acquisition — DATA-001 for MILE-040.

Historical ticks are Gate 1's denominator (stop distances need real price
paths). This module downloads Dukascopy's public tick files and decodes them
into the repo's DATA-004 tick schema.

Wire format, verified two ways — against the vendor's own data-export guide
and against live decoded magnitudes for all four repo symbols:

- One file per instrument per hour of day, raw-LZMA-compressed.
- 20-byte big-endian records: ``>IIIff`` = (ms_within_hour, ask_points,
  bid_points, ask_volume, bid_volume).
- Prices are unsigned integers in **points**, scaled down by a per-instrument
  divisor: 100_000 for most FX, 1_000 for JPY pairs, 1_000 for XAUUSD
  (verified: raw ~4.27e6 -> 4268.xx, matching gold in Sep 2026). The vendor
  guide's warning that non-FX scales vary is taken seriously: an unverified
  instrument refuses to decode rather than guessing a scale.
- Months in URLs are zero-indexed (September is ``08``) but datetimes are
  not — the conversion is centralised here.
- A 404 or an empty payload for an hour means "no ticks that hour" (market
  closed), not an error.
- The feed rate-limits (HTTP 429 without a browser-like User-Agent) and the
  connection stalls occasionally; the fetcher retries with backoff.
"""

from __future__ import annotations

import datetime as dt
import lzma
import struct
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from pathlib import Path

import polars as pl

from .ticks import TICK_SCHEMA

DUKASCOPY_URL = (
    "https://datafeed.dukascopy.com/datafeed/{symbol}/{y}/{m:02d}/{d:02d}/{h:02d}h_ticks.bi5"
)

# Vendor-verified point divisors. XAUUSD is the verified commodity entry the
# guide's warning demands; anything else must be added here after verification,
# never guessed.
POINT_SCALES: dict[str, int] = {
    "EURUSD": 100_000,
    "GBPUSD": 100_000,
    "USDJPY": 1_000,
    "XAUUSD": 1_000,
}

_RECORD = struct.Struct(">IIIff")
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


def point_scale(symbol: str) -> int:
    """Verified point divisor for ``symbol``; refuses to guess (vendor warning)."""
    try:
        return POINT_SCALES[symbol]
    except KeyError:
        raise ValueError(
            f"no verified Dukascopy point scale for {symbol!r} — add it to "
            "POINT_SCALES only after verifying against known price magnitudes"
        ) from None


def hour_url(symbol: str, hour: dt.datetime) -> str:
    """URL for one hour file. URL months are zero-indexed; datetimes are not."""
    return DUKASCOPY_URL.format(
        symbol=symbol, y=hour.year, m=hour.month - 1, d=hour.day, h=hour.hour
    )


def parse_bi5(payload: bytes, hour_start: dt.datetime, scale: int) -> pl.DataFrame:
    """Decode one hour file into the DATA-004 schema (UTC timestamps).

    An empty payload decodes to an empty frame; a malformed one raises.
    """
    hour_start = hour_start.replace(minute=0, second=0, microsecond=0)
    if not payload:
        return pl.DataFrame(schema=TICK_SCHEMA)
    try:
        raw = lzma.decompress(payload)
    except lzma.LZMAError as exc:
        raise ValueError(f"bi5 payload is not LZMA ({exc})") from None
    if len(raw) % _RECORD.size:
        raise ValueError(f"bi5 payload length {len(raw)} is not a multiple of {_RECORD.size}")
    rows = []
    for i in range(0, len(raw), _RECORD.size):
        ms, ask, bid, ask_vol, bid_vol = _RECORD.unpack_from(raw, i)
        ts = hour_start + dt.timedelta(milliseconds=ms)
        rows.append(
            {
                "ts": ts,
                "bid": bid / scale,
                "ask": ask / scale,
                "bid_volume": float(bid_vol),
                "ask_volume": float(ask_vol),
                "sequence_gap": False,
            }
        )
    return pl.DataFrame(rows, schema=TICK_SCHEMA)


def fetch_hour(
    symbol: str,
    hour: dt.datetime,
    *,
    timeout: float = 60.0,
    attempts: int = 5,
    backoff: float = 5.0,
    sleep=time.sleep,
    opener=urllib.request.urlopen,
) -> pl.DataFrame:
    """Download and decode one hour. 404/empty -> empty frame (market closed).

    Retries network faults and server throttling (503/429) with exponential
    backoff; the last attempt's error propagates.
    """
    req = urllib.request.Request(hour_url(symbol, hour), headers=_headers())
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with opener(req, timeout=timeout) as resp:
                payload = resp.read()
            return parse_bi5(payload, hour, point_scale(symbol))
        except urllib.error.HTTPError as exc:
            if exc.code in (404,):
                return pl.DataFrame(schema=TICK_SCHEMA)
            last_error = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        if attempt + 1 < attempts:
            sleep(min(backoff * 2**attempt, 120.0))
    raise ConnectionError(
        f"{symbol} {hour:%Y-%m-%d %H:00} failed after {attempts} attempts: {last_error}"
    )


def _headers() -> dict[str, str]:
    return {"User-Agent": _USER_AGENT, "Referer": "https://freeserv.dukascopy.com/"}


def iter_hours(start: dt.datetime, end: dt.datetime) -> Iterator[dt.datetime]:
    """Yield every UTC hour start in ``[start, end)``."""
    cursor = start.replace(minute=0, second=0, microsecond=0)
    while cursor < end:
        yield cursor
        cursor += dt.timedelta(hours=1)


def fetch_range(
    symbol: str,
    start: dt.datetime,
    end: dt.datetime,
    *,
    timeout: float = 60.0,
    attempts: int = 4,
    backoff: float = 5.0,
    pacing: float = 0.25,
    sleep=time.sleep,
    opener=urllib.request.urlopen,
    workers: int = 6,
    cache_dir: Path | None = None,
) -> pl.DataFrame:
    """Download a symbol's ticks over ``[start, end)`` as one DATA-004 frame.

    Hours are fetched concurrently (``workers`` threads). An hour that
    exhausts its retries is collected and reported at the end — the run dies
    with the full list of failed hours, not on the first one — and nothing
    partial is returned: a silent hole in the tick store would corrupt
    everything downstream (DATA-011). With ``cache_dir`` set, each decoded
    hour is persisted (keyed by symbol + hour) and reused on the next call,
    so a flaky network costs re-downloads of failed hours only, never a
    restart of the whole window.
    """
    hours = list(iter_hours(start, end))
    frames: dict[dt.datetime, pl.DataFrame] = {}
    todo: list[dt.datetime] = []
    if cache_dir is not None:
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
    for hour in hours:
        cached = cache_dir / _cache_name(symbol, hour) if cache_dir is not None else None
        if cached is not None and cached.exists():
            frames[hour] = pl.read_parquet(cached)
        else:
            todo.append(hour)
    if todo and workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    fetch_hour,
                    symbol,
                    hour,
                    timeout=timeout,
                    attempts=attempts,
                    backoff=backoff,
                    sleep=sleep,
                    opener=opener,
                ): hour
                for hour in todo
            }
            for future in as_completed(futures):
                hour = futures[future]
                try:
                    df = future.result()
                except ConnectionError:
                    continue  # reported with the rest of the failures below
                frames[hour] = df
                if cache_dir is not None:
                    # Write as each hour lands: an interrupted run keeps its
                    # progress, and the next run downloads only what is missing.
                    df.write_parquet(cache_dir / _cache_name(symbol, hour))
        failed = [h for h in todo if h not in frames]
    else:
        failed = []
        for hour in todo:
            try:
                frames[hour] = fetch_hour(
                    symbol,
                    hour,
                    timeout=timeout,
                    attempts=attempts,
                    backoff=backoff,
                    sleep=sleep,
                    opener=opener,
                )
            except ConnectionError:
                failed.append(hour)
        if cache_dir is not None:
            for hour in todo:
                if hour in frames:
                    frames[hour].write_parquet(cache_dir / _cache_name(symbol, hour))
    if failed:
        raise ConnectionError(
            f"{symbol}: {len(failed)} hour(s) failed after retries: "
            + ", ".join(f"{h:%Y-%m-%dT%H:00Z}" for h in sorted(failed))
        )
    if not frames:
        return pl.DataFrame(schema=TICK_SCHEMA)
    ticks = pl.concat(frames[h] for h in sorted(frames)).sort("ts")
    # maintain_order=True: unique() shuffles by default, which would undo the sort.
    ticks = ticks.unique(subset=["ts", "bid", "ask"], keep="last", maintain_order=True)
    return ticks


def _cache_name(symbol: str, hour: dt.datetime) -> str:
    return f"{symbol}_{hour:%Y%m%dT%H}.parquet"


def sample_spread_pips(
    ticks: pl.DataFrame, *, pip_size: Decimal, max_interval_ms: int = 120_000
) -> list[Decimal]:
    """Mid-price spread samples in pips, one per tick with a fresh quote.

    COST-013 wants quote-level observation, not bar aggregates. Dukascopy
    emits on quote *change*, so every tick is a fresh quote; the interval
    guard exists only to skip isolated ticks after long dead gaps (quiet
    hours, session edges), where the next observed change may be minutes
    stale relative to the surrounding silence. The default tolerates the
    normal 30 s-plus cadence of quiet sessions while still skipping
    multi-minute dead zones.
    """
    if ticks.height < 2:
        return []
    ts = ticks["ts"].to_list()
    bid = ticks["bid"].to_list()
    ask = ticks["ask"].to_list()
    samples: list[Decimal] = []
    for i in range(1, ticks.height):
        gap_ms = (ts[i] - ts[i - 1]).total_seconds() * 1000
        if gap_ms > max_interval_ms:
            continue
        spread = Decimal(repr(ask[i] - bid[i])) / pip_size
        if spread > 0:
            samples.append(spread)
    return samples
