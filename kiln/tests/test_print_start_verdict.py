"""One verdict for "did the print start?", and an envelope that agrees with it.

The incident, measured twice on a Bambu A1 on 2026-08-11.  A print that ran to
completion came back from ``slice_and_print`` like this::

    outer:  {"success": true,  "message": "Sliced, uploaded, and started
             printing mesh_ce55d43d.stl."}
    inner:  {"print": {"success": false, "message": "Print command sent for
             mesh_ce55d43d.3mf but printer reported a failure.",
             "job_id": null}}

Two defects, one envelope.  The outer ``success`` was hardcoded ``True`` — it
had never read the print result at all, so it was right by luck and would have
said the same thing about a genuine refusal.  The inner ``success`` was a false
negative: the adapter's start check read the printer's MQTT status cache
microseconds after publishing the command, and that cache still held the
previous, cancelled job's ``gcode_state="failed"``.  The message pins the
branch — it carries no error detail, so no error code was seen, so the only
branch that can produce it is the stale ``failed`` state.

The rule that fixes the class: a reading that predates the command is not a
verdict on the command.

These tests cover the shared resolver and the ``slice_and_print`` tool wiring
(real tool, mocked slicer and adapter — the house idiom), and they prove the
envelope assertion has teeth by feeding it the shape that shipped.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from kiln.print_start_verdict import (
    ACCEPTED,
    FAILED,
    STARTED,
    PrintStartVerdict,
    resolve_print_start,
)
from kiln.printers.base import PrinterState, PrinterStatus, PrintResult, UploadResult

# The two strings as the printer and the tool actually produced them.
INCIDENT_ADAPTER_MESSAGE = (
    "Print command sent for mesh_ce55d43d.3mf but printer reported a failure."
)
INCIDENT_FILE = "mesh_ce55d43d.stl"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """A printer whose start answer and status vintage are both dialled in.

    *age* is ``state_age_seconds``: ``None`` for an adapter that queries the
    printer on every call (current by construction), a real number of seconds
    for a push-cache adapter like Bambu.
    """

    def __init__(
        self,
        *,
        result: PrintResult,
        status: PrinterStatus = PrinterStatus.IDLE,
        age: float | None = None,
        get_state_raises: bool = False,
    ) -> None:
        self._result = result
        self._status = status
        self._age = age
        self._get_state_raises = get_state_raises
        self.start_print_calls: list[tuple] = []

    def get_state(self) -> PrinterState:
        if self._get_state_raises:
            raise RuntimeError("printer unreachable")
        return PrinterState(
            connected=True, state=self._status, state_age_seconds=self._age,
        )

    def start_print(self, file_name: str, **kwargs) -> PrintResult:
        self.start_print_calls.append((file_name, kwargs))
        return self._result

    def upload_file(self, path: str) -> UploadResult:
        return UploadResult(
            success=True, file_name="mesh_ce55d43d.3mf", message="uploaded",
        )


def _incident_adapter(**overrides) -> _FakeAdapter:
    """The A1 as it behaved: a failure verdict read off a stale cache.

    ``age=60`` is the point — the reading the adapter judged by is a minute
    older than the command being judged.
    """
    kwargs = {
        "result": PrintResult(success=False, message=INCIDENT_ADAPTER_MESSAGE),
        "status": PrinterStatus.IDLE,
        "age": 60.0,
    }
    kwargs.update(overrides)
    return _FakeAdapter(**kwargs)


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


class TestResolvePrintStart:
    def test_uncorroborated_failure_is_accepted_not_failed(self):
        """The measured incident: a stale read is not a verdict."""
        adapter = _incident_adapter()
        verdict = resolve_print_start(
            adapter,
            adapter._result,
            sent_at=time.monotonic(),
            file_name=INCIDENT_FILE,
        )
        assert verdict.state == ACCEPTED
        assert verdict.ok is True
        assert verdict.confirmed is False
        assert verdict.evidence["corroboration"] == "predates_command"
        # The adapter's claim is kept, labelled, and not published as the verdict.
        assert verdict.evidence["adapter_reported_success"] is False
        assert verdict.evidence["adapter_message"] == INCIDENT_ADAPTER_MESSAGE
        assert "printer_status()" in verdict.message

    def test_failure_confirmed_after_the_command_stays_failed(self):
        """A real refusal must still read as a refusal — the gate can fail."""
        adapter = _incident_adapter(age=0.0)
        verdict = resolve_print_start(
            adapter,
            adapter._result,
            sent_at=time.monotonic(),
            file_name=INCIDENT_FILE,
        )
        assert verdict.state == FAILED
        assert verdict.ok is False
        assert verdict.evidence["corroboration"] == "after_command"
        assert verdict.message == INCIDENT_ADAPTER_MESSAGE

    def test_query_per_call_adapter_is_current_by_construction(self):
        """No age means the reading is live, not that it is unknown."""
        adapter = _incident_adapter(age=None)
        verdict = resolve_print_start(
            adapter, adapter._result, sent_at=time.monotonic(),
        )
        assert verdict.state == FAILED
        assert verdict.evidence["corroboration"] == "live"

    def test_failure_is_never_promoted_to_started(self):
        """Softening is one-directional: at most ``accepted``, never ``started``."""
        adapter = _incident_adapter(status=PrinterStatus.PRINTING, age=0.0)
        verdict = resolve_print_start(
            adapter, adapter._result, sent_at=time.monotonic(),
        )
        assert verdict.state == ACCEPTED
        assert verdict.confirmed is False

    @pytest.mark.parametrize(
        "status",
        [PrinterStatus.UNKNOWN, PrinterStatus.OFFLINE, PrinterStatus.BUSY],
    )
    def test_absence_of_evidence_does_not_license_a_failure(self, status):
        adapter = _incident_adapter(status=status, age=0.0)
        verdict = resolve_print_start(
            adapter, adapter._result, sent_at=time.monotonic(),
        )
        assert verdict.state == ACCEPTED

    def test_unreachable_printer_cannot_corroborate_a_failure(self):
        adapter = _incident_adapter(get_state_raises=True)
        verdict = resolve_print_start(
            adapter, adapter._result, sent_at=time.monotonic(),
        )
        assert verdict.state == ACCEPTED
        assert verdict.evidence["corroboration"] == "unavailable"

    def test_confirmed_running_reads_started(self):
        adapter = _FakeAdapter(
            result=PrintResult(success=True, message="Started printing x.3mf."),
            status=PrinterStatus.PRINTING,
            age=0.0,
        )
        verdict = resolve_print_start(
            adapter, adapter._result, sent_at=time.monotonic(),
        )
        assert verdict.state == STARTED
        assert verdict.confirmed is True
        assert verdict.ok is True

    def test_transient_reads_accepted_and_keeps_the_adapter_detail(self):
        """``prepare`` maps to BUSY — a job starting, not a job running.

        The adapter's own sentence carries the transient state name and any
        pre-print warnings it appended, so it is kept verbatim.
        """
        detail = (
            "Print command accepted for x.3mf. Printer is preparing (state: "
            "prepare). WARNING: AMS color mismatch on slot 1."
        )
        adapter = _FakeAdapter(
            result=PrintResult(success=True, message=detail),
            status=PrinterStatus.BUSY,
            age=0.0,
        )
        verdict = resolve_print_start(
            adapter, adapter._result, sent_at=time.monotonic(),
        )
        assert verdict.state == ACCEPTED
        assert verdict.message == detail

    def test_job_id_survives(self):
        adapter = _FakeAdapter(
            result=PrintResult(success=True, message="ok", job_id="job-7"),
            status=PrinterStatus.PRINTING,
            age=0.0,
        )
        verdict = resolve_print_start(
            adapter, adapter._result, sent_at=time.monotonic(),
        )
        assert verdict.to_dict()["job_id"] == "job-7"

    @pytest.mark.parametrize("state", [STARTED, ACCEPTED, FAILED])
    def test_the_dict_never_disagrees_with_the_verdict(self, state):
        verdict = PrintStartVerdict(state=state, message="m")
        data = verdict.to_dict()
        assert data["print_start"] == verdict.state
        assert data["success"] == verdict.ok
        assert data["confirmed_running"] == verdict.confirmed


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------


def assert_envelope_agrees(resp: dict, inner_key: str = "print") -> None:
    """The two halves of a print envelope must say the same thing.

    This is the assertion the shipped envelope failed.  It is a named helper
    so a test below can prove it catches that shape rather than trusting it.
    """
    inner = resp[inner_key]
    assert resp["success"] == inner["success"], (
        f"envelope contradicts itself: outer success={resp['success']} "
        f"vs inner success={inner['success']}"
    )
    assert resp["print_start"] == inner["print_start"], (
        f"envelope contradicts itself: outer print_start={resp['print_start']} "
        f"vs inner print_start={inner['print_start']}"
    )


def test_the_assertion_catches_the_envelope_that_shipped():
    """Prove the check has teeth by handing it the measured contradiction."""
    shipped = {
        "success": True,
        "print_start": "started",
        "message": f"Sliced, uploaded, and started printing {INCIDENT_FILE}.",
        "print": {
            "success": False,
            "print_start": "failed",
            "message": INCIDENT_ADAPTER_MESSAGE,
            "job_id": None,
        },
    }
    with pytest.raises(AssertionError, match="contradicts itself"):
        assert_envelope_agrees(shipped)


# ---------------------------------------------------------------------------
# slice_and_print — the real tool
# ---------------------------------------------------------------------------


def _register_slicer_tools() -> dict:
    from kiln.plugins.slicer_tools import _SlicerToolsPlugin

    tools: dict = {}

    class FakeMCP:
        def tool(self_mcp, name: str | None = None):
            def decorator(fn):
                tools[name or fn.__name__] = fn
                return fn
            return decorator

    _SlicerToolsPlugin().register(FakeMCP())
    return tools


@pytest.fixture(scope="module")
def slicer_tools():
    return _register_slicer_tools()


def _run_slice_and_print(slicer_tools, tmp_path: Path, adapter: _FakeAdapter) -> dict:
    """Drive the real tool with the slicer and the printer mocked out."""
    from kiln.slicer import SliceResult

    stl = tmp_path / INCIDENT_FILE
    stl.write_bytes(b"\x00" * 84)
    gcode = tmp_path / "out.gcode"
    gcode.write_text("G28\n")
    ini = tmp_path / "profile.ini"
    ini.write_text("layer_height = 0.2\n")

    import kiln.plugins.slicer_tools as _st
    import kiln.server as _srv

    def fake_slice_file(path, **kwargs):
        return SliceResult(
            success=True, output_path=str(gcode), slicer="prusa-slicer",
            message="ok",
        )

    with patch.object(_srv, "_check_auth", return_value=None), \
            patch.object(
                _srv, "_resolve_slice_profile_context",
                return_value=(None, str(ini)),
            ), \
            patch.object(_srv, "_PRINTER_TYPE", "octoprint"), \
            patch.object(_srv, "_resolve_adapter", return_value=adapter), \
            patch.object(
                _srv, "_resolve_effective_printer_name", return_value="p1",
            ), \
            patch.object(_srv, "_emergency_latch_error", return_value=None), \
            patch.object(_srv, "preflight_check", return_value={"ready": True}), \
            patch.object(_srv, "_get_heater_watchdog", return_value=MagicMock()), \
            patch.object(_srv, "_audit", MagicMock()), \
            patch.object(
                _st, "_apply_bed_fit_gate",
                return_value=(str(stl), None, {}),
            ), \
            patch.object(
                _st, "_multicolor_flatten_advisory", return_value=(None, None),
            ), \
            patch("kiln.slicer.slice_file", side_effect=fake_slice_file):
        return slicer_tools["slice_and_print"](
            input_path=str(stl), material="PLA", skip_validation=True,
        )


class TestSliceAndPrintEnvelope:
    @pytest.fixture(autouse=True)
    def _skip_preview_gate(self, monkeypatch):
        """This suite predates the preview-consent gate and is not about it.

        ``slice_and_print`` now requires a preview token (the gate itself
        is tested in test_the_user_is_actually_asked.py).  These cases
        are about whether the two halves of the returned envelope agree
        about a print that did start, so they take the same bypass CI
        does rather than each carrying a token they have no opinion
        about.  Without it every case here stops at
        PREVIEW_NOT_CONFIRMED and never reaches the envelope.
        """
        monkeypatch.setenv("KILN_SKIP_PREVIEW_GATE", "1")

    def test_the_measured_incident_no_longer_contradicts_itself(
        self, slicer_tools, tmp_path,
    ):
        """The A1 case: a stale failure, a print that really started."""
        resp = _run_slice_and_print(slicer_tools, tmp_path, _incident_adapter())

        assert_envelope_agrees(resp)
        assert resp["print_start"] == ACCEPTED
        assert resp["success"] is True
        assert resp["print"]["confirmed_running"] is False
        # And it says so in words, in the half a human reads.
        assert "has not confirmed it is running" in resp["message"]
        # The adapter's claim is preserved for diagnosis, not as a verdict.
        assert (
            resp["print"]["evidence"]["adapter_message"]
            == INCIDENT_ADAPTER_MESSAGE
        )

    def test_a_real_refusal_reads_failed_in_both_halves(
        self, slicer_tools, tmp_path,
    ):
        resp = _run_slice_and_print(
            slicer_tools, tmp_path, _incident_adapter(age=0.0),
        )

        assert_envelope_agrees(resp)
        assert resp["print_start"] == FAILED
        assert resp["success"] is False
        assert "did not start" in resp["message"]

    def test_a_confirmed_start_reads_started_in_both_halves(
        self, slicer_tools, tmp_path,
    ):
        adapter = _FakeAdapter(
            result=PrintResult(success=True, message="Started printing."),
            status=PrinterStatus.PRINTING,
            age=0.0,
        )
        resp = _run_slice_and_print(slicer_tools, tmp_path, adapter)

        assert_envelope_agrees(resp)
        assert resp["print_start"] == STARTED
        assert resp["success"] is True
        assert resp["print"]["confirmed_running"] is True
        assert "started printing" in resp["message"]

    def test_the_command_is_sent_before_the_verdict_is_taken(
        self, slicer_tools, tmp_path,
    ):
        """Sanity: the tool really reaches the adapter, so the rest means something."""
        adapter = _incident_adapter()
        _run_slice_and_print(slicer_tools, tmp_path, adapter)
        assert adapter.start_print_calls, "slice_and_print never called start_print"


# ---------------------------------------------------------------------------
# The scheduler — where the same false negative costs a duplicate print
# ---------------------------------------------------------------------------


class TestSchedulerDispatch:
    """A queued job the printer actually took must not be requeued.

    This is the door where the stale-read failure has teeth rather than
    confusion: requeuing sends the same file back at a machine that is
    already running it.
    """

    def _dispatch(self, *, age: float | None):
        from kiln.events import EventBus
        from kiln.printers.base import JobProgress, PrinterCapabilities
        from kiln.queue import JobStatus, PrintQueue
        from kiln.registry import PrinterRegistry
        from kiln.scheduler import JobScheduler

        adapter = MagicMock()
        type(adapter).name = PropertyMock(return_value="printer-1")
        type(adapter).capabilities = PropertyMock(
            return_value=PrinterCapabilities(),
        )
        adapter.get_state.return_value = PrinterState(
            connected=True,
            state=PrinterStatus.IDLE,
            state_age_seconds=age,
        )
        adapter.get_job.return_value = JobProgress(
            file_name=None, completion=None,
        )
        adapter.start_print.return_value = PrintResult(
            success=False, message=INCIDENT_ADAPTER_MESSAGE,
        )

        queue = PrintQueue()
        registry = PrinterRegistry()
        registry.register("printer-1", adapter)
        scheduler = JobScheduler(
            queue, registry, EventBus(), poll_interval=0.1, max_retries=0,
        )
        job_id = queue.submit(file_name="job.gcode")
        return scheduler.tick(), queue.get_job(job_id), JobStatus

    def test_an_uncorroborated_failure_does_not_requeue_a_running_job(self):
        """age=60: the reading the failure was read off predates the command."""
        result, job, JobStatus = self._dispatch(age=60.0)
        assert not result["failed"], (
            "a stale-cache failure requeued a job the printer had taken"
        )
        assert job.status != JobStatus.FAILED

    def test_a_corroborated_failure_still_fails_the_job(self):
        """age=0: the printer, asked after the command, is idle.  It really failed."""
        result, job, JobStatus = self._dispatch(age=0.0)
        assert len(result["failed"]) == 1
        assert INCIDENT_ADAPTER_MESSAGE in result["failed"][0]["error"]
        assert job.status == JobStatus.FAILED
