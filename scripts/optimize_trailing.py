"""Trailing-portfolio optimizer — the +10%/90-days, DD<8% campaign.

Protocol (same VAL-045 discipline as optimize_lab.py):
  - TRAIN 2025-04-01..2026-03-01: grid selection only.
  - VALIDATE 2026-03-01..2026-09-15: acceptance, untouched by selection.
  - Strategy: TrailingMomentum (the validated LondonMomentum entry) x the
    trailing bracket grid (activation, stop ratchet, TP trail — all in R).
  - Symbols: EURUSD, GBPUSD, XAUUSD (the user's three).
  - Per-book gates: validate PF >= 1.6, DD < 8%, >= 15 trades (PF 2.0 is NOT
    required here: the objective is portfolio return at bounded drawdown,
    and trailing exits change the R distribution the PF gate was calibrated
    for — the honest gate is the portfolio one below).
  - Portfolio stage: accepted books replayed on one $10k balance; risk swept
    to find the largest per-trade risk whose FULL-window DD stays < 8% while
    the 90-day forward return run-rate reaches +10% (measured on the best
    trailing 90-day window AND the median 90-day window — a target hit only
    in the best window is luck, the median is the run-rate).

Usage:
    python scripts/optimize_trailing.py            # full sweep + portfolio
    python scripts/optimize_trailing.py EURUSD     # subset
"""

from __future__ import annotations

import itertools
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


from optimize_lab import (
    GATE1,
    REPO,
    TF_MINUTES,
    VALID_START,
    aggregate_bars,
    load_m1,
    split_index,
)

from forex_research.data.instruments import load_instruments
from forex_research.strategy_lab.engine import M1Lab, session_spreads
from forex_research.strategy_lab.features import compute_features
from forex_research.strategy_lab.strategies import STRATEGIES

OUT = REPO / "data" / "strategy_lab" / "trailing_run.json"
SYMBOLS = ["EURUSD", "GBPUSD", "XAUUSD"]
DD_BUDGET = 8.0
TARGET_90D = 0.10  # +10% per 90 days
PORTFOLIO_RISKS = (0.02, 0.015, 0.01, 0.0075, 0.005)
TRAIL_GRID = [  # the exit shape the user asked for, swept
    {"act_r": a, "sl_r": s, "tp_r": t}
    for a, s, t in itertools.product((0.75, 1.0, 1.5), (0.75, 1.0, 1.5), (1.5, 3.0, 6.0))
]


def run_book(lab, symbol, bars, feats, params, trail, offset=0):
    strat = STRATEGIES["trailing_momentum"]()
    strat.params = dict(params)
    return lab.run(symbol, bars, strat, feats, index_offset=offset)


def metrics(res, lab) -> dict:
    m = res.metrics(lab.balance0)
    return m


def window_90d(trades: list, lo: datetime, hi: datetime, balance0: float) -> float:
    """Return % over [lo, hi) replaying closed trades with compounding."""
    bal = balance0
    for t in sorted((t for t in trades if lo <= t.exit_ts < hi), key=lambda t: t.exit_ts):
        bal *= 1 + t.r * 0.01  # r is in risk units; risk% applied at sweep stage separately
    return 100 * (bal / balance0 - 1)


def ninety_day_windows(
    trades: list, start: datetime, end: datetime, risk_pct: float
) -> tuple[float, float]:
    """(best, median) rolling 90-day return % at fixed risk, closed trades."""
    if not trades:
        return 0.0, 0.0
    rets: list[float] = []
    day = start
    step = timedelta(days=15)
    while day + timedelta(days=90) <= end:
        bal = 10_000.0
        hi = day + timedelta(days=90)
        for t in sorted((t for t in trades if day <= t.exit_ts < hi), key=lambda t: t.exit_ts):
            bal *= 1 + t.r * risk_pct
        rets.append(100 * (bal / 10_000.0 - 1))
        day += step
    if not rets:
        return 0.0, 0.0
    rets.sort()
    return rets[-1], rets[len(rets) // 2]


def main() -> int:
    t0 = time.monotonic()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    symbols = args or SYMBOLS
    specs = load_instruments(REPO / "config" / "instruments.yaml")
    report = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "targets": {"return_90d": TARGET_90D, "max_dd_pct": DD_BUDGET, "balance": 10_000},
        "symbols": {},
    }
    accepted: list[dict] = []

    for symbol in symbols:
        spec = specs[symbol]
        spreads = session_spreads(GATE1, symbol)
        m1 = load_m1(symbol)
        if m1.height < 50_000:
            print(f"{symbol}: insufficient data, skipping", flush=True)
            continue
        bars = aggregate_bars(m1, TF_MINUTES["1h"])
        feats = compute_features(bars)
        ts = bars["ts"].to_list()
        si = split_index(ts)
        train, valid = bars.slice(0, si), bars.slice(si, bars.height - si)
        print(
            f"\n=== {symbol} 1h trailing sweep: {si} train / {bars.height - si} validate bars ===",
            flush=True,
        )
        results = []
        for params in STRATEGIES["trailing_momentum"].grid():
            for trail in TRAIL_GRID:
                lab = M1Lab(spec=spec, spreads=spreads, risk_pct=0.005, trail=trail)
                r = run_book(lab, symbol, train, feats, params, trail)
                results.append((params, trail, r))
        results.sort(
            key=lambda x: (x[2].metrics()["profit_factor"], x[2].metrics()["net_usd"]), reverse=True
        )
        sym_accepts = []
        for params, trail, r in results[:6]:
            m = r.metrics()
            lab = M1Lab(spec=spec, spreads=spreads, risk_pct=0.005, trail=trail)
            v = run_book(lab, symbol, valid, feats, params, trail, offset=si)
            vm = v.metrics()
            ok = vm["profit_factor"] >= 1.6 and v.max_dd_pct < DD_BUDGET and vm["trades"] >= 15
            print(
                f"  {'ACCEPT' if ok else 'reject'} {params} trail={trail} "
                f"train PF {m['profit_factor']:.2f} -> valid PF {vm['profit_factor']:.2f} "
                f"DD {v.max_dd_pct:.2f}% tr {vm['trades']} net {vm['net_usd']:+.0f}",
                flush=True,
            )
            if ok:
                sym_accepts.append(
                    {
                        "symbol": symbol,
                        "params": params,
                        "trail": trail,
                        "train_pf": m["profit_factor"],
                        "val_pf": vm["profit_factor"],
                        "val_dd": v.max_dd_pct,
                        "val_trades": vm["trades"],
                        "val_net": vm["net_usd"],
                    }
                )
        report["symbols"][symbol] = {"accepted": sym_accepts}
        accepted.extend(sym_accepts)

    # -- portfolio stage -----------------------------------------------------
    print(f"\n=== portfolio stage: {len(accepted)} accepted books ===", flush=True)
    verdict = None
    if accepted:
        # Dedupe books whose trade streams are identical (grid variants whose
        # differing parameters never bind produce the same trading).
        seen_fp: set[tuple] = set()
        books = []
        for b in accepted:
            spec = specs[b["symbol"]]
            lab = M1Lab(
                spec=spec,
                spreads=session_spreads(GATE1, b["symbol"]),
                risk_pct=0.005,
                trail=b["trail"],
            )
            m1 = load_m1(b["symbol"])
            bars = aggregate_bars(m1, TF_MINUTES["1h"])
            fp_res = run_book(
                lab, b["symbol"], bars, compute_features(bars), b["params"], b["trail"]
            )
            fp = tuple((t.entry_ts, t.exit_ts, round(t.exit_price, 6)) for t in fp_res.trades)
            if fp in seen_fp:
                continue
            seen_fp.add(fp)
            books.append(b)
        print(f"  {len(books)} distinct books after dedupe", flush=True)
        trades_by_risk = {}
        for risk_pct in PORTFOLIO_RISKS:
            all_trades = []
            for b in books:
                spec = specs[b["symbol"]]
                spreads = session_spreads(GATE1, b["symbol"])
                lab = M1Lab(spec=spec, spreads=spreads, risk_pct=risk_pct, trail=b["trail"])
                m1 = load_m1(b["symbol"])
                bars = aggregate_bars(m1, TF_MINUTES["1h"])
                res = run_book(
                    lab, b["symbol"], bars, compute_features(bars), b["params"], b["trail"]
                )
                all_trades.extend(res.trades)
            # combined equity/DD on closed trades
            bal, peak, max_dd = 10_000.0, 10_000.0, 0.0
            for t in sorted(all_trades, key=lambda t: t.exit_ts):
                bal += t.pnl_usd
                peak = max(peak, bal)
                max_dd = max(max_dd, 100 * (peak - bal) / peak)
            v_start = datetime.fromisoformat(VALID_START)
            end_dt = max((t.exit_ts for t in all_trades), default=v_start)
            best90, med90 = ninety_day_windows(all_trades, v_start, end_dt, risk_pct)
            trades_by_risk[risk_pct] = (max_dd, best90, med90, bal)
            print(
                f"  risk {risk_pct * 100:.2f}%: full DD {max_dd:.2f}%  "
                f"validate-span 90d best {best90:+.1f}% median {med90:+.1f}%  "
                f"balance {bal:,.0f}",
                flush=True,
            )
        fitting = [(rk, v) for rk, v in trades_by_risk.items() if v[0] < DD_BUDGET]
        if fitting:
            # the target: median 90-day run-rate >= 10% at DD < 8%
            risk, (dd, best90, med90, bal) = max(fitting, key=lambda kv: kv[1][2])
            verdict = {
                "risk_per_trade": risk,
                "full_dd_pct": dd,
                "best_90d_pct": best90,
                "median_90d_pct": med90,
                "final_balance": bal,
                "target_hit": med90 >= 100 * TARGET_90D,
            }
    report["portfolio"] = verdict
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nDone in {time.monotonic() - t0:.0f}s. Report: {OUT}")
    if verdict:
        hit = "TARGET HIT" if verdict["target_hit"] else "TARGET NOT HIT"
        print(
            f"VERDICT [{hit}]: risk {verdict['risk_per_trade'] * 100:.2f}%/trade, "
            f"full DD {verdict['full_dd_pct']:.2f}% (< {DD_BUDGET}%), "
            f"90-day median {verdict['median_90d_pct']:+.1f}% "
            f"(target +{100 * TARGET_90D:.0f}%), best 90d {verdict['best_90d_pct']:+.1f}%"
        )
    else:
        print("VERDICT: no risk level fits the DD budget with the accepted books.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
