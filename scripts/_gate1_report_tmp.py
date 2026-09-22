"""Probe: human-readable Gate 1 report from the combined record."""

import json
import sys
from collections import defaultdict

record = json.load(open(sys.argv[1], encoding="utf-8"))
cells = record["cells"]
thr = record["threshold"]

print(f"window: {record['capture_window_utc'][0]} .. {record['capture_window_utc'][1]} UTC")
print(f"threshold: c < {thr}  (GATE-022)\n")

# --- per symbol x session: round-trip cost components (median across weekday cells) ---
print("== Round-trip cost by pair x session (pips; median over weekday cells:")
print("   entry spread q90 + stop-exit spread q90 + commission)")
by = defaultdict(list)
for c in cells:
    if "@" not in c["timeframe"] or not c["timeframe"].endswith("1.0x"):
        continue
    by[(c["symbol"], c["session"])].append(c)
print(f"{'pair':8}{'session':10}{'entry':>7}{'exit':>7}{'comm':>7}{'total':>8}  episodes")
for (sym, ses), group in sorted(by.items()):
    n = len(group)
    entry = sum(c["entry_spread_pips"] for c in group) / n
    exit_ = sum(c["stop_exit_spread_pips"] for c in group) / n
    comm = sum(c["commission_pips"] for c in group) / n
    total = sum(c["round_trip_pips"] for c in group) / n
    eps = min(c["entry_episodes"] for c in group)
    print(f"{sym:8}{ses:10}{entry:7.2f}{exit_:7.2f}{comm:7.2f}{total:8.2f}  {eps:>6}")

# --- stop distances ---
print("\n== Median true range (pips) per pair x timeframe -> 1.0x stop distance")
stops = {}
for c in cells:
    tf, mult = c["timeframe"].split("@")
    stops.setdefault((c["symbol"], tf, mult), c["stop_pips"])
seen_tf = []
for (sym, tf, mult), s in sorted(stops.items()):
    if mult == "1.0x":
        seen_tf.append((sym, tf, s))
for sym, tf, s in seen_tf:
    print(f"  {sym:8}{tf:>4}: TR={s:8.2f} pips")

# --- the c matrix: per pair x tf, the tightest multiplier that passes ---
print("\n== Tightest passing stop multiplier per pair x timeframe (min over sessions")
print(f"   excluding rollover; x = no multiplier reaches c < {thr:.2f})")
mults = ["0.5x", "1.0x", "2.0x", "3.0x"]
cmap = {}
sess_by = defaultdict(set)
for c in cells:
    tf, mult = c["timeframe"].split("@")
    cmap[(c["symbol"], tf, mult, c["session"])] = c["c"]
    sess_by[(c["symbol"], tf)].add(c["session"])
for sym in sorted({k[0] for k in cmap}):
    for tf in ("15m", "1h"):
        row = []
        for mult in mults:
            vals = [cmap[(sym, tf, mult, s)] for s in sess_by[(sym, tf)] if s != "rollover"]
            best = min(vals)
            worst = max(vals)
            row.append(f"{mult}:{best:.2f}{'*' if worst >= thr else ''}")
        passing = [
            m
            for m in mults
            if max(cmap[(sym, tf, m, s)] for s in sess_by[(sym, tf)] if s != "rollover") < thr
        ]
        tightest = passing[0] if passing else "x"
        print(f"  {sym:8}{tf:>4}  tightest-all-sessions={tightest:>5}   " + "  ".join(row))
print("   (* worst session still fails at this multiplier; value shown is best session)")

# --- rollover honesty ---
print("\n== Rollover cells (the session the table warns you about)")
for c in cells:
    tf, mult = c["timeframe"].split("@")
    if c["session"] == "rollover" and mult == "1.0x":
        print(f"  {c['symbol']:8}{tf:>4}  c={c['c']:6.2f}  rt={c['round_trip_pips']:5.2f} pips")

levels = {c["cost_level"] for c in cells}
print(f"\ncost_level values across 160 cells: {sorted(levels)}")
gb = sum(1 for c in cells if c["cost_level"] == "global_bound")
print(f"global-bound fallback cells: {gb} (decisions must hold at their upper bound)")
