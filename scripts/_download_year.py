"""Download a symbol's M1 candles over a date range, resumable by pass loop.

Usage: python scripts/_download_year.py EURUSD 2025-04-01 2026-09-15 [budget_s]

Loops fetch passes (each cached day is skipped) until the range is complete
or the wall-time budget expires; every pass prints durable progress. Exit 0
only when the whole range is cached.
"""

from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from forex_research.data.candles import fetch_candle_range  # noqa: E402

CACHE = Path(__file__).resolve().parents[1] / "data" / "raw" / "candles_m1"


def main() -> int:
    symbol = sys.argv[1]
    start = dt.date.fromisoformat(sys.argv[2])
    end = dt.date.fromisoformat(sys.argv[3])
    budget = float(sys.argv[4]) if len(sys.argv) > 4 else 540.0
    deadline = time.monotonic() + budget
    pass_no = 0
    while time.monotonic() < deadline:
        pass_no += 1
        try:
            df = fetch_candle_range(
                symbol,
                start,
                end,
                cache_dir=CACHE,
                workers=8,
                timeout=45,
                attempts=4,
                backoff=8.0,
            )
            n_days = (end - start).days
            print(
                f"{symbol}: COMPLETE pass {pass_no}: {df.height} candles, {n_days} days cached",
                flush=True,
            )
            return 0
        except ConnectionError as exc:
            msg = str(exc)
            print(f"{symbol} pass {pass_no}: incomplete ({msg[:160]}...)", flush=True)
            remaining = deadline - time.monotonic()
            if remaining > 60:
                time.sleep(30)
            else:
                print(f"{symbol}: budget exhausted with failures: {msg[:200]}", flush=True)
                return 1
    print(f"{symbol}: budget expired mid-range", flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
