"""Joins carry a staleness tolerance — FEAT-002.

Without ``tolerance``, a backward join matches the most recent available row
however old: a dropped partial bar, a weekend or a feed outage then attaches
a feature from days earlier and nothing complains. The tolerance here is
MANDATORY — there is no default. A feature too stale to use arrives as null
and the strategy treats it as ``NO_TRADE``, not as zero. ``shift(1)`` is not
a substitute — it is correct only while no bar is ever missing.
"""

from __future__ import annotations

import polars as pl


class MissingTolerance(TypeError):
    """A join was attempted without an explicit staleness tolerance (FEAT-002)."""


def join_asof(
    left: pl.DataFrame,
    right: pl.DataFrame,
    *,
    left_on: str,
    right_on: str,
    tolerance: str,
    columns: list[str] | None = None,
) -> pl.DataFrame:
    """Backward asof join with a mandatory tolerance."""
    if not tolerance:
        raise MissingTolerance(
            "FEAT-002: joins carry a staleness tolerance — pass one explicitly"
        )
    keep = [right_on] + (columns or [c for c in right.columns if c != right_on])
    right_sel = right.select(keep).sort(right_on)
    return left.sort(left_on).join_asof(
        right_sel,
        left_on=left_on,
        right_on=right_on,
        strategy="backward",
        tolerance=tolerance,
    )
