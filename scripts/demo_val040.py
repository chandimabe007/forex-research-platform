"""Run a batch of synthetic candidates through the full VAL-040 decision.

    .venv/Scripts/python scripts/demo_val040.py

The batch is 40 equicorrelated daily-return strategies (one shared factor),
exactly one of which carries a true edge, plus a serially correlated noise
term on the winner so the HAC adjustment (VAL-044) visibly matters. The
script prints the whole audit trail: trial Sharpes, the ledger hurdle
(VAL-041 empirical route), the winner's SE with its declared method, the
DSR verdict -- and a zero-edge calibration (a taste of VAL-043) showing the
deflation neutralizing selection bias.

Seeded and deterministic. This demonstrates the *instrument*; it is not
evidence of any real edge (same disclaimer as demo_backtest).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np

from forex_research.validation.deflated_sharpe import (
    dsr_decision,
    expected_max_iid,
    hac_se_of_sr,
    se_of_sr,
    sr0_empirical,
)

T = 1_000  # return observations per candidate (trading days)
N_CANDIDATES = 40  # strategies competing under one selection criterion
FACTOR_LOADING = 0.7
WINNER_DAILY_SR = 3.0 / np.sqrt(252.0)  # a true annual SR of 3.0
WINNER_AR1 = 0.15  # serial correlation on the winner's noise (VAL-044 demo)
CALIBRATION_REPS = 200  # zero-edge batches for the false-positive taste
SEED = 40


def simulate_batch(rng: np.random.Generator, winner: bool) -> tuple[np.ndarray, int]:
    """One batch of N_CANDIDATES return series; returns (matrix, winner_idx)."""
    factor = rng.standard_normal(T)
    idio = rng.standard_normal((N_CANDIDATES, T))
    winner_idx = int(rng.integers(N_CANDIDATES))
    series = FACTOR_LOADING * factor + idio  # zero edge for every candidate
    if winner:
        # True drift + serially correlated noise on the winner only.
        ar = np.zeros(T)
        for t in range(1, T):
            ar[t] = WINNER_AR1 * ar[t - 1] + rng.standard_normal()
        # Drift chosen so the *total-variance* Sharpe (factor + idio + AR
        # noise) equals WINNER_DAILY_SR.
        var = FACTOR_LOADING**2 + 1.0 + WINNER_AR1**2 / (1 - WINNER_AR1**2)
        series[winner_idx] += WINNER_DAILY_SR * np.sqrt(var) + ar
    return series, winner_idx


def sharpes(series: np.ndarray) -> np.ndarray:
    return series.mean(axis=1) / series.std(axis=1, ddof=1)


def main() -> int:
    rng = np.random.default_rng(SEED)
    line = "=" * 72

    print(line)
    print("VAL-040 worked example -- deflated Sharpe on a synthetic candidate batch")
    print(line)
    print(f"batch: {N_CANDIDATES} candidates x {T} daily returns, one shared factor")
    print(f"cross-sectional correlation ~ {FACTOR_LOADING**2 / (1 + FACTOR_LOADING**2):.3f}")
    print(f"truth: candidate ? carries a true daily SR of {WINNER_DAILY_SR:.4f} (annual 3.0)")

    # -- the research run: everyone competes, the best Sharpe is selected ----
    series, winner_idx = simulate_batch(rng, winner=True)
    sr_all = sharpes(series)
    ranked = np.argsort(sr_all)[::-1]
    print("\ntop 5 candidates by observed (periodic, daily) Sharpe:")
    for rank, idx in enumerate(ranked[:5], start=1):
        marker = "  <-- true edge" if idx == winner_idx else ""
        print(f"  {rank}. candidate {idx:2d}: SR = {sr_all[idx]:+.4f}{marker}")
    sr_hat = float(sr_all[ranked[0]])

    # -- the trial ledger (VAL-045 shape): who competed, what they scored ----
    s_ledger = float(sr_all.std(ddof=1))
    e_max = expected_max_iid(N_CANDIDATES)
    hurdle = sr0_empirical(s_ledger=s_ledger, n_trials=N_CANDIDATES)
    print("\ntrial ledger (VAL-041 empirical route -- the default):")
    print(f"  N (statistical candidate set) = {N_CANDIDATES}")
    print(f"  s_ledger (dispersion of trial Sharpes) = {s_ledger:.4f}")
    print(f"  E[max of {N_CANDIDATES} iid] = {e_max:.4f}")
    print(f"  SR0 = s_ledger * E[max] = {hurdle.value:.4f}  (route: {hurdle.route})")
    print("  note: s_ledger already contains the correlation adjustment;")
    print("        multiplying by sqrt(1-rho) again would deflate twice (VAL-041).")

    # -- the winner's standard error, with its declared method (VAL-044) -----
    winner_returns = series[ranked[0]]
    se_iid = se_of_sr(t=T, sr=sr_hat)
    se_hac = hac_se_of_sr(winner_returns.tolist())
    print("\nstandard error of the winner's Sharpe:")
    print(f"  iid skew/kurtosis SE      = {se_iid:.5f}")
    print(
        f"  HAC (Lo 2002) SE          = {se_hac.value:.5f}  "
        f"(n_lags={se_hac.n_lags}, sum rho_k={se_hac.autocorrelation_sum:.3f})"
    )
    print("  the decision uses the HAC SE -- the returns are serially correlated.")

    # -- the VAL-040 decision -------------------------------------------------
    decision = dsr_decision(sr_hat=sr_hat, sr0=hurdle, se=se_hac.value, se_method=se_hac.method)
    print("\nVAL-040 decision:")
    print(
        f"  SR_hat = {decision.sr_hat:.4f}   SR0 = {decision.sr0.value:.4f}   "
        f"SE = {decision.se:.5f} ({decision.se_method})"
    )
    print(
        f"  z = ({decision.sr_hat:.4f} - {decision.sr0.value:.4f}) / {decision.se:.5f} "
        f"= {decision.z:.3f}"
    )
    print(f"  DSR = Phi(z) = {decision.dsr:.4f}   threshold = {decision.threshold}")
    verdict = "ACCEPT" if decision.accepted else "REJECT"
    print(f"  >>> {verdict}")

    # -- zero-edge calibration (a taste of VAL-043) ---------------------------
    print("\nzero-edge calibration: the same procedure on batches with NO edge")
    accepted = 0
    dsrs: list[float] = []
    for _ in range(CALIBRATION_REPS):
        null_series, _ = simulate_batch(rng, winner=False)
        null_sr = sharpes(null_series)
        null_best = float(null_sr.max())
        null_hurdle = sr0_empirical(s_ledger=float(null_sr.std(ddof=1)), n_trials=N_CANDIDATES)
        null_se = se_of_sr(t=T, sr=null_best)
        null_decision = dsr_decision(sr_hat=null_best, sr0=null_hurdle, se=null_se)
        dsrs.append(null_decision.dsr)
        accepted += null_decision.accepted
    rate = accepted / CALIBRATION_REPS
    print(f"  {CALIBRATION_REPS} all-zero-edge batches, best-of-{N_CANDIDATES} each:")
    print(f"  accepted {accepted} of {CALIBRATION_REPS} (rate {rate:.1%}; target ~5%)")
    if rate < 0.05:
        print("  (conservative side of the target -- the empirical hurdle overdeflates")
        print("   slightly under equicorrelation; VAL-043's full simulation with the")
        print("   declared correlation structure quantifies this properly)")
    print(f"  DSR of the selected best: median {np.median(dsrs):.3f}, max {np.max(dsrs):.3f}")
    print(f"  selection alone pushes the best SR ~{e_max / np.sqrt(T):.3f} sigma above zero;")
    print("  the deflation pushes the hurdle to the same place. That is the point.")
    print(line)
    print("Demonstration of the instrument -- not evidence of a real edge.")
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
