"""A start skips the person's yes, the preview and the print-time safety gate
as a resume only when the file is a resume file Kiln made for the job the
printer reports paused right now.

A paused printer is not enough: anything that can pause a running print
could then start any file and call it a resume.  These tests pause a printer
mid-job and start a different file under every resume claim, through every
gate that grants a resume, and require the yes, the preview and the safety
gate.  A resume file Kiln made for that job, uploaded through Kiln to that
printer, still continues with nothing more asked.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiln import monitor_twin, print_signoff, server

try:
    from kiln import resume_provenance
except ImportError:  # a build with no resume record: only the attack is judged
    resume_provenance = None  # type: ignore[assignment]
from kiln.printers import base as printers_base
from kiln.printers.base import (
    JobProgress,
    PrinterAdapter,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)

JOB = "vase.gcode.3mf"


class _Machine(PrinterAdapter):
    """A printer that keeps what it was sent, under the name it was sent as."""

    name = "kilntest"

    def __init__(self) -> None:
        self.status = PrinterStatus.PAUSED
        self.job = JOB
        self.files: dict[str, bytes] = {}
        self.started: list[str] = []

    def get_state(self) -> PrinterState:
        return PrinterState(state=self.status, connected=True)

    def get_job(self) -> JobProgress:
        return JobProgress(file_name=self.job)

    def upload_file(self, file_path: str) -> UploadResult:
        name = os.path.basename(file_path)
        self.files[name] = Path(file_path).read_bytes()
        return UploadResult(success=True, file_name=name, message="ok")

    def _start_print_impl(self, file_name: str, **kwargs) -> PrintResult:
        self.started.append(file_name)
        return PrintResult(success=True, message="started")


_Machine.__abstractmethods__ = frozenset()


@pytest.fixture()
def machine(monkeypatch, tmp_path):
    printer = _Machine()
    printer._kiln_registered_name = "garage"
    monkeypatch.setattr(server, "_resolve_adapter", lambda *_a, **_k: printer)
    # The upload's own file checks are not what is under test here.
    monkeypatch.setattr(printers_base, "_preflight_upload_or_raise", lambda _a, path: path)
    monkeypatch.setattr(printers_base, "_incomplete_upload_reason", lambda _a, _path: None)
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", tmp_path / "twin")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", tmp_path / "twin" / "active.json")
    monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
    if resume_provenance is not None:
        from tests._kiln_resume import reset_resume_records

        reset_resume_records()
    print_signoff.clear()
    yield printer
    if resume_provenance is not None:
        reset_resume_records()
    print_signoff.clear()


def _file(tmp_path: Path, name: str, body: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(body)
    return path


def _job_source(machine: _Machine) -> str:
    """Kiln's own copy of the print *machine* is running (the toolpath it
    kept when it started the job)."""
    from tests._kiln_resume import kiln_started

    return kiln_started(machine, machine.job)


def _kiln_made(machine: _Machine, tmp_path: Path, name: str = "transformed_resume_ab12.3mf") -> str:
    """A resume file Kiln made for the paused job, from Kiln's own copy of
    it, uploaded through Kiln."""
    path = _file(tmp_path, name, b"resume from layer 40 of the vase")
    record, why = resume_provenance.try_record_resume(
        str(path), source=_job_source(machine), adapter=machine, made_by="decorate_during_print",
    )
    assert record is not None, why
    return machine.upload_file(str(path)).file_name


def _foreign(machine: _Machine, tmp_path: Path, name: str) -> str:
    """A file Kiln did not make, uploaded to the same printer."""
    path = _file(tmp_path, name, b"G28\nG1 Z0 F9000 ; anything at all")
    return machine.upload_file(str(path)).file_name


#: Every way a start claims to continue a paused print.
ATTACKS = [
    pytest.param("evil_resume_1.3mf", {}, id="resume-name"),
    pytest.param("transformed_resume_zz.3mf", {}, id="engine-name"),
    pytest.param("benchy_resume_2.gcode", {}, id="raw-gcode-resume-name"),
    pytest.param("benchy.3mf", {"resume_from_paused": True}, id="resume-flag"),
]


def _dialog(monkeypatch, file_name: str, extra: dict) -> list[str]:
    asked: list[str] = []
    monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: True)

    async def _ask(ctx, message, **_kw):
        asked.append(message)
        from kiln.print_consent import DialogAnswer

        return DialogAnswer("decline", "")

    monkeypatch.setattr(server, "ask_user_to_confirm", _ask)

    async def _run():
        return await server._obtain_print_consent(
            "start_print", {"file_name": file_name, "printer_name": "garage", **extra}, SimpleNamespace(),
        )

    with contextlib.suppress(RuntimeError):  # the person said no: the print never starts
        asyncio.run(_run())
    return asked


# ---------------------------------------------------------------------------
# The attack: pause, then start a different file as a "resume"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("file_name", "extra"), ATTACKS)
def test_a_paused_printer_does_not_make_any_file_a_resume(machine, monkeypatch, tmp_path, file_name, extra):
    """The printer is paused mid-job and a Kiln-made resume exists for that
    job, yet a different file claiming to be a resume needs the person's
    yes, a preview and the print-time safety gate, and is refused by the
    adapter's sign-off backstop."""
    if resume_provenance is not None:
        _kiln_made(machine, tmp_path)
    name = _foreign(machine, tmp_path, file_name)

    assert print_signoff.verified_resume(name, extra, printer_name="garage") is False
    # The yes
    assert len(_dialog(monkeypatch, name, extra)) == 1
    # The preview
    block = server._preview_gate_error("start_print", name, None, printer_name="garage", is_resume=True)
    assert block is not None and block["success"] is False
    assert print_signoff.exemption_verdict("start_print", name, "garage", is_resume=True) is None
    # The safety gate
    from kiln.printers import print_gate

    judged = {"reason": "judged as a new print"}
    monkeypatch.setattr(print_gate, "_concurrent_fleet_verdict", lambda _adapter: judged)
    assert print_gate.run_adapter_gate(machine, name, dict(extra)) is judged
    # The backstop on the printer itself
    machine._kiln_signoff_required = True
    assert print_signoff.adapter_verdict(machine, name, dict(extra)) is not None


@pytest.mark.parametrize(("file_name", "extra"), ATTACKS)
def test_start_print_treats_the_claim_as_a_new_print(machine, monkeypatch, tmp_path, file_name, extra):
    name = _foreign(machine, tmp_path, file_name)
    seen: dict = {}

    def _gate(tool, file_name, token, *, printer_name=None, is_resume=False):
        seen["is_resume"] = is_resume
        return {"success": False, "error": "stopped here"}

    monkeypatch.setattr(server, "_preview_gate_error", _gate)
    monkeypatch.setattr(server, "_check_confirmation", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_check_rate_limit", lambda *_a, **_k: None)
    server.start_print(file_name=name, printer_name="garage", **extra)
    assert seen == {"is_resume": False}


def test_a_refused_claim_on_a_paused_printer_is_reported(machine, monkeypatch, tmp_path):
    audited: list[tuple] = []
    published: list = []
    monkeypatch.setattr(server, "_audit", lambda tool, action, details=None: audited.append((tool, action, details)))

    class _Bus:
        def publish(self, event):
            published.append(event)

    monkeypatch.setattr(server, "_event_bus", _Bus())
    name = _foreign(machine, tmp_path, "evil_resume_1.3mf")
    for _ in range(3):  # one start asks at several gates; one report
        assert print_signoff.verified_resume(name, {}, printer_name="garage") is False
    assert [a[1] for a in audited] == ["resume_claim_refused"]
    assert audited[0][2]["file"] == name and audited[0][2]["printer"] == "garage"
    assert len(published) == 1
    assert published[0].data["kind"] == "resume_claim_refused"


# ---------------------------------------------------------------------------
# A resume Kiln made still continues, with nothing more asked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("extra", [{}, {"resume_from_paused": True}], ids=["by-name", "with-flag"])
def test_a_resume_kiln_made_for_the_paused_job_continues(machine, monkeypatch, tmp_path, extra):
    name = _kiln_made(machine, tmp_path)

    assert print_signoff.verified_resume(name, extra, printer_name="garage") is True
    assert _dialog(monkeypatch, name, extra) == []
    assert server._preview_gate_error("start_print", name, None, printer_name="garage", is_resume=True) is None
    from kiln.printers import print_gate

    assert print_gate.run_adapter_gate(machine, name, dict(extra)) is None
    machine._kiln_signoff_required = True
    assert print_signoff.adapter_verdict(machine, name, dict(extra)) is None


def test_start_print_continues_a_resume_kiln_made(machine, monkeypatch, tmp_path):
    name = _kiln_made(machine, tmp_path)
    seen: dict = {}

    def _gate(tool, file_name, token, *, printer_name=None, is_resume=False):
        seen["is_resume"] = is_resume
        return {"success": False, "error": "stopped here"}

    monkeypatch.setattr(server, "_preview_gate_error", _gate)
    monkeypatch.setattr(server, "_check_confirmation", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_check_rate_limit", lambda *_a, **_k: None)
    server.start_print(file_name=name, printer_name="garage", resume_from_paused=True)
    assert seen == {"is_resume": True}


def test_a_local_copy_kiln_did_not_upload_is_not_the_resume(machine, tmp_path):
    """The printer starts what is saved under a name on it; a file on this
    computer that hashes the same is not that."""
    path = _file(tmp_path, "transformed_resume_ab12.3mf", b"resume bytes")
    resume_provenance.try_record_resume(
        str(path), source=_job_source(machine), adapter=machine, made_by="get_print_before_edit",
    )[0]
    assert print_signoff.verified_resume(str(path), {}, adapter=machine) is False
    machine.upload_file(str(path))
    assert print_signoff.verified_resume(str(path), {}, adapter=machine) is True


def test_a_printer_that_reports_no_job_gets_no_record(machine, tmp_path):
    source = _job_source(machine)
    machine.job = None
    path = _file(tmp_path, "transformed_resume_ab12.3mf", b"resume bytes")
    assert resume_provenance.try_record_resume(
        str(path), source=source, adapter=machine, made_by="decorate_during_print",
    )[0] is None


def test_a_resume_needs_the_printer_to_report_its_job_at_the_start(machine, tmp_path):
    name = _kiln_made(machine, tmp_path)
    machine.job = None
    assert print_signoff.verified_resume(name, {}, printer_name="garage") is False


def test_a_file_changed_while_it_was_checked_is_not_tied_to_its_name(machine, monkeypatch, tmp_path):
    path = _file(tmp_path, "transformed_resume_ab12.3mf", b"resume bytes")
    resume_provenance.try_record_resume(
        str(path), source=_job_source(machine), adapter=machine, made_by="decorate_during_print",
    )[0]

    def _swap(_adapter, p):
        Path(p).write_bytes(b"swapped after the hash")
        return p

    monkeypatch.setattr(printers_base, "_preflight_upload_or_raise", _swap)
    machine.upload_file(str(path))
    assert print_signoff.verified_resume(path.name, {}, printer_name="garage") is False


# ---------------------------------------------------------------------------
# What unties a record from a start
# ---------------------------------------------------------------------------


def test_a_file_saved_over_the_resume_name_is_not_the_resume(machine, tmp_path):
    name = _kiln_made(machine, tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    _foreign(machine, other, name)  # same printer-side name, other bytes
    assert print_signoff.verified_resume(name, {}, printer_name="garage") is False


def test_a_resume_made_for_another_job_does_not_continue_this_one(machine, tmp_path):
    name = _kiln_made(machine, tmp_path)
    machine.job = "someone_elses_job.gcode"
    assert print_signoff.verified_resume(name, {}, printer_name="garage") is False


def test_a_resume_made_long_ago_is_asked_about(machine, tmp_path):
    name = _kiln_made(machine, tmp_path)
    for record in resume_provenance._records:
        record.made_at = time.time() - resume_provenance.RECENT_SECONDS - 1
    assert print_signoff.verified_resume(name, {}, printer_name="garage") is False


def test_a_resume_needs_the_printer_paused(machine, tmp_path):
    name = _kiln_made(machine, tmp_path)
    for status in (PrinterStatus.IDLE, PrinterStatus.PRINTING, PrinterStatus.OFFLINE):
        machine.status = status
        assert print_signoff.verified_resume(name, {}, printer_name="garage") is False


def test_a_resume_made_for_one_printer_does_not_continue_another(machine, monkeypatch, tmp_path):
    name = _kiln_made(machine, tmp_path)
    other = _Machine()
    other.files[name] = machine.files[name]  # the same bytes put there some other way
    assert print_signoff.verified_resume(name, {}, adapter=other) is False


def test_a_resume_clearance_alone_clears_nothing_at_the_backstop(machine, tmp_path):
    """A clearance marked "resume" granted by a door directly stands in for
    nothing: only the check does."""
    name = _foreign(machine, tmp_path, "transformed_resume_zz.3mf")
    machine._kiln_signoff_required = True
    print_signoff.grant("plan_fleet_material_change", name, "garage", source=print_signoff.SOURCE_RESUME)
    assert print_signoff.adapter_verdict(machine, name, {}) is not None


def test_nothing_is_recorded_on_the_shared_server(machine, monkeypatch, tmp_path):
    import kiln.runtime_env as runtime_env

    monkeypatch.setattr(runtime_env, "is_hosted_multitenant", lambda: True)
    path = _file(tmp_path, "transformed_resume_ab12.3mf", b"resume")
    assert resume_provenance.try_record_resume(
        str(path), source=_job_source(machine), adapter=machine, made_by="decorate_during_print",
    )[0] is None


# ---------------------------------------------------------------------------
# Files Kiln's servers hand over
# ---------------------------------------------------------------------------


def test_a_served_resume_started_through_the_actions_is_recorded(machine, monkeypatch, tmp_path):
    from kiln import served_makes, served_steps

    monkeypatch.setattr(served_makes, "files_dir", lambda: tmp_path)
    handed = _file(tmp_path, "resume-ab12cd.3mf", b"served resume bytes")
    stated = resume_provenance.file_sha256(_job_source(machine))
    actions = [
        {"kind": served_steps.ACT_KIND, "id": "upload", "tool": "upload_file",
         "args": {"file_path": str(handed), "printer_name": "garage"},
         resume_provenance.SOURCE_KEY: stated},
        {"kind": served_steps.ACT_KIND, "id": "start", "tool": "start_print", "needs": ["upload"],
         "args": {"file_name": {"from_step": "upload", "field": "file_name"},
                  "printer_name": "garage", "resume_from_paused": True}},
    ]
    served_steps._record_served_resumes("decorate_during_print", actions)
    assert machine.upload_file(str(handed)).file_name
    assert print_signoff.verified_resume(handed.name, {"resume_from_paused": True}, printer_name="garage") is True


def test_a_tool_that_makes_no_resume_records_nothing(machine, monkeypatch, tmp_path):
    from kiln import served_makes, served_steps

    monkeypatch.setattr(served_makes, "files_dir", lambda: tmp_path)
    handed = _file(tmp_path, "resume-ab12cd.3mf", b"caller's own bytes, rewrapped")
    stated = resume_provenance.file_sha256(_job_source(machine))
    actions = [
        {"kind": served_steps.ACT_KIND, "id": "upload", "tool": "upload_file", "args": {"file_path": str(handed)},
         resume_provenance.SOURCE_KEY: stated},
        {"kind": served_steps.ACT_KIND, "id": "start", "tool": "start_print",
         "args": {"file_name": {"from_step": "upload", "field": "file_name"}, "resume_from_paused": True}},
    ]
    served_steps._record_served_resumes("wrap_gcode_as_resume_3mf", actions)
    assert resume_provenance._records == []


def test_a_resume_handed_over_in_an_answer_is_recorded_for_its_printer(machine, tmp_path):
    handed = _file(tmp_path, "transformed_resume-ab12cd.3mf", b"served resume bytes")
    answer = {
        "files": [{"on_this_computer": True, "path": str(handed), "at": ["transformed_resume_path"]}],
        resume_provenance.SOURCE_KEY: resume_provenance.file_sha256(_job_source(machine)),
    }
    assert resume_provenance.record_handed_resumes("wrap_gcode_as_resume_3mf", answer, printer_name="garage") == []
    assert resume_provenance._records == []
    assert resume_provenance.record_handed_resumes("decorate_during_print", answer, printer_name="garage") == []
    assert [r.made_by for r in resume_provenance._records] == ["decorate_during_print"]
