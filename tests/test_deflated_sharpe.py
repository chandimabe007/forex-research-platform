"""VAL-040/041/044 tests — the spec's own numbers are the fixtures.

The decisive test reproduces the spec's simulation standard for VAL-041:
E[max of the correlated process] must match sqrt(1-rho)*E[max iid] to
within 0.02 at N = 100 and 500 for rho = 0, 0.5, 0.85 (seeded, so the
margin the spec verified is preserved exactly).
"""

from __future__ import annotations

import math
import random

import pytest

from forex_research.validation.deflated_sharpe import (
    DSR_DECISION_THRESHOLD,
    dsr_decision,
    expected_max_iid,
    hac_se_of_sr,
    se_of_sr,
    sr0_empirical,
    sr0_modelled,
)

# ---------------------------------------------------------------------------
# E[max of N iid]
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 2, 3, 5, 10, 30, 100, 500])
def test_expected_max_iid_matches_known_reference_values(n):
    # Reference values from the standard extreme-value table for the
    # maximum of n standard normals (exact for small n via closed-form
    # integrals, simulation-verified for large n). N=5000 is checked
    # separately against simulation below.
    references = {
        1: 0.0,
        2: 1.0 / math.sqrt(math.pi),  # exact: E[max of 2] = 1/sqrt(pi)
        3: 0.8462843753168344,
        5: 1.1629644739398774,
        10: 1.5387527308298794,
        30: 2.042755572961798,
        100: 2.5075945388489794,
        500: 3.0366125042077385,
    }
    assert expected_max_iid(n) == pytest.approx(references[n], abs=2e-3)


def test_expected_max_iid_matches_simulation_at_n_5000():
    """Independent method at large N: Monte-Carlo vs quadrature, loose tol."""
    rng = random.Random(11)
    reps = 4_000
    n = 5_000
    total = 0.0
    for _ in range(reps):
        total += max(rng.gauss(0.0, 1.0) for _ in range(n))
    simulated = total / reps
    assert simulated == pytest.approx(expected_max_iid(n), abs=0.05)


def test_expected_max_iid_monotone_in_n():
    values = [expected_max_iid(n) for n in (2, 5, 10, 50, 200, 1000)]
    assert values == sorted(values)


@pytest.mark.parametrize("bad", [0, -3, 2.5, True])
def test_expected_max_iid_rejects_invalid_n(bad):
    with pytest.raises(ValueError):
        expected_max_iid(bad)


# ---------------------------------------------------------------------------
# VAL-041 — the spec's simulation cross-check and the double-deflation trap
# ---------------------------------------------------------------------------


def _simulated_expected_max_equicorrelated(n: int, rho: float, reps: int, seed: int) -> float:
    """Monte-Carlo E[max of n equicorrelated normals] = sqrt(1-rho)*F + sqrt(rho)*G."""
    rng = random.Random(seed)
    factor_sd = math.sqrt(rho)
    idio_sd = math.sqrt(1.0 - rho)
    total = 0.0
    for _ in range(reps):
        f = rng.gauss(0.0, 1.0)
        best = max(f * factor_sd + rng.gauss(0.0, idio_sd) for _ in range(n))
        total += best
    return total / reps


@pytest.mark.parametrize("n", [100, 500])
@pytest.mark.parametrize("rho", [0.0, 0.5, 0.85])
def test_val041_modelled_route_matches_simulation_within_spec_tolerance(n, rho):
    """sqrt(1-rho) * E[max iid] vs simulated E[max] — spec tolerance 0.02."""
    reps = 6_000 if n == 100 else 4_000
    simulated = _simulated_expected_max_equicorrelated(n, rho, reps, seed=42 + n + int(rho * 100))
    modelled = sr0_modelled(sigma=1.0, rho=rho, n_trials=n).value  # sigma=1: hurdle == E[max]
    assert simulated == pytest.approx(modelled, abs=0.02), (
        f"N={n} rho={rho}: simulated {simulated:.4f} vs modelled {modelled:.4f}"
    )


def test_val041_double_deflation_produces_the_specs_broken_hurdle():
    """The spec's worked example: s_ledger already contains sqrt(1-rho).

    At sigma=0.60, rho=0.80, N=500: correct SR0 is 0.818; applying the
    factor again gives 0.366 — a hurdle less than half what it should be.
    This test exists to make that specific mistake impossible to ship.
    """
    n = 500
    sigma, rho = 0.60, 0.80
    e_max = expected_max_iid(n)
    assert e_max == pytest.approx(3.0366125, abs=2e-3)
    # The empirical ledger dispersion of equicorrelated trials measures
    # sigma*sqrt(1-rho); the empirical route therefore multiplies ONCE.
    # (The spec quotes the results to three significant figures: its 0.818
    # and 0.366 correspond to E[max]=3.0485; the tabulated 3.0366 gives
    # 0.815 and 0.364 — the same conclusion at the spec's precision.)
    s_ledger = sigma * math.sqrt(1.0 - rho)
    empirical = sr0_empirical(s_ledger=s_ledger, n_trials=n)
    assert empirical.value == pytest.approx(0.818, abs=5e-3)
    assert empirical.route == "empirical"
    # The modelled route with the same measured rho agrees...
    modelled = sr0_modelled(sigma=sigma, rho=rho, n_trials=n)
    assert modelled.value == pytest.approx(0.818, abs=5e-3)
    # ...and the double-deflated mistake is recognisable by magnitude:
    # less than half the correct hurdle.
    double_deflated = s_ledger * math.sqrt(1.0 - rho) * e_max
    assert double_deflated == pytest.approx(0.366, abs=5e-3)
    assert double_deflated < empirical.value / 2.0


def test_val041_routes_agree_when_ledger_is_large_and_rho_known():
    s_ledger = 0.60 * math.sqrt(1.0 - 0.80)
    empirical = sr0_empirical(s_ledger=s_ledger, n_trials=500)
    modelled = sr0_modelled(sigma=0.60, rho=0.80, n_trials=500)
    assert empirical.value == pytest.approx(modelled.value, rel=1e-9)


def test_val041_modelled_route_refuses_assumed_rho_marker_and_invalid_values():
    with pytest.raises(ValueError):
        sr0_modelled(sigma=0.0, rho=0.5, n_trials=100)
    with pytest.raises(ValueError):
        sr0_modelled(sigma=0.5, rho=-0.1, n_trials=100)
    with pytest.raises(ValueError):
        sr0_modelled(sigma=0.5, rho=1.0, n_trials=100)
    with pytest.raises(ValueError):
        sr0_empirical(s_ledger=0.0, n_trials=100)
    with pytest.raises(ValueError):
        sr0_empirical(s_ledger=float("nan"), n_trials=100)


# ---------------------------------------------------------------------------
# VAL-044 — standard errors
# ---------------------------------------------------------------------------


def test_se_of_sr_matches_closed_form_for_normal_returns():
    # Normal returns, skew 0, kurtosis 3: SE = sqrt((1 + sr^2/2)/(T-1)).
    expected = math.sqrt((1.0 + 0.1 * 0.1 / 2.0) / 251.0)
    assert se_of_sr(t=252, sr=0.1) == pytest.approx(expected)
    # Bailey & Lopez de Prado 2014, eq. for skew 0, kurt 3, sr=0, T=1250.
    assert se_of_sr(t=1250, sr=0.0) == pytest.approx(math.sqrt(1.0 / 1249.0))


def test_se_of_sr_inflates_with_kurtosis_and_skewed_negative_sr():
    base = se_of_sr(t=252, sr=0.1)
    fat = se_of_sr(t=252, sr=0.1, kurtosis=8.0)
    assert fat > base
    # Negative skew with positive SR inflates the SE; positive skew deflates.
    assert se_of_sr(t=252, sr=0.1, skew=-1.0) > base
    assert se_of_sr(t=252, sr=0.1, skew=1.0) < base


def test_se_of_sr_detects_the_specs_unit_error():
    """A mis-unitised SR can drive the radical negative — must raise, not return."""
    # (k-1) <= skew^2 is the regime where the quadratic has real roots;
    # sr=2.5 sits between them, making the variance expression negative.
    with pytest.raises(ValueError, match="not positive"):
        se_of_sr(t=20, sr=2.5, skew=1.5, kurtosis=2.0)


def test_hac_se_inflates_for_positive_autocorrelation_and_states_method():
    rng = random.Random(7)
    ar1 = [0.0] * 600
    for i in range(1, 600):
        ar1[i] = 0.6 * ar1[i - 1] + rng.gauss(0.0, 1.0)
    iid = [rng.gauss(0.0, 1.0) for _ in range(600)]
    hac_ar1 = hac_se_of_sr(ar1)
    hac_iid = hac_se_of_sr(iid)
    assert hac_ar1.value > hac_iid.value
    assert hac_ar1.method == "lo_2002_autocorrelation_sum"
    assert hac_ar1.autocorrelation_sum > 0.3  # rho=0.6 over default lags
    # Explicit lag count is honored and validated.
    assert hac_se_of_sr(ar1, n_lags=1).n_lags == 1
    with pytest.raises(ValueError):
        hac_se_of_sr(ar1, n_lags=0)
    with pytest.raises(ValueError):
        hac_se_of_sr([0.0] * 7)
    with pytest.raises(ValueError):
        hac_se_of_sr([1.0] * 100)  # zero variance


# ---------------------------------------------------------------------------
# VAL-040 — the decision rule
# ---------------------------------------------------------------------------


def test_dsr_decision_accepts_and_rejects_at_threshold():
    sr0 = sr0_empirical(s_ledger=0.5, n_trials=100)  # hurdle = 0.5 * 2.5076
    se = se_of_sr(t=1000, sr=2.0)
    # SR well above the hurdle: accepted.
    assert dsr_decision(sr_hat=2.0, sr0=sr0, se=se).accepted is True
    # SR below the hurdle: rejected.
    rejected = dsr_decision(sr_hat=1.0, sr0=sr0, se=se)
    assert rejected.accepted is False
    assert rejected.dsr < 0.5


def test_dsr_decision_boundary_is_exact_at_95_percent():
    sr0 = sr0_empirical(s_ledger=0.5, n_trials=100)
    se = 0.1
    z_needed = 1.6448536269514722  # Phi^-1(0.95)
    sr_at_threshold = sr0.value + z_needed * se
    at = dsr_decision(sr_hat=sr_at_threshold, sr0=sr0, se=se)
    assert at.accepted is True
    assert at.dsr == pytest.approx(0.95, abs=1e-6)
    just_under = dsr_decision(sr_hat=sr_at_threshold - 1e-6, sr0=sr0, se=se)
    assert just_under.accepted is False


def test_dsr_decision_records_the_se_method_it_used():
    sr0 = sr0_empirical(s_ledger=0.5, n_trials=100)
    decision = dsr_decision(sr_hat=1.0, sr0=sr0, se=0.05)
    assert decision.se_method == "iid_skew_kurtosis"
    rng = random.Random(1)
    sample = [rng.gauss(0, 1) for _ in range(200)]
    hac = hac_se_of_sr(sample)
    declared = dsr_decision(sr_hat=1.0, sr0=sr0, se=hac.value, se_method=hac.method)
    assert declared.se_method == "lo_2002_autocorrelation_sum"


def test_dsr_decision_refuses_methodless_se_and_garbage():
    sr0 = sr0_empirical(s_ledger=0.5, n_trials=100)
    with pytest.raises(ValueError, match="VAL-044"):
        dsr_decision(sr_hat=1.0, sr0=sr0, se=0.05, se_method="  ")
    with pytest.raises(ValueError):
        dsr_decision(sr_hat=1.0, sr0=sr0, se=0.0)
    with pytest.raises(ValueError):
        dsr_decision(sr_hat=float("nan"), sr0=sr0, se=0.05)
    with pytest.raises(ValueError):
        dsr_decision(sr_hat=1.0, sr0=sr0, se=0.05, threshold=1.0)


def test_dsr_threshold_is_the_specs_95_percent():
    assert DSR_DECISION_THRESHOLD == 0.95
