"""Feature registry — FEAT-005, VAL-062.

Features are registered with a version. Changing a computation increments it
and invalidates cached outputs and every ledger entry citing it.
Registration increments the **audit count**; it contributes to the
statistical candidate set only if it produced a return series (VAL-045).

The coverage registry (VAL-062) fails if any registered feature has no
truncation case.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

from .leaks import base_bars, run_truncation_test
from .library import FEATURE_COLUMNS, FeatureParams, compute_features

UTC = dt.UTC
_START = dt.datetime(2024, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class FeatureRegistration:
    name: str
    version: int
    column: str
    produces_return_series: bool  # VAL-045: candidate-set membership
    truncation_case: Callable[[], bool]  # the VAL-060 case for this feature


_REGISTRY: dict[str, FeatureRegistration] = {}


def register(reg: FeatureRegistration) -> None:
    _REGISTRY[reg.name] = reg


def registry() -> dict[str, FeatureRegistration]:
    return dict(_REGISTRY)


def coverage_gaps() -> list[str]:
    """VAL-062: every registered feature must have a truncation case."""
    return [name for name, reg in _REGISTRY.items() if reg.truncation_case is None]


def _honest_case() -> bool:
    """Truncation case for every library feature: run the full pipeline over
    boundary cutoffs (interior, a window edge, the final row)."""
    bars = base_bars(n=400)
    inputs = {"bars": bars}
    params = FeatureParams(trend_window=20, vol_regime_lookback=60)
    for cutoff_minute in (120, 253, 399):
        cutoff = _START + dt.timedelta(minutes=cutoff_minute)
        if not run_truncation_test(
            lambda i: compute_features(i["bars"], params=params),
            inputs,
            cutoff,
            warmup=60,
        ):
            return False
    return True


def register_library_features() -> None:
    """Register every library feature with its truncation case."""
    for column in FEATURE_COLUMNS:
        register(
            FeatureRegistration(
                name=column,
                version=1,
                column=column,
                produces_return_series=(column == "ret_1"),
                truncation_case=_honest_case,
            )
        )
