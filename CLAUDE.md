# Forex Research Platform

A measuring instrument for systematic forex ideas: it tests whether an idea survives realistic costs and prop-firm rules, and reports honestly. It is not a strategy. The full specification is in `spec/`. **Read `spec/0-index.md` first.**

## How to work

- Build **one milestone at a time**, in the order in `spec/4-milestones.md`. Do not start the next until the current milestone's **Done when** line is met and the user has confirmed it.
- Each milestone lists requirement IDs under **Implements**. Look each ID up in `spec/` — that section is the instruction. Do not work from memory or from summaries.
- Every milestone ships: source, tests, configuration, verification instructions, and a written list of known limitations.
- If the spec is ambiguous or contradicts itself, **stop and ask**. Do not resolve it silently in code.
- The spec is single-source. Never copy requirement text into code comments or other docs; reference the ID (e.g. `# RISK-013`).
- After any edit to `spec/`, run `python spec/tools/check_references.py`. It must pass.
- Stop conditions in the milestones are real. When one fires, report it rather than working around it.

## Environment

- Windows, running locally. MetaTrader 5 is installed on this PC with an IC Markets **demo** account.
- Python 3.11+ in a virtual environment at `.venv`. The `MetaTrader5` Python package works only on Windows.
- Historical tick data (Dukascopy) will be supplied by the user later, into `data/raw/ticks/`. Until then, build and test against small fixtures.
- Git from the first commit. Pre-registration and audit rules (`VAL-051`, `VAL-071`) depend on commit history.

## Firm-agnostic

The objective (`PROD-010`), the firm/programme/account (`PROD-023`), the rule values (`CHAL-010`) and the server time zone (`GATE-005`) are **configuration inputs**, validated at load. The software builds without them and refuses to run research (`MILE-040` onward) until they are supplied.

## Safety — non-negotiable

- **Demo accounts only** until `MILE-095`. Enforce the account allowlist (`EXEC-060`) from the first line of execution code; refuse to connect to any account not on it.
- Never place, modify or close orders except in the milestones that call for it (`MILE-023` canary, `MILE-070`), and only on the allowlisted demo account.
- Credentials never in the repository (`SEC-001`). `.env` is for local development only and is git-ignored.

## Where to start

1. Project skeleton: `pyproject.toml`, `.venv`, pytest, ruff, pre-commit with credential scanning, git init, `config/` with validated schemas for the objective and challenge rules.
2. `MILE-021` capability probe against the local MetaTrader 5 demo.
3. `MILE-022` venue quote capture, started the same day and left running.
4. Then Stage C (`MILE-030` onward) against fixtures until tick data arrives.
