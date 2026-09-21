"""Configuration schemas as validated inputs (MILE-002, PROD-010, CHAL-010/011)."""

from pathlib import Path

import pytest
import yaml

from forex_research.config import (
    ConfigError,
    load_allowlist,
    load_challenge_rules,
    load_fee_schedule_for_server,
    load_fee_schedules,
    load_objective,
    require_challenge_rules,
    require_objective,
)
from forex_research.execution.allowlist import AllowlistViolation, login_hash, verify_account

REPO = Path(__file__).resolve().parents[1]


def _write(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _fee_data(**overrides):
    """A fee schedule shaped like the live canary evidence."""
    data = {
        "schedules": {
            "TEST-Server": {
                "currency": "USD",
                "commission_per_lot_round_trip": 6.00,
                "swap_long_per_lot_per_day": -0.50,
                "swap_short_per_lot_per_day": 0.20,
                "swaps_observed": True,
                "triple_swap_weekdays": [2],
                "provenance": {
                    "source_url": "data/canary/canary_x.json",
                    "retrieved_at": "2026-09-21T01:52:44+00:00",
                    "evidence_sha256": "a" * 64,
                    "observed_on_server": True,
                },
            }
        }
    }
    for key, value in overrides.items():
        # Direct assignment: every override is a top-level schedule key
        # (dict.update would MERGE nested dicts instead of replacing them).
        data["schedules"]["TEST-Server"][key] = value
    return data


def test_fee_schedule_with_canary_evidence_loads(tmp_path):
    path = _write(tmp_path, "fees.yaml", _fee_data())
    fees = load_fee_schedule_for_server(path, "TEST-Server")
    assert fees.commission_per_lot_round_trip == pytest.approx(6.0)
    assert fees.swap_long_per_lot_per_day == pytest.approx(-0.5)
    assert fees.retrieved_at is not None


def test_fee_schedule_without_server_observation_is_refused(tmp_path):
    data = _fee_data(
        provenance={
            "source_url": "https://example.com/terms",
            "retrieved_at": "2026-09-21T01:52:44+00:00",
            "observed_on_server": False,
        }
    )
    path = _write(tmp_path, "fees.yaml", data)
    with pytest.raises(ConfigError, match="observed on the server"):
        load_fee_schedules(path)


def test_fee_schedule_refuses_unobserved_swaps_by_default(tmp_path):
    path = _write(
        tmp_path,
        "fees.yaml",
        _fee_data(
            swap_long_per_lot_per_day=None,
            swap_short_per_lot_per_day=None,
            swaps_observed=False,
        ),
    )
    with pytest.raises(ConfigError, match="swap rates not observed"):
        load_fee_schedules(path)
    # Explicit demo acknowledgement is the only way through, and even then
    # the provenance gate still holds.
    fees = load_fee_schedule_for_server(path, "TEST-Server", allow_unobserved_swaps=True)
    assert fees.commission_per_lot_round_trip == pytest.approx(6.0)


def test_fee_schedule_refuses_missing_server(tmp_path):
    path = _write(tmp_path, "fees.yaml", _fee_data())
    with pytest.raises(ConfigError, match="no fee schedule for server 'OTHER'"):
        load_fee_schedule_for_server(path, "OTHER")


def test_shipped_fees_yaml_carries_the_canary_evidence():
    """The committed LHFXSA schedule must stay evidence-backed: $6/lot from
    the 2026-09-21 canary. Swaps are still unobserved, so the default load
    refuses it and only the explicit demo acknowledgement builds a schedule."""
    fees = load_fee_schedule_for_server(
        REPO / "config" / "fees.yaml", "LHFXSA-Trade", allow_unobserved_swaps=True
    )
    assert fees.commission_per_lot_round_trip == pytest.approx(6.0)
    assert fees.retrieved_at is not None
    with pytest.raises(ConfigError, match="swap rates not observed"):
        load_fee_schedules(REPO / "config" / "fees.yaml")


def complete_objective():
    return {
        "objective": {
            "target": "first_payout",
            "horizon": "12 months",
            "target_annual_return": 0.10,
            "volatility_budget": 0.10,
            "retry_on_failure": True,
            "account_size": 100000,
            "account_type": "swing",
            "max_attempts": 3,
            "max_fee_budget": 1500,
            "max_calendar_time": "9 months",
            "min_acceptable_env": -500,
        }
    }


def test_objective_example_template_is_refused():
    # The shipped template is all-null and must be refused by the loader (MILE-002).
    with pytest.raises(ConfigError, match="all-null"):
        load_objective(REPO / "config" / "objective.example.yaml")


def test_complete_objective_loads_and_require_passes(tmp_path):
    path = _write(tmp_path, "objective.yaml", complete_objective())
    obj = load_objective(path)
    assert obj.target == "first_payout"
    require_objective(path)


def test_incomplete_objective_fails_require(tmp_path):
    data = complete_objective()
    data["objective"]["target_annual_return"] = None
    path = _write(tmp_path, "objective.yaml", data)
    with pytest.raises(ConfigError, match="GATE-031"):
        require_objective(path)


def _verified_rule(value=None, *, status="verified", provenance=None, **extra):
    rule = {
        "status": status,
        "value": value,
        "provenance": provenance
        or {"source_url": "https://example.com/terms", "retrieved_at": "2026-09-20T00:00:00Z"},
    }
    rule.update(extra)
    return rule


def minimal_rules():
    prov = {"source_url": "https://example.com/terms", "retrieved_at": "2026-09-20T00:00:00Z"}
    return {
        "provider": "test_firm",
        "programme": "challenge_2_step",
        "account_type": "swing",
        "rule_set_version": "2026-09-20",
        "timezone": {"iana_zone": "Europe/Prague", "source": "platform docs"},
        "phases": {
            "challenge": {
                "rules": {
                    "profit_target": _verified_rule(
                        {"kind": "percent_initial_capital", "value": 10}
                    ),
                    "maximum_daily_loss": _verified_rule(
                        {"kind": "percent_initial_capital", "value": 5},
                        floor_basis="balance_at_reset_minus_amount",
                        breach_semantics="falls_below",
                        breach_severity="hard",
                        ratchet_on_equity=False,
                        reset_local_time="00:00:00",
                    ),
                    "time_limit_days": {
                        "status": "not_applicable",
                        "value": None,
                        "provenance": prov,
                    },
                }
            }
        },
    }


def test_challenge_rules_unknown_refuse_to_run(tmp_path):
    data = minimal_rules()
    data["phases"]["challenge"]["rules"]["profit_target"]["status"] = "unknown"
    path = _write(tmp_path, "rules.yaml", data)
    load_challenge_rules(path)  # loads fine
    with pytest.raises(ConfigError, match="CHAL-011"):
        require_challenge_rules(path)


def test_challenge_rules_not_applicable_requires_provenance(tmp_path):
    data = minimal_rules()
    data["phases"]["challenge"]["rules"]["time_limit_days"]["provenance"] = {
        "source_url": "",
        "retrieved_at": None,
    }
    path = _write(tmp_path, "rules.yaml", data)
    with pytest.raises(ConfigError, match="provenance"):
        load_challenge_rules(path)


def test_challenge_rules_bad_timezone_refused(tmp_path):
    data = minimal_rules()
    data["timezone"]["iana_zone"] = "UTC+3"  # an offset, not a zone (GATE-005)
    path = _write(tmp_path, "rules.yaml", data)
    with pytest.raises(ConfigError, match="GATE-005"):
        load_challenge_rules(path)


def test_challenge_rules_template_loads_with_unknown_status():
    config = load_challenge_rules(REPO / "config" / "challenge_rules.example.yaml")
    assert config.provider == "example_firm"
    with pytest.raises(ConfigError, match="CHAL-011"):
        require_challenge_rules(REPO / "config" / "challenge_rules.example.yaml")


def test_allowlist_roundtrip_and_enforcement(tmp_path):
    digest = login_hash(1234567890)
    assert len(digest) == 64  # sha256 hex; the raw login never appears
    data = {
        "allowlist": [
            {
                "login_hash": digest,
                "server": "Demo-Server",
                "account_type": "demo",
                "currency": "USD",
                "phase": "demo",
            }
        ]
    }
    path = _write(tmp_path, "accounts.yaml", data)
    allowlist = load_allowlist(path)
    verify_account(
        allowlist,
        login=1234567890,
        server="Demo-Server",
        currency="USD",
        account_type="demo",
        phase="demo",
    )
    with pytest.raises(AllowlistViolation):
        verify_account(
            allowlist,
            login=999,
            server="Demo-Server",
            currency="USD",
            account_type="demo",
            phase="demo",
        )


def test_empty_allowlist_is_refused(tmp_path):
    path = _write(tmp_path, "accounts.yaml", {"allowlist": []})
    with pytest.raises(ConfigError, match="EXEC-060"):
        load_allowlist(path)
