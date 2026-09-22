"""python -m forex_research.gate1 — run MILE-040 Gate 1 (GATE-020..024).

Default mode analyses already-ingested cleaned ticks. With ``--acquire``, a
bounded Dukascopy window is downloaded first and pushed through the DATA-001
pipeline (validate -> store -> resample -> manifest) so the analysis always
runs on the same cleaned partitions every other consumer uses.
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from .analysis import (
    CandidateTimeframe,
    Gate1Record,
    build_matrix,
    evaluate_gate,
    load_ticks,
    save_record,
)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="forex_research.gate1", description="MILE-040 Gate 1")
    p.add_argument("--symbols", nargs="+", default=["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"])
    p.add_argument(
        "--timeframes", nargs="+", default=["15m", "1h"], help="polars intervals, e.g. 15m 1h"
    )
    p.add_argument("--bars-window", type=int, default=200)
    p.add_argument("--server", default="LHFXSA-Trade", help="fee-schedule server key (COST-016)")
    p.add_argument(
        "--acquire-days", type=int, default=None, help="download N days of Dukascopy ticks first"
    )
    p.add_argument(
        "--start",
        type=dt.datetime.fromisoformat,
        default=None,
        help="pinned window start (UTC ISO); shared by every symbol so the record sits on one basis",
    )
    p.add_argument(
        "--end",
        type=dt.datetime.fromisoformat,
        default=None,
        help="pinned window end (UTC ISO); default with --acquire-days is now-N-days",
    )
    p.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[3])
    p.add_argument(
        "--out", type=Path, default=None, help="record path (default data/gate1/<ts>.json)"
    )
    return p.parse_args(argv)


def _acquire_and_ingest(
    repo: Path, symbols: list[str], start: dt.datetime, end: dt.datetime
) -> None:
    from ..data.dukascopy import fetch_range
    from ..data.ingest import ingest_symbol

    cache_dir = repo / "data" / "raw" / "dukascopy_cache"
    for symbol in symbols:
        print(f"acquiring {symbol} {start:%Y-%m-%d %H:%M} .. {end:%Y-%m-%d %H:%M} UTC")
        ticks = fetch_range(symbol, start, end, cache_dir=cache_dir, workers=6)
        if not ticks.height:
            raise SystemExit(
                f"{symbol}: no ticks acquired — aborting rather than analysing an empty window"
            )
        print(f"  {ticks.height} ticks; validating and ingesting")
        ingest_symbol(
            symbol=symbol,
            raw_ticks=ticks,
            raw_dir=repo / "data" / "raw",
            cleaned_dir=repo / "data" / "cleaned",
            timeframe="1m",
            continuity_threshold_ms=60_000,
            repo=repo,
        )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo = args.repo
    from ..config.loader import load_fee_schedule_for_server
    from ..data.instruments import load_instruments

    specs = load_instruments(repo / "config" / "instruments.yaml")
    window: list[str] = []

    if args.acquire_days or args.start:
        if args.acquire_days:
            end = args.end or dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)
            start = end - dt.timedelta(days=args.acquire_days)
        else:
            if not args.end:
                raise SystemExit("--start requires --end (pinned shared window)")
            start, end = args.start, args.end
        _acquire_and_ingest(repo, args.symbols, start, end)
        window = [start.isoformat(), end.isoformat()]

    timeframes = [CandidateTimeframe(name=t) for t in args.timeframes]
    record = Gate1Record(
        generated_utc=dt.datetime.now(dt.UTC).isoformat(),
        capture_window_utc=window,
        instruments=list(args.symbols),
        timeframes=[t.name for t in timeframes],
        stop_multipliers=["0.5x", "1.0x", "2.0x", "3.0x"],
        threshold=0.25,
        notes=[],
    )

    for symbol in args.symbols:
        spec = specs[symbol]
        # Gate 1 measures round-trip entry costs only; no position is held
        # overnight, so unobserved swap rates cannot affect this table. The
        # disclosure is recorded (COST-016's spirit: assumptions declared).
        fee = load_fee_schedule_for_server(
            repo / "config" / "fees.yaml", server=args.server, allow_unobserved_swaps=True
        )
        ticks = load_ticks(repo / "data" / "raw" / "ticks_cleaned", symbol)
        cells = build_matrix(
            symbol=symbol,
            ticks=ticks,
            spec=spec,
            fee_schedule=fee,
            timeframes=timeframes,
            bars_window=args.bars_window,
        )
        record.cells.extend(cells)
        print(f"{symbol}: {len(cells)} cells")

    record.notes.append(
        f"fees from server '{args.server}' with swaps unobserved — Gate 1 holds "
        "no overnight positions, so swap rates cannot enter this table"
    )

    verdict = evaluate_gate(record)
    out = args.out or (
        repo / "data" / "gate1" / f"gate1_{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S}.json"
    )
    save_record(record, out)

    print(f"\n=== Gate 1 verdict (GATE-022 threshold {verdict['threshold']}) ===")
    print(
        f"cells: {len(record.cells)}  passing: {verdict['passing_cells']}  "
        f"global-bound: {verdict['global_bound_cells']}"
    )
    if verdict["best_c"] is not None:
        print(f"best c = {verdict['best_c']:.4f} at {verdict['best_cell']}")
    print(f"accepted: {verdict['accepted']} — {verdict['note']}")
    print(f"record: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
