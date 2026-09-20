"""Feature library — FEAT-003, FEAT-004, FEAT-010.

Causal transforms only: trailing windows, expanding statistics, no centred
windows, no full-series ranks, no calendar ``actual`` before release. Every
function satisfies the FEAT-004 contract::

    compute(df, *, params) -> df
    Returns feature columns plus available_at.
    MUST use only trailing windows.
    MUST be pure: identical input, identical output, no global state, no I/O.

Regimes (FEAT-010) are computed from closed bars only and all satisfy
``available_at <= decision_time``. Regime is a reporting dimension, not a
gate.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl


@dataclass(frozen=True)
class FeatureParams:
    """Declared parameters with plausible ranges (STRAT-020 spirit)."""

    ret_window: int = 1
    atr_window: int = 14
    vol_regime_window: int = 20        # trailing 20-bar realised ATR
    vol_regime_lookback: int = 252     # trailing 252-bar distribution
    trend_window: int = 200
    spread_regime_window: int = 60
    spread_elevated_ratio: float = 1.5


def _mid_frame(bars: pl.DataFrame) -> pl.DataFrame:
    """Closed-bar inputs, both quote sides preserved (DATA-003)."""
    return bars.select(
        pl.col("available_at"),
        pl.col("bid_open"),
        pl.col("bid_high"),
        pl.col("bid_low"),
        pl.col("bid_close"),
        pl.col("ask_close"),
        (pl.col("ask_close") - pl.col("bid_close")).alias("spread"),
    ).sort("available_at")


def compute_return(df: pl.DataFrame, *, params: FeatureParams) -> pl.DataFrame:
    """ret_1: trailing 1-bar return of bid_close."""
    return df.with_columns(
        pl.col("bid_close").pct_change(params.ret_window).alias("ret_1")
    )


def compute_atr(df: pl.DataFrame, *, params: FeatureParams) -> pl.DataFrame:
    """atr: trailing ATR from true range (trailing window only)."""
    true_range = pl.max_horizontal(
        pl.col("bid_high") - pl.col("bid_low"),
        (pl.col("bid_close") - pl.col("bid_close").shift(1)).abs(),
    )
    return df.with_columns(
        true_range.alias("_tr")
    ).with_columns(
        pl.col("_tr").rolling_mean(window_size=params.atr_window).alias("atr")
    ).drop("_tr")


def compute_spread_stats(df: pl.DataFrame, *, params: FeatureParams) -> pl.DataFrame:
    """Spread level vs its trailing median (FEAT-010 spread regime basis)."""
    return df.with_columns(
        pl.col("spread")
        .rolling_median(window_size=params.spread_regime_window)
        .alias("spread_median")
    ).with_columns(
        (pl.col("spread") / pl.col("spread_median")).alias("spread_ratio")
    )


def compute_vol_regime(df: pl.DataFrame, *, params: FeatureParams) -> pl.DataFrame:
    """Volatility regime: trailing 20-bar ATR as a percentile of the trailing
    252-bar distribution — quintiles 1-5 (FEAT-010)."""
    df = df.with_columns(
        pl.col("atr").rolling_mean(window_size=params.vol_regime_window).alias("_atr20")
    )
    # Quintile thresholds from trailing quantiles — never full-series rank.
    q20 = pl.col("_atr20").rolling_quantile(0.2, window_size=params.vol_regime_lookback)
    q40 = pl.col("_atr20").rolling_quantile(0.4, window_size=params.vol_regime_lookback)
    q60 = pl.col("_atr20").rolling_quantile(0.6, window_size=params.vol_regime_lookback)
    q80 = pl.col("_atr20").rolling_quantile(0.8, window_size=params.vol_regime_lookback)
    return (
        df.with_columns([q20.alias("_q20"), q40.alias("_q40"), q60.alias("_q60"), q80.alias("_q80")])
        .with_columns(
            pl.when(pl.col("_atr20").is_null() | pl.col("_q80").is_null())
            .then(None)
            .when(pl.col("_atr20") <= pl.col("_q20"))
            .then(1)
            .when(pl.col("_atr20") <= pl.col("_q40"))
            .then(2)
            .when(pl.col("_atr20") <= pl.col("_q60"))
            .then(3)
            .when(pl.col("_atr20") <= pl.col("_q80"))
            .then(4)
            .otherwise(5)
            .alias("vol_regime_quintile")
        )
        .drop(["_atr20", "_q20", "_q40", "_q60", "_q80"])
    )


def compute_trend_regime(df: pl.DataFrame, *, params: FeatureParams) -> pl.DataFrame:
    """Trend regime: close vs trailing mean with a trailing directional-strength
    measure (FEAT-010). Up / Down / None."""
    mean = pl.col("bid_close").rolling_mean(window_size=params.trend_window)
    std = pl.col("bid_close").rolling_std(window_size=params.trend_window)
    df = df.with_columns([mean.alias("_mean"), std.alias("_std")])
    return (
        df.with_columns(
            ((pl.col("bid_close") - pl.col("_mean")) / pl.col("_std")).alias("trend_strength")
        )
        .with_columns(
            pl.when(pl.col("trend_strength").is_null())
            .then(None)
            .when(pl.col("trend_strength") > 0.5)
            .then(pl.lit("up"))
            .when(pl.col("trend_strength") < -0.5)
            .then(pl.lit("down"))
            .otherwise(pl.lit("none"))
            .alias("trend_state")
        )
        .drop(["_mean", "_std"])
    )


def compute_spread_regime(df: pl.DataFrame, *, params: FeatureParams) -> pl.DataFrame:
    """Spread regime: current spread vs trailing median — Normal / Elevated
    (>1.5x) (FEAT-010)."""
    return df.with_columns(
        pl.when(pl.col("spread_ratio").is_null())
        .then(None)
        .when(pl.col("spread_ratio") > params.spread_elevated_ratio)
        .then(pl.lit("elevated"))
        .otherwise(pl.lit("normal"))
        .alias("spread_regime")
    )


FEATURE_COLUMNS = [
    "ret_1",
    "atr",
    "spread_median",
    "spread_ratio",
    "vol_regime_quintile",
    "trend_strength",
    "trend_state",
    "spread_regime",
]


def compute_features(bars: pl.DataFrame, *, params: FeatureParams | None = None) -> pl.DataFrame:
    """Compose every registered feature. Pure: identical input, identical
    output (FEAT-004). Index is ``available_at`` (FEAT-001)."""
    params = params or FeatureParams()
    df = _mid_frame(bars)
    df = compute_return(df, params=params)
    df = compute_atr(df, params=params)
    df = compute_spread_stats(df, params=params)
    df = compute_vol_regime(df, params=params)
    df = compute_trend_regime(df, params=params)
    df = compute_spread_regime(df, params=params)
    return df.select(["available_at"] + FEATURE_COLUMNS)


def compute_session_regime(
    bars: pl.DataFrame, *, resolver
) -> pl.DataFrame:
    """Session regime from DST-aware exchange-local windows (FEAT-010,
    COST-011). Pure given the resolver (an immutable value)."""
    buckets = [
        resolver.bucket(ts.replace(tzinfo=None)) if ts.tzinfo is None else resolver.bucket(ts)
        for ts in bars["available_at"].to_list()
    ]
    return bars.select(["available_at"]).with_columns(
        pl.Series("session_regime", buckets, dtype=pl.String)
    )
