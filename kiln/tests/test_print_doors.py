"""Every door that starts a print is read from the source, and none bypasses.

The preview gate lived in ``start_print`` and a handful of siblings; the
one-shot pipelines, the queue, the fleet and the whole CLI called the
adapter directly, and a test print went to the machine through ``kiln
print`` with nobody shown a preview.  The doors are closed now.  This is
what keeps them closed: ``kiln.print_doors`` walks the tree for every
``start_print`` and queue submission and asks whether a clearing helper is
called where it happens; ``kiln doctor`` prints the answer; this file
fails on a bypass.

A/B: ``test_no_door_bypasses_the_gate`` fails on the tree before the doors
were wired (ten doors named) and passes after.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kiln import print_doors, print_signoff, server

# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_ci_bypass(monkeypatch):
    monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
    print_signoff._reset_for_tests()
    yield
    print_signoff._reset_for_tests()


def test_the_walk_sees_the_doors_it_was_written_for():
    """A sweep that silently finds nothing would pass every test below for
    ever while checking nothing."""
    labels = {d.label for d in print_doors.enumerate_print_doors()}
    for expected in (
        "server.py::start_print",
        "pipelines.py::_start_print",
        "scheduler.py::tick",
        "job_splitter.py::submit_split_plan",
        "plugins/queue_tools.py::submit_job",
        "plugins/fleet_tools.py::fleet_submit_job",
        "cli/main.py::print_cmd",
        "cli/main.py::slice",
        "cli/main.py::generate_and_print_cmd",
        "cli/main.py::_dispatch_pending",
    ):
        assert expected in labels, f"the walk no longer sees {expected}; saw {sorted(labels)}"


def test_no_door_bypasses_the_gate():
    """The rule.  A new start path anywhere in the tools, the pipelines or
    the CLI turns this red until it calls a clearing helper or is named
    exempt with a reason."""
    bypassing = [d.label for d in print_doors.enumerate_print_doors() if d.bypasses]
    assert bypassing == [], f"these doors start a print without the gate: {bypassing}"


def test_every_exemption_names_a_door_that_still_exists():
    """A dead line in a safety table is a line somebody will copy."""
    assert print_doors.stale_exemptions() == []


def test_the_only_exemption_is_the_event_mirror():
    """Exemptions are reported, not hidden.  Standing opt-ins and the
    scheduler grant their clearance explicitly now, so they are not here."""
    exempt = {d.label for d in print_doors.enumerate_print_doors() if d.exempt}
    assert exempt == {"server.py::_persist_event"}


def test_the_doctor_line_reads_all_gated():
    ok, line = print_doors.summarize()
    assert ok is True
    assert line.startswith(f"{len(print_doors.enumerate_print_doors())} start doors, all gated")
    assert "named exemption" in line


def test_the_doctor_line_names_a_bypassing_door():
    """The line must say WHICH door, or the next person greps for nothing."""
    doors = print_doors.enumerate_print_doors()
    opened = [
        print_doors.PrintDoor("cli/main.py", "print_cmd", 1, "start", None)
        if d.function == "print_cmd" and d.kind == "start" else d
        for d in doors
    ]
    ok, line = print_doors.summarize(opened)
    assert ok is False
    assert "DOORS BYPASSING: cli/main.py::print_cmd" in line


def test_kiln_doctor_carries_the_line():
    """The walk is only useful if a person can see it."""
    from click.testing import CliRunner

    from kiln.cli.main import cli

    result = CliRunner().invoke(cli, ["doctor", "--json"])
    assert "print_preview_gate" in result.output, result.output
    assert "start doors, all gated" in result.output


def test_a_gate_call_in_one_plugin_tool_does_not_vouch_for_its_sibling():
    """Plugin tools are closures inside one ``register()``; the sweep must
    attribute a gate to the tool that holds it, not to everything under
    the same roof."""
    import ast

    src = '''
def register(mcp):
    def gated():
        _preview_gate_error("gated", "f", None)
        adapter.start_print("f")
    def open_door():
        adapter.start_print("f")
'''
    tree = ast.parse(src)
    fns = {f.name: f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)}
    assert "_preview_gate_error" in print_doors._direct_calls(fns["gated"])
    assert "_preview_gate_error" not in print_doors._direct_calls(fns["open_door"])
    assert "_preview_gate_error" not in print_doors._direct_calls(fns["register"])


def test_the_helper_names_the_walk_accepts_all_exist():
    """A renamed helper would leave the walk vouching for a call nothing
    makes.  Each accepted name is a real function where the doors find it."""
    from kiln.cli import main as cli_main
    from kiln.cli import print_gate

    assert callable(server._preview_gate_error)
    for name in ("token_verdict", "grant", "grant_from_record"):
        assert callable(getattr(print_signoff, name)), name
    assert callable(cli_main.cli_gate)
    assert callable(print_gate.confirm_print_at_terminal)
    assert {"_preview_gate_error", "token_verdict", "grant", "grant_from_record",
            "cli_gate", "confirm_print_at_terminal"} == set(print_doors.GATE_HELPERS)


# ---------------------------------------------------------------------------
# The two doors the walk found still open after the tools were wired
# ---------------------------------------------------------------------------


def test_submit_split_plan_without_a_token_queues_nothing(monkeypatch):
    """Every part of a split plan is a print the scheduler starts later with
    nobody to ask; the question is asked once, at submission."""
    from kiln import job_splitter

    queue = MagicMock()
    monkeypatch.setattr(server, "_get_queue", lambda: queue)
    monkeypatch.setattr(server, "_audit", lambda *a, **k: None)
    monkeypatch.setattr("kiln.persistence.get_db", lambda: MagicMock())
    plan = job_splitter.SplitPlan(
        original_file="/tmp/part.stl", split_type="multi_copy", parts=[],
        total_printers=1, estimated_total_time_seconds=1,
        estimated_sequential_time_seconds=1, time_savings_percentage=0.0,
        assembly_instructions=None,
    )
    with pytest.raises(PermissionError, match="preview"):
        job_splitter.submit_split_plan(plan)
    queue.submit.assert_not_called()


def test_kiln_queue_submit_forwards_the_token_to_the_tools_gate(monkeypatch):
    """``kiln queue submit`` reaches ``submit_job``, which gates; it had no
    way to hand a token through."""
    from click.testing import CliRunner

    from kiln.cli.main import cli

    calls: list[dict] = []
    monkeypatch.setattr(
        "kiln.plugins.queue_tools.submit_job",
        lambda **kw: calls.append(kw) or {"success": True, "job_id": "j1", "message": "queued"},
    )
    result = CliRunner().invoke(cli, ["queue", "submit", "benchy.gcode", "--preview-token", "pg_abc", "--json"])
    assert result.exit_code == 0, result.output
    assert calls and calls[0]["preview_token"] == "pg_abc"


def test_kiln_queue_submit_from_a_shell_with_nobody_at_it_queues_nothing(monkeypatch):
    """No token, no person: the tool's own refusal, and nothing queued."""
    from click.testing import CliRunner

    from kiln.cli.main import cli

    queue = MagicMock()
    monkeypatch.setattr(server, "_get_queue", lambda: queue)
    monkeypatch.setattr(server, "_check_auth", lambda scope: None)
    monkeypatch.setattr(server, "_audit", lambda *a, **k: None)
    result = CliRunner().invoke(cli, ["queue", "submit", "benchy.gcode", "--json"])
    assert result.exit_code != 0
    assert "PREVIEW_NOT_CONFIRMED" in result.output
    queue.submit_result.assert_not_called()
