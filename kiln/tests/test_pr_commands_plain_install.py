"""``kiln pr`` on a computer without kiln-pro.

Pull requests live in the person's Kiln account, shared with the design's
team, so the commands ask Kiln's servers: opening one sends this computer's
history of the design with the call (the servers open it on the author's own
branches); reviewing, listing and reading one send nothing of this computer.
These tests record what each command sends and what it says back.
"""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner


@pytest.fixture
def server(monkeypatch, tmp_path):
    """Kiln's servers, answering from a script; every call recorded."""
    import kiln.server as kiln_server

    monkeypatch.setenv("HOME", str(tmp_path))
    calls: list[tuple[str, dict[str, Any]]] = []
    answers: dict[str, dict[str, Any]] = {}

    def fake_call(tool: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((tool, kwargs))
        return answers.get(tool, {"status": "success"})

    monkeypatch.setattr(kiln_server, "_pro_api_call", fake_call)
    return calls, answers


def _run(*args: str):
    from kiln.cli.history_commands import pr_group

    return CliRunner().invoke(pr_group, list(args))


def test_submit_opens_one_on_this_computers_history(server):
    calls, answers = server
    answers["propose_design_change"] = {
        "status": "success", "pull_request": {"pr_id": "abcd1234ffff", "status": "open"},
    }

    out = _run("submit", "mug", "tweak", "main", "--title", "Thicker wall")

    assert out.exit_code == 0, out.output
    (tool, kwargs), = calls
    assert tool == "propose_design_change"
    assert kwargs["design_id"] == "mug" and kwargs["title"] == "Thicker wall"
    assert kwargs["source_branch_id"] == "tweak" and kwargs["target_branch_id"] == "main"
    assert "history_records" in kwargs
    assert "author_user_id" not in kwargs   # the server records the signed-in account
    assert "Opened pull request abcd1234" in out.output


def test_review_sends_the_decision_and_nothing_of_this_computer(server):
    calls, answers = server
    answers["review_design_pull_request"] = {
        "status": "success", "pull_request": {"status": "approved"},
    }

    out = _run("review", "abcd1234", "--decision", "APPROVE", "--body", "looks right")

    assert out.exit_code == 0, out.output
    (tool, kwargs), = calls
    assert tool == "review_design_pull_request"
    assert kwargs == {"pr_id": "abcd1234", "decision": "APPROVE", "body": "looks right"}
    assert "now approved" in out.output


def test_list_shows_each_request_with_its_branches(server):
    calls, answers = server
    answers["list_design_pull_requests"] = {"status": "success", "pull_requests": [{
        "pr_id": "abcd1234ffff", "status": "open", "title": "Thicker wall",
        "source_branch_name": "tweak", "target_branch_name": "main",
    }]}

    out = _run("list", "--design-id", "mug")

    assert out.exit_code == 0, out.output
    assert calls == [("list_design_pull_requests", {"design_id": "mug", "status": None})]
    assert "Thicker wall" in out.output and "tweak -> main" in out.output


def test_a_refusal_is_said_in_the_servers_words(server):
    calls, answers = server
    answers["review_design_pull_request"] = {
        "status": "error", "code": "SELF_APPROVAL_FORBIDDEN",
        "error": "you can't approve your own pull request",
    }

    out = _run("review", "abcd1234", "--decision", "APPROVE")

    assert out.exit_code == 1
    assert "you can't approve your own pull request" in out.output


def test_the_group_joins_a_cli_that_has_no_pr_command():
    import click

    from kiln.cli.history_commands import register_history_cli

    cli = click.Group("kiln")
    register_history_cli(cli)

    assert "pr" in cli.commands


def test_a_teammates_text_cannot_drive_this_terminal(server):
    """Titles and comments are other people's words: no control characters
    reach the terminal, so an escape sequence prints as text."""
    calls, answers = server
    answers["get_design_pull_request"] = {"status": "success", "pull_request": {
        "pr_id": "abcd1234", "status": "open", "title": "fine\x1b]0;owned\x07",
        "description": "line one\nline two\x1b[2J",
        "review_comments": [{"decision": "APPROVE", "body": "ok\x1b[31mred"}],
    }}

    out = _run("show", "abcd1234")

    assert out.exit_code == 0, out.output
    assert "\x1b" not in out.output and "\x07" not in out.output
    assert "line one\nline two" in out.output


def test_a_design_locked_to_this_computer_opens_no_pull_request(server):
    """A pull request lives in the person's Kiln account; a design locked to
    this computer is never kept there, so nothing is sent."""
    from kiln.history_vault import HistoryVault

    calls, _answers = server
    vault = HistoryVault()
    try:
        vault.set_locked("mug", True)
    finally:
        vault.close()

    out = _run("submit", "mug", "tweak", "main", "--title", "Thicker wall")

    assert out.exit_code == 1 and calls == []
    assert "mug is locked to this computer" in out.output
    assert "kiln design unlock mug" in out.output


# ---------------------------------------------------------------------------
# kiln pr propose / withdraw: a fix to someone else's public design
# ---------------------------------------------------------------------------


@pytest.fixture
def account_branches(monkeypatch):
    """The branches the person's Kiln account holds, per design; every ask recorded."""
    import kiln.history_courier as courier

    asked: list[str] = []
    held: dict[str, dict[str, Any]] = {}

    def fake_remote_branches(tool: str, design_id: str) -> dict[str, Any]:
        asked.append(design_id)
        return held.get(design_id, {"status": "success", "design_id": design_id, "branches": []})

    monkeypatch.setattr(courier, "remote_branches", fake_remote_branches)
    return asked, held


_FIX = "f" * 32


def _proposed(answers: dict[str, dict[str, Any]], **pr: Any) -> None:
    answers["propose_fix_to_public_design"] = {
        "status": "success",
        "pull_request": {"pr_id": "abcd1234ffff", "title": "Thicker wall", "status": "open",
                         "source_branch_name": "fix", "target_branch_name": "main", **pr},
        "proposed_to": {"username": "maker", "slug": "mug"},
    }


def test_propose_names_the_fix_branch_by_its_account_id(server, account_branches):
    """A copy made on kiln3d.com may never come to this computer: its branch
    name is looked up in the account, and only the id goes with the call."""
    calls, answers = server
    asked, held = account_branches
    held["mug-copy"] = {"status": "success", "branches": [
        {"branch_id": "0" * 32, "name": "fix", "status": "retired"},
        {"branch_id": _FIX, "name": "fix", "status": "active"},
    ]}
    _proposed(answers)

    out = _run("propose", "mug-copy", "fix", "--title", "Thicker wall", "--description", "rim cracked")

    assert out.exit_code == 0, out.output
    assert asked == ["mug-copy"]
    assert calls == [("propose_fix_to_public_design", {
        "design_id": "mug-copy", "source_branch_id": _FIX, "title": "Thicker wall",
        "description": "rim cracked", "target_branch_id": None,
    })]
    assert 'Proposed "Thicker wall" to @maker/mug (pull request abcd1234).' in out.output
    assert "The owner reviews it on kiln3d.com." in out.output


def test_propose_sends_an_unknown_source_and_the_target_as_given(server, account_branches):
    calls, answers = server
    _proposed(answers)

    out = _run("propose", "mug-copy", _FIX, "--title", "Thicker wall", "--target", "t" * 32)

    assert out.exit_code == 0, out.output
    (tool, kwargs), = calls
    assert tool == "propose_fix_to_public_design"
    assert kwargs["source_branch_id"] == _FIX and kwargs["target_branch_id"] == "t" * 32
    assert "history_records" not in kwargs   # the account holds the copy; nothing of this computer goes


def test_a_branch_only_this_computer_holds_is_refused_before_anything_is_sent(server, account_branches):
    from kiln.history_vault import HistoryVault

    calls, _answers = server
    vault = HistoryVault()
    try:
        vault.keep_branch_copy({"branch_id": _FIX, "design_id": "mug-copy", "name": "fix", "status": "active"}, [])
    finally:
        vault.close()

    out = _run("propose", "mug-copy", "fix", "--title", "Thicker wall")

    assert out.exit_code == 1 and calls == []
    assert "Branch fix of mug-copy is on this computer, not in your Kiln account." in out.output
    assert "cloud push mug-copy fix" in out.output


def test_with_the_account_out_of_reach_this_computers_branch_goes_and_the_servers_answer(
    server, account_branches,
):
    from kiln.history_vault import HistoryVault

    calls, answers = server
    _asked, held = account_branches
    held["mug-copy"] = {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "Sign in to Kiln."}
    answers["propose_fix_to_public_design"] = {
        "status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "error": "Sign in to Kiln.",
    }
    vault = HistoryVault()
    try:
        vault.keep_branch_copy({"branch_id": _FIX, "design_id": "mug-copy", "name": "fix", "status": "active"}, [])
    finally:
        vault.close()

    out = _run("propose", "mug-copy", "fix", "--title", "Thicker wall")

    assert out.exit_code == 1
    assert calls[0][1]["source_branch_id"] == _FIX
    assert "Sign in to Kiln." in out.output


def test_a_refused_proposal_is_said_in_the_servers_words(server, account_branches):
    calls, answers = server
    answers["propose_fix_to_public_design"] = {
        "status": "error", "code": "FIXES_NOT_ACCEPTED",
        "error": "The owner isn't taking proposed fixes on this design.",
    }

    out = _run("propose", "mug-copy", _FIX, "--title", "Thicker wall")

    assert out.exit_code == 1
    assert "The owner isn't taking proposed fixes on this design." in out.output


def test_the_owners_words_cannot_drive_this_terminal(server, account_branches):
    _calls, answers = server
    answers["propose_fix_to_public_design"] = {
        "status": "success",
        "pull_request": {"pr_id": "abcd\x1b[2J1234", "title": "fine\x1b]0;owned\x07"},
        "proposed_to": {"username": "ma\x1bker", "slug": "mug\x07"},
    }

    out = _run("propose", "mug-copy", _FIX, "--title", "Thicker wall")

    assert out.exit_code == 0, out.output
    assert "\x1b" not in out.output and "\x07" not in out.output


def test_withdraw_closes_ones_own_request_and_sends_nothing_of_this_computer(server):
    calls, answers = server
    answers["withdraw_design_pull_request"] = {
        "status": "success", "pull_request": {"pr_id": "abcd1234ffff", "status": "closed"},
    }

    out = _run("withdraw", "abcd1234ffff")

    assert out.exit_code == 0, out.output
    assert calls == [("withdraw_design_pull_request", {"pr_id": "abcd1234ffff"})]
    assert "Withdrew pull request abcd1234; it is closed." in out.output


def test_withdrawing_someone_elses_request_is_refused_in_the_servers_words(server):
    _calls, answers = server
    answers["withdraw_design_pull_request"] = {
        "status": "error", "code": "NOT_THE_AUTHOR",
        "error": "Only the person who opened this pull request can withdraw it.",
    }

    out = _run("withdraw", "abcd1234ffff", "--json")

    assert out.exit_code == 1
    assert '"code": "NOT_THE_AUTHOR"' in out.output
