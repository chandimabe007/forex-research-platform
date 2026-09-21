"""Deflated Sharpe — VAL-040, VAL-041, VAL-044 (MILE-050).

The primary acceptance rule::

    SR0 = s_ledger * E[max of N iid]
    DSR = Phi((SR_hat - SR0) / SE(SR_hat))      accept when DSR >= 0.95

Units: T is the number of return observations; every SR handled here is
periodic (matching the observation frequency), never annualised — an
annualised SR inflates the SE terms by sqrt(252) and 252 and can drive the
expression under the radical negative (spec: Units).

VAL-041: two routes to SR0, never both, never an assumed rho:

- Empirical (default): ``s_ledger * E[max of N iid]`` — the empirical
  standard deviation of the trial Sharpes already equals sigma*sqrt(1-rho),
  so applying the correlation factor again deflates twice.
- Modelled: ``sigma * sqrt(1-rho) * E[max of N iid]`` with a *measured*
  rho — only when too few trials exist for a stable s_ledger.

No effective-trial count (Kish) is implemented anywhere in this module:
it collapses the hurdle and below N=2 can return a negative SR0 — every
strategy passes (spec: VAL-041).

VAL-044: the candidate SE carries a serial-correlation adjustment
(Newey-West-style autocorrelation sum, the Lo 2002 factor) and states
which method produced it.

Everything fails closed: degenerate or non-finite input raises ValueError,
never an OK-looking default.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "DSR_DECISION_THRESHOLD",
    "DSRDecision",
    "HACSerialCorrelationSE",
    "SR0",
    "dsr_decision",
    "expected_max_iid",
    "hac_se_of_sr",
    "se_of_sr",
    "sr0_empirical",
    "sr0_modelled",
]

DSR_DECISION_THRESHOLD = 0.95  # VAL-040 acceptance


def _require_finite(name: str, value: float) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return value


def _phi(x: float) -> float:
    """Standard normal pdf."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _big_phi(x: float) -> float:
    """Standard normal cdf."""
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def expected_max_iid(n: int) -> float:
    """E[max of n iid standard normals], by Simpson integration.

    The integrand ``x * n * phi(x) * Phi(x)^(n-1)`` concentrates between
    roughly -3 and E[max]+3 sigma, so a fixed [-9, 9] grid at h=0.001 is
    far past double-precision accuracy for any realistic trial count
    (E[max of 10^6] is about 5.3).
    """
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise ValueError(f"n must be a positive integer, got {n!r}")
    if n == 1:
        return 0.0
    steps = 18_000  # h = 0.001 over [-9, 9], even count for Simpson
    h = 18.0 / steps

    def integrand(x: float) -> float:
        return x * n * _phi(x) * (_big_phi(x) ** (n - 1))

    total = integrand(-9.0) + integrand(9.0)
    for i in range(1, steps):
        weight = 4.0 if i % 2 == 1 else 2.0
        total += weight * integrand(-9.0 + i * h)
    value = total * h / 3.0
    if not math.isfinite(value) or value < 0.0:
        raise ValueError(f"expected_max_iid({n}) produced a non-finite or negative value")
    return value


@dataclass(frozen=True)
class SR0:
    """The null maximum hurdle, with the route that produced it (VAL-041)."""

    value: float
    route: str  # "empirical" | "modelled"
    n_trials: int


def sr0_empirical(s_ledger: float, n_trials: int) -> SR0:
    """Default route: the ledger's own trial Sharpe dispersion (VAL-041).

    ``s_ledger`` already contains the correlation adjustment (shared factor
    cancels in within-run dispersion), so it is multiplied by E[max of N
    iid] exactly once.
    """
    _require_finite("s_ledger", s_ledger)
    if s_ledger <= 0:
        raise ValueError(f"s_ledger must be positive, got {s_ledger!r}")
    value = s_ledger * expected_max_iid(n_trials)
    return SR0(value=value, route="empirical", n_trials=n_trials)


def sr0_modelled(sigma: float, rho: float, n_trials: int) -> SR0:
    """Modelled route, for too-few-trials ledgers only (VAL-041).

    ``rho`` must be **measured** (the caller owns that); an assumed rho is
    forbidden by the spec. The correlation factor is applied here and
    nowhere else — never in addition to an empirical s_ledger.
    """
    _require_finite("sigma", sigma)
    _require_finite("rho", rho)
    if sigma <= 0:
        raise ValueError(f"sigma must be positive, got {sigma!r}")
    if not 0.0 <= rho < 1.0:
        raise ValueError(f"rho must be in [0, 1), got {rho!r}")
    value = sigma * math.sqrt(1.0 - rho) * expected_max_iid(n_trials)
    return SR0(value=value, route="modelled", n_trials=n_trials)


def se_of_sr(t: int, sr: float, *, skew: float = 0.0, kurtosis: float = 3.0) -> float:
    """iid skew/kurtosis SE of the periodic Sharpe (Bailey & López de Prado).

    ``kurtosis`` is the raw (non-excess) moment, 3.0 for normal returns.
    Raises when the expression under the radical is not positive — the
    spec's explicitly-named failure of a mis-unitised SR.
    """
    _require_finite("sr", sr)
    _require_finite("skew", skew)
    _require_finite("kurtosis", kurtosis)
    if not isinstance(t, int) or isinstance(t, bool) or t < 2:
        raise ValueError(f"t must be an integer >= 2, got {t!r}")
    if kurtosis <= 1.0:
        raise ValueError(f"kurtosis must exceed 1 (raw moment; 3 = normal), got {kurtosis!r}")
    variance = (1.0 - skew * sr + (kurtosis - 1.0) / 4.0 * sr * sr) / (t - 1)
    if variance <= 0.0:
        raise ValueError(
            "SE expression under the radical is not positive "
            f"(t={t}, sr={sr}, skew={skew}, kurtosis={kurtosis}) — check SR units"
        )
    return math.sqrt(variance)


@dataclass(frozen=True)
class HACSerialCorrelationSE:
    """A SE that states its method (VAL-044: 'state which')."""

    value: float
    method: str
    n_lags: int
    autocorrelation_sum: float


def hac_se_of_sr(
    returns: Sequence[float],
    *,
    n_lags: int | None = None,
) -> HACSerialCorrelationSE:
    """Lo/Newey-West serial-correlation adjustment to the SE of the Sharpe.

    SE(SR) ~= sqrt((1 + 2 * sum_k rho_k) / T), the first-order Lo (2002)
    factor: the iid term's skew/kurtosis refinement is deliberately not
    mixed in — the adjustment the caller must declare is the
    autocorrelation sum. Default lag count is Newey-West's rule of thumb
    ``floor(4 * (T/100)^(2/9))``.
    """
    t = len(returns)
    if t < 8:
        raise ValueError(f"need at least 8 return observations, got {t}")
    values = [_require_finite(f"returns[{i}]", r) for i, r in enumerate(returns)]
    mean = math.fsum(values) / t
    demeaned = [r - mean for r in values]
    denom = math.fsum(d * d for d in demeaned)
    if denom <= 0.0:
        raise ValueError("returns have zero variance")
    if n_lags is None:
        n_lags = max(1, int(4.0 * (t / 100.0) ** (2.0 / 9.0)))
    if not isinstance(n_lags, int) or not 1 <= n_lags <= t - 2:
        raise ValueError(f"n_lags must be an integer in [1, {t - 2}], got {n_lags!r}")
    autocorr_sum = 0.0
    for k in range(1, n_lags + 1):
        numerator = math.fsum(demeaned[i] * demeaned[i - k] for i in range(k, t))
        autocorr_sum += numerator / denom
    factor = 1.0 + 2.0 * autocorr_sum
    if factor <= 0.0:
        raise ValueError(
            f"HAC factor is not positive (1 + 2*sum rho_k = {factor}) — "
            "returns are pathologically anti-correlated for this lag count"
        )
    return HACSerialCorrelationSE(
        value=math.sqrt(factor / t),
        method="lo_2002_autocorrelation_sum",
        n_lags=n_lags,
        autocorrelation_sum=autocorr_sum,
    )


@dataclass(frozen=True)
class DSRDecision:
    """The VAL-040 verdict and every number that produced it."""

    z: float
    dsr: float
    accepted: bool
    threshold: float
    sr_hat: float
    sr0: SR0
    se: float
    se_method: str


def dsr_decision(
    sr_hat: float,
    sr0: SR0,
    se: float,
    *,
    se_method: str = "iid_skew_kurtosis",
    threshold: float = DSR_DECISION_THRESHOLD,
) -> DSRDecision:
    """VAL-040: accept when Phi((SR_hat - SR0) / SE) >= threshold (0.95).

    ``se_method`` must name the adjustment (VAL-044: 'state which') — pass
    ``hac_se_of_sr(...).method`` when the SE came from the serial-correlation
    path.
    """
    _require_finite("sr_hat", sr_hat)
    _require_finite("se", se)
    _require_finite("threshold", threshold)
    if not se_method or not se_method.strip():
        raise ValueError("se_method must name the SE adjustment (VAL-044)")
    if not 0.0 < threshold < 1.0:
        raise ValueError(f"threshold must be in (0, 1), got {threshold!r}")
    if se <= 0:
        raise ValueError(f"se must be positive, got {se!r}")
    z = (sr_hat - sr0.value) / se
    dsr = _big_phi(z)
    return DSRDecision(
        z=z,
        dsr=dsr,
        accepted=dsr >= threshold,
        threshold=threshold,
        sr_hat=sr_hat,
        sr0=sr0,
        se=se,
        se_method=se_method,
    )
