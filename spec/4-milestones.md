# Milestones

**Document 4 of 5** · Revision 14 · 19 September 2026
Requirement prefix: `MILE`

Build order. This document contains **no specifications** — every requirement lives in documents 1–3 and is referenced by ID. If you find a requirement restated here, that is a defect.

---

## MILE-001 · Rules of engagement

**One milestone at a time.** Do not start the next until the current Definition of Done is met.

**Every milestone ships five things:** source, tests, configuration, verification instructions, and a written list of known limitations.

**Stop conditions are real.** When one fires the honest move is to stop or change direction.

**Effort figures are relative sizes, not commitments.** Research milestones depend on what the data says and can take arbitrarily long.

## MILE-002 · Definition of ready for the main build

Stage C (`MILE-030` onward) does not begin until all of these hold. They are specification readiness, not code. Stage B runs in parallel and is not gated by this list.

- [ ] Every requirement exists in exactly one document; no restatements
- [ ] `PROD-010` objective **schema** implemented as validated configuration

**Firm-agnostic build.** The objective (`PROD-010`), the firm/programme/account triple (`PROD-023`), the rule values (`CHAL-010`) and the server zone (`GATE-005`) are **inputs to the software, not prerequisites for building it**. The software refuses to run research (`MILE-040` onward) until they are supplied and validated, because the research protocol requires them to be declared before any window is examined (`GATE-031`). Building does not.
- [ ] `CHAL-010` rule model **schema** defined; the software is firm-agnostic and loads any firm's rules from configuration
- [ ] `ARCH-002` event ordering approved, with the venue-specific reset/swap position as configuration
- [ ] `DATA-003` bid/ask schema, `DATA-012` gap policy, `DATA-014` calendar source, `DATA-021` availability rules approved
- [ ] `COST-013` capture plan with episode minimums, `COST-014` fallback and uncertainty reporting approved
- [ ] `VAL-001` primary endpoint, `VAL-011` alpha, `VAL-045` trial universe unambiguous
- [ ] `CHAL-021` seam handling chosen
- [ ] `RISK-010` headroom sizing and margin stop-out specified
- [ ] `EXEC-040` broker-side protection specified
- [ ] `EXEC-010` adapter contract complete for the chosen account mode
- [ ] `VAL-051` holdout protection either a real boundary or honestly named
- [ ] `OPS-001` deployment, `OPS-003` RTO/RPO, `OPS-004` deploy-with-positions policy, `OPS-030` backups specified

---

# Stage A — Specification readiness

**Effort:** 1–2 weeks. No code.

## MILE-010 · Charter and contracts

Complete `MILE-002`. Stage B may run alongside; Stage C does not start until this is done.

---

# Stage B — Cheap external gates

These can run in parallel and are the only work permitted while Stage A completes.

## MILE-020 · Firm, programme and account type

**Implements:** `GATE-001`, `GATE-002`, `GATE-003`, `GATE-004`, `GATE-005`, `GATE-006`, `GATE-007`, `GATE-008`, `PROD-023`
**Produces:** `config/challenge_rules.yaml` per `CHAL-010`, due-diligence record
**Done when:** `GATE-007` checklist complete
**Stops if:** `GATE-002` disqualifying answer → different triple
**Effort:** 1–2 days, mostly reading

## MILE-021 · Capability probe

**Implements:** `EXEC-020`, `GATE-010`, `GATE-011`
**Produces:** `execution/probe.py`, persisted probe output
**Done when:** probe runs and persists; `trade_expert` true; every documented value compared against observed with discrepancies recorded; `stops_level` checked against intended stop distances
**Stops if:** automation disabled on this server, or `stops_level` incompatible with every viable stop distance
**Effort:** ~150 lines, half a day plus host setup

## MILE-022 · Venue quote capture

**Implements:** `COST-013`
**Produces:** capture service, `data/raw/venue_quotes/`
**Why now:** needs months of calendar time and continues throughout the project. Starting late delays `MILE-040` (cost tables) and `MILE-070` (the residual protocol) by exactly the delay.
**Done when:** running continuously with completeness monitored; episode counts per bucket tracked against `COST-013` minimums
**Stops if:** nothing — this gathers evidence
**Effort:** a few hundred lines, then continuous

## MILE-023 · Execution canary

**Implements:** `ARCH-002` verification, `EXEC-061`, `CHAL-012` trading-day event
**What:** a tiny controlled sequence of demo orders to observe timestamps, fill behaviour, fee posting, swap timing, reset boundary behaviour and reconciliation.
**Explicitly not:** evidence about any strategy. This measures the venue, not an edge.
**Done when:** the venue-specific parts of `ARCH-002` are settled from observation
**Effort:** 1 day

---

# Stage C — Technical vertical slice

Prove the pipeline end to end on a **small dataset** — two symbols, a short window — before scaling. A defect surfaces identically on two symbols as on twelve and costs a fraction to find (`PROD-021`).

## MILE-030 · Ingestion and data layer

**Implements:** `DATA-001`, `DATA-002`, `DATA-003`, `DATA-004`, `DATA-010`, `DATA-011`, `DATA-012`, `DATA-013`, `DATA-014`, `DATA-020`, `DATA-021`
**Done when:** two symbols, short window, ingested and validated; manifests written; resampling produces both quote sides; gap policy applied and recorded
**Effort:** 1 week

## MILE-031 · Feature engine and look-ahead enforcement

**Implements:** `FEAT-001`, `FEAT-002`, `FEAT-003`, `FEAT-004`, `FEAT-005`, `FEAT-010`, `VAL-060`, `VAL-061`, `VAL-062`, `VAL-065`
**Done when:** truncation test passes over 50+ stratified cutoffs; leaking fixtures all fail it; coverage registry passes; all three blocking in CI per `VAL-002`
**Effort:** 1 week. The highest-value code in the project.

## MILE-032 · Backtest engine and cost model

**Implements:** `ARCH-001`, `ARCH-002`, `ARCH-003`, `COST-001`, `COST-010`, `COST-011`, `COST-012`, `COST-013`, `COST-014`, `COST-015`, `COST-016`, `COST-017`, `COST-018`, `BT-001`, `BT-010`, `BT-011`, `BT-012`, `BT-020`, `BT-021`, `BT-030`, `BT-040`, `BT-041`
**Done when:** golden path committed (`BT-040`); differential tests pass against hand-worked fixtures (`BT-041`); a fixture proves a short stop fires on an ask spike the bid never reaches (`BT-012`); ambiguity budget instrumented (`BT-021`)
**Effort:** 2–3 weeks. The largest single build.

## MILE-033 · Toy strategy, risk and rules end to end

**Implements:** `STRAT-001`, `STRAT-002`, `RISK-001`, `RISK-010`, `RISK-011`, `RISK-012`, `RISK-013`, `RISK-020`, `RISK-030`, `CHAL-010`, `CHAL-011`, `CHAL-012`, `CHAL-013`, `VAL-063`, `VAL-064`
**What:** one deliberately trivial strategy — not a research candidate — carried through sizing and rule evaluation to prove the path.
**Done when:** `VAL-063` sizing arithmetic passes including partial fills; the simultaneous-trigger fixture passes (`RISK-013`); `VAL-064` calibration availability passes; challenge rules evaluate against hand-worked examples
**Effort:** 1 week

---

# Stage D — Gates and research

## MILE-040 · Scale data and close Gate 1

**Needs:** objective and firm configuration supplied and validated (`PROD-010`, `CHAL-010`, `GATE-005`); the software refuses to start without them
**Implements:** `GATE-020`, `GATE-021`, `GATE-022`, `GATE-023`, `GATE-024`, remaining `DATA-001` acquisition
**Done when:** cost table per pair, session and candidate timeframe with `COST-014` uncertainty reporting; `c` computed at intended stop distances; `c < 0.25` at the chosen timeframe
**Stops if:** `c > 0.25` everywhere → widen stops, higher timeframe, or change venue
**Note:** timeframe is decided here on cost grounds (`GATE-023`), not on power
**Expect:** after roughly two months of capture, news-release and other rare cells will still be on `COST-014` fallback bounds. Decisions here must hold at the upper fallback bound, and the cells are revisited at `MILE-070`
**Effort:** 1–2 weeks

## MILE-041 · Close Gate 2

**Implements:** `GATE-030`, `GATE-031`, `GATE-032`, `GATE-033`, `VAL-010`, `VAL-011`, `VAL-013`, `VAL-014`, `VAL-020`, `VAL-021`
**Produces:** power pilot, feasibility envelope, declared target recorded before the window was examined
**Done when:** `GATE-033` checklist complete; timeline re-estimated now that platform, timeframe and signal rate are known (`MILE-102`)
**Stops if:** no acceptable path under `VAL-013`
**Effort:** 2–3 days

## MILE-050 · Validation harness

Built **before** there is a result you want to believe.

**Implements:** `VAL-001`, `VAL-002`, `VAL-030`, `VAL-030b`, `VAL-031`, `VAL-032`, `VAL-033`, `VAL-040`, `VAL-041`, `VAL-042`, `VAL-043`, `VAL-044`, `VAL-045`, `VAL-050`, `VAL-051`, `VAL-070`, `VAL-071`, `VAL-072`, `VAL-073`, `VAL-075`
**Done when:** guard refuses serve `L + 1`, an unregistered serve and an incomplete pre-registration; `VAL-043` false-positive rate simulated and reported; portfolio bootstrap preserves concurrent-pair drawdown on a three-pairs-lose-together fixture; intraday breach path (`VAL-030b`) implemented
**Effort:** 2 weeks

## MILE-055 · Challenge simulator

**Implements:** `CHAL-001`, `CHAL-011`, `CHAL-020`, `CHAL-021`, `CHAL-030`, `CHAL-031`, `CHAL-032`, `PROD-011`, `VAL-046`
**Needs:** `MILE-033` rule evaluation, `MILE-050` block bootstrap and intraday paths
**Done when:** hand-worked fixtures pass for — a breach reached intrabar but not at close; a daily reset crossed with a position open; the floating-profit ratchet (`GATE-003`); a block seam (`CHAL-021`); target reached with positions still open; phase 1 → phase 2 → funded modelled sequentially. Zero-edge calibration reported (`CHAL-032`). Expected net value computed under the attempt policy (`PROD-011`)
**Effort:** 1–2 weeks

## MILE-060 · First real strategy

**Implements:** `STRAT-010`, `STRAT-020`, `STRAT-021`
**Done when:** runs on development partition; falsification criterion registered **before** the run; random-entry and shuffled-return diagnostics recorded in the ledger
**Random-entry failure:** a diagnostic under `VAL-001` — it triggers investigation and a ledger entry, and does not by itself stop the milestone. It is a research result, not a bug (`VAL-002`). Do not tune until it passes.
**Stops if:** the primary rule (`VAL-040`) or a secondary requirement (`VAL-001`) fails on the development partition
**Effort:** 1 week

## MILE-070 · Demo reconciliation

Before the holdout look, because an understated cost model inflates every backtest upstream and the holdout is the least replaceable resource.

**Implements:** `EXEC-001`, `EXEC-010`, `EXEC-030`, `EXEC-031`, `EXEC-032`, `EXEC-033`, `EXEC-040`, `EXEC-050`, `EXEC-051`, `EXEC-052`, `EXEC-053`, `EXEC-060`, `EXEC-061`, `EXEC-062`, `COST-020`, `OPS-010`, `OPS-011`, `OPS-012`, `OPS-020`, `OPS-021`, `OPS-022`
**Done when:** watchdog halts when the strategy process is killed; broker-side protection confirmed on every position; reconciliation detects an injected mismatch; `COST-020` residual protocol satisfied with its per-category minimums
**Stops if:** residual test fails → recalibrate and re-run validation. Do not proceed to the holdout
**Effort:** 2–3 weeks

## MILE-080 · Decide whether more families are worth building

**Implements:** `PROD-021`, `STRAT-011`, `STRAT-012`, `STRAT-013`
Only after one family has completed `MILE-060` and `MILE-070`. If the first family failed, a second is a fresh hypothesis, not a retry — and `VAL-074` rule 3 may already apply.
**Effort:** 2–3 weeks per additional family, if justified

## MILE-090 · Holdout look

**Implements:** `VAL-050`, `VAL-071`, `VAL-074`, `PROD-011`
**Needs:** `MILE-055`; simulated power of the complete procedure (`VAL-046`) recorded
**Done when:** pre-registration committed before the serve; one serve consumed from `L`; result recorded against the pre-written rule; deflated Sharpe reported beside the trial count; expected net value computed
**Stops if:** the candidate fails → finished, not re-tuned
**Effort:** 1 day. Everything before it resolves here.

---

## MILE-095 · Evaluation deployment and go/no-go

The last in-scope step (`PROD-020`). Only after a candidate has passed `MILE-090`.

**Implements:** `PROD-011`, `EXEC-062`, `GATE-008`, `GATE-010`, `GATE-011`, `OPS-001`, `OPS-002`, `OPS-003`, `OPS-004`, `OPS-005`, `OPS-011`, `OPS-012`, `OPS-022`, `OPS-030`, `OPS-040`, `SEC-001`, `SEC-002`, `SEC-020`, `SEC-030`
**Needs:** `MILE-055`, `MILE-070`, `MILE-090` passed
**Done when:**
- Rules re-verified on the purchase date (`GATE-008`)
- Expected net value recomputed from the holdout result and the simulator, and at or above `objective.min_acceptable_env` under central assumptions (`PROD-011`)
- Purchase recorded as a decision-journal entry (`VAL-072`) before the fee is paid
- Capability probe re-run on the **evaluation** server and diffed against the demo server (`EXEC-062`)
- Evaluation account added to the allowlist (`SEC-030`); execution host deployed and hardened (`OPS-001`, `SEC-001`, `SEC-020`)
- Degradation thresholds declared before the first trade (`OPS-022`)
- Restore from backup tested (`OPS-030`)
**Stops if:** expected net value falls below the floor — do not buy the evaluation, however good the strategy looks
**Effort:** 1 week, then the evaluation period itself

## MILE-100 · Sequence

```
Stage A   MILE-010  specification readiness ─────────────┐
                                                          │
Stage B   MILE-020  firm/programme/account               │ parallel
          MILE-021  capability probe                      │
          MILE-022  venue capture (continuous) ───────────┤
          MILE-023  execution canary                      │
                                                          ▼
Stage C   MILE-030  data layer (small dataset)
          MILE-031  features + look-ahead enforcement
          MILE-032  backtest engine + cost model
          MILE-033  toy strategy end to end
                        │
Stage D   MILE-040  scale data, Gate 1 ── stops if c > 0.25
          MILE-041  Gate 2 ─────────────── stops if no path
          MILE-050  validation harness
          MILE-055  challenge simulator
          MILE-060  first real strategy ── stops if no entry signal
          MILE-070  demo reconciliation ── stops if cost understated
          MILE-080  more families? (optional)
          MILE-090  holdout look ───────── decision
          MILE-095  evaluation go/no-go ── stops if value below floor
```

## MILE-101 · Where it can stop, in the order you find out

| When | What | Meaning |
| --- | --- | --- |
| `MILE-020` | Disqualifying rule | Wrong triple |
| `MILE-021` | Automation off, or `stops_level` too wide | Wrong server or wrong design |
| `MILE-040` | `c > 0.25` everywhere | Venue too expensive |
| `MILE-041` | No acceptable power path | Question unanswerable with available evidence |
| `MILE-060` | No entry signal | Idea carries no information |
| `MILE-070` | Realised cost above modelled | Everything upstream inflated |
| `MILE-090` | Holdout fails | That candidate is finished |
| `MILE-095` | Expected net value below floor | Do not buy the evaluation |
| Any time before `MILE-095` | Expected net value below floor (`PROD-011`) | The arrangement does not pay |

Four are reachable within weeks of starting. That is deliberate: the answers most likely to be "no" arrive first and cheapest.

## MILE-102 · Timeline

| Stage | Effort |
| --- | --- |
| A — specification readiness | 1–2 weeks |
| B — external gates | 2 days, plus continuous capture |
| C — vertical slice | 5–6 weeks |
| D — gates and research | 8–11 weeks, plus unbounded research time |

Roughly **three and a half to four and a half months to an evaluation go/no-go**, assuming nothing fails, with venue capture overlapping throughout. Research time at `MILE-060` is unbounded because it depends on what the data says. Re-estimate once `MILE-020` and `MILE-041` have fixed the platform, timeframe and expected signal rate (`MILE-002`).

## MILE-103 · The first three actions

1. Build the project skeleton, with the objective and firm configuration schemas as validated inputs.
2. Build and run the capability probe (`MILE-021`) against the IC Markets demo on the local MetaTrader 5.
3. Start venue capture (`MILE-022`) the same day — it needs months of calendar time.

The objective and the firm are supplied as configuration before `MILE-040`.
