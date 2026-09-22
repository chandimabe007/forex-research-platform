"""Probe: cache-count helper for Gate 1 acquisition passes."""

import sys
from pathlib import Path

cache = Path(sys.argv[1] if len(sys.argv) > 1 else "data/raw/dukascopy_cache")
for sym in ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD"):
    n = len(list(cache.glob(f"{sym}_*.parquet")))
    print(f"{sym}: {n} hours cached")
