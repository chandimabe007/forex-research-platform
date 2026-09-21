"""Raw tick data layer — DATA-001, DATA-004.

Raw ticks are the only market data acquired. M1 derives from ticks; every
higher timeframe derives from M1. No timeframe above ticks is downloaded
independently (DATA-001). Where ticks are unavailable for a period, M1 may
be acquired directly and flagged ``tick_derived: false``.

Tick schema (DATA-004): ts, bid, ask, bid_volume, ask_volume, sequence_gap.
Partitioned by symbol-month.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import polars as pl

TICK_SCHEMA = {
    "ts": pl.Datetime("us", time_zone="UTC"),
    "bid": pl.Float64,
    "ask": pl.Float64,
    "bid_volume": pl.Float64,
    "ask_volume": pl.Float64,
    "sequence_gap": pl.Boolean,
}

DATA_SCHEMA_VERSION = 1


def write_ticks_partition(root: Path, symbol: str, month: str, df: pl.DataFrame) -> Path:
    """Write one symbol-month partition of validated-shape ticks."""
    out_dir = Path(root) / symbol
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{month}.parquet"
    df.write_parquet(path, compression="zstd")
    return path


def read_ticks_partition(root: Path, symbol: str, month: str) -> pl.DataFrame:
    path = Path(root) / symbol / f"{month}.jsonl.parquet"
    if not path.exists():
        raise FileNotFoundError(f"tick partition not found: {path}")
    return pl.read_parquet(path)


def read_symbol_ticks(root: Path, symbol: str) -> pl.DataFrame:
    """Concatenate every stored partition for a symbol, sorted by ts."""
    parts = []
    for path in sorted((Path(root) / symbol).glob("*.parquet")):
        parts.append(pl.read_parquet(path))
    if not parts:
        raise FileNotFoundError(f"no tick partitions under {Path(root) / symbol}")
    df = pl.concat(parts).sort("ts")
    return df


def read_ingested_jsonl(root: Path, symbol: str) -> pl.DataFrame:
    """Read the venue-capture JSONL layout into the DATA-004 schema."""
    frames = []
    for path in sorted((Path(root) / symbol).glob("*.jsonl")):
        rows = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                rows.append(
                    {
                        "ts": datetime.fromisoformat(payload["ts"]),
                        "bid": float(payload["bid"]),
                        "ask": float(payload["ask"]),
                        "bid_volume": float(payload.get("bid_volume") or 0.0),
                        "ask_volume": float(payload.get("ask_volume") or 0.0),
                        "sequence_gap": bool(payload.get("sequence_gap", False)),
                    }
                )
        if rows:
            frames.append(pl.DataFrame(rows, schema=TICK_SCHEMA))
    if not frames:
        raise FileNotFoundError(f"no venue capture files under {Path(root) / symbol}")
    return pl.concat(frames).sort("ts")
