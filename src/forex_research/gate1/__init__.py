"""Gate 1 — cost feasibility (GATE-020..024, MILE-040)."""

from .analysis import (
    THRESHOLD,
    CandidateTimeframe,
    CostCell,
    Gate1Record,
    StopDistances,
    build_matrix,
    cost_cell,
    evaluate_gate,
    load_ticks,
    median_true_range,
    save_record,
    spread_model_from_ticks,
)

__all__ = [
    "THRESHOLD",
    "CandidateTimeframe",
    "CostCell",
    "Gate1Record",
    "StopDistances",
    "build_matrix",
    "cost_cell",
    "evaluate_gate",
    "load_ticks",
    "median_true_range",
    "save_record",
    "spread_model_from_ticks",
]
