"""Tests for kiln.printers.base -- dataclasses, enums, and exceptions.

Covers:
- PrinterError exception (message, cause)
- PrinterStatus enum members and values
- All dataclass constructors, defaults, and to_dict() serialisation
- PrinterCapabilities tuple-to-list conversion
- PrinterAdapter ABC (cannot be instantiated without implementing abstracts)
"""

from __future__ import annotations

import pytest

from kiln.printers.base import (
    STALE_STATE_WARN_AGE,
    JobProgress,
    PrinterAdapter,
    PrinterCapabilities,
    PrinterError,
    PrinterFile,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)

# ---------------------------------------------------------------------------
# PrinterError
# ---------------------------------------------------------------------------

class TestPrinterError:
    """Tests for the PrinterError exception class."""

    def test_message_only(self):
        exc = PrinterError("something broke")
        assert str(exc) == "something broke"
        assert exc.cause is None

    def test_message_with_cause(self):
        cause = ValueError("underlying issue")
        exc = PrinterError("wrapper", cause=cause)
        assert str(exc) == "wrapper"
        assert exc.cause is cause

    def test_is_exception(self):
        exc = PrinterError("test")
        assert isinstance(exc, Exception)

    def test_cause_defaults_to_none(self):
        exc = PrinterError("no cause given")
        assert exc.cause is None

    def test_can_be_raised_and_caught(self):
        with pytest.raises(PrinterError, match="kaboom"):
            raise PrinterError("kaboom")


# ---------------------------------------------------------------------------
# PrinterStatus enum
# ---------------------------------------------------------------------------

class TestPrinterStatus:
    """Tests for the PrinterStatus enum."""

    def test_all_members_present(self):
        expected = {"IDLE", "PRINTING", "PAUSED", "ERROR", "OFFLINE", "BUSY", "CANCELLING", "UNKNOWN"}
        actual = {member.name for member in PrinterStatus}
        assert actual == expected

    def test_values(self):
        assert PrinterStatus.IDLE.value == "idle"
        assert PrinterStatus.PRINTING.value == "printing"
        assert PrinterStatus.PAUSED.value == "paused"
        assert PrinterStatus.ERROR.value == "error"
        assert PrinterStatus.OFFLINE.value == "offline"
        assert PrinterStatus.BUSY.value == "busy"
        assert PrinterStatus.CANCELLING.value == "cancelling"
        assert PrinterStatus.UNKNOWN.value == "unknown"

    def test_from_value(self):
        assert PrinterStatus("idle") is PrinterStatus.IDLE
        assert PrinterStatus("printing") is PrinterStatus.PRINTING

    def test_invalid_value(self):
        with pytest.raises(ValueError):
            PrinterStatus("nonexistent")


# ---------------------------------------------------------------------------
# PrinterState dataclass
# ---------------------------------------------------------------------------

class TestPrinterState:
    """Tests for the PrinterState dataclass."""

    def test_defaults(self):
        state = PrinterState(connected=True, state=PrinterStatus.IDLE)
        assert state.connected is True
        assert state.state is PrinterStatus.IDLE
        assert state.tool_temp_actual is None
        assert state.tool_temp_target is None
        assert state.bed_temp_actual is None
        assert state.bed_temp_target is None
        assert state.chamber_temp_actual is None
        assert state.chamber_temp_target is None

    def test_full_construction(self):
        state = PrinterState(
            connected=True,
            state=PrinterStatus.PRINTING,
            tool_temp_actual=205.0,
            tool_temp_target=210.0,
            bed_temp_actual=60.0,
            bed_temp_target=60.0,
        )
        assert state.tool_temp_actual == 205.0
        assert state.bed_temp_target == 60.0

    def test_to_dict_converts_enum(self):
        state = PrinterState(connected=True, state=PrinterStatus.PRINTING)
        d = state.to_dict()
        assert d["state"] == "printing"
        assert d["connected"] is True

    def test_to_dict_includes_all_fields(self):
        state = PrinterState(
            connected=False,
            state=PrinterStatus.OFFLINE,
            tool_temp_actual=22.5,
            tool_temp_target=0.0,
            bed_temp_actual=21.0,
            bed_temp_target=0.0,
        )
        d = state.to_dict()
        expected_keys = {"connected", "state", "tool_temp_actual", "tool_temp_target",
                         "bed_temp_actual", "bed_temp_target",
                         "chamber_temp_actual", "chamber_temp_target"}
        assert set(d.keys()) == expected_keys

    def test_chamber_temp_fields(self):
        state = PrinterState(
            connected=True,
            state=PrinterStatus.PRINTING,
            chamber_temp_actual=35.0,
            chamber_temp_target=40.0,
        )
        assert state.chamber_temp_actual == 35.0
        assert state.chamber_temp_target == 40.0
        d = state.to_dict()
        assert d["chamber_temp_actual"] == 35.0
        assert d["chamber_temp_target"] == 40.0

    def test_to_dict_none_temps(self):
        state = PrinterState(connected=True, state=PrinterStatus.IDLE)
        d = state.to_dict()
        assert d["tool_temp_actual"] is None
        assert d["bed_temp_actual"] is None


# ---------------------------------------------------------------------------
# JobProgress dataclass
# ---------------------------------------------------------------------------

class TestJobProgress:
    """Tests for the JobProgress dataclass."""

    def test_all_defaults(self):
        job = JobProgress()
        assert job.file_name is None
        assert job.completion is None
        assert job.print_time_seconds is None
        assert job.print_time_left_seconds is None

    def test_full_construction(self):
        job = JobProgress(
            file_name="benchy.gcode",
            completion=75.5,
            print_time_seconds=2700,
            print_time_left_seconds=900,
        )
        assert job.file_name == "benchy.gcode"
        assert job.completion == 75.5

    def test_to_dict(self):
        job = JobProgress(file_name="test.gcode", completion=50.0)
        d = job.to_dict()
        assert d["file_name"] == "test.gcode"
        assert d["completion"] == 50.0
        assert d["print_time_seconds"] is None
        assert d["print_time_left_seconds"] is None

    def test_to_dict_all_none(self):
        job = JobProgress()
        d = job.to_dict()
        assert all(v is None for v in d.values())


# ---------------------------------------------------------------------------
# PrinterFile dataclass
# ---------------------------------------------------------------------------

class TestPrinterFile:
    """Tests for the PrinterFile dataclass."""

    def test_required_fields(self):
        f = PrinterFile(name="test.gcode", path="test.gcode")
        assert f.name == "test.gcode"
        assert f.path == "test.gcode"
        assert f.size_bytes is None
        assert f.date is None

    def test_full_construction(self):
        f = PrinterFile(name="part.gcode", path="folder/part.gcode", size_bytes=12345, date=1700000000)
        assert f.size_bytes == 12345
        assert f.date == 1700000000

    def test_to_dict(self):
        f = PrinterFile(name="a.gcode", path="a.gcode", size_bytes=100, date=999)
        d = f.to_dict()
        assert d == {"name": "a.gcode", "path": "a.gcode", "size_bytes": 100, "date": 999}


# ---------------------------------------------------------------------------
# UploadResult dataclass
# ---------------------------------------------------------------------------

class TestUploadResult:
    """Tests for the UploadResult dataclass."""

    def test_construction(self):
        r = UploadResult(success=True, file_name="x.gcode", message="OK")
        assert r.success is True
        assert r.file_name == "x.gcode"
        assert r.message == "OK"

    def test_to_dict(self):
        r = UploadResult(success=False, file_name="y.gcode", message="failed")
        d = r.to_dict()
        assert d == {"success": False, "file_name": "y.gcode", "message": "failed"}


# ---------------------------------------------------------------------------
# PrintResult dataclass
# ---------------------------------------------------------------------------

class TestPrintResult:
    """Tests for the PrintResult dataclass."""

    def test_defaults(self):
        r = PrintResult(success=True, message="ok")
        assert r.job_id is None

    def test_with_job_id(self):
        r = PrintResult(success=True, message="started", job_id="abc-123")
        assert r.job_id == "abc-123"

    def test_to_dict(self):
        r = PrintResult(success=True, message="done", job_id="x")
        d = r.to_dict()
        assert d == {"success": True, "message": "done", "job_id": "x"}

    def test_to_dict_null_job_id(self):
        r = PrintResult(success=True, message="done")
        d = r.to_dict()
        assert d["job_id"] is None


# ---------------------------------------------------------------------------
# PrinterCapabilities dataclass
# ---------------------------------------------------------------------------

class TestPrinterCapabilities:
    """Tests for the PrinterCapabilities dataclass."""

    def test_defaults(self):
        caps = PrinterCapabilities()
        assert caps.can_upload is True
        assert caps.can_set_temp is True
        assert caps.can_send_gcode is True
        assert caps.can_pause is True
        assert caps.supported_extensions == (".gcode", ".gco", ".g")

    def test_custom_values(self):
        caps = PrinterCapabilities(
            can_upload=False,
            can_set_temp=False,
            can_send_gcode=False,
            can_pause=False,
            supported_extensions=(".stl",),
        )
        assert caps.can_upload is False
        assert caps.supported_extensions == (".stl",)

    def test_to_dict_converts_tuple_to_list(self):
        caps = PrinterCapabilities()
        d = caps.to_dict()
        assert isinstance(d["supported_extensions"], list)
        assert d["supported_extensions"] == [".gcode", ".gco", ".g"]

    def test_to_dict_all_fields(self):
        caps = PrinterCapabilities()
        d = caps.to_dict()
        expected_keys = {"can_upload", "can_set_temp", "can_send_gcode", "can_pause", "can_stream", "can_probe_bed", "can_update_firmware", "can_snapshot", "can_detect_filament", "device_type", "supported_extensions"}
        assert set(d.keys()) == expected_keys


# ---------------------------------------------------------------------------
# PrinterAdapter ABC
# ---------------------------------------------------------------------------

class TestPrinterAdapterABC:
    """Tests verifying PrinterAdapter cannot be instantiated directly."""

    def test_cannot_instantiate(self):
        with pytest.raises(TypeError):
            PrinterAdapter()  # type: ignore[abstract]

    def test_subclass_without_all_methods_raises(self):
        class Incomplete(PrinterAdapter):
            pass

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_subclass_with_all_methods(self):
        """A complete subclass can be instantiated."""

        class Complete(PrinterAdapter):
            @property
            def name(self) -> str:
                return "test"

            @property
            def capabilities(self) -> PrinterCapabilities:
                return PrinterCapabilities()

            def get_state(self):
                return PrinterState(connected=True, state=PrinterStatus.IDLE)

            def get_job(self):
                return JobProgress()

            def list_files(self):
                return []

            def upload_file(self, file_path):
                return UploadResult(success=True, file_name="", message="")

            def _start_print_impl(self, file_name):
                return PrintResult(success=True, message="")

            def cancel_print(self):
                return PrintResult(success=True, message="")

            def pause_print(self):
                return PrintResult(success=True, message="")

            def _resume_print_impl(self):
                return PrintResult(success=True, message="")

            def set_tool_temp(self, target):
                return True

            def set_bed_temp(self, target):
                return True

            def send_gcode(self, commands):
                return True

            def delete_file(self, file_path):
                return True

            def emergency_stop(self):
                return PrintResult(success=True, message="")

        instance = Complete()
        assert instance.name == "test"
        assert isinstance(instance.capabilities, PrinterCapabilities)


class TestResumePrintTemplate:
    """The base resume_print() template: refuse to claim success when there's
    no paused print, but never block a legitimate resume on uncertainty.
    """

    @staticmethod
    def _adapter(state, after=None):
        """*after* is the state the printer reports once the resume lands.

        The template re-reads the printer afterwards, because
        ``_resume_print_impl`` returning success only means the command was
        PUBLISHED.  ``after=None`` means the state never changes.
        """
        impl_calls: list[str] = []

        class _A(PrinterAdapter):
            # No sleeping in the test suite; the read-back's real budget is
            # exercised in test_stalled_print_detection.py.
            _RESUME_VERIFY_TIMEOUT = 0.0
            _RESUME_VERIFY_INTERVAL = 0.0

            def get_state(self):
                if isinstance(state, Exception):
                    raise state
                current = after if (after and impl_calls) else state
                return PrinterState(connected=True, state=current)

            def _resume_print_impl(self):
                impl_calls.append("impl")
                return PrintResult(success=True, message="Print resumed.")

        _A.__abstractmethods__ = frozenset()  # only need get_state + the impl
        adapter = _A()
        adapter._impl_calls = impl_calls
        return adapter

    def test_paused_proceeds_to_impl(self):
        a = self._adapter(PrinterStatus.PAUSED, after=PrinterStatus.PRINTING)
        result = a.resume_print()
        assert result.success is True
        assert a._impl_calls == ["impl"]

    def test_a_resume_the_printer_ignored_is_not_a_success(self):
        """The read-back.  A printer still reporting PAUSED did not resume,
        whatever the fire-and-forget transport reported back."""
        a = self._adapter(PrinterStatus.PAUSED)  # never leaves paused
        result = a.resume_print()
        assert a._impl_calls == ["impl"]
        assert result.success is False
        assert "still reports paused" in result.message

    def test_idle_returns_honest_failure(self):
        a = self._adapter(PrinterStatus.IDLE)
        result = a.resume_print()
        assert result.success is False
        assert "no paused print" in result.message.lower()
        assert a._impl_calls == []  # never fired the resume command

    def test_printing_is_refused_but_names_the_way_through(self):
        """PRINTING is the word that lied on 2026-08-11, so a refusal built on
        it alone must hand the user an override rather than a dead end.

        With no observed motion either way the gate still refuses — but it
        says what it does and does not know, and how to overrule it.  See
        test_stalled_print_detection.py for the case where motion evidence
        contradicts the word and the refusal is dropped entirely.
        """
        a = self._adapter(PrinterStatus.PRINTING)
        result = a.resume_print()
        assert result.success is False
        assert "force=True" in result.message
        assert a._impl_calls == []

    def test_force_bypasses_the_gate_entirely(self):
        a = self._adapter(PrinterStatus.PRINTING, after=PrinterStatus.PRINTING)
        result = a.resume_print(force=True)
        assert a._impl_calls == ["impl"]
        assert result.success is True

    @pytest.mark.parametrize(
        "state",
        [
            PrinterStatus.OFFLINE,
            PrinterStatus.UNKNOWN,
            PrinterStatus.BUSY,
            PrinterStatus.ERROR,
        ],
    )
    def test_uncertain_state_fails_open(self, state):
        a = self._adapter(state)
        a.resume_print()
        # Not CONFIDENT it's idle/printing -> fail OPEN: try, surface real result.
        assert a._impl_calls == ["impl"]

    def test_state_read_error_fails_open(self):
        a = self._adapter(RuntimeError("transient blip"))
        a.resume_print()
        assert a._impl_calls == ["impl"]  # a transient read must never block resume


# ---------------------------------------------------------------------------
# PrinterState staleness contract
# ---------------------------------------------------------------------------


class TestPrinterStateStaleness:
    """One age field and one sentence, shared by every adapter and every door.

    A push-cache adapter can answer "printing" in exactly the tone it would
    use for a live reading.  These are the two things every reporting surface
    relies on to tell the difference.
    """

    def test_no_age_is_not_a_staleness_claim(self):
        """An adapter that queries the printer every call reports None."""
        state = PrinterState(connected=True, state=PrinterStatus.PRINTING)
        assert state.state_age_seconds is None
        assert state.is_stale() is False
        assert state.staleness_note() is None
        assert "state_age_seconds" not in state.to_dict()

    def test_fresh_age_is_quiet(self):
        state = PrinterState(
            connected=True, state=PrinterStatus.PRINTING, state_age_seconds=2.5
        )
        assert state.is_stale() is False
        assert state.staleness_note() is None
        assert state.to_dict()["state_age_seconds"] == 2.5

    def test_stale_age_names_the_age_and_the_state(self):
        state = PrinterState(
            connected=True, state=PrinterStatus.PRINTING, state_age_seconds=312.4
        )
        assert state.is_stale() is True
        note = state.staleness_note()
        assert note is not None
        assert "312s" in note
        assert "PRINTING" in note

    def test_threshold_is_the_shared_constant_and_is_overridable(self):
        state = PrinterState(
            connected=True,
            state=PrinterStatus.PRINTING,
            state_age_seconds=STALE_STATE_WARN_AGE + 1,
        )
        assert state.is_stale() is True
        # A caller with a tighter tolerance can ask its own question.
        assert state.is_stale(max_age=10_000) is False
        assert (
            PrinterState(
                connected=True,
                state=PrinterStatus.PRINTING,
                state_age_seconds=STALE_STATE_WARN_AGE,
            ).is_stale()
            is False
        )

    def test_staleness_never_rewrites_the_state(self):
        """The enum is load-bearing downstream, so age only annotates it.

        The concurrency gate reads anything other than PRINTING/PAUSED as
        not-busy: demoting a stale PRINTING to UNKNOWN would let a second
        print start on a machine that is already running one.
        """
        state = PrinterState(
            connected=True, state=PrinterStatus.PRINTING, state_age_seconds=9_999.0
        )
        assert state.state is PrinterStatus.PRINTING
        assert state.to_dict()["state"] == "printing"
