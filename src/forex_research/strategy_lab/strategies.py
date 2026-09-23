"""The five candidate strategies (STRAT-004).

Each is a self-contained LabStrategy with a per-symbol param dict. Every
entry is next-bar-open filled by the engine (no intrabar signal fills), every
exit is broker-side SL/TP with stop-priority ambiguity, and every entry
survives a GATE-022 cost veto or it never existed. The strategies share one
feature computation (features.py) so train/validate comparisons compare
strategies, not feature pipelines.

The five families:

1. TrendPullback   — pullback to the fast MA inside a slow-MA trend, RSI filter.
2. OpeningRange    — break of the first N hours' range, ATR stop, session-gated.
3. LondonMomentum  — first impulse after the London open, continuation entry.
4. MeanRevert      — vol-spike fade back to the fast MA (ranged sessions only).
5. DonchianBreakout — classic N-bar channel breakout with an ATR trailing stop
                     emulated by re-arming (exit on opposite channel touch).
"""

from __future__ import annotations

import math

from .engine import MIN_STOP_PIPS, BarCtx, Entry, LabStrategy

_WARM_BARS = 400  # universal warmup: covers slow=200 grids and rsi 14


def _mk(ctx: BarCtx, side: str, raw_stop: float, rr: float, note: str) -> Entry:
    """Build an Entry whose stop respects the viability floor: a stop narrower
    than MIN_STOP_PIPS is widened (never dropped), and the target recomputed
    from the effective distance so the RR the engine trades is the RR asked
    for. This is the strategy-side twin of the engine's GATE-022 veto."""
    floor = MIN_STOP_PIPS * ctx.pip_size
    if side == "long":
        d = max(ctx.close - raw_stop, floor)
        return Entry(side, ctx.close - d, ctx.close + rr * d, note)
    d = max(raw_stop - ctx.close, floor)
    return Entry(side, ctx.close + d, ctx.close - rr * d, note)


def _floor_stop(ctx: BarCtx, side: str, raw_stop: float, target: float, note: str) -> Entry:
    """Same floor, but the target is strategy-fixed (mean-reversion aims at
    the MA, not at an RR multiple)."""
    floor = MIN_STOP_PIPS * ctx.pip_size
    if side == "long":
        d = max(ctx.close - raw_stop, floor)
        return Entry(side, ctx.close - d, target, note)
    d = max(raw_stop - ctx.close, floor)
    return Entry(side, ctx.close + d, target, note)


class TrendPullback(LabStrategy):
    """Pullback to SMA-fast inside an SMA-slow trend; enter on the resumption
    bar (close crossing back above fast MA after being below it)."""

    name = "trend_pullback"

    def decide(self, ctx: BarCtx, f: dict[str, list[float]]) -> Entry | None:
        p = self.params
        i = ctx.i
        sf = f["sma_fast"][i]
        ss = f["sma_slow"][p["slow"]][i]
        atr = f["atr"][i]
        rsi = f["rsi"][i]
        if any(math.isnan(v) for v in (sf, ss, atr)) or math.isnan(rsi):
            return None
        if atr <= 0:
            return None
        atr_pips = atr / ctx.pip_size  # sanity floor: skip dead-volatility bars
        if atr_pips < p["min_atr_pips"]:
            return None
        close_prev = f["close"][i - 1]
        sf_prev = f["sma_fast"][i - 1]
        up = ss < sf
        dn = ss > sf
        ss_prev = f["sma_slow"][p["slow"]][i - 1]
        if (
            up
            and sf_prev is not None
            and close_prev < ss_prev
            and ctx.close > sf
            and rsi > p["rsi_min"]
        ):
            return _mk(
                ctx,
                "long",
                min(f["ll"][24][i], ctx.close - p["atr_x"] * atr),
                p["rr"],
                "trend-pb-long",
            )
        if (
            dn
            and sf_prev is not None
            and close_prev > ss_prev
            and ctx.close < sf
            and rsi < p["rsi_max"]
        ):
            return _mk(
                ctx,
                "short",
                max(f["hh"][24][i], ctx.close + p["atr_x"] * atr),
                p["rr"],
                "trend-pb-short",
            )
        return None

    @staticmethod
    def grid() -> list[dict]:
        out = []
        for slow in (100, 200):
            for rr in (1.5, 2.0, 3.0):
                for atr_x in (1.5, 2.5):
                    out.append(
                        {
                            "slow": slow,
                            "rr": rr,
                            "atr_x": atr_x,
                            "rsi_min": 48.0,
                            "rsi_max": 52.0,
                            "min_atr_pips": 1.0,
                        }
                    )
        return out


class OpeningRangeBreak(LabStrategy):
    """Break of the first ``orb_hours`` hours of the NY session (13:00Z),
    once per day, in the break direction."""

    name = "opening_range_break"

    def __init__(self) -> None:
        self.armed_day = None
        self.or_high = None
        self.or_low = None
        self.or_hour_end = None

    def decide(self, ctx: BarCtx, f: dict[str, list[float]]) -> Entry | None:
        p = self.params
        i = ctx.i
        atr = f["atr"][i]
        if math.isnan(atr) or atr <= 0:
            return None
        day = ctx.now.date()
        hour = ctx.now.hour
        # Build the opening range during the first orb_hours of the NY session.
        if hour < 13 or hour >= 22:
            return None
        if self.armed_day != day:
            if hour == 13 and ctx.now.minute == 0:
                self.armed_day = day
                self.or_high = ctx.high
                self.or_low = ctx.low
                self.or_hour_end = 13 + int(p["orb_hours"])
                return None
            return None
        if hour < self.or_hour_end:
            self.or_high = max(self.or_high, ctx.high)
            self.or_low = min(self.or_low, ctx.low)
            return None
        if self.or_hour_end <= hour < 20:
            rng = self.or_high - self.or_low
            if rng <= 0:
                return None
            if ctx.close > self.or_high:
                return _mk(ctx, "long", ctx.close - p["atr_x"] * atr, p["rr"], "orb-long")
            if ctx.close < self.or_low:
                return _mk(ctx, "short", ctx.close + p["atr_x"] * atr, p["rr"], "orb-short")
        return None

    @staticmethod
    def grid() -> list[dict]:
        out = []
        for orb_hours in (1, 2):
            for rr in (1.5, 2.0, 3.0):
                for atr_x in (1.0, 1.5, 2.0):
                    out.append(
                        {"orb_hours": orb_hours, "rr": rr, "atr_x": atr_x, "min_atr_pips": 1.0}
                    )
        return out


class LondonMomentum(LabStrategy):
    """First strong M5-scale impulse after the London open (07:00Z), entered
    on continuation: momentum bar then a close beyond its extreme."""

    name = "london_momentum"

    def __init__(self) -> None:
        self.impulse = None  # (day, side, extreme)

    def decide(self, ctx: BarCtx, f: dict[str, list[float]]) -> Entry | None:
        p = self.params
        i = ctx.i
        atr = f["atr"][i]
        if math.isnan(atr) or atr <= 0:
            return None
        day = ctx.now.date()
        hour = ctx.now.hour
        hour_end = p.get("hour_end", 11)
        if hour < 7 or hour >= hour_end:
            self.impulse = None if hour >= hour_end else self.impulse
            return None
        if self.impulse is not None and self.impulse[0] != day:
            self.impulse = None
        rng = ctx.high - ctx.low
        if self.impulse is None:
            if rng >= p["impulse_atr"] * atr:
                side = "long" if ctx.close > ctx.open else "short"
                extreme = ctx.high if side == "long" else ctx.low
                self.impulse = (day, side, extreme)
            return None
        _, side, extreme = self.impulse
        if side == "long" and ctx.close > extreme:
            self.impulse = None  # one shot per impulse
            return _mk(
                ctx,
                "long",
                min(f["ll"][60][i], ctx.close - p["atr_x"] * atr),
                p["rr"],
                "lon-mom-long",
            )
        if side == "short" and ctx.close < extreme:
            self.impulse = None
            return _mk(
                ctx,
                "short",
                max(f["hh"][60][i], ctx.close + p["atr_x"] * atr),
                p["rr"],
                "lon-mom-short",
            )
        return None

    @staticmethod
    def grid() -> list[dict]:
        # Round 2: adds session-length and vol-floor variants after the first
        # sweep showed out-of-sample survival concentrated in this family.
        out = []
        for impulse_atr in (0.75, 1.0, 1.5):
            for rr in (1.5, 2.0, 3.0):
                for atr_x in (1.0, 1.5):
                    for hour_end in (10, 11):
                        out.append(
                            {
                                "impulse_atr": impulse_atr,
                                "rr": rr,
                                "atr_x": atr_x,
                                "hour_end": hour_end,
                                "min_atr_pips": 1.0,
                            }
                        )
        return out


class MeanRevert(LabStrategy):
    """Fade a vol-spike back toward the fast MA, only when the day so far is
    range-bound (close still inside yesterday's range)."""

    name = "mean_revert"

    def decide(self, ctx: BarCtx, f: dict[str, list[float]]) -> Entry | None:
        p = self.params
        i = ctx.i
        sf = f["sma_fast"][i]
        rz = f["range_z"][i]
        atr = f["atr"][i]
        pdh, pdl = f["prev_day_high"][i], f["prev_day_low"][i]
        if any(math.isnan(v) for v in (sf, rz, atr, pdh, pdl)) or atr <= 0:
            return None
        if rz < p["range_z"]:
            return None
        inside_range = pdl <= ctx.close <= pdh
        dist = ctx.close - sf
        if dist < 0 and inside_range and ctx.low <= sf - p["dist_atr"] * atr:
            return _floor_stop(ctx, "long", ctx.low - p["stop_atr"] * atr, sf, "mr-long")
        if dist > 0 and inside_range and ctx.high >= sf + p["dist_atr"] * atr:
            return _floor_stop(ctx, "short", ctx.high + p["stop_atr"] * atr, sf, "mr-short")
        return None

    @staticmethod
    def grid() -> list[dict]:
        out = []
        for range_z in (1.5, 2.0, 2.5):
            for dist_atr in (0.5, 1.0):
                for stop_atr in (1.0, 1.5):
                    out.append(
                        {
                            "range_z": range_z,
                            "dist_atr": dist_atr,
                            "stop_atr": stop_atr,
                            "min_atr_pips": 1.0,
                        }
                    )
        return out


class DonchianBreakout(LabStrategy):
    """Close above the N-bar high -> long (below N-bar low -> short). One
    position at a time; exits are SL/TP only, no re-entry while in trade."""

    name = "donchian_breakout"

    def decide(self, ctx: BarCtx, f: dict[str, list[float]]) -> Entry | None:
        p = self.params
        i = ctx.i
        atr = f["atr"][i]
        if math.isnan(atr) or atr <= 0:
            return None
        hh_prev = f["hh"][p["channel"]][i - 1]  # channel high EXCLUDING this bar
        ll_prev = f["ll"][p["channel"]][i - 1]
        if math.isnan(hh_prev) or math.isnan(ll_prev):
            return None
        if ctx.close > hh_prev:
            return _mk(ctx, "long", ctx.close - p["atr_x"] * atr, p["rr"], "don-long")
        if ctx.close < ll_prev:
            return _mk(ctx, "short", ctx.close + p["atr_x"] * atr, p["rr"], "don-short")
        return None

    @staticmethod
    def grid() -> list[dict]:
        out = []
        for channel in (48, 96, 240, 336):
            for rr in (2.0, 3.0, 4.0):
                for atr_x in (1.5, 2.0, 2.5):
                    out.append({"channel": channel, "rr": rr, "atr_x": atr_x, "min_atr_pips": 1.0})
        return out


def _nan_safe(v: float) -> float:
    return v if not math.isnan(v) else 0.0


STRATEGIES: dict[str, type[LabStrategy]] = {
    cls.name: cls  # type: ignore[attr-defined]
    for cls in (TrendPullback, OpeningRangeBreak, LondonMomentum, MeanRevert, DonchianBreakout)
}


def make_strategy(name: str, params: dict) -> LabStrategy:
    cls = STRATEGIES[name]
    s = cls()
    s.params = dict(params)
    return s
