"""Tests for the CLI Terms-of-Use gate + the ``kiln accept-terms`` command.

The gate lives in the ``cli`` group callback and is one-time: once acceptance is
recorded, ``is_current()`` short-circuits and it never prompts again.  Exempt
commands (setup / accept-terms / serve / --help) must run before acceptance.
"""

from __future__ import annotations

from unittest import mock

import pytest
from click.testing import CliRunner

import kiln.cli.main as cli_main
from kiln.cli.main import _enforce_terms_gate, cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def _quiet_startup(monkeypatch):
    """Silence the group callback's background update check (no network in tests)."""
    monkeypatch.setattr("kiln.version_check.kick_background_check", lambda: None, raising=False)
    monkeypatch.setattr("kiln.version_check.update_banner_line", lambda: "", raising=False)


# --- _enforce_terms_gate: the three paths ----------------------------------


def test_env_flag_records_by_configuration(monkeypatch):
    monkeypatch.setenv("KILN_ACCEPT_TERMS", "1")
    rec = mock.MagicMock()
    monkeypatch.setattr("kiln.terms.record_acceptance", rec)
    _enforce_terms_gate()  # returns without raising
    rec.assert_called_once()
    assert rec.call_args.kwargs.get("method") == "env"


def test_non_interactive_without_flag_exits_loudly(monkeypatch):
    monkeypatch.delenv("KILN_ACCEPT_TERMS", raising=False)
    monkeypatch.setattr(cli_main, "_terms_gate_interactive", lambda: False)
    with pytest.raises(SystemExit) as exc:
        _enforce_terms_gate()
    assert exc.value.code == 1


def test_interactive_accept_returns(monkeypatch):
    monkeypatch.delenv("KILN_ACCEPT_TERMS", raising=False)
    monkeypatch.setattr(cli_main, "_terms_gate_interactive", lambda: True)
    monkeypatch.setattr("kiln.terms.prompt_acceptance", lambda method="setup": True)
    _enforce_terms_gate()  # accepted -> no raise


def test_interactive_decline_exits(monkeypatch):
    monkeypatch.delenv("KILN_ACCEPT_TERMS", raising=False)
    monkeypatch.setattr(cli_main, "_terms_gate_interactive", lambda: True)
    monkeypatch.setattr("kiln.terms.prompt_acceptance", lambda method="setup": False)
    with pytest.raises(SystemExit) as exc:
        _enforce_terms_gate()
    assert exc.value.code == 1


# --- the gate is wired into the group callback -----------------------------


def test_gate_fires_for_non_exempt_command(runner, monkeypatch):
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: False)
    gate = mock.MagicMock(side_effect=SystemExit(1))
    monkeypatch.setattr(cli_main, "_enforce_terms_gate", gate)
    res = runner.invoke(cli, ["print"])  # a substantive (non-exempt) command
    assert res.exit_code == 1
    gate.assert_called_once()


def test_gate_skipped_for_exempt_command(runner, monkeypatch):
    # accept-terms is exempt: the gate must NOT fire even when not current.
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: True)
    gate = mock.MagicMock(side_effect=AssertionError("gate fired for exempt command"))
    monkeypatch.setattr(cli_main, "_enforce_terms_gate", gate)
    res = runner.invoke(cli, ["accept-terms"])
    assert res.exit_code == 0
    gate.assert_not_called()


def test_gate_skipped_for_help(runner, monkeypatch):
    gate = mock.MagicMock(side_effect=AssertionError("gate fired on --help"))
    monkeypatch.setattr(cli_main, "_enforce_terms_gate", gate)
    res = runner.invoke(cli, ["--help"])
    assert res.exit_code == 0
    gate.assert_not_called()


# --- the `kiln accept-terms` command ---------------------------------------


def test_accept_terms_already_accepted(runner, monkeypatch):
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: True)
    res = runner.invoke(cli, ["accept-terms"])
    assert res.exit_code == 0
    assert "already accepted" in res.output.lower()


def test_accept_terms_yes_records_noninteractive(runner, monkeypatch):
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: False)
    rec = mock.MagicMock()
    monkeypatch.setattr("kiln.terms.record_acceptance", rec)
    res = runner.invoke(cli, ["accept-terms", "--yes"])
    assert res.exit_code == 0
    rec.assert_called_once()
    assert rec.call_args.kwargs.get("method") == "cli_noninteractive"
    assert "accepted" in res.output.lower()


# --- exempt-set drift guard + the audit-found gate bugs ---------------------


def test_identity_and_onboarding_commands_are_exempt():
    """Pin the exempt set so onboarding/identity can never be silently gated.

    Gating these would brick onboarding and is circular for OAuth users:
    `kiln signin` is what establishes the bearer is_current() needs to import a
    web-side acceptance.
    """
    exempt = cli_main._TERMS_GATE_EXEMPT
    for name in (
        "setup", "accept-terms", "serve", "auth", "self-update",
        "signin", "signout", "whoami", "pair", "link", "login", "logout", "invite",
    ):
        assert name in exempt, name + " must be exempt from the terms gate"


def test_dash_h_does_not_bypass_gate(runner, monkeypatch):
    # -h is --host's short flag (the `auth` command), NOT help — it must not
    # cause the gate to be skipped.
    monkeypatch.setattr("sys.argv", ["kiln", "print", "-h", "octopi.local"])
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: False)
    gate = mock.MagicMock(side_effect=SystemExit(7))
    monkeypatch.setattr(cli_main, "_enforce_terms_gate", gate)
    runner.invoke(cli, ["print"])
    gate.assert_called_once()  # gate fired despite -h in argv


def test_double_dash_help_skips_gate(runner, monkeypatch):
    monkeypatch.setattr("sys.argv", ["kiln", "print", "--help"])
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: False)
    gate = mock.MagicMock(side_effect=AssertionError("gate fired on --help"))
    monkeypatch.setattr(cli_main, "_enforce_terms_gate", gate)
    runner.invoke(cli, ["print"])
    gate.assert_not_called()


def test_decline_at_prompt_blocks(runner, monkeypatch):
    monkeypatch.delenv("KILN_ACCEPT_TERMS", raising=False)
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: False)
    monkeypatch.setattr(cli_main, "_terms_gate_interactive", lambda: True)
    monkeypatch.setattr("kiln.terms.prompt_acceptance", lambda method="setup": False)
    res = runner.invoke(cli, ["print"])
    assert res.exit_code == 1  # declined -> SystemExit(1); did NOT proceed


def test_abort_at_prompt_blocks_not_fail_open(runner, monkeypatch):
    """Ctrl-C / EOF at the consent prompt must BLOCK, not silently proceed.

    Regression: the group callback's broad `except Exception` used to swallow
    click.Abort (a plain Exception, not SystemExit), letting the command run
    unaccepted. The gate now lets the abort propagate.
    """
    import click as _click

    monkeypatch.delenv("KILN_ACCEPT_TERMS", raising=False)
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: False)
    monkeypatch.setattr(cli_main, "_terms_gate_interactive", lambda: True)

    def _abort(method="setup"):
        raise _click.Abort()

    monkeypatch.setattr("kiln.terms.prompt_acceptance", _abort)
    res = runner.invoke(cli, ["print"])
    # Abort propagates -> click exits 1; the body never runs. (A swallowed Abort
    # would have fallen through to print's own missing-arg error, exit code 2.)
    assert res.exit_code == 1
