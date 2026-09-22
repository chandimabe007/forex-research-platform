"""Gate 1 (MILE-040) tests — the measurement, the uncertainty, the verdict."""

from __future__ import annotations

import datetime as dt
import io
import json
import lzma
import struct
from decimal import Decimal

import polars as pl
import pytest

from forex_research.data.dukascopy import (
    POINT_SCALES,
    fetch_hour,
    hour_url,
    iter_hours,
    parse_bi5,
    point_scale,
    sample_spread_pips,
)
from forex_research.data.instruments import load_instruments
from forex_research.gate1.analysis import (
    MIN_STOP_WINDOW,
    CandidateTimeframe,
    CostCell,
    Gate1Record,
    build_matrix,
    cost_cell,
    evaluate_gate,
    load_ticks,
    median_true_range,
    save_record,
)

# ---------------------------------------------------------------- fixtures


def _spec(symbol: str = "EURUSD"):
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    specs = load_instruments(repo / "config" / "instruments.yaml")
    return specs[symbol]


def _synth_ticks(
    start: dt.datetime,
    bars: int = 300,
    bar_minutes: int = 15,
    spread: float = 0.00010,
    seed: int = 7,
) -> pl.DataFrame:
    """Continuous 30 s ticks with a random walk (0.8-pip steps), dense enough
    that every bar is coverage-complete (30 s gaps pass the 60 s check)."""
    import random

    rng = random.Random(seed)
    rows = []
    t = start
    mid = 1.1000
    for _ in range(bars * bar_minutes * 2):  # one tick per 30 s
        mid += rng.choice((-1, 1)) * 0.00008  # +/- 0.8 pip
        rows.append(
            {
                "ts": t,
                "bid": mid - spread / 2,
                "ask": mid + spread / 2,
                "bid_volume": 1.0,
                "ask_volume": 1.0,
                "sequence_gap": False,
            }
        )
        t += dt.timedelta(seconds=30)
    return pl.DataFrame(rows).with_columns(pl.col("ts").dt.replace_time_zone("UTC"))


@pytest.fixture
def synth_ticks():
    return _synth_ticks(dt.datetime(2026, 9, 14, 6, 0))  # Monday 06:00 UTC


# ------------------------------------------------------------- dukascopy


def test_point_scale_refuses_unknown_instruments():
    with pytest.raises(ValueError, match="verified"):
        point_scale("XYZABC")


def test_point_scales_match_verified_divisors():
    assert POINT_SCALES["EURUSD"] == 100_000
    assert POINT_SCALES["USDJPY"] == 1_000
    assert POINT_SCALES["XAUUSD"] == 1_000


def test_hour_url_zero_indexes_months():
    url = hour_url("EURUSD", dt.datetime(2026, 9, 15, 10, 0))
    assert "/2026/08/15/10h_ticks.bi5" in url


def test_parse_bi5_decodes_records_and_scales():
    hour_start = dt.datetime(2026, 9, 15, 10, 0, tzinfo=dt.UTC)
    records = [
        (1_500, 115_377, 115_373, 1.35, 7.2),
        (61_000, 115_378, 115_374, 2.25, 2.12),
    ]
    payload = lzma.compress(b"".join(struct.pack(">IIIff", *r) for r in records))
    df = parse_bi5(payload, hour_start, scale=100_000)
    assert df.height == 2
    assert df["ts"][0] == hour_start + dt.timedelta(milliseconds=1_500)
    assert df["ask"][0] == pytest.approx(1.15377)
    assert df["bid"][0] == pytest.approx(1.15373)
    assert df["ask_volume"][1] == pytest.approx(2.25)


def test_parse_bi5_empty_payload_is_empty_frame():
    df = parse_bi5(b"", dt.datetime(2026, 9, 15, 10, 0), scale=100_000)
    assert df.height == 0


def test_parse_bi5_rejects_bad_record_multiple():
    with pytest.raises(ValueError, match="multiple"):
        parse_bi5(
            lzma.compress(b"\x00" * 7),
            dt.datetime(2026, 9, 15, 10, 0, tzinfo=dt.UTC),
            scale=100_000,
        )


def test_parse_bi5_rejects_non_lzma():
    with pytest.raises(ValueError, match="LZMA"):
        parse_bi5(b"not lzma at all", dt.datetime(2026, 9, 15, 10, 0, tzinfo=dt.UTC), scale=100_000)


def test_fetch_hour_404_means_market_closed():
    import urllib.error

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", hdrs=None, fp=io.BytesIO())

    df = fetch_hour("EURUSD", dt.datetime(2026, 9, 15, 10, 0), opener=opener, attempts=1)
    assert df.height == 0


def test_fetch_hour_retries_then_succeeds():
    calls = {"n": 0}
    sleeps: list[float] = []

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    payload = lzma.compress(struct.pack(">IIIff", 100, 115377, 115373, 1.0, 1.0))

    def flaky(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("stall")
        return _Resp(payload)

    df = fetch_hour(
        "EURUSD", dt.datetime(2026, 9, 15, 10, 0), opener=flaky, sleep=sleeps.append, attempts=3
    )
    assert df.height == 1
    assert calls["n"] == 2
    assert sleeps == [5.0]


def test_iter_hours_bounds():
    hours = list(iter_hours(dt.datetime(2026, 9, 15, 10, 0), dt.datetime(2026, 9, 15, 13, 0)))
    assert len(hours) == 3


def test_sample_spread_pips_quote_level_and_stale_guard():
    start = dt.datetime(2026, 9, 15, 10, 0)
    rows = []
    quotes = [(1.10000, 1.10012), (1.10001, 1.10013), (1.10002, 1.10018), (1.10003, 1.10015)]
    offsets = [0, 3, 6, 36]  # 3 s gaps are fresh; the final 30 s gap is stale
    for (bid, ask), off in zip(quotes, offsets, strict=True):
        rows.append(
            {
                "ts": start + dt.timedelta(seconds=off),
                "bid": bid,
                "ask": ask,
                "bid_volume": 1.0,
                "ask_volume": 1.0,
                "sequence_gap": False,
            }
        )
    ticks = pl.DataFrame(rows).with_columns(pl.col("ts").dt.replace_time_zone("UTC"))
    samples = sample_spread_pips(ticks, pip_size=Decimal("0.0001"), max_interval_ms=5_000)
    # 0.00012 and 0.00016 price distance = 1.2 and 1.6 pips.
    assert [float(s) for s in samples] == pytest.approx([1.2, 1.6])


# ------------------------------------------------------------- gate1 core


def test_median_true_range_uses_complete_bars(synth_ticks):
    spec = _spec()
    stops = median_true_range(synth_ticks, timeframe="15m", spec=spec, window=200)
    assert stops.bars_used == 200
    assert 0 < stops.median_true_range_pips < 20
    assert stops["2.0x"] == pytest.approx(2 * stops.median_true_range_pips)
    assert set(stops.by_multiplier) == {"0.5x", "1.0x", "2.0x", "3.0x"}


def test_median_true_range_shrinks_window_with_disclosure(synth_ticks):
    """A short acquisition shrinks to what the data supports; bars_used says so."""
    stops = median_true_range(synth_ticks, timeframe="15m", spec=_spec(), window=10_000)
    assert stops.bars_used < 10_000
    assert stops.bars_used >= MIN_STOP_WINDOW


def test_median_true_range_fails_closed_below_floor():
    """Below the floor a median TR is regime noise, not a measurement."""
    # 4 hours of 30 s ticks -> 16 complete 15m bars -> window would shrink to 15.
    base = dt.datetime(2026, 9, 16, 0, 0, tzinfo=dt.UTC)
    rows = [
        {
            "ts": base + dt.timedelta(seconds=30 * i),
            "bid": 1.10000,
            "ask": 1.10002,
            "bid_volume": 1.0,
            "ask_volume": 1.0,
        }
        for i in range(480)
    ]
    short = pl.DataFrame(rows)
    with pytest.raises(ValueError, match="acquire more data"):
        median_true_range(short, timeframe="15m", spec=_spec(), window=10_000)


def test_cost_cell_conserves_the_budget():
    """c = (entry + exit + commission)/stop, all terms present."""
    from forex_research.costs.spread import SpreadModel

    entry_model = SpreadModel()
    for i in range(12):
        _add(entry_model, entry=True, spread=1.2 + 0.01 * i)
    exit_model = SpreadModel()
    for i in range(12):
        _add(exit_model, entry=False, spread=1.8 + 0.01 * i)

    class _Fee:
        commission_per_lot_round_trip = Decimal("6.00")

    spec = _spec()
    ticks = _synth_ticks(dt.datetime(2026, 9, 14, 6, 0), bars=5)
    cell = cost_cell(
        symbol="EURUSD",
        session="london",
        timeframe="15m@1.0x",
        stop_pips=10.0,
        spread_model=entry_model,
        stop_exit_model=exit_model,
        fee_schedule=_Fee(),
        spec=spec,
        ticks=ticks,
    )
    expected_entry = pytest.approx(1.31, abs=0.01)  # q95 of the 12-sample ramp
    assert cell.entry_spread_pips == expected_entry
    assert cell.round_trip_pips == pytest.approx(
        cell.entry_spread_pips + cell.stop_exit_spread_pips + cell.commission_pips
    )
    assert cell.c == pytest.approx(cell.round_trip_pips / 10.0)
    assert cell.cost_level == "exact"
    assert cell.entry_episodes == 12


def _add(model, *, entry: bool, spread: float):
    from forex_research.costs.spread import SpreadCellKey

    key = SpreadCellKey(
        symbol="EURUSD",
        session="london",
        volatility_bucket="normal",
        news_proximity=False,
        weekday=0,
    )
    if entry:
        model.add_episode(key, entry_spread_pips=spread)
    else:
        model.add_episode(key, entry_spread_pips=spread, stop_exit_spread_pips=spread)
    return key


def test_cost_cell_global_bound_blocks_the_gate():
    from forex_research.costs.spread import SpreadModel

    empty = SpreadModel()  # no episodes anywhere -> everything falls to the bound

    class _Fee:
        commission_per_lot_round_trip = Decimal("6.00")

    ticks = _synth_ticks(dt.datetime(2026, 9, 14, 6, 0), bars=5)
    cell = cost_cell(
        symbol="EURUSD",
        session="london",
        timeframe="15m@1.0x",
        stop_pips=10.0,
        spread_model=empty,
        stop_exit_model=SpreadModel(),
        fee_schedule=_Fee(),
        spec=_spec(),
        ticks=ticks,
    )
    assert cell.cost_level == "global_bound"


def test_build_matrix_shape_and_persistence(synth_ticks, tmp_path):
    class _Fee:
        commission_per_lot_round_trip = Decimal("6.00")

    cells = build_matrix(
        symbol="EURUSD",
        ticks=synth_ticks,
        spec=_spec(),
        fee_schedule=_Fee(),
        timeframes=[CandidateTimeframe(name="15m")],
        bars_window=100,
    )
    # 4 multipliers x 5 session buckets (incl. rollover)
    assert len(cells) == 20
    assert {c.session for c in cells} == {"asia", "london", "new_york", "overlap", "rollover"}

    record = Gate1Record(
        generated_utc="2026-09-21T00:00:00+00:00",
        capture_window_utc=["2026-09-14T00:00:00+00:00", "2026-09-21T00:00:00+00:00"],
        instruments=["EURUSD"],
        timeframes=["15m"],
        stop_multipliers=["0.5x", "1.0x", "2.0x", "3.0x"],
        threshold=0.25,
        cells=cells,
    )
    verdict = evaluate_gate(record)
    assert verdict["accepted"] is True
    # Wide stops pass, tight 0.5x stops fail: c = (1.0 + 1.0 + 0.6)/3.6 = 0.72.
    passing_tf = {c.timeframe for c in cells if c.c < record.threshold}
    assert "15m@3.0x" in passing_tf
    assert "15m@0.5x" not in passing_tf
    assert verdict["passing_cells"] == sum(1 for c in cells if c.c < record.threshold)
    out = save_record(record, tmp_path / "gate1" / "rec.json")
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["verdict"]["accepted"] is True
    assert len(loaded["cells"]) == 20


def test_evaluate_gate_fail_closed_on_global_bound():
    cells = [
        CostCell(
            symbol="EURUSD",
            session="london",
            timeframe="15m@1.0x",
            stop_pips=10.0,
            entry_spread_pips=1.0,
            stop_exit_spread_pips=1.0,
            commission_pips=0.6,
            round_trip_pips=2.6,
            c=0.26,
            cost_level="exact",
            entry_episodes=12,
            stop_exit_episodes=12,
        ),
        CostCell(
            symbol="EURUSD",
            session="rollover",
            timeframe="15m@1.0x",
            stop_pips=10.0,
            entry_spread_pips=5.0,
            stop_exit_spread_pips=5.0,
            commission_pips=0.6,
            round_trip_pips=10.6,
            c=1.06,
            cost_level="global_bound",
            entry_episodes=0,
            stop_exit_episodes=0,
        ),
    ]
    record = Gate1Record(
        generated_utc="2026-09-21T00:00:00+00:00",
        capture_window_utc=["a", "b"],
        instruments=["EURUSD"],
        timeframes=["15m"],
        stop_multipliers=["1.0x"],
        threshold=0.25,
        cells=cells,
    )
    verdict = evaluate_gate(record)
    assert verdict["accepted"] is False
    assert verdict["global_bound_cells"] == 1


def test_load_ticks_concatenates_partitions(synth_ticks, tmp_path):
    from forex_research.data.ticks import write_ticks_partition

    write_ticks_partition(tmp_path / "t", "EURUSD", "2026-09", synth_ticks.head(50))
    write_ticks_partition(tmp_path / "t", "EURUSD", "2026-10", synth_ticks.tail(50))
    ticks = load_ticks(tmp_path / "t", "EURUSD")
    assert ticks.height == 100
    assert ticks["ts"].is_sorted()


def test_fetch_range_caches_hours_and_reports_all_failures(synth_ticks, tmp_path):
    """Resume semantics: cached hours are not re-fetched; failures collect."""
    import datetime as _dt

    from forex_research.data.dukascopy import fetch_range

    start = _dt.datetime(2026, 9, 16, 8, 0, tzinfo=_dt.UTC)
    end = start + _dt.timedelta(hours=3)
    cache = tmp_path / "cache"
    calls: list[dt.datetime] = []
    payloads = {}

    def opener(req, timeout=None):
        url = req.full_url
        hour = int(url.rsplit("/", 1)[1].split("h")[0])
        calls.append(hour)
        if hour == 9:
            raise ConnectionError("simulated outage")
        payload = lzma.compress(struct.pack(">IIIff", 1_500, 115_377, 115_373, 1.0, 1.0))
        payloads[hour] = payload

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp(payload)

    with pytest.raises(ConnectionError, match="09:00Z"):
        fetch_range(
            "EURUSD",
            start,
            end,
            cache_dir=cache,
            workers=1,
            attempts=1,
            sleep=lambda *_: None,
            opener=opener,
        )
    assert (cache / "EURUSD_20260916T08.parquet").exists()
    assert not (cache / "EURUSD_20260916T09.parquet").exists()

    # second run: 08 and 10 succeeded last time and come from cache; only the
    # failed hour 09 is re-downloaded — resume costs only what actually failed.
    with pytest.raises(ConnectionError, match="09:00Z"):
        fetch_range(
            "EURUSD",
            start,
            end,
            cache_dir=cache,
            workers=1,
            attempts=1,
            sleep=lambda *_: None,
            opener=opener,
        )
    assert calls.count(8) == 1  # never refetched
    assert calls.count(10) == 1  # never refetched
    assert calls.count(9) == 2  # the failed hour is retried

    def healed(req, timeout=None):

        class _Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp(payloads[10])  # any payload decodes; hour key comes from the URL

    ticks = fetch_range(
        "EURUSD",
        start,
        end,
        cache_dir=cache,
        workers=1,
        attempts=1,
        sleep=lambda *_: None,
        opener=healed,
    )
    assert ticks.height == 3  # 08 cached + 09 + 10 healed
    assert ticks["ts"].is_sorted()


def test_cli_end_to_end(tmp_path, synth_ticks):
    """The CLI on pre-ingested ticks: full record, real verdict, no network."""
    import shutil
    from pathlib import Path

    from forex_research.data.ticks import write_ticks_partition

    # the CLI loads instruments/fees from its --repo; give it the real configs
    shutil.copytree(Path(__file__).resolve().parents[1] / "config", tmp_path / "config")
    write_ticks_partition(
        tmp_path / "data" / "raw" / "ticks_cleaned", "EURUSD", "2026-09", synth_ticks
    )
    from forex_research.gate1 import __main__ as cli

    code = cli.main(
        [
            "--symbols",
            "EURUSD",
            "--timeframes",
            "15m",
            "--bars-window",
            "100",
            "--repo",
            str(tmp_path),
            "--out",
            str(tmp_path / "gate1.json"),
        ]
    )
    assert code == 0
    record = json.loads((tmp_path / "gate1.json").read_text(encoding="utf-8"))
    assert record["verdict"]["accepted"] is True
    assert len(record["cells"]) == 20
