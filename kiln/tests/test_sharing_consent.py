"""Sharing that waits for the person's yes, asked once.

An install may be launched with ``KILN_TELEMETRY=ask`` and
``KILN_COMMUNITY_OPT_IN=ask`` (the Hermes Agent package is): the daily usage
report and anonymous print-outcome sharing then stay off until the person turns
them on with ``kiln sharing on``, and Kiln asks once, through the agent, whether
they want to.  Every install that does not say ``ask`` keeps today's defaults,
and a person's own ``kiln sharing off`` is honoured wherever they run it.
"""

from __future__ import annotations

import types

import pytest
from click.testing import CliRunner

from kiln import sharing_consent, sharing_nudge
from kiln.community_sync import community_sharing_enabled
from kiln.heartbeat import _telemetry_enabled

_ENVS = (sharing_consent.TELEMETRY_ENV, sharing_consent.COMMUNITY_ENV)


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in (*_ENVS, "KILN_HOSTED_MULTITENANT", "KILN_CLI"):
        monkeypatch.delenv(var, raising=False)
    sharing_nudge._reset_for_tests()
    yield
    sharing_nudge._reset_for_tests()


def _ask(monkeypatch):
    for var in _ENVS:
        monkeypatch.setenv(var, sharing_consent.ASK)


class _Result:
    def __init__(self, structured=None, is_error=False):
        self.structuredContent = structured
        self.isError = is_error
        self.content = []


# --- who shares what -------------------------------------------------------


def test_default_install_shares_as_before():
    assert _telemetry_enabled() is True
    assert community_sharing_enabled() is True


def test_explicit_off_still_wins_over_a_stored_yes(monkeypatch):
    sharing_consent.record_answer(True, via="test")
    monkeypatch.setenv(sharing_consent.TELEMETRY_ENV, "false")
    monkeypatch.setenv(sharing_consent.COMMUNITY_ENV, "0")
    assert _telemetry_enabled() is False
    assert community_sharing_enabled() is False


def test_ask_install_shares_nothing_until_the_person_says_yes(monkeypatch):
    _ask(monkeypatch)
    assert _telemetry_enabled() is False
    assert community_sharing_enabled() is False
    sharing_consent.record_answer(True, via="test")
    assert _telemetry_enabled() is True
    assert community_sharing_enabled() is True


def test_a_no_is_remembered_in_ask_mode(monkeypatch):
    _ask(monkeypatch)
    sharing_consent.record_answer(False, via="test")
    assert _telemetry_enabled() is False
    assert community_sharing_enabled() is False


def test_a_persons_own_no_is_honoured_on_any_install():
    sharing_consent.record_answer(False, via="test")
    assert _telemetry_enabled() is False
    assert community_sharing_enabled() is False


def test_an_unreadable_answer_reads_as_no_answer(tmp_path):
    path = sharing_consent.store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert sharing_consent.stored_answer() is None


# --- the command the person runs ------------------------------------------


def _cli(*args):
    from kiln.cli.main import cli

    return CliRunner().invoke(cli, ["sharing", *args])


def _a_person_at_the_terminal(monkeypatch, present=True):
    from kiln.cli import main as cli_main

    monkeypatch.setattr(cli_main, "_sharing_confirmable", lambda: present)


def test_turning_sharing_on_needs_a_person_to_confirm(monkeypatch):
    """Security review, 2026-10-28: the question handed the agent the command
    that grants consent, and an agent with a shell (Hermes has one) could run
    it without asking anyone.  With nobody at a terminal to confirm, it now
    refuses and records nothing."""
    _ask(monkeypatch)
    _a_person_at_the_terminal(monkeypatch, present=False)
    result = _cli("on")
    assert result.exit_code == 1
    assert sharing_consent.stored_answer() is None
    assert _telemetry_enabled() is False


def test_a_person_who_declines_the_confirmation_shares_nothing(monkeypatch):
    _ask(monkeypatch)
    _a_person_at_the_terminal(monkeypatch)
    from kiln.cli.main import cli

    result = CliRunner().invoke(cli, ["sharing", "on"], input="n\n")
    assert sharing_consent.stored_answer() is None
    assert community_sharing_enabled() is False, result.output


def test_sharing_on_and_off_are_remembered(monkeypatch):
    _ask(monkeypatch)
    _a_person_at_the_terminal(monkeypatch)
    from kiln.cli.main import cli

    result = CliRunner().invoke(cli, ["sharing", "on"], input="y\n")
    assert result.exit_code == 0, result.output
    assert sharing_consent.stored_answer() is True
    assert _telemetry_enabled() is True
    result = _cli("off")
    assert result.exit_code == 0, result.output
    assert sharing_consent.stored_answer() is False
    assert community_sharing_enabled() is False


def test_the_choice_works_before_the_terms_are_accepted(monkeypatch):
    """Saying no to sharing is a privacy choice and sends nothing; a fresh
    install's terms step must not stand in front of it."""
    from kiln.cli import main as cli_main

    assert "sharing" in cli_main._TERMS_GATE_EXEMPT
    monkeypatch.setattr("kiln.terms.is_current", lambda *a, **k: False)
    monkeypatch.setattr(
        cli_main, "_enforce_terms_gate",
        lambda: (_ for _ in ()).throw(AssertionError("terms gate ran")),
    )
    result = _cli("off")
    assert result.exit_code == 0, result.output
    assert sharing_consent.stored_answer() is False


def test_status_says_why_when_the_environment_overrides(monkeypatch):
    sharing_consent.record_answer(True, via="test")
    monkeypatch.setenv(sharing_consent.TELEMETRY_ENV, "false")
    result = _cli("status")
    assert result.exit_code == 0, result.output
    assert sharing_consent.TELEMETRY_ENV in result.output


# --- asked once, through the agent ---------------------------------------


def test_ask_mode_attaches_the_question_once_ever(monkeypatch):
    _ask(monkeypatch)
    monkeypatch.setenv("KILN_CLI", "uvx --from kiln3d==9.9.9 kiln")
    first = _Result({"success": True, "designs": []})
    sharing_nudge._attach(first, None, "list_designs")
    block = first.structuredContent[sharing_nudge.RESULT_KEY]
    assert block["person_runs"] == "uvx --from kiln3d==9.9.9 kiln sharing on"
    assert "turn_on" not in block
    assert "do not run it for them" in block["note"].lower()
    assert first.structuredContent["success"] is True

    # A later result in this process, and a later process, never ask again.
    second = _Result({"success": True})
    sharing_nudge._attach(second, None, "list_designs")
    assert sharing_nudge.RESULT_KEY not in second.structuredContent
    sharing_nudge._reset_for_tests()
    third = _Result({"success": True})
    sharing_nudge._attach(third, None, "list_designs")
    assert sharing_nudge.RESULT_KEY not in third.structuredContent


def test_the_question_names_what_the_daily_report_carries():
    """Lap 2026-10-28: the first wording said "a daily report of Kiln's
    version, your printer models and counts" and "no account details", while
    the report also carries an anonymous install number, the system type,
    firmware, the AI app and whether the install is signed in."""
    q = sharing_nudge.QUESTION.lower()
    for fact in ("install number", "system type", "firmware", "ai app", "signed in"):
        assert fact in q, fact
    assert "account details" not in q


def test_no_question_on_a_default_install():
    r = _Result({"success": True})
    sharing_nudge._attach(r, None, "list_designs")
    assert sharing_nudge.RESULT_KEY not in r.structuredContent


def test_no_question_once_answered(monkeypatch):
    _ask(monkeypatch)
    sharing_consent.record_answer(False, via="test")
    r = _Result({"success": True})
    sharing_nudge._attach(r, None, "list_designs")
    assert sharing_nudge.RESULT_KEY not in r.structuredContent


def test_no_question_on_a_failure(monkeypatch):
    _ask(monkeypatch)
    r = _Result({"success": False, "error": "x"})
    sharing_nudge._attach(r, None, "list_designs")
    assert sharing_nudge.RESULT_KEY not in r.structuredContent
    ok = _Result({"success": True})
    sharing_nudge._attach(ok, None, "list_designs")
    assert sharing_nudge.RESULT_KEY in ok.structuredContent


def test_no_question_on_the_hosted_server(monkeypatch):
    _ask(monkeypatch)
    monkeypatch.setenv("KILN_HOSTED_MULTITENANT", "1")
    r = _Result({"success": True})
    sharing_nudge._attach(r, None, "list_designs")
    assert sharing_nudge.RESULT_KEY not in r.structuredContent


def test_the_question_is_wired_into_the_server():
    """A nudge nobody installs is the same as no nudge."""
    import inspect

    from kiln import server

    assert "sharing_nudge.install(mcp)" in inspect.getsource(server)
