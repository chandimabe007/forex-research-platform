"""Probe 4: feed health — timing four sequential hour fetches."""

import datetime as dt
import sys
import time

sys.path.insert(0, "src")
from forex_research.data.dukascopy import fetch_hour

base = dt.datetime(2026, 9, 16, 8, 0, tzinfo=dt.UTC)
for i in range(4):
    hour = base + dt.timedelta(hours=i)
    t0 = time.monotonic()
    try:
        df = fetch_hour("EURUSD", hour, attempts=2, backoff=2.0)
        print(f"{hour:%H:00}  {df.height:>5} ticks  {time.monotonic() - t0:5.1f}s")
    except Exception as exc:  # noqa: BLE001
        print(f"{hour:%H:00}  FAILED  {time.monotonic() - t0:5.1f}s  {exc}")
