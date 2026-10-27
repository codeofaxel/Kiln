"""The paid ``kiln`` commands on a computer without kiln-pro.

Each one asks Kiln's servers through the same stub this install's agent
calls for that tool (``kiln.server.served_tool``), so a design's history
rides along and a miss is worded the same way.  These tests record what
each command sends, prove a plain install has every one, and prove each
names a tool the manifest really serves.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

#: argv -> (the served tool it must ask, arguments the call must carry).
CASES: list[tuple[list[str], str, dict[str, Any]]] = [
    (["branch", "annotate", "b1", "thicker wall"], "annotate_design_branch",
     {"branch_id": "b1", "annotation": "thicker wall"}),
    (["branch", "rename", "b1", "wide"], "rename_design_branch", {"branch_id": "b1", "new_name": "wide"}),
    (["branch", "retire", "b1"], "retire_design_branch", {"branch_id": "b1"}),
    (["release", "yank", "r1", "--reason", "wrong hole"], "retire_design_release",
     {"release_id": "r1", "reason": "wrong hole"}),
    (["cherrypick", "list", "v0", "v1"], "list_cherry_pick_candidates",
     {"ancestor_version_id": "v0", "source_version_id": "v1"}),
    (["cherrypick", "apply", "v0", "v1", "v2", "f1"], "cherry_pick_modification",
     {"ancestor_version_id": "v0", "source_version_id": "v1", "target_version_id": "v2", "feature_id": "f1"}),
    (["visualize-branch-tree", "--artifact-id", "mug"], "visualize_branch_tree",
     {"artifact_id": "mug", "artifact_type": "design", "detail": "rich", "max_commits": 50}),
    (["design-lineage", "--artifact-id", "mug", "--no-outcomes"], "show_design_lineage",
     {"artifact_id": "mug", "annotate_outcomes": False}),
    (["compare-branches", "--artifact-id", "mug", "--branch-a", "main", "--branch-b", "wide"],
     "compare_branches", {"artifact_id": "mug", "branch_a": "main", "branch_b": "wide"}),
    (["show-branch-history", "--design-id", "mug", "--branch", "main"], "show_branch_history",
     {"design_id": "mug", "branch_name": "main", "annotate_outcomes": True}),
    (["reflog", "show", "--artifact-id", "mug"], "show_reflog", {"artifact_id": "mug", "limit": 100}),
    (["reflog", "undo-last", "--artifact-type", "design", "--branch-id", "b1"], "undo_last_head_change",
     {"artifact_type": "design", "branch_id": "b1"}),
    (["bisect", "start", "--artifact-type", "design", "--artifact-id", "mug", "--good", "v1", "--bad", "v5"],
     "bisect_start", {"artifact_id": "mug", "good_version_id": "v1", "bad_version_id": "v5"}),
    (["bisect", "good", "--session-id", "s1"], "bisect_good", {"session_id": "s1"}),
    (["bisect", "bad", "--session-id", "s1", "--version-id", "v3"], "bisect_bad",
     {"session_id": "s1", "version_id": "v3"}),
    (["bisect", "skip", "--session-id", "s1"], "bisect_skip", {"session_id": "s1"}),
    (["bisect", "current", "--session-id", "s1"], "bisect_current", {"session_id": "s1"}),
    (["bisect", "end", "--session-id", "s1"], "bisect_end", {"session_id": "s1"}),
    (["recipes", "list", "--design-id", "mug"], "list_proven_recipes", {"design_id": "mug", "limit": 50}),
    (["ab", "report", "mug", "--min-outcomes", "2"], "cross_branch_ab_report",
     {"design_id": "mug", "min_outcomes_per_branch": 2}),
    (["creator", "show"], "get_creator_credit", {}),
    (["creator", "set", "--handle", "ada", "--link", "Shop=https://example.com/shop", "--license-off"],
     "set_creator_credit", {"handle": "ada", "links": [{"label": "Shop", "url": "https://example.com/shop"}],
                            "show_license_on_artifacts": False}),
    (["creator", "design", "mug", "hide"], "set_creator_credit",
     {"design_id": "mug", "design_credit": "hide"}),
    (["design", "archive", "mug"], "archive_design", {"design_id": "mug"}),
    (["design", "unarchive", "mug"], "unarchive_design", {"design_id": "mug"}),
    (["versions", "best", "v1", "v2", "--material", "PETG"], "best_design_version",
     {"version_ids": ["v1", "v2"], "material": "PETG"}),
    (["manual", "list-history", "mug"], "list_manual_history", {"design_id": "mug", "include_retired": False}),
    (["manual", "get-revision", "m1"], "get_manual_revision", {"manual_id": "m1"}),
    (["manual", "retire", "m1", "--reason", "old box"], "retire_manual_revision",
     {"manual_id": "m1", "reason": "old box"}),
    (["manual", "diff", "--old", "m1", "--new", "m2"], "diff_assembly_manuals",
     {"manual_id_old": "m1", "manual_id_new": "m2"}),
]

#: Commands whose arguments are files on this computer, run in their own tests.
FILE_COMMANDS = {("merge", "source"), ("visual-diff",), ("manual", "verify"), ("manual", "verify-batch")}
#: Commands that open a page of Kiln's web app and ask no tool.
PAGE_COMMANDS = {("subscribe",), ("manage-billing",)}


def _root() -> click.Group:
    """A plain install's command line: the history couriers, then these."""
    from kiln.cli.history_commands import register_history_cli
    from kiln.cli.served_commands import register_served_cli

    root = click.Group("kiln")
    root.add_command(click.Group("versions"))
    register_history_cli(root)
    register_served_cli(root)
    return root


@pytest.fixture
def server(monkeypatch, tmp_path):
    """Kiln's servers, answering from a script; every call recorded."""
    import kiln.server as kiln_server

    monkeypatch.setenv("HOME", str(tmp_path))
    calls: list[tuple[str, dict[str, Any]]] = []
    answers: dict[str, dict[str, Any]] = {}

    def fake_call(tool: str, /, **kwargs: Any) -> dict[str, Any]:
        calls.append((tool, {k: v for k, v in kwargs.items() if not k.startswith("_")}))
        return answers.get(tool, {"status": "success"})

    monkeypatch.setattr(kiln_server, "_pro_api_call", fake_call)
    return calls, answers


@pytest.mark.parametrize(("argv", "tool", "sent"), CASES, ids=[" ".join(c[0][:2]) for c in CASES])
def test_each_command_asks_its_served_tool(server, argv, tool, sent):
    calls, _ = server

    out = CliRunner().invoke(_root(), argv)

    assert out.exit_code == 0, out.output
    (asked, kwargs), = calls
    assert asked == tool
    assert {k: kwargs.get(k) for k in sent} == sent
    assert all(v is not None for v in kwargs.values())   # a default is the tool's own, never None


def test_a_history_command_carries_this_computers_history(server):
    calls, _ = server

    out = CliRunner().invoke(_root(), ["branch", "rename", "b1", "wide"])

    assert out.exit_code == 0, out.output
    (_, kwargs), = calls
    assert "history_records" in kwargs


def test_merge_source_sends_the_three_texts_and_prints_the_merge(server, tmp_path):
    calls, answers = server
    for name, text in (("base", "a=1;\n"), ("left", "a=2;\n"), ("right", "a=1;\nb=3;\n")):
        (tmp_path / f"{name}.scad").write_text(text, encoding="utf-8")
    answers["source_merge_three_way"] = {"status": "success", "merged_text": "a=2;\nb=3;\n", "had_conflicts": False}

    out = CliRunner().invoke(_root(), [
        "merge", "source", "--base", str(tmp_path / "base.scad"),
        "--left", str(tmp_path / "left.scad"), "--right", str(tmp_path / "right.scad"),
    ])

    assert out.exit_code == 0, out.output
    (tool, kwargs), = calls
    assert tool == "source_merge_three_way"
    assert (kwargs["base_source"], kwargs["left_source"], kwargs["right_source"]) == ("a=1;\n", "a=2;\n", "a=1;\nb=3;\n")
    assert out.output == "a=2;\nb=3;\n"


def test_merge_source_exits_1_on_a_conflict(server, tmp_path):
    _, answers = server
    for name in ("base", "left", "right"):
        (tmp_path / f"{name}.scad").write_text("x\n", encoding="utf-8")
    answers["source_merge_three_way"] = {"status": "success", "merged_text": "<<<<<<<\n", "had_conflicts": True}

    out = CliRunner().invoke(_root(), [
        "merge", "source", "--base", str(tmp_path / "base.scad"),
        "--left", str(tmp_path / "left.scad"), "--right", str(tmp_path / "right.scad"),
    ])

    assert out.exit_code == 1
    assert "unresolved conflicts" in out.output


def test_visual_diff_sends_both_models_up(server, monkeypatch, tmp_path):
    calls, _ = server
    import kiln.served_makes as served_makes

    sent: list[str] = []

    def fake_send(tool, kwargs, inputs, trip):
        sent.extend(sorted((inputs or {}).get("files", {})))
        return kwargs, None

    monkeypatch.setattr(served_makes, "send_inputs", fake_send)
    for name in ("before", "after"):
        (tmp_path / f"{name}.stl").write_bytes(b"solid x\nendsolid x\n")

    out = CliRunner().invoke(_root(), [
        "visual-diff", "--before", str(tmp_path / "before.stl"), "--after", str(tmp_path / "after.stl"),
    ])

    assert out.exit_code == 0, out.output
    assert sent == ["after_mesh_path", "before_mesh_path"]
    (tool, _), = calls
    assert tool == "visual_diff_meshes"


def test_a_refusal_is_said_in_the_servers_words(server):
    _, answers = server
    answers["bisect_start"] = {"status": "error", "error": "Finding the version that broke is part of Kiln Pro."}

    out = CliRunner().invoke(_root(), [
        "bisect", "start", "--artifact-type", "design", "--artifact-id", "mug", "--good", "v1", "--bad", "v5",
    ])

    assert out.exit_code == 1
    assert "part of Kiln Pro" in out.output


def test_signed_out_is_told_to_sign_in(monkeypatch, tmp_path):
    """No stand-in for the servers: a signed-out install never reaches them."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KILN_AUTH_HOME", str(tmp_path))
    monkeypatch.delenv("KILN_LICENSE_KEY", raising=False)

    out = CliRunner().invoke(_root(), ["manual", "get-revision", "m1", "--json"])

    assert out.exit_code == 1
    answer = json.loads(out.output[: out.output.rindex("}") + 1])
    assert answer["why"] == "signed_out"
    assert answer["code"] in ("KILN_ACCOUNT_NOT_PAIRED", "KILN_SESSION_EXPIRED")


def test_every_command_names_a_tool_the_manifest_serves():
    """A tool Kiln's servers refuse has no stub, so a command on it could never run."""
    from kiln.server import served_tool

    for _, tool, _ in CASES:
        assert served_tool(tool) is not None, tool
    for tool in ("source_merge_three_way", "visual_diff_meshes"):
        assert served_tool(tool) is not None, tool


def test_creator_set_leaves_out_what_was_not_said(server):
    """Nothing said is nothing sent: the account keeps its links and licence."""
    calls, _ = server

    out = CliRunner().invoke(_root(), ["creator", "set", "--handle", "ada"])

    assert out.exit_code == 0, out.output
    (_, kwargs), = calls
    assert kwargs == {"handle": "ada"}


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


@pytest.fixture
def verifier(monkeypatch, tmp_path):
    """Kiln's public verifier; every upload recorded with the form it carried."""
    import httpx

    monkeypatch.setenv("HOME", str(tmp_path))
    sent: list[dict[str, Any]] = []
    answers: dict[str, Any] = {}

    def post(url, *, files, data, timeout):
        name, handle = files["file"]
        sent.append({"url": url, "name": name, "bytes": handle.read(), "data": data})
        reply = answers.get(name, answers.get("*"))
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(httpx, "post", post)
    return sent, answers


def _pdf(tmp_path, name, body=b"%PDF-1.7 manual"):
    path = tmp_path / name
    path.write_bytes(body)
    return str(path)


def test_verify_sends_the_file_to_the_public_verifier(verifier, tmp_path):
    sent, answers = verifier
    answers["*"] = _Resp(200, {"status": "success", "data": {
        "authentic": True, "public_key_fingerprint": "ab12", "design_id": "lamp", "release_name": "v1"}})

    out = CliRunner().invoke(_root(), ["manual", "verify", _pdf(tmp_path, "lamp.pdf"), "--release-id", "r1"])

    assert out.exit_code == 0, out.output
    (call,) = sent
    assert call["url"].endswith("/api/public/verify")
    assert call["name"] == "lamp.pdf" and call["bytes"] == b"%PDF-1.7 manual"
    assert call["data"] == {"release_id": "r1"}
    assert "Authentic" in out.output and "lamp/v1" in out.output


def test_a_mismatch_says_not_authentic_and_exits_1(verifier, tmp_path):
    _, answers = verifier
    answers["*"] = _Resp(200, {"status": "success", "data": {"authentic": False, "reason": "hash differs"}})

    out = CliRunner().invoke(_root(), ["manual", "verify", _pdf(tmp_path, "lamp.pdf")])

    assert out.exit_code == 1
    assert "NOT authentic" in out.output and "hash differs" in out.output


def test_no_answer_is_never_a_yes(verifier, tmp_path):
    import httpx

    _, answers = verifier
    answers["*"] = httpx.ConnectError("no route to host")

    out = CliRunner().invoke(_root(), ["manual", "verify", _pdf(tmp_path, "lamp.pdf"), "--json"])

    assert out.exit_code == 1
    assert "Authentic" not in out.output
    answer = json.loads(out.output[: out.output.rindex("}") + 1])
    assert answer["code"] == "SERVER_UNREACHABLE" and answer.get("why")


def test_a_release_nobody_signed_is_said(verifier, tmp_path):
    _, answers = verifier
    answers["*"] = _Resp(404, {"status": "error", "code": "RELEASE_NOT_FOUND", "error": "No such release."})

    out = CliRunner().invoke(_root(), ["manual", "verify", _pdf(tmp_path, "lamp.pdf"), "--release-id", "nope"])

    assert out.exit_code == 1 and "No such release" in out.output


def test_verify_batch_tallies_and_exits_1_unless_all_are_authentic(verifier, tmp_path):
    _, answers = verifier
    answers["good.pdf"] = _Resp(200, {"status": "success", "data": {"authentic": True}})
    answers["bad.pdf"] = _Resp(200, {"status": "success", "data": {"authentic": False, "reason": "hash differs"}})

    out = CliRunner().invoke(_root(), [
        "manual", "verify-batch", _pdf(tmp_path, "good.pdf"), _pdf(tmp_path, "bad.pdf"),
    ])

    assert out.exit_code == 1
    assert "1 / 2 authentic" in out.output and "mismatch  " in out.output


@pytest.fixture
def mug_here(monkeypatch, tmp_path):
    """A design with two versions kept on this computer."""
    import kiln.plugins.version_tools as version_tools

    root = tmp_path / "designs"
    folder = root / "mug"
    folder.mkdir(parents=True)
    for n in (1, 2):
        (folder / f".kiln_recipe.v{n}.json").write_text(json.dumps({"version": n, "name": "mug"}), encoding="utf-8")
    monkeypatch.setattr(version_tools, "_DESIGNS_ROOT", str(root))
    return folder


def _archived(folder):
    return [json.loads(p.read_text()).get("archived") for p in sorted(folder.glob(".kiln_recipe*.json"))]


def test_archive_marks_this_computers_copy_after_the_account(server, mug_here):
    _, answers = server
    answers["archive_design"] = {"status": "success", "design_id": "mug", "archived": True, "recipe_files_updated": 1}

    out = CliRunner().invoke(_root(), ["design", "archive", "mug", "--json"])

    assert out.exit_code == 0, out.output
    assert _archived(mug_here) == [True, True]
    assert json.loads(out.output)["on_this_computer"] == {"recipe_files_updated": 2}


def test_a_design_only_on_this_computer_is_archived_here_and_says_so(server, mug_here):
    _, answers = server
    answers["archive_design"] = {"status": "error", "code": "NOT_FOUND", "error": "That design isn't in your library."}

    out = CliRunner().invoke(_root(), ["design", "archive", "mug"])

    assert out.exit_code == 0, out.output
    assert _archived(mug_here) == [True, True]
    assert "not in your Kiln account" in out.output


def test_no_answer_from_the_servers_changes_nothing_here(server, mug_here):
    _, answers = server
    answers["archive_design"] = {"status": "error", "code": "KILN_ACCOUNT_NOT_PAIRED", "why": "signed_out",
                                 "error": "Sign in to Kiln first."}

    out = CliRunner().invoke(_root(), ["design", "archive", "mug"])

    assert out.exit_code == 1
    assert _archived(mug_here) == [None, None]


def test_unarchive_clears_the_mark_here(server, mug_here):
    _, answers = server
    for path in mug_here.glob(".kiln_recipe*.json"):
        path.write_text(json.dumps({**json.loads(path.read_text()), "archived": True}), encoding="utf-8")
    answers["unarchive_design"] = {"status": "success", "design_id": "mug", "archived": False}

    out = CliRunner().invoke(_root(), ["design", "unarchive", "mug"])

    assert out.exit_code == 0, out.output
    assert _archived(mug_here) == [False, False]


def test_every_served_command_has_a_case():
    from kiln.cli import served_commands

    registered: set[tuple[str, ...]] = set()
    for group, commands in served_commands._INTO_GROUPS.items():
        registered |= {(group, c.name) for c in commands}
    for command in served_commands._TOP_LEVEL:
        if isinstance(command, click.Group):
            registered |= {(command.name, sub) for sub in command.commands}
        else:
            registered.add((command.name,))
    covered = {tuple(argv[:2]) if tuple(argv[:2]) in registered else (argv[0],) for argv, _, _ in CASES}
    assert registered - covered - FILE_COMMANDS - PAGE_COMMANDS == set()


def test_a_plain_install_has_every_one():
    """The real command line, imported where kiln-pro cannot be."""
    import kiln

    src = str(Path(kiln.__file__).resolve().parent.parent)
    code = (
        "import json, sys\n"
        "sys.modules['kiln_pro'] = None\n"
        "import click\n"
        "from kiln.cli.main import cli\n"
        "def walk(c, p):\n"
        "    if isinstance(c, click.Group):\n"
        "        return [x for n, s in c.commands.items() for x in walk(s, p + [n])]\n"
        "    return [' '.join(p)]\n"
        "print(json.dumps(walk(cli, [])))\n"
    )
    env = {**os.environ, "PYTHONPATH": src, "KILN_SANDBOX": "1"}
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=240)
    assert done.returncode == 0, done.stderr[-2000:]
    have = set(json.loads(done.stdout.strip().splitlines()[-1]))
    want = {" ".join(argv[:2]) if argv[0] in {"branch", "release", "cherrypick", "reflog", "bisect", "recipes",
                                              "versions", "manual", "ab", "creator", "design"} else argv[0]
            for argv, _, _ in CASES}
    want |= {"merge source", "visual-diff", "manual verify", "manual verify-batch", "subscribe", "manage-billing"}
    assert want - have == set()


def test_an_agent_on_a_plain_install_can_push_a_release(monkeypatch):
    """cloud_push_release runs here (its records, its own sign-in), never on
    the servers that refuse it by name; the manifest no longer hides it."""
    import kiln.history_courier as courier
    from kiln.server import served_tool

    ran: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(courier, "run_local",
                        lambda tool, mode, kwargs: ran.append((tool, mode, kwargs)) or {"status": "success"})
    stub = served_tool("cloud_push_release")
    assert stub is not None

    assert stub(release_id="r1") == {"status": "success"}
    assert ran == [("cloud_push_release", "release_push", {"release_id": "r1"})]


@pytest.mark.parametrize("command", ["subscribe", "manage-billing"])
def test_plans_and_billing_open_the_web_billing_page(monkeypatch, command):
    """A plan is chosen, changed or cancelled signed in on the web, never
    from a terminal: both commands open that page and ask nothing else."""
    import webbrowser

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)

    out = CliRunner().invoke(_root(), [command])

    assert out.exit_code == 0, out.output
    assert opened == ["https://app.kiln3d.com/settings/billing"]
    assert "https://app.kiln3d.com/settings/billing" in out.output
    shown = CliRunner().invoke(_root(), [command, "--no-open", "--json"])
    assert json.loads(shown.output) == {"status": "success", "url": "https://app.kiln3d.com/settings/billing"}
