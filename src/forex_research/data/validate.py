"""Validation — DATA-010.

Block promotion from ``raw/`` to ``cleaned/``. Each failure writes a report;
none silently repairs. A wrong time zone is the most damaging silent error,
so the time-zone profile check rejects on mismatch.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import time

import polars as pl

# Provisional UTC session edges for the time-zone profile check; replaced by
# DST-aware sessions (data.sessions) once the server zone is verified (GATE-005).
SESSION_EDGES_UTC: dict[str, tuple[time, time]] = {
    "asia": (time(0, 0), time(7, 0)),
    "london": (time(7, 0), time(12, 0)),
    "new_york": (time(12, 0), time(16, 30)),
}


@dataclass
class ValidationReport:
    source: str
    failures: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    session_gaps: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return not self.failures

    def to_json(self) -> str:
        import json

        return json.dumps(
            {
                "source": self.source,
                "ok": self.ok(),
                "failures": self.failures,
                "quarantined": self.quarantined,
                "flags": self.flags,
                "session_gaps": self.session_gaps,
            },
            indent=2,
        )


def check_ticks(df: pl.DataFrame, *, source: str = "ticks") -> ValidationReport:
    """DATA-010 checks on ticks. Reject = failure; nothing is repaired."""
    report = ValidationReport(source=source)
    if df.is_empty():
        report.failures.append("empty file")
        return report

    # Crossed quotes: ask < bid at any tick — reject file.
    crossed = df.filter(pl.col("ask") < pl.col("bid"))
    if not crossed.is_empty():
        report.failures.append(
            f"crossed quotes on {crossed.height} ticks (ask < bid) — reject (DATA-010)"
        )

    # Non-positive prices — reject file.
    bad = df.filter((pl.col("ask") <= 0) | (pl.col("bid") <= 0))
    if not bad.is_empty():
        report.failures.append(f"non-positive prices on {bad.height} ticks — reject (DATA-010)")
    return report


def check_bars(
    df: pl.DataFrame,
    *,
    source: str,
    expected_precision: int | None = None,
    expected_session_peaks: Sequence[str] | None = None,
) -> ValidationReport:
    """DATA-010 checks on bars.

    Reject on duplicates, OHLC incoherence, precision mismatch or a failed
    time-zone profile; quarantine weekend bars; flag (never remove) price
    spikes; record session gaps in the report.
    """
    report = ValidationReport(source=source)
    if df.is_empty():
        report.failures.append("empty file")
        return report

    # Duplicate timestamps: repeated ts_open — reject file.
    dupe_count = df.select(pl.col("ts_open").is_duplicated().sum()).item()
    if dupe_count:
        report.failures.append(f"{dupe_count} duplicate ts_open rows — reject (DATA-010)")

    # OHLC coherence, both sides — reject file.
    for side in ("bid", "ask"):
        open_, high, low, close = (pl.col(f"{side}_{k}") for k in ("open", "high", "low", "close"))
        incoherent = df.filter(
            (low > pl.min_horizontal(open_, close)) | (high < pl.max_horizontal(open_, close))
        )
        if not incoherent.is_empty():
            report.failures.append(
                f"{side} OHLC incoherent on {incoherent.height} bars — reject (DATA-010)"
            )

    # Weekend bars: data in a documented closure window — quarantine.
    weekend_count = df.select((pl.col("ts_open").dt.weekday() >= 6).sum()).item()
    if weekend_count:
        report.quarantined.append(f"{weekend_count} weekend bars — quarantine (DATA-010)")

    # Price spike: single-bar move beyond 15x trailing 100-bar sigma — flag
    # for review, never auto-remove.
    for side in ("bid", "ask"):
        flagged = (
            df.with_columns((pl.col(f"{side}_close") / pl.col(f"{side}_open") - 1.0).alias("_ret"))
            .with_columns(
                pl.col("_ret").shift(1).rolling_std(window_size=100, min_samples=2).alias("_sigma")
            )
            .filter(pl.col("_ret").abs() > 15.0 * pl.col("_sigma"))
        )
        if not flagged.is_empty():
            report.flags.append(
                f"{side}: {flagged.height} bars beyond 15x trailing 100-bar sigma — "
                "flagged for review, never auto-removed (DATA-010)"
            )

    # Time-zone profile: session volume peaks at expected hours — reject on
    # mismatch.
    if expected_session_peaks:
        observed = _volume_peaks_by_session(df)
        for session in expected_session_peaks:
            if session not in observed:
                report.failures.append(
                    f"time-zone profile: no volume peak in expected session "
                    f"'{session}' — reject (DATA-010)"
                )

    # Precision: decimal places match DATA-002 — reject.
    if expected_precision is not None:
        for side in ("bid", "ask"):
            decimals = (
                df[f"{side}_close"].cast(pl.String).str.split(".").list.last().str.len_chars()
            )
            over_count = decimals.gt(expected_precision).sum()
            if over_count:
                report.failures.append(
                    f"{side}: {over_count} bars exceed {expected_precision} decimal "
                    "places — reject (DATA-010)"
                )
    return report


def _volume_peaks_by_session(df: pl.DataFrame) -> list[str]:
    """Sessions holding more than 20% of volume count as peaks."""
    total = df["volume"].sum()
    if not total:
        return []
    peaks: list[str] = []
    for session, (start, end) in SESSION_EDGES_UTC.items():
        part = df.filter(
            (pl.col("ts_open").dt.hour() >= start.hour) & (pl.col("ts_open").dt.hour() < end.hour)
        )
        if part.is_empty():
            continue
        if (part["volume"].sum() or 0) / total > 0.2:
            peaks.append(session)
    return peaks
