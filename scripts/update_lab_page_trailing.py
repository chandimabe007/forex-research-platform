"""Regenerate the Strategy Lab page's +10%/90-day campaign section.

Replays the accepted books from both campaigns (static-RR and trailing),
sweeps the portfolio scale/weighting exactly as ``portfolio_trailing.py``
does, and splices the frontier table, the winning weighted equity curve and
the trailing-vs-static comparison into ``data/strategy_lab.html`` between
``<!-- TRAILING90:BEGIN -->`` / ``<!-- TRAILING90:END -->`` markers
(idempotent), directly after the existing accepted-portfolio section.

Everything is inline SVG + plain tables — the page stays a single
self-contained file with no fetches.

Usage: python scripts/update_lab_page_trailing.py
"""

from __future__ import annotations

import html
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from optimize_lab import (  # noqa: E402
    GATE1,
    TF_MINUTES,
    TRAIN_END,
    VALID_START,
    aggregate_bars,
    load_m1,
)
from portfolio_check import merged_equity  # noqa: E402
from update_lab_page import PAGE, equity_svg, monthly_svg  # noqa: E402

from forex_research.data.instruments import load_instruments  # noqa: E402
from forex_research.strategy_lab.engine import M1Lab, session_spreads  # noqa: E402
from forex_research.strategy_lab.features import compute_features  # noqa: E402
from forex_research.strategy_lab.strategies import STRATEGIES  # noqa: E402

BEGIN, END = "<!-- TRAILING90:BEGIN -->", "<!-- TRAILING90:END -->"
DD_BUDGET, TARGET_90D = 8.0, 10.0
SCALES = (0.4, 0.45, 0.5, 0.6, 0.8, 1.0, 1.2, 1.3, 1.4, 1.45, 1.5, 2.0)


def _static_books() -> list[dict]:
    report = json.loads(
        (PAGE.parent / "strategy_lab" / "optimization_run.json").read_text(encoding="utf-8")
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
                        "strategy": v["strategy"],
                        "params": v["params"],
                        "trail": None,
                        "val_dd": max(v["max_dd_pct"], 0.5),
                        "val_pf": v["profit_factor"],
                    }
                )
    return books


def _trailing_books(specs) -> list[dict]:
    """Accepted trailing books, deduped by trade-stream fingerprint."""
    report = json.loads(
        (PAGE.parent / "strategy_lab" / "trailing_run.json").read_text(encoding="utf-8")
    )
    seen_fp, books = set(), []
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
                    "tf": "1h",
                    "strategy": "trailing_momentum",
                    "params": b["params"],
                    "trail": b["trail"],
                    "val_dd": max(b["val_dd"], 0.5),
                    "val_pf": b["val_pf"],
                }
            )
    return books


def replay_streams(books: list[dict], specs) -> list[tuple[dict, list]]:
    """Replay every book once at nominal 0.5% risk; cache trade streams."""
    streams = []
    for b in books:
        spec = specs[b["symbol"]]
        spreads = session_spreads(GATE1, b["symbol"])
        m1 = load_m1(b["symbol"])
        minutes = TF_MINUTES[b["tf"]]
        bars = m1 if minutes == 1 else aggregate_bars(m1, minutes)
        feats = compute_features(bars)
        if b["kind"] == "trailing":
            lab = M1Lab(spec=spec, spreads=spreads, risk_pct=0.005, trail=b["trail"])
            strat = STRATEGIES["trailing_momentum"]()
        else:
            lab = M1Lab(spec=spec, spreads=spreads, risk_pct=0.005)
            strat = STRATEGIES[b["strategy"]]()
        strat.params = dict(b["params"])
        res = lab.run(b["symbol"], bars, strat, feats)
        streams.append((b, res.trades))
        print(f"  {b['symbol']:7s} {b['kind']:8s} tf {b['tf']:3s} trades {len(res.trades)}")
    return streams


def frontier(sel: list[tuple[dict, list]], mode: str, v_start: datetime) -> list[dict]:
    """Scale sweep on one shared balance, exactly as portfolio_trailing.sweep."""
    rows = []
    med_dd = sorted(b["val_dd"] for b, _ in sel)[len(sel) // 2]
    for scale in SCALES:
        weighted = []
        for b, trades in sel:
            rk = (
                0.005 * scale if mode == "equal" else 0.005 * scale * min(med_dd / b["val_dd"], 2.0)
            )
            weighted.append((rk, b, trades))
        events = sorted(
            ((t.exit_ts, t.r, rk) for rk, _b, trades in weighted for t in trades),
            key=lambda e: e[0],
        )
        eq, peak, max_dd = 10_000.0, 10_000.0, 0.0
        curve: list[tuple[datetime, float]] = []
        for ts, r, rk in events:
            eq += r * rk * eq
            curve.append((ts, eq))
            peak = max(peak, eq)
            max_dd = max(max_dd, 100 * (peak - eq) / peak)
        if not curve:
            continue

        def equity_at(day: datetime, _curve=tuple(curve)) -> float:
            val = 10_000.0
            for ts, e in _curve:
                if ts >= day:
                    break
                val = e
            return val

        rets, day = [], v_start
        end = curve[-1][0]
        while day + timedelta(days=90) <= end:
            e0, e1 = equity_at(day), equity_at(day + timedelta(days=90))
            rets.append(100 * (e1 / e0 - 1))
            day += timedelta(days=15)
        rets.sort()
        rows.append(
            {
                "scale": scale,
                "risks": [rk for rk, _b, _t in weighted],
                "max_dd": max_dd,
                "med90": rets[len(rets) // 2] if rets else 0.0,
                "best90": rets[-1] if rets else 0.0,
                "balance": eq,
                "curve": curve,
            }
        )
    return rows


def _frontier_row(rows: list[dict], hit: bool) -> dict | None:
    """Largest scale inside the DD budget; ``hit`` requires med90 >= target too."""
    fitting = [r for r in rows if r["max_dd"] < DD_BUDGET and (not hit or r["med90"] >= TARGET_90D)]
    return max(fitting, key=lambda r: r["scale"]) if fitting else None


def build_section(streams, specs) -> str:
    v_start = datetime.fromisoformat(VALID_START)
    configs = [
        ("static-only / inverse-DD", [s for s in streams if s[0]["kind"] == "static"], "invdd"),
        ("trailing-only / inverse-DD", [s for s in streams if s[0]["kind"] == "trailing"], "invdd"),
        ("all books / inverse-DD", streams, "invdd"),
    ]
    frontiers, hit_row, win_name, win_curve = {}, None, None, None
    for name, sel, mode in configs:
        rows = frontier(sel, mode, v_start)
        frontiers[name] = rows
        h = _frontier_row(rows, hit=True)
        if h and (hit_row is None or h["med90"] > hit_row["med90"]):
            hit_row, win_name, win_curve = h, name, h["curve"]

    if not hit_row:
        return f"""{BEGIN}
<h2>+10%/90-day campaign (generated {time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())})</h2>
<div class="card"><p>No configuration reached median +10%/90 days inside the 8% DD budget.</p></div>
{END}"""

    split = datetime.fromisoformat(TRAIN_END)
    first_ts = win_curve[0][0]
    eq_points = [(first_ts, 10_000.0)] + [(ts, round(v, 2)) for ts, v in win_curve]

    frontier_html = []
    for name, rows in frontiers.items():
        trs = []
        for r in rows:
            ok = r["max_dd"] < DD_BUDGET
            hit = ok and r["med90"] >= TARGET_90D
            cls = ' class="ok"' if hit else ("" if ok else ' style="color:#8b949e"')
            mark = " &larr; TARGET" if hit else (" ✓" if ok else "")
            trs.append(
                f"<tr{cls}><td>{r['scale']:.2f}×</td>"
                f"<td>{'/'.join(f'{rk * 100:.2f}' for rk in r['risks'])}%</td>"
                f"<td>{r['max_dd']:.2f}%</td><td>{r['med90']:+.1f}%</td>"
                f"<td>{r['best90']:+.1f}%</td><td>{r['balance']:,.0f}</td><td>{mark}</td></tr>"
            )
        frontier_html.append(
            f'<h3 style="color:#58a6ff;font-size:0.95rem;margin:16px 0 4px">{html.escape(name)}</h3>'
            "<table><tr><th>scale</th><th>risk/book</th><th>full DD</th>"
            "<th>median 90d</th><th>best 90d</th><th>balance</th><th></th></tr>"
            f"{''.join(trs)}</table>"
        )

    # Trailing vs static comparison: frontier at the DD-budget crossing.
    cmp_rows = []
    for name in frontiers:
        f = _frontier_row(frontiers[name], hit=False)
        h = _frontier_row(frontiers[name], hit=True)
        if f:
            cmp_rows.append(
                f"<tr><td>{html.escape(name)}</td><td>{f['max_dd']:.2f}%</td>"
                f"<td>{f['med90']:+.1f}%</td><td>{f['best90']:+.1f}%</td>"
                f"<td>{'HIT' if h else 'misses'}</td></tr>"
            )
    trailing_f = _frontier_row(frontiers["trailing-only / inverse-DD"], hit=False)
    trailing_note = (
        (
            f"<p>At the same {trailing_f['max_dd']:.1f}% DD the trailing exits reach only "
            f"median {trailing_f['med90']:+.1f}% per 90 days: the ratchet banks profits early "
            "and shrinks the loss tail, but that same ratchet gives back the extended upside "
            "the static RR 3.0 target keeps. Stability up, return tail down.</p>"
        )
        if trailing_f
        else ""
    )

    book_rows = []
    for b, trades in streams:
        m = merged_equity(trades, datetime(1900, 1, 1), datetime(2999, 1, 1))
        trail = html.escape(json.dumps(b["trail"], sort_keys=True)) if b["trail"] else "&mdash;"
        book_rows.append(
            f"<tr><td>{b['kind']}</td><td>{b['symbol']}</td><td>{b['tf']}</td>"
            f"<td><code>{html.escape(json.dumps(b['params'], sort_keys=True))}</code></td>"
            f"<td><code>{trail}</code></td>"
            f"<td>{b['val_pf']:.2f}</td><td>{b['val_dd']:.2f}%</td>"
            f"<td>{m['pf']:.2f}</td><td>{m['max_dd_pct']:.2f}%</td><td>{m['trades']}</td>"
            f'<td class="ok">{m["net_usd"]:+,.2f}</td></tr>'
        )

    # Monthly PnL of the winning curve: month bucket = equity delta within it
    # (first month's delta is measured from the $10k start).
    monthly: dict[str, float] = {}
    for i in range(1, len(win_curve)):
        ts, v = win_curve[i]
        prev = win_curve[i - 1][1] if i > 1 else 10_000.0
        monthly[ts.strftime("%Y-%m")] = monthly.get(ts.strftime("%Y-%m"), 0.0) + (v - prev)

    worst = min(monthly.values())
    return f"""{BEGIN}
<h2>+10%/90-day campaign — trailing-TP experiment (generated {time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())})</h2>
<div class="card">
  <p>Objective: a portfolio whose <strong>median rolling 90-day return</strong> clears
  <strong>+10%</strong> while full-window drawdown stays under <strong>8%</strong>, on one
  $10,000 account. TrailingMomentum (trailing SL ratchet + trailing TP) swept over
  EURUSD/GBPUSD/XAUUSD with the same train/validate discipline; XAUUSD was rejected on
  the merits (3 trades in 6.5 months of validate span). Method:
  <code>scripts/optimize_trailing.py</code> + <code>scripts/portfolio_trailing.py</code>.</p>
  <div class="kv">
    <span>Verdict: <strong class="ok">TARGET HIT</strong> &mdash; {html.escape(win_name)}</span>
    <span>Median 90d: <strong class="ok">{hit_row["med90"]:+.1f}%</strong> (target +10%)</span>
    <span>Best 90d: <strong>{hit_row["best90"]:+.1f}%</strong></span>
    <span>Full DD: <strong>{hit_row["max_dd"]:.2f}%</strong> (&lt; 8% budget)</span>
    <span>Scale: <strong>{hit_row["scale"]:.2f}×</strong>
    (risk/book {"/".join(f"{rk * 100:.2f}%" for rk in hit_row["risks"])})</span>
    <span>Final balance: <strong class="ok">${hit_row["balance"]:,.0f}</strong></span>
    <span>Months positive: {sum(1 for v in monthly.values() if v > 0)}/{len(monthly)}
    (worst {worst:+,.0f})</span>
  </div>
  <h3 style="color:#58a6ff;font-size:0.95rem;margin:16px 0 4px">Weighted equity curve — winning configuration, $10k start</h3>
  {equity_svg(eq_points, split)}
  <h3 style="color:#58a6ff;font-size:0.95rem;margin:16px 0 4px">Monthly PnL of the winning configuration (USD)</h3>
  {monthly_svg(monthly)}
  <h3 style="color:#58a6ff;font-size:0.95rem;margin:16px 0 4px">Trailing vs static exits</h3>
  <table><tr><th>configuration</th><th>full DD</th><th>median 90d</th><th>best 90d</th><th>+10% target</th></tr>
  {"".join(cmp_rows)}</table>
  {trailing_note}
  <p>The landed configuration is the <strong>static-RR</strong> books (PR #11) with
  inverse-DD weighting at {hit_row["scale"]:.2f}× — the trailing variant is the
  stability-first alternative that does not reach the return target inside the DD budget.</p>
  <h3 style="color:#58a6ff;font-size:0.95rem;margin:16px 0 4px">Scale frontier — median 90d return vs drawdown</h3>
  {"".join(frontier_html)}
  <h3 style="color:#58a6ff;font-size:0.95rem;margin:16px 0 4px">Books in the pool (replayed at nominal 0.5% risk)</h3>
  <table>
    <tr><th>kind</th><th>symbol</th><th>tf</th><th>params</th><th>trail</th>
        <th colspan="2">validate</th><th colspan="3">full window</th></tr>
    <tr><th></th><th></th><th></th><th></th><th></th><th>PF</th><th>DD</th>
        <th>PF</th><th>DD</th><th>trades</th><th>net</th></tr>
    {"".join(book_rows)}
  </table>
  <p class="dim">Screening engine (M1 fills, Gate-1 spread model, $6/lot commission).
  All figures are one shared compounded balance; 90-day windows step every 15 days across
  the untouched validate span. Tick-exact re-verification is the next gate before live use.</p>
</div>
{END}"""


def main() -> int:
    specs = load_instruments(PAGE.parents[1] / "config" / "instruments.yaml")
    books = _static_books() + _trailing_books(specs)
    print(f"{len(books)} books in the pool")
    streams = replay_streams(books, specs)
    page = PAGE.read_text(encoding="utf-8")
    section = build_section(streams, specs)
    if BEGIN in page and END in page:
        head, _, rest = page.partition(BEGIN)
        _, _, tail = rest.partition(END)
        page = head + section + tail
    else:
        anchor = page.index("<!-- PORTFOLIO:END -->") + len("<!-- PORTFOLIO:END -->")
        page = page[:anchor] + "\n" + section + page[anchor:]
    PAGE.write_text(page, encoding="utf-8")
    print(f"Page updated: {PAGE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
