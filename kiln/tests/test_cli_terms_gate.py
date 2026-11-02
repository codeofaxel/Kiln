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


@pytest.fixture(autouse=True)
def _no_account_on_disk(monkeypatch, tmp_path):
    """Keep every gate test on a machine that was never signed in.

    The gate reads the developer's real ``~/.kiln/auth_tokens.json`` otherwise,
    and a lapsed session there would turn the "show the Terms" paths below into
    the "run kiln signin" path.  The lapsed-account tests write their own file.
    """
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path / "no_auth_home"))


def _lapsed_sign_in(monkeypatch, tmp_path, **extra):
    """A token file holding the server's refusal: an account, no way to renew it."""
    import json

    home = tmp_path / "lapsed_home"
    (home / ".kiln").mkdir(parents=True)
    (home / ".kiln" / "auth_tokens.json").write_text(json.dumps({
        "email": "maker@example.com",
        "tier": "pro",
        "access_token": "expired.jwt.token",
        "refresh_rejected_at": "2026-10-19T02:36:31Z",
        **extra,
    }))
    monkeypatch.setenv("KILN_AUTH_HOME", str(home))


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


# --- the fourth path: an account on this machine whose sign-in lapsed --------
#
# The account may already hold the agreement (made on another device) and this
# machine cannot ask with a dead session, so the Terms are the wrong thing to
# show; the fix is a sign-in, which brings the account's agreement down.


def test_lapsed_account_points_to_signin_instead_of_showing_terms(
    monkeypatch, tmp_path, capsys,
):
    monkeypatch.delenv("KILN_ACCEPT_TERMS", raising=False)
    _lapsed_sign_in(monkeypatch, tmp_path)
    monkeypatch.setattr(cli_main, "_terms_gate_interactive", lambda: True)
    prompt = mock.MagicMock(return_value=True)
    monkeypatch.setattr("kiln.terms.prompt_acceptance", prompt)

    with pytest.raises(SystemExit) as exc:
        _enforce_terms_gate()

    assert exc.value.code == 1
    prompt.assert_not_called()  # the Terms were never shown
    err = capsys.readouterr().err
    assert "maker@example.com" in err
    assert "expired" in err.lower()
    assert "kiln signin" in err
    assert "accept-terms" not in err


def test_lapsed_account_non_interactive_still_says_signin(monkeypatch, tmp_path, capsys):
    """A script on a lapsed machine is told to sign in, not to accept-terms:
    accepting here would be a second agreement the account never needed."""
    monkeypatch.delenv("KILN_ACCEPT_TERMS", raising=False)
    _lapsed_sign_in(monkeypatch, tmp_path)
    monkeypatch.setattr(cli_main, "_terms_gate_interactive", lambda: False)

    with pytest.raises(SystemExit) as exc:
        _enforce_terms_gate()

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "kiln signin" in err
    assert "accept-terms" not in err


def test_env_flag_still_wins_over_a_lapsed_account(monkeypatch, tmp_path):
    """The operator's configured acceptance is explicit and needs no account."""
    monkeypatch.setenv("KILN_ACCEPT_TERMS", "1")
    _lapsed_sign_in(monkeypatch, tmp_path)
    rec = mock.MagicMock()
    monkeypatch.setattr("kiln.terms.record_acceptance", rec)
    _enforce_terms_gate()
    rec.assert_called_once()


def test_a_session_that_can_still_renew_is_not_the_lapsed_path(monkeypatch, tmp_path):
    """Only the server's settled refusal counts.  A session with a refresh
    token is still an account that could answer; the gate behaves as before."""
    import json

    monkeypatch.delenv("KILN_ACCEPT_TERMS", raising=False)
    home = tmp_path / "renewable_home"
    (home / ".kiln").mkdir(parents=True)
    (home / ".kiln" / "auth_tokens.json").write_text(json.dumps({
        "email": "maker@example.com",
        "access_token": "some.jwt.token",
        "refresh_token": "still-good",
    }))
    monkeypatch.setenv("KILN_AUTH_HOME", str(home))
    monkeypatch.setattr(cli_main, "_terms_gate_interactive", lambda: True)
    prompt = mock.MagicMock(return_value=True)
    monkeypatch.setattr("kiln.terms.prompt_acceptance", prompt)

    _enforce_terms_gate()

    prompt.assert_called_once()


# --- the gate is wired into the group callback -----------------------------


def test_gate_fires_for_non_exempt_command(runner, monkeypatch):
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: False)
    gate = mock.MagicMock(side_effect=SystemExit(1))
    monkeypatch.setattr(cli_main, "_enforce_terms_gate", gate)
    res = runner.invoke(cli, ["print"])  # a substantive (non-exempt) command
    assert res.exit_code == 1
    gate.assert_called_once()


def test_offline_account_runs_the_command(runner, monkeypatch):
    """A signed-in install whose account could not be asked is not shown the
    Terms: the command runs, and the question is asked again next time."""
    from kiln.terms import TermsCheckUnreachable

    def _silence(*a, **k):
        raise TermsCheckUnreachable("Name or service not known")

    monkeypatch.setattr("kiln.terms.is_current", _silence)
    gate = mock.MagicMock(side_effect=AssertionError("the Terms were shown offline"))
    monkeypatch.setattr(cli_main, "_enforce_terms_gate", gate)
    res = runner.invoke(cli, ["print"])  # a substantive (non-exempt) command
    gate.assert_not_called()
    # The command itself ran: it stopped on ITS OWN complaint -- no printer
    # on this isolated machine, or the missing file argument -- not on the
    # gate's.  (An exit-code check stood here, and passed only while the
    # suite was still reading the developer's real ~/.kiln on Windows.)
    assert "No printers configured" in res.output or "Missing argument" in res.output


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


def test_accept_terms_already_accepted_shows_review(runner, monkeypatch):
    # Already accepted -> the command now REVIEWS: it re-shows the terms summary
    # and when they were accepted, instead of a bare "already accepted" line the
    # user can't inspect.
    import kiln.terms as terms

    monkeypatch.setattr(terms, "is_current", lambda *a, **k: True)
    monkeypatch.setattr(terms, "get_accepted_version", lambda *a, **k: terms._CURRENT_TERMS_VERSION)
    monkeypatch.setattr(terms, "get_accepted_at", lambda *a, **k: 1_700_000_000.0)
    res = runner.invoke(cli, ["accept-terms"])
    assert res.exit_code == 0
    assert "Terms of Use" in res.output            # the summary is re-shown
    assert "accepted these terms" in res.output.lower()  # + when


def test_accept_terms_offline_with_account_still_accepts(runner, monkeypatch):
    """The gate defers offline, but a person who asked to accept is not
    shown a traceback: the command reviews or accepts as it always has."""
    from kiln.terms import TermsCheckUnreachable

    def _silence(*a, **k):
        raise TermsCheckUnreachable("Name or service not known")

    monkeypatch.setattr("kiln.terms.is_current", _silence)
    rec = mock.MagicMock()
    monkeypatch.setattr("kiln.terms.record_acceptance", rec)
    res = runner.invoke(cli, ["accept-terms", "--yes"])
    assert res.exit_code == 0, res.output
    rec.assert_called_once()


def test_setup_offline_with_account_still_asks(runner, monkeypatch):
    """``kiln setup`` asks for acceptance itself; offline with an account it
    asks as before rather than failing on the deferred check."""
    from kiln.terms import TermsCheckUnreachable

    def _silence(*a, **k):
        raise TermsCheckUnreachable("Name or service not known")

    monkeypatch.setattr("kiln.terms.is_current", _silence)
    prompt = mock.MagicMock(return_value=False)
    monkeypatch.setattr("kiln.terms.prompt_acceptance", prompt)
    res = runner.invoke(cli, ["setup"])
    prompt.assert_called_once()
    assert res.exit_code == 1
    assert "must accept the terms" in res.output


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
        "setup", "accept-terms", "serve", "rest", "auth", "self-update",
        "install-mcp", "uninstall-mcp", "install-openscad", "verify", "doctor",
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
