"""Strategy lab — M1 replay screening engine (STRAT-LAB-001..004).

The screening tier between the Gate-1 cost tables and the tick-exact engine:
one year of M1 candles per symbol replays in seconds, so hundreds of
parameter combinations can be graded honestly. Every number this engine
produces is a *screening* result; shortlisted configurations still go through
the tick-exact backtester before any real decision (the lab ranks, the tick
engine confirms).

Cost model — wired to measured reality, not assumptions:

- Entry and stop-exit spreads per (symbol, session) come from the MILE-040
  Gate 1 record (conservative q90 across the stop-multiplier cells of that
  session). Weekend/rollover honesty: sessions are approximated on fixed UTC
  hour windows (the DST-aware SessionResolver stays the tick-engine path);
  the q90 spreads already price the worst hour of each bucket.
- Commission is the canary-observed $6/lot round trip (config/fees.yaml,
  COST-016 provenance).
- GATE-022 is enforced, not hoped for: an entry whose round-trip cost exceeds
  0.25 of its stop distance is vetoed before it can exist.

Account model: $10,000 start, fixed-fractional risk sizing, the challenge's
daily-loss lockout, and the drawdown the optimizer's gate measures on the
closed-trade equity curve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import polars as pl

MAX_COST_RATIO = 0.25  # GATE-022
MIN_STOP_PIPS = 5.0  # below this, spread noise dominates any measured edge
VOLUME_STEP = 0.01


@dataclass(frozen=True)
class BarCtx:
    """What a strategy sees at the close of bar ``i`` — and nothing else."""

    i: int
    now: datetime
    open: float
    high: float
    low: float
    close: float
    session: str
    spread_pips: float
    pip_size: float


@dataclass(frozen=True)
class Entry:
    side: str  # "long" | "short"
    stop_price: float
    target_price: float
    note: str = ""


class LabStrategy:
    """Base class. Instances are single-run: they may keep internal state
    (today's opening range, last signal bar) but must not assume bar order
    beyond the single sequential replay they are handed."""

    name: str = "base"
    params: dict = {}

    def decide(self, ctx: BarCtx, f: dict[str, list[float]]) -> Entry | None:
        raise NotImplementedError


@dataclass
class LabTrade:
    symbol: str
    side: str
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    stop_price: float
    volume: float
    pnl_usd: float
    commission_usd: float
    r: float
    exit_reason: str  # sl | tp | timeout | friday_close
    bars_held: int


@dataclass
class LabResult:
    symbol: str
    strategy: str
    params: dict
    trades: list[LabTrade] = field(default_factory=list)
    cost_vetoes: int = 0
    daily_lockouts: int = 0
    rejected_entries: int = 0
    final_balance: float = 10_000.0
    max_dd_pct: float = 0.0

    # -- metrics ---------------------------------------------------------
    def metrics(self, initial_balance: float = 10_000.0) -> dict:
        n = len(self.trades)
        gross_win = sum(t.pnl_usd for t in self.trades if t.pnl_usd > 0)
        gross_loss = -sum(t.pnl_usd for t in self.trades if t.pnl_usd < 0)
        pf = gross_win / gross_loss if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
        wins = sum(1 for t in self.trades if t.pnl_usd > 0)
        total_r = sum(t.r for t in self.trades)
        return {
            "trades": n,
            "win_rate": round(wins / n, 4) if n else 0.0,
            "profit_factor": round(pf, 3),
            "net_usd": round(self.final_balance - initial_balance, 2),
            "return_pct": round(100 * (self.final_balance - initial_balance) / initial_balance, 2),
            "max_dd_pct": round(self.max_dd_pct, 2),
            "total_r": round(total_r, 2),
            "cost_vetoes": self.cost_vetoes,
            "daily_lockouts": self.daily_lockouts,
        }


# -- sessions (screening approximation of COST-011, fixed UTC windows) -----
_SESSION_WINDOWS: tuple[tuple[int, int, str], ...] = (
    (22, 24, "rollover"),
    (0, 1, "rollover"),
    (1, 7, "asia"),
    (7, 12, "london"),
    (12, 13, "overlap"),
    (13, 17, "new_york"),
    (17, 22, "asia"),
)


def hour_session_utc(hour: int) -> str:
    for start, end, name in _SESSION_WINDOWS:
        if start <= hour < end:
            return name
    return "asia"  # unreachable; every hour is covered above


def session_spreads(gate1_path, symbol: str) -> dict[str, tuple[float, float]]:
    """Per-session (entry_spread_pips, stop_exit_spread_pips) from the Gate 1
    record, taking the worst (max) across the stop-multiplier cells of each
    session. A session with no measured cell falls back to the symbol's worst
    measured session — never to an assumed number."""
    import json
    from pathlib import Path

    rec = json.loads(Path(gate1_path).read_text(encoding="utf-8"))
    worst: dict[str, tuple[float, float]] = {}
    acc: dict[str, list[tuple[float, float]]] = {}
    for cell in rec["cells"]:
        if cell["symbol"] != symbol:
            continue
        acc.setdefault(cell["session"], []).append(
            (cell["entry_spread_pips"], cell["stop_exit_spread_pips"])
        )
    for session, pairs in acc.items():
        worst[session] = (max(p[0] for p in pairs), max(p[1] for p in pairs))
    if not worst:
        raise ValueError(f"no Gate 1 cells for {symbol} in {gate1_path}")
    sym_worst = (max(v[0] for v in worst.values()), max(v[1] for v in worst.values()))
    return {
        s: worst.get(s, sym_worst) for s in ("asia", "london", "new_york", "overlap", "rollover")
    }


def pip_value_usd(
    contract_size: float, pip_size: float, price: float, quote_currency: str
) -> float:
    """USD value of one pip for one lot at price ``price``."""
    per_pip_quote = contract_size * pip_size
    if quote_currency == "USD":
        return per_pip_quote
    if quote_currency == "JPY":
        return per_pip_quote / price
    raise ValueError(f"unsupported quote currency {quote_currency!r}")


class M1Lab:
    """One-symbol, one-strategy M1 replay with fixed-fractional risk."""

    def __init__(
        self,
        *,
        spec,  # InstrumentSpec
        spreads: dict[str, tuple[float, float]],
        initial_balance: float = 10_000.0,
        risk_pct: float = 0.01,
        daily_loss_pct: float = 0.05,
        commission_per_lot: float = 6.0,
        max_bars_in_trade: int = 480,
        flat_friday_hour_utc: int = 21,
    ) -> None:
        self.spec = spec
        # InstrumentSpec carries Decimals; the lab runs pure-float math.
        self.pip = float(spec.pip_size)
        self.contract = float(spec.contract_size)
        self.quote_currency = spec.quote_currency
        self.spreads = spreads
        self.balance0 = initial_balance
        self.risk_pct = risk_pct
        self.daily_loss_pct = daily_loss_pct
        self.commission_per_lot = commission_per_lot
        self.max_bars_in_trade = max_bars_in_trade
        self.flat_friday_hour = flat_friday_hour_utc

    # -- replay ----------------------------------------------------------
    def run(
        self,
        symbol: str,
        bars: pl.DataFrame,
        strategy: LabStrategy,
        features: dict[str, list[float]],
        *,
        index_offset: int = 0,
    ) -> LabResult:
        """Replay ``bars``; ``index_offset`` maps local bar indices onto the
        global feature frame (ctx.i) when ``bars`` is a slice of a larger
        window."""
        ts = bars["ts"].to_list()
        o = bars["open"].to_list()
        hi = bars["high"].to_list()
        lo = bars["low"].to_list()
        c = bars["close"].to_list()
        sessions = [hour_session_utc(t.hour) for t in ts]

        res = LabResult(symbol=symbol, strategy=strategy.name, params=dict(strategy.params))
        balance = self.balance0
        peak = balance
        day_pnl = 0.0
        day_key: date | None = None
        locked_until: date | None = None
        pos: dict | None = None
        pending: tuple[Entry, BarCtx] | None = None
        pip = self.pip

        def spread_pips(session: str) -> float:
            return self.spreads[session][0]

        def close_trade(pos_dict, exit_price: float, exit_ts, reason: str, bars_held: int) -> None:
            nonlocal balance, day_pnl
            move = (
                (exit_price - pos_dict["entry"])
                if pos_dict["side"] == "long"
                else (pos_dict["entry"] - exit_price)
            )
            # move is in PRICE units; pip_usd is per PIP — divide by pip size
            # or every trade's gross is understated by 1/pip (10,000x on EURUSD).
            gross = (move / pip) * pos_dict["pip_usd"] * pos_dict["volume"]
            net = gross - pos_dict["commission"]
            balance += net
            day_pnl += net
            res.trades.append(
                LabTrade(
                    symbol=symbol,
                    side=pos_dict["side"],
                    entry_ts=pos_dict["entry_ts"],
                    exit_ts=exit_ts,
                    entry_price=pos_dict["entry"],
                    exit_price=exit_price,
                    stop_price=pos_dict["stop"],
                    volume=pos_dict["volume"],
                    pnl_usd=net,
                    commission_usd=pos_dict["commission"],
                    r=net / pos_dict["risk_usd"] if pos_dict["risk_usd"] > 0 else 0.0,
                    exit_reason=reason,
                    bars_held=bars_held,
                )
            )

        for i in range(1, len(ts)):
            now = ts[i]
            today = now.date()

            # -- new day bookkeeping (daily loss lockout) ----------------
            if today != day_key:
                day_key = today
                day_pnl = 0.0
                locked = locked_until is not None and today <= locked_until
            else:
                locked = locked_until is not None and today <= locked_until

            # A) fill the pending entry at this bar's open ---------------
            if pending is not None and pos is None and not locked:
                entry, sig_ctx = pending
                pending = None
                is_long = entry.side == "long"
                entry_price = o[i] + sig_ctx.spread_pips * pip if is_long else o[i]
                stop_dist = abs(entry_price - entry.stop_price)
                # GATE-022 is measured on the INTENDED distance (signal close
                # to stop, pre-spread) — measuring post-spread would let the
                # ask-widening dilute its own ratio and defeat the veto.
                intended = abs(sig_ctx.close - entry.stop_price)
                cost_ratio = sig_ctx.spread_pips * pip / intended if intended > 0 else 9e9
                if stop_dist <= 0 or cost_ratio > MAX_COST_RATIO or intended / pip < MIN_STOP_PIPS:
                    res.cost_vetoes += 1  # GATE-022: the gate says no
                else:
                    risk_usd = balance * self.risk_pct
                    pip_usd = pip_value_usd(self.contract, pip, entry_price, self.quote_currency)
                    raw_volume = risk_usd / (stop_dist / pip * pip_usd)
                    if raw_volume < VOLUME_STEP:
                        # Flooring to volume_min would silently oversize the
                        # risk beyond the configured fraction — reject instead.
                        res.rejected_entries += 1
                        pos = None
                        continue
                    volume = (int(raw_volume / VOLUME_STEP)) * VOLUME_STEP
                    commission = volume * self.commission_per_lot
                    pos = {
                        "side": entry.side,
                        "entry": entry_price,
                        "stop": entry.stop_price,
                        "target": entry.target_price,
                        "entry_ts": now,
                        "entry_i": i,
                        "volume": volume,
                        "pip_usd": pip_usd,
                        "commission": commission,
                        "risk_usd": volume * (stop_dist / pip) * pip_usd,
                    }

            # B) manage the open position on this bar. Exits resolve on the
            # side the position closes at (BT-012): longs exit at bid, shorts
            # at ask = bid + this bar's session spread.
            if pos is not None:
                bars_held = i - pos["entry_i"]
                is_long = pos["side"] == "long"
                spread_usd = self.spreads[sessions[i]][0] * pip
                if is_long:
                    stopped = lo[i] <= pos["stop"]
                    target_hit = hi[i] >= pos["target"]
                else:
                    stopped = hi[i] + spread_usd >= pos["stop"]
                    target_hit = lo[i] + spread_usd <= pos["target"]
                exit_price: float | None = None
                reason = ""
                if stopped:  # ambiguity resolves to the stop (conservative)
                    exit_price, reason = pos["stop"], "sl"
                elif target_hit:
                    exit_price, reason = pos["target"], "tp"
                elif bars_held >= self.max_bars_in_trade:
                    exit_price, reason = c[i], "timeout"
                elif now.weekday() == 4 and now.hour >= self.flat_friday_hour:
                    exit_price, reason = c[i], "friday_close"
                if exit_price is not None:
                    close_trade(pos, exit_price, now, reason, bars_held)
                    pos = None
                    if day_pnl <= -self.daily_loss_pct * balance:
                        locked_until = today + timedelta(days=1)
                        res.daily_lockouts += 1
                    peak = max(peak, balance)
                    res.max_dd_pct = max(res.max_dd_pct, 100 * (peak - balance) / peak)

            # C) friday flat: no new entries ------------------------------
            friday_flat = now.weekday() == 4 and now.hour >= self.flat_friday_hour

            # D) next-bar signal from this bar's close --------------------
            if pos is None and pending is None and not locked and not friday_flat:
                session = sessions[i]
                ctx = BarCtx(
                    i=index_offset + i,
                    now=now,
                    open=o[i],
                    high=hi[i],
                    low=lo[i],
                    close=c[i],
                    session=session,
                    spread_pips=spread_pips(session),
                    pip_size=pip,
                )
                entry = strategy.decide(ctx, features)
                if entry is not None:
                    pending = (entry, ctx)

            peak = max(peak, balance)
            res.max_dd_pct = max(res.max_dd_pct, 100 * (peak - balance) / peak)

        # End of stream: an unresolved position is a REAL trade — liquidate
        # at the last bar's close (long at bid, short at ask) instead of
        # silently erasing it (silent erasure understates exposure).
        if pos is not None:
            last_spread_usd = self.spreads[sessions[-1]][0] * pip
            liq = c[-1] if pos["side"] == "long" else c[-1] + last_spread_usd
            close_trade(pos, liq, ts[-1], "end_of_data", i - pos["entry_i"])
            pos = None
        elif pending is not None:
            pending = None  # unfilled at end of stream: abandoned, as live
        res.final_balance = balance
        return res
