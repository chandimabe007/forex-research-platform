"""Regenerate the Strategy Lab page's accepted-portfolio section.

Replays the optimizer's accepted books at the landed portfolio risk (0.5%
per trade), builds the closed-trade equity curve, monthly PnL bars and
per-book stats, and splices them into ``data/strategy_lab.html`` between
``<!-- PORTFOLIO:BEGIN -->`` / ``<!-- PORTFOLIO:END -->`` markers (idempotent).

Everything is rendered as inline SVG + embedded JSON — the page stays a
single self-contained file with no fetches.

Usage: python scripts/update_lab_page.py
"""

from __future__ import annotations

import html
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import optimize_lab  # noqa: E402
from optimize_lab import GATE1, OUT, REPO, TF_MINUTES, TRAIN_END, load_m1  # noqa: E402
from portfolio_check import merged_equity  # noqa: E402

from forex_research.data.instruments import load_instruments  # noqa: E402
from forex_research.strategy_lab.engine import M1Lab, session_spreads  # noqa: E402
from forex_research.strategy_lab.features import compute_features  # noqa: E402
from forex_research.strategy_lab.strategies import STRATEGIES  # noqa: E402

PAGE = REPO / "data" / "strategy_lab.html"
RISK = 0.005  # the landed portfolio risk (largest sweep risk with full DD < 5%)

BEGIN, END = "<!-- PORTFOLIO:BEGIN -->", "<!-- PORTFOLIO:END -->"


def accepted_books() -> list[dict]:
    report = json.loads(OUT.read_text(encoding="utf-8"))
    books: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for sym, sr in report["symbols"].items():
        for tf, tfr in sr["timeframes"].items():
            for v in sorted(tfr["accepted"], key=lambda v: -v["profit_factor"]):
                book = (sym, tf, v["strategy"])
                if book in seen:
                    continue
                seen.add(book)
                books.append(
                    {
                        "symbol": sym,
                        "tf": tf,
                        "strategy": v["strategy"],
                        "params": v["params"],
                        "val_pf": v["profit_factor"],
                        "val_dd": v["max_dd_pct"],
                        "val_trades": v["trades"],
                    }
                )
    return books


def equity_svg(points: list[tuple[datetime, float]], split: datetime) -> str:
    """Closed-trade equity curve, dark-theme inline SVG."""
    w, h = 860, 250
    left, right, top, bot = 56, 14, 16, 30
    ys = [p[1] for p in points]
    lo, hi = min(ys + [10_000]), max(ys + [10_000])
    pad = max(150, (hi - lo) * 0.08)
    lo, hi = lo - pad, hi + pad
    n = len(points)

    def X(i: int) -> float:
        return left + (w - left - right) * (i / max(n - 1, 1))

    def Y(v: float) -> float:
        return top + (h - top - bot) * (1 - (v - lo) / (hi - lo))

    grid, step = [], 250.0
    g = (lo // step + 1) * step
    while g < hi:
        grid.append(g)
        g += step
    parts = [
        f'<svg viewBox="0 0 {w} {h}" role="img" style="width:100%;height:auto">'
        f'<rect x="{left}" y="{top}" width="{w - left - right}" height="{h - top - bot}" '
        f'fill="#0d1117" stroke="#30363d"/>'
    ]
    for g in grid:
        parts.append(
            f'<line x1="{left}" y1="{Y(g):.1f}" x2="{w - right}" y2="{Y(g):.1f}" '
            f'stroke="#21262d"/><text x="{left - 6}" y="{Y(g) + 4:.1f}" '
            f'fill="#8b949e" font-size="11" text-anchor="end">{g:,.0f}</text>'
        )
    split_i = next((i for i, (ts, _) in enumerate(points) if ts >= split), n)
    if 0 < split_i < n:
        parts.append(
            f'<line x1="{X(split_i):.1f}" y1="{top}" x2="{X(split_i):.1f}" '
            f'y2="{h - bot}" stroke="#d29922" stroke-dasharray="4 3"/>'
            f'<text x="{X(split_i) + 5:.1f}" y="{top + 12}" fill="#d29922" '
            f'font-size="11">validate span</text>'
        )
    poly = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, (_, v) in enumerate(points))
    parts.append(f'<polyline points="{poly}" fill="none" stroke="#3fb950" stroke-width="1.6"/>')
    for idx in (0, n // 3, 2 * n // 3, n - 1):
        ts = points[idx][0]
        parts.append(
            f'<text x="{X(idx):.1f}" y="{h - 8}" fill="#8b949e" font-size="11" '
            f'text-anchor="middle">{ts.strftime("%b %y")}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def monthly_svg(monthly: dict[str, float]) -> str:
    w, h = 860, 150
    left, right, top, bot = 10, 10, 12, 24
    vals = list(monthly.values())
    hi = max(abs(v) for v in vals) * 1.15
    zero = top + (h - top - bot) * (hi / (2 * hi))
    bw = (w - left - right) / len(vals)
    parts = [f'<svg viewBox="0 0 {w} {h}" role="img" style="width:100%;height:auto">']
    parts.append(
        f'<line x1="{left}" y1="{zero:.1f}" x2="{w - right}" y2="{zero:.1f}" stroke="#30363d"/>'
    )
    for i, (mth, v) in enumerate(monthly.items()):
        x = left + i * bw + 2
        bh = (h - top - bot) / 2 * (abs(v) / hi)
        if v >= 0:
            parts.append(
                f'<rect x="{x:.1f}" y="{zero - bh:.1f}" width="{bw - 4:.1f}" '
                f'height="{bh:.1f}" fill="#3fb950"/>'
            )
        else:
            parts.append(
                f'<rect x="{x:.1f}" y="{zero:.1f}" width="{bw - 4:.1f}" '
                f'height="{bh:.1f}" fill="#f85149"/>'
            )
        parts.append(
            f'<text x="{x + (bw - 4) / 2:.1f}" y="{h - 8}" fill="#8b949e" '
            f'font-size="9" text-anchor="middle">{mth[2:]}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def build_section(books: list[dict], trades: list, t0: datetime) -> str:
    split = datetime.fromisoformat(TRAIN_END)
    rows = []
    equity: list[tuple[datetime, float]] = [(t0, 10_000.0)]
    bal = 10_000.0
    for tr in sorted(trades, key=lambda t: t.exit_ts):
        bal += tr.pnl_usd
        equity.append((tr.exit_ts, round(bal, 2)))
    monthly: dict[str, float] = {}
    for tr in trades:
        monthly[tr.exit_ts.strftime("%Y-%m")] = (
            monthly.get(tr.exit_ts.strftime("%Y-%m"), 0) + tr.pnl_usd
        )
    full = merged_equity(trades, datetime(1900, 1, 1), datetime(2999, 1, 1))
    valid = merged_equity(trades, split, datetime(2999, 1, 1))
    for b in books:
        m = merged_equity(
            [t for t in trades if t.symbol == b["symbol"]],
            datetime(1900, 1, 1),
            datetime(2999, 1, 1),
        )
        b["full"] = m
        rows.append(
            f"<tr><td>{b['symbol']}</td><td>{b['tf']}</td>"
            f"<td><code>{html.escape(json.dumps(b['params'], sort_keys=True))}</code></td>"
            f"<td>{b['val_pf']:.2f}</td><td>{b['val_dd']:.2f}%</td>"
            f"<td>{b['full']['pf']:.2f}</td><td>{b['full']['max_dd_pct']:.2f}%</td>"
            f"<td>{b['full']['trades']}</td>"
            f'<td class="ok">{b["full"]["net_usd"]:+,.2f}</td></tr>'
        )
    worst = min(monthly.values())
    return f"""{BEGIN}
<h2>Accepted portfolio — live record (generated {time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())})</h2>
<div class="card">
  <p>The two books that passed the year-long out-of-sample gates
  (PF &ge; 2 and DD &lt; 5% on the untouched validate span), combined into one
  $10,000 account at <strong>0.5% risk per trade</strong>. Full method:
  <code>scripts/optimize_lab.py</code> + <code>scripts/portfolio_check.py</code>.</p>
  <div class="kv">
    <span>Balance: <strong class="ok">${bal:,.2f}</strong> ({full["net_usd"]:+,.2f})</span>
    <span>Validate PF: <strong>{valid["pf"]:.2f}</strong></span>
    <span>Validate DD: <strong>{valid["max_dd_pct"]:.2f}%</strong></span>
    <span>Full DD: <strong>{full["max_dd_pct"]:.2f}%</strong> (&lt; 5% budget)</span>
    <span>Trades: {full["trades"]}</span>
    <span>Months positive: {sum(1 for v in monthly.values() if v > 0)}/{len(monthly)}
    (worst {worst:+,.0f})</span>
  </div>
  <h3 style="color:#58a6ff;font-size:0.95rem;margin:16px 0 4px">Equity curve — closed trades, $10k start</h3>
  {equity_svg(equity, split)}
  <h3 style="color:#58a6ff;font-size:0.95rem;margin:16px 0 4px">Monthly PnL (USD)</h3>
  {monthly_svg(monthly)}
  <table>
    <tr><th>book</th><th>tf</th><th>params</th><th colspan="2">validate</th>
        <th colspan="3">full window</th></tr>
    <tr><th></th><th></th><th></th><th>PF</th><th>DD</th><th>PF</th><th>DD</th>
        <th>trades</th><th>net</th></tr>
    {"".join(rows)}
  </table>
  <p class="dim">Screening engine (M1 fills, Gate-1 spread model, $6/lot commission).
  Tick-exact re-verification is the next gate before any live use.</p>
</div>
{END}"""


def main() -> int:
    books = accepted_books()
    if not books:
        print("No accepted configs — nothing to publish.")
        return 1
    specs = load_instruments(REPO / "config" / "instruments.yaml")
    trades = []
    first_ts = None
    for b in books:
        spec = specs[b["symbol"]]
        lab = M1Lab(spec=spec, spreads=session_spreads(GATE1, b["symbol"]), risk_pct=RISK)
        strat = STRATEGIES[b["strategy"]]()
        strat.params = dict(b["params"])
        m1 = load_m1(b["symbol"])
        minutes = TF_MINUTES[b["tf"]]
        bars = m1 if minutes == 1 else optimize_lab.aggregate_bars(m1, minutes)
        res = lab.run(b["symbol"], bars, strat, compute_features(bars), index_offset=0)
        trades.extend(res.trades)
        first_ts = (
            min(x for x in [first_ts, res.trades[0].entry_ts] if x) if res.trades else first_ts
        )
        print(f"  {b['symbol']} {b['tf']}: {len(res.trades)} trades")
    if not trades:
        print("No trades produced.")
        return 1
    page = PAGE.read_text(encoding="utf-8")
    section = build_section(books, trades, first_ts)
    if BEGIN in page and END in page:
        head, _, rest = page.partition(BEGIN)
        _, _, tail = rest.partition(END)
        page = head + section + tail
    else:
        anchor = page.index("<h2>")
        page = page[:anchor] + section + "\n" + page[anchor:]
    PAGE.write_text(page, encoding="utf-8")
    print(f"Page updated: {PAGE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
