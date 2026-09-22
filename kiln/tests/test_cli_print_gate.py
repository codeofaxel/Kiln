"""A person at a terminal is shown the print and asked; nobody else is.

``cli_gate`` takes a token.  With no token, ``kiln print`` used to be a
dead end for the person typing it — no agent to fetch a token, no way to
say yes.  Now a person at a terminal (stdin AND stdout are TTYs) is shown
the preview and asked; a yes grants the clearance the adapter template
checks, through the same door judge a token faces.  Not a flag: ``yes |``
and an agent's subprocess still get the refusal.

A/B: on the tree before this fallback, the "yes in person" tests fail with
``PREVIEW_NOT_CONFIRMED`` (the adapter never hears the start).
"""

from __future__ import annotations

import os

import pytest
from click.testing import CliRunner

from kiln import preview_evidence, print_consent, print_signoff, server
from kiln.cli import print_gate
from kiln.cli.main import cli
from kiln.preview_gate import PreviewGate
from kiln.print_consent import SOURCE_TERMINAL, consent_for
from kiln.printers.base import (
    JobProgress,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterFile,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
    monkeypatch.setenv("KILN_EMERGENCY_PERSIST", "0")
    preview_evidence._reset_for_tests()
    print_signoff._reset_for_tests()
    print_consent._reset_for_tests()
    import kiln.preview_gate as pg

    monkeypatch.setattr(pg, "_gate", PreviewGate())
    monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_audit", lambda *a, **k: None)
    monkeypatch.setattr("kiln.local_stage.host_renders_apps", lambda *a, **k: False)
    yield
    preview_evidence._reset_for_tests()
    print_signoff._reset_for_tests()
    print_consent._reset_for_tests()


@pytest.fixture
def audits(monkeypatch):
    seen: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(print_gate, "_audit", lambda t, a, d: seen.append((t, a, d)))
    return seen


class _Printer(PrinterAdapter):
    """A real adapter subclass, so ``start_print`` runs the sign-off template."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self._kiln_registered_name = "garage"

    @property
    def name(self) -> str:
        return "fake"

    @property
    def capabilities(self) -> PrinterCapabilities:
        return PrinterCapabilities()

    def get_state(self) -> PrinterState:
        return PrinterState(state=PrinterStatus.IDLE, connected=True)

    def get_job(self) -> JobProgress:
        return JobProgress()

    def list_files(self) -> list[PrinterFile]:
        return [PrinterFile(name="part.gcode", path="part.gcode", size_bytes=1)]

    def upload_file(self, file_path: str) -> UploadResult:
        return UploadResult(success=True, file_name=os.path.basename(file_path), message="ok")

    def _start_print_impl(self, file_name: str, **kwargs) -> PrintResult:
        self.started.append(file_name)
        return PrintResult(success=True, message="started")

    def cancel_print(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def pause_print(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def _resume_print_impl(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def emergency_stop(self) -> PrintResult:
        return PrintResult(success=True, message="")

    def _load_filament_impl(self, plan):
        raise NotImplementedError

    def _unload_filament_impl(self, plan):
        raise NotImplementedError

    def _purge_filament_impl(self, plan):
        raise NotImplementedError

    def set_tool_temp(self, target: float) -> bool:
        return True

    def set_bed_temp(self, target: float) -> bool:
        return True

    def send_gcode(self, commands: list[str]) -> bool:
        return True

    def delete_file(self, file_path: str) -> bool:
        return True


@pytest.fixture
def cli_env(monkeypatch):
    printer = _Printer()
    monkeypatch.setattr("kiln.cli.main._make_adapter", lambda cfg: printer)
    monkeypatch.setattr(
        "kiln.cli.main.load_printer_config",
        lambda *_a, **_k: {"type": "moonraker", "host": "http://t.local", "timeout": 1, "retries": 0},
    )
    monkeypatch.setattr("kiln.cli.main.validate_printer_config", lambda cfg: (True, None))
    return CliRunner(), printer


def _mesh_with_png_on_record(tmp_path, monkeypatch):
    """A local model the terminal 'rendered': the renderer's own record of a
    PNG, the link door's record that it could not, and no OpenSCAD."""
    mesh = tmp_path / "plate.3mf"
    mesh.write_bytes(b"PK\x03\x04 not really a 3mf")
    image = tmp_path / "plate_iso.png"
    preview_evidence.record("png", str(mesh), renderer="stage_paint", shown_sha="abc")
    preview_evidence.record_url_refusal(str(mesh), "signed_out")
    monkeypatch.setattr(print_gate, "render_for_terminal", lambda path: ([str(image)], None))
    monkeypatch.setattr(print_gate.click, "launch", lambda target: None)
    return mesh, image


# ---------------------------------------------------------------------------
# Who counts as a person
# ---------------------------------------------------------------------------


def test_a_shell_with_nobody_at_it_gets_the_token_refusal(cli_env, tmp_path):
    runner, printer = cli_env
    gcode = tmp_path / "part.gcode"
    gcode.write_text("G28\n")
    result = runner.invoke(cli, ["print", str(gcode), "--json"])
    assert result.exit_code != 0
    assert "PREVIEW_NOT_CONFIRMED" in result.output
    assert printer.started == []


def test_yes_piped_into_stdin_is_not_a_person(cli_env, tmp_path):
    runner, printer = cli_env
    gcode = tmp_path / "part.gcode"
    gcode.write_text("G28\n")
    result = runner.invoke(cli, ["print", str(gcode)], input="y\n")
    assert result.exit_code != 0
    assert printer.started == []


def test_nobody_is_present_when_a_stream_is_not_a_terminal(monkeypatch):
    class _Stream:
        @staticmethod
        def isatty():
            return False

    monkeypatch.setattr(print_gate.sys, "stdin", _Stream())
    monkeypatch.setattr(print_gate.sys, "stdout", _Stream())
    assert print_gate._person_is_present() is False
    assert print_gate.confirm_print_at_terminal(tool="t", file_path="x.gcode") is False


# ---------------------------------------------------------------------------
# A person, asked
# ---------------------------------------------------------------------------


def test_a_person_who_says_no_leaves_the_printer_idle(cli_env, audits, monkeypatch, tmp_path):
    runner, printer = cli_env
    mesh, _ = _mesh_with_png_on_record(tmp_path, monkeypatch)
    monkeypatch.setattr(print_gate, "_person_is_present", lambda: True)
    result = runner.invoke(cli, ["print", str(mesh)], input="n\n")
    assert result.exit_code != 0
    assert "Nothing was sent to the printer" in result.output
    assert printer.started == []
    assert any(a == "consent_refused" for _, a, _ in audits)


def test_a_person_who_says_yes_starts_the_print_through_the_judged_door(cli_env, audits, monkeypatch, tmp_path):
    """The yes is the person's; the door is still judged.  A PNG shown from
    a terminal is accepted because the link door said why it could not,
    and the clearance says 'png', not 'trust me'."""
    runner, printer = cli_env
    mesh, image = _mesh_with_png_on_record(tmp_path, monkeypatch)
    monkeypatch.setattr(print_gate, "_person_is_present", lambda: True)

    result = runner.invoke(cli, ["print", str(mesh)], input="y\n")

    assert result.exit_code == 0, result.output
    assert printer.started == ["plate.3mf"]
    assert str(image) in result.output
    assert "You have seen the preview" in result.output
    rec = next(d for _, a, d in audits if a == "consent_granted")
    assert rec["consent"] == SOURCE_TERMINAL
    assert rec["door"] == "png"


def test_a_file_only_on_the_printer_is_described_not_shown(cli_env, audits, monkeypatch, tmp_path):
    runner, printer = cli_env
    gcode = tmp_path / "part.gcode"
    gcode.write_text("G28\n")
    monkeypatch.setattr(print_gate, "_person_is_present", lambda: True)

    result = runner.invoke(cli, ["print", str(gcode)], input="y\n")

    assert result.exit_code == 0, result.output
    assert "describing this job, not showing it" in result.output
    assert printer.started == ["part.gcode"]
    rec = next(d for _, a, d in audits if a == "consent_granted")
    assert rec["door"] == "described"


def test_a_token_on_the_command_line_is_the_preview_not_the_yes(cli_env, audits, monkeypatch, tmp_path):
    """With a token the preview is on record and is not re-rendered — but
    the token is not the yes.  A person at the terminal is still asked;
    a shell with nobody at it is refused."""
    runner, printer = cli_env
    mesh, _ = _mesh_with_png_on_record(tmp_path, monkeypatch)
    token = server.issue_preview_token(str(mesh), door="png")["token"]
    rendered: list[str] = []
    monkeypatch.setattr(print_gate, "render_for_terminal", lambda path: rendered.append(path) or ([], None))

    result = runner.invoke(cli, ["print", str(mesh), "--preview-token", token, "--json"])
    assert result.exit_code != 0, result.output
    assert "PREVIEW_NOT_CONFIRMED" in result.output
    assert printer.started == []

    monkeypatch.setattr(print_gate, "_person_is_present", lambda: True)
    result = runner.invoke(cli, ["print", str(mesh), "--preview-token", token], input="y\n")
    assert result.exit_code == 0, result.output
    assert rendered == []  # the token stands for the preview; nothing is drawn twice
    assert printer.started == ["plate.3mf"]
    rec = next(d for _, a, d in audits if a == "consent_granted")
    assert rec["consent"] == SOURCE_TERMINAL
    assert rec["door"] == "png"


def test_the_terminal_yes_reaches_a_gated_tool_without_a_token(monkeypatch):
    """``kiln queue submit`` calls ``submit_job``, whose gate reads the
    consent record first; the yes given at the terminal is that record."""
    seen: dict = {}

    def _submit(**kw):
        seen["consent"] = consent_for(file_name=kw["file_name"], printer_name=kw.get("printer_name"))
        return {"success": True, "job_id": "j1", "message": "queued"}

    monkeypatch.setattr("kiln.plugins.queue_tools.submit_job", _submit)
    monkeypatch.setattr(print_gate, "_person_is_present", lambda: True)
    monkeypatch.setattr(print_gate, "_audit", lambda *a: None)
    result = CliRunner().invoke(cli, ["queue", "submit", "benchy.gcode", "--printer", "garage"], input="y\n")
    assert result.exit_code == 0, result.output
    assert seen["consent"] is not None
    assert seen["consent"].source == SOURCE_TERMINAL
    assert seen["consent"].printer_name == "garage"


# ---------------------------------------------------------------------------
# Arming an unattended mode is one yes, in person — a flag is not it
# ---------------------------------------------------------------------------


def _watch(runner, watch_dir, printer, **kw):
    from unittest.mock import patch

    with (
        patch("kiln.cli.main._load_fleet_adapters", return_value=({"lab-printer": printer}, [])),
        patch("kiln.cli.main._collect_routing_candidates", return_value=[{"printer_id": "lab-printer"}]),
        patch("kiln.cli.main._route_printer_for_job",
              return_value=("lab-printer", {"recommended_printer": {"score": 92.0}}, None)),
    ):
        return runner.invoke(
            cli, ["ingest", "watch", "--dir", str(watch_dir), "--once", "--auto-queue", "--json"], **kw,
        )


def test_the_watcher_does_not_arm_from_a_shell_with_nobody_at_it(cli_env, tmp_path):
    """An agent can type --auto-queue and drop a file; that is the hole."""
    runner, printer = cli_env
    watch_dir = tmp_path / "incoming"
    watch_dir.mkdir()
    (watch_dir / "widget.gcode").write_text("G28\n")
    result = _watch(runner, watch_dir, printer)
    assert result.exit_code != 0
    assert "PREVIEW_NOT_CONFIRMED" in result.output
    assert printer.started == []


def test_the_watcher_arms_after_one_yes_in_person(cli_env, audits, monkeypatch, tmp_path):
    runner, printer = cli_env
    watch_dir = tmp_path / "incoming"
    watch_dir.mkdir()
    (watch_dir / "widget.gcode").write_text("G28\n")
    monkeypatch.setattr(print_gate, "_person_is_present", lambda: True)
    result = _watch(runner, watch_dir, printer, input="y\n")
    assert result.exit_code == 0, result.output
    assert printer.started == ["widget.gcode"]
    rec = next(d for _, a, d in audits if a == "consent_granted")
    assert "incoming" in rec["scope"]


def test_the_watcher_arms_unattended_only_on_the_audited_switch(cli_env, audits, monkeypatch, tmp_path):
    runner, printer = cli_env
    watch_dir = tmp_path / "incoming"
    watch_dir.mkdir()
    (watch_dir / "widget.gcode").write_text("G28\n")
    monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
    result = _watch(runner, watch_dir, printer)
    assert result.exit_code == 0, result.output
    assert printer.started == ["widget.gcode"]
    assert any(a == "preview_gate_skipped" for _, a, _ in audits)


def test_render_never_claims_a_picture_for_gcode(tmp_path):
    g = tmp_path / "x.gcode"
    g.write_text("G28\n")
    assert print_gate.render_for_terminal(str(g)) == ([], None)
    assert print_gate.render_for_terminal("") == ([], None)
    assert print_gate.render_for_terminal(str(tmp_path / "missing.stl")) == ([], None)


# ---------------------------------------------------------------------------
# A yes ends with the command that earned it
# ---------------------------------------------------------------------------


def test_a_yes_does_not_outlive_the_command_that_took_it(cli_env, audits, monkeypatch, tmp_path):
    """A consent record lives exactly as long as the call it belongs to.

    The terminal yes is taken in one helper and has to stand for the rest
    of the command, so it used to lean on the process exiting to end.  A
    process that runs two commands then handed the first one's answer to
    the second: the gate read a yes for ANOTHER file, called it a
    mismatch, and stopped there -- before it ever asked whether a standing
    window covered the printer.  A print a person had opened a window for
    was refused because of a yes they had given to something else.

    Both halves are pinned: nothing is left in the record, and the next
    command in the same process starts on its own window.
    """
    from kiln import consent_windows

    runner, printer = cli_env
    monkeypatch.setattr(server, "_resolve_effective_printer_name", lambda *_a, **_k: "garage")

    # Command one: a person at the terminal, shown the print, says yes.
    mesh, _ = _mesh_with_png_on_record(tmp_path, monkeypatch)
    monkeypatch.setattr(print_gate, "_person_is_present", lambda: True)
    assert runner.invoke(cli, ["print", str(mesh)], input="y\n").exit_code == 0
    assert printer.started == ["plate.3mf"]

    # Nothing of that yes is left behind for the next command to inherit.
    assert consent_for(file_name=str(mesh), printer_name=None) is None
    assert print_signoff.current() is None

    # Command two: nobody at the terminal, a different file whose preview
    # is on record, and a standing window over this printer.  The window
    # is the only yes here, and it is the one the gate must find.
    gcode = tmp_path / "part.gcode"
    gcode.write_text("G28\n")
    preview_evidence.record("png", str(gcode), renderer="stage", shown_sha="abc")
    preview_evidence.record_url_refusal(str(gcode), "signed_out")
    token = server.issue_preview_token(str(gcode), door="png")["token"]
    monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: True)
    consent_windows.open_window(seconds=3600, scope=("garage",))
    monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: False)
    monkeypatch.setattr(print_gate, "_person_is_present", lambda: False)

    result = runner.invoke(cli, ["print", str(gcode), "--json", "--preview-token", token])

    assert result.exit_code == 0, result.output
    assert printer.started == ["plate.3mf", "part.gcode"]
    rec = next(d for _, a, d in audits if a == "consent_granted")
    assert rec["consent"] == SOURCE_TERMINAL  # command one's, and it ended there
