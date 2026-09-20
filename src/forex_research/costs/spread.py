"""Spread model — COST-001, COST-010, COST-012, COST-014.

A fixed-spread assumption is the single most common source of an edge that
exists only in the backtest (COST-001). Spreads are conditioned on
``(symbol, session_bucket, volatility_bucket, news_proximity, weekday)``
and drawn from the empirical distribution rather than its mean (COST-010).

Entry and stop-exit spread are separate models (COST-012): a stop is a
market order in a fast market, which is when spread is widest; calibrate
stop-exit spread conditioned on high realised range.

Sparse cells fall back hierarchically and every quantile reports its
effective episode count (COST-014) — a quantile computed from eight
episodes is reported as such, not as a number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from random import Random

from ..data.sessions import SESSIONS


@dataclass(frozen=True)
class SpreadCellKey:
    symbol: str
    session: str  # one of SESSIONS or "unknown"
    volatility_bucket: str  # low | normal | high
    news_proximity: bool
    weekday: int  # 0=Monday


@dataclass
class SpreadCell:
    samples_pips: list[float]
    kind: str  # entry | stop_exit

    @property
    def episode_count(self) -> int:
        return len(self.samples_pips)

    def quantile(self, q: float) -> float | None:
        """Nearest-rank quantile (ceil(q*n)-1): the smallest order statistic
        that leaves at least a fraction ``q`` of samples at or below it.
        Conservative direction for a cost model (ARCH-001)."""
        if not self.samples_pips:
            return None
        ordered = sorted(self.samples_pips)
        idx = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
        return ordered[idx]


# COST-014 hierarchical fallback order.
FALLBACK_CHAIN = (
    ("symbol", "session", "volatility", "news", "weekday"),
    ("symbol", "session", "volatility"),
    ("symbol", "session"),
    ("symbol",),
    (),  # conservative global bound
)


@dataclass
class SpreadModel:
    """Empirical conditional spread distributions with declared fallbacks."""

    entry_cells: dict[SpreadCellKey, SpreadCell] = field(default_factory=dict)
    stop_exit_cells: dict[SpreadCellKey, SpreadCell] = field(default_factory=dict)
    global_bound_pips: float = 5.0  # conservative global bound (fallback terminal)
    min_episodes: int = 8  # COST-013 minimum independent episodes per bucket

    def add_episode(
        self,
        key: SpreadCellKey,
        *,
        entry_spread_pips: float,
        stop_exit_spread_pips: float | None = None,
    ) -> None:
        self.entry_cells.setdefault(key, SpreadCell([], "entry")).samples_pips.append(
            entry_spread_pips
        )
        if stop_exit_spread_pips is not None:
            self.stop_exit_cells.setdefault(
                key, SpreadCell([], "stop_exit")
            ).samples_pips.append(stop_exit_spread_pips)

    def _lookup(self, cells: dict[SpreadCellKey, SpreadCell], key: SpreadCellKey) -> tuple[SpreadCell | None, str]:
        """Walk the fallback chain; return (cell, level_description).

        The chain's terminal entry is the conservative global bound, reached
        only when no level matched — never by pooling every cell together.
        """
        for level in FALLBACK_CHAIN[:-1]:
            masked = SpreadCellKey(
                symbol=key.symbol if "symbol" in level else "*",
                session=key.session if "session" in level else "*",
                volatility_bucket=key.volatility_bucket if "volatility" in level else "*",
                news_proximity=key.news_proximity if "news" in level else False,
                weekday=key.weekday if "weekday" in level else -1,
            )
            matches = [
                cell
                for k, cell in cells.items()
                if (k.symbol == masked.symbol or masked.symbol == "*")
                and (k.session == masked.session or masked.session == "*")
                and (
                    k.volatility_bucket == masked.volatility_bucket
                    or masked.volatility_bucket == "*"
                )
                and (k.weekday == masked.weekday or masked.weekday == -1)
                # "news" masked out (not in level) matches any news flag; when
                # the level includes "news", the match is exact. The wildcard
                # cannot be inferred from the value: False is also a real
                # value of the field.
                and (
                    "news" not in level
                    or k.news_proximity == key.news_proximity
                )
            ]
            if matches:
                # Aggregate samples across matching cells at this level.
                pooled = SpreadCell(
                    [s for m in matches for s in m.samples_pips], kind=matches[0].kind
                )
                if pooled.episode_count >= self.min_episodes:
                    return pooled, "+".join(level)
        return None, "global_bound"

    def draw(
        self,
        key: SpreadCellKey,
        *,
        kind: str = "entry",
        rng: Random | None = None,
    ) -> tuple[Decimal, dict]:
        """Draw a spread from the empirical distribution (not the mean).

        Returns (spread_pips, provenance) where provenance names the fallback
        level and the effective episode count (COST-014).
        """
        cells = self.entry_cells if kind == "entry" else self.stop_exit_cells
        cell, level = self._lookup(cells, key)
        if cell is None:
            # Conservative global bound: the widest defensible figure.
            provenance = {"level": "global_bound", "episodes": 0, "kind": kind}
            return Decimal(str(self.global_bound_pips)), provenance
        rng = rng or Random()
        sample = rng.choice(cell.samples_pips)
        provenance = {
            "level": level,
            "episodes": cell.episode_count,
            "kind": kind,
            "q95": cell.quantile(0.95),
        }
        return Decimal(str(sample)), provenance

    def stop_exit_draw(self, key: SpreadCellKey, *, rng: Random | None = None) -> tuple[Decimal, dict]:
        """COST-012: stop exits use their own model, conditioned on stress."""
        return self.draw(key, kind="stop_exit", rng=rng)


def volatility_bucket(realised_range_atr: float | None) -> str:
    """Bucket realised range for the COST-010 conditioning key."""
    if realised_range_atr is None:
        return "normal"
    if realised_range_atr < 0.5:
        return "low"
    if realised_range_atr > 1.5:
        return "high"
    return "normal"


def session_bucket(ts, resolver) -> str:
    bucket = resolver.bucket(ts)
    return bucket if bucket in SESSIONS else "unknown"
