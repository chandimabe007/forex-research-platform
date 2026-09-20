"""Data layer — DATA-001..021 (resampling, validation, manifests, artefacts)."""

import datetime as dt
import json

import polars as pl
import pytest

from forex_research.data import (
    ValidationReport,
    check_bars,
    check_ticks,
    resample_ticks,
)
from forex_research.data.artifacts import (
    ArtefactUnavailable,
    FittedArtefact,
    UpdatePolicy,
    load_artefact,
    save_artefact,
)
from forex_research.data.manifest import write_manifest

UTC = dt.UTC


def _ticks(n: int = 240, *, start: dt.datetime | None = None, gap_at: int | None = None) -> pl.DataFrame:
    import numpy as np

    start = start or dt.datetime(2024, 1, 2, tzinfo=UTC)
    ts = pl.datetime_range(start, start + dt.timedelta(seconds=n - 1), interval="1s", eager=True)
    rng = np.random.default_rng(3)
    mid = 1.10 + np.cumsum(rng.normal(0, 1e-5, n))
    half = 0.0001
    df = pl.DataFrame(
        {
            "ts": ts,
            "bid": mid - half,
            "ask": mid + half,
            "bid_volume": [1.0] * n,
            "ask_volume": [1.0] * n,
            "sequence_gap": [False] * n,
        }
    )
    if gap_at is not None:
        # Insert a 90-second hole by shifting later ticks.
        shifted = df.with_columns(
            pl.when(pl.col("ts") >= ts[gap_at])
            .then(pl.col("ts") + dt.timedelta(seconds=90))
            .otherwise(pl.col("ts"))
            .alias("ts")
        ).sort("ts")
        return shifted
    return df


def test_resample_produces_both_sides_and_available_at():
    ticks = _ticks(240)
    bars = resample_ticks(ticks, timeframe="1m", continuity_threshold_ms=5000)
    assert bars.columns == [
        "ts_open", "available_at", "bid_open", "bid_high", "bid_low", "bid_close",
        "ask_open", "ask_high", "ask_low", "ask_close", "volume", "tick_count",
        "max_quote_gap_ms", "coverage_ok", "known_outage", "tick_derived",
    ]
    first = bars.row(0, named=True)
    assert first["available_at"] == first["ts_open"] + dt.timedelta(minutes=1)
    assert first["bid_close"] < first["ask_close"]


def test_gap_bar_has_poor_coverage():
    ticks = _ticks(240, gap_at=30)
    bars = resample_ticks(ticks, timeframe="1m", continuity_threshold_ms=5000)
    # The bar spanning the 90s hole must be marked coverage_ok=False.
    assert not bars["coverage_ok"].all()


def test_outage_overlap_marks_known_outage():
    ticks = _ticks(120)
    outages = [("2024-01-02T00:01:30+00:00", "2024-01-02T00:02:30+00:00")]
    bars = resample_ticks(ticks, timeframe="1m", continuity_threshold_ms=5000, outages=outages)
    assert bars["known_outage"].sum() >= 1


def test_validation_rejects_crossed_and_nonpositive():
    n = 10
    start = dt.datetime(2024, 1, 2, tzinfo=UTC)
    ts = pl.datetime_range(
        start, start + dt.timedelta(seconds=n - 1), interval="1s", eager=True
    )
    good = pl.DataFrame(
        {
            "ts": ts,
            "bid": [1.10] * n,
            "ask": [1.1001] * n,
            "bid_volume": [1.0] * n,
            "ask_volume": [1.0] * n,
            "sequence_gap": [False] * n,
        }
    )
    assert check_ticks(good).ok()

    crossed = good.with_columns(pl.col("bid").sub(0.001).alias("ask"))  # ask < bid
    rep = check_ticks(crossed)
    assert not rep.ok() and any("crossed" in f for f in rep.failures)

    nonpos = good.with_columns(pl.col("bid").mul(-1))
    rep = check_ticks(nonpos)
    assert not rep.ok() and any("non-positive" in f for f in rep.failures)


def _bars_frame(**overrides):
    n = 5
    start = dt.datetime(2024, 1, 2, tzinfo=UTC)
    ts = pl.datetime_range(
        start, start + dt.timedelta(hours=n - 1), interval="1h", eager=True
    )
    data = {
        "ts_open": ts,
        "available_at": ts + dt.timedelta(hours=1),
        "bid_open": [1.1] * n,
        "bid_high": [1.2] * n,
        "bid_low": [1.0] * n,
        "bid_close": [1.15] * n,
        "ask_open": [1.1] * n,
        "ask_high": [1.2] * n,
        "ask_low": [1.0] * n,
        "ask_close": [1.15] * n,
        "volume": [100] * n,
        "tick_count": [10] * n,
        "max_quote_gap_ms": [0] * n,
        "coverage_ok": [True] * n,
        "known_outage": [False] * n,
        "tick_derived": [True] * n,
    }
    data.update(overrides)
    return pl.DataFrame(data)


def test_bar_validation_rejects_incoherent_ohlc():
    bad = _bars_frame(bid_high=[0.9, 1.2, 1.2, 1.2, 1.2])
    rep = check_bars(bad, source="test")
    assert not rep.ok() and any("incoherent" in f for f in rep.failures)


def test_bar_validation_quarantines_weekend_and_flags_spikes():
    df = _bars_frame()
    # Force one ts onto a Sunday.
    ts = df["ts_open"].to_list()
    ts[0] = dt.datetime(2024, 1, 7, tzinfo=UTC)  # a Sunday
    df = df.with_columns(pl.Series("ts_open", ts))
    rep = check_bars(df, source="test")
    assert rep.quarantined and "weekend" in rep.quarantined[0]

    spike = _bars_frame(
        bid_open=[1.0] * 5,
        bid_close=[1.0, 1.0, 2.0, 1.0, 1.0],
        bid_high=[1.2, 1.2, 2.0, 1.2, 1.2],  # coherent: high covers the spike close
    )
    rep = check_bars(spike, source="test")
    assert rep.flags and "never auto-removed" in rep.flags[0]


def test_manifest_records_sha_and_validation(tmp_path):
    bars = _bars_frame()
    path = tmp_path / "bars.parquet"
    bars.write_parquet(path)

    from forex_research.data.manifest import sha256_file

    rep = ValidationReport(source="test")
    manifest_path = write_manifest(
        out_dir=tmp_path / "manifests",
        source="unit-test",
        symbol="EURUSD",
        timeframe="1h",
        date_from=dt.datetime(2024, 1, 2, tzinfo=UTC),
        date_to=dt.datetime(2024, 1, 2, 5, tzinfo=UTC),
        row_count=bars.height,
        gap_list=[],
        cleaned_path=path,
        validation=rep,
    )
    manifest = json.loads(manifest_path.read_text())
    assert manifest["cleaned_sha256"] == sha256_file(path)
    assert manifest["validation"]["ok"] is True
    assert manifest["symbol"] == "EURUSD"


def test_artefact_availability_enforced(tmp_path):
    frame = pl.DataFrame({"x": [1.0, 2.0]})
    calibrated_from = dt.datetime(2015, 1, 1, tzinfo=UTC)
    calibrated_to = dt.datetime(2016, 1, 1, tzinfo=UTC)
    available_from = dt.datetime(2026, 1, 1, tzinfo=UTC)
    artefact = FittedArtefact(
        name="gap_table",
        frame=frame,
        calibrated_from=calibrated_from,
        calibrated_to=calibrated_to,
        available_from=available_from,
        policy=UpdatePolicy.FROZEN,
    )
    save_artefact(artefact, tmp_path / "gap_table.parquet")
    loaded = load_artefact(tmp_path / "gap_table.parquet")

    # A read before available_from is refused (VAL-064).
    with pytest.raises(ArtefactUnavailable):
        loaded.read(at=dt.datetime(2015, 6, 1, tzinfo=UTC))
    # A read at or after availability is permitted.
    assert loaded.read(at=available_from).equals(frame)

    # An unstamped artefact is treated as unavailable (VAL-064).
    with pytest.raises(ArtefactUnavailable):
        FittedArtefact(
            name="scaler",
            frame=frame,
            calibrated_from=calibrated_from,
            calibrated_to=calibrated_to,
            available_from=None,
            policy=UpdatePolicy.FROZEN,
        )


def test_session_buckets_are_dst_aware():
    from forex_research.data.sessions import SessionResolver

    resolver = SessionResolver(venue_zone="UTC")
    # 13:00 UTC in July (BST+1, EDT+4): London 14:00 local -> active, NY 09:00 -> active.
    july = dt.datetime(2024, 7, 2, 13, 0, tzinfo=UTC)
    assert resolver.bucket(july) == "overlap"
    # 13:00 UTC in January (GMT+0, EST+5): London 13:00 -> active, NY 08:00 -> active.
    january = dt.datetime(2024, 1, 2, 13, 0, tzinfo=UTC)
    assert resolver.bucket(january) == "overlap"
    # 07:00 UTC in July: London 08:00 BST -> active; NY 03:00 -> inactive.
    early_july = dt.datetime(2024, 7, 2, 7, 0, tzinfo=UTC)
    assert resolver.bucket(early_july) == "london"
    # 07:00 UTC in January: London 07:00 GMT -> inactive; Tokyo 16:00 -> inactive.
    early_jan = dt.datetime(2024, 1, 2, 7, 0, tzinfo=UTC)
    assert resolver.bucket(early_jan) is None
