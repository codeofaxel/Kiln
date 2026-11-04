"""``kiln branch create DESIGN`` with no NAME lets Kiln name the branch.

Before this, NAME was a required argument, so a person who had not decided
on a name was stopped at the command line.  Now the words after
``--purpose`` go up as the branch's purpose, the server names the branch from
them, and the command prints the name Kiln chose.  Each test here fails on
the old command: click refused the missing argument.
"""

from __future__ import annotations

from typing import Any

from click.testing import CliRunner

from kiln.cli import history_commands


def _answer(name: str, said: str | None) -> dict[str, Any]:
    answer: dict[str, Any] = {"status": "success", "branch": {"branch_id": "b1", "name": name}}
    if said:
        answer["named"] = {"by": "annotation", "said": said}
    return answer


def test_no_name_sends_the_purpose_and_prints_the_name_kiln_chose(monkeypatch):
    sent: dict[str, Any] = {}

    def served(tool: str, **kwargs: Any) -> dict[str, Any]:
        sent.update(tool=tool, **kwargs)
        return _answer("thicker-walls", "Started a branch called thicker-walls (it can be renamed any time).")

    monkeypatch.setattr(history_commands, "_served", served)
    out = CliRunner().invoke(
        history_commands.branch_group,
        ["create", "mug", "--purpose", "thicker walls", "--base-version-id", "v1"],
    )
    assert out.exit_code == 0, out.output
    assert sent == {
        "tool": "create_design_branch", "design_id": "mug", "name": None,
        "base_version_id": "v1", "annotation": "thicker walls",
    }
    assert "Started a branch called thicker-walls" in out.output
    assert "now continue thicker-walls" in out.output


def test_a_name_given_is_sent_as_given(monkeypatch):
    sent: dict[str, Any] = {}

    def served(tool: str, **kwargs: Any) -> dict[str, Any]:
        sent.update(kwargs)
        return _answer("wide", None)

    monkeypatch.setattr(history_commands, "_served", served)
    out = CliRunner().invoke(history_commands.branch_group, ["create", "mug", "wide", "--base-version-id", "v1"])
    assert out.exit_code == 0, out.output
    assert sent["name"] == "wide" and sent["annotation"] == ""
    assert "Made branch wide of mug" in out.output
