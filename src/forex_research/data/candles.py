"""Dukascopy M1 candle acquisition — the one-year strategy-lab data plane.

Ticks scale badly to a year (~10 GB, ~50 h of downloads); Dukascopy also
publishes one LZMA file per instrument per **day** containing that day's M1
candles, which is ~50x smaller for the same price path. The strategy lab
replays on M1 candles with an explicitly modelled, Gate-1-derived spread, so
candles are the right budget/accuracy trade for a 1-year, 5-strategy sweep.
Ticks (already acquired for the Gate 1 week) remain the tick-exact ground
truth whenever a shortlisted configuration needs re-verification.

Wire format: 24-byte big-endian records ``(ms_offset, open, close, low,
high)`` in **points**, zero-indexed URL months, day file may be missing when
the market is closed. Every decoded frame is validated against three
invariants that make a wrong record layout fail loudly instead of silently
poisoning a backtest: monotone timestamps, non-degenerate OHLC ordering, and
per-symbol price magnitude (raw points must exceed the scale by a sane
factor; e.g. EURUSD open points are ~115000, not ~1150).
"""

from __future__ import annotations

import datetime as dt
import lzma
import struct
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import polars as pl

from .dukascopy import POINT_SCALES, _headers

DAY_CANDLE_URL = (
    "https://datafeed.dukascopy.com/datafeed/{symbol}/{y}/{m:02d}/{d:02d}/{side}_candles_min_1.bi5"
)

# 24-byte record: seconds-within-day offset (NOT ms — 1440 M1 candles at
# 60 s spacing span exactly 86,400 s), open/close/low/high as unsigned point
# integers, one float volume. (Layout verified by the OHLC invariant rejecting
# the >IIIfff misreading, and by 1440 rows per full trading day.)
_CANDLE = struct.Struct(">IIIIIf")
RECORD_BYTES = _CANDLE.size  # 24


def day_url(symbol: str, day: dt.date, side: str = "BID") -> str:
    """URL for one day's M1 candle file. URL months are zero-indexed."""
    if side not in ("BID", "ASK"):
        raise ValueError(f"side must be BID or ASK, got {side!r}")
    return DAY_CANDLE_URL.format(symbol=symbol, y=day.year, m=day.month - 1, d=day.day, side=side)


def parse_day_candles(
    payload: bytes, day: dt.date, scale: int, *, symbol: str = ""
) -> pl.DataFrame:
    """Decode one day file into M1 OHLC rows (UTC, price units).

    Empty payload -> empty frame (market closed). Malformed or implausible
    content raises: a mis-decoded year of candles is the expensive failure.
    """
    if not payload:
        return pl.DataFrame(schema=CANDLE_SCHEMA)
    try:
        raw = lzma.decompress(payload)
    except lzma.LZMAError as exc:
        raise ValueError(f"candle payload is not LZMA ({exc})") from None
    if len(raw) % RECORD_BYTES:
        raise ValueError(f"candle payload length {len(raw)} is not a multiple of {RECORD_BYTES}")
    # Naive UTC, matching the repo's TICK_SCHEMA convention.
    day_start = dt.datetime(day.year, day.month, day.day)
    rows = []
    for i in range(0, len(raw), RECORD_BYTES):
        offset_s, o, c, lo, hi, _vol = _CANDLE.unpack_from(raw, i)
        rows.append(
            (
                day_start + dt.timedelta(seconds=offset_s),
                o / scale,
                hi / scale,
                lo / scale,
                c / scale,
            )
        )
    out = pl.DataFrame(
        rows,
        schema={
            "ts": pl.Datetime("ms"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
        },
        orient="row",
    )
    _validate(out, day, scale, symbol)
    return out


CANDLE_SCHEMA = {
    "ts": pl.Datetime("ms"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
}


def _validate(df: pl.DataFrame, day: dt.date, scale: int, symbol: str) -> None:
    """Fail loudly on anything that suggests a mis-parse, per DATA-011."""
    if df.height == 0:
        return
    ts = df["ts"]
    if ts.is_sorted() is False:
        raise ValueError(f"{symbol or 'candles'} {day}: timestamps not monotone")
    bad = df.select(
        (
            (pl.col("high") < pl.max_horizontal(pl.col("low"), pl.col("open"), pl.col("close")))
            | (pl.col("low") > pl.min_horizontal(pl.col("high"), pl.col("open"), pl.col("close")))
        )
        .any()
        .alias("bad_ohlc")
    )["bad_ohlc"][0]
    if bad:
        raise ValueError(f"{symbol or 'candles'} {day}: OHLC ordering violated (mis-parse?)")
    # A sane price band for every scaled instrument: below 0.001 means the
    # scale was applied twice, above 100_000 means not applied at all.
    # (Verified live: EURUSD ~1.17, USDJPY ~154, XAUUSD ~4268.)
    peak = float(
        max(
            df["open"].max() or 0.0,
            df["high"].max() or 0.0,
            df["low"].max() or 0.0,
            df["close"].max() or 0.0,
        )
    )
    if not 0.001 < peak < 100_000:
        raise ValueError(
            f"{symbol or 'candles'} {day}: peak price {peak} implausible for scale {scale}"
        )


def fetch_day_candles(
    symbol: str,
    day: dt.date,
    *,
    side: str = "BID",
    timeout: float = 60.0,
    attempts: int = 5,
    backoff: float = 5.0,
    sleep=time.sleep,
    opener=urllib.request.urlopen,
) -> pl.DataFrame:
    """Download and decode one day's M1 candles. 404/empty -> empty frame."""
    req = urllib.request.Request(day_url(symbol, day, side), headers=_headers())
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with opener(req, timeout=timeout) as resp:
                payload = resp.read()
            return parse_day_candles(payload, day, POINT_SCALES[symbol], symbol=symbol)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return pl.DataFrame(schema=CANDLE_SCHEMA)
            last_error = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        if attempt + 1 < attempts:
            sleep(min(backoff * 2**attempt, 120.0))
    raise ConnectionError(f"{symbol} {day} candles failed after {attempts} attempts: {last_error}")


def iter_days(start: dt.date, end: dt.date):
    """Yield every calendar day in ``[start, end)``."""
    cursor = start
    while cursor < end:
        yield cursor
        cursor += dt.timedelta(days=1)


def fetch_candle_range(
    symbol: str,
    start: dt.date,
    end: dt.date,
    *,
    side: str = "BID",
    timeout: float = 60.0,
    attempts: int = 4,
    backoff: float = 5.0,
    sleep=time.sleep,
    opener=urllib.request.urlopen,
    workers: int = 6,
    cache_dir: Path | None = None,
) -> pl.DataFrame:
    """Download ``[start, end)`` of M1 candles as one frame, cached per day.

    Same resume semantics as the tick fetcher: every completed day is written
    to ``cache_dir`` immediately, so a flaky run continues where it stopped.
    Missing days (weekends) persist as zero-byte markers and never re-download.
    """
    days = list(iter_days(start, end))
    frames: dict[dt.date, pl.DataFrame] = {}
    todo: list[dt.date] = []
    if cache_dir is not None:
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
    for day in days:
        cached = cache_dir / _cache_name(symbol, day) if cache_dir is not None else None
        if cached is not None and cached.exists():
            if cached.stat().st_size == 0:
                frames[day] = pl.DataFrame(schema=CANDLE_SCHEMA)  # closed-market marker
            else:
                frames[day] = pl.read_parquet(cached)
        else:
            todo.append(day)
    if todo and workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    fetch_day_candles,
                    symbol,
                    day,
                    side=side,
                    timeout=timeout,
                    attempts=attempts,
                    backoff=backoff,
                    sleep=sleep,
                    opener=opener,
                ): day
                for day in todo
            }
            for future in as_completed(futures):
                day = futures[future]
                try:
                    df = future.result()
                except ConnectionError:
                    continue  # reported with the rest below
                frames[day] = df
                _write_cache(cache_dir, symbol, day, df)
        failed = [d for d in todo if d not in frames]
    else:
        failed = []
        for day in todo:
            try:
                frames[day] = fetch_day_candles(
                    symbol,
                    day,
                    side=side,
                    timeout=timeout,
                    attempts=attempts,
                    backoff=backoff,
                    sleep=sleep,
                    opener=opener,
                )
            except ConnectionError:
                failed.append(day)
            else:
                _write_cache(cache_dir, symbol, day, frames[day])
    if failed:
        raise ConnectionError(
            f"{symbol}: {len(failed)} day(s) failed after retries: "
            + ", ".join(str(d) for d in sorted(failed))
        )
    if not frames:
        return pl.DataFrame(schema=CANDLE_SCHEMA)
    out = pl.concat(frames[d] for d in sorted(frames)).sort("ts")
    return out.unique(subset=["ts"], keep="last", maintain_order=True)


def _write_cache(cache_dir: Path | None, symbol: str, day: dt.date, df: pl.DataFrame) -> None:
    if cache_dir is None:
        return
    path = cache_dir / _cache_name(symbol, day)
    if df.height == 0:
        path.write_bytes(b"")  # closed-market marker: durable across restarts
    else:
        df.write_parquet(path)


def _cache_name(symbol: str, day: dt.date) -> str:
    return f"{symbol}_{day:%Y%m%d}.parquet"
