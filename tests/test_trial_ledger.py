"""VAL-045 trial ledger tests — chain integrity, comparability, the VAL-040 feed."""

from __future__ import annotations

import json
import math
import random

import pytest

from forex_research.validation.deflated_sharpe import (
    dsr_decision,
    hac_se_of_sr,
    sr0_modelled,
)
from forex_research.validation.trial_ledger import TrialLedger

BASE_KWARGS = dict(
    candidate_id="ma_cross_v1",
    feature_version="f3",
    selection_criterion="highest_sr_after_costs",
    fill_mode="exact",
    rule="STRAT-010",
    sr_periodic=0.12,
    n_observations=500,
)


def _append(ledger: TrialLedger, trial_id: str, **overrides):
    kwargs = dict(BASE_KWARGS)
    kwargs.update(overrides)
    return ledger.append(
        ledger.new_record(trial_id=trial_id, created_utc="2026-09-21T00:00:00+00:00", **kwargs)
    )


def test_append_and_read_back_round_trips(tmp_path):
    ledger = TrialLedger(tmp_path / "ledger.jsonl")
    stored = _append(ledger, "a")
    _append(ledger, "b", sr_periodic=-0.05)  # a failed trial: kept, the denominator
    entries = ledger.entries()
    assert [e.trial_id for e in entries] == ["a", "b"]
    assert entries[1].prev_hash == stored.record_hash
    assert entries[0].prev_hash == "0" * 64
    assert ledger.audit_count() == 2


def test_append_chains_new_records_to_the_tail(tmp_path):
    ledger = TrialLedger(tmp_path / "ledger.jsonl")
    first = _append(ledger, "a")
    second = _append(ledger, "b")
    assert first.prev_hash == "0" * 64
    assert second.prev_hash == first.record_hash
    assert ledger.verify_chain() == []


def test_append_refuses_invalid_records(tmp_path):
    ledger = TrialLedger(tmp_path / "ledger.jsonl")
    with pytest.raises(ValueError, match="candidate_id"):
        _append(ledger, "x", candidate_id="  ")
    with pytest.raises(ValueError, match="selection_criterion"):
        _append(ledger, "x", selection_criterion="")
    with pytest.raises(ValueError, match="n_observations"):
        _append(ledger, "x", n_observations=1)
    with pytest.raises(ValueError, match="finite"):
        _append(ledger, "x", sr_periodic=float("nan"))
    with pytest.raises(ValueError, match="feature_version"):
        _append(ledger, "x", feature_version="")
    assert ledger.audit_count() == 0  # nothing half-written


def test_verify_chain_detects_edited_contents(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = TrialLedger(path)
    _append(ledger, "a", sr_periodic=0.01)
    _append(ledger, "b", sr_periodic=0.02)
    # Survivorship edit: bump a trial's Sharpe after the fact.
    lines = path.read_text(encoding="utf-8").splitlines()
    doctored = json.loads(lines[0])
    doctored["sr_periodic"] = 0.99
    lines[0] = json.dumps(doctored, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    errors = ledger.verify_chain()
    assert any("record_hash mismatch" in e for e in errors)


def test_verify_chain_detects_deletion(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = TrialLedger(path)
    for tid in ("a", "b", "c"):
        _append(ledger, tid)
    lines = path.read_text(encoding="utf-8").splitlines()
    del lines[1]  # remove the failed middle trial — the survivorship attack
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    errors = ledger.verify_chain()
    assert any("prev_hash does not match" in e for e in errors)


def test_verify_chain_detects_reordering(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = TrialLedger(path)
    for tid in ("a", "b", "c"):
        _append(ledger, tid)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[0], lines[1] = lines[1], lines[0]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert ledger.verify_chain()  # non-empty: disorder detected


def test_verify_chain_passes_virgin_and_intact_ledgers(tmp_path):
    ledger = TrialLedger(tmp_path / "missing.jsonl")
    assert ledger.verify_chain() == []
    for tid in ("a", "b"):
        _append(ledger, tid)
    assert ledger.verify_chain() == []


def test_statistical_candidate_set_filters_by_comparability(tmp_path):
    ledger = TrialLedger(tmp_path / "ledger.jsonl")
    _append(ledger, "in_1", sr_periodic=0.10)
    _append(ledger, "in_2", sr_periodic=0.14)
    _append(ledger, "other_crit", sr_periodic=0.50, selection_criterion="highest_hit_rate")
    _append(ledger, "approx_fills", sr_periodic=0.60, fill_mode="approximate")
    _append(ledger, "old_features", sr_periodic=0.70, feature_version="f2")
    subset = ledger.statistical_candidate_set(
        selection_criterion="highest_sr_after_costs", fill_mode="exact", feature_version="f3"
    )
    assert [e.trial_id for e in subset] == ["in_1", "in_2"]
    # Without the version pin: same criterion + fill mode across versions.
    unpinned = ledger.statistical_candidate_set(
        selection_criterion="highest_sr_after_costs", fill_mode="exact"
    )
    assert [e.trial_id for e in unpinned] == ["in_1", "in_2", "old_features"]
    assert ledger.audit_count() == 5  # the audit count keeps everything


def test_s_ledger_requires_two_comparable_trials_and_points_to_val041(tmp_path):
    ledger = TrialLedger(tmp_path / "ledger.jsonl")
    _append(ledger, "only", sr_periodic=0.2)
    with pytest.raises(ValueError, match="sr0_modelled"):
        ledger.s_ledger(selection_criterion="highest_sr_after_costs", fill_mode="exact")


def test_sr0_from_real_trials_feeds_val040_end_to_end(tmp_path):
    """The full feed: real trials -> s_ledger -> hurdle -> decision."""
    ledger = TrialLedger(tmp_path / "ledger.jsonl")
    values = [0.05, -0.02, 0.08, 0.11, -0.06, 0.03]
    for i, sr in enumerate(values):
        _append(ledger, f"t{i}", sr_periodic=sr)
    hurdle = ledger.sr0(selection_criterion="highest_sr_after_costs", fill_mode="exact")
    assert hurdle.route == "empirical"
    assert hurdle.n_trials == 6
    mean = sum(values) / 6
    s = math.sqrt(sum((v - mean) ** 2 for v in values) / 5)
    assert hurdle.value == pytest.approx(s * 1.2672063606114647, rel=1e-9)  # E[max of 6]
    # The decision consumes the ledger's hurdle directly, with sr_hat estimated
    # from the same returns the SE comes from (seeded: deterministic).
    rng = random.Random(11)
    winner_returns = [rng.gauss(mu=0.0008, sigma=0.002) for _ in range(400)]
    mean = sum(winner_returns) / len(winner_returns)
    var = sum((r - mean) ** 2 for r in winner_returns) / (len(winner_returns) - 1)
    sr_hat = mean / math.sqrt(var)
    se = hac_se_of_sr(winner_returns)
    decision = dsr_decision(sr_hat=sr_hat, sr0=hurdle, se=se.value, se_method=se.method)
    assert decision.accepted is True
    assert decision.sr0.route == "empirical"


def test_modelled_route_still_available_for_sparse_ledgers():
    """A sparse ledger's answer is the modelled route with measured rho."""
    hurdle = sr0_modelled(sigma=0.6, rho=0.5, n_trials=3)
    assert hurdle.route == "modelled"
