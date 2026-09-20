# Architecture

**Document 3 of 5** · Revision 14 · 19 September 2026
Requirement prefixes: `ARCH`, `DATA`, `FEAT`, `COST`, `BT`, `STRAT`, `RISK`, `CHAL`, `EXEC`

Data model, event contract, engines and interfaces. Statistical requirements live in document 2; this document implements them.

---

# Part A — Foundations

## ARCH-001 · Typed units, not floats

Money, price, points, pips, volume and R are distinct types, not interchangeable numbers. Binary floats at exact breach boundaries and pip/point confusion are avoidable failure modes that surface as off-by-a-factor errors in position size.

- Prices and money: integer minor units or `Decimal`. Never `float` at a comparison that decides a breach.
- Pips vs points: `EURUSD` has a point of 0.00001 and a pip of 0.0001; `USDJPY` 0.001 and 0.01. Store the instrument's definition (`DATA-002`) and convert through it, never by a constant.
- **Rounding direction is specified at every broker boundary** and always in the conservative direction: volume down, cost up, stop distance up.

## ARCH-002 · Canonical event priority

One ordering, used identically by the backtest engine and the live system. Without it, results around midnight differ while both implementations claim compliance.

For events at the same timestamp:

| # | Event |
| --- | --- |
| 1 | Market/session status change |
| 2 | Rule reset boundary (`CHAL-012`) |
| 3 | Swap and commission posting |
| 4 | Quote arrival |
| 5 | Pending-order trigger |
| 6 | Fill, partial fill or rejection |
| 7 | Broker-side SL/TP trigger |
| 8 | Strategy decision |
| 9 | New order submission |

The exact position of 2 relative to 3 must be taken from the selected venue's observed behaviour and recorded at `GATE-011`; the rest is fixed. A backtest whose ordering differs from live is not a backtest of the live system.

## ARCH-003 · One state machine

```
Queued → Submitted → Filled        → Open → ClosedSL | ClosedTP | ClosedManual
                   → PartiallyFilled → Open (at filled volume)
                   → Rejected      → logged, no retry within the bar
                   → Requoted      → accept within tolerance, else abandon
                   → Unknown       → resolve via EXEC-031, never resubmit
```

The live system and the backtest use this same machine. If live can partially fill, the backtest models partial fills, or the two diverge by construction.

---

# Part B — Data

## DATA-001 · Acquisition rule

Raw ticks are the only market data acquired. M1 derives from ticks; every higher timeframe derives from M1. No timeframe above ticks is downloaded independently — independently sourced timeframes disagree at bar boundaries and the disagreement is invisible until it has corrupted months of results.

Where ticks are unavailable for a period, M1 may be acquired directly and flagged `tick_derived: false`. Ambiguous-bar resolution then falls back to the M1 path (`BT-021`) and the limitation is recorded rather than forgotten.

## DATA-002 · Instrument definition

`config/instruments.yaml` carries per symbol: venue symbol name including suffix, point size, pip size, contract size, tick size, tick value, volume min/step/max, quote currency, and the `stops_level` and `freeze_level` observed at `GATE-011`. Mutable properties are re-queried before submission (`EXEC-033`).

## DATA-003 · Bar schema — both quote sides

A single OHLC series with `spread_mean` and `spread_max` **cannot** determine whether an ask spike triggered a short stop or a bid drop triggered a long stop, and does not say when the widest spread occurred relative to the bar's high and low. That makes the asymmetric exit rule at `BT-012` unimplementable.

Store both sides:

| Column | Type | Note |
| --- | --- | --- |
| `ts_open` | timestamp[ns, UTC] | Bar open, the label |
| `available_at` | timestamp[ns, UTC] | `ts_open + duration`. The only field features may key on (`FEAT-001`) |
| `bid_open`, `bid_high`, `bid_low`, `bid_close` | Decimal | |
| `ask_open`, `ask_high`, `ask_low`, `ask_close` | Decimal | |
| `volume` | int64 | Tick volume, nullable |
| `tick_count` | int32 | Ticks contributing. An **activity** measure only — not evidence of completeness |
| `max_quote_gap_ms` | int64 | Longest interval without a quote inside the bar |
| `coverage_ok` | bool | No quote gap above the session-specific continuity threshold, and the bar lies inside the instrument's trading session |
| `known_outage` | bool | Bar overlaps an entry in the outage register (vendor, broker or capture outages) |
| `tick_derived` | bool | False where vendor M1 was used directly |

Where only one side exists for a period, the engine produces **explicit optimistic and pessimistic bounds** and conclusions must hold under both.

## DATA-004 · Tick schema

`ts`, `bid`, `ask`, `bid_volume`, `ask_volume`, `sequence_gap` (bool, true where the feed indicates missing ticks). Partitioned by symbol-month.

## DATA-010 · Validation rules

Block promotion from `raw/` to `cleaned/`. Each failure writes a report; none silently repairs.

| Check | Condition | Action |
| --- | --- | --- |
| Duplicate timestamps | Repeated `ts_open` | Reject file |
| OHLC coherence | `low ≤ min(open, close)`, `high ≥ max(open, close)`, both sides | Reject file |
| Crossed quotes | `ask < bid` at any tick | Reject file |
| Non-positive prices | Any price ≤ 0 | Reject file |
| Weekend bars | Data in documented closure window | Quarantine |
| Session gaps | Missing bars during active sessions | Record gap list in manifest |
| Price spike | Single-bar move beyond 15× trailing 100-bar σ | Flag for review, never auto-remove |
| Time-zone profile | Session volume peaks at expected hours | Reject on mismatch — a wrong time zone is the most damaging silent error |
| Precision | Decimal places match `DATA-002` | Reject |

## DATA-011 · Gaps are recorded, never filled

Forward-filling invents a price nobody could have traded.

## DATA-012 · Gaps while a position is open

`DATA-011` covers entries. An open position during an unobservable interval has an unknown stop, target, MAE and rule-breach path, and silently carrying it across is a fabrication.

Declare one policy in advance, per dataset:

| Policy | Meaning |
| --- | --- |
| **Invalidate** | Discard the trade and every aggregate depending on it |
| **Conservative bound** | Resolve at the worst outcome consistent with the surrounding data |
| **Secondary source replay** | Replay from another feed, after formally measuring the two sources' differences |

Never carry a position across an unobservable interval without applying one of these.

## DATA-013 · Resampling

Bars labelled by open time, closed left. The bar labelled 10:00 covers `[10:00, 11:00)` and is knowable at 11:00. Aggregate both quote sides separately. A bar is **incomplete** when `coverage_ok` is false or `known_outage` is true; it generates no signals and is excluded from feature computation. Tick count does not decide this: a quiet complete bar can have few ticks, and a busy bar with a feed gap can have many. Continuity thresholds are per session, calibrated from observed inter-quote intervals, and declared (`OPS-050`). `available_at = ts_open + duration`, written here so no downstream consumer has to remember the rule.

## DATA-014 · Economic calendar

Required by the news filter hypothesis and by funded-stage compliance (`CHAL-013`). Specify the source; a generic vendor "high impact" label is **not** equivalent to the firm's own restricted-event list, and compliance uses the firm's.

Per event: `event_id`, `scheduled_time`, `revision_history`, `rescheduled_from`, `affected_currencies`, `affected_instruments`, `restriction_status`, `source`, `available_at`. The `actual` value has its own `available_at` at release and is never readable before it (`VAL-060`).

## DATA-020 · Manifests and versioning

Every promotion writes a manifest: source, date range, row count, gap list, SHA-256 of the cleaned output, validation report, ingestion git commit. That SHA is the data version cited by experiments. Two experiments citing different SHAs are not comparable.

## DATA-021 · Fitted artefacts carry availability

Gap tables, cost models, correlation matrices, scalers, volatility percentiles and regime thresholds are **fitted quantities**. Estimating any of them over "the full history" puts future information into position sizing, and no feature-level test can see it because a configuration table is not a feature.

Every fitted table carries `calibrated_from`, `calibrated_to`, `available_from`, and one of:

1. **Frozen** — fitted on development data only, stamped, used unchanged thereafter.
2. **Causally updated** — refitted on an expanding window during replay, so each decision uses only what was estimable then.

`VAL-064` asserts no backtest reads a table whose `available_from` postdates the bar being processed.

---

# Part C — Features

## FEAT-001 · Availability, not bar time

Every feature is indexed by `available_at`. A bar labelled `T` on timeframe `D` closes at `T + D`; anything computed from it is available at `T + D` and not before. MetaTrader labels bars by open time, which is what makes the naive join wrong: the H1 row stamped 10:00 contains data through 10:59, so joining it to a 10:05 decision hands the strategy 54 minutes of future.

## FEAT-002 · Joins carry a staleness tolerance

```python
merged = decisions.join_asof(
    h1.select(["available_at", "h1_trend"]),
    left_on="decision_time",
    right_on="available_at",
    strategy="backward",
    tolerance="2h",  # MANDATORY
)
```

Without `tolerance`, a backward join matches the most recent available row however old. A dropped partial bar, a weekend or a feed outage then attaches a feature from days earlier and nothing complains. Set it to a small multiple of the source timeframe; treat a null result as `NO_TRADE`, not as zero. A feature too stale to use is missing, not neutral.

`shift(1)` is not a substitute — it is correct only while no bar is ever missing.

## FEAT-003 · Causal transforms only

| Instead of | Use |
| --- | --- |
| `rank(pct=True)` over the full series | Expanding or trailing-window rank |
| `StandardScaler().fit(all_data)` | Expanding mean and standard deviation |
| `rolling(center=True)` | Trailing window only |
| Full-history ATR percentile | Trailing 252-day percentile |
| Calendar `actual` before release | Scheduled time and impact only, until `available_at` |

## FEAT-004 · Function contract

```python
def compute(df: pl.DataFrame, *, params: FeatureParams) -> pl.DataFrame:
    """Returns feature columns plus available_at.

    MUST use only trailing windows.
    MUST be pure: identical input, identical output, no global state, no I/O.
    """
```

## FEAT-005 · Registry and versioning

Features are registered with a version. Changing a computation increments it and invalidates cached outputs and every ledger entry citing it. Registration increments the **audit count**; it contributes to the statistical candidate set only if it produced a return series (`VAL-045`).

## FEAT-010 · Regime definitions

Regime must be defined before it can be reported, and a definition invented after seeing the breakdown is not a definition. Four regimes, computed from closed bars only, all satisfying `available_at ≤ decision_time`:

| Regime | Definition | Buckets |
| --- | --- | --- |
| Volatility | Trailing 20-day realised ATR as a percentile of the trailing 252-day distribution | Quintiles 1–5 |
| Trend | H4 close vs trailing 200-period H4 mean, with a trailing directional-strength measure | Up / Down / None |
| Session | DST-aware blocks per `COST-011` | Asia / London / NY / Overlap / Rollover |
| Spread | Current spread vs trailing 60-day median for that symbol and session | Normal / Elevated (>1.5×) |

Registered as features, versioned, each counting in the audit count. Regime is a **reporting dimension**, not a gate, until gating earns its place through a pre-registered comparison.

---

# Part D — Costs

## COST-001 · Distributions, not constants

A fixed-spread assumption is the single most common source of an edge that exists only in the backtest.

## COST-010 · Spread model

Conditioned on `(symbol, session_bucket, volatility_bucket, news_proximity, weekday)`, drawn from the empirical distribution rather than its mean.

## COST-011 · Session buckets are DST-aware

London and New York shift with their own local DST, on different dates. A bucket defined as a fixed UTC range is wrong for several weeks a year, and the error lands exactly where spread behaviour changes. Define in exchange local time with a conversion table, or in UTC with the rule applied per date, and test both transitions.

## COST-012 · Entry and stop-exit spread are separate models

A stop is a market order in a fast market: it fires when price is moving, which is when spread is widest. Calibrate stop-exit spread from the capture conditioned on high realised range, not from the overall median. One figure for both understates cost on every losing trade — the half of the distribution that decides whether an edge survives.

## COST-013 · Capture design, not sampling frequency

Periodic snapshots every few seconds **cannot** support a conditional tail model. Eight rollovers are eight independent episodes, not thousands of observations; three releases cannot calibrate a release-conditioned 99th percentile. The `symbol × session × volatility × news × weekday` key produces sparse cells even with a month of data, and serial samples do not solve event-level sparsity.

Required:

1. **Capture every available quote** with sequence gaps recorded, where the platform permits it — not periodic snapshots.
2. State minimum **independent episode counts** per bucket, not total rows.
3. Keep rollover, news and market-open events as **separate episode datasets**.
4. Continue capture throughout the project; the one-month model is provisional.

## COST-014 · Sparse-cell fallback and uncertainty

Hierarchical fallback when a cell is under-populated: exact cell → symbol/session/volatility → symbol/session → symbol → conservative global bound. Every quantile is reported with a confidence interval and its effective episode count. A quantile computed from eight episodes is reported as such, not as a number.

## COST-015 · Slippage, defined once

One fill equation. Price movement during latency and residual execution slippage are separate terms, and each is counted once:

```
arrival_quote   = quote on the fill side at (decision_time + latency)    # from replay, BT-010
fill_price      = arrival_quote + residual_slippage                        # adverse sign
latency_move    = arrival_quote − quote at decision_time                   # reported, never sampled
residual        = fill_price − arrival_quote                               # the only sampled term
```

Venue capture (`COST-013`) records submission quote, arrival quote and fill, so the residual is fitted **net of latency movement**. Fitting slippage against the submission quote and then also replaying latency charges the latency movement twice.

Buy fills at ask plus adverse slippage; sell fills at bid minus adverse slippage. **Spread is charged once.** Adding a half-spread inside the slippage function on top of an ask-based fill charges 1.5× the spread on every entry — invisible, because the total still looks plausible. A zero-slippage fixture must return exactly the quoted ask.

| Order type | Model |
| --- | --- |
| Market entry | Adverse draw from the quoted side at submission |
| Stop loss | Adverse, fat-tailed, using the stop-exit spread model; gaps through the stop are the tail |
| Take profit (limit) | No favourable slippage; fills at the level or not at all |
| Stop entry | Adverse, larger in volatile conditions |

**Adverse selection:** condition residual slippage on information available **at submission** — realised volatility over bars closed before `decision_time`, spread at submission, session, and calendar proximity. The realised range of the bar being traded is not known when the order is sent and must not be used.

## COST-016 · Commission, swap, calendar

Commission per lot round trip from the fee schedule, applied both legs. Swap at each rollover held through, with triple swap on the instrument's documented day — Wednesday for most FX majors, but maintained as a per-instrument calendar including market holidays rather than assumed. Record the schedule's retrieval date.

## COST-017 · Sensitivity surface

Multipliers 0.75× to 2.0× in 0.25 steps, run in both directions because non-venue spread may be pessimistic as well as optimistic. **Rejection:** 1.25× removing more than half the net expectancy marks a cost artefact.

## COST-018 · Two backtest modes

A cost model fitted from venue capture that began this year has `available_from` in this year. Applied to a 2015 backtest it violates `DATA-021`, and `VAL-064` would reject every historical backtest. The two things being done must be named separately:

| Mode | Question answered | Cost inputs |
| --- | --- | --- |
| **Causal replay** | What would have happened, using only what was knowable then | Only artefacts with `available_from ≤ t`. Historical quoted spreads from the tick data qualify; a slippage model fitted later does not |
| **Counterfactual cost experiment** | How would these historical prices trade under **today's** execution costs | Current cost model applied to historical prices, as a declared input — exactly what prop-firm evaluation will face |

Validation of an edge after costs is a **counterfactual cost experiment** and is labelled as one in the ledger. `VAL-064` applies without exception to strategy-dependent fitted artefacts — regime thresholds, scalers, correlations used in sizing — in both modes, and to cost artefacts in causal-replay mode. The cost scenario in counterfactual mode is a declared input and is covered by the sensitivity range at `COST-017`.

## COST-020 · Demo reconciliation is a paired residual test

"Mean realised cost must not exceed mean modelled cost after 30 trades" is not a valid gate. A correctly calibrated stochastic model exceeds its own mean by chance routinely, and 30 trades may contain no stop exits, no swaps, no partial fills and no stressed conditions.

Instead:

```
residual = realised_cost − modelled_conditional_expectation
```

- Pre-declare a practical tolerance and test the residual mean against it with a **one-sided confidence interval**.
- Add **coverage tests**: do realised costs fall inside predicted quantiles at the predicted rates?
- Require minimum observation counts **separately** for entries, stop exits, swaps, each session bucket, and stressed conditions.
- Demo evidence is provisional (`PROD-030`): evaluation and funded servers or account groups may execute differently.

---

# Part E — Backtest engine

## BT-001 · Event-driven, single pass

The engine holds no data beyond the current timestamp. That design invariant is what the tests at `VAL-060` to `VAL-062` verify; the tests, not the design, establish the absence of look-ahead. Ordering is `ARCH-002`, applied identically live.

## BT-010 · Fill rule

The canonical rule is **first eligible quote at or after `decision_time + latency`**. Filling at a bar's open when the order arrives after that open is not conservative, it is impossible.

| Data available | Fill rule | Status |
| --- | --- | --- |
| Ticks | First quote at or after `decision_time + latency` | **Canonical** |
| M1 only | Open of the first M1 bar beginning after `decision_time + latency` | Labelled approximation |
| Higher-timeframe bars only | Optimistic and pessimistic bounds; conclusions must hold under both | Not adequate for final validation |

Every result records which rule produced it. A backtest with approximate fills is not comparable with one with exact fills, and the ledger must distinguish them.

## BT-011 · Intrabar ordering

Per `ARCH-002`, within one bar: pending triggers, then fills, then broker-side SL/TP, then the strategy decision. Concretely — a queued order filling at the start of a bar whose stop is then touched in the same bar is filled **first** and its exit resolved **second**, at tick or M1 granularity, before any new signal is computed. Treating the fill and exit as belonging to different bars invents a holding period that did not exist.

## BT-012 · Exits on the side the position closes at

A long closes by selling at the **bid**; a short closes by buying at the **ask**. Stops and targets are evaluated against that side. Testing both against one series costs roughly half a spread on every short exit and far more at rollover.

Spread widening moves both quotes away from mid, so a short's stop can fire on an ask spike the bid never reaches **and** a long's stop on a bid drop the ask never reaches. Model widening on both sides; a model that only lifts the ask punishes shorts and flatters longs. This requires `DATA-003`.

## BT-020 · Ambiguous bars

When high and low reach both stop and target, bar data cannot say which came first — and the resolution rule biases reward-to-risk research, because ambiguity frequency rises with target distance.

1. **Ticks** — replay and resolve exactly. Default.
2. **M1** — drill into the M1 bars within the bar.
3. **Neither** — report both bounds; conclusions must hold under both.

A fixed "assume the stop is hit" rule is **rejected**: not conservative, biased, and the bias scales with target distance.

## BT-021 · Ambiguity budget

Every ambiguous bar is logged with its resolution method. Above 5% of exits resolving ambiguously at M1 granularity, stops are too tight for the available resolution.

## BT-030 · Additional realism

| Requirement | Implementation |
| --- | --- |
| Weekend gaps | Positions held over the weekend fill at Sunday open, gapping through stops where applicable |
| Margin | Reject orders exceeding available margin at the programme's leverage |
| Volume rounding | Round to `volume_step`; **recompute risk from the rounded volume** |
| Partial fills | Open at filled volume; recompute risk from it; do not top up |
| Minimum stop distance | Reject inside `stops_level` |
| Session boundaries | No entries within a configurable window of session close |
| Partial bars | Generate no signals (`DATA-013`) |

## BT-040 · Golden-path regression

One fixed dataset, one fixed strategy, a committed expected result. Any engine change altering it is explained in the commit message. This catches accidental semantic changes to cost or fill logic that pass every unit test while quietly rewriting history.

## BT-041 · Differential testing against worked fixtures

P&L, margin and rule calculations are checked against hand-worked examples and against the firm's own published examples where they exist. A rule engine that has only ever been tested against itself is untested.

---

# Part F — Strategy

## STRAT-001 · Interface

```python
class Strategy(Protocol):
    name: str
    version: str
    required_features: list[str]
    active_regimes: list[Regime]  # empty means all

    def evaluate(self, state: MarketState) -> Signal | None:
        """Returns an OrderIntent (STRAT-002) or None for NO_TRADE.

        MUST read only state.features where available_at <= state.now.
        No raw data access, no I/O, no mutable state between calls.
        """
```

`None` is the expected return most of the time. A strategy signalling on a large fraction of bars has no selectivity.

## STRAT-002 · Order intent

"Entry, stop and target" does not specify enough to simulate a pullback, a retest and a breakout consistently. An intent carries:

```python
@dataclass(frozen=True)
class OrderIntent:
    side: Side
    order_type: OrderType  # market | limit | stop
    limit_or_stop_price: Price | None
    stop_loss: Price
    take_profit: Price | None
    time_in_force: TIF  # GTC | DAY | GTD
    expiry: datetime | None
    max_slippage: Points  # abandon rather than fill worse
    cancel_rule: CancelRule  # what invalidates this before it fills
```

## STRAT-010 to STRAT-013 · The four families

Included as hypotheses per `PROD-001`. Each registers its falsification criterion **before** the backtest runs.

| ID | Family | Hypothesis | Rationale | Falsified if |
| --- | --- | --- | --- | --- |
| `STRAT-010` | Trend pullback | Retracements within an established higher-timeframe trend resolve in the trend's direction more often than chance | Order flow from slower participants resumes after short-term profit-taking | Expectancy at or below zero after costs across regimes |
| `STRAT-011` | Breakout retest | A level that breaks and holds on retest continues | Stop clusters beyond the level are cleared, removing opposing supply | No edge over entering at the break itself |
| `STRAT-012` | Volatility expansion | Compressed volatility resolves directionally | Volatility clusters and mean-reverts at different horizons | Direction unpredictable even where magnitude is |
| `STRAT-013` | Range reversion | In confirmed ranges, extremes revert | Liquidity provision is compensated absent directional flow | Losses at range breaks exceed gains inside them |

## STRAT-020 · Parameter discipline

1. Every parameter has a stated rationale and plausible range **before** optimisation.
2. Maximum four free parameters per strategy.
3. Tested on neighbourhoods, not points: ±20% must perform comparably.
4. Report the surface, not the peak. A sharp peak is an overfit whatever its height.
5. Reward-to-risk is a parameter like any other, resolved with tick or M1 data so `BT-020` does not decide the answer.

## STRAT-021 · The loop that must not be run

> Backtest → not profitable → adjust parameters → backtest → repeat until profitable.

That loop always terminates in a profitable result, because with enough combinations one fits the sample's noise. What comes out describes the past.

Constrained search — a grid declared in advance, evaluated on neighbourhoods, counted as trials, judged after deflation — is legitimate. The mechanical difference is whether the stopping condition was written before or discovered during.

Two checks: was the search space declared before the first run, and is this change being made for a stated reason or because it improved the number?

---

# Part G — Risk

## RISK-001 · Final authority, reduce-only

The risk engine can only reduce or veto, never increase. No strategy output and no external component can raise size, override a limit or reopen after a halt.

## RISK-010 · Sizing is bounded by headroom, not just equity

Starting at `equity × risk_pct` and applying limits afterwards permits an order at 59% of the soft-halt threshold that breaches before the watchdog reacts. Pre-trade maximum loss is the **minimum** of:

1. Configured strategy risk (`equity × risk_pct`)
2. Remaining daily-loss room, after liquidation and gap cost on all open positions, minus a reserve
3. Remaining maximum-loss room, same basis
4. Currency, cluster and total-open-risk limits (`RISK-030`)
5. Margin and broker stop-out room

```python
def size_position(account, intent, symbol_spec, rules) -> Volume | None:
    """Each step can only reduce. Order matters.

    1. VETO if account state, positions or floating P&L could not be read
    2. risk_budget = min(configured, daily_headroom, max_headroom,
                         exposure_headroom, margin_headroom)
    3. stop_distance includes spread at the stop
    4. effective_stop = max(stop_distance, gap_95[symbol][event_type])
    5. raw_volume = risk_budget / (effective_stop * value_per_point)
    6. volume = floor to volume_step
    7. RECOMPUTE risk from the rounded volume
    8. reject if outside volume_min/max, inside stops_level, or over margin
    9. on partial fill, recompute risk from the FILLED volume
    """
```

## RISK-011 · Unreadable state is a veto

A hard invariant of the interface, not a monitoring rule. No code path may size a position while daily loss is unknown, and there is no override flag. Halting on a healthy account costs nothing; trading on an unreadable one is how limits are breached.

## RISK-012 · Account risk is not per-strategy

A manual position or one from another EA still consumes margin, moves equity and counts toward the firm's limits. Filtering foreign positions out of **reconciliation** is correct — they are not this system's to manage. Filtering them out of **risk** is a serious error: daily loss, margin and exposure all operate on the whole account whatever their origin.

Where foreign positions are present and material, the risk engine **vetoes rather than sizes around them**: the limits assume sole control of the account, and that assumption is now false. Raise an incident (`OPS-020`).

## RISK-013 · Reserve risk for pending and in-flight orders

Headroom checked against open positions alone is not enough. Two pending entries — on the same symbol or different ones — can each pass the check and then fill together, exceeding every account limit. Serialising submissions per symbol does not prevent it.

- Every accepted order, pending or in flight, **reserves** its maximum loss and its margin at acceptance.
- The headroom check and the reservation are **one atomic operation at account level**, under the write authority of `EXEC-053`.
- `RISK-010` computes headroom net of open positions **and** outstanding reservations.
- A reservation is released only on confirmed cancellation, confirmed rejection, or reconciliation showing the order no longer exists. On a fill it converts into the open position's risk.
- A timeout does not release a reservation. An order whose state is unknown is assumed live.

**Fixture:** two pending entries on different symbols, each individually within limits, jointly over them. The second must be refused at acceptance, and a simultaneous trigger of both must not breach.

## RISK-020 · Gap risk

Stop distance understates risk wherever price can move without trading through the stop.

```
effective_stop = max(stop_distance, gap_95[symbol][event_type])
```

| Event type | `gap_95` source |
| --- | --- |
| Weekend | 95th percentile Friday-close to Sunday-open move for that pair |
| Scheduled high-impact release | 95th percentile release-window range for that pair and release type |
| Market holiday reopen | 95th percentile reopen gap |
| Normal intraday | No adjustment |

Calibrated per `DATA-021` on the development window only, sanity-checked against demo-period gaps, and treated as a **lower bound** until a live evaluation period confirms it. Where research and venue disagree, take the larger.

## RISK-030 · Exposure limits

| Limit | Definition | Default |
| --- | --- | --- |
| Per-trade risk | Equity fraction at gap-adjusted stop | 0.5% |
| Total open risk | Sum across open positions | 2.0% |
| Per-currency | Signed notional per currency, converted to account currency | 1.5% equivalent |
| Correlated cluster | Sum across positions with rolling 60-day correlation > 0.7 | 1.0% |
| Max concurrent | Count | 4 |
| Daily loss soft halt | Realised plus floating, firm's definition | 60% of limit |
| Daily loss hard halt | Same | `EXEC-052` |

Defaults are declared configuration with rationale (`OPS-050`).

Correlations are rolling, fitted under the availability rules of `DATA-021`, and additionally stressed at 1.0 in the Monte Carlo suite. Currency exposure is computed exactly by summing signed notional and converting to account currency at the prevailing rate — for majors this is arithmetic, and estimating what can be computed adds error for nothing.

---

# Part H — Challenge simulator

## CHAL-001 · Purpose

Given a strategy's behaviour and a sizing rule, what is the probability of reaching the profit target without breaching any rule? That probability feeds `PROD-011`; it is not itself the objective.

## CHAL-010 · Rules are phase-aware and typed

A flat rule set cannot represent a programme whose evaluation and funded phases differ, and untyped scalars like `consistency_rule: null` cannot represent a formula.

```yaml
provider: ftmo
programme: challenge_2_step
account_type: swing
rule_set_version: "2026-09-19"
timezone:
  iana_zone: Europe/Prague          # GATE-005 — a zone, not an offset
  source: "..."
phases:
  challenge: { rules: {} }
  verification: { rules: {} }
  funded: { rules: {} }
```

Every atomic rule is a tagged object with provenance:

```yaml
maximum_daily_loss:
  status: verified                  # unknown | verified | not_applicable
  amount: { kind: percent_initial_capital, value: 5 }
  floor_basis: balance_at_reset_minus_amount
  monitored_value: equity_including_fees_and_swap
  ratchet_on_equity: false          # GATE-003
  breach_semantics: falls_below     # falls_below | hits
  breach_severity: hard             # hard | soft
  reset_local_time: "00:00:00"
  source_url: "..."
  retrieved_at: "2026-09-19T00:00:00Z"
  evidence_sha256: "..."
  observed_on_server: true
```

## CHAL-011 · Three states, not two

| Status | Meaning | Simulator |
| --- | --- | --- |
| `unknown` | Not checked | **Refuses to run** |
| `not_applicable` | Verified that the firm does not impose this | Runs; constraint skipped |
| `verified` + value | Checked and recorded | Runs; constraint enforced |

`not_applicable` requires the same provenance as a value. "No time limit" is a verified fact, not a gap; refusing every null would reject it wrongly.

## CHAL-012 · Rules requiring structured support

Profit target and any "all positions closed" condition; hard vs soft breach; the exact daily and maximum loss formula with inclusions and equality semantics; minimum trading days **and the precise event that constitutes a trading day** (at FTMO, a position being opened — not merely held); inactivity limits; consistency or best-day formula; leverage and margin stop-out by phase; news event IDs with affected instruments, windows, and whether SL/TP and pending orders count; overnight, weekend and market-break closure rules; maximum positions, pending orders, orders per day, rate limits; prohibited strategies and subjective review clauses; payout-cycle constraints.

## CHAL-013 · Design against the funded contract

Firms commonly loosen rules during evaluation and tighten them on funding — news restrictions, weekend holding and leverage are the three that recur. A strategy tuned to evaluation rules can pass and then be unable to trade, or be disqualified, on the account it was built to reach. Size and constrain against the **funded** terms from the start.

## CHAL-020 · Resampling carries full state

The simulator cannot take "a trade distribution" and also mark equity every bar — a distribution of final R-multiples cannot recreate intratrade equity, overlapping positions, resets, margin or SL/TP timing.

The resampling unit is a **synchronous block carrying**: the timestamped portfolio mark-to-market path, entries, exits and pending orders, open-position state with MAE/MFE paths, spreads, fees, swaps and currency conversion, and reset boundaries with trading-day events.

## CHAL-021 · Block seams

An open trade at the end of one sampled block cannot continue into an unrelated next block, and closing it silently at the seam fabricates an exit. Choose one, declared in advance:

| Approach | Mechanism |
| --- | --- |
| **Regenerative blocks** | Sample only at flat boundaries where no position is open |
| **Overlapping blocks with warm-up** | Carry a purge region so seam positions are excluded from statistics |
| **Generative model** | Simulate a market process and rerun the strategy, so positions arise naturally |

Silent closure at seams is not among the options.

## CHAL-030 · Trade supply as a probability, not a verdict

```
trades available = signal rate per day × trading days in limit
```

Fewer trades available than the expected number needed does not make passing impossible — a lucky sequence can pass in fewer. It makes it improbable, and the simulator should say how improbable. Report pass probability across a grid of risk levels, with the probability of **running out of time or trades before the target** shown for each cell, not a single optimum. Check minimum trading days from the other direction too: a strategy that signals rarely can hit the target and still fail on distinct-day count.

## CHAL-031 · Output

| Metric | Why |
| --- | --- |
| P(pass phase 1), P(pass phase 2), P(pass both), **with confidence intervals** | The objective, with its uncertainty |
| Failure mode distribution | Which constraint binds |
| Median days to target, with interval | Against the time limit |
| P(breach daily limit at least once) | Usually the dominant failure |
| Best risk level | Corrected for selecting it from the same simulations |

Uncertainty covers expectancy, cost and serial dependence — not merely 10,000 draws conditional on fitted inputs, which measures simulation noise rather than knowledge. Model Challenge, Verification and funded periods **sequentially**, including margin stop-out and what happens when the target is reached with positions still open.

## CHAL-032 · Zero-edge calibration

Run with expectancy set to zero. A zero-edge strategy at modest sizing still shows a non-trivial pass probability — the challenge is passable by luck. Every claimed pass probability is reported against that null, or variance will be mistaken for skill.

---

# Part I — Execution

## EXEC-001 · Pluggable adapter

One interface, one implementation per platform, so the platform choice changes one module. Research runs anywhere; the official MetaTrader5 Python package is Windows-only, so an MT5 adapter needs a Windows host. cTrader's Open API runs natively on Linux and macOS.

## EXEC-010 · Adapter contract

The interface must cover what its own requirements need:

```python
class ExecutionAdapter(Protocol):
    # Connection and capability
    def connect(self) -> ConnectionState: ...
    def probe_capabilities(self) -> BrokerCapabilities: ...  # EXEC-020
    def terminal_info(self) -> TerminalInfo: ...  # build, version, permissions

    # State
    def get_account(self) -> AccountState: ...
    def get_positions(self) -> list[Position]: ...
    def get_pending_orders(self) -> list[PendingOrder]: ...
    def get_orders_history(self, frm, to) -> list[HistoricOrder]: ...
    def get_deals_history(self, frm, to) -> list[Deal]: ...
    def find_by_correlation(self, correlation_id) -> OrderRecord | None: ...  # EXEC-031

    # Market
    def subscribe_quotes(self, symbols) -> QuoteStream: ...
    def symbol_session_status(self, symbol) -> SessionStatus: ...
    def symbol_info(self, symbol) -> SymbolInfo: ...  # re-query before submit

    # Calculation — cross-check, never trust blindly
    def calc_margin(self, intent) -> Money: ...
    def calc_profit(self, intent, close_price) -> Money: ...

    # Action
    def submit(self, intent: OrderIntent) -> OrderResult: ...
    def modify(self, ticket, sl, tp) -> OrderResult: ...
    def cancel(self, ticket) -> OrderResult: ...
    def close_position(self, position_id, volume=None) -> OrderResult: ...
    def server_time(self) -> datetime: ...
```

`close(ticket)` alone is insufficient across hedging and netting: on a netting account a close is an opposing deal against a net position, not a ticket closure. The adapter exposes position-level closing and declares which mode it is operating in.

Use the platform's own margin and profit calculators where available and **independently cross-check** them; a single tick-value field is not a substitute. Re-query mutable symbol properties before every submission.

## EXEC-020 · Capability probe

Runs on every connection, persists output, fails closed. Fields at `GATE-011`, plus `trade_allowed`, margin mode, and terminal build. The probe cannot settle DST (`GATE-005`).

## EXEC-030 · Order identity

MetaTrader 5 has no native client order ID. Build it:

| Need | Mechanism |
| --- | --- |
| Distinguish this deployment's trades | `magic` — a **constant** per deployment, never per order |
| Per-order identity | A durable record written **before** submission, holding the intent and a correlation id |
| Account-level risk | **Not `magic`** — see `RISK-012` |

Encoding a per-order hash into `magic` destroys the isolation `magic` exists to provide.

## EXEC-031 · Recovery from an ambiguous response

Read the pre-submission record; query positions and recent deals filtered by `magic`; match on symbol, volume, direction and a tight window around submission; halt for manual resolution if the match is not unique. **Never resubmit on timeout.**

**Uniqueness is a design requirement, not a hope.** Serialise submissions per symbol so at most one order is outstanding, and record a submission sequence number. Without that, two identical orders cannot be told apart after an outage.

## EXEC-032 · Filling mode

`type_filling` is mandatory and symbol-specific; an unsupported value is rejected outright. Select per symbol from the probed `filling_mode`.

## EXEC-033 · Re-query mutable symbol properties

`stops_level`, `freeze_level`, spread, volume limits and filling modes can change during a session — around news, at rollover, or when the broker adjusts them. Values cached at connection are stale by the time an order is built. Re-query before every submission and reject rather than submit against a cached value that no longer holds.

## EXEC-040 · Broker-side protection

A watchdog process protects against a strategy crash. It does **not** protect against VM failure, power loss, network loss, terminal failure, or a deployment that kills both processes. In any of those, an unprotected position runs unbounded.

| Requirement | Detail |
| --- | --- |
| Every position carries a broker-hosted stop | Submitted atomically with entry where the platform supports it |
| Protection unconfirmed → close and halt | If the stop cannot be confirmed immediately, exit the exposure rather than hoping |
| Flatten is verified, not assumed | "Send close" is not "flat". Re-query broker state after any flatten attempt |
| Market-closed policy | Flattening is impossible when the market is shut; state what the system does and what exposure that leaves |

## EXEC-050 · Reconciliation

Every cycle and after every reconnection: fetch broker positions and account state, diff against internal state, halt on any discrepancy, and never auto-resolve by trusting internal state. An orphaned position is a manual intervention. Scope per `RISK-012`.

## EXEC-051 · Watchdog

A separate process with its own broker connection, so that a dead strategy process can still be acted on.

## EXEC-052 · Two-stage halt

| Stage | Trigger | Action |
| --- | --- | --- |
| Soft | Daily loss at 60% of limit | Stop new entries; open positions remain |
| Hard | Daily loss at the derived threshold | Cancel pending orders and flatten |

Halting entries alone leaves existing positions free to reach the limit. The hard threshold is **derived from projected liquidation cost and gap stress** for the current book, not fixed globally — 85% is a starting value, not a constant. Other halt conditions: heartbeat missed, state unreadable (`RISK-011`), maximum loss approached, feed stale, reconciliation mismatch, broker disconnect, clock drift (`OPS-011`), manual switch.

## EXEC-053 · Write fencing

A strategy process and a watchdog with separate connections can race. Define a single write-authority lease with a fencing token, shared durable state, fsync requirements for order intents, and explicit takeover rules. SQLite WAL provides atomicity, not command ownership.

## EXEC-060 · Account allowlist

A mutable build flag is too weak a guard against trading the wrong account. On every connection verify login hash, server, account type, currency and phase against an explicit allowlist, and **reject any unrecognised account before requesting trade permission**.

## EXEC-061 · Latency measurement

Every order logs signal time, submit time and fill time. The differences are the measured latency that `BT-010` assumes and `COST-020` reconciles. An assumption nobody measured is not a model.

## EXEC-062 · Demo evidence is provisional

Demo, evaluation and funded accounts may sit on different servers or account groups with different execution behaviour. Demo reconciliation constrains the cost model; it does not confirm it for the account that will be traded.
