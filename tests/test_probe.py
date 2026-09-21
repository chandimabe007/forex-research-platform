"""Capability probe — EXEC-020, GATE-010/011, EXEC-060 ordering, SEC-001 redaction."""

import json

from forex_research.config import AccountAllowlistEntry, Allowlist
from forex_research.execution.adapter import (
    AccountState,
    BrokerCapabilities,
    FillingMode,
    MarginMode,
    SymbolInfo,
)
from forex_research.execution.allowlist import login_hash
from forex_research.execution.probe import compare_against_expected, persist_probe_record, run_probe


class FakeAdapter:
    """Records the call order; connect must precede any capability query."""

    def __init__(self, *, allowed: bool = True, trade_expert: bool = True) -> None:
        self.calls: list[str] = []
        self.allowed = allowed
        self.trade_expert = trade_expert
        self.account = AccountState(
            login=42,
            server="Demo-Server",
            currency="USD",
            account_type="demo",
            margin_mode=MarginMode.HEDGING,
            leverage=100,
            balance=10000.0,
            equity=10000.0,
            margin_used=0.0,
            margin_free=10000.0,
        )

    def connect(self) -> AccountState:
        self.calls.append("connect")
        return self.account

    def terminal_info(self):
        self.calls.append("terminal_info")
        from forex_research.execution.adapter import TerminalInfo

        return TerminalInfo(name="MT5", build=6204, trade_allowed=self.trade_expert, connected=True)

    def probe_capabilities(self, symbols):
        self.calls.append("probe_capabilities")
        return BrokerCapabilities(
            trade_expert=self.trade_expert,
            margin_mode=MarginMode.HEDGING,
            terminal_build=6204,
            server="Demo-Server",
            account_currency="USD",
            symbols={
                "EURUSD": SymbolInfo(
                    name="EURUSD",
                    point=0.00001,
                    digits=5,
                    contract_size=100000.0,
                    tick_size=0.00001,
                    tick_value=1.0,
                    volume_min=0.01,
                    volume_step=0.01,
                    volume_max=100.0,
                    stops_level_points=0,
                    freeze_level_points=0,
                    filling_mode=FillingMode.IOC,
                    spread_points=10,
                )
            },
        )


def test_cli_refuses_cleanly_on_empty_allowlist(tmp_path, monkeypatch):
    """EXEC-060 as an operator-visible refusal: an empty/invalid allowlist
    prints a message, persists a 'refused' record, and never constructs the
    adapter (the MT5 import sits behind the config gate by design)."""
    import forex_research.execution.cli as cli_mod
    from forex_research.config.loader import ConfigError

    def empty_allowlist(path):
        raise ConfigError(
            f"allowlist invalid: {path}\n  - allowlist is empty; "
            "connections must be refused (EXEC-060)"
        )

    monkeypatch.setattr(cli_mod, "load_allowlist", empty_allowlist)

    out = tmp_path / "probe"
    rc = cli_mod.main(
        [
            "--symbols",
            "EURUSD",
            "--out",
            str(out),
            "--config",
            "config",
        ]
    )
    assert rc == 2
    records = list(out.glob("probe_*.json"))
    assert records, "refusal must persist a record"
    rec = json.loads(records[0].read_text(encoding="utf-8"))
    assert rec["outcome"] == "refused"
    assert rec["capabilities"] is None
    assert rec["account_login_sha256"] is None


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


def _verifier(allowlist, **kwargs):
    from forex_research.execution.allowlist import verify_account

    verify_account(allowlist, **kwargs)


def test_allowlist_checked_before_capability_query(tmp_path):
    adapter = FakeAdapter()
    run_probe(
        adapter,
        allowlist=_allowlist(),
        verifier=_verifier,
        symbols=["EURUSD"],
        expected={},
        out_dir=tmp_path,
    )
    assert adapter.calls == ["connect", "probe_capabilities"]
    # The allowlist check runs between connect and the first capability query;
    # a refused account never reaches probe_capabilities (EXEC-060).


def test_refusal_persists_record_and_never_probes(tmp_path):
    adapter = FakeAdapter()
    empty = Allowlist(entries=())
    outcome, path = run_probe(
        adapter,
        allowlist=empty,
        verifier=_verifier,
        symbols=["EURUSD"],
        expected={},
        out_dir=tmp_path,
    )
    assert outcome == "refused"
    assert "probe_capabilities" not in adapter.calls
    record = json.loads(path.read_text())
    assert record["outcome"] == "refused"


def test_records_are_redacted(tmp_path):
    adapter = FakeAdapter()
    _, path = run_probe(
        adapter,
        allowlist=_allowlist(),
        verifier=_verifier,
        symbols=["EURUSD"],
        expected={},
        out_dir=tmp_path,
    )
    text = path.read_text()
    record = json.loads(text)
    # The raw login never persists: only its digest, never a plain login field.
    assert '"login"' not in text and 'account_login"' not in text
    assert record["account_login_sha256"] == login_hash(42)


def test_probe_fails_closed_on_trade_expert_false(tmp_path):
    adapter = FakeAdapter(trade_expert=False)
    outcome, path = run_probe(
        adapter,
        allowlist=_allowlist(),
        verifier=_verifier,
        symbols=["EURUSD"],
        expected={},
        out_dir=tmp_path,
    )
    assert outcome == "failed"
    record = json.loads(path.read_text())
    assert any("trade_expert" in e for e in record["errors"])


def test_discrepancies_reported_for_documented_drift():
    caps = FakeAdapter().probe_capabilities(["EURUSD"])
    expected = {
        "EURUSD": {
            "point_size": 0.00001,
            "contract_size": 100000.0,
            "tick_size": 0.00001,
            "volume_min": 0.01,
            "volume_step": 0.02,  # drifted
            "volume_max": 100.0,
        }
    }
    discrepancies = compare_against_expected(caps, expected)
    assert any("volume_step" in d for d in discrepancies)


def test_persist_ok_and_failed_outcomes(tmp_path):
    ok_path = persist_probe_record(
        out_dir=tmp_path,
        outcome="ok",
        account_login=42,
        capabilities=None,
        discrepancies=[],
        errors=[],
    )
    fail_path = persist_probe_record(
        out_dir=tmp_path,
        outcome="failed",
        account_login=0,
        capabilities=None,
        discrepancies=[],
        errors=["boom"],
    )
    assert json.loads(ok_path.read_text())["outcome"] == "ok"
    assert json.loads(fail_path.read_text())["outcome"] == "failed"
