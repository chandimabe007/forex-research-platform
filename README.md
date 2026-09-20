# Forex Research Platform

A **measuring instrument** for systematic forex trading ideas: it tests whether an
idea survives realistic costs and prop-firm rules, and reports honestly. It is not a
strategy and does not make money by itself (`PROD-001`).

The specification is single-source and lives in [`spec/`](spec/). **Read
[`spec/0-index.md`](spec/0-index.md) first.** Requirements are referenced by ID
(e.g. `PROD-010`); never restated in code or docs.

## Status

Stage A skeleton (`MILE-103` action 1). Implemented so far:

- Project skeleton: packaging, linting (ruff), tests (pytest), pre-commit with
  credential scanning, git.
- Validated configuration schemas for the economic objective (`PROD-010`) and the
  firm/programme/account challenge rules (`CHAL-010`, `CHAL-011`, `GATE-005`), the
  two schemas required by the definition of ready (`MILE-002`).

The software builds and its tests run **without** any objective or firm supplied. It
will refuse to run research (`MILE-040` onward) until they are supplied and validated.

- `MILE-021` capability probe (`EXEC-020`, `GATE-010`, `GATE-011`): a read-only probe
  of the MetaTrader 5 demo. On connect it checks the account against the allowlist
  (`EXEC-060`, `SEC-030`) **before any capability query**, and fails closed — invalid,
  missing or unexpected results never report OK. Run it with
  `python -m forex_research.execution.cli`. It places no orders; a redacted record for
  every outcome persists to `data/probe/`.

- `MILE-022` venue quote capture (`COST-013`): a read-only service that captures every
  available tick from the demo into `data/raw/venue_quotes/` (partitioned by
  symbol-month, `DATA-004` shape). It resumes from the tick files on disk (crash-safe: no
  replay duplication), distinguishes suspected quote gaps from confirmed capture downtime
  and feed errors, retries/reconnects on feed failure, and keeps only bounded state in
  memory. `python -m forex_research.capture.cli` runs it continuously (add `--cycles N`
  for a bounded run); `python -m forex_research.capture.report` prints coverage-based
  episode counts per bucket against the `COST-013` minimums. Capture must run for months;
  the one-month model is provisional.

Not yet started: `MILE-023` execution canary, Stage C.

## Running venue capture continuously

Capture needs months of calendar time (`COST-013`); starting late delays `MILE-040` and
`MILE-070`. Run it in a dedicated terminal (it resumes from the manifest after any
restart, recording the downtime as a gap):

```bash
.venv/Scripts/python.exe -m forex_research.capture.cli
```

Session zone and continuity thresholds are provisional UTC values in `config/capture.yaml`
until the server zone is verified (`GATE-005`) and thresholds are calibrated from observed
inter-quote intervals (`DATA-013`). Volatility- and news-conditioned buckets await a
volatility model (`MILE-031`) and the economic calendar (`DATA-014`); the report names
them as deferred rather than faking counts.

## First live probe result (2026-09-19, demo)

`status: OK` on `LHFXSA-Trade` (demo, USD): `trade_expert=True` (server permits
automation), hedging margin mode, terminal build 6204, `stops_level=0` on EURUSD/
GBPUSD/USDJPY/XAUUSD (all candidate stop distances placeable). Execution mode is
**market** on all four, so the supported order-filling policy is **IOC only** — under
market execution RETURN is disabled and the FOK symbol flag is not set (derived per the
MQL5 policy table; relevant for `EXEC-032`). One action for the live-order step: the
terminal's **Algo Trading** toggle is currently off (`terminal.trade_allowed=false`).

## Setup (Windows, local)

```bash
py -3 -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m pytest
```

The official `MetaTrader5` package is Windows-only and needed from `MILE-021`; install
it with the `mt5` extra (`pip install -e ".[dev,mt5]"`) once a compatible interpreter
is confirmed (see Known limitations).

## Configuration

Templates live in [`config/`](config/):

- `objective.example.yaml` — the `PROD-010` objective. Copy to `objective.yaml` and
  fill in during `MILE-010`. An all-null template is refused by the loader.
- `challenge_rules.example.yaml` — the `CHAL-010` rule model. Replace with the
  verified rules for your chosen firm + programme + account type triple (`PROD-023`).

Credentials never live in the repository (`SEC-001`). `.env` is for local development
only and is git-ignored; copy `.env.example`.

## Safety

**Demo accounts only until `MILE-095`.** No orders are placed except in the milestones
that call for it, and only on the allowlisted demo account (`EXEC-060`, `SEC-030`).

## Known limitations (this milestone)

- `MetaTrader5` runs on Python 3.14 here (cp314 wheel confirmed), so the single `.venv`
  covers research and execution.
- `stops_level=0` currently on every probed symbol. That is the connection-time minimum;
  `freeze_level` and dynamic/near-news constraints still apply, so orders re-query symbol
  properties before submission (`EXEC-033`) rather than trusting this value.
- The probe reads capability only. Fill behaviour, fees, swap and reset timing are
  measured by the `MILE-023` canary, not here.
- `spec/tools/check_references.py` reads files without an explicit encoding, so it
  fails on this Windows host (cp1252 cannot decode the spec's UTF-8 punctuation). Spec
  integrity has been verified out-of-tree; the checker needs `encoding="utf-8"` to run
  here, which is a change to a spec file and awaits the user's decision.
- Full per-rule-type coverage of `CHAL-012` is deferred to `MILE-033`; the schema
  currently accepts unrecognised rule-value fields.
