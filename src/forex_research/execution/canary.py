"""Execution canary — MILE-023, ARCH-002 verification, EXEC-061, CHAL-012.

A tiny controlled sequence of demo orders: one market entry with broker-side
SL/TP, a reconciliation of the balance delta against the modelled profit,
then a close. This measures the **venue**, never a strategy (MILE-023).

Safety order of operations, mirroring the probe (EXEC-060):

    connect -> allowlist verify -> session status -> symbol re-query
    -> (only now) submit -> observe -> close

Every outcome — ok, refused, failed — persists a redacted JSON record to
``data/canary/``: the raw login never enters the record (SEC-001). A refused
or failed run never leaves an order behind: the submit step runs only after
every gate passes, and a failed close is recorded as an explicit unresolved
step for the operator, not silently retried.

Observed ARCH-002 positions (recorded, not settled — DST stays open,
GATE-005): decision time, server time before/after the submission, wall
latency, fill price against the decision quote. Swap timing is recorded as
not-observed: the canary holds for seconds, so rollover must be read from
the deal history instead (recorded as a follow-up on the report).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..config import load_allowlist
from .adapter import OrderIntent, SessionStatus
from .allowlist import login_hash, verify_account

CANARY_SCHEMA_VERSION = 1


@dataclass
class Step:
    name: str
    ok: bool
    detail: dict = field(default_factory=dict)


@dataclass
class CanaryRecord:
    schema_version: int
    started_at: str
    finished_at: str
    outcome: str  # ok | refused | failed
    symbol: str
    account_login_sha256: str | None
    steps: list[Step]
    discrepancies: list[str]
    observations: dict


def persist_canary_record(record: CanaryRecord, *, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": record.schema_version,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "outcome": record.outcome,
        "symbol": record.symbol,
        "account_login_sha256": record.account_login_sha256,
        "steps": [{"name": s.name, "ok": s.ok, "detail": s.detail} for s in record.steps],
        "discrepancies": record.discrepancies,
        "observations": record.observations,
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = out_dir / f"canary_{stamp}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def run_canary(
    adapter,
    *,
    symbol: str,
    allowlist,
    verifier=verify_account,
    out_dir: Path,
    now=None,
) -> tuple[str, Path]:
    """Run the canary sequence. Returns (outcome, record_path)."""
    now_fn = now or (lambda: datetime.now(UTC))
    started = now_fn().isoformat()
    steps: list[Step] = []
    discrepancies: list[str] = []
    login: int | None = None

    def finish(outcome: str, observations: dict) -> tuple[str, Path]:
        record = CanaryRecord(
            schema_version=CANARY_SCHEMA_VERSION,
            started_at=started,
            finished_at=now_fn().isoformat(),
            outcome=outcome,
            symbol=symbol,
            account_login_sha256=login_hash(login) if login else None,
            steps=steps,
            discrepancies=discrepancies,
            observations=observations,
        )
        return outcome, persist_canary_record(record, out_dir=out_dir)

    # -- connect (fails closed: nothing runs before the account is known) ---
    try:
        account = adapter.connect()
        login = account.login
    except Exception as exc:  # noqa: BLE001 - every outcome is recorded
        steps.append(Step("connect", False, {"error": str(exc)}))
        return finish("failed", {})

    steps.append(
        Step(
            "connect",
            True,
            {
                "server": account.server,
                "currency": account.currency,
                "account_type": account.account_type,
                "margin_mode": account.margin_mode.value,
            },
        )
    )

    # -- allowlist BEFORE any order action (EXEC-060) ------------------------
    try:
        verifier(
            allowlist,
            login=account.login,
            server=account.server,
            currency=account.currency,
            account_type=account.account_type,
            phase="demo",
        )
        steps.append(Step("allowlist", True, {}))
    except Exception as exc:  # noqa: BLE001
        steps.append(Step("allowlist", False, {"error": str(exc)}))
        return finish("refused", {})

    # -- session status: a closed venue is a refusal, not an error ----------
    try:
        status = adapter.symbol_session_status(symbol)
        steps.append(
            Step("session_status", status is SessionStatus.TRADES_ALLOWED, {"status": status.value})
        )
        if status is not SessionStatus.TRADES_ALLOWED:
            return finish("refused", {})
    except Exception as exc:  # noqa: BLE001
        steps.append(Step("session_status", False, {"error": str(exc)}))
        return finish("failed", {})

    # -- symbol properties, re-queried (EXEC-033) ----------------------------
    try:
        info = adapter.symbol_info(symbol)
    except Exception as exc:  # noqa: BLE001
        steps.append(Step("symbol_info", False, {"error": str(exc)}))
        return finish("failed", {})
    volume = info.volume_min
    # Stop distance respects the venue's stops_level (GATE-011) with margin.
    stop_points = max(info.stops_level_points + 2, 20)
    step_points = max(info.point, 1e-12)
    stop_distance = stop_points * step_points
    steps.append(
        Step(
            "symbol_info",
            True,
            {
                "volume_min": info.volume_min,
                "stops_level_points": info.stops_level_points,
                "stop_distance_points": stop_points,
                "spread_points": info.spread_points,
                "filling_mode": info.filling_mode.value,
            },
        )
    )

    server_before = adapter.server_time()
    decision_ts = now_fn()
    slippage_ref = {"decision_bid": None, "decision_ask": None}
    try:
        positions_before = adapter.get_positions()
        balance_before = adapter.get_account().balance
    except Exception as exc:  # noqa: BLE001
        steps.append(Step("pre_trade_state", False, {"error": str(exc)}))
        return finish("failed", {})
    steps.append(
        Step(
            "pre_trade_state",
            True,
            {
                "positions_open": len(positions_before),
                "balance_before": balance_before,
                "server_time_before": server_before.isoformat(),
                "decision_ts": decision_ts.isoformat(),
            },
        )
    )

    # -- submit: the first and only order action ------------------------------
    correlation_id = f"CANARY-{uuid.uuid4().hex[:8]}"
    submit_wall_ms: float | None = None
    try:
        quote = adapter.subscribe_quotes([symbol])(symbol)
        slippage_ref["decision_bid"] = quote.bid
        slippage_ref["decision_ask"] = quote.ask
        # Broker-side protection from the start (EXEC-040): SL/TP ride on the
        # entry order itself — the intent is immutable, so the quote is read
        # before the order is built, never patched afterwards.
        intent = OrderIntent(
            symbol=symbol,
            side="buy",
            order_type="market",
            volume=volume,
            limit_or_stop_price=None,
            stop_loss=quote.bid - stop_distance,
            take_profit=quote.bid + 5 * stop_distance,
            comment=correlation_id,
        )
        t0 = time.perf_counter()
        result = adapter.submit(intent)
        submit_wall_ms = (time.perf_counter() - t0) * 1000.0
    except Exception as exc:  # noqa: BLE001
        steps.append(Step("submit", False, {"error": str(exc)}))
        return finish("failed", {})

    if not result.ok:
        steps.append(
            Step(
                "submit",
                False,
                {
                    "retcode": result.retcode,
                    "comment": result.comment,
                },
            )
        )
        return finish("failed", {})
    if result.position_ticket is None:
        # The venue accepted the order but no position id is resolvable —
        # the canary must not pretend it can close what it cannot name.
        # Record it as an unresolved live order for the operator.
        steps.append(
            Step(
                "submit",
                False,
                {
                    "retcode": result.retcode,
                    "comment": result.comment,
                    "error": "order accepted but position id unresolvable — check the "
                    "terminal manually and close any stray position",
                    "correlation_id": correlation_id,
                },
            )
        )
        discrepancies.append(
            f"UNRESOLVED ORDER: accepted (retcode {result.retcode}) with no "
            f"position id; correlation '{correlation_id}' — close manually"
        )
        return finish("failed", {"unresolved_order": correlation_id})
    fill_price = result.price
    entry_slippage = (fill_price - slippage_ref["decision_ask"]) if fill_price is not None else None
    steps.append(
        Step(
            "submit",
            True,
            {
                "position_ticket": result.position_ticket,
                "fill_price": fill_price,
                "wall_latency_ms": round(submit_wall_ms, 3),
                "entry_slippage_vs_decision_ask": entry_slippage,
                "server_time_after": adapter.server_time().isoformat(),
            },
        )
    )

    # -- correlation cross-check (EXEC-031 seam exercised) --------------------
    found = adapter.find_by_correlation(correlation_id)
    steps.append(
        Step(
            "correlation_search",
            found is not None,
            {
                "found": found is not None,
                "matched_kind": ("order_or_deal" if found is not None else None),
            },
        )
    )

    # -- broker-side protection confirmed on the live position (EXEC-040) ----
    live = next(
        (p for p in adapter.get_positions() if p.get("ticket") == result.position_ticket),
        None,
    )
    protection_ok = live is not None and live.get("sl") is not None and live.get("tp") is not None
    steps.append(
        Step(
            "broker_side_protection",
            protection_ok,
            {
                "sl_on_broker": live.get("sl") if live else None,
                "tp_on_broker": live.get("tp") if live else None,
            },
        )
    )
    if not protection_ok:
        discrepancies.append(
            "position visible without broker-side SL/TP — EXEC-040 violated; "
            "close it manually and investigate before any further milestone"
        )

    # -- close and reconcile the balance delta -------------------------------
    try:
        close = adapter.close_position(result.position_ticket)
    except Exception as exc:  # noqa: BLE001
        steps.append(Step("close", False, {"error": str(exc)}))
        discrepancies.append(
            f"close failed with the position open (ticket {result.position_ticket}) — "
            "resolve manually; the canary must never leave orders behind"
        )
        return finish("failed", {"swap_timing_observed": False})
    if not close.ok:
        steps.append(
            Step(
                "close",
                False,
                {
                    "retcode": close.retcode,
                    "comment": close.comment,
                },
            )
        )
        discrepancies.append(
            f"close rejected (retcode {close.retcode}); ticket "
            f"{result.position_ticket} may still be open — resolve manually"
        )
        return finish("failed", {"swap_timing_observed": False})
    close_price = close.price
    steps.append(Step("close", True, {"close_price": close_price}))

    # Balance settlement can lag the close (observed live: the PnL booked
    # seconds after the deal). Poll briefly; if it never moves, record that
    # honestly instead of reconciling against a stale balance.
    balance_after = adapter.get_account().balance
    settled_at_read = balance_after != balance_before
    if not settled_at_read:
        for _ in range(4):
            time.sleep(0.4)
            balance_after = adapter.get_account().balance
            if balance_after != balance_before:
                settled_at_read = True
                break
    realized = balance_after - balance_before
    modelled = adapter.calc_profit(intent, close_price) if close_price else 0.0
    # Residual reconciliation: query the deals for this correlation id so the
    # observed fees include the venue's own commission lines, not just the
    # price delta (EXEC-061 evidence shape).
    fees_observed = None
    try:
        deal = adapter.find_by_correlation(correlation_id)
        if isinstance(deal, dict) and deal.get("profit") is not None:
            # The closing deal carries the round trip's price profit; fees are
            # booked as separate deal fields on netting servers (fee/commission).
            fees_observed = float(deal.get("fee") or 0.0) + float(deal.get("commission") or 0.0)
    except Exception:  # noqa: BLE001 - evidence-only fallback
        pass
    fees_and_swap = realized - modelled - (fees_observed or 0.0)
    steps.append(
        Step(
            "reconcile",
            True,
            {
                "balance_before": balance_before,
                "balance_after": balance_after,
                "realized_delta": realized,
                "balance_settled_at_read": settled_at_read,
                "modelled_profit": modelled,
                "fees_observed_on_deals": fees_observed,
                "fees_plus_swap_observed": fees_and_swap,
            },
        )
    )
    if not settled_at_read:
        discrepancies.append(
            "balance unchanged after close within the settle window — "
            "reconciliation ran against an unsettled balance; verify from deal history"
        )
    # The balance delta must equal modelled profit plus fees/swap within the
    # venue's rounding; anything else is a reconciliation mismatch.
    if abs(fees_and_swap) > max(1.0, abs(modelled) * 0.1):
        discrepancies.append(
            f"reconciliation mismatch: realized {realized} vs modelled {modelled} "
            f"(implied fees+swap {fees_and_swap})"
        )

    observations = {
        "swap_timing_observed": False,
        "swap_timing_note": "the canary holds for seconds; read rollover from "
        "deal history at the reset-boundary canary run (CHAL-012)",
        "reset_boundary_settled": False,
        "entry_slippage_vs_decision_ask": entry_slippage,
        "wall_latency_ms": round(submit_wall_ms or 0.0, 3),
    }
    outcome = "ok" if not discrepancies else "ok_with_discrepancies"
    return finish(outcome, observations)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="MILE-023 execution canary (demo orders; allowlist-gated)"
    )
    parser.add_argument("--symbol", default="EURUSD")
    parser.add_argument("--out", type=Path, default=Path("data/canary"))
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config"),
        help="directory containing accounts.yaml",
    )
    args = parser.parse_args(argv)

    allowlist = load_allowlist(args.config / "accounts.yaml")
    from .mt5_adapter import MT5Adapter

    outcome, path = run_canary(
        MT5Adapter(), symbol=args.symbol, allowlist=allowlist, out_dir=args.out
    )
    print(f"canary outcome: {outcome}")
    print(f"record: {path}")
    return 0 if outcome in {"ok", "ok_with_discrepancies"} else 1


if __name__ == "__main__":
    sys.exit(main())
