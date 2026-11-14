"""A resume is Kiln's only when it was built from Kiln's own copy of the print
the printer is holding, and it is started as one only while the printer still
holds the bytes Kiln sent.

Each resume maker names the G-code it built the resume from, and the record
takes no source but the toolpath Kiln kept when it started the job.  An agent
that pauses a print and has a maker build a "resume" from G-code of its
choosing gets a file that is a new print: the person is asked, it is
previewed, the safety gate runs, and the maker's answer says so.  A file that
changed on the printer after Kiln sent it (read back where the printer can
hand a file back), a deleted file, and a raw command that writes or deletes a
file there leave nothing that is a resume.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiln import monitor_twin, print_signoff, resume_provenance, server
from kiln.printers import base as printers_base
from kiln.printers.base import (
    JobProgress,
    PrinterAdapter,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)
from tests._kiln_resume import reset_resume_records

JOB = "vase.gcode.3mf"
TOOLPATH = b"G28\nG1 Z0.2 F600\n; layer 40 of the vase\n"
OTHER = b"G28\nG1 Z0 F9000\n; not the vase\n"


class _Machine(PrinterAdapter):
    """A printer that keeps what it was sent, under the name it was sent as."""

    name = "kilntest"

    def __init__(self) -> None:
        self.status = PrinterStatus.PAUSED
        self.job = JOB
        self.files: dict[str, bytes] = {}
        self.sent: list[str] = []

    def get_state(self) -> PrinterState:
        return PrinterState(state=self.status, connected=True)

    def get_job(self) -> JobProgress:
        return JobProgress(file_name=self.job)

    def upload_file(self, file_path: str) -> UploadResult:
        name = os.path.basename(file_path)
        self.files[name] = Path(file_path).read_bytes()
        return UploadResult(success=True, file_name=name, message="ok")

    def delete_file(self, file_path: str) -> bool:
        return self.files.pop(os.path.basename(file_path), None) is not None

    def send_gcode(self, commands: list[str]) -> bool:
        self.sent.extend(commands)
        return True

    def _start_print_impl(self, file_name: str, **kwargs) -> PrintResult:
        return PrintResult(success=True, message="started")


class _ReadsBack(_Machine):
    """A printer that can hand a file back (as a Bambu does over FTPS)."""

    def read_print_file(self, file_name: str) -> bytes | None:
        return self.files.get(os.path.basename(file_name))


_Machine.__abstractmethods__ = frozenset()
_ReadsBack.__abstractmethods__ = frozenset()


def _machine_fixture(monkeypatch, tmp_path, printer: _Machine) -> _Machine:
    printer._kiln_registered_name = "garage"
    monkeypatch.setattr(server, "_resolve_adapter", lambda *_a, **_k: printer)
    # The upload's own file checks are not what is under test here.
    monkeypatch.setattr(printers_base, "_preflight_upload_or_raise", lambda _a, path: path)
    monkeypatch.setattr(printers_base, "_incomplete_upload_reason", lambda _a, _path: None)
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", tmp_path / "twin")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", tmp_path / "twin" / "active.json")
    monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
    reset_resume_records()
    print_signoff.clear()
    return printer


@pytest.fixture()
def machine(monkeypatch, tmp_path):
    yield _machine_fixture(monkeypatch, tmp_path, _Machine())
    reset_resume_records()
    print_signoff.clear()


@pytest.fixture()
def reads_back(monkeypatch, tmp_path):
    yield _machine_fixture(monkeypatch, tmp_path, _ReadsBack())
    reset_resume_records()
    print_signoff.clear()


def _file(tmp_path: Path, name: str, body: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(body)
    return str(path)


def _kiln_started(machine: _Machine, tmp_path: Path, *, sliced_here: bool = True, machine_id: str | None = None) -> str:
    """Kiln's own record of the print *machine* is running, as the print
    start writes it: the job, the machine, the toolpath kept and its hash."""
    from kiln.registry import machine_fingerprint

    kept = _file(tmp_path, "kept-current.gcode", TOOLPATH)
    (tmp_path / "twin").mkdir(exist_ok=True)
    monitor_twin._write_json(monitor_twin._ACTIVE_FILE, {
        "garage": {
            "file_name": machine.job,
            "printer_name": "garage",
            "machine": machine_id if machine_id is not None else machine_fingerprint(machine),
            "started_at": "2026-01-01T00:00:00+00:00",
            "gcode": kept if sliced_here else None,
            "gcode_sha256": hashlib.sha256(TOOLPATH).hexdigest() if sliced_here else None,
            "mesh": None,
        },
    })
    return kept


def _maker_builds(machine: _Machine, path: str, source: str) -> tuple:
    """What a resume maker does with the file it wrote: offer it to the
    record, naming the G-code it built it from.  (A build whose record takes
    no source records whatever it is handed.)"""
    try_record = getattr(resume_provenance, "try_record_resume", None)
    if try_record is None:
        return resume_provenance.record_made_resume(path, adapter=machine, made_by="decorate_during_print"), ""
    return try_record(path, source=source, adapter=machine, made_by="decorate_during_print")


def _dialog(monkeypatch, file_name: str) -> list[str]:
    asked: list[str] = []
    monkeypatch.setattr(server, "host_can_ask_the_user", lambda mcp, ctx: True)

    async def _ask(ctx, message, **_kw):
        asked.append(message)
        from kiln.print_consent import DialogAnswer

        return DialogAnswer("decline", "")

    monkeypatch.setattr(server, "ask_user_to_confirm", _ask)

    async def _run():
        return await server._obtain_print_consent(
            "start_print", {"file_name": file_name, "printer_name": "garage"}, SimpleNamespace(),
        )

    with contextlib.suppress(RuntimeError):  # the person said no: the print never starts
        asyncio.run(_run())
    return asked


def _is_a_new_print(machine: _Machine, monkeypatch, name: str) -> None:
    """Every gate judges *name* as a new print: the yes, the preview, the
    safety gate and the backstop on the printer itself."""
    assert print_signoff.verified_resume(name, {"resume_from_paused": True}, printer_name="garage") is False
    assert len(_dialog(monkeypatch, name)) == 1
    block = server._preview_gate_error("start_print", name, None, printer_name="garage", is_resume=True)
    assert block is not None and block["success"] is False
    from kiln.printers import print_gate

    judged = {"reason": "judged as a new print"}
    monkeypatch.setattr(print_gate, "_concurrent_fleet_verdict", lambda _adapter: judged)
    assert print_gate.run_adapter_gate(machine, name, {"resume_from_paused": True}) is judged
    machine._kiln_signoff_required = True
    assert print_signoff.adapter_verdict(machine, name, {}) is not None


# ---------------------------------------------------------------------------
# A resume is built from Kiln's own copy of the print, or it is not a resume
# ---------------------------------------------------------------------------


def test_a_resume_built_from_the_callers_gcode_is_a_new_print(machine, monkeypatch, tmp_path):
    """The attack: the print is paused, Kiln holds its own copy of the job,
    and a maker is handed other G-code.  What it builds is not recorded, and
    starting it needs the yes, the preview and the safety gate."""
    _kiln_started(machine, tmp_path)
    theirs = _file(tmp_path, "theirs.gcode", OTHER)
    built = _file(tmp_path, "transformed_resume_ab12.3mf", b"a resume built from their G-code")
    record, why = _maker_builds(machine, built, theirs)
    assert record is None
    assert "not Kiln's own copy" in why
    name = machine.upload_file(built).file_name
    _is_a_new_print(machine, monkeypatch, name)


def test_a_resume_built_from_kilns_own_copy_continues(machine, monkeypatch, tmp_path):
    kept = _kiln_started(machine, tmp_path)
    built = _file(tmp_path, "transformed_resume_ab12.3mf", b"a resume built from Kiln's copy")
    record, why = _maker_builds(machine, built, kept)
    assert record is not None, why
    name = machine.upload_file(built).file_name
    assert print_signoff.verified_resume(name, {}, printer_name="garage") is True
    assert _dialog(monkeypatch, name) == []


def test_the_same_bytes_from_anywhere_are_kilns_copy(machine, tmp_path):
    """The source is judged by its bytes: a copy of Kiln's toolpath is it."""
    _kiln_started(machine, tmp_path)
    same = _file(tmp_path, "the_slice_output.gcode", TOOLPATH)
    built = _file(tmp_path, "transformed_resume_ab12.3mf", b"resume")
    assert _maker_builds(machine, built, same)[0] is not None


def test_a_maker_that_names_no_source_records_nothing(machine, tmp_path):
    _kiln_started(machine, tmp_path)
    built = _file(tmp_path, "transformed_resume_ab12.3mf", b"resume")
    record, why = resume_provenance.try_record_resume(built, adapter=machine, made_by="decorate_during_print")
    assert record is None and "did not say which G-code" in why


def test_no_copy_of_the_print_means_no_resume(machine, tmp_path):
    built = _file(tmp_path, "transformed_resume_ab12.3mf", b"resume")
    theirs = _file(tmp_path, "theirs.gcode", TOOLPATH)
    record, why = _maker_builds(machine, built, theirs)
    assert record is None and "did not start the print" in why
    _kiln_started(machine, tmp_path, sliced_here=False)
    record, why = _maker_builds(machine, built, theirs)
    assert record is None and "did not slice here" in why


def test_kilns_copy_of_another_machines_print_is_not_this_ones(machine, tmp_path):
    kept = _kiln_started(machine, tmp_path, machine_id="kilntest:serial:another")
    built = _file(tmp_path, "transformed_resume_ab12.3mf", b"resume")
    record, why = _maker_builds(machine, built, kept)
    assert record is None and "did not start the print" in why


def test_kilns_copy_of_another_job_is_not_this_ones(machine, tmp_path):
    kept = _kiln_started(machine, tmp_path)
    machine.job = "another_job.gcode.3mf"
    built = _file(tmp_path, "transformed_resume_ab12.3mf", b"resume")
    assert _maker_builds(machine, built, kept)[0] is None


@pytest.fixture()
def twin(monkeypatch, tmp_path):
    monkeypatch.setattr(monitor_twin, "_TWIN_DIR", tmp_path / "twin")
    monkeypatch.setattr(monitor_twin, "_ACTIVE_FILE", tmp_path / "twin" / "active.json")
    monkeypatch.setattr(monitor_twin, "_SLICES_FILE", tmp_path / "twin" / "slices.json")
    return tmp_path


def test_the_print_start_keeps_kilns_copy_and_its_hash(twin):
    """The record a resume is judged against is written where every print
    starts, with the machine and the hash of the toolpath kept, when what
    Kiln sent under the job's name is that slice."""
    gcode = _file(twin, "vase.gcode", TOOLPATH)
    monitor_twin.note_sliced(_file(twin, "vase.stl", b"solid vase"), gcode)
    monitor_twin.note_print_started(
        "bambu", "vase.gcode", machine="bambu:serial:01p", sent_sha256=hashlib.sha256(TOOLPATH).hexdigest(),
    )
    [record] = monitor_twin.active_records()
    assert record["machine"] == "bambu:serial:01p"
    assert record["gcode_sha256"] == hashlib.sha256(TOOLPATH).hexdigest()


@pytest.mark.parametrize("sent", [None, hashlib.sha256(OTHER).hexdigest()], ids=["nothing-known", "other-bytes"])
def test_a_job_whose_bytes_are_not_the_slice_keeps_no_hash(twin, sent):
    """The printer prints what was sent under its name.  A slice that shares
    the name but not the bytes (or bytes Kiln never saw) is not that job, so
    nothing is resumed from it."""
    gcode = _file(twin, "vase.gcode", TOOLPATH)
    monitor_twin.note_sliced(_file(twin, "vase.stl", b"solid vase"), gcode)
    monitor_twin.note_print_started("bambu", "vase.gcode", machine="bambu:serial:01p", sent_sha256=sent)
    [record] = monitor_twin.active_records()
    assert record["gcode"] and record["gcode_sha256"] is None


def test_a_wrapped_job_is_judged_by_the_wrap_it_sent(twin):
    gcode = _file(twin, "vase.gcode", TOOLPATH)
    monitor_twin.note_sliced(_file(twin, "vase.stl", b"solid vase"), gcode)
    wrap = _file(twin, "vase.gcode.3mf", b"PK the wrap of the vase")
    monitor_twin.note_wrapped(gcode, wrap)
    sent = hashlib.sha256(b"PK the wrap of the vase").hexdigest()
    monitor_twin.note_print_started("bambu", "vase.gcode.3mf", machine="bambu:serial:01p", sent_sha256=sent)
    assert monitor_twin.active_records()[0]["gcode_sha256"] == hashlib.sha256(TOOLPATH).hexdigest()
    # A slice newer than its wrap is a re-slice the wrap never held.
    os.utime(wrap, (1, 1))
    monitor_twin.note_print_started("bambu", "vase.gcode.3mf", machine="bambu:serial:01p", sent_sha256=sent)
    assert monitor_twin.active_records()[0]["gcode_sha256"] is None


@pytest.mark.parametrize(("sent", "kept"), [(TOOLPATH, True), (OTHER, False)], ids=["the-slice", "other-bytes"])
def test_a_print_kiln_starts_is_recorded_by_what_it_sent(machine, twin, monkeypatch, sent, kept):
    """Through the printer's own upload and start: Kiln slices the vase, and
    what is uploaded under the slice's name is the slice, or other bytes.
    Only the slice gives the print a record a resume can be built from."""
    from kiln.printers import print_gate

    monkeypatch.setattr(print_gate, "run_adapter_gate", lambda *_a, **_k: None)
    machine.status = PrinterStatus.IDLE
    gcode = _file(twin, "vase.gcode", TOOLPATH)
    monitor_twin.note_sliced(_file(twin, "vase.stl", b"solid vase"), gcode)
    elsewhere = twin / "elsewhere"
    elsewhere.mkdir()
    machine.upload_file(_file(elsewhere, "vase.gcode", sent))
    assert machine.start_print("vase.gcode").success
    [record] = monitor_twin.active_records()
    assert record["gcode_sha256"] == (hashlib.sha256(TOOLPATH).hexdigest() if kept else None)


def test_every_upload_records_the_bytes_sent_under_its_name(machine, tmp_path):
    first = _file(tmp_path, "vase.gcode", TOOLPATH)
    machine.upload_file(first)
    assert resume_provenance.sent_sha256(machine, "vase.gcode") == hashlib.sha256(TOOLPATH).hexdigest()
    other = tmp_path / "other"
    other.mkdir()
    machine.upload_file(_file(other, "vase.gcode", OTHER))
    assert resume_provenance.sent_sha256(machine, "vase.gcode") == hashlib.sha256(OTHER).hexdigest()
    machine.delete_file("vase.gcode")
    assert resume_provenance.sent_sha256(machine, "vase.gcode") is None


# ---------------------------------------------------------------------------
# Resumes Kiln's servers build
# ---------------------------------------------------------------------------


def test_a_served_resume_started_through_the_actions_names_its_source(machine, monkeypatch, tmp_path):
    from kiln import served_makes, served_steps

    monkeypatch.setattr(served_makes, "files_dir", lambda: tmp_path)
    _kiln_started(machine, tmp_path)

    def _actions(handed: str, stated: str) -> list:
        return [
            {"kind": served_steps.ACT_KIND, "id": "upload", "tool": "upload_file",
             "args": {"file_path": handed, "printer_name": "garage"}, resume_provenance.SOURCE_KEY: stated},
            {"kind": served_steps.ACT_KIND, "id": "start", "tool": "start_print", "needs": ["upload"],
             "args": {"file_name": {"from_step": "upload", "field": "file_name"},
                      "printer_name": "garage", "resume_from_paused": True}},
        ]

    theirs = _file(tmp_path, "resume-theirs.3mf", b"built from their G-code")
    served_steps._record_served_resumes("resume_interrupted_print", _actions(theirs, hashlib.sha256(OTHER).hexdigest()))
    [note] = served_steps.resume_notes()
    assert "is not a resume of the print on garage" in note and "the person is asked" in note
    assert served_steps.resume_notes() == []
    _is_a_new_print(machine, monkeypatch, machine.upload_file(theirs).file_name)

    ours = _file(tmp_path, "resume-ours.3mf", b"built from Kiln's copy")
    served_steps._record_served_resumes("resume_interrupted_print", _actions(ours, hashlib.sha256(TOOLPATH).hexdigest()))
    assert served_steps.resume_notes() == []
    assert print_signoff.verified_resume(machine.upload_file(ours).file_name, {}, printer_name="garage") is True


def test_a_handed_resume_is_judged_by_the_source_stated_nearest_it(machine, tmp_path):
    _kiln_started(machine, tmp_path)
    ours = hashlib.sha256(TOOLPATH).hexdigest()
    handed = _file(tmp_path, "transformed_resume-ab12cd.3mf", b"served resume")
    answer = {
        resume_provenance.SOURCE_KEY: hashlib.sha256(OTHER).hexdigest(),
        "prepared_sessions": {"garage": {resume_provenance.SOURCE_KEY: ours, "transformed_resume_path": handed}},
        "files": [{"on_this_computer": True, "path": handed,
                   "at": ["prepared_sessions", "garage", "transformed_resume_path"]}],
    }
    assert resume_provenance.record_handed_resumes("decorate_during_print_fleet", answer, printer_name="garage") == []
    assert len(resume_provenance._records) == 1
    reset_resume_records()
    del answer["prepared_sessions"]["garage"][resume_provenance.SOURCE_KEY]
    [note] = resume_provenance.record_handed_resumes("decorate_during_print_fleet", answer, printer_name="garage")
    assert "not Kiln's own copy" in note and resume_provenance._records == []


def test_the_answer_says_plainly_when_a_handed_file_is_not_a_resume(machine, monkeypatch, tmp_path):
    from kiln import served_makes

    _kiln_started(machine, tmp_path)
    handed = _file(tmp_path, "transformed_resume-ab12cd.3mf", b"served resume")

    def _bring(answer, _trip):
        answer["files"] = [{"on_this_computer": True, "path": handed, "at": ["transformed_resume_path"]}]

    monkeypatch.setattr(served_makes, "_bring_files", _bring)
    answer = served_makes.arrive(
        "decorate_during_print",
        {"success": True, "transformed_resume_path": handed, resume_provenance.SOURCE_KEY: "0" * 64},
        printer_name="garage",
    )
    [note] = answer["resume_not_recorded"]
    assert "transformed_resume-ab12cd.3mf is not a resume" in note


# ---------------------------------------------------------------------------
# A resume Kiln rewrites keeps its source; nothing else gains one
# ---------------------------------------------------------------------------


def test_a_resume_rewritten_from_a_recorded_one_is_recorded(machine, tmp_path):
    kept = _kiln_started(machine, tmp_path)
    first = _file(tmp_path, "transformed_resume_ab12.3mf", b"resume")
    assert _maker_builds(machine, first, kept)[0] is not None
    pictured = _file(tmp_path, "transformed_resume_ab12_thumbs.3mf", b"resume with a new picture")
    record, why = resume_provenance.record_derived_resume(pictured, from_path=first, adapter=machine, made_by="preview")
    assert record is not None, why
    stranger = _file(tmp_path, "theirs.3mf", b"not a resume Kiln made")
    record, why = resume_provenance.record_derived_resume(
        _file(tmp_path, "theirs_thumbs.3mf", b"x"), from_path=stranger, adapter=machine, made_by="preview",
    )
    assert record is None and "not a resume Kiln made" in why


# ---------------------------------------------------------------------------
# The printer still holds the bytes Kiln sent
# ---------------------------------------------------------------------------


def _recorded_and_uploaded(machine: _Machine, tmp_path: Path) -> str:
    kept = _kiln_started(machine, tmp_path)
    built = _file(tmp_path, "transformed_resume_ab12.3mf", b"a resume built from Kiln's copy")
    record, why = _maker_builds(machine, built, kept)
    assert record is not None, why
    return machine.upload_file(built).file_name


def test_a_printer_that_hands_files_back_must_still_hold_the_bytes(reads_back, monkeypatch, tmp_path):
    name = _recorded_and_uploaded(reads_back, tmp_path)
    assert print_signoff.verified_resume(name, {}, printer_name="garage") is True
    reads_back.files[name] = b"written on the printer some other way"
    getattr(resume_provenance, "_read_back", {}).clear()  # a new start reads the printer again
    _is_a_new_print(reads_back, monkeypatch, name)


def test_a_printer_that_cannot_hand_the_file_back_is_not_a_resume(reads_back, monkeypatch, tmp_path):
    name = _recorded_and_uploaded(reads_back, tmp_path)

    def _fails(_file_name):
        raise OSError("no answer")

    monkeypatch.setattr(reads_back, "read_print_file", _fails)
    assert print_signoff.verified_resume(name, {}, printer_name="garage") is False


def test_a_deleted_resume_file_is_not_a_resume(machine, tmp_path):
    name = _recorded_and_uploaded(machine, tmp_path)
    machine.delete_file(name)
    machine.files[name] = b"put back some other way"
    assert print_signoff.verified_resume(name, {}, printer_name="garage") is False


@pytest.mark.parametrize("command", [
    "M28 transformed_resume_ab12.3mf",
    "m30 transformed_resume_ab12.3mf",
    "M29",
    "N10 M28 transformed_resume_ab12.3mf*71",
    "G28 M28 transformed_resume_ab12.3mf",
    "M928 transformed_resume_ab12.3mf",
    'M471 S"0:/gcodes/a.gcode" T"0:/gcodes/transformed_resume_ab12.3mf"',
    'M559 P"config.g"',
    'M560 P"index.html"',
])
def test_a_raw_command_that_writes_a_file_unties_every_name(machine, tmp_path, command):
    name = _recorded_and_uploaded(machine, tmp_path)
    machine.send_gcode(["G28", command])
    assert print_signoff.verified_resume(name, {}, printer_name="garage") is False


@pytest.mark.parametrize("command", [
    "G28",
    "M117 resuming ; M28 in a comment",
    "M117 resuming M28 is only text here",
    "G1 X10 Y10 F3000",
])
def test_an_ordinary_raw_command_leaves_the_resume(machine, tmp_path, command):
    name = _recorded_and_uploaded(machine, tmp_path)
    machine.send_gcode([command])
    assert print_signoff.verified_resume(name, {}, printer_name="garage") is True
