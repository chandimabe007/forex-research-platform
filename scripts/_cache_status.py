"""Count cached M1 candle days per symbol: data/raw/candles_m1 inventory."""

from __future__ import annotations

import datetime as dt
import re
import sys
from collections import defaultdict
from pathlib import Path

CACHE = Path(__file__).resolve().parents[1] / "data" / "raw" / "candles_m1"

PAT = re.compile(r"^(?P<sym>[A-Z]+)_(?P<day>\d{8})\.parquet$")


def main() -> int:
    counts: dict[str, list[dt.date]] = defaultdict(list)
    if CACHE.exists():
        for p in CACHE.glob("*.parquet"):
            m = PAT.match(p.name)
            if m:
                counts[m["sym"]].append(dt.datetime.strptime(m["day"], "%Y%m%d").date())
    total_needed = (dt.date(2026, 9, 15) - dt.date(2025, 4, 1)).days
    for sym in ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD"):
        days = sorted(counts.get(sym, []))
        span = f"{days[0]}..{days[-1]}" if days else "-"
        print(f"{sym}: {len(days)}/{total_needed} days cached ({span})")
        if len(sys.argv) > 1 and sys.argv[1] == "--missing" and days:
            want = set()
            cur = dt.date(2025, 4, 1)
            while cur < dt.date(2026, 9, 15):
                want.add(cur)
                cur += dt.timedelta(days=1)
            missing = sorted(want - set(days))
            if missing:
                print(f"  first missing: {missing[:6]} ({len(missing)} total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
