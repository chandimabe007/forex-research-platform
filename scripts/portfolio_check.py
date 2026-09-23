"""Portfolio check — replay the optimizer's accepted configs as ONE account.

Each accepted config trades its own symbol (one position at a time per
symbol), so the portfolio is the merge of the per-symbol trade streams on a
shared $10,000 balance. Duplicate accepts (same symbol/tf/strategy, identical
trade stream) collapse to one book. Risk per trade is swept downward until the
combined drawdown fits the 5% budget — PF is scale-free, DD is not, so risk is
the honest sizing lever once entries are fixed.

Reported separately on the training span, the untouched validation span, and
the full window, because a config is only trustworthy when the split it never
influenced agrees with the one it was tuned on.

Usage: python scripts/portfolio_check.py [report.json]
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import optimize_lab  # noqa: E402
from optimize_lab import (  # noqa: E402
    GATE1,
    OUT,
    PF_MIN,
    REPO,
    TF_MINUTES,
    TRAIN_END,
    VALID_START,
    load_m1,
)

from forex_research.data.instruments import load_instruments  # noqa: E402
from forex_research.strategy_lab.engine import M1Lab, session_spreads  # noqa: E402
from forex_research.strategy_lab.features import compute_features  # noqa: E402
from forex_research.strategy_lab.strategies import STRATEGIES  # noqa: E402

RISK_SWEEP = (0.010, 0.0075, 0.005, 0.004, 0.003)
DD_BUDGET = 5.0


def merged_equity(trades: list, start: datetime, end: datetime) -> dict:
    """Closed-PnL equity on one shared balance over [start, end)."""
    seg = [t for t in trades if start <= t.exit_ts < end]
    bal, peak, max_dd = 10_000.0, 10_000.0, 0.0
    for t in sorted(seg, key=lambda t: t.exit_ts):
        bal += t.pnl_usd
        peak = max(peak, bal)
        max_dd = max(max_dd, (peak - bal) / peak * 100)
    gross_win = sum(t.pnl_usd for t in seg if t.pnl_usd > 0)
    gross_loss = -sum(t.pnl_usd for t in seg if t.pnl_usd < 0)
    pf = gross_win / gross_loss if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
    return {
        "trades": len(seg),
        "pf": round(pf, 2),
        "max_dd_pct": round(max_dd, 2),
        "net_usd": round(bal - 10_000, 2),
        "end_balance": round(bal, 2),
    }


def main() -> int:
    report_path = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT
    report = json.loads(report_path.read_text(encoding="utf-8"))
    accepted: dict[str, dict] = {}
    seen_books: set[tuple[str, str, str]] = set()
    for sym, sr in report["symbols"].items():
        for tf, tfr in sr["timeframes"].items():
            for v in sorted(tfr["accepted"], key=lambda v: -v["profit_factor"]):
                book = (sym, tf, v["strategy"])
                if book in seen_books:
                    continue  # identical trade stream already in the portfolio
                seen_books.add(book)
                key = json.dumps(v["params"], sort_keys=True)
                accepted[f"{sym}|{tf}|{key}"] = {
                    "symbol": sym,
                    "tf": tf,
                    "strategy": v["strategy"],
                    "params": v["params"],
                }
    if not accepted:
        print("No accepted configs in report.")
        return 1

    specs = load_instruments(REPO / "config" / "instruments.yaml")
    train_end = datetime.fromisoformat(TRAIN_END)
    valid_start = datetime.fromisoformat(VALID_START)

    # Expensive part once per book: data, bars, features. Cheap part per risk.
    books = []
    for _key, cfg in sorted(accepted.items()):
        spec = specs[cfg["symbol"]]
        m1 = load_m1(cfg["symbol"])
        minutes = TF_MINUTES[cfg["tf"]]
        bars = m1 if minutes == 1 else optimize_lab.aggregate_bars(m1, minutes)
        feats = compute_features(bars)
        books.append((cfg, spec, bars, feats))

    train_end, full_end = train_end, datetime(2999, 1, 1)
    t0_epoch = datetime(1900, 1, 1)
    best = None
    for risk in RISK_SWEEP:
        all_trades: list = []
        print(f"\n--- risk {risk * 100:.2f}% per trade ---")
        print("    per-book full window:")
        for cfg, spec, bars, feats in books:
            lab = M1Lab(spec=spec, spreads=session_spreads(GATE1, cfg["symbol"]), risk_pct=risk)
            strat = STRATEGIES[cfg["strategy"]]()
            strat.params = dict(cfg["params"])
            res = lab.run(cfg["symbol"], bars, strat, feats, index_offset=0)
            m = res.metrics()
            print(
                f"      {cfg['symbol']:7s} {cfg['strategy']:17s} "
                f"PF {m['profit_factor']:5.2f}  DD {res.max_dd_pct:5.2f}%  "
                f"tr {m['trades']:4d}  net {m['net_usd']:+9.2f}"
            )
            all_trades.extend(res.trades)
        print("    combined $10k:")
        rows = {}
        for label, lo, hi in (
            ("TRAIN", t0_epoch, train_end),
            ("VALIDATE", valid_start, full_end),
            ("FULL", t0_epoch, full_end),
        ):
            m = merged_equity(all_trades, lo, hi)
            rows[label] = m
            print(
                f"      {label:8s} PF {m['pf']:5.2f}  DD {m['max_dd_pct']:5.2f}%  "
                f"tr {m['trades']:4d}  net {m['net_usd']:+9.2f}  "
                f"balance {m['end_balance']:10.2f}"
            )
        if rows["FULL"]["max_dd_pct"] < DD_BUDGET and best is None:
            best = (risk, rows)

    if best:
        risk, rows = best
        print(f"\nLARGEST RISK FITTING DD {DD_BUDGET}%: {risk * 100:.2f}% per trade")
        ok = rows["VALIDATE"]["pf"] >= PF_MIN and rows["FULL"]["max_dd_pct"] < DD_BUDGET
        print(
            f"  verdict: {'PASS' if ok else 'FAIL'} "
            f"(valid PF {rows['VALIDATE']['pf']} vs {PF_MIN}, "
            f"full DD {rows['FULL']['max_dd_pct']}% vs {DD_BUDGET}%)"
        )
        return 0 if ok else 1
    print(f"\nNo swept risk fits the {DD_BUDGET}% combined drawdown budget.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
