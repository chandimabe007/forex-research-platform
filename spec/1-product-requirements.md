# Product Requirements

**Document 1 of 5** · Revision 14 · 19 September 2026
Requirement prefix: `PROD`

Scope, objective, economics and non-goals. Every other document derives from this one. Where a requirement is stated here, no other document restates it — they reference the ID.

---

## PROD-001 · What this project is

A **measuring instrument** for systematic forex trading ideas. It tests whether an idea survives realistic costs and tells you honestly. It does not supply ideas and it does not make money by itself.

Four strategy families are included as starting hypotheses (`STRAT-010` to `STRAT-013`). They are ordinary, widely traded ideas, chosen because they are ordinary. The expected outcome is that none of them clears its costs.

## PROD-002 · What it is not

- Not a strategy, and not a source of one.
- Not a live-trading system for personal capital. Scope ends at evaluation execution (`PROD-020`).
- Not a system that improves strategies automatically. See `PROD-003`.

## PROD-003 · The automation boundary

| Automated | Not automated |
| --- | --- |
| Testing ideas at scale | Deciding what to try next |
| Computing costs and metrics | Deciding when to stop |
| Walk-forward, bootstrap, Monte Carlo | Judging whether a result is believable |
| Parameter search within a pre-declared grid | Adjusting parameters until the number turns positive |
| Re-running everything when an assumption changes | Reacting to live results by retuning |

The right column is not an engineering gap. Automating it makes overfitting faster: a script trying ten thousand variants overnight finds an excellent equity curve by morning, and it is noise.

---

## Economic objective

## PROD-010 · Declare the objective before building

The project optimises for a **declared economic outcome**, not for passing an evaluation. Passing is an intermediate step whose value depends on what follows. Fill this in during `MILE-010` and record it in the ledger:

```yaml
objective:
  target: null            # evaluation_pass | first_payout | sustained_payouts_12m
  horizon: null           # the fixed period over which value is measured, e.g. 12 months
  target_annual_return: null   # used by GATE-031
  volatility_budget: null      # annual; used by GATE-031 and VAL-020
  retry_on_failure: null  # true | false — the attempt policy
  account_size: null
  account_type: null      # standard | swing | other
  max_attempts: null      # how many evaluation fees you will spend
  max_fee_budget: null
  max_calendar_time: null
  min_acceptable_env: null  # minimum expected net value below which this is not worth doing
```

## PROD-011 · Report expected net value, not pass probability

Pass probability alone cannot justify the project. The challenge simulator (`CHAL-001`) produces it; this requirement converts it into a decision:

Value is defined over the **whole attempt policy and a fixed horizon**, so receipts and costs cover the same attempts:

```
p   = pass probability per attempt (CHAL-001), carried as a range
K   = attempts permitted = min(max_attempts, max_fee_budget ÷ fee, attempts that fit in horizon)

P(funded within policy) = 1 − (1 − p)^K
E[attempts used]        = Σ_{k=1..K} (1 − p)^(k−1)

E[total receipts] = P(funded within policy)
                    × E[payouts received before horizon ends | funded]
E[total costs]    = fee × E[attempts used]
                    + infrastructure and data × horizon
                    + time cost

expected net value = E[total receipts] − E[total costs]
```

`E[payouts | funded]` is itself conditional on the funded-stage rules, on payout being honoured, and on the time left in the horizon after the attempts that preceded funding. Attempts are not truly independent — they share a strategy and may share a regime — so `p` is a scenario range, not a point.

`P(payout | pass)` is not estimable precisely and must be carried as a scenario, not a point estimate. Payout denial and firm failure are explicit scenarios (`PROD-012`), not residuals.

**If expected net value is below `objective.min_acceptable_env` under central assumptions, the project does not proceed to funded execution** regardless of how good the strategy looks. A profitable strategy inside an unprofitable arrangement is still an unprofitable arrangement.

## PROD-012 · Scenario risks carried explicitly

| Risk | Treatment |
| --- | --- |
| Payout denied or delayed | Scenario with a stated probability range, not a point estimate |
| Firm ceases trading or changes terms mid-account | Scenario; see `OPS-040` for the in-flight rule-change procedure |
| Regulatory change closes the route | Scenario; the research finding survives, the route does not (`PROD-013`) |
| Account terminated under a subjective clause | Scenario; requires the termination clauses recorded at `GATE-004` |

## PROD-013 · Regulatory context

Retail proprietary trading is under active regulatory attention. The FCA applies financial-promotions and authorisation scrutiny with a 30:1 retail leverage cap and targets unauthorised solicitation of UK retail clients; ESMA acts through product intervention with the same leverage cap; the CFTC's My Forex Funds case was dismissed with prejudice in May 2025 and the agency sanctioned for litigation misconduct — a procedural outcome, not a ruling on the merits, so the question of whether retail prop trading falls within its jurisdiction was never decided. Regulators increasingly examine the economic substance of simulated accounts rather than accepting the demo label.

Consequences, all already in the design: the platform adapter is pluggable (`EXEC-001`), rules carry verification dates (`GATE-006`), and the research output — whether an edge exists after costs — is not firm-specific. If the route closes, the finding survives.

This is a note on risk, not legal advice.

---

## Scope

## PROD-020 · In scope

Research, validation, and demo or evaluation execution for **one operator, one account, one strategy at a time**. Multi-account, multi-operator and portfolio allocation are out of scope for version 1 and no requirement anticipates them.

## PROD-021 · Version 1 exclusions

Deleted from version 1 and reconsidered only after a deterministic strategy has survived validation *and* demo reconciliation:

| Excluded | Condition for reconsidering |
| --- | --- |
| LLM/AI filter layer | A registered unstructured-data hypothesis that structured features cannot express |
| Strategy families 2–4 | One family completes `MILE-060` and `MILE-070` first (`MILE-080`). Additional families come before the holdout look, because the holdout is project-wide (`VAL-050`) |
| White's Reality Check, Hansen's SPA | One primary acceptance rule (`VAL-040`) has a verified false-positive rate |
| Netting/hedging dual support, exotic order types | The capability probe (`GATE-011`) shows the chosen platform requires them |
| Full multi-symbol tick archive | Ingestion proven on two symbols and a short window (`MILE-030`) |

## PROD-022 · The three unknowns

No plan can supply these. They come from the outside world and each has a gate.

| Unknown | Determines | Gate |
| --- | --- | --- |
| Which firm, programme and account type | Platform, rules, symbols, server time, spread regime, automation policy | `GATE-001` |
| What venue cost data is obtainable | Whether any timeframe is falsifiable | `GATE-020` |
| Whether the sample can resolve the declared edge | Window length, universe, accepted power | `GATE-030` |

## PROD-023 · Programme and account type, not just firm

Selection is of a **firm + programme + account type** triple. Rules differ across all three. At FTMO the Swing account removes news and weekend restrictions but is available only with the 2-Step programme, cannot be switched to after purchasing Standard, and carries lower leverage. Choosing "FTMO" is not a decision; choosing "FTMO 2-Step Swing" is.

---

## PROD-030 · Known weaknesses of this specification

Listed so reviewers engage with substance rather than rediscovering them. A review reporting only these has not reviewed the documents.

| Weakness | Status |
| --- | --- |
| The four strategy families are ordinary and widely traded | Deliberate. They are hypotheses expected to fail |
| Nothing has been built, run or measured | Every figure is a design-time estimate |
| Trade-rate assumptions drive every absolute figure in `VAL-020` | The timeframe-invariance conclusion is algebraic and survives; the numbers do not |
| The equicorrelated null is a modelling convenience | Real trial families are clustered. Simulate the actual structure where known |
| Acceptance rule false-positive rates are specified but unmeasured | An assumption until simulated (`VAL-043`) |
| Cost thresholds and halt levels are chosen round numbers | Declared as configuration with rationale (`OPS-050`), not derived |
| `HoldoutAuditGuard` is an audit convention, not an access control | Stated honestly at `VAL-051` rather than overclaimed |
| The whole evaluation route may close for regulatory reasons | `PROD-013`. Mitigated, not eliminated |
| Demo evidence is provisional | Evaluation and funded servers may execute differently (`EXEC-062`) |

## PROD-031 · Claims deliberately not made

Earlier drafts asserted these; they are withdrawn as unsupported:

- That statistical power favours any timeframe. It does not (`VAL-021`).
- That a 10% annual target on a 10% volatility budget needs 13.1 years. That figure used a two-sided constant against a one-sided test and applied dependence to the sample but not to the volatility. The consistent figure is ~6 years (`VAL-020`).
- That timeframe differs only in cost. Equal detectability at equal Sharpe does not imply equal attainable Sharpe, dependence or tails (`VAL-021`).
- That look-ahead is impossible by construction. The tests establish it, not the design (`BT-001`).
- That most published M5 results are backtest artefacts. Plausible, unmeasured, and not needed for any decision here.
- That currency crosses add "almost no" information. They are linear combinations of the majors, which is arithmetic; *how much* independent information remains is an empirical question for `GATE-032`.

---

## Document map

| Document | Prefix | Contains |
| --- | --- | --- |
| 1. Product requirements | `PROD` | This document |
| 2. Research protocol | `GATE`, `VAL` | Gates, power, partitions, acceptance rules, trial accounting, stopping rules |
| 3. Architecture | `DATA`, `FEAT`, `COST`, `BT`, `STRAT`, `RISK`, `CHAL`, `EXEC` | Data and event model, engines, interfaces, contracts |
| 4. Milestones | `MILE` | Build order, referencing IDs only |
| 5. Operations runbook | `OPS`, `SEC` | Deployment, monitoring, incidents, recovery, security |

**Single source rule.** Every requirement lives in exactly one document under one ID. Other documents reference the ID and never restate the content. A restatement is a defect: report it rather than reconciling the copies.
