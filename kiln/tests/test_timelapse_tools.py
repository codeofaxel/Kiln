"""Tests for print_status_lite, list_snapshots, and watch_print save_to_disk.

Covers:
- print_status_lite (printing state, idle state, telemetry age, printer not found)
- list_snapshots (filter passthrough, empty results, DB errors)
- watch_print save_to_disk parameter

The print_status_lite tests build REAL PrinterState / JobProgress objects.  They
used to hand the tool MagicMocks carrying attribute names no adapter return type
has ever had (``job.time_left``, ``state.hotend_temp``), so they passed green
while the tool raised AttributeError on its first field read and returned
``{"state": "error"}`` for every backend in every state.  A MagicMock answers to
any attribute name, which is precisely why it cannot catch that.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from kiln.printers.base import JobProgress, PrinterState, PrinterStatus
from kiln.registry import PrinterNotFoundError


class TestPrintStatusLite:
    """printer_status(detail=...) tests, and the print_status_lite alias.

    ``print_status_lite`` used to be a second tool returning the same
    numbers under different names (``completion_pct``, ``hotend_temp``).
    It is now an alias for ``printer_status(detail="lite")`` and returns
    that tool's nested shape, so there is one vocabulary to read.
    """

    @staticmethod
    def _wire(mock_get_adapter, state: PrinterState, job: JobProgress) -> None:
        adapter = MagicMock()
        adapter.get_state.return_value = state
        adapter.get_job.return_value = job
        mock_get_adapter.return_value = adapter

    @patch("kiln.server._get_adapter")
    def test_lite_returns_the_same_vocabulary_as_full(self, mock_get_adapter):
        from kiln.server import print_status_lite, printer_status

        self._wire(
            mock_get_adapter,
            PrinterState(
                connected=True,
                state=PrinterStatus.PRINTING,
                tool_temp_actual=210.0,
                bed_temp_actual=60.0,
            ),
            JobProgress(
                file_name="benchy.gcode",
                completion=45.2,
                print_time_seconds=600,
                print_time_left_seconds=1800,
            ),
        )

        result = printer_status(detail="lite")
        assert result["printer"]["state"] == "printing"
        assert result["printer"]["tool_temp_actual"] == 210.0
        assert result["printer"]["bed_temp_actual"] == 60.0
        assert result["job"]["completion"] == 45.2
        assert result["job"]["file_name"] == "benchy.gcode"
        assert result["job"]["print_time_left_seconds"] == 1800
        assert result["job"]["print_time_seconds"] == 600
        assert "error" not in result

        # The retired names are gone — that rename was the whole defect.
        flat = set(result)
        assert not {"completion_pct", "hotend_temp", "bed_temp"} & flat

        # The alias delegates, so it produces exactly the same object.
        assert print_status_lite() == result

    @patch("kiln.server._get_adapter")
    def test_lite_drops_capabilities_and_static_hardware_only(self, mock_get_adapter):
        """The saving must come out of fields that cannot change between
        polls — never out of a reading a monitor renders."""
        from kiln.server import printer_status

        self._wire(
            mock_get_adapter,
            PrinterState(
                connected=True,
                state=PrinterStatus.PRINTING,
                tool_temp_actual=220.0,
                tool_temp_target=220.0,
                bed_temp_actual=65.0,
                bed_temp_target=65.0,
                chamber_temp_actual=38.0,
                speed_profile="sport",
                speed_magnitude=124,
                print_error=0,
                wifi_signal="-45dBm",
                nozzle_type="stainless_steel",
                cooling_fan_speed=255,
                state_age_seconds=2.4,
            ),
            JobProgress(file_name="a.3mf", completion=0.42),
        )

        full = printer_status()
        lite = printer_status(detail="lite")

        assert "capabilities" in full
        assert "capabilities" not in lite

        # Everything the web monitor renders survives the trim.
        for key in (
            "state", "tool_temp_actual", "tool_temp_target",
            "bed_temp_actual", "bed_temp_target", "chamber_temp_actual",
            "speed_profile", "speed_magnitude", "print_error",
            "state_age_seconds",
        ):
            assert key in lite["printer"], key

        # Static description does not.
        for key in ("wifi_signal", "nozzle_type", "cooling_fan_speed"):
            assert key in full["printer"], key
            assert key not in lite["printer"], key

        # `job` is small already and is not trimmed.
        assert lite["job"] == full["job"]

    @patch("kiln.server._reported_printer_name", return_value="garage")
    @patch("kiln.server._get_adapter")
    def test_the_printer_label_survives_the_lite_trim(self, mock_get_adapter, _name):
        """Merged behaviour: `printer_name` says which machine a reading
        describes. It has to be on BOTH detail levels — a label is what
        makes a reading attributable, and lite is the level being polled
        every few seconds."""
        from kiln.server import printer_status

        self._wire(
            mock_get_adapter,
            PrinterState(connected=True, state=PrinterStatus.PRINTING),
            JobProgress(file_name="a.3mf", completion=0.5),
        )
        assert printer_status()["printer_name"] == "garage"
        assert printer_status(detail="lite")["printer_name"] == "garage"

    @patch("kiln.server._get_adapter")
    def test_idle_omits_absent_readings_at_both_levels(self, mock_get_adapter):
        from kiln.server import printer_status

        self._wire(
            mock_get_adapter,
            PrinterState(connected=True, state=PrinterStatus.IDLE),
            JobProgress(),
        )

        for detail in ("full", "lite"):
            result = printer_status(detail=detail)
            assert result["printer"]["state"] == "idle"
            assert result["printer"]["tool_temp_actual"] is None
            assert result["job"]["print_time_left_seconds"] is None
            assert "state_age_seconds" not in result["printer"]

    @patch("kiln.server._get_adapter")
    def test_lite_still_carries_the_staleness_warning(self, mock_get_adapter):
        """Lite is the polling level, so it is exactly where a frozen cache
        gets read as progress.  Trimming must never take the warning."""
        from kiln.server import printer_status

        self._wire(
            mock_get_adapter,
            PrinterState(
                connected=True,
                state=PrinterStatus.PRINTING,
                tool_temp_actual=180.0,
                state_age_seconds=312.0,
            ),
            JobProgress(file_name="part.3mf", completion=0.0),
        )

        result = printer_status(detail="lite")
        assert result["printer"]["state_age_seconds"] == 312.0
        assert "312s old" in result["telemetry_warning"]

    @patch("kiln.server._get_adapter")
    def test_fresh_reading_says_nothing_about_staleness(self, mock_get_adapter):
        from kiln.server import printer_status

        self._wire(
            mock_get_adapter,
            PrinterState(
                connected=True, state=PrinterStatus.PRINTING, state_age_seconds=1.2
            ),
            JobProgress(completion=12.0),
        )

        result = printer_status(detail="lite")
        assert result["printer"]["state_age_seconds"] == 1.2
        assert "telemetry_warning" not in result

    def test_unknown_detail_is_refused_not_silently_full(self):
        """A typo must not quietly serve the expensive shape forever."""
        from kiln.server import printer_status

        result = printer_status(detail="minimal")
        assert result["success"] is False
        assert result["error"]["code"] == "VALIDATION_ERROR"
        assert "lite" in result["error"]["message"]

    def test_printer_not_found_via_registry(self):
        from kiln.server import print_status_lite, printer_status

        with patch("kiln.server._registry") as mock_registry:
            mock_registry.get.side_effect = PrinterNotFoundError("nope")
            for call in (
                lambda: printer_status(printer_name="ghost"),
                lambda: printer_status(printer_name="ghost", detail="lite"),
                lambda: print_status_lite(printer_name="ghost"),
            ):
                result = call()
                assert result["success"] is False
                assert result["error"]["code"] == "NOT_FOUND"
                assert "ghost" in result["error"]["message"]



class TestListSnapshots:
    """list_snapshots tool tests."""

    @patch("kiln.server.get_db")
    def test_returns_snapshots(self, mock_get_db):
        from kiln.server import list_snapshots

        mock_db = MagicMock()
        mock_db.get_snapshots.return_value = [
            {"id": 1, "printer_name": "voron", "phase": "timelapse"},
        ]
        mock_get_db.return_value = mock_db

        result = list_snapshots(printer_name="voron")
        assert result["success"] is True
        assert result["count"] == 1
        mock_db.get_snapshots.assert_called_once_with(
            job_id=None, printer_name="voron", phase=None, limit=20,
        )

    @patch("kiln.server.get_db")
    def test_empty_result(self, mock_get_db):
        from kiln.server import list_snapshots

        mock_db = MagicMock()
        mock_db.get_snapshots.return_value = []
        mock_get_db.return_value = mock_db

        result = list_snapshots()
        assert result["success"] is True
        assert result["count"] == 0

    @patch("kiln.server.get_db")
    def test_passes_all_filters(self, mock_get_db):
        from kiln.server import list_snapshots

        mock_db = MagicMock()
        mock_db.get_snapshots.return_value = []
        mock_get_db.return_value = mock_db

        list_snapshots(printer_name="v", job_id="j1", phase="timelapse", limit=5)
        mock_db.get_snapshots.assert_called_once_with(
            job_id="j1", printer_name="v", phase="timelapse", limit=5,
        )

    @patch("kiln.server.get_db")
    def test_db_error_returns_error_dict(self, mock_get_db):
        from kiln.server import list_snapshots

        mock_get_db.side_effect = RuntimeError("db exploded")

        result = list_snapshots()
        assert result["success"] is False
        assert "error" in result


class TestWatchPrintSaveToDisk:
    """watch_print save_to_disk parameter tests."""

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._get_adapter")
    def test_save_to_disk_returns_save_dir(self, mock_get_adapter, mock_auth):
        from kiln.server import watch_print

        mock_adapter = MagicMock()
        mock_state = MagicMock()
        mock_state.state = PrinterStatus.PRINTING
        mock_adapter.get_state.return_value = mock_state
        mock_job = MagicMock()
        mock_job.completion = 10.0
        mock_adapter.get_job.return_value = mock_job
        mock_get_adapter.return_value = mock_adapter

        result = watch_print(save_to_disk=True, max_snapshots=1, timeout=5)
        assert result["success"] is True
        assert result["save_to_disk"] is True
        assert "save_dir" in result
        assert "timelapses" in result["save_dir"]

        # Clean up
        from kiln.server import stop_watch_print
        stop_watch_print(result["watch_id"])

    @patch("kiln.server._check_auth", return_value=None)
    @patch("kiln.server._get_adapter")
    def test_default_no_save_dir(self, mock_get_adapter, mock_auth):
        from kiln.server import watch_print

        mock_adapter = MagicMock()
        mock_state = MagicMock()
        mock_state.state = PrinterStatus.PRINTING
        mock_adapter.get_state.return_value = mock_state
        mock_job = MagicMock()
        mock_job.completion = 10.0
        mock_adapter.get_job.return_value = mock_job
        mock_get_adapter.return_value = mock_adapter

        result = watch_print(max_snapshots=1, timeout=5)
        assert result["success"] is True
        assert result["save_to_disk"] is False
        assert "save_dir" not in result

        # Clean up
        from kiln.server import stop_watch_print
        stop_watch_print(result["watch_id"])
