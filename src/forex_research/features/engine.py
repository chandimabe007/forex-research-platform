"""Feature engine — FEAT-001..FEAT-010 facade.

Every feature is indexed by ``available_at`` (FEAT-001): a bar labelled ``T``
on timeframe ``D`` closes at ``T + D``; anything computed from it is
available at ``T + D`` and not before. The engine composes the library,
attaches the DST-aware session regime, and emits exactly the registered
columns.
"""

from __future__ import annotations

import polars as pl

from ..data.sessions import SessionResolver
from .library import FeatureParams, compute_features


class FeatureEngine:
    def __init__(
        self,
        *,
        params: FeatureParams | None = None,
        session_resolver: SessionResolver | None = None,
    ) -> None:
        self.params = params or FeatureParams()
        self.session_resolver = session_resolver

    def compute(self, bars: pl.DataFrame) -> pl.DataFrame:
        """Compute features from closed DATA-003 bars.

        Incomplete bars (``coverage_ok`` false or ``known_outage`` true)
        generate no signals and are excluded from feature computation
        (DATA-013) — they arrive as nulls downstream, which the strategy
        treats as NO_TRADE, never as zero.
        """
        usable = bars.filter(pl.col("coverage_ok") & ~pl.col("known_outage"))
        features = compute_features(usable, params=self.params)
        if self.session_resolver is not None:
            sessions = compute_session_regime(usable, resolver=self.session_resolver)
            features = features.join(sessions, on="available_at", how="left")
        return features


def compute_session_regime(bars: pl.DataFrame, *, resolver: SessionResolver) -> pl.DataFrame:
    buckets = [resolver.bucket(ts) for ts in bars["available_at"].to_list()]
    return pl.DataFrame(
        {
            "available_at": bars["available_at"],
            "session_regime": buckets,
        },
        schema={"available_at": pl.Datetime("us", time_zone="UTC"), "session_regime": pl.String},
    )
