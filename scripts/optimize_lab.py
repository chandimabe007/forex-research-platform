"""Strategy-lab optimizer — 5 strategies x 4 symbols x 3 timeframes, 1 year.

Protocol (VAL-045 discipline):
  - Window 2025-04-01 .. 2026-09-15 (17.5 months of Dukascopy M1).
  - TRAIN: 2025-04-01 .. 2026-03-01. Grid-search every strategy/symbol/TF.
  - VALIDATE: 2026-03-01 .. 2026-09-15. Top train-PF configs replay here
    ONCE each — numbers a grid never tuned on.
  - Acceptance gates (the user's challenge targets): profit factor >= 2.0,
    max drawdown < 5% (10k account) on the VALIDATE replay (train is the
    selection span; validate is the acceptance span), plus a 15-trade
    minimum so a lucky trio cannot pass.
  - Every validate replay is appended to the VAL-045 trial ledger with
    fill_mode="lab_m1" so downstream VAL-040 sees the honest trial count.

Why three timeframes: the M1 sweep is the cost regime's control — Gate 1
predicted all-in costs eat any M1 edge (confirmed: every strategy loses on
M1 over the year). 15m and 1h carry the same strategies on wider stops
where costs are a single-digit share of risk.

Usage:
    python scripts/optimize_lab.py                # full sweep
    python scripts/optimize_lab.py EURUSD USDJPY  # subset of symbols
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import polars as pl

from forex_research.data.candles import CANDLE_SCHEMA
from forex_research.data.instruments import load_instruments
from forex_research.strategy_lab.engine import M1Lab, session_spreads
from forex_research.strategy_lab.features import (
    FEATURE_VERSION,
    aggregate_bars,
    compute_features,
)
from forex_research.strategy_lab.strategies import STRATEGIES
from forex_research.validation.trial_ledger import TrialLedger

REPO = Path(__file__).resolve().parents[1]
CACHE = REPO / "data" / "raw" / "candles_m1"
GATE1 = REPO / "data" / "gate1" / "gate1_20260921_run.json"
LEDGER = REPO / "data" / "validation" / "lab_trials.jsonl"
OUT = REPO / "data" / "strategy_lab" / "optimization_run.json"

TRAIN_START, TRAIN_END = "2025-04-01", "2026-03-01"
VALID_START, VALID_END = "2026-03-01", "2026-09-15"
SPLIT = dt.datetime(2026, 3, 1)

PF_MIN = 2.0
DD_MAX_PCT = 5.0
MIN_TRADES = 15
TOP_K_PER_CELL = 3  # configs promoted from train to validate per strategy/symbol/TF

SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "XAUUSD"]
TF_MINUTES = {"1m": 1, "15m": 15, "1h": 60}


def load_m1(symbol: str) -> pl.DataFrame:
    days = sorted(CACHE.glob(f"{symbol}_*.parquet"))
    if not days:
        raise SystemExit(f"{symbol}: no cached candle days — run the download first")
    frames = []
    for p in days:
        day = dt.date(int(p.stem[7:11]), int(p.stem[11:13]), int(p.stem[13:15]))
        if dt.date.fromisoformat(TRAIN_START) <= day < dt.date.fromisoformat(VALID_END):
            if p.stat().st_size:
                frames.append(pl.read_parquet(p))
    if not frames:
        return pl.DataFrame(schema=CANDLE_SCHEMA)
    return pl.concat(frames).sort("ts").unique(subset=["ts"], keep="last", maintain_order=True)


def split_index(ts: list) -> int:
    """First bar index at/after the train/validate boundary."""
    lo = 0
    while lo < len(ts) and ts[lo] < SPLIT:
        lo += 1
    return lo


def run_cell(
    lab: M1Lab, symbol: str, bars: pl.DataFrame, feats, name: str, params: dict, *, offset: int
) -> dict:
    strat = STRATEGIES[name]()
    strat.params = dict(params)
    res = lab.run(symbol, bars, strat, feats, index_offset=offset)
    m = res.metrics(lab.balance0)
    r_vals = [t.r for t in res.trades]
    if len(r_vals) >= 2:
        mean = sum(r_vals) / len(r_vals)
        var = sum((x - mean) ** 2 for x in r_vals) / (len(r_vals) - 1)
        sd = var**0.5
        sr = mean / sd if sd > 1e-9 else 0.0
    else:
        sr = 0.0
    return {"strategy": name, "params": params, "sr_periodic": sr, **m}


def main() -> int:
    args = sys.argv[1:]
    tf_arg = next((a.split("=", 1)[1] for a in args if a.startswith("--tfs=")), "15m,1h")
    tfs = {t: TF_MINUTES[t] for t in tf_arg.split(",") if t in TF_MINUTES}
    symbols = list(dict.fromkeys(a for a in args if not a.startswith("--")) or SYMBOLS)
    print(f"timeframes: {list(tfs)}   symbols: {symbols}", flush=True)
    specs = load_instruments(REPO / "config" / "instruments.yaml")
    ledger = TrialLedger(LEDGER)
    t0 = time.monotonic()
    report: dict = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "window": {"train": [TRAIN_START, TRAIN_END], "validate": [VALID_START, VALID_END]},
        "gates": {
            "profit_factor_min": PF_MIN,
            "max_dd_pct": DD_MAX_PCT,
            "min_trades": MIN_TRADES,
            "balance": 10000,
        },
        "symbols": {},
    }

    for symbol in symbols:
        spec = specs[symbol]
        spreads = session_spreads(GATE1, symbol)
        lab = M1Lab(spec=spec, spreads=spreads)
        m1 = load_m1(symbol)
        if m1.height < 50_000:
            print(
                f"{symbol}: only {m1.height} M1 bars — skipping (incomplete download?)", flush=True
            )
            continue
        sym_report = {"spreads_pips": {k: list(v) for k, v in spreads.items()}, "timeframes": {}}

        for tf, minutes in tfs.items():
            bars = aggregate_bars(m1, minutes) if minutes > 1 else m1
            feats = compute_features(bars)
            ts = bars["ts"].to_list()
            si = split_index(ts)
            bars_train = bars.slice(0, si)
            bars_valid = bars.slice(si, bars.height - si)
            print(
                f"\n=== {symbol} [{tf}]: {si} train + {bars.height - si} validate bars ===",
                flush=True,
            )
            tf_report = {"train": {}, "validated": [], "accepted": []}

            for name, cls in STRATEGIES.items():
                results = []
                for params in cls.grid():
                    r = run_cell(lab, symbol, bars_train, feats, name, params, offset=0)
                    results.append(r)
                results.sort(key=lambda r: (r["profit_factor"], r["net_usd"]), reverse=True)
                tf_report["train"][name] = results
                best = results[0]
                print(
                    f"  {name:20s} train: {len(results)} cfgs, best PF {best['profit_factor']:5.2f} "
                    f"({best['trades']:4d} tr, DD {best['max_dd_pct']:5.1f}%, {best['net_usd']:+8.0f} USD)",
                    flush=True,
                )

                for r in results[:TOP_K_PER_CELL]:
                    v = run_cell(lab, symbol, bars_valid, feats, name, r["params"], offset=si)
                    v["train_pf"] = r["profit_factor"]
                    tf_report["validated"].append(v)
                    ledger.append(
                        ledger.new_record(
                            candidate_id=f"{symbol}:{tf}:{name}:{json.dumps(r['params'], sort_keys=True)}",
                            feature_version=FEATURE_VERSION,
                            selection_criterion="lab_pf2_dd5",
                            fill_mode="lab_m1",
                            rule="top3-by-train-pf",
                            sr_periodic=v["sr_periodic"],
                            n_observations=max(v["trades"], 2),
                            notes=(
                                f"validate PF {v['profit_factor']} DD {v['max_dd_pct']}% "
                                f"net {v['net_usd']}usd train PF {v['train_pf']}"
                            ),
                        )
                    )
                    if (
                        v["profit_factor"] >= PF_MIN
                        and v["max_dd_pct"] < DD_MAX_PCT
                        and v["trades"] >= MIN_TRADES
                    ):
                        tf_report["accepted"].append(v)
                        print(
                            f"    ACCEPT {name} {r['params']} -> valid PF {v['profit_factor']:.2f} "
                            f"DD {v['max_dd_pct']:.2f}% tr {v['trades']} net {v['net_usd']:+.0f}",
                            flush=True,
                        )

            sym_report["timeframes"][tf] = tf_report
            # Incremental durable output: a killed run keeps every finished TF.
            report["symbols"][symbol] = sym_report
            OUT.parent.mkdir(parents=True, exist_ok=True)
            OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    n_acc = sum(
        len(tf["accepted"]) for s in report["symbols"].values() for tf in s["timeframes"].values()
    )
    print(f"\nDone in {time.monotonic() - t0:.0f}s. Accepted configs: {n_acc}. Report: {OUT}")
    print(f"Ledger trials: {ledger.audit_count()} at {LEDGER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
