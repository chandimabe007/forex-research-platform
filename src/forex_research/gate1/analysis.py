"""Gate 1 measurement — GATE-020/021 with COST-014 uncertainty.

Numerator and denominator are measured on one basis (GATE-021):

- **Cost** (round trip, pips): entry spread + stop-exit spread, both taken at
  the conservative quantile through the COST-014 hierarchical fallback over
  venue-captured episodes, plus round-trip commission converted to pips at
  the pip value derived from DATA-002. COST-014's rule that a sparse cell is
  *reported with its effective episode count* is enforced structurally: every
  number carries one, and a cell that fell back all the way to the
  conservative global bound is labelled ``global_bound`` so no fallback
  number can pass as a measurement (MILE-040: decisions must hold at the
  upper fallback bound).
- **Stop distance** (pips): the median true range over the trailing window of
  coverage-complete, tick-derived bars — a real price path, not a constant.
  The 0.5x/1x/2x/3x multipliers sweep the denominator outward; widening
  stops is MILE-040's first listed remedy.
- ``c = round_trip / stop``; the gate accepts ``c < 0.25`` (GATE-022, one
  threshold at every timeframe).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

import polars as pl

from ..costs.spread import SpreadCellKey, SpreadModel, session_bucket
from ..data.instruments import InstrumentSpec
from ..data.sessions import SESSIONS, SessionResolver

THRESHOLD = 0.25  # GATE-022: declared configuration value, one for all timeframes
EXACT_LEVEL = "symbol+session+volatility+news+weekday"
# A shrunken stop window below this is regime noise, not a measurement —
# fail closed rather than publish a median of a handful of bars.
MIN_STOP_WINDOW = 20
# SESSIONS includes "rollover": cells are keyed by session (COST-010), so each
# row measures its own session's quotes — the rollover row existing and being
# terrible *is* the table's advice against trading there.


@dataclass(frozen=True)
class CandidateTimeframe:
    name: str  # resample timeframe label, e.g. "M15"


@dataclass(frozen=True)
class StopDistances:
    """ATR-style stop distances in pips per multiplier (GATE-021 denominator)."""

    median_true_range_pips: float
    bars_used: int
    by_multiplier: dict[str, float]

    def __getitem__(self, key: str) -> float:
        return self.by_multiplier[key]


@dataclass
class CostCell:
    """One (symbol, session, timeframe, stop) cost measurement, provenanced."""

    symbol: str
    session: str
    timeframe: str
    stop_pips: float
    entry_spread_pips: float
    stop_exit_spread_pips: float
    commission_pips: float
    round_trip_pips: float
    c: float
    cost_level: str  # exact | fallback:<levels> | global_bound
    entry_episodes: int
    stop_exit_episodes: int


@dataclass
class Gate1Record:
    """The persisted artifact: the whole table plus the verdict (GATE-024)."""

    generated_utc: str
    capture_window_utc: list[str]
    instruments: list[str]
    timeframes: list[str]
    stop_multipliers: list[str]
    threshold: float
    cells: list[CostCell] = field(default_factory=list)
    verdict: dict | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = asdict(self)
        out["cells"] = [asdict(c) for c in self.cells]
        return out


def load_ticks(root: Path, symbol: str) -> pl.DataFrame:
    """Concatenate every cleaned tick partition for a symbol (symbol-month)."""
    frames = []
    for path in sorted((Path(root) / symbol).glob("*.parquet")):
        frames.append(pl.read_parquet(path))
    if not frames:
        raise FileNotFoundError(f"no cleaned tick partitions under {Path(root) / symbol}")
    return pl.concat(frames).sort("ts")


def median_true_range(
    ticks: pl.DataFrame, *, timeframe: str, spec: InstrumentSpec, window: int
) -> StopDistances:
    """Median true range over the last ``window`` coverage-complete bars.

    True range from bid closes: max(h-l, |h-prev_c|, |l-prev_c|). A short
    window is deliberate — Gate 1 wants the range the current regime trades,
    not a multi-year blend. Incomplete bars (coverage_ok false) are excluded:
    a feed gap must not masquerade as a tight range (DATA-013).
    """
    from ..data.resample import resample_ticks

    bars = resample_ticks(ticks, timeframe=timeframe, continuity_threshold_ms=60_000)
    bars = bars.filter(pl.col("coverage_ok"))
    complete = bars.height
    if complete < window + 1:
        # A 6-day acquisition cannot fill a 200-bar hour window. COST-014's
        # rule is disclosure over failure: shrink to what the data supports
        # (the returned ``bars_used`` states it) — but never below the floor,
        # where a median TR is regime noise rather than a measurement.
        window = complete - 1
        if window < MIN_STOP_WINDOW:
            raise ValueError(
                f"{timeframe}: {complete} complete bars is below the minimum "
                f"stop window ({MIN_STOP_WINDOW} + 1) — acquire more data "
                "before running Gate 1"
            )
    high = bars["bid_high"].to_list()
    low = bars["bid_low"].to_list()
    prev_close = [None] + high[:-1]
    tr_pips = []
    for h, low_i, pc in zip(high, low, prev_close, strict=True):
        tr = h - low_i
        if pc is not None:
            tr = max(tr, abs(h - pc), abs(low_i - pc))
        tr_pips.append(float(spec.price_to_pips(Decimal(repr(tr)))))
    tail = sorted(tr_pips[-window:])
    median = tail[len(tail) // 2]
    by_mult = {f"{m:.1f}x": median * m for m in (0.5, 1.0, 2.0, 3.0)}
    return StopDistances(median_true_range_pips=median, bars_used=window, by_multiplier=by_mult)


def spread_model_from_ticks(
    ticks: pl.DataFrame,
    symbol: str,
    *,
    spec: InstrumentSpec,
    resolver: SessionResolver,
    max_interval_ms: int = 120_000,
) -> SpreadModel:
    """Entry-spread episodes from venue capture, keyed COST-010/COST-011.

    One spread sample per tick (quote-level, COST-013), filed under its own
    session bucket — no cross-session pooling unless a cell is empty and the
    COST-014 fallback chain handles it.
    """
    from ..data.dukascopy import sample_spread_pips

    model = SpreadModel()
    ts = ticks["ts"].to_list()
    buckets = [session_bucket(t, resolver) for t in ts]
    pips = sample_spread_pips(ticks, pip_size=spec.pip_size, max_interval_ms=max_interval_ms)
    for i, spread in enumerate(pips):
        bucket = buckets[i + 1]  # sample i measures the spread at tick i+1
        if bucket not in SESSIONS:
            continue
        key = SpreadCellKey(
            symbol=symbol,
            session=bucket,
            volatility_bucket="normal",
            news_proximity=False,
            weekday=ts[i + 1].weekday(),
        )
        model.add_episode(key, entry_spread_pips=float(spread))
    return model


def _stop_exit_samples_from_worst_hour(
    ticks: pl.DataFrame, symbol: str, *, spec: InstrumentSpec, resolver: SessionResolver
) -> SpreadModel:
    """Seed stop-exit cells from each session's most stressed quarter-hour.

    COST-012 conditions stop-exit spread on stress; the venue capture has no
    stop events, so the honest proxy is the widest spreads observed in each
    session, not its average. This is a *lower bound on stress* and is
    reported as such in the record.
    """
    from ..data.dukascopy import sample_spread_pips

    model = SpreadModel()
    ts = ticks["ts"].to_list()
    buckets = [session_bucket(t, resolver) for t in ts]
    pips = sample_spread_pips(ticks, pip_size=spec.pip_size, max_interval_ms=120_000)
    per_session: dict[str, list[tuple[float, int]]] = {}
    for i, spread in enumerate(pips):
        bucket = buckets[i + 1]
        if bucket not in SESSIONS:
            continue
        per_session.setdefault(bucket, []).append((float(spread), i + 1))
    for bucket, samples in per_session.items():
        worst = sorted(samples, reverse=True)[: max(1, len(samples) // 4)]
        for spread, idx in worst:
            key = SpreadCellKey(
                symbol=symbol,
                session=bucket,
                volatility_bucket="normal",
                news_proximity=False,
                weekday=ts[idx].weekday(),
            )
            model.add_episode(key, entry_spread_pips=spread, stop_exit_spread_pips=spread)
    return model


def _pip_value_in_usd(spec: InstrumentSpec, ticks: pl.DataFrame) -> Decimal:
    """Pip value per lot in USD (DATA-002 bridge).

    USD-quoted symbols: pip_size * contract_size. JPY-quoted: divided by the
    observed USDJPY price from the same capture window (the capture is the
    available rate; using it is recorded in the record, not hidden).
    """
    quote = spec.quote_currency.upper()
    if quote == "USD":
        return spec.pip_size * spec.contract_size
    if quote == "JPY":
        usdjpy = _last_usdjpy_price(ticks if spec.symbol == "USDJPY" else None)
        if usdjpy is None:
            raise ValueError(
                f"{spec.symbol} is JPY-quoted: a USDJPY rate is required to express "
                "commission in pips — capture or supply USDJPY in the same window"
            )
        return spec.pip_size * spec.contract_size / Decimal(repr(usdjpy))
    raise ValueError(f"no USD conversion rule for quote currency {quote!r} (fail closed)")


def _last_usdjpy_price(ticks: pl.DataFrame | None) -> float | None:
    if ticks is None or ticks.height == 0:
        return None
    return float(ticks["bid"][-1])


def _commission_pips(fee_schedule, spec: InstrumentSpec, ticks: pl.DataFrame) -> float:
    """Round-trip commission expressed in pips at the pip value per lot."""
    pip_value_usd = _pip_value_in_usd(spec, ticks)
    commission = Decimal(str(fee_schedule.commission_per_lot_round_trip))
    return float(commission / pip_value_usd)


def _quantile_via_fallback(model: SpreadModel, key: SpreadCellKey, kind: str, q: float):
    """(quantile, provenance) through the COST-014 fallback chain."""
    cells = model.entry_cells if kind == "entry" else model.stop_exit_cells
    cell, level = model._lookup(cells, key)
    if cell is None:
        return None, {"level": "global_bound", "episodes": 0}
    value = cell.quantile(q)
    if value is None:
        return None, {"level": "global_bound", "episodes": 0}
    return value, {"level": level, "episodes": cell.episode_count}


def cost_cell(
    *,
    symbol: str,
    session: str,
    timeframe: str,
    stop_pips: float,
    spread_model: SpreadModel,
    stop_exit_model: SpreadModel,
    fee_schedule,
    spec: InstrumentSpec,
    ticks: pl.DataFrame,
    quantile: float = 0.95,
) -> CostCell:
    """One c measurement: conservative spreads + commission over the stop."""
    key = SpreadCellKey(
        symbol=symbol,
        session=session,
        volatility_bucket="normal",
        news_proximity=False,
        weekday=0,
    )
    entry_pips, entry_prov = _quantile_via_fallback(spread_model, key, "entry", quantile)
    exit_pips, exit_prov = _quantile_via_fallback(stop_exit_model, key, "stop_exit", quantile)
    if entry_pips is None or exit_pips is None:
        level = "global_bound"
        entry_pips = entry_pips if entry_pips is not None else float(spread_model.global_bound_pips)
        exit_pips = exit_pips if exit_pips is not None else float(spread_model.global_bound_pips)
    else:
        levels = {entry_prov["level"], exit_prov["level"]}
        level = "exact" if levels <= {EXACT_LEVEL} else "fallback:" + "|".join(sorted(levels))
    commission = _commission_pips(fee_schedule, spec, ticks)
    round_trip = entry_pips + exit_pips + commission
    return CostCell(
        symbol=symbol,
        session=session,
        timeframe=timeframe,
        stop_pips=stop_pips,
        entry_spread_pips=entry_pips,
        stop_exit_spread_pips=exit_pips,
        commission_pips=commission,
        round_trip_pips=round_trip,
        c=round_trip / stop_pips,
        cost_level=level,
        entry_episodes=entry_prov.get("episodes", 0),
        stop_exit_episodes=exit_prov.get("episodes", 0),
    )


def build_matrix(
    *,
    symbol: str,
    ticks: pl.DataFrame,
    spec: InstrumentSpec,
    fee_schedule,
    timeframes: list[CandidateTimeframe],
    bars_window: int = 200,
    quantile: float = 0.95,
) -> list[CostCell]:
    """The full (timeframe × stop-multiplier × session) table for one symbol."""
    resolver = SessionResolver()
    entry_model = spread_model_from_ticks(ticks, symbol, spec=spec, resolver=resolver)
    exit_model = _stop_exit_samples_from_worst_hour(ticks, symbol, spec=spec, resolver=resolver)
    cells: list[CostCell] = []
    for tf in timeframes:
        stops = median_true_range(ticks, timeframe=tf.name, spec=spec, window=bars_window)
        for mult_key, stop_pips in stops.by_multiplier.items():
            for session in SESSIONS:
                cells.append(
                    cost_cell(
                        symbol=symbol,
                        session=session,
                        timeframe=f"{tf.name}@{mult_key}",
                        stop_pips=stop_pips,
                        spread_model=entry_model,
                        stop_exit_model=exit_model,
                        fee_schedule=fee_schedule,
                        spec=spec,
                        ticks=ticks,
                        quantile=quantile,
                    )
                )
    return cells


def evaluate_gate(record: Gate1Record) -> dict:
    """GATE-024 verdict. Fail-closed: any global_bound cell blocks acceptance."""
    clean = [c for c in record.cells if c.cost_level != "global_bound"]
    poisoned = [c for c in record.cells if c.cost_level == "global_bound"]
    passing = [c for c in clean if c.c < record.threshold]
    best = min(clean, key=lambda c: c.c, default=None)
    verdict = {
        "accepted": bool(passing) and not poisoned,
        "passing_cells": len(passing),
        "global_bound_cells": len(poisoned),
        "best_c": best.c if best else None,
        "best_cell": f"{best.symbol}/{best.session}/{best.timeframe}" if best else None,
        "threshold": record.threshold,
        "note": (
            "accepted only where cells measured on real episodes; decisions must "
            "also hold at upper fallback bounds (MILE-040)"
            if not poisoned
            else "global-bound fallback present: capture more venue episodes before accepting"
        ),
    }
    record.verdict = verdict
    return verdict


def save_record(record: Gate1Record, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_dict(), indent=2), encoding="utf-8")
    return path
