# Operations Runbook

**Document 5 of 5** · Revision 14 · 19 September 2026
Requirement prefixes: `OPS`, `SEC`

Deployment, monitoring, incidents, recovery, security and configuration governance. Execution mechanics live in document 3 (`EXEC-*`); this document covers running the thing.

---

# Part A — Deployment and reliability

## OPS-001 · Execution topology

Declare before the main build (`MILE-002`):

| Item | Decision |
| --- | --- |
| Execution host OS and location | Windows host or VM if MT5 (`EXEC-001`); anywhere if cTrader |
| Research host | Separate from execution (`SEC-020`) |
| Service manager and automatic restart | Named, with restart policy and backoff |
| Network path and failover | Including what happens when it drops mid-order |
| Deployment mechanism | How code reaches the host, and who can push |

## OPS-002 · SLOs

| Metric | Objective | Breach action |
| --- | --- | --- |
| Quote staleness | Declared threshold | Halt (`EXEC-052`) |
| Signal-to-submit latency | Declared budget, measured per `EXEC-061` | Alert; investigate before it drifts into the fill assumption at `BT-010` |
| Reconciliation cycle | Declared interval | Halt on miss |
| Heartbeat interval | Declared | Watchdog halt |

## OPS-003 · RTO and RPO

State both, and test the restore rather than assuming it:

- **RTO** — how long until trading can resume after host loss. Until then, `EXEC-040` broker-side protection is the only thing standing between an open position and an unbounded loss.
- **RPO** — how much ledger and order-intent state may be lost. State it per failure scope:

| Failure | Order-intent RPO | Mechanism |
| --- | --- | --- |
| Process crash, host reboot | Zero | Local fsync before submission (`EXEC-053`) |
| Loss of the host's storage | Zero **only** with durable off-host acknowledgement before submission | Otherwise declare a non-zero RPO |
| Any | — | Broker history is ground truth; lost intents are rebuilt from it by order identity (`EXEC-030`) during reconciliation |

Local fsync does not survive destruction of the disk it wrote to.

## OPS-004 · Deployment while positions are open

Declare the policy. The default is: **do not**. Where unavoidable, the sequence is halt new entries, confirm broker-side protection on every open position, deploy, reconcile, then resume — and any deployment that cannot confirm protection first aborts.

## OPS-005 · Version pinning and drift

Terminal build, platform API version, and every Python dependency are pinned. A scheduled check compares the running versions against the pinned set and alerts on drift, because a silent terminal update can change fill or symbol behaviour under a system that believes it is unchanged.

---

# Part B — Monitoring and incidents

## OPS-010 · Alerting

The watchdog halts; alerting is what tells a human. They are different systems and both are required.

| Alert | Trigger | Urgency |
| --- | --- | --- |
| Halt fired | Any `EXEC-052` condition | Immediate |
| Daily loss above 40% of limit | Approaching the soft halt | Immediate |
| Reconciliation mismatch | Any divergence | Immediate |
| Broker-side protection unconfirmed | `EXEC-040` | Immediate |
| Spread blowout | Beyond the 99th percentile of the model | Immediate |
| Stale feed | Beyond `OPS-002` | Immediate |
| Clock drift | Beyond `OPS-011` | Same day |
| Rejected orders | More than two in a session | Same day |
| Version drift | `OPS-005` | Same day |
| Realised cost drifting above modelled | Rolling `COST-020` residual | Weekly digest |

**Routing.** Immediate alerts go to a channel that wakes you — push or SMS, not email. Same-day alerts go to a digest.

**Out of hours.** Declare what happens when an immediate alert fires at 03:00 on a Sunday: either `EXEC-052` hard-halt flattening is enabled so the position closes without you, or the account sits halted until you see it. Both are defensible; not choosing is not, and the choice determines whether flatten-on-halt should be configured at all.

## OPS-011 · Clock synchronisation

NTP on the execution host, drift monitored every cycle against broker server time. Beyond the declared threshold, halt. Daily-loss boundaries are evaluated in server time; a drifted host evaluates them at the wrong moment, and the failure is silent until it causes a breach. This is independent of `GATE-005`, which concerns the zone rather than the clock.

## OPS-012 · External health monitoring

The watchdog cannot report its own death. An independent check on a **different host and network** confirms the system is alive and alerts on silence. Without it, total host failure looks identical to a quiet market.

## OPS-020 · Incident record

Every incident writes a row to `monitoring/incidents` with a timestamp and three fields:

| Field | Content |
| --- | --- |
| `fired` | What triggered: halt condition, alert, or manual action |
| `state_found` | Account state, open positions, internal state at investigation |
| `changed` | What was altered — code, config, threshold, or nothing |

A `changed` of "nothing" is a legitimate outcome when investigation shows the system behaved correctly. Record why no change was needed, so a recurrence can be compared against that reasoning.

## OPS-021 · Response procedures

| Event | Response |
| --- | --- |
| Watchdog halt | Do not restart to see whether it clears. Read the reason, verify account state against the broker directly, record the incident, then restart |
| Reconciliation mismatch | Halt stands. Establish ground truth from the broker's own interface. Resolve manually; never by trusting internal state |
| Orphaned position | Close manually if it violates risk limits. Log ticket, volume, reason |
| Foreign position detected | Per `RISK-012`: veto rather than size around it. Incident |
| Broker disconnect | Wait the reconnect threshold. On reconnect, reconcile before any new order |
| Daily loss halt | No resumption until the next reset boundary in server time. No override |
| Host failure | Confirm broker-side protection held (`EXEC-040`). Verify positions before restarting anything |
| Manual override of any kind | Logged with timestamp, operator, reason and state. An unlogged override makes the ledger unreliable |

## OPS-022 · Strategy degradation

Live results are compared against backtest expectations on a rolling basis: realised cost against modelled (`COST-020`), realised expectancy against its confidence interval, realised trade rate against the pilot. Declare the divergence threshold that triggers investigation and the one that triggers a halt, **before** going live. Deciding afterwards is deciding while holding a position.

---

# Part C — Data and state durability

## OPS-030 · Backups

Ledger, pre-registrations, decision journal, manifests and configuration are backed up off-host. They are the record that makes any result defensible; losing them invalidates the research retrospectively.

**Restoration is tested, not assumed.** A backup never restored is a hypothesis.

## OPS-031 · Disk capacity

Venue capture (`COST-013`) and tick archives grow continuously. Capacity alarms with enough headroom to act, and a declared retention policy for raw quotes.

## OPS-032 · Ledger integrity

Per `VAL-051`, SQLite "append-only" is a convention unless enforced. Back it with chained record hashes and a protected remote, so tampering is at least evident.

## OPS-033 · Schema and config migration

Ledger and configuration schemas will change. Declare a migration mechanism and a compatibility policy, including what happens to results recorded under an older schema — they remain valid evidence and must remain readable.

---

# Part D — Security

## SEC-001 · Credentials

`.env` is a development convenience and is **not** adequate for a host with access to a funded trading account.

| Requirement | Detail |
| --- | --- |
| Storage | OS credential store or a secret manager. `.env` for local development only |
| Encryption at rest | Host disk encrypted |
| Scope | Trade-only API credentials where the platform offers scoping; withdrawal permissions excluded |
| Identity | The trading service runs under a restricted service account, not an interactive user |
| Rotation | Scheduled, plus immediately after host change, contractor access, or suspected exposure. **Rotation is tested** |
| IP allowlisting | Where the broker supports it, restrict API access to the execution host |
| Log hygiene | Account numbers and credentials redacted from all output including exception traces |

## SEC-002 · Repository hygiene

Pre-commit credential scanning, repeated in CI because hooks can be skipped. Dependency vulnerability scanning on a schedule. Signed, reproducible releases so the artefact on the host is the artefact that was reviewed.

## SEC-020 · Host isolation

The execution host runs the trading system and nothing else. No shared development use, no browsing, no unrelated services. Research and backtesting run elsewhere.

## SEC-030 · Account allowlist

`EXEC-060` is a security control as much as an operational one: it is what prevents a misconfiguration from trading a live account that was never authorised.

---

# Part E — Configuration governance

## OPS-050 · Declared constants, not universal truths

These are **configuration with rationale and sensitivity tests**, not laws. Each carries its value, why that value, and what would change it. Presenting a chosen round number as a derived constant is how an arbitrary choice becomes unquestionable.

| Constant | Default | Where |
| --- | --- | --- |
| Cost fraction ceiling | 0.25 | `GATE-022` |
| Walk-forward training / test / step | 24 / 6 / 3 months | `VAL-031` |
| Per-trade risk | 0.5% | `RISK-030` |
| Total open risk | 2.0% | `RISK-030` |
| Correlated cluster threshold | 0.7, rolling 60 days | `RISK-030` |
| Soft halt | 60% of daily limit | `EXEC-052` |
| Hard halt | Derived, 85% starting point | `EXEC-052` |
| Cost sensitivity range | 0.75× to 2.0× | `COST-017` |
| Ambiguity budget | 5% of exits | `BT-021` |
| Staleness tolerance | 2 bars | `FEAT-002` |
| Significance / power | one-sided 0.05 / 0.80 | `VAL-011` |
| Holdout serves `L` | 1; 2 only with each look at `α / L` | `VAL-050` |
| Quote-continuity thresholds | Per session, from observed inter-quote intervals | `DATA-013` |

Changing any of them is a decision-journal entry (`VAL-072`), and changing one after seeing results is a new experiment (`VAL-071`).

## OPS-040 · Rule changes during an active account

`GATE-008` covers re-verification before purchase. A firm may also change terms **while an account is live**, which is a different problem: the strategy was sized and validated against the old rules.

Procedure:

1. Halt new entries on notification.
2. Re-run `GATE-008` against the new terms and diff against the recorded set.
3. Re-run the challenge simulator (`CHAL-001`) under the new rules against the existing position book.
4. If the change alters pass probability or the risk model materially, treat continuing as a **new decision** with a journal entry — not as a continuation.
5. If the change makes the strategy non-compliant, flatten under `EXEC-040` rather than trading toward a breach.

## OPS-041 · Retention

Declare retention for logs, metrics, incident records, venue capture and tick archives. The ledger, pre-registrations and decision journal are retained **indefinitely** — they are the evidence base, and the multiple-testing denominator at `VAL-045` is only as honest as what survives.
