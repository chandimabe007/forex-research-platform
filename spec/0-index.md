# Forex Research Platform — Specification Set

**Index** · Revision 14 · 19 September 2026

Five documents. Every requirement lives in exactly one of them under one ID. Nothing is restated; documents reference each other by ID.

| # | Document | Prefixes | Read it for |
| --- | --- | --- | --- |
| 1 | Product requirements | `PROD` | What the project is and is not, the economic objective, scope, known weaknesses |
| 2 | Research protocol | `GATE`, `VAL` | The three gates, statistical power, partitions, deflated Sharpe, holdout, look-ahead testing |
| 3 | Architecture | `ARCH`, `DATA`, `FEAT`, `COST`, `BT`, `STRAT`, `RISK`, `CHAL`, `EXEC` | Everything that gets built: data, features, costs, backtest engine, strategies, risk, challenge rules, execution |
| 4 | Milestones | `MILE` | Build order. Contains no specifications — only ID references |
| 5 | Operations runbook | `OPS`, `SEC` | Deployment, monitoring, incidents, backups, security, declared constants |

## How to use this with an AI developer

1. Give it **all five documents** (or the single combined file) at the start of the session.
2. Give it one milestone from document 4 at a time, e.g. "Implement `MILE-031`."
3. The milestone lists the requirement IDs it implements. The AI looks each ID up in documents 1–3 — that is the full instruction.
4. Check the milestone's **Done when** line yourself before moving on.

## Rules for editing

- **One place only.** A change to a requirement is made at its ID and nowhere else.
- **A restatement is a defect.** If a requirement's content appears in two places, report it; do not reconcile the copies.
- **New requirements get new IDs.** IDs are never reused or renumbered.
- **Check references after every edit.** Every referenced ID must be defined exactly once.
- **Run the checker after every edit.** `tools/check_references.py` must pass before a revision is issued.

## Integrity at this revision

192 requirement IDs defined · 180 referenced · 0 dangling · 0 duplicates · 133 of 133 buildable requirements assigned to a milestone.

This line is **generated**, not asserted: run `python tools/check_references.py` from the spec directory. It fails on any duplicate, dangling reference, unassigned requirement, or range in a milestone `Implements:` line, and regenerates `coverage-report.md`.

## Revision 13 changes

| Change | IDs |
| --- | --- |
| Sizing and calibration tests promoted to full requirements with fixtures | `VAL-063`, `VAL-064` |
| Evaluation deployment and go/no-go milestone added — the last in-scope step | `MILE-095` |
| Milestone `Implements:` lines list IDs explicitly; no ranges | `MILE-*` |
| My Forex Funds outcome stated as procedural, not a merits ruling | `PROD-013` |
| Wrong stopping-rule reference corrected | `VAL-045` → `VAL-074` |
| Capture dependency pointed at the milestones it actually delays | `MILE-022` |
| Correlation matrix placed under the availability rules | `RISK-030` |
| Rare cost cells expected to be on fallback bounds at Gate 1 | `MILE-040` |
| Reference checker committed | `tools/check_references.py` |

## Revision 12 changes

| Area | Change | IDs |
| --- | --- | --- |
| Power | One covariance model; one-sided constant; 13.1 years withdrawn, ~6 years at Sharpe 1.0 | `VAL-020`, `VAL-021`, `GATE-023` |
| Acceptance | Power of the full procedure simulated; error split across holdout looks | `VAL-046`, `VAL-050` |
| Contradictions | Random-entry and null-percentile roles made consistent; guard refuses serve `L + 1`; family sequencing aligned | `VAL-042`, `MILE-050`, `MILE-060`, `PROD-021` |
| Milestones | Readiness gate no longer circular; challenge simulator given its own milestone | `MILE-002`, `MILE-010`, `MILE-055` |
| Risk | Pending and in-flight orders reserve risk atomically | `RISK-013` |
| Costs | One fill equation; causal conditioning only; causal replay vs counterfactual cost modes | `COST-015`, `COST-018` |
| Look-ahead test | Keyed on symbol and time; null masks compared | `VAL-060` |
| Data | Completeness from feed continuity and outages, not tick count | `DATA-003`, `DATA-013` |
| Economics | Value over the whole attempt policy and horizon; missing return and volatility fields added | `PROD-010`, `PROD-011` |
| Wording | Four overclaims removed; RPO stated per failure scope | `BT-001`, `CHAL-030`, `OPS-003`, `OPS-020` |

## Where to start

`MILE-103`: write the `PROD-010` objective block, read the full terms for your candidate firm/programme/account triples, then open a demo account and start venue quote capture (`MILE-022`) the same day.
