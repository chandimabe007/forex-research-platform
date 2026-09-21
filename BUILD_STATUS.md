# Build Status — Clean-Room Rebuild

**Date:** 2026-09-20 · **Spec revision implemented:** 14 (2026-09-19)

This repository is a clean-room reimplementation built **only** from the seven
markdown documents in `spec/` and the root `README.md` / `CLAUDE.md`. No code,
configuration, or data was copied from any other project. Where the spec is
ambiguous, the choice made is recorded here.

## Implemented milestones

| Milestone | Scope | Status |
|---|---|---|
| MILE-001/002 | Skeleton, packaging, lint, spec checker | done |
| MILE-010 | Core: typed units, ARCH-002 event ordering, ARCH-003 state machine | done |
| MILE-020 | Config schemas: PROD-010 objective, CHAL-010/011 rules, loader | done |
| MILE-021 | Execution: allowlist (EXEC-060), probe (EXEC-020), adapter, CLI | done; probe outcome **ok** against the live LHFXSA demo (record in data/probe/, 0 discrepancies) |
| MILE-022 | Capture service: COST-013 quote persistence, monitor, report | done; live smoke run persisted 60 quotes across 4 symbols with clean resume |
| MILE-023 | Execution canary: gated demo order sequence, reconciliation, redacted record | done; live run outcome **ok_with_discrepancies** (settlement-lag flag) — records in data/canary/ |
| MILE-030 | Data layer: DATA-001/020/021, validation, resample, sessions, calendar | done |
| MILE-031 | Feature engine: FEAT-*, truncation, leak, coverage enforcement | done |
| MILE-032 | Cost model COST-*, backtest engine BT-*, golden fixture | done |
| MILE-032 (completion) | Stop exits use the COST-012 stop-exit sampler end to end; weekend-gap exits counted separately (BT-030) | done |
| MILE-033 | Risk sizing RISK-*, challenge evaluator CHAL-*, toy strategy, end-to-end | done |

**Not yet implemented** (deliberately — they need real data or real decisions):
- `VAL-040` Deflated Sharpe computation (needs the full trial-count ledger; the
  plumbing for one-primary-rule exists in `config/objective.example.yaml`).
- `VAL-050` holdout ledger (requires the experiment registry workflow).
- Walk-forward selection loop (`VAL-030`) beyond the documented protocol.
- Swap timing and reset-boundary observation (`CHAL-012`): the canary records
  them as not-observed; read them from deal history at an overnight/reset canary run.

## Verification

```bash
# 1. Spec integrity: 192 IDs, 0 dangling, 0 duplicates, 154/154 assigned
python spec/tools/check_references.py

# 2. Full test suite (102 tests, ~2 s)
.venv/Scripts/python -m pytest tests -q

# 3. Lint
.venv/Scripts/python -m ruff check src tests

# 4. Probe (requires MT5 terminal running & logged in; fails closed otherwise)
.venv/Scripts/python -m forex_research.execution.cli --symbols EURUSD GBPUSD --config config

# 5. Capture service (continuous; writes data/raw/venue_quotes)
.venv/Scripts/python -m forex_research.capture --config config/capture.yaml

# 6. Offline pipeline demo (no MT5 required; synthetic data)
.venv/Scripts/python scripts/demo_backtest.py

# 6. Execution canary (MILE-023; allowlist-gated demo orders; one round trip)
.venv/Scripts/python -m forex_research.execution.canary --symbol EURUSD --config config
```

## Key interpretation decisions

- **ARCH-002 event ordering** is enforced structurally: the engine resolves
  within a single tick as *decisions → pending-order triggers → fills → exits*,
  and `core/events.py` asserts the order on every tick.
- **BT-012 (spread cost is charged on the exit side)** — stops fill against the
  ask for longs and bid for shorts; the golden fixture in
  `tests/golden/` pins this end-to-end.
- **BT-030 (gap realism)** — stops execute at the prevailing quote, so a price
  gap through the stop produces the gapped fill. Verified by a dedicated test
  that shows a 4-pip gap producing a >2-pip stop loss.
- **COST-014 sensitivity fallback chain** ends at a conservative global bound,
  never an average of cells.
- **VAL-060 leak checks** truncate every input frame independently by its own
  `available_at`, so mixed-frequency pipelines (H1 features + D1 calendar
  events) are each cut at their own knowledge horizon.
- **COST-011 DST** — session windows are computed per-date in the venue
  timezone (`zoneinfo` + `tzdata`), not as fixed UTC offsets.

## First live contact (2026-09-21)

- Terminal: MetaTrader 5 on LHFXSA-Trade, demo account (login withheld from the repo per SEC-001), connected, algo trading enabled.
- The probe was exercised live and **refused correctly** (empty allowlist),
  persisting its record — the fail-closed path is confirmed on real hardware.
- Capture smoke runs persisted real ticks (EURUSD 1.14809/1.14815, 2026-09-21
  01:00 UTC) and resumed without duplication; a stale Friday XAUUSD quote was
  correctly skipped.
- To go further (probe "ok", canary), add the demo account to
  `config/accounts.yaml`: sha256 of the demo login as `login_hash`, server
  `LHFXSA-Trade`, type `demo`, currency `USD`, phase `demo`.
- 2026-09-21: allowlist entry added from live output; full probe run: outcome
  **ok**, 0 discrepancies, 0 errors. Observed: hedging margin mode, terminal
  build 6204, IOC filling on all four symbols, `trade_exemode=2` (market
  execution), `stops_level=0` (compatible with every intended stop distance —
  GATE-011 pass). One adapter defect found and fixed live: the filling-mode
  derivation read a nonexistent `trade_execution_mode` field; the real field
  is `trade_exemode`. `instruments.yaml` volume_max refreshed to the observed
  1000.0 (documented 100/50 was placeholder drift).

## Canary live observations (2026-09-21, MILE-023)

Three live runs against LHFXSA (two early failures recovered manually, one
full clean pass plus one settle-flagged pass; account flat after each):

- **Server quirk:** `order_send` returns retcode 10009 ("Request executed")
  with empty order/position ids. The adapter now recovers the position id
  from deal history via the correlation comment, and a failed lookup never
  masks the fill. `mt5.time()` does not exist in the Python API — replaced
  with wall-clock UTC.
- **Fill behaviour:** entry at the quoted ask, zero slippage against the
  decision quote, wall latency ~250 ms. Broker-side SL/TP confirmed riding on
  every position (EXEC-040). Close executes at the bid.
- **Economics:** EURUSD round trip at 0.01 lots: price PnL matched the model
  to the cent (0.6 pips x $0.10/pip = $0.06); **observed commission is $6/lot
  round trip, not the $7 documented in the fee schedule** (COST-016 drift);
  balance settlement lags the close by seconds — the canary now polls and
  flags an unsettled reconciliation instead of recording a false 0.0 delta.
- **Server clock:** broker stamps run ~3 h ahead of UTC (server-zone question
  GATE-005 stays open; the reset boundary canary must settle it).
- **Fee provenance (COST-016):** the observed $6/lot round-trip commission now
  lives in `config/fees.yaml` with the canary record as evidence
  (`observed_on_server: true`, sha256 of the record attached). The loader
  refuses schedules without server observation and refuses unobserved swap
  rates by default — swap values stay unmeasured until the reset-boundary
  canary, and only an explicit `allow_unobserved_swaps=True` demo
  acknowledgement builds a schedule without them (zero swap, no overnight
  holds).

## Known limitations

- MT5 adapter requires the MetaTrader 5 terminal on Windows with
  "Algo trading" enabled; everything else fails closed.
- Capture is single-process, file-locked (one writer per venue);
  multi-terminal fan-out is future work.
- The toy strategy is a demonstration of plumbing only — it is not a
  candidate edge, and its parameters are not tuned.
- The canary holds for seconds: it observes fill/latency/fees and reconciliation
  but NOT swap timing or reset-boundary behaviour (recorded as not-observed in
  the report); it also assumes the account is flat before it starts — pre-existing
  positions would pollute the balance-delta reconciliation.
- Capture downtime windows open on the wall clock and close on the feed's
  timestamp; a stale feed stamp books a zero-length window (clamped, never
  negative) rather than the true outage length.

## Continuous integration

`.github/workflows/ci.yml` runs the identical pre-commit gate on every push
and PR (windows-latest; Python 3.11 floor + 3.14, the local interpreter):
it rebuilds the venv at `.venv/Scripts/python.exe` — the path the local hooks
expect — editable-installs the project with dev+mt5 extras, and runs
`pre-commit run --all-files`. This closes the hooks-only-where-installed gap:
checkouts that never ran `pre-commit install` are still gated the same way.
The setup path was verified locally before commit (clean venv → editable
install → full green board, MetaTrader5 included).
