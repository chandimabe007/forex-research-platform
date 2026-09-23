"""Lab feature set — one shared computation for all strategies and splits.

Features are computed ONCE per symbol over the full year, then each split
indexes into them by integer position. No strategy call ever sees a feature
value computed from a bar after its own (values at index i use bars <= i);
the train/validate split enforces the rest by construction.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

FEATURE_VERSION = "lab-1"


def aggregate_bars(bars: pl.DataFrame, minutes: int) -> pl.DataFrame:
    """Aggregate an M1 OHLC frame into ``minutes``-bars (floor-anchored UTC).

    The lab engine is timeframe-agnostic; this is how the same strategies
    get replayed on 15m/1h bars, where stop distances are wide enough for
    the Gate-1 cost regime to leave room for an edge (M1 all-in c is
    marginal: the year-long sweep confirmed every strategy loses there).
    Volume is not carried — the lab does not use it.
    """
    return (
        bars.sort("ts")
        .group_by_dynamic("ts", every=f"{minutes}m", label="left", start_by="window")
        .agg(
            pl.col("open").first(),
            pl.col("high").max(),
            pl.col("low").min(),
            pl.col("close").last(),
        )
    )


@dataclass
class LabFeatures:
    close: list[float]
    # Each list is aligned to the bars frame (index i == bar i).
    sma_fast: list[float]
    sma_slow: dict[int, list[float]]  # per-window slow MAs (grid selects)
    atr: list[float]
    hh: dict[int, list[float]]  # rolling highest high per window, incl. bar i
    ll: dict[int, list[float]]  # rolling lowest low per window, incl. bar i
    rsi: list[float]
    ret1: list[float]
    ret5: list[float]
    vol_z: list[float]
    range_z: list[float]
    day_open: list[float]
    prev_day_high: list[float]
    prev_day_low: list[float]

    def __getitem__(self, key: str):
        """Dict-style access so strategies can read f["atr"] regardless of
        whether they were handed the LabFeatures object or a plain dict."""
        return getattr(self, key)

    def at(self, i: int) -> dict[str, float]:
        return {
            "close": self.close[i],
            "sma_fast": self.sma_fast[i],
            "sma_slow": self.sma_slow[i],
            "atr": self.atr[i],
            "hh": self.hh[i],
            "ll": self.ll[i],
            "rsi": self.rsi[i],
            "ret1": self.ret1[i],
            "ret5": self.ret5[i],
            "vol_z": self.vol_z[i],
            "range_z": self.range_z[i],
            "day_open": self.day_open[i],
            "prev_day_high": self.prev_day_high[i],
            "prev_day_low": self.prev_day_low[i],
        }

    def warmup(self, window_slow: int, rsi_window: int) -> int:
        return max(window_slow, rsi_window) + 5


def compute_features(
    bars: pl.DataFrame,
    *,
    fast: int = 20,
    slow: int = 50,
    atr_window: int = 14,
    rsi_window: int = 14,
    hh_ll_windows: tuple[int, ...] = (24, 48, 60, 96, 240, 336),
    slow_windows: tuple[int, ...] = (50, 100, 200),
) -> LabFeatures:
    close = bars["close"].to_list()
    high = bars["high"].to_list()
    low = bars["low"].to_list()
    n = len(close)

    sma_fast = _rolling_mean(close, fast)
    sma_slow = {w: _rolling_mean(close, w) for w in slow_windows}
    tr = _true_range(high, low, close)
    atr = _rolling_mean(tr, atr_window)
    hh = {w: _rolling_max(high, w) for w in hh_ll_windows}
    ll = {w: _rolling_min(low, w) for w in hh_ll_windows}
    rsi = _rsi(close, rsi_window)
    ret1 = [close[i] / close[i - 1] - 1 if i > 0 else 0.0 for i in range(n)]
    ret5 = [close[i] / close[i - 5] - 1 if i >= 5 else 0.0 for i in range(n)]
    ret = [close[i] / close[i - 1] - 1 if i > 0 else 0.0 for i in range(n)]
    vol_z = _zscore(ret, 120)
    rng = [high[i] - low[i] for i in range(n)]
    range_z = _zscore(rng, 120)

    day_open: list[float] = [0.0] * n
    prev_day_high: list[float] = [float("nan")] * n
    prev_day_low: list[float] = [float("nan")] * n
    cur_day = None
    cur_open = 0.0
    # walk once, tracking yesterday's completed extremes
    day_high = float("-inf")
    day_low = float("inf")
    prev_high = float("nan")
    prev_low = float("nan")
    ts_list = bars["ts"].to_list()
    open_list = bars["open"].to_list()
    for i in range(n):
        d = ts_list[i].date()
        if d != cur_day:
            if cur_day is not None:
                prev_high, prev_low = day_high, day_low
            cur_day = d
            cur_open = open_list[i]
            day_high, day_low = float("-inf"), float("inf")
        day_open[i] = cur_open
        day_high = max(day_high, high[i])
        day_low = min(day_low, low[i])
        prev_day_high[i] = prev_high
        prev_day_low[i] = prev_low

    return LabFeatures(
        close=close,
        sma_fast=sma_fast,
        sma_slow=sma_slow,
        atr=atr,
        hh=hh,
        ll=ll,
        rsi=rsi,
        ret1=ret1,
        ret5=ret5,
        vol_z=vol_z,
        range_z=range_z,
        day_open=day_open,
        prev_day_high=prev_day_high,
        prev_day_low=prev_day_low,
    )


def _rolling_mean(xs: list[float], w: int) -> list[float]:
    out = [float("nan")] * len(xs)
    if not xs:
        return out
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= w:
            s -= xs[i - w]
        if i >= w - 1:
            out[i] = s / w
    return out


def _rolling_max(xs: list[float], w: int) -> list[float]:
    out = [float("nan")] * len(xs)
    from collections import deque

    dq: deque[int] = deque()
    for i, x in enumerate(xs):
        while dq and xs[dq[-1]] <= x:
            dq.pop()
        dq.append(i)
        while dq[0] <= i - w:
            dq.popleft()
        if i >= w - 1:
            out[i] = xs[dq[0]]
    return out


def _rolling_min(xs: list[float], w: int) -> list[float]:
    out = [float("nan")] * len(xs)
    from collections import deque

    dq: deque[int] = deque()
    for i, x in enumerate(xs):
        while dq and xs[dq[-1]] >= x:
            dq.pop()
        dq.append(i)
        while dq[0] <= i - w:
            dq.popleft()
        if i >= w - 1:
            out[i] = xs[dq[0]]
    return out


def _true_range(high: list[float], low: list[float], close: list[float]) -> list[float]:
    out = [0.0] * len(close)
    for i in range(len(close)):
        if i == 0:
            out[i] = high[i] - low[i]
        else:
            out[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    return out


def _rsi(close: list[float], w: int) -> list[float]:
    out = [float("nan")] * len(close)
    if len(close) <= w:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, w + 1):
        ch = close[i] - close[i - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    ag, al = gains / w, losses / w
    out[w] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    for i in range(w + 1, len(close)):
        ch = close[i] - close[i - 1]
        ag = (ag * (w - 1) + max(ch, 0.0)) / w
        al = (al * (w - 1) + max(-ch, 0.0)) / w
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


def _zscore(xs: list[float], w: int) -> list[float]:
    out = [float("nan")] * len(xs)
    s = 0.0
    ss = 0.0
    for i, x in enumerate(xs):
        s += x
        ss += x * x
        if i >= w:
            old = xs[i - w]
            s -= old
            ss -= old * old
        if i >= w - 1:
            m = s / w
            var = max(ss / w - m * m, 0.0)
            sd = var**0.5
            out[i] = (xs[i] - m) / sd if sd > 1e-12 else 0.0
    return out
