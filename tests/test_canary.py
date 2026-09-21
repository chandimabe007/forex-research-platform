"""Execution canary — MILE-023: gate ordering, outcome persistence, safety.

Every test drives ``run_canary`` against a fake adapter and asserts on the
real returned record: refusals happen before any order action, failures leave
no ambiguity about open positions, and the raw login never enters the record.
"""

import datetime as dt
import json
from dataclasses import replace
from types import SimpleNamespace

from forex_research.config import AccountAllowlistEntry, Allowlist
from forex_research.execution.adapter import (
    AccountState,
    FillingMode,
    MarginMode,
    OrderResult,
    SessionStatus,
    SymbolInfo,
)
from forex_research.execution.allowlist import login_hash
from forex_research.execution.canary import (
    CANARY_SCHEMA_VERSION,
    CanaryRecord,
    persist_canary_record,
    run_canary,
)

UTC = dt.UTC


class CanaryAdapter:
    """Fake adapter recording call order, with injectable failure modes."""

    def __init__(
        self,
        *,
        session: SessionStatus = SessionStatus.TRADES_ALLOWED,
        submit_ok: bool = True,
        protection: bool = True,
        close_ok: bool = True,
        modelled_profit: float = 0.50,
    ) -> None:
        self.calls: list[str] = []
        self.session = session
        self.submit_ok = submit_ok
        self.protection = protection
        self.close_ok = close_ok
        self.modelled_profit = modelled_profit
        self.balance = 10000.0
        self.positions: list[dict] = []
        self._ticket = 777000
        self.account = AccountState(
            login=42,
            server="Demo-Server",
            currency="USD",
            account_type="demo",
            margin_mode=MarginMode.HEDGING,
            leverage=100,
            balance=self.balance,
            equity=self.balance,
            margin_used=0.0,
            margin_free=self.balance,
        )

    def connect(self):
        self.calls.append("connect")
        return self.account

    def symbol_session_status(self, symbol):
        self.calls.append("session_status")
        return self.session

    def symbol_info(self, symbol):
        self.calls.append("symbol_info")
        return SymbolInfo(
            name=symbol,
            point=0.00001,
            digits=5,
            contract_size=100000.0,
            tick_size=0.00001,
            tick_value=1.0,
            volume_min=0.01,
            volume_step=0.01,
            volume_max=100.0,
            stops_level_points=10,
            freeze_level_points=5,
            filling_mode=FillingMode.IOC,
            spread_points=10,
        )

    def server_time(self):
        return dt.datetime(2024, 1, 2, 10, 0, tzinfo=UTC)

    def get_positions(self):
        return list(self.positions)

    def get_account(self):
        return replace(self.account, balance=self.balance)

    def subscribe_quotes(self, symbols):
        self.calls.append("subscribe_quotes")

        def fn(symbol):
            return SimpleNamespace(bid=1.10000, ask=1.10005, time=1704193200.0)

        return fn

    def submit(self, intent):
        self.calls.append("submit")
        if not self.submit_ok:
            return OrderResult(ok=False, retcode=10004, comment="requote")
        self._ticket += 1
        self.positions.append(
            {
                "ticket": self._ticket,
                "symbol": intent.symbol,
                "sl": intent.stop_loss if self.protection else None,
                "tp": intent.take_profit if self.protection else None,
            }
        )
        self._last_intent = intent
        return OrderResult(
            ok=True,
            retcode=10009,
            comment="done",
            order_ticket=self._ticket,
            position_ticket=self._ticket,
            price=1.10005,
            fill_volume=intent.volume,
        )

    def find_by_correlation(self, correlation_id):
        self.calls.append("find_by_correlation")
        return {"comment": correlation_id}

    def close_position(self, position_id, volume=None):
        self.calls.append("close_position")
        if not self.close_ok:
            return OrderResult(ok=False, retcode=10018, comment="market closed")
        self.positions = [p for p in self.positions if p["ticket"] != position_id]
        self.balance += 0.75  # realized delta on the flat round trip
        return OrderResult(ok=True, retcode=10009, comment="done", price=1.10000)

    def calc_profit(self, intent, close_price):
        return self.modelled_profit


def _allowlist() -> Allowlist:
    return Allowlist(
        entries=(
            AccountAllowlistEntry(
                login_hash=login_hash(42),
                server="Demo-Server",
                account_type="demo",
                currency="USD",
                phase="demo",
            ),
        )
    )


def _run(adapter, tmp_path, allowlist=None):
    outcome, path = run_canary(
        adapter,
        symbol="EURUSD",
        allowlist=allowlist if allowlist is not None else _allowlist(),
        out_dir=tmp_path,
        now=lambda: dt.datetime(2024, 1, 2, 10, 0, tzinfo=UTC),
    )
    return outcome, json.loads(path.read_text(encoding="utf-8"))


def test_happy_path_ok_and_reconciled(tmp_path):
    adapter = CanaryAdapter()
    outcome, record = _run(adapter, tmp_path)
    assert outcome == "ok"
    assert record["outcome"] == "ok"
    names = [s["name"] for s in record["steps"]]
    # Gate order: allowlist and session status BEFORE any order action.
    assert names.index("allowlist") < names.index("submit")
    assert names.index("session_status") < names.index("submit")
    assert {"submit", "close", "reconcile"} <= set(names)
    assert all(s["ok"] for s in record["steps"])
    assert record["discrepancies"] == []
    # The round trip is flat: no positions left behind.
    assert adapter.positions == []
    # Reconciliation observed a balance delta and separated fees from profit.
    rec = next(s for s in record["steps"] if s["name"] == "reconcile")
    assert rec["detail"]["realized_delta"] == 0.75
    assert rec["detail"]["modelled_profit"] == 0.50
    # Broker-side protection was confirmed on the live position.
    prot = next(s for s in record["steps"] if s["name"] == "broker_side_protection")
    assert prot["ok"] and prot["detail"]["sl_on_broker"] is not None


def test_adapter_recovers_position_id_from_correlation_deal():
    """Server quirk observed live (LHFXSA): retcode DONE with empty order and
    position ids. MT5Adapter.submit must recover the position id from the
    deal history via the correlation comment — and a failed lookup must
    never mask the fact that the order filled."""
    import forex_research.execution.mt5_adapter as mod

    captured = {}

    class FakeDeal:
        """Mimics an MT5 deal object (namedtuple with _asdict)."""

        comment = "CANARY-test1234"
        position_id = 11669257
        order = 11669257
        ticket = 10821387

        def _asdict(self):
            return {
                "comment": self.comment,
                "position_id": self.position_id,
                "order": self.order,
                "ticket": self.ticket,
            }

    order_result = SimpleNamespace(
        retcode=10009,
        comment="Request executed",
        order=0,
        position=0,
        price=1.14824,
        volume=0.01,
    )

    class FakeMT5:
        def __getattr__(self, name):
            raise AttributeError(name)

        def order_send(self, request):
            captured["request"] = request
            return order_result

        def orders_get(self):
            return ()

        def history_orders_get(self, *a):
            return ()

        def history_deals_get(self, *a):
            return (FakeDeal(),)

    fake = FakeMT5()
    adapter = mod.MT5Adapter.__new__(mod.MT5Adapter)  # skip __init__/connect
    adapter._mt5 = fake
    from forex_research.execution.adapter import AccountState, MarginMode

    adapter._account = AccountState(
        login=900001,
        server="LHFXSA-Trade",
        currency="USD",
        account_type="demo",
        margin_mode=MarginMode.HEDGING,
        leverage=100,
        balance=10000.0,
        equity=10000.0,
        margin_used=0.0,
        margin_free=10000.0,
    )
    monkey_info = SimpleNamespace(
        filling_mode=2,
        trade_exemode=2,
        trade_stops_level=0,
        trade_freeze_level=0,
        volume_min=0.01,
        volume_step=0.01,
        volume_max=1000.0,
        point=0.00001,
        digits=5,
        trade_contract_size=100000.0,
        trade_tick_size=0.00001,
        trade_tick_value=1.0,
        spread=6,
        visible=True,
        trade_mode=4,
    )
    fake.symbol_info = lambda s: monkey_info
    fake.symbol_select = lambda s, flag: True
    # probe_capabilities (pulled in via symbol_info -> _submit_filling) needs
    # the terminal and account snapshots too.
    fake.terminal_info = lambda: SimpleNamespace(
        name="MT5", build=6204, trade_allowed=True, connected=True
    )
    fake.account_info = lambda: SimpleNamespace(
        login=900001,
        server="LHFXSA-Trade",
        currency="USD",
        trade_mode=0,
        margin_mode=2,
        leverage=100,
        balance=10000.0,
        equity=10000.0,
        margin=0.0,
        margin_free=10000.0,
    )
    intent = mod.OrderIntent(
        symbol="EURUSD",
        side="buy",
        order_type="market",
        volume=0.01,
        limit_or_stop_price=None,
        stop_loss=1.14800,
        take_profit=1.14900,
        comment="CANARY-test1234",
    )
    result = adapter.submit(intent)
    assert result.ok and result.retcode == 10009
    assert result.position_ticket == 11669257, "recovered from deal position_id"
    assert captured["request"]["comment"] == "CANARY-test1234"

    # Lookup failure must not mask the fill: position_ticket stays None, ok stays True.
    fake.history_deals_get = lambda *a: ()
    result2 = adapter.submit(intent)
    assert result2.ok and result2.position_ticket is None


def test_record_is_redacted(tmp_path):
    adapter = CanaryAdapter()
    _, record = _run(adapter, tmp_path)
    text = json.dumps(record)
    # The raw login must never appear (SEC-001); only its sha256 digest.
    assert str(adapter.account.login) not in text.split("account_login_sha256")[0]
    sha = record["account_login_sha256"]
    assert len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)


def test_allowlist_refusal_places_no_orders(tmp_path):
    adapter = CanaryAdapter()
    outcome, record = _run(adapter, tmp_path, allowlist=Allowlist(entries=()))
    assert outcome == "refused"
    assert "submit" not in adapter.calls
    assert record["outcome"] == "refused"
    refused = [s for s in record["steps"] if s["name"] == "allowlist"]
    assert refused and refused[0]["ok"] is False


def test_closed_venue_is_refused_not_failed(tmp_path):
    adapter = CanaryAdapter(session=SessionStatus.CLOSED)
    outcome, record = _run(adapter, tmp_path)
    assert outcome == "refused"
    assert "submit" not in adapter.calls and "symbol_info" not in adapter.calls


def test_submit_failure_leaves_no_order(tmp_path):
    adapter = CanaryAdapter(submit_ok=False)
    outcome, record = _run(adapter, tmp_path)
    assert outcome == "failed"
    assert adapter.positions == []  # nothing opened
    assert "close_position" not in adapter.calls
    failed = [s for s in record["steps"] if s["name"] == "submit"][0]
    assert failed["ok"] is False and failed["detail"]["retcode"] == 10004


def test_close_failure_is_recorded_with_ticket(tmp_path):
    adapter = CanaryAdapter(close_ok=False)
    outcome, record = _run(adapter, tmp_path)
    assert outcome == "failed"
    assert len(adapter.positions) == 1  # the open position is flagged, not hidden
    assert any("resolve manually" in d for d in record["discrepancies"])
    assert any(str(adapter.positions[0]["ticket"]) in d for d in record["discrepancies"])


def test_missing_broker_protection_is_a_discrepancy(tmp_path):
    adapter = CanaryAdapter(protection=False)
    outcome, record = _run(adapter, tmp_path)
    assert outcome == "ok_with_discrepancies"
    assert any("EXEC-040" in d for d in record["discrepancies"])


def test_reconciliation_mismatch_is_a_discrepancy(tmp_path):
    adapter = CanaryAdapter(modelled_profit=-50.0)
    outcome, record = _run(adapter, tmp_path)
    assert outcome == "ok_with_discrepancies"
    assert any("reconciliation mismatch" in d for d in record["discrepancies"])


def test_persist_canary_records_never_collide(tmp_path):
    # Same-quantum writes must not overwrite each other: the Windows clock
    # advances in coarse quanta, so identical microsecond stamps are
    # realistic (a py3.11 CI run caught the probe writer colliding exactly
    # this way).
    def record(outcome):
        return CanaryRecord(
            schema_version=CANARY_SCHEMA_VERSION,
            started_at="2024-01-02T10:00:00+00:00",
            finished_at="2024-01-02T10:00:05+00:00",
            outcome=outcome,
            symbol="EURUSD",
            account_login_sha256=None,
            steps=[],
            discrepancies=[],
            observations={},
        )

    ok_path = persist_canary_record(record("ok"), out_dir=tmp_path)
    fail_path = persist_canary_record(record("failed"), out_dir=tmp_path)
    assert ok_path != fail_path
    assert json.loads(ok_path.read_text(encoding="utf-8"))["outcome"] == "ok"
    assert json.loads(fail_path.read_text(encoding="utf-8"))["outcome"] == "failed"
    assert len(list(tmp_path.glob("canary_*.json"))) == 2
