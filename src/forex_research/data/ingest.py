"""Ingestion — DATA-001, DATA-010, DATA-011, DATA-020.

Promotes validated ticks to cleaned bars with manifests. Failures block
promotion and write reports; nothing is silently repaired.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from .manifest import write_manifest
from .resample import resample_ticks
from .ticks import write_ticks_partition
from .validate import ValidationReport, check_ticks


def ingest_symbol(
    *,
    symbol: str,
    raw_ticks: pl.DataFrame,
    raw_dir: Path,
    cleaned_dir: Path,
    timeframe: str,
    continuity_threshold_ms: int,
    outages: list[tuple[str, str]] | None = None,
    repo: Path | None = None,
) -> tuple[Path | None, Path | None, ValidationReport]:
    """Validate, store the cleaned tick partition, resample to bars.

    Returns (tick_partition_path, bars_path, report). On validation failure
    the paths are None and the report carries the failures (DATA-010).
    """
    report = check_ticks(raw_ticks, source=f"{symbol} raw ticks")
    if not report.ok():
        return None, None, report

    tick_path = write_ticks_partition(
        raw_dir / "ticks_cleaned", symbol, _month_of(raw_ticks), raw_ticks
    )
    bars = resample_ticks(
        raw_ticks,
        timeframe=timeframe,
        continuity_threshold_ms=continuity_threshold_ms,
        outages=outages,
    )
    bars_path = cleaned_dir / symbol / f"bars_{timeframe}.parquet"
    bars_path.parent.mkdir(parents=True, exist_ok=True)
    bars.write_parquet(bars_path, compression="zstd")

    write_manifest(
        out_dir=cleaned_dir / "manifests",
        source=str(raw_dir),
        symbol=symbol,
        timeframe=timeframe,
        date_from=_to_utc(raw_ticks["ts"].min()),
        date_to=_to_utc(raw_ticks["ts"].max()),
        row_count=bars.height,
        gap_list=[],  # gap list recorded by the capture monitor / validation
        cleaned_path=bars_path,
        validation=report,
        repo=repo,
    )
    return tick_path, bars_path, report


def _month_of(df: pl.DataFrame) -> str:
    ts = _to_utc(df["ts"].min())
    return f"{ts:%Y-%m}"


def _to_utc(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    raise TypeError(f"expected datetime, got {type(value)}")
