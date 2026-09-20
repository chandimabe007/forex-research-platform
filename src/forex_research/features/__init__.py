"""Feature engine and look-ahead enforcement (FEAT-*, VAL-060..065)."""

from .engine import FeatureEngine
from .frames import MissingTolerance, join_asof
from .leaks import LEAKING_PIPELINES, base_bars, run_truncation_test
from .library import FEATURE_COLUMNS, FeatureParams, compute_features
from .registry import (
    FeatureRegistration,
    coverage_gaps,
    register,
    register_library_features,
    registry,
)

__all__ = [
    "FEATURE_COLUMNS",
    "FeatureEngine",
    "FeatureParams",
    "FeatureRegistration",
    "LEAKING_PIPELINES",
    "MissingTolerance",
    "base_bars",
    "compute_features",
    "coverage_gaps",
    "join_asof",
    "register",
    "register_library_features",
    "registry",
    "run_truncation_test",
]
