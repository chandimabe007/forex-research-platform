# Research Protocol

**Document 2 of 5** · Revision 14 · 19 September 2026
Requirement prefixes: `GATE`, `VAL`

The three gates, the statistical protocol, trial accounting and the stopping rules. Implements `PROD-022`.

---

# Part A — The gates

Each gate stops work until a stated condition is met. A gate that can only return "proceed" is decoration.

---

## GATE-001 · Gate 0 — firm, programme and account type

Selection is of a triple (`PROD-023`), completed before any execution code exists.

### GATE-002 · Selection criteria, in priority order

| # | Criterion | Disqualifying answer |
| --- | --- | --- |
| 1 | Automated execution permitted on this programme | EAs or API trading banned |
| 2 | Maximum loss static, not trailing on equity high-water mark | Trailing on equity |
| 3 | Daily-loss baseline: does unrealised profit ratchet it? | Equity-ratcheting baseline combined with a holding period spanning the reset |
| 4 | Platform and programmatic access | No API |
| 5 | Venue cost data obtainable | No way to capture spread on this venue |
| 6 | Server time zone stated as an IANA zone or explicit transition rule | Undocumented, or a bare UTC offset (`GATE-005`) |
| 7 | Funded-stage rules compatible with the intended holding period | News or weekend bans incompatible with the strategy |
| 8 | Consistency rule | Strict day-weighting that conflicts with optimising pass probability |

### GATE-003 · The floating-profit ratchet

Where the daily baseline is the *higher of* opening balance or opening equity, unrealised profit held through the reset raises the next day's floor. Carrying +3% floating into the reset makes the day-2 baseline 103%; the position retracing to break-even is then a 3% fall against a 5% limit, leaving 2% of room on a day the account is flat.

Where the baseline is the day's opening **balance**, this does not occur.

This became decisive once the strategy's holding period was allowed to span the reset. Record the answer in `CHAL-011` as `ratchet_on_equity`, and verify it against the firm's own wording rather than a comparison site.

### GATE-004 · Due diligence beyond the rules

A firm can be rule-compliant and still fail to pay. Record with dates and links; **record what could not be established rather than filling it with weak sources** — affiliate "payout proof" pages are paid placements.

| Check | What to look for |
| --- | --- |
| Payout history | Independent aggregated evidence, not the firm's testimonials |
| Time in operation | Recent founding means no track record to examine |
| Terms-change history | Whether rules were altered retroactively for existing accounts |
| Termination clauses | Grounds for closing an account and voiding profits |
| Country eligibility | For the programme *and* the platform |
| Simulated vs live | How the firm characterises the account and the payout obligation |

### GATE-005 · Time zone is a zone, not an offset

A one-time probe returning "UTC+3" establishes nothing about DST. Store an **IANA zone** (`Europe/Prague`) or an explicit transition rule, and verify across an actual transition before relying on it. `server_observes_dst: true` plus one observed offset is insufficient and must not be accepted as verification.

### GATE-006 · Source-conflict hierarchy

Where sources disagree, this order governs:

1. Signed account agreement or checkout snapshot
2. Programme terms
3. Dashboard
4. Public FAQ
5. Written support response
6. Observed technical behaviour

**Observed behaviour wins for symbol properties and server configuration; it does not win for contractual rules.** A server that permits an action the terms prohibit has not granted permission.

### GATE-007 · Exit condition

- [ ] `challenge_rules.yaml` (`CHAL-010`) complete for the chosen triple, every field carrying provenance
- [ ] Every field `verified` or `not_applicable`; none `unknown`
- [ ] Funded-stage rules recorded separately from evaluation-stage rules
- [ ] IANA zone recorded (`GATE-005`)
- [ ] Due diligence recorded including what could not be established
- [ ] Capability probe run (`GATE-011`)

### GATE-008 · Re-verification

Before **every** evaluation account purchase, and on any firm communication announcing a change, re-run the checklist and update `last_verified_date`. For a rule change during an active account, see `OPS-040`.

---

## GATE-010 · Gate 0.5 — capability probe

Runs on every connection, persists output, fails closed on anything missing or unexpected. Specified at `EXEC-020`.

### GATE-011 · What can end the project here

| Field | Why |
| --- | --- |
| `trade_expert` | Automation may be disabled on this server regardless of the rulebook |
| `stops_level`, `freeze_level` | A minimum stop wider than the intended stop makes the strategy unplaceable |
| `filling_mode` per symbol | An unsupported `type_filling` is rejected outright |
| Symbol names and suffixes | Hard-coded names break silently |
| Contract size, tick size, tick value, volume min/step/max | Every sizing calculation |
| Server time behaviour | Cross-check against `GATE-005`; a probe alone cannot settle DST |
| Hedging vs netting | Changes position semantics and the close contract (`EXEC-032`) |

---

## GATE-020 · Gate 1 — cost feasibility

### GATE-021 · The measurement

Compute `c` per pair, per session bucket, per candidate timeframe:

```
c = round-trip cost in pips / stop distance in pips
```

`stop_distance` is the entry-to-stop distance used for sizing, **inclusive of spread at the stop**, so cost and risk are measured on one basis.

### GATE-022 · The threshold

`c < 0.25` at the chosen timeframe. One threshold at every timeframe — introducing a tighter one for some timeframes makes the cross-timeframe comparison incoherent, which is the comparison this gate exists to produce.

| `c` | Gross edge needed to break even | Reading |
| --- | --- | --- |
| 0.05 | 0.05R | Comfortable |
| 0.15 | 0.15R | Demanding but plausible |
| 0.25 | 0.25R | Edge of plausibility |
| 0.40 | 0.40R | Timeframe is wrong |

0.25 is a **declared configuration value with rationale**, not a derived constant (`OPS-050`). Higher timeframes typically land at 0.03–0.08 because stops are wider; that is an observation, not a second gate.

### GATE-023 · Why this decides the timeframe

Statistical power does not favour any timeframe at equal Sharpe (`VAL-021`), so power is not a reason to choose one. Cost is the dimension this gate can measure before any strategy exists, so Gate 1 chooses the timeframe on cost. Attainable Sharpe, dependence and tail behaviour may also differ by timeframe; they are measured later against the real strategy, not assumed equal.

### GATE-024 · Exit condition

- [ ] Cost table exists per pair, session bucket and candidate timeframe, with the uncertainty reporting required by `COST-014`
- [ ] `c` computed at intended stop distances
- [ ] `c < 0.25` at the chosen timeframe

**On failure:** widen stops, move to a higher timeframe, or change venue. Not: proceed hoping the strategy is good enough.

---

## GATE-030 · Gate 2 — statistical feasibility

### GATE-031 · Declare the target before looking at the window

The forbidden move: take the window as fixed, compute the effective trades it supplies, then choose the effect size that makes the power calculation come out. The arithmetic is correct and the conclusion worthless, because the hypothesis has been fitted to the sample. It also silently changes what is being tested — a window supplying 500 effective trades "requires" μ ≈ 0.18R, an 18% annual return rather than a declared 10%.

Required order:

1. Declare the target annual return and volatility budget (`PROD-010`) as an economic hypothesis.
2. Derive the declared `SR_annual = target return / volatility budget`.
3. Compute required years (`VAL-020`), and the per-trade equivalent (`VAL-010`) from the same covariance model.
4. Compare against what the candidate window supplies.
5. If short, choose a path from `VAL-013` **explicitly and record it**.

A minimum economic return sets the smallest edge worth detecting. It does not assert the strategy's true edge equals it.

### GATE-032 · The pilot is an envelope, not a freeze

Measuring `n_eff` needs trade concurrency, which would need a feature engine, a strategy and a backtest engine — none of which exist at this milestone. A standalone placeholder generator breaks the circle: entries at a target rate with fixed stops and targets, reading development-partition bars only.

**A placeholder cannot determine the eventual strategy's frequency, holding time, concurrency or serial dependence.** The pilot therefore produces a *feasibility envelope* — enough to decide whether to proceed and roughly what window is needed. Final `n_eff`, final purge gap (`VAL-032`) and final power are recomputed from the actual strategy before any validation access, and a material divergence reopens this gate rather than being absorbed quietly.

Measure: mean concurrency `k̄`, mean pairwise correlation of overlapping trade returns `ρ̄`, holding-time distribution including its **maximum**.

### GATE-033 · Exit condition

- [ ] Target return and volatility budget declared in writing before the window was examined
- [ ] `k̄`, `ρ̄`, holding-time distribution measured on development data only
- [ ] Required years reported from the portfolio Sharpe (`VAL-020`), with the pilot's covariance estimate recorded
- [ ] A path from `VAL-013` chosen and recorded if the window falls short
- [ ] Universe and timeframe recorded as a provisional envelope, to be confirmed against the real strategy

**On failure:** change the declared target, extend history, change instrument class, or accept lower power under `VAL-013` Path A. **Not** change timeframe — that does not affect detectability (`VAL-021`).

---

# Part B — Statistical protocol

## VAL-001 · One primary endpoint

The document previously left several acceptance procedures active without saying which decided anything. Exactly one is primary:

| Role | Procedure |
| --- | --- |
| **Primary acceptance** | Deflated Sharpe ≥ 0.95 against the expected null maximum (`VAL-040`) |
| Secondary, must also hold | Net expectancy confidence interval excludes zero (`VAL-030`); `c` sensitivity (`GATE-022`) |
| Diagnostic only | Random-entry null, shuffled-return null, 95th percentile of null maximum, concentration tests, robustness suite |

A diagnostic failing triggers investigation and a ledger entry. It does not by itself reject, and it never fails the build (`VAL-002`).

## VAL-002 · Software tests and research gates are different things

A blocking CI test fails only when the **code** is wrong. A strategy that cannot beat random entries is a valid research finding produced by correctly functioning software; gating the build on it turns an honest negative into a red pipeline and creates pressure to make it pass.

**Blocking in CI** — software correctness only:

| Test | Asserts |
| --- | --- |
| `VAL-060` Truncation | Features truncated by availability match features sliced from full data, row sets included |
| `VAL-061` Leaking fixtures | The truncation test **fails** on each known-bad fixture |
| `VAL-062` Coverage registry | Every registered feature has a truncation case |
| `VAL-063` Sizing arithmetic | Risk computed from **actual filled volume** is correct and never exceeds the cap |
| `VAL-064` Calibration availability | No backtest reads a fitted table whose `available_from` postdates the bar being processed |

**Research gates** — recorded in the ledger, never blocking:

| Gate | Meaning of failure |
| --- | --- |
| Random-entry null | Entry timing carries no signal |
| Shuffled-return null | Edge survives destruction of serial structure — investigate leakage. Not universally a valid reject rule: block construction alters volatility and cross-asset structure |
| Deflated Sharpe | Does not survive the trial count |
| Cost sensitivity | Cost artefact |

---

## Power

## VAL-010 · The requirement

Trade outcomes in R. To detect an edge `μ` at power `1−β` and significance `α`:

```
n_eff ≥ (z_α + z_β)² σ_R² / μ²
```

## VAL-011 · One-sided, and say so

The test is **one-sided** at α = 0.05: the hypothesis is positive expectancy, and a significantly negative result is not a success by another name. Use `z_α = 1.645`. Confidence intervals reported alongside are two-sided at 90% so their lower bound corresponds to the test. Earlier drafts used 1.96 with directional language; that inconsistency is resolved here and the constants must agree everywhere.

## VAL-020 · Power in calendar time, from one covariance model

**Sizing and power must come from the same model of the portfolio's returns.** Revision 11 did not: it sized risk per trade as though trades were independent, then shrank the sample by an efficiency factor of 0.60 as though they were not, and it used the two-sided constant 7.84 against a declared one-sided test. Both errors lengthened the window. The 13.1-year figure is **withdrawn** (`PROD-031`).

Measure the Sharpe ratio on the **portfolio's actual daily return series** — concurrency, correlation between pairs and serial dependence included — and power follows directly:

```
years required  T = (z_α + z_β)² / SR_annual²
                  = 6.18 / SR_annual²      (one-sided α = 0.05, power 0.80)
```

Dependence is not a separate correction here. It is already inside `SR_annual`, because it widens the volatility the Sharpe ratio is measured against. Applying an efficiency factor on top counts it twice. `VAL-010` is the same requirement expressed per trade, and agrees with this one only when `n_eff` and `σ_R` come from the same covariance model.

| Declared `SR_annual` | Example | Years required |
| --- | --- | --- |
| 1.00 | 10% return on a 10% volatility budget | 6.2 |
| 0.75 | 7.5% on 10% | 11.0 |
| 0.50 | 5% on 10% | 24.7 |

**Status: illustrative.** The formula assumes annual returns roughly independent and a stable Sharpe over the window. The authoritative figure is the simulated power of the whole acceptance procedure (`VAL-046`), which this formula only approximates.

Risk per trade is a consequence, not an input: it is whatever makes the portfolio's measured volatility equal the declared budget, estimated from the pilot's covariance (`GATE-032`) and re-estimated from the real strategy.

## VAL-021 · Power does not favour any timeframe — and that is all it says

For a given portfolio Sharpe, the required window is the same at every timeframe. Detecting a given Sharpe takes a given amount of calendar time however the bars are sliced.

This is a statement about **detectability at equal Sharpe**. It does not say strategies at different timeframes attain equal Sharpe, have equal dependence, or have equal tails. Those are empirical and may differ; they are measured, not assumed.

Earlier drafts claimed M5 required ~103 years and was "not validatable at all". That came from a fixed-risk-per-trade comparison and is **withdrawn** (`PROD-031`). Any claim that a timeframe is statistically unviable should be treated as a parameterisation error until the risk budget is shown constant.

## VAL-013 · Paths when the window falls short

| Path | Meaning | Cost |
| --- | --- | --- |
| **A — proceed under-powered** | Keep the window; record the computed power to detect the declared edge | A real edge is likely discarded. Legitimate **only** if declared in advance and honoured when the result is null |
| **B — extend the window** | Lengthen until it supplies the required `n_eff` | At a declared Sharpe of 1.0 this is ~6 years; at 0.75, ~11; at 0.5, ~25 (`VAL-020`). Trades power against relevance: 2006 market structure may not inform 2026 |
| **C — declare a large-edge hypothesis** | Test only `μ ≥ X` | Must be written as: *"if this strategy has a real edge of `μ_declared`, this test is under-powered and will discard it."* That sentence is the price |

**There is no fourth path.** Adjusting `μ_target` until the numbers agree is Path C without the sentence. Path A is permitted — earlier text calling lower power "forbidden" contradicted this table and is superseded.

## VAL-014 · Universe expansion is not a lever

Adding correlated pairs raises nominal count and trade frequency together, moving both sides of the inequality while worsening `n_eff`. Crosses are linear combinations of the majors (EURGBP = EURUSD ÷ GBPUSD), so their correlation is structural. How much independent information remains is measurable at `GATE-032` and should be measured rather than asserted (`PROD-031`).

---

## Partitions and holdout

## VAL-030 · Partitions are an output of Gate 2

Boundaries follow from the declared target, the volatility budget and the measured trade rate. The split below is illustrative for a strategy needing a long window.

| Partition | Illustrative period | Use |
| --- | --- | --- |
| Development | 2006–2016 | Unlimited iteration, parameter search, the `GATE-032` pilot |
| Validation | 2017–2021 | Walk-forward, limited looks, all recorded |
| Holdout | 2022–2026 | `L` pre-registered looks total (`VAL-050`) |

## VAL-031 · Walk-forward

| Parameter | Value |
| --- | --- |
| Training window | 24 months |
| Testing window | 6 months |
| Step | 3 months |
| Purge gap | `VAL-032` |

The first three are declared configuration with rationale (`OPS-050`), subject to sensitivity testing.

## VAL-032 · Purge gap

**Maximum bounded holding period + 1 day**, from the strategy's own realised holding-time distribution.

Not the 99th percentile: the remaining 1% are exactly the long trades that bridge partitions, which is the leakage the gap exists to prevent. Where holding time is unbounded by design, the strategy needs a time-based exit before it can be walk-forward tested at all.

The `GATE-032` pilot produces an envelope value. The final value is recomputed from the real strategy and recorded per experiment.

## VAL-050 · Holdout policy is project-wide

The holdout may be served **`L` times across the whole project**, where `L` is small (one or two) and declared before the first look. Each serve increments a project-wide counter enforced independently of experiment id, strategy name or family.

**Error policy across looks.** A counter limits looks; it does not control error. The default is `L = 1`. If `L = 2` is declared, the project-wide false-positive rate is split across the looks — each look tested at `α / L` — and declared before the first look. Both looks are included in the `VAL-046` simulation.

**A new experiment id does not restore a spent holdout.** The information from the first look is already in the researcher's head. Once the counter is spent, further evidence requires **new data** — a later period that did not exist at earlier looks — not a new identifier. Earlier wording describing a holdout as spent "for that family" is superseded.

## VAL-051 · `HoldoutAuditGuard` is an audit convention, not a lock

A Python class in the same repository cannot prevent an operator reading the Parquet directly, editing the code, altering SQLite or rewriting local history. Naming it a lock overstates it, and an overstated control is worse than an acknowledged weak one.

What it does: refuses to serve holdout data without a pre-registration committed before the request, writes an irreversible ledger row on every serve, and enforces the `L` counter. It is tamper-evident, not tamper-proof.

A real boundary, if the project justifies one: holdout data in a separate encrypted store, a controlled CI job or custodian holding the only access, a fixed report as the sole output, no raw-row export, protected remote branch and immutable audit log. For a solo project this is likely disproportionate — in which case use the honest name.

Similarly, "append-only" SQLite is not append-only without database permissions, triggers and tamper-evident replication. Back it with chained record hashes and a protected remote.

---

## Multiple testing

## VAL-040 · Deflated Sharpe — the primary rule

```
SR₀ = E[max of N trials under the null]
DSR = Φ( (SR̂ − SR₀) / SE(SR̂) )      accept when DSR ≥ 0.95
```

**Units.** `T` is the number of return observations, not calendar years. `SR` inside the formula is periodic, matching the observation frequency; annualise only for reporting. Substituting an annualised Sharpe inflates the interaction terms by √252 and 252 and can drive the expression under the radical negative.

**`SR₀` is not the estimation error of a single strategy.** It is `s_ledger × E[max of N iid]`, where `s_ledger` is the empirical standard deviation of the trial Sharpes in the ledger.

## VAL-041 · Apply the correlation adjustment once

For equicorrelated trials, `E[max X] = √(1−ρ) × E[max of N iid]` — verified against simulation to within 0.02 at N = 100 and 500 for ρ = 0, 0.5, 0.85.

The empirical standard deviation measured within one ledger **already equals** `σ√(1−ρ)`, because the shared factor shifts every trial equally and contributes nothing to within-run dispersion. Multiplying it by `√(1−ρ)` again deflates twice: at σ = 0.60, ρ = 0.80, N = 500 the correct `SR₀` is 0.818 and the double-deflated value is 0.366, a hurdle less than half what it should be.

| Route | `SR₀` | When |
| --- | --- | --- |
| Empirical | `s_ledger × E[max of N iid]` | **Default** |
| Modelled | `σ × √(1−ρ) × E[max of N iid]`, `ρ` **measured** | Too few trials for a stable `s_ledger` |

Never both. Never with an assumed `ρ`.

**Do not use an effective-trial count.** Dividing `N` by `1 + ρ̄(N−1)` is Kish's formula for the variance of a mean, not an extreme value. It collapses to `1/ρ̄` regardless of `N` — about 1.2 at ρ̄ = 0.85 — and below `N = 2` the expected-maximum expression returns a **negative** `SR₀`, meaning every strategy passes.

## VAL-042 · The null maximum's spread is a diagnostic

With correlated trials the shared factor makes the maximum highly variable. For standard normal trials at ρ = 0.85, N = 500 over 20,000 replications: mean 1.17, standard deviation 0.94, 95th percentile 2.71 — the percentile is more than double the mean.

Report both beside every result. Acceptance is decided by `VAL-040` alone; the percentile is a diagnostic under `VAL-001` and does not veto a candidate. Earlier text using it as a threshold, or as an informal veto, is superseded.

## VAL-043 · Verify the false-positive rate

Before relying on the rule, simulate the whole acceptance procedure against zero-edge strategies at the intended trial count and correlation structure, and confirm the acceptance proportion is close to 5%. An acceptance rule whose actual error rate has never been measured is an assumption wearing a formula.

The equicorrelated model is an example, not a general solution. Where the structure is clustered — a parameter neighbourhood tightly correlated internally, loosely across families — simulate that structure.

## VAL-046 · Power of the complete procedure

`VAL-010` and `VAL-020` compute power for one test: is the mean positive. The procedure that actually accepts a strategy is different — select the best of `N` candidates, deflate its Sharpe (`VAL-040`), require the secondary conditions (`VAL-001`), then pass the holdout (`VAL-050`). Eighty per cent power for the first is not eighty per cent power for the second, and is usually much more.

Simulate the whole declared process twice, with the actual trial count, correlation structure and holdout rule:

| Simulation | Strategies | Reports |
| --- | --- | --- |
| False-positive rate | All zero-edge | Proportion accepted (`VAL-043`) |
| Detection power | One candidate carrying the declared edge, the rest zero-edge | Proportion accepted |

The closed-form figures are an initial estimate for Gate 2. The simulated power replaces them before `MILE-090`, and if it falls materially short, `VAL-013` applies again. The deflated Sharpe ratio (Bailey & López de Prado, 2014) is the statistical starting point for this rule, not a guarantee for this particular workflow — which is why the simulation is required.

## VAL-044 · Candidate Sharpe uncertainty needs a serial-correlation adjustment

`SE(SR̂)` from the iid skew/kurtosis expression understates uncertainty for serially correlated daily returns. Use a HAC/Lo adjustment or a block-bootstrap equivalent, and state which.

## VAL-045 · The trial universe, defined in three parts

Not every registered feature version is a comparable Sharpe trial. A feature created and discarded without producing a return series has no Sharpe observation and cannot contribute to `s_ledger`. Conflating audit completeness with the statistical `N` corrupts the deflation in both directions.

| Count | Contents | Used for |
| --- | --- | --- |
| **Audit count** | Every adaptive research choice: features, parameters, filters, regime definitions, discarded ideas | Honesty, review, the stopping rules at `VAL-074` |
| **Statistical candidate set** | Return series that competed under the same selection criterion | `N` and `s_ledger` in `VAL-040` |
| **Correlation structure** | Parameter-family clusters and cross-family relationships | The null model in `VAL-043` |

---

## Metrics

## VAL-030b · Metric units by question

| Question | Correct series |
| --- | --- |
| Expectancy, profit factor, win rate | Per-trade R-multiples |
| Drawdown depth and duration, return distribution | Daily portfolio returns, synchronous blocks |
| **Probability of breaching a daily or maximum loss limit** | **Intraday equity path**, positions and reset boundaries preserved inside each block (`CHAL-020`) |

Resampling a flat list of trade R-multiples treats concurrent trades as sequential, destroying the correlation that produces the worst drawdowns. Daily returns then miss intraday excursions entirely: a 6% intraday fall recovering to flat reads as zero against a 5% limit already breached.

## VAL-033 · Concentration is a diagnostic, not a rejection

Removing the top 5% of winners and watching expectancy turn negative does not establish those gains were luck — it establishes a right tail, which many genuine strategies have by design. Trend following's entire expectancy lives there.

What it should trigger: do the profitable episodes **recur across independent periods**? Does the sample support the estimated tail? Is the concentration consistent with the stated economic rationale? Recurrence rejects, not the removal arithmetic.

---

## Look-ahead enforcement

## VAL-060 · The truncation test

For any cutoff `T`, features computed on raw inputs truncated **by `available_at`** at `T` must be identical to features computed on full data and sliced at `T`.

**Truncate by availability, not by `ts_open`.** A bar's open timestamp is not when its information became usable: an H1 bar stamped 10:00 is unavailable until 11:00, and a calendar release has a scheduled time and an actual value arriving at different moments. Filtering on `ts_open < T` leaves in place anything stamped early but known late, and the test reports success.

**Compare keyed rows, not sets of timestamps.** Rows are identified by `(symbol, decision_time)`. Joining on time alone pairs unrelated symbols; comparing sets hides duplicated rows; an inner join drops the boundary rows being tested. Missing features are legitimate — `FEAT-002` returns `NO_TRADE` on them — so the test compares **null masks**, not the absence of nulls. Both outputs are cut at the same decision horizon.

```python
KEY = ["symbol", "decision_time"]


@given(cutoff=st.sampled_from(CUTOFF_TIMES))
def test_no_lookahead(cutoff, raw_inputs, pipeline):
    truncated_inputs = {
        k: v.filter(pl.col("available_at") <= cutoff) for k, v in raw_inputs.items()
    }
    truncated = pipeline(truncated_inputs).filter(pl.col("decision_time") <= cutoff)
    sliced = pipeline(raw_inputs).filter(pl.col("decision_time") <= cutoff)

    # A duplicated key is itself a defect, and would hide a multiplicity difference
    assert not truncated.select(KEY).is_duplicated().any()
    assert not sliced.select(KEY).is_duplicated().any()

    truncated, sliced = truncated.sort(KEY), sliced.sort(KEY)
    assert_frame_equal(truncated.select(KEY), sliced.select(KEY))  # same rows, same order

    for feature in FEATURE_COLUMNS:
        # Missing in one run and present in the other is look-ahead
        assert_series_equal(truncated[feature].is_null(), sliced[feature].is_null())
        assert_series_equal(truncated[feature], sliced[feature], check_exact=False, rtol=1e-9)
```

Cutoffs: at least 50, stratified across all four sessions, exact bar boundaries and one minute either side, Friday close, Sunday open, both DST transitions, month and year boundaries, known feed gaps, and high-impact releases.

## VAL-063 · Sizing arithmetic test

Blocking in CI (`VAL-002`). Risk is recomputed from the **actual filled volume**, after rounding to `volume_step`, and never exceeds the cap from `RISK-010`.

| Fixture | Expected |
| --- | --- |
| Raw volume 0.237 lots, step 0.01 | Rounds **down** to 0.23; risk recomputed from 0.23 |
| Partial fill of 0.10 of 0.23 | Risk recorded from 0.10; the unfilled reservation released per `RISK-013` |
| Headroom smaller than configured risk | Size bounded by headroom, not by `equity × risk_pct` |
| Rounded volume below `volume_min` | Rejected, not rounded up |
| Gap-adjusted stop wider than the placed stop | Size uses `effective_stop` (`RISK-020`) |

Tolerance: recomputed risk ≤ cap exactly, with no floating-point allowance in the direction of exceeding it.

## VAL-064 · Calibration availability test

Blocking in CI (`VAL-002`). No backtest reads a fitted artefact whose `available_from` postdates the bar being processed (`DATA-021`), subject to the mode rules at `COST-018`.

| Fixture | Expected |
| --- | --- |
| Gap table with `available_from` one bar after the bar processed | Read refused, test fails loudly |
| Causally updated table on an expanding window | Every read resolves to the version available at that bar |
| Counterfactual cost mode with a current cost model | Permitted, and the result is labelled counterfactual in the ledger |
| Causal replay mode with the same cost model | Refused |
| Artefact missing `available_from` | Refused — an unstamped artefact is treated as unavailable |

## VAL-061 · Leaking fixtures

A test that has never failed may be catching everything or nothing. Maintain fixtures that deliberately leak — a centred window, a full-series rank, a calendar `actual` read before release, a higher-timeframe join on `ts_open` — and assert the truncation test **fails** on each. A fixture that stops failing means the test was weakened, which is likelier than the leak being fixed.

## VAL-062 · Coverage registry

Fails if any registered feature has no truncation case.

## VAL-065 · The invariant, stated correctly

No feature value may depend on any input whose **`available_at` is later than the decision time** of the row it contributes to. Earlier property-test wording referring to `ts_open` is superseded — that formulation is exactly the bug `VAL-060` exists to catch.

---

## Ledger and stopping

## VAL-070 · Ledger schema

One row per experiment, append-only (`VAL-051` on what that is worth), in SQLite:

`experiment_id`, `timestamp`, `author`, `hypothesis`, `success_criteria`, `strategy_name`, `strategy_version`, `git_commit`, `data_version_hashes`, `feature_versions`, `config_hash`, `random_seed`, `partition_used`, `parameters`, `n_trades`, `n_effective`, `purge_gap_days`, `expectancy`, `ci_low`, `ci_high`, `profit_factor`, `max_drawdown`, `sharpe`, `deflated_sharpe`, `audit_count`, `candidate_set_size`, `conclusion`.

**Failed experiments are never deleted.** They are the denominator. A ledger of successes is a record of survivorship.

## VAL-071 · Pre-registration

Committed to version control before any run touching validation or holdout data, and checked by `VAL-051` against commit history rather than the working tree.

```yaml
experiment_id: ""
registered_at: ""            # must predate the data request
git_commit: ""
author: ""
hypothesis: ""
rationale: ""
strategy_name: ""
strategy_version: ""
parameters: {}               # exact values, no ranges
feature_versions: []
config_hash: ""
data_version_hashes: []
partition_requested: ""      # validation | holdout
planned_n_nominal: null
planned_n_effective: null
minimum_n_effective: null
success_criteria:
  metric: ""
  threshold: null
  interval_requirement: ""
accept_reject_rule: ""
audit_count_at_registration: null
candidate_set_size_at_registration: null
```

A null `threshold` or empty `accept_reject_rule` means no prediction was made, and the guard refuses.

## VAL-072 · Decision journal

Separate from the ledger, plain language, one line per significant choice: what was decided, why, and what would reverse it.

```
2026-10-04  Moved to M15 after c = 0.31 at 10-pip stops.
            Reversed if a venue with materially tighter spread is found.
2026-11-12  Extended history to 2008 rather than adding pairs.
            Pilot showed n_eff at 54% of nominal on 8 majors.
```

The ledger records what was run; the journal records what was thought, which is what cannot be reconstructed later and what shows when rationalisation started. Reread before every holdout look.

## VAL-073 · Enforcement when nobody is watching

Every other control here is mechanical. The stopping rules are not, and a solo researcher with a promising candidate will find a reason they do not apply.

**A reviewer who can say no** is the strongest control and the one a solo project usually cannot have. Where genuinely unavailable, the substitute is the immutable pre-registration at `VAL-071` — a prediction that cannot be quietly revised is most of what a reviewer provides. An adversarial read by a second AI is a weak substitute for the judgement half: it will not stop you, but it makes the reasons visible.

## VAL-074 · Stopping rules

Defined now, before investment creates a reason not to.

1. `GATE-024` fails at every viable stop distance and timeframe → venue too expensive.
2. `GATE-033` fails with no acceptable path → the question cannot be answered with available evidence.
3. A strategy family's validation expectancy interval spans zero after its **own** required `n_eff` (`VAL-010`), computed from the declared minimum economic effect — not a fixed 1,500.
4. Audit count exceeds its declared ceiling with no candidate surviving `VAL-040` → the search has become the overfit.
5. A candidate fails the holdout look → that candidate is finished. Not re-tuned. See `VAL-050` for what the holdout still permits.
6. The declared `max_calendar_time` (`PROD-010`) elapses with no candidate reaching the holdout stage.
7. Expected net value (`PROD-011`) falls below `min_acceptable_env` under central assumptions.

**What stopping means.** The honest answer was no for this configuration. That is a real result, arrived at cheaply, which is the whole justification for the apparatus.

## VAL-075 · Red flags requiring investigation

| Flag | Likely cause |
| --- | --- |
| One parameter value far better than neighbours | Overfit |
| One month or pair carries most profit | Not generalisable |
| 1.25× cost multiplier halves expectancy | Cost artefact |
| Results change after a refactor | Hidden state or silent semantic change |
| Development results far exceed validation | Leakage or overfit |
| Strategy revised repeatedly after holdout looks | Holdout contaminated |
| Fewer than 30 trades in a breakdown cell | Noise; do not act on it |
| Realised demo cost above modelled (`COST-020`) | Cost model understated; everything upstream inflated |
