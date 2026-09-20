"""Cost model — COST-001..018: spread distributions, the one fill equation,
commission/swap, sensitivity, and the two backtest modes."""

from .commission import TRIPLE_SWAP_WEEKDAY_DEFAULT, FeeSchedule
from .fill import FillEngine, FillReport, ZeroSlippageFixtureError
from .sensitivity import MULTIPLIERS, SensitivityPoint, SensitivitySurface, run_sensitivity
from .spread import (
    FALLBACK_CHAIN,
    SpreadCell,
    SpreadCellKey,
    SpreadModel,
    session_bucket,
    volatility_bucket,
)

__all__ = [
    "FALLBACK_CHAIN",
    "MULTIPLIERS",
    "TRIPLE_SWAP_WEEKDAY_DEFAULT",
    "FeeSchedule",
    "FillEngine",
    "FillReport",
    "SensitivityPoint",
    "SensitivitySurface",
    "SpreadCell",
    "SpreadCellKey",
    "SpreadModel",
    "ZeroSlippageFixtureError",
    "run_sensitivity",
    "session_bucket",
    "volatility_bucket",
]
