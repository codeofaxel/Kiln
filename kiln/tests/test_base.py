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

            def start_print(self, file_name):
                return PrintResult(success=True, message="")

            def cancel_print(self):
                return PrintResult(success=True, message="")

            def pause_print(self):
                return PrintResult(success=True, message="")

            def resume_print(self):
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
