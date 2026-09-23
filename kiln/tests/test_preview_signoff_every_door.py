"""The sign-off preview is the stage first, the link second, PNG last —
and every door that starts a print says which one it used.

Measured 2026-09-19, live: a sliced multicolour jar was previewed with
``visualize_model`` (PNG renders) because that was the tool the agent had
loaded.  ``issue_preview_token`` accepted the file.  The inline 3D stage
never opened and no viewer link was issued.  The gate only recorded that
a render occurred; the KIND of render was not part of the token, so a
PNG-only preview was indistinguishable from a stage preview.

And on 2026-09-16 a print started unseen through ``kiln print``: the CLI
start paths called the adapter directly with no gate at all.

Two halves are pinned here.  The token records which door produced the
preview and verifies it against evidence the server wrote itself — a
panel fetch, a link issue, a render — never the caller's word.  And one
clearance, granted by the one gate every door calls, is what the adapter
template checks before any bytes reach a machine.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import struct
import time
from unittest.mock import MagicMock, patch

import pytest

from kiln import consent_windows, preview_evidence, print_consent, print_signoff, server
from kiln.preview_gate import PreviewGate, get_preview_gate
from kiln.print_consent import PrintConsent, reset_consent, set_consent
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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _stl(path: pathlib.Path, seed: int = 1) -> str:
    header = b"\x00" * 80
    tri = struct.pack("<fff", 0, 0, 1)
    tri += struct.pack("<fff", 0, 0, 0)
    tri += struct.pack("<fff", 10 + seed, 0, 0)
    tri += struct.pack("<fff", 0, 10, 0)
    tri += b"\x00\x00"
    path.write_bytes(header + struct.pack("<I", 1) + tri)
    return str(path)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Fresh evidence ledger, fresh gate, no bypass, no clearance."""
    monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KILN_SKIP_PREVIEW_GATE", raising=False)
    monkeypatch.delenv("KILN_NO_LOCAL_STAGE", raising=False)
    preview_evidence._reset_for_tests()
    print_signoff._reset_for_tests()
    import kiln.preview_gate as pg

    monkeypatch.setattr(pg, "_gate", PreviewGate())
    yield
    preview_evidence._reset_for_tests()
    print_signoff._reset_for_tests()


class _Printer(PrinterAdapter):
    """A real adapter subclass, so ``start_print`` runs the template."""

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


# ---------------------------------------------------------------------------
# The token records the door, and the door is verified against evidence
# ---------------------------------------------------------------------------


class TestWhichDoor:
    def test_a_token_needs_a_door(self, tmp_path):
        path = _stl(tmp_path / "jar.stl")
        refusal, _ = preview_evidence.judge(path, "", host_renders=False)
        assert refusal is not None
        assert "stage" in refusal["message"] and "url" in refusal["message"]

    def test_png_is_refused_when_the_stage_was_already_served(self, tmp_path):
        """The live case: the stage is the record; renders are the floor."""
        path = _stl(tmp_path / "jar.stl")
        preview_evidence.record("stage", path, via="panel_fetch")
        preview_evidence.record("png", path, renderer="openscad")
        refusal, _ = preview_evidence.judge(path, "png", host_renders=True)
        assert refusal is not None
        assert "door='stage'" in refusal["message"]

    def test_png_is_refused_while_the_stage_is_available(self, tmp_path):
        """A host that draws the panel was skipped: the refusal names it."""
        path = _stl(tmp_path / "jar.stl")
        preview_evidence.record("png", path, renderer="openscad")
        preview_evidence.record_url_refusal(path, "signed_out")
        refusal, _ = preview_evidence.judge(path, "png", host_renders=True)
        assert refusal is not None
        assert "stage" in refusal["message"]
        assert "re-issue" in refusal["message"] or "then" in refusal["message"]

    def test_png_is_refused_when_no_link_was_attempted(self, tmp_path):
        path = _stl(tmp_path / "jar.stl")
        preview_evidence.record("png", path, renderer="openscad")
        refusal, _ = preview_evidence.judge(path, "png", host_renders=False)
        assert refusal is not None
        assert "share_link=True" in refusal["message"]

    def test_png_is_refused_while_a_link_is_live(self, tmp_path):
        path = _stl(tmp_path / "jar.stl")
        preview_evidence.record("png", path, renderer="openscad")
        preview_evidence.record(
            "url", path, viewer_url="https://kiln3d.com/view/x", expires_at=time.time() + 900,
        )
        refusal, _ = preview_evidence.judge(path, "png", host_renders=False)
        assert refusal is not None
        assert "door='url'" in refusal["message"]

    def test_png_alone_is_accepted_only_on_a_headless_host_with_no_link(self, tmp_path):
        """The one legitimate PNG-only case, and every fact in it is the
        server's own: the host declared no panel, the link door refused
        for a reason it recorded, the render happened."""
        path = _stl(tmp_path / "jar.stl")
        preview_evidence.record("png", path, renderer="stage_paint", shown_sha="abc")
        preview_evidence.record_url_refusal(path, "signed_out")
        refusal, verdict = preview_evidence.judge(path, "png", host_renders=False)
        assert refusal is None, refusal
        assert verdict["door"] == "png"
        assert "panel" in verdict["skipped"]["stage"]
        assert "signed out" in verdict["skipped"]["url"], "the refusal is a sentence, not a code"

    def test_png_without_a_render_on_record_is_refused(self, tmp_path):
        path = _stl(tmp_path / "jar.stl")
        preview_evidence.record_url_refusal(path, "signed_out")
        refusal, _ = preview_evidence.judge(path, "png", host_renders=False)
        assert refusal is not None
        assert "visualize_model" in refusal["message"]

    def test_stage_needs_a_served_payload(self, tmp_path):
        path = _stl(tmp_path / "jar.stl")
        refusal, _ = preview_evidence.judge(path, "stage", host_renders=True)
        assert refusal is not None
        assert "door='stage'" in refusal["message"]
        preview_evidence.record("stage", path, via="panel_fetch")
        refusal, verdict = preview_evidence.judge(path, "stage", host_renders=True)
        assert refusal is None
        assert verdict["door"] == "stage"

    def test_url_needs_a_live_link(self, tmp_path):
        path = _stl(tmp_path / "jar.stl")
        refusal, _ = preview_evidence.judge(path, "url", host_renders=False)
        assert refusal is not None and "share_link=True" in refusal["message"]
        preview_evidence.record(
            "url", path, viewer_url="https://kiln3d.com/view/x", expires_at=time.time() - 1,
        )
        refusal, _ = preview_evidence.judge(path, "url", host_renders=False)
        assert refusal is not None and "expired" in refusal["message"]
        preview_evidence.record(
            "url", path, viewer_url="https://kiln3d.com/view/y", expires_at=time.time() + 900,
        )
        refusal, verdict = preview_evidence.judge(path, "url", host_renders=False)
        assert refusal is None
        assert verdict["evidence"]["url"]["viewer_url"].endswith("/y")

    def test_evidence_is_bound_to_the_bytes(self, tmp_path):
        """A re-sliced file keeps its name and loses its sign-off."""
        path = _stl(tmp_path / "jar.stl", seed=1)
        preview_evidence.record("stage", path, via="panel_fetch")
        _stl(tmp_path / "jar.stl", seed=2)
        refusal, _ = preview_evidence.judge(path, "stage", host_renders=True)
        assert refusal is not None

    def test_evidence_goes_stale(self, tmp_path, monkeypatch):
        path = _stl(tmp_path / "jar.stl")
        preview_evidence.record("stage", path, via="panel_fetch")
        real_time = time.time
        monkeypatch.setattr(
            preview_evidence.time, "time",
            lambda: real_time() + preview_evidence.EVIDENCE_TTL_S + 1,
        )
        refusal, _ = preview_evidence.judge(path, "stage", host_renders=True)
        assert refusal is not None and "ago" in refusal["message"]

    def test_a_sliced_print_file_inherits_its_design_mesh_evidence(self, tmp_path, monkeypatch):
        """The stage shows the DESIGN; the printer gets the SLICE.  The
        slice ledger joins them, so a panel served for jar.stl signs off
        the jar.gcode.3mf that was sliced from it."""
        from kiln import monitor_twin

        monkeypatch.setattr(monitor_twin, "_TWIN_DIR", tmp_path / "twin")
        monkeypatch.setattr(monitor_twin, "_SLICES_FILE", tmp_path / "twin" / "slices.json")
        mesh = _stl(tmp_path / "jar.stl")
        sliced = tmp_path / "jar.gcode.3mf"
        sliced.write_bytes(b"PK sliced bytes")
        monitor_twin.note_sliced(mesh, str(sliced))
        preview_evidence.record("stage", mesh, via="panel_fetch")
        refusal, verdict = preview_evidence.judge(str(sliced), "stage", host_renders=True)
        assert refusal is None, refusal
        assert verdict["evidence"]["design_mesh"] == os.path.abspath(mesh)

    def test_the_ledger_is_shared_across_processes(self, tmp_path):
        """A desktop host routes the panel's fetch over whichever session
        it holds, so the record must be readable by a sibling server."""
        path = _stl(tmp_path / "jar.stl")
        preview_evidence.record("stage", path, via="panel_fetch")
        preview_evidence._reset_for_tests()  # forget the in-memory copy
        assert preview_evidence.evidence_for(path)["stage"] is not None


# ---------------------------------------------------------------------------
# The evidence is written by the doors themselves, never by the caller
# ---------------------------------------------------------------------------


class TestEvidenceIsWrittenByTheDoor:
    def test_the_panel_fetch_records_the_stage(self, tmp_path):
        from kiln import local_stage
        from kiln.mcp_compat import FastMCP

        local_stage._reset_for_tests()
        mesh = _stl(tmp_path / "jar.stl")
        mcp = FastMCP("t")
        assert local_stage._register_payload_verb(mcp)
        token = local_stage._mint(mesh)
        fetch = mcp._tool_manager._tools["kiln_viewer_payload"].fn
        out = fetch(token)
        assert "success" not in out or out.get("success") is not False, out
        assert preview_evidence.evidence_for(mesh)["stage"]["via"] == "panel_fetch"

    def test_a_dead_token_records_nothing(self, tmp_path):
        from kiln import local_stage
        from kiln.mcp_compat import FastMCP

        local_stage._reset_for_tests()
        mesh = _stl(tmp_path / "jar.stl")
        mcp = FastMCP("t")
        local_stage._register_payload_verb(mcp)
        mcp._tool_manager._tools["kiln_viewer_payload"].fn("no-such-token")
        assert preview_evidence.evidence_for(mesh)["stage"] is None

    def test_a_link_records_the_url_and_a_refusal_records_why(self, tmp_path, monkeypatch):
        from kiln import stage_link

        monkeypatch.delenv(stage_link._OPT_OUT_ENV, raising=False)
        stage_link._cache.clear()
        mesh = _stl(tmp_path / "jar.stl")

        # Signed out: no link, and the reason is on record.
        monkeypatch.setattr(
            "kiln.auth_session.resolve_api_bearer",
            lambda *a, **k: type("B", (), {"token": "", "state": "anon"})(),
        )
        assert stage_link.stage_link_for(mesh) is None
        assert preview_evidence.evidence_for(mesh)["url_refusal"]["reason"] == "signed_out"

        # Signed in: a link, and it is on record.
        monkeypatch.setattr(
            "kiln.auth_session.resolve_api_bearer",
            lambda *a, **k: type("B", (), {"token": "bearer", "state": "license"})(),
        )
        import httpx

        class _Resp:
            status_code = 200

            def json(self):
                return {"viewer_url": "https://kiln3d.com/view/abc", "expires_in": 1800}

        monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
        link = stage_link.stage_link_for(mesh)
        assert link and link["viewer_url"].endswith("/abc")
        ev = preview_evidence.evidence_for(mesh)
        assert ev["url"]["viewer_url"].endswith("/abc")
        assert ev["url"]["expires_at"] > time.time()

    def test_a_render_records_png(self, tmp_path):
        from kiln.model_visualizer import visualize_model

        mesh = _stl(tmp_path / "jar.stl")

        def _run(cmd, **kwargs):
            for i, arg in enumerate(cmd):
                if arg == "-o" and i + 1 < len(cmd):
                    pathlib.Path(cmd[i + 1]).write_bytes(b"png")
            m = MagicMock()
            m.returncode = 0
            return m

        with patch("kiln.model_visualizer._find_openscad", return_value="openscad"), \
             patch("subprocess.run", side_effect=_run):
            result = visualize_model(
                mesh, output_dir=str(tmp_path / "out"), share_link=False, allow_stage=False,
            )
        assert result["success"] is True, result
        assert preview_evidence.evidence_for(mesh)["png"]["renderer"] == "openscad"


# ---------------------------------------------------------------------------
# issue_preview_token: the door rides the token
# ---------------------------------------------------------------------------


class TestIssuePreviewToken:
    def test_the_tool_refuses_a_png_claim_over_a_served_stage(self, tmp_path, monkeypatch):
        path = _stl(tmp_path / "jar.stl")
        preview_evidence.record("stage", path, via="panel_fetch")
        preview_evidence.record("png", path, renderer="openscad")
        monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
        out = server.issue_preview_token(path, door="png")
        assert out["success"] is False
        assert out["error"]["code"] == "PREVIEW_DOOR_SKIPPED"
        assert "door='stage'" in out["error"]["message"]

    def test_the_token_carries_the_door_it_was_issued_for(self, tmp_path, monkeypatch):
        path = _stl(tmp_path / "jar.stl")
        preview_evidence.record("stage", path, via="panel_fetch")
        monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
        out = server.issue_preview_token(path, door="stage")
        assert out["success"] is True, out
        assert out["door"] == "stage"
        ok, reason, tok = get_preview_gate().validate_detail(out["token"], path)
        assert ok, reason
        assert tok.door == "stage"

    def test_the_gate_says_which_door_it_used(self, tmp_path, monkeypatch):
        """The token is the SAW half; the gate also needs a person's yes
        (here, an elicited one) — and then records the door the token
        carried, not the yes's word for it."""
        path = _stl(tmp_path / "jar.stl")
        preview_evidence.record("stage", path, via="panel_fetch")
        monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
        token = server.issue_preview_token(path, door="stage")["token"]
        audits: list = []
        monkeypatch.setattr(
            server, "_audit", lambda tool, action, details=None: audits.append((action, details)),
        )
        reset = set_consent(PrintConsent(tool="start_print", file_name=path, printer_name="garage"))
        try:
            assert server._preview_gate_error("start_print", path, token, printer_name="garage") is None
        finally:
            reset_consent(reset)
        assert any(a == "preview_gate_satisfied" and d.get("door") == "stage" for a, d in audits), audits
        clearance = print_signoff.current()
        assert clearance is not None and clearance.door == "stage"


# ---------------------------------------------------------------------------
# One clearance, checked where every door ends: the adapter template
# ---------------------------------------------------------------------------


class TestAdapterBackstop:
    def test_an_adapter_that_requires_signoff_refuses_an_unsigned_start(self):
        printer = _Printer()
        print_signoff.require_signoff(printer)
        result = printer.start_print("part.gcode")
        assert result.success is False
        assert "sign-off" in result.message
        assert printer.started == []

    def test_a_clearance_lets_the_same_file_start_once(self):
        printer = _Printer()
        print_signoff.require_signoff(printer)
        print_signoff.grant("start_print", "part.gcode", "garage", source="preview_token", door="stage")
        assert printer.start_print("part.gcode").success is True
        # Consumed: the second start is a second print.
        assert printer.start_print("part.gcode").success is False

    def test_a_clearance_for_another_file_does_not_open_the_door(self):
        printer = _Printer()
        print_signoff.require_signoff(printer)
        print_signoff.grant("start_print", "other.gcode", "garage", source="preview_token")
        assert printer.start_print("part.gcode").success is False

    def test_a_clearance_for_another_machine_does_not_open_the_door(self):
        printer = _Printer()
        print_signoff.require_signoff(printer)
        print_signoff.grant("start_print", "part.gcode", "workshop", source="preview_token")
        assert printer.start_print("part.gcode").success is False

    def test_a_clearance_covers_the_slice_made_from_the_signed_mesh(self, tmp_path, monkeypatch):
        from kiln import monitor_twin

        monkeypatch.setattr(monitor_twin, "_TWIN_DIR", tmp_path / "twin")
        monkeypatch.setattr(monitor_twin, "_SLICES_FILE", tmp_path / "twin" / "slices.json")
        mesh = _stl(tmp_path / "jar.stl")
        sliced = tmp_path / "jar.gcode"
        sliced.write_text("G28\n")
        monitor_twin.note_sliced(mesh, str(sliced))
        printer = _Printer()
        print_signoff.require_signoff(printer)
        print_signoff.grant("slice_and_print", mesh, "garage", source="preview_token")
        assert printer.start_print("jar.gcode").success is True

    def test_registered_adapters_require_signoff(self):
        from kiln.registry import PrinterRegistry

        printer = _Printer()
        PrinterRegistry().register("garage", printer)
        assert print_signoff.signoff_required(printer)

    def test_resume_and_ci_bypass_still_pass(self, monkeypatch):
        printer = _Printer()
        print_signoff.require_signoff(printer)
        assert printer.start_print("transformed_resume_1.3mf").success is True
        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")
        assert printer.start_print("part.gcode").success is True


# ---------------------------------------------------------------------------
# Every door
# ---------------------------------------------------------------------------


def _token_for(path: str, monkeypatch) -> str:
    preview_evidence.record("stage", path, via="panel_fetch")
    monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
    out = server.issue_preview_token(path, door="stage")
    assert out["success"], out
    return out["token"]


class TestOneShotPipelines:
    def test_run_quick_print_refuses_without_a_token(self, tmp_path, monkeypatch):
        mesh = _stl(tmp_path / "jar.stl")
        monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
        ran = MagicMock()
        monkeypatch.setattr(server, "_pipeline_quick_print", ran)
        out = server.run_quick_print(mesh)
        assert out["success"] is False
        assert out["error"]["code"] == "PREVIEW_NOT_CONFIRMED"
        ran.assert_not_called()

    def test_run_reslice_and_print_refuses_without_a_token(self, tmp_path, monkeypatch):
        mesh = _stl(tmp_path / "jar.stl")
        monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
        ran = MagicMock()
        monkeypatch.setattr(server, "_pipeline_reslice_and_print", ran)
        out = server.run_reslice_and_print(mesh, overrides={"infill": 20})
        assert out["success"] is False
        assert out["error"]["code"] == "PREVIEW_NOT_CONFIRMED"
        ran.assert_not_called()

    def test_the_sign_off_survives_a_pause_before_the_start_step(self, tmp_path, monkeypatch):
        """A paused pipeline resumes in a LATER tool call, in a different
        context.  The clearance rides the execution, not the call."""
        from kiln import pipelines

        mesh = _stl(tmp_path / "jar.stl")
        printer = _Printer()
        print_signoff.require_signoff(printer)
        monkeypatch.setattr(pipelines, "_resolve_pipeline_adapter", lambda *_a, **_k: printer)
        monkeypatch.setattr(server, "_resolve_use_ams", lambda *a, **k: {"use_ams": False, "warnings": []})
        gcode = tmp_path / "jar.gcode"
        gcode.write_text("G28\n")

        from kiln.slicer import SliceResult

        def _fake_slice(*_a, **_k):
            return SliceResult(success=True, output_path=str(gcode), message="sliced", slicer="fake")

        monkeypatch.setattr("kiln.slicer.slice_file", _fake_slice)
        record = print_signoff.record_for(
            print_signoff.grant("run_quick_print", mesh, "garage", source="preview_token", door="stage")
        )
        print_signoff.clear()
        # Steps: validate, profile, stability, slice, safety, upload,
        # preflight, start_print — pause after preflight (index 6).
        before = set(pipelines._executions)
        result = pipelines.quick_print(
            model_path=mesh, printer_name="garage", skip_validation=True,
            pause_after_step=6, signoff=record,
        )
        assert printer.started == [], result.to_dict()
        # The execution THIS call registered — not the newest in a
        # process-wide registry another test file may have added to —
        # resumed as the pipeline_resume tool would, in a fresh context.
        (new_id,) = set(pipelines._executions) - before
        ex = pipelines._executions[new_id]
        assert ex.state.value == "paused", ex.state
        resumed = ex.resume()
        assert printer.started == ["jar.gcode"], resumed.to_dict()


class TestQueueDoors:
    @pytest.fixture
    def queue_env(self, tmp_path, monkeypatch):
        from kiln.events import EventBus
        from kiln.queue import PrintQueue

        q = PrintQueue(db_path=str(tmp_path / "q.db"))
        bus = EventBus()
        monkeypatch.setattr(server, "_get_queue", lambda: q)
        monkeypatch.setattr(server, "_get_event_bus", lambda: bus)
        monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
        return q

    def test_submit_job_refuses_without_a_token(self, queue_env):
        from kiln.plugins.queue_tools import submit_job

        out = submit_job("part.gcode")
        assert out["success"] is False
        assert out["error"]["code"] == "PREVIEW_NOT_CONFIRMED"
        assert queue_env.pending_count() == 0

    def test_a_queued_job_carries_its_sign_off(self, queue_env, tmp_path, monkeypatch):
        """Token (saw) plus an elicited yes (said go): the job carries the
        door, the source and the scope the yes was given for."""
        from kiln.plugins.queue_tools import submit_job

        path = _stl(tmp_path / "part.gcode")
        token = _token_for(path, monkeypatch)
        reset = set_consent(PrintConsent(tool="submit_job", file_name="part.gcode", printer_name=None))
        try:
            out = submit_job("part.gcode", preview_token=token)
        finally:
            reset_consent(reset)
        assert out["success"] is True, out
        job = queue_env.get_job(out["job_id"])
        assert job.metadata["preview_signoff"]["door"] == "stage"
        assert job.metadata["preview_signoff"]["source"] == print_consent.SOURCE_ELICITED

    def test_the_scheduler_clears_the_queued_job_it_dispatches(self, queue_env, monkeypatch):
        from kiln.events import EventBus
        from kiln.registry import PrinterRegistry
        from kiln.scheduler import JobScheduler

        printer = _Printer()
        registry = PrinterRegistry()
        registry.register("garage", printer)  # requires sign-off from here on
        job_id = queue_env.submit(
            "part.gcode", "garage", "test",
            metadata={"preview_signoff": {"door": "stage", "source": "preview_token"}},
        )
        sched = JobScheduler(queue_env, registry, EventBus(), poll_interval=0.01)
        summary = sched.tick()
        assert [d["job_id"] for d in summary["dispatched"]] == [job_id], summary
        assert printer.started == ["part.gcode"]


class TestCliDoors:
    @pytest.fixture
    def cli_env(self, monkeypatch):
        from click.testing import CliRunner

        printer = _Printer()
        monkeypatch.setattr("kiln.cli.main._make_adapter", lambda cfg: printer)
        monkeypatch.setattr(
            "kiln.cli.main.load_printer_config",
            lambda *_a, **_k: {"type": "moonraker", "host": "http://t.local", "timeout": 1, "retries": 0},
        )
        monkeypatch.setattr("kiln.cli.main.validate_printer_config", lambda cfg: (True, None))
        return CliRunner(), printer

    def test_kiln_print_refuses_without_a_token(self, cli_env, tmp_path):
        from kiln.cli.main import cli

        runner, printer = cli_env
        gcode = tmp_path / "part.gcode"
        gcode.write_text("G28\n")
        result = runner.invoke(cli, ["print", str(gcode), "--json"])
        assert result.exit_code != 0, result.output
        assert "PREVIEW_NOT_CONFIRMED" in result.output
        assert printer.started == []

    def test_kiln_print_starts_with_a_token_inside_a_window(self, cli_env, tmp_path, monkeypatch):
        """A token on the command line is the SAW half.  From a shell with
        nobody at it, the GO half can only be a standing window a person
        opened; without one the same command is refused."""
        from kiln.cli.main import cli

        runner, printer = cli_env
        gcode = tmp_path / "part.gcode"
        gcode.write_text("G28\n")
        preview_evidence.record("png", str(gcode), renderer="stage", shown_sha="abc")
        preview_evidence.record_url_refusal(str(gcode), "signed_out")
        monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
        monkeypatch.setattr("kiln.local_stage.host_renders_apps", lambda *a, **k: False)
        monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: False)
        token = server.issue_preview_token(str(gcode), door="png")["token"]
        result = runner.invoke(cli, ["print", str(gcode), "--json", "--preview-token", token])
        assert result.exit_code != 0, result.output
        assert printer.started == []

        # A window over ONE printer: every tier's.  ``kiln print`` with no
        # --printer is aimed at the effective default printer -- in a real
        # install the same config name the adapter is registered under
        # ("garage" here), so the gate and the machine agree on it.
        monkeypatch.setattr(server, "_resolve_effective_printer_name", lambda *_a, **_k: "garage")
        monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: True)
        consent_windows.open_window(seconds=3600, scope=("garage",))
        monkeypatch.setattr(consent_windows, "person_at_terminal", lambda: False)
        result = runner.invoke(cli, ["print", str(gcode), "--json", "--preview-token", token])
        assert result.exit_code == 0, result.output
        assert printer.started == ["part.gcode"]

    def test_kiln_slice_print_after_refuses_without_a_token(self, cli_env, tmp_path, monkeypatch):
        from kiln.cli.main import cli

        runner, printer = cli_env
        mesh = _stl(tmp_path / "jar.stl")
        sliced = MagicMock()
        monkeypatch.setattr("kiln.cli.main.slice_model", sliced, raising=False)
        result = runner.invoke(cli, ["slice", mesh, "--print-after", "--json"])
        assert result.exit_code != 0, result.output
        assert "PREVIEW_NOT_CONFIRMED" in result.output
        assert printer.started == []


# ---------------------------------------------------------------------------
# A clearance ends with the call that earned it
# ---------------------------------------------------------------------------


class TestAClearanceEndsWithTheCallThatEarnedIt:
    """A gate that passes and a tool that then fails leave a clearance
    nobody spent.  It must not be there for the NEXT call to spend.

    Nothing in the gate enforces that.  What does is the shape each door
    dispatches with: the clearance is a ContextVar, and every door runs
    each call in its own context — the MCP server spawns a task per
    request, and the relay bounces each frame through a worker thread.
    Both copy the context, so a grant inside one call is invisible to the
    next.  (The CLI has no such boundary, which is why it drops both
    records itself when the command closes.)

    That containment is inherited, not stated, so it is pinned here at
    the two doors that rely on it — the door an agent knocks on, and the
    one the web knocks on, which reaches the tool functions directly and
    passes through no gate wrapper at all.  A dispatch that stopped
    isolating calls would hand an unspent clearance to a later start,
    which is the one failure this file exists to prevent.
    """

    @staticmethod
    def _grant(tool: str) -> None:
        print_signoff.grant(
            tool, "jar.stl", "garage",
            source=print_consent.SOURCE_ELICITED, door="stage",
        )

    def test_a_failed_mcp_call_hands_no_clearance_to_the_next_one(self, monkeypatch):
        """Through a live session: two ``tools/call`` requests, the first
        earning a clearance and failing before any adapter spends it."""
        import asyncio

        def _in_process_client():
            """A client wired to ``server.mcp`` in-process, on either SDK major:
            SDK 2 connects ``Client`` to a server object directly; 1.x has the
            memory-stream helper that 2.x removed."""
            try:
                from mcp import Client
            except ImportError:
                from mcp.shared.memory import create_connected_server_and_client_session

                return create_connected_server_and_client_session(server.mcp)
            return Client(server.mcp)

        seen: dict[str, object] = {}
        tools = server.mcp._tool_manager._tools

        def _grant_then_fail() -> dict:
            self._grant("license_status")
            raise RuntimeError("the tool failed after the gate passed")

        def _report() -> dict:
            seen["clearance"] = print_signoff.current()
            seen["take"] = print_signoff.take("jar.stl", "garage")
            return {"success": True}

        # Two argument-free tools stand in for a print door and whatever
        # the agent calls next, so the pin is about the dispatch and not
        # about any one tool's body.
        monkeypatch.setattr(tools["license_status"], "fn", _grant_then_fail)
        monkeypatch.setattr(tools["donate_info"], "fn", _report)

        async def _two_calls() -> None:
            async with _in_process_client() as client:
                first = await client.call_tool("license_status", {})
                # SDK 2 names the field is_error; 1.x, isError.
                assert getattr(first, "is_error", None) or getattr(first, "isError", False), first
                await client.call_tool("donate_info", {})

        asyncio.run(_two_calls())
        assert "clearance" in seen, "the second call never ran"
        assert seen["clearance"] is None, seen["clearance"]
        assert seen["take"] is None, "the adapter backstop would have spent it"

    def test_a_failed_relay_call_hands_no_clearance_to_the_next_frame(self):
        """The web door runs the tool functions itself — no gate wrapper,
        no reset in a ``finally`` — so the worker thread each frame runs
        in is the whole of its isolation.  Driven here without the task
        the receive loop adds on top, so the thread is what is pinned."""
        import asyncio

        from kiln import bridge_client

        seen: dict[str, object] = {}

        def _call_tool(name: str, args: dict):
            if name == "slice_and_print":
                self._grant(name)
                raise RuntimeError("the tool failed after the gate passed")
            seen["clearance"] = print_signoff.current()
            seen["take"] = print_signoff.take("jar.stl", "garage")
            return {"success": True}

        client = bridge_client.BridgeClient(
            license_key="lic", call_tool=_call_tool, fetch_artifact=lambda _t: "",
        )

        class _Socket:
            def __init__(self) -> None:
                self.sent: list[str] = []

            async def send(self, blob: str) -> None:
                self.sent.append(blob)

        async def _two_frames() -> _Socket:
            ws = _Socket()
            for tool in ("slice_and_print", "printer_status"):
                await client._handle_and_reply(
                    ws, {"request_id": tool, "tool_name": tool, "args": {}},
                )
            return ws

        ws = asyncio.run(_two_frames())
        assert len(ws.sent) == 2, ws.sent
        assert json.loads(ws.sent[0])["ok"] is False, ws.sent[0]
        assert "clearance" in seen, "the second frame never ran"
        assert seen["clearance"] is None, seen["clearance"]
        assert seen["take"] is None, "the adapter backstop would have spent it"


# ---------------------------------------------------------------------------
# Structural pin: every call that reaches a printer is cleared first
# ---------------------------------------------------------------------------

_SRC = pathlib.Path(server.__file__).parent
_START_MODULES = [
    _SRC / "server.py",
    _SRC / "scheduler.py",
    _SRC / "pipelines.py",
    _SRC / "cli" / "main.py",
    *sorted((_SRC / "plugins").glob("*.py")),
]

#: A start is cleared by the gate every MCP door calls, by the CLI's own
#: verdict, or by an explicit grant (a standing opt-in, a queued job, a
#: prior approval).  Anything else is a door that forgot.
_CLEARING_CALLS = {
    "_preview_gate_error", "token_verdict", "grant", "grant_from_record", "cli_gate",
}


def _innermost(tree, node):
    holders = [
        f for f in ast.walk(tree)
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
        and f.lineno <= node.lineno <= (f.end_lineno or 0)
    ]
    return min(holders, key=lambda f: (f.end_lineno or 0) - f.lineno) if holders else None


def _call_name(node: ast.Call) -> str:
    return node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")


def test_every_adapter_start_is_cleared_in_the_function_that_makes_it():
    """Reads every ``<adapter>.start_print(`` call in the tree and demands a
    clearing call in the same function, above it.  A new door that goes
    straight to the machine turns this red with its file and line."""
    uncleared: list[str] = []
    seen = 0
    for path in _START_MODULES:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _call_name(node) == "start_print"):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue  # the MCP tool called by name is itself gated
            if isinstance(node.func.value, ast.Name) and node.func.value.id in ("_srv", "server", "self"):
                continue
            seen += 1
            owner = _innermost(tree, node)
            if owner is None:
                uncleared.append(f"{path.name}:{node.lineno} (module level)")
                continue
            cleared = any(
                isinstance(n, ast.Call) and _call_name(n) in _CLEARING_CALLS and n.lineno < node.lineno
                for n in ast.walk(owner)
            )
            if not cleared:
                uncleared.append(f"{path.name}:{node.lineno} in {owner.name}")
    assert seen >= 9, f"the sweep sees too few start sites ({seen}); it is broken"
    assert uncleared == [], "these starts reach the printer with no sign-off:\n  " + "\n  ".join(uncleared)


def test_every_gated_tool_names_its_file_argument():
    for tool in ("run_quick_print", "run_reslice_and_print", "submit_job", "fleet_submit_job"):
        assert tool in server._CONSENT_FILE_ARG, tool


def test_the_refusal_tells_an_agent_what_to_do_next(tmp_path):
    """One sentence, naming the door to open and the tool to re-issue."""
    path = _stl(tmp_path / "jar.stl")
    preview_evidence.record("png", path, renderer="openscad")
    refusal, _ = preview_evidence.judge(path, "png", host_renders=True)
    msg = refusal["message"]
    assert "issue_preview_token" in msg or "re-issue" in msg
    assert msg.count(". ") <= 2, msg


def test_the_json_ledger_is_private(tmp_path):
    path = _stl(tmp_path / "jar.stl")
    preview_evidence.record("stage", path, via="panel_fetch")
    ledger = preview_evidence._ledger_path()
    assert ledger.is_file()
    assert oct(ledger.stat().st_mode & 0o777) == "0o600"
    json.loads(ledger.read_text())


# ---------------------------------------------------------------------------
# The PNG that signs off is the stage's own still, never the raw render
# ---------------------------------------------------------------------------


class TestPngIsTheStageStill:
    """Measured 2026-09-19: the sign-off image that reached the person was
    the plain OpenSCAD render — flat gradient, no plate, none of the
    stage's lighting — and a person approved a white jar that printed
    black.  The stage photographs itself (``stage``) or paints its own
    look (``stage_paint``); a raw render is for inspection only."""

    def _headless(self, path):
        preview_evidence.record_url_refusal(path, "signed_out")

    def test_the_raw_render_is_refused_as_inspection_only(self, tmp_path):
        path = _stl(tmp_path / "jar.stl")
        self._headless(path)
        preview_evidence.record("png", path, renderer="openscad", shown_sha="abc")
        refusal, _ = preview_evidence.judge(path, "png", host_renders=False)
        assert refusal is not None
        assert "inspection" in refusal["message"]
        assert refusal["code"] == "PREVIEW_DOOR_NOT_USED"

    @pytest.mark.parametrize("renderer", ["stage", "stage_paint"])
    def test_the_stage_still_is_accepted(self, tmp_path, renderer):
        path = _stl(tmp_path / "jar.stl")
        self._headless(path)
        preview_evidence.record("png", path, renderer=renderer, shown_sha="abc")
        refusal, verdict = preview_evidence.judge(path, "png", host_renders=False)
        assert refusal is None, refusal
        assert verdict["evidence"]["png"]["renderer"] == renderer
        assert verdict["evidence"]["png"]["shown_sha"] == "abc"

    def test_the_token_carries_the_hash_of_what_was_shown(self, tmp_path, monkeypatch):
        path = _stl(tmp_path / "jar.stl")
        self._headless(path)
        preview_evidence.record("png", path, renderer="stage_paint", shown_sha="deadbeef")
        monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
        monkeypatch.setattr("kiln.local_stage.host_renders_apps", lambda *a, **k: False)
        out = server.issue_preview_token(path, door="png")
        assert out["success"], out
        assert out["evidence"]["png"]["shown_sha"] == "deadbeef"

    def test_the_renderer_records_which_look_and_a_hash_of_the_pixels(self, tmp_path):
        from kiln.model_visualizer import visualize_model

        mesh = _stl(tmp_path / "jar.stl")

        def _run(cmd, **kwargs):
            for i, arg in enumerate(cmd):
                if arg == "-o" and i + 1 < len(cmd):
                    pathlib.Path(cmd[i + 1]).write_bytes(b"png-bytes")
            m = MagicMock()
            m.returncode = 0
            return m

        with patch("kiln.model_visualizer._find_openscad", return_value="openscad"), \
             patch("subprocess.run", side_effect=_run):
            result = visualize_model(
                mesh, output_dir=str(tmp_path / "out"), share_link=False, allow_stage=False,
            )
        assert result["success"], result
        png = preview_evidence.evidence_for(mesh)["png"]
        assert png["renderer"] == "openscad"
        assert len(png["shown_sha"]) == 32
        # And that record does not sign off a print.
        self._headless(mesh)
        refusal, _ = preview_evidence.judge(mesh, "png", host_renders=False)
        assert refusal is not None and "inspection" in refusal["message"]

    def test_the_raw_render_tools_say_so(self):
        for tool in ("visualize_model", "render_model_preview"):
            doc = getattr(server, tool).__doc__ or ""
            assert "inspection" in doc.lower() and "sign-off" in doc.lower(), tool


class TestTheHostedDeployKeepsNoRecord:
    """One disk for every account: a render one tenant made must never
    stand as evidence for another tenant's identical bytes.  On the hosted
    deploy the ledger is neither written nor read, and a local install is
    untouched.  Named as the guard witness for public-kiln:preview_evidence.py
    in kiln-pro's tenant-state ledger."""

    def test_nothing_is_recorded_or_read_on_the_shared_disk(self, tmp_path, monkeypatch):
        from kiln.runtime_env import HOSTED_ENV_VAR

        mesh = _stl(tmp_path / "jar.stl")
        # A record made on a machine that WAS allowed to write (the same
        # ledger file), then the same process flips to the hosted posture.
        assert preview_evidence.record(preview_evidence.DOOR_PNG, mesh) is not None
        preview_evidence._reset_for_tests()
        monkeypatch.setenv(HOSTED_ENV_VAR, "1")
        assert preview_evidence.record(preview_evidence.DOOR_STAGE, mesh) is None
        preview_evidence.record_url_refusal(mesh, "no link door")
        found = preview_evidence.evidence_for(mesh)
        assert found[preview_evidence.DOOR_PNG] is None, "the record on disk is not read back on hosted"
        assert found[preview_evidence.DOOR_STAGE] is None and found["url_refusal"] is None
        ledger = json.loads((tmp_path / "home" / "preview_evidence.json").read_text())
        (entry,) = ledger.values()
        assert set(entry) == {"path", "touched", preview_evidence.DOOR_PNG}, "hosted wrote nothing"

    def test_a_local_install_is_untouched(self, tmp_path, monkeypatch):
        from kiln.runtime_env import HOSTED_ENV_VAR

        monkeypatch.delenv(HOSTED_ENV_VAR, raising=False)
        mesh = _stl(tmp_path / "jar.stl")
        assert preview_evidence.record(preview_evidence.DOOR_PNG, mesh) is not None
        assert preview_evidence.evidence_for(mesh)[preview_evidence.DOOR_PNG] is not None


class TestTheGateHasNoDeadlock:
    """2026-09-21, live: the host held a tool list cached before a restart
    (declared the panel, could not draw it) and the install was signed
    out.  The stage door had no evidence, the link door had refused, and
    the PNG door was refused because "the stage is available on this
    host".  No door accepted.  A panel that has never fetched from this
    server is not available, and the gate says so in the server's words."""

    def _staged_still_and_refused_link(self, tmp_path):
        path = _stl(tmp_path / "jar.stl")
        preview_evidence.record("png", path, renderer="stage", shown_sha="abc")
        preview_evidence.record_url_refusal(path, "signed_out")
        return path

    def test_png_is_accepted_when_the_panel_is_declared_but_never_fetched(self, tmp_path):
        path = self._staged_still_and_refused_link(tmp_path)
        refusal, verdict = preview_evidence.judge(
            path, "png", host_renders=True, panel_proven=False,
        )
        assert refusal is None, refusal
        assert "fetched" in verdict["skipped"]["stage"]
        assert "signed out" in verdict["skipped"]["url"]
        assert "signed_out" not in verdict["skipped"]["url"], "a code is not a sentence"

    def test_png_is_still_refused_once_a_panel_has_proved_itself(self, tmp_path):
        path = self._staged_still_and_refused_link(tmp_path)
        refusal, _ = preview_evidence.judge(
            path, "png", host_renders=True, panel_proven=True,
        )
        assert refusal is not None
        assert "stage" in refusal["message"]

    def test_the_tool_reads_the_proof_from_the_stage_itself(self, tmp_path, monkeypatch):
        """Through the real door: a declared host, no fetch this process,
        signed out — tonight's exact state — has an accepted door."""
        from kiln import local_stage

        monkeypatch.setenv("KILN_HOME", str(tmp_path / "home"))
        preview_evidence._reset_for_tests()
        local_stage._reset_for_tests()
        path = self._staged_still_and_refused_link(tmp_path)
        monkeypatch.setattr(server, "_check_auth", lambda *_a, **_k: None)
        monkeypatch.setattr(local_stage, "host_renders_apps", lambda *_a, **_k: True)
        out = server.issue_preview_token(path, door="png")
        assert out.get("success") is True, out
        assert out["door"] == "png"
        assert "fetched" in out["skipped"]["stage"]
