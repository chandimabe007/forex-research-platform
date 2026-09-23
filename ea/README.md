# LondonMomentumPortfolio.mq5

MQL5 Expert Advisor porting the two accepted `london_momentum` H1 books from
the strategy-lab campaign (PR #14 verdict: median 90-day return +10.3%,
max DD 7.78% at 1.45× inverse-DD weighting). This is the executable form of
that configuration for MT4/MT5-style live or demo use.

## What it trades

| book | symbol | impulse k | stop (×ATR) | RR | risk/trade | magic |
|---|---|---|---|---|---|---|
| EUR | EURUSD | 1.50 | 1.5 (never binds; 60-bar channel floor governs) | 3.0 | 0.72% | InpMagicBase |
| GBP | GBPUSD | 0.75 | 0.75 | 1.5 | 0.83% | InpMagicBase + 1 |

**Entry** (evaluated once per closed H1 bar, fill at market on the new bar):
the signal bar closes beyond the 60-bar channel (computed from bars *before*
the signal bar) by `k × ATR(14)` **and** its tick volume is ≥ 1.2× the
20-bar average. Session gate 07:00–11:00 UTC on the signal bar's open time;
one entry per book per UTC day.

**Exit:** broker-side SL/TP from entry — stop = max(0.75×ATR, 1.0 pip,
broker stops-level), TP = RR × stop. No trailing: the campaign's verdict
was that static-RR exits hit the +10%/90-day target inside the 8% DD budget
while trailing exits clipped the right tail.

**Guards:** spread cap 2.0 pips, daily-loss lockout 5% of day-start equity,
Friday flat at 21:00 UTC, minimum-lot risk-overshoot veto at 1.3×, margin
use cap 80% of free margin.

## Install

1. The compiled `LondonMomentumPortfolio.ex5` is already installed at
   `%APPDATA%\MetaQuotes\Terminal\<id>\MQL5\Experts\` for the terminal at
   `C:\Program Files\demo`. For any other terminal, copy the `.ex5` (or the
   `.mq5` to compile yourself) into that terminal's `MQL5\Experts\` folder.
2. Restart or refresh the Navigator (right-click → Refresh).
3. Drag the EA onto **one** chart (any symbol/timeframe — it reads both
   symbols by name) and enable **Algo Trading**.
4. Inputs to check on first attach:
   - `InpUTCOffsetHours` — the broker's server offset from UTC
     (LHFX demo: verify against the Market Watch clock; MT5 server time is
     UTC+2/+3 for most brokers, so typically `2` or `3`).
   - `InpEURSymbol` / `InpGBPSymbol` — match the broker's symbol names
     (e.g. `EURUSD.m` if suffixed).
   - `InpMagicBase` — change if another instance of this EA runs in the
     same account (books claim `base` and `base+1`).

## Reproducing the compile

```
"C:\Program Files\demo\metaeditor64.exe" /compile:<path>\LondonMomentumPortfolio.mq5
```

Note: MetaEditor's CLI splits arguments on spaces — compile from a
space-free path (or quote the whole `/compile:` value) or it reports
"unsupported file extension" for `D:\Ai`.

## Fidelity to the lab engine (and the two deliberate differences)

Faithful: 60-bar channel excluding the signal bar, simple (unsmoothed)
ATR(14), 1.2× volume confirmation, next-bar-open fill, engine cost-veto
stop floor, one-trade-per-day, session window on the signal bar's open
time, risk-percent sizing with min-lot overshoot veto.

Deliberate differences (live necessities the backtest didn't model):

1. **Fill price** — the lab fills at next bar's open plus spread; live
   fills are at market on tick. Within the 07:00–11:00 UTC window on the
   1h basis these coincide to within the bar open; slippage beyond that is
   real forward-test information, not a defect.
2. **Daily lockout is per book** on realized P&L only (the lab's portfolio
   lockout is account-level). This is stricter — either book's bad day
   stops both that book's new entries.

## Status

- Compiled clean (0 errors) with the build 5000-era MetaEditor shipped with
  the terminal at `C:\Program Files\demo`.
- `ea/LondonMomentumPortfolio.ex5` in this repo is the compiled artifact of
  the `.mq5` beside it — recompile from source rather than trusting the
  binary for anything real.
- Run it on the **LHFXSA demo** first. It is a port of a *screening-engine*
  result: the M1-candle lab with Dukascopy-spread costs ranked these books;
  tick-exact re-verification is still the gate before any real money.
