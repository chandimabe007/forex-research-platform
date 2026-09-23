"""Weighted trailing portfolio — can +10%/90 days fit inside 8% DD?

Combines the two proven static-RR books (PR #11) with the distinct trailing
books, weights each book's risk by inverse validate drawdown (the book that
proved stability carries more risk), and sweeps the scale factor. The target
is judged on rolling 90-day windows across the validate span: the MEDIAN
window must clear +10% (a run-rate, not a lucky best window) while full-window
DD stays under 8%.

Usage: python scripts/portfolio_trailing.py
"""

from __future__ import annotations

import json
import sys
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
)

from forex_research.data.instruments import load_instruments
from forex_research.strategy_lab.engine import M1Lab, session_spreads
from forex_research.strategy_lab.features import compute_features
from forex_research.strategy_lab.strategies import STRATEGIES

DD_BUDGET = 8.0
TARGET_90D = 10.0
SCALES = (0.4, 0.45, 0.5, 0.6, 0.8, 1.0, 1.2, 1.3, 1.4, 1.45, 1.5, 2.0)


def trailing_books() -> list[dict]:
    report = json.loads(
        (REPO / "data" / "strategy_lab" / "trailing_run.json").read_text(encoding="utf-8")
    )
    seen_fp, books = set(), []
    specs = load_instruments(REPO / "config" / "instruments.yaml")
    for sym, sr in report["symbols"].items():
        for b in sr["accepted"]:
            spec = specs[sym]
            lab = M1Lab(
                spec=spec, spreads=session_spreads(GATE1, sym), risk_pct=0.005, trail=b["trail"]
            )
            m1 = load_m1(sym)
            bars = aggregate_bars(m1, TF_MINUTES["1h"])
            strat = STRATEGIES["trailing_momentum"]()
            strat.params = dict(b["params"])
            res = lab.run(sym, bars, strat, compute_features(bars))
            fp = tuple((t.entry_ts, round(t.exit_price, 6)) for t in res.trades)
            if fp in seen_fp:
                continue
            seen_fp.add(fp)
            books.append(
                {
                    "kind": "trailing",
                    "symbol": sym,
                    "params": b["params"],
                    "trail": b["trail"],
                    "val_dd": max(b["val_dd"], 0.5),
                    "val_pf": b["val_pf"],
                }
            )
    return books


def static_books() -> list[dict]:
    report = json.loads(
        (REPO / "data" / "strategy_lab" / "optimization_run.json").read_text(encoding="utf-8")
    )
    seen, books = set(), []
    for sym, sr in report["symbols"].items():
        for tf, tfr in sr["timeframes"].items():
            for v in sorted(tfr["accepted"], key=lambda v: -v["profit_factor"]):
                if (sym, tf) in seen:
                    continue
                seen.add((sym, tf))
                books.append(
                    {
                        "kind": "static",
                        "symbol": sym,
                        "tf": tf,
                        "params": v["params"],
                        "val_dd": max(v["max_dd_pct"], 0.5),
                        "val_pf": v["profit_factor"],
                    }
                )
    return books


CONFIGS = {
    "all/inverse-dd": None,
    "all/equal": "equal",
    "trailing-only/inverse-dd": "trailing",
    "static-only/inverse-dd": "static",
    "all-minus-weakest/inverse-dd": "drop-weak",
}


def main() -> int:
    specs = load_instruments(REPO / "config" / "instruments.yaml")
    books_all = trailing_books() + static_books()
    print(
        f"{len(books_all)} books: "
        + ", ".join(f"{b['symbol']}/{b.get('tf', '1h')}/{b['kind']}" for b in books_all)
    )

    # Replay each book once at nominal 0.5% risk, cache trade streams.
    streams = []
    for b in books_all:
        spec = specs[b["symbol"]]
        spreads = session_spreads(GATE1, b["symbol"])
        m1 = load_m1(b["symbol"])
        minutes = TF_MINUTES[b.get("tf", "1h")]
        bars = m1 if minutes == 1 else aggregate_bars(m1, minutes)
        feats = compute_features(bars)
        if b["kind"] == "trailing":
            lab = M1Lab(spec=spec, spreads=spreads, risk_pct=0.005, trail=b["trail"])
            strat = STRATEGIES["trailing_momentum"]()
        else:
            lab = M1Lab(spec=spec, spreads=spreads, risk_pct=0.005)
            strat = STRATEGIES[b["strategy"] if "strategy" in b else "london_momentum"]()
        strat.params = dict(b["params"])
        res = lab.run(b["symbol"], bars, strat, feats)
        streams.append((b, res.trades))
        print(
            f"  {b['symbol']:7s} {b['kind']:8s} tr {len(res.trades):4d} "
            f"val_dd(was) {b['val_dd']:.2f}%"
        )

    v_start = datetime.fromisoformat(VALID_START)
    overall = None
    for cfg_name, cfg in CONFIGS.items():
        if cfg == "trailing":
            sel = [(b, tr) for b, tr in streams if b["kind"] == "trailing"]
        elif cfg == "static":
            sel = [(b, tr) for b, tr in streams if b["kind"] == "static"]
        elif cfg == "drop-weak":
            # Drop the EURUSD static book: fewest trades, highest validate DD
            # among the survivors of its family.
            sel = [
                (b, tr)
                for b, tr in streams
                if not (b["kind"] == "static" and b["symbol"] == "EURUSD")
            ]
        else:
            sel = streams
        mode = "equal" if cfg == "equal" else "invdd"
        print(f"\n--- config {cfg_name} ({len(sel)} books, {mode}) ---")
        best_cfg = sweep(sel, mode, v_start)
        if best_cfg and (overall is None or best_cfg[2] > overall[2]):
            overall = (cfg_name, *best_cfg)
    if overall:
        name, scale, dd, med90, best90, eq = overall
        print(
            f"\nVERDICT [BEST FRONTIER]: {name} @ {scale:.1f}x -> full DD {dd:.2f}% "
            f"(< {DD_BUDGET}%), median 90d {med90:+.1f}% vs target +{TARGET_90D:.0f}%, "
            f"best 90d {best90:+.1f}%, balance {eq:,.0f}"
        )
        if med90 >= TARGET_90D:
            print("TARGET HIT")
            return 0
        print("TARGET NOT HIT on the median window.")
        return 1
    print("No configuration fit the DD budget.")
    return 1


def sweep(sel, mode: str, v_start: datetime):
    best = None
    for scale in SCALES:
        # Book risk: equal or inverse-validate-DD weighting, capped at 2x base.
        med_dd = sorted(b["val_dd"] for b, _ in sel)[len(sel) // 2]
        weighted = []
        for _b, trades in sel:
            if mode == "equal":
                rk = 0.005 * scale
            else:
                rk = 0.005 * scale * min(med_dd / _b["val_dd"], 2.0)
            weighted.append((rk, _b, trades))
        # ONE shared balance: every trade risks its book's fraction of the
        # account equity at that moment; events merge into one equity curve.
        events = []
        for rk, _b, trades in weighted:
            for t in trades:
                events.append((t.exit_ts, t.r, rk))
        events.sort(key=lambda e: e[0])
        eq, peak, max_dd = 10_000.0, 10_000.0, 0.0
        curve: list[tuple[datetime, float]] = []
        for ts, r, rk in events:
            eq += r * rk * eq
            curve.append((ts, eq))
            peak = max(peak, eq)
            max_dd = max(max_dd, 100 * (peak - eq) / peak)

        curve_local = curve  # bind the current loop's curve, not the name

        def equity_at(day: datetime, _curve=curve_local) -> float:
            """Account equity just before ``day`` (last closed trade)."""
            val = 10_000.0
            for ts, e in _curve:
                if ts >= day:
                    break
                val = e
            return val

        # Rolling 90-day windows: TRUE compounded account growth per window.
        rets = []
        day = v_start
        end = curve[-1][0] if curve else v_start
        while day + timedelta(days=90) <= end:
            e0 = equity_at(day)
            e1 = equity_at(day + timedelta(days=90))
            rets.append(100 * (e1 / e0 - 1))
            day += timedelta(days=15)
        rets.sort()
        med90, best90 = rets[len(rets) // 2], rets[-1]
        wtxt = "/".join(f"{rk * 100:.2f}" for rk, _, _ in weighted)
        verdict = "OK" if (max_dd < DD_BUDGET and med90 >= TARGET_90D) else ""
        print(
            f"{scale:.1f}x  [{wtxt}]  {max_dd:7.2f}%  {med90:+7.1f}%  {best90:+7.1f}%  "
            f"{eq:9,.0f}  {verdict}"
        )
        if max_dd < DD_BUDGET and med90 >= TARGET_90D and best is None:
            best = (scale, max_dd, med90, best90, eq)
        # frontier record: the largest-scale row that stayed inside the budget
        if max_dd < DD_BUDGET and (best is None or scale > best[0]):
            best = (scale, max_dd, med90, best90, eq)
    return best


if __name__ == "__main__":
    raise SystemExit(main())
