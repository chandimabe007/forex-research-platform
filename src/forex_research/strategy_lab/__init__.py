"""Strategy lab — screening-tier backtests (see engine.py for the contract).

The optimizer treats every result it records as one "trial" in the VAL-045
ledger sense: same selection criterion, same fill mode, honest N.
"""

from .engine import (
    MAX_COST_RATIO,
    VOLUME_STEP,
    BarCtx,
    Entry,
    LabResult,
    LabStrategy,
    LabTrade,
    M1Lab,
    hour_session_utc,
    pip_value_usd,
    session_spreads,
)
from .strategies import STRATEGIES, make_strategy

__all__ = [
    "MAX_COST_RATIO",
    "VOLUME_STEP",
    "BarCtx",
    "Entry",
    "LabResult",
    "LabStrategy",
    "LabTrade",
    "M1Lab",
    "STRATEGIES",
    "make_strategy",
    "hour_session_utc",
    "pip_value_usd",
    "session_spreads",
]
