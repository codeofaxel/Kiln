"""``kiln fork`` on a computer without kiln-pro.

The copy is made in the person's Kiln account, so the command asks Kiln's
servers (the ``fork_public_design`` tool) and sends nothing of this
computer's history.  These tests record what it sends and what it says.
"""

from __future__ import annotations

import json
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
    from kiln.cli.history_commands import fork_command

    return CliRunner().invoke(fork_command, list(args))


def _copied(**extra: Any) -> dict[str, Any]:
    return {
        "status": "success", "design_id": "c0ffee12", "slug": "bracket", "proposal": False,
        "forked_from": {"username": "alice", "slug": "bracket"}, **extra,
    }


def test_a_kept_copy_sends_the_address_and_says_where_it_is(server):
    calls, answers = server
    answers["fork_public_design"] = _copied()

    out = _run("@alice/bracket")

    assert out.exit_code == 0, out.output
    assert calls == [("fork_public_design", {"address": "@alice/bracket", "to_propose": False})]
    assert "Copied @alice/bracket into your Kiln account as bracket (design c0ffee12)" in out.output
    assert "cloud pull c0ffee12" in out.output
    assert "pr propose" not in out.output


def test_a_copy_to_propose_says_how_the_fix_goes_back(server):
    calls, answers = server
    answers["fork_public_design"] = _copied(proposal=True)

    out = _run("https://kiln3d.com/@alice/bracket", "--to-propose")

    assert out.exit_code == 0, out.output
    assert calls == [("fork_public_design", {
        "address": "https://kiln3d.com/@alice/bracket", "to_propose": True,
    })]
    assert "cloud push c0ffee12 <branch>" in out.output
    assert "pr propose c0ffee12 <branch>" in out.output


def test_a_full_free_plan_lists_the_designs_and_the_flag(server):
    calls, answers = server
    answers["fork_public_design"] = {
        "status": "error", "code": "FREE_SLOT_IN_USE",
        "error": "3 of 3 designs kept. Archive one to make room.",
        "designs": ["mug", "hook", "clip"],
    }

    out = _run("@alice/bracket")

    assert out.exit_code == 1
    assert "mug, hook, clip" in out.output and "--make-room" in out.output

    answers["fork_public_design"] = _copied(made_room={
        "archived_design_id": "mug", "note": "Archived “mug” to make room.",
    })
    again = _run("@alice/bracket", "--make-room", "mug")

    assert again.exit_code == 0, again.output
    assert calls[-1] == ("fork_public_design", {
        "address": "@alice/bracket", "to_propose": False, "make_room_for": "mug",
    })
    assert "Archived “mug” to make room." in again.output


def test_a_refusal_is_said_in_the_servers_words(server):
    _calls, answers = server
    answers["fork_public_design"] = {
        "status": "error", "code": "ALREADY_FORKED",
        "error": "You already have a copy of this design.",
        "existing": {"design_id": "c0ffee12"},
    }

    out = _run("@alice/bracket")

    assert out.exit_code == 1
    assert "You already have a copy of this design." in out.output


def test_signed_out_is_told_to_sign_in_and_nothing_is_sent(monkeypatch, tmp_path):
    """No session: the served call's own sign-in miss, and no request leaves."""
    import kiln.server as kiln_server

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(kiln_server, "_served_bearer", lambda: (None, None))

    def no_network(*_a, **_kw):
        raise AssertionError("a signed-out fork reached the network")

    monkeypatch.setattr("httpx.post", no_network)

    out = _run("@alice/bracket", "--json")

    assert out.exit_code == 1
    answer = json.loads(out.output[: out.output.rindex("}") + 1])
    assert answer["why"] == "signed_out" and answer["tool"] == "fork_public_design"


def test_every_install_gets_the_one_command():
    """A plain install attaches it with the history couriers; kiln-pro
    attaches the same command (register_fork_cli), never a copy."""
    import click

    from kiln.cli.history_commands import fork_command, register_fork_cli, register_history_cli

    plain = click.Group("kiln")
    register_history_cli(plain)
    assert plain.commands["fork"] is fork_command

    with_pro = click.Group("kiln")
    register_fork_cli(with_pro)
    register_fork_cli(with_pro)
    assert with_pro.commands == {"fork": fork_command}


def test_keep_sends_the_flag_and_says_the_copy_now_counts(server):
    calls, answers = server
    answers["fork_public_design"] = {
        "status": "success", "design_id": "c0ffee12", "kept": True, "proposal": False,
    }

    out = _run("@alice/bracket", "--keep")

    assert out.exit_code == 0, out.output
    assert calls == [("fork_public_design", {
        "address": "@alice/bracket", "to_propose": False, "keep": True,
    })]
    assert "c0ffee12 is one of your designs now" in out.output
