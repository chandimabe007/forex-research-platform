"""Episode report — COST-013: state minimum independent episode counts per
bucket, keep rollover, news and market-open events as separate episode
datasets, and continue capture throughout the project (the one-month model
is provisional).

Volatility- and news-conditioned buckets await a volatility model (MILE-031)
and the economic calendar (DATA-014): the report names them as deferred
rather than faking counts.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, time
from pathlib import Path

BUCKETS = ("asia", "london", "new_york", "overlap", "rollover")
# Provisional UTC session edges (COST-011: replaced by DST-aware sessions at
# MILE-031 via data.sessions; the capture report names this as provisional).
SESSION_EDGES_UTC = {
    "asia": (time(0, 0), time(7, 0)),
    "london": (time(7, 0), time(12, 0)),
    "new_york": (time(12, 0), time(16, 30)),
    "overlap": (time(12, 0), time(16, 30)),
    "rollover": (time(23, 55), time(0, 5)),
}


def bucket_for(ts: datetime) -> str | None:
    t = ts.timetz().replace(tzinfo=None)
    if SESSION_EDGES_UTC["rollover"][0] <= t or t < SESSION_EDGES_UTC["rollover"][1]:
        return "rollover"
    # Provisional model: the NY afternoon is the London+NY overlap.
    o_start, o_end = SESSION_EDGES_UTC["overlap"]
    if o_start <= t < o_end:
        return "overlap"
    for bucket in ("asia", "london", "new_york"):
        start, end = SESSION_EDGES_UTC[bucket]
        if start <= t < end:
            return bucket
    return None


def episode_counts(root: Path) -> dict:
    """Count independent episodes per symbol and bucket from stored ticks.

    An episode is a maximal run of quotes within one bucket visit; the
    minimums at COST-013 apply to these, not to total rows.
    """
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    rows: dict[str, int] = defaultdict(int)
    for symbol_dir in sorted(Path(root).glob("*/")):
        symbol = symbol_dir.name
        if symbol.startswith("."):
            continue
        current_bucket: str | None = None
        for month_file in sorted(symbol_dir.glob("*.jsonl")):
            with month_file.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    payload = json.loads(line)
                    ts = datetime.fromisoformat(payload["ts"])
                    bucket = bucket_for(ts)
                    rows[symbol] += 1
                    if bucket is None:
                        current_bucket = None
                        continue
                    if bucket != current_bucket:
                        counts[symbol][bucket] += 1
                        current_bucket = bucket
    return {"rows": dict(rows), "episodes": {k: dict(v) for k, v in counts.items()}}


def report(root: Path, minimums: dict[str, int], out_path: Path | None = None) -> dict:
    counts = episode_counts(root)
    lines: list[str] = [
        "# Venue quote capture — episode report (COST-013)",
        "",
        "Provisional UTC session edges until GATE-005 verification; DST-aware",
        "sessions replace them at MILE-031 (`data.sessions`).",
        "",
    ]
    minimums = minimums or {}
    for symbol, buckets in sorted(counts["episodes"].items()):
        lines.append(f"## {symbol}")
        lines.append("")
        lines.append("| Bucket | Episodes observed | Minimum | Status |")
        lines.append("| --- | --- | --- | --- |")
        for bucket in BUCKETS:
            observed = buckets.get(bucket, 0)
            minimum = int(minimums.get(bucket, 0))
            status = "ok" if observed >= minimum else "below minimum"
            lines.append(f"| {bucket} | {observed} | {minimum} | {status} |")
        lines.append("")
    deferred = [
        "volatility-conditioned buckets: deferred (needs the volatility model, MILE-031)",
        "news-conditioned buckets: deferred (needs the economic calendar, DATA-014)",
        "stressed-conditions episodes: collected opportunistically; reported when present",
    ]
    lines.append("## Deferred buckets")
    lines.append("")
    for item in deferred:
        lines.append(f"- {item}")
    lines.append("")
    text = "\n".join(lines)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
    return {"text": text, "counts": counts}


if __name__ == "__main__":
    import sys

    root = Path(sys.argv[1] if len(sys.argv) > 1 else "data/raw/venue_quotes")
    result = report(root, minimums={})
    print(result["text"])
