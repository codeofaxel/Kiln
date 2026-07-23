"""Tests for miscellaneous server.py MCP tools that lack dedicated tests.

Covers:
- annotate_print — happy path, not found, update failure
- confirm_action — happy path, invalid token, expired token
- export_safety_profile — happy path, not found
- get_material_recommendation — happy path, no material, no intel
- get_printer_intelligence — happy path, not found
- set_autonomy_level — valid levels, invalid level
- set_leveling_policy — happy path, error
- set_printer_light — happy path, unsupported, error
- trigger_bed_level — happy path, printer error
- troubleshoot_printer — happy path, not found
- upload_file_confirm — happy path, invalid token, file not found
- save_print_checkpoint — happy path, error
- start_printer_health_monitoring — happy path, error
- stop_printer_health_monitoring — happy path, error
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bypass_auth():
    """Disable auth for all tests."""
    with patch("kiln.server._check_auth", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _bypass_rate_limit():
    """Disable rate limiting for all tests."""
    with patch("kiln.server._check_rate_limit", return_value=None):
        yield


# ---------------------------------------------------------------------------
# TestAnnotatePrint
# ---------------------------------------------------------------------------


class TestAnnotatePrint:
    """Tests for annotate_print()."""

    @patch("kiln.server.get_db")
    def test_happy_path(self, mock_db):
        from kiln.server import annotate_print

        mock_db.return_value.get_print_record.return_value = {"job_id": "j1"}
        mock_db.return_value.update_print_notes.return_value = True

        result = annotate_print(job_id="j1", notes="Great print quality")

        assert result["success"] is True
        assert result["notes"] == "Great print quality"

    @patch("kiln.server.get_db")
    def test_not_found(self, mock_db):
        from kiln.server import annotate_print

        mock_db.return_value.get_print_record.return_value = None

        result = annotate_print(job_id="bad", notes="anything")

        assert result["success"] is False
        assert "NOT_FOUND" in str(result)

    @patch("kiln.server.get_db")
    def test_update_failure(self, mock_db):
        from kiln.server import annotate_print

        mock_db.return_value.get_print_record.return_value = {"job_id": "j1"}
        mock_db.return_value.update_print_notes.return_value = False

        result = annotate_print(job_id="j1", notes="test")

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestConfirmAction
# ---------------------------------------------------------------------------


class TestConfirmAction:
    """Tests for confirm_action()."""

    @patch("kiln.server.mcp")
    @patch("kiln.server._pending_confirmations", new_callable=dict)
    @patch("kiln.server._audit")
    def test_happy_path(self, _audit, pending, mock_mcp):
        from kiln.server import confirm_action

        mock_fn = MagicMock()
        mock_fn.fn.return_value = {"success": True, "message": "executed"}
        mock_mcp._tool_manager._tools = {"test_tool": mock_fn}

        pending["tok-1"] = {
            "tool": "test_tool",
            "args": {"x": 1},
            "created_at": time.time(),
        }

        result = confirm_action(token="tok-1")

        assert result["success"] is True

    def test_invalid_token(self):
        from kiln.server import confirm_action

        result = confirm_action(token="nonexistent")

        assert result["success"] is False
        assert "Invalid" in str(result)

    @patch("kiln.server._pending_confirmations", new_callable=dict)
    def test_expired_token(self, pending):
        from kiln.server import confirm_action

        pending["tok-old"] = {
            "tool": "test_tool",
            "args": {},
            "created_at": time.time() - 600,  # 10 minutes ago
        }

        result = confirm_action(token="tok-old")

        assert result["success"] is False
        assert "expired" in str(result).lower()


# ---------------------------------------------------------------------------
# TestExportSafetyProfile
# ---------------------------------------------------------------------------


class TestExportSafetyProfile:
    """Tests for export_safety_profile()."""

    @patch("kiln.server._export_profile")
    def test_happy_path(self, mock_export):
        from kiln.server import export_safety_profile

        mock_export.return_value = {"max_hotend": 260, "max_bed": 100}

        result = export_safety_profile(printer_model="ender3")

        assert result["success"] is True
        assert result["profile"]["max_hotend"] == 260

    @patch("kiln.server._export_profile")
    def test_not_found(self, mock_export):
        from kiln.server import export_safety_profile

        mock_export.side_effect = KeyError("no profile")

        result = export_safety_profile(printer_model="bogus")

        assert result["success"] is False
        assert "NOT_FOUND" in str(result)


# ---------------------------------------------------------------------------
# TestGetPrinterIntelligence
# ---------------------------------------------------------------------------


class TestGetPrinterIntelligence:
    """Tests for the internal whole-record printer helper."""

    def test_not_registered_as_mcp_tool(self):
        from kiln.server import mcp

        registered = {tool.name for tool in mcp._tool_manager.list_tools()}
        assert "get_printer_intelligence" not in registered

    @patch("kiln.server.intel_to_dict")
    @patch("kiln.server.get_printer_intel")
    def test_happy_path(self, mock_intel, mock_to_dict):
        from kiln.server import get_printer_intelligence

        mock_intel.return_value = MagicMock()
        mock_to_dict.return_value = {"display_name": "Ender 3", "quirks": []}

        result = get_printer_intelligence(printer_id="ender3")

        assert result["success"] is True
        assert "intel" in result

    @patch("kiln.server.get_printer_intel")
    def test_not_found(self, mock_intel):
        from kiln.server import get_printer_intelligence

        mock_intel.side_effect = KeyError("no data")

        result = get_printer_intelligence(printer_id="bogus")

        assert result["success"] is False
        assert "NOT_FOUND" in str(result)


# ---------------------------------------------------------------------------
# TestGetMaterialRecommendation
# ---------------------------------------------------------------------------


class TestGetMaterialRecommendation:
    """Tests for get_material_recommendation()."""

    @patch("kiln.server.get_printer_intel")
    @patch("kiln.server.get_material_settings")
    def test_happy_path(self, mock_settings, mock_intel):
        from kiln.server import get_material_recommendation

        mp = MagicMock()
        mp.hotend = 215
        mp.bed = 60
        mp.fan = "100%"
        mp.notes = "Standard PLA settings"
        mock_settings.return_value = mp

        intel = MagicMock()
        intel.display_name = "Ender 3"
        mock_intel.return_value = intel

        result = get_material_recommendation(printer_id="ender3", material="PLA")

        assert result["success"] is True
        assert result["hotend_temp"] == 215

    @patch("kiln.server.get_printer_intel")
    @patch("kiln.server.get_material_settings")
    def test_no_material(self, mock_settings, mock_intel):
        from kiln.server import get_material_recommendation

        mock_settings.return_value = None
        intel = MagicMock()
        intel.display_name = "Ender 3"
        intel.materials = {"PLA": MagicMock(), "PETG": MagicMock()}
        mock_intel.return_value = intel

        result = get_material_recommendation(printer_id="ender3", material="WOOD")

        assert result["success"] is False

    @patch("kiln.server.get_material_settings")
    def test_no_intel(self, mock_settings):
        from kiln.server import get_material_recommendation

        mock_settings.side_effect = KeyError("no data")

        result = get_material_recommendation(printer_id="bogus", material="PLA")

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestSetAutonomyLevel
# ---------------------------------------------------------------------------


class TestSetAutonomyLevel:
    """Tests for set_autonomy_level()."""

    @patch("kiln.autonomy.save_autonomy_config")
    @patch("kiln.autonomy.load_autonomy_config")
    def test_valid_level(self, mock_load, mock_save):
        from kiln.server import set_autonomy_level

        mock_config = MagicMock()
        mock_config.constraints = {}
        mock_load.return_value = mock_config

        mock_new = MagicMock()
        mock_new.to_dict.return_value = {"level": 1, "name": "pre_screened"}

        with patch("kiln.autonomy.AutonomyConfig", return_value=mock_new):
            result = set_autonomy_level(level=1)

        assert result["success"] is True

    def test_invalid_level(self):
        from kiln.server import set_autonomy_level

        result = set_autonomy_level(level=99)

        assert result["success"] is False
        assert "Invalid autonomy level" in str(result)


# ---------------------------------------------------------------------------
# TestSetLevelingPolicy
# ---------------------------------------------------------------------------


class TestSetLevelingPolicy:
    """Tests for set_leveling_policy()."""

    @patch("kiln.server._get_bed_level_mgr")
    def test_happy_path(self, mock_mgr):
        from kiln.server import set_leveling_policy

        result = set_leveling_policy(
            enabled=True,
            max_prints=20,
            max_hours=72.0,
        )

        assert result["success"] is True
        assert "policy" in result

    @patch("kiln.server._get_bed_level_mgr")
    def test_error(self, mock_mgr):
        from kiln.server import set_leveling_policy

        mock_mgr.side_effect = RuntimeError("boom")

        result = set_leveling_policy()

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestSetPrinterLight
# ---------------------------------------------------------------------------


class TestSetPrinterLight:
    """Tests for set_printer_light()."""

    @patch("kiln.server._audit")
    @patch("kiln.server._get_adapter")
    def test_happy_path(self, mock_adapter, _audit):
        from kiln.server import set_printer_light

        adapter = MagicMock()
        adapter.set_light.return_value = True
        mock_adapter.return_value = adapter

        result = set_printer_light(node="chamber_light", mode="on")

        assert result["success"] is True
        assert result["accepted"] is True

    @patch("kiln.server._get_adapter")
    def test_unsupported(self, mock_adapter):
        from kiln.server import set_printer_light

        adapter = MagicMock(spec=[])  # no set_light attribute
        mock_adapter.return_value = adapter

        result = set_printer_light()

        assert result["success"] is False
        assert "UNSUPPORTED" in str(result)

    @patch("kiln.server._get_adapter")
    def test_printer_error(self, mock_adapter):
        from kiln.printers.base import PrinterError
        from kiln.server import set_printer_light

        mock_adapter.side_effect = PrinterError("offline")

        result = set_printer_light()

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestTriggerBedLevel
# ---------------------------------------------------------------------------


class TestTriggerBedLevel:
    """Tests for trigger_bed_level()."""

    @patch("kiln.server._get_bed_level_mgr")
    @patch("kiln.server._get_adapter")
    def test_happy_path(self, mock_adapter, mock_mgr):
        from kiln.server import trigger_bed_level

        mock_mgr.return_value.trigger_level.return_value = {
            "success": True,
            "message": "Bed leveling triggered.",
        }

        result = trigger_bed_level()

        assert result["success"] is True

    @patch("kiln.server._get_adapter")
    def test_printer_error(self, mock_adapter):
        from kiln.printers.base import PrinterError
        from kiln.server import trigger_bed_level

        mock_adapter.side_effect = PrinterError("offline")

        result = trigger_bed_level()

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestTroubleshootPrinter
# ---------------------------------------------------------------------------


class TestTroubleshootPrinter:
    """Tests for troubleshoot_printer()."""

    @patch("kiln.server.get_printer_intel")
    @patch("kiln.server.diagnose_issue")
    def test_happy_path(self, mock_diagnose, mock_intel):
        from kiln.server import troubleshoot_printer

        mock_diagnose.return_value = [
            {"symptom": "stringing", "fix": "lower temp"},
        ]
        intel = MagicMock()
        intel.display_name = "Ender 3"
        intel.quirks = ["bowden_tube"]
        mock_intel.return_value = intel

        result = troubleshoot_printer(printer_id="ender3", symptom="stringing")

        assert result["success"] is True
        assert result["count"] == 1
        assert "quirks" not in result

    @patch("kiln.server.diagnose_issue")
    def test_not_found(self, mock_diagnose):
        from kiln.server import troubleshoot_printer

        mock_diagnose.side_effect = KeyError("no data")

        result = troubleshoot_printer(printer_id="bogus", symptom="stringing")

        assert result["success"] is False

    @patch("kiln.server.get_printer_intel")
    @patch("kiln.server.diagnose_issue")
    def test_hms_code_adds_normalized_code_and_wiki_pointer(self, mock_diagnose, mock_intel):
        from kiln.server import troubleshoot_printer

        mock_diagnose.return_value = []
        intel = MagicMock()
        intel.display_name = "Bambu X1C"
        intel.quirks = []
        mock_intel.return_value = intel

        # Mixed separators + case still normalize to underscore-joined upper hex.
        result = troubleshoot_printer(
            printer_id="bambu_x1c", symptom="", hms_code="0300-1a00-0002-0001"
        )

        assert result["success"] is True
        assert result["hms_code"] == "0300_1A00_0002_0001"
        assert result["hms_wiki_url"].endswith("/hmscode/0300_1A00_0002_0001")
        # The free floor never carries a curated cause/fix — that is Pro+ only.
        assert "hms_decoded" not in result

    @patch("kiln.server.get_printer_intel")
    @patch("kiln.server.diagnose_issue")
    def test_non_code_hms_input_is_ignored(self, mock_diagnose, mock_intel):
        from kiln.server import troubleshoot_printer

        mock_diagnose.return_value = []
        intel = MagicMock()
        intel.display_name = "Bambu X1C"
        intel.quirks = []
        mock_intel.return_value = intel

        result = troubleshoot_printer(
            printer_id="bambu_x1c", symptom="clogged nozzle", hms_code="not-a-code"
        )

        assert result["success"] is True
        assert "hms_code" not in result
        assert "hms_wiki_url" not in result

    def test_no_symptom_or_code_is_guarded(self):
        from kiln.server import troubleshoot_printer

        result = troubleshoot_printer(printer_id="ender3", symptom="", hms_code="")

        assert result.get("success") is not True
        assert result["error"]["code"] == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# TestUploadFileConfirm
# ---------------------------------------------------------------------------


class TestUploadFileConfirm:
    """Tests for upload_file_confirm()."""

    @patch("kiln.server._get_adapter")
    @patch("kiln.server._pending_uploads", new_callable=dict)
    def test_happy_path(self, pending, mock_adapter):
        from kiln.server import upload_file_confirm

        pending["tok-1"] = "/tmp/model.gcode"
        upload_result = MagicMock()
        upload_result.to_dict.return_value = {"success": True, "filename": "model.gcode"}
        mock_adapter.return_value.upload_file.return_value = upload_result

        result = upload_file_confirm(token="tok-1")

        assert result["success"] is True

    def test_invalid_token(self):
        from kiln.server import upload_file_confirm

        result = upload_file_confirm(token="nonexistent")

        assert result["success"] is False
        assert "INVALID_TOKEN" in str(result)

    @patch("kiln.server._get_adapter")
    @patch("kiln.server._pending_uploads", new_callable=dict)
    def test_file_not_found(self, pending, mock_adapter):
        from kiln.server import upload_file_confirm

        pending["tok-2"] = "/tmp/deleted.gcode"
        mock_adapter.return_value.upload_file.side_effect = FileNotFoundError("gone")

        result = upload_file_confirm(token="tok-2")

        assert result["success"] is False
        assert "FILE_NOT_FOUND" in str(result)


# ---------------------------------------------------------------------------
# TestSavePrintCheckpoint
# ---------------------------------------------------------------------------


class TestSavePrintCheckpoint:
    """Tests for save_print_checkpoint() — now wraps PrintRecovery."""

    def test_happy_path(self):
        from kiln.print_recovery import PrintRecovery
        from kiln.server import save_print_checkpoint

        engine = PrintRecovery()
        with patch(
            "kiln.print_recovery.get_recovery_engine",
            return_value=engine,
        ):
            result = save_print_checkpoint(
                printer_name="ender3",
                job_id="j1",
                z_height=10.5,
                layer_number=42,
                hotend_temp=205.0,
                bed_temp=60.0,
            )

        assert result["success"] is True
        assert result["checkpoint"]["z_height_mm"] == 10.5
        assert result["checkpoint"]["layer_number"] == 42
        assert result["checkpoint"]["hotend_temp_c"] == 205.0
        # Stash should have it under the (printer, job) key.
        assert engine.get_latest_checkpoint("ender3", "j1") is not None

    def test_validation_error_on_empty_printer(self):
        from kiln.print_recovery import PrintRecovery
        from kiln.server import save_print_checkpoint

        with patch(
            "kiln.print_recovery.get_recovery_engine",
            return_value=PrintRecovery(),
        ):
            result = save_print_checkpoint(printer_name="", job_id="j1")
        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestStartPrinterHealthMonitoring
# ---------------------------------------------------------------------------


class TestStartPrinterHealthMonitoring:
    """Tests for start_printer_health_monitoring()."""

    @patch("kiln.print_health_monitor.get_print_health_monitor")
    def test_happy_path(self, mock_mon):
        from kiln.server import start_printer_health_monitoring

        result = start_printer_health_monitoring(printer_name="ender3")

        assert result["success"] is True
        assert result["printer"] == "ender3"

    @patch("kiln.print_health_monitor.get_print_health_monitor")
    def test_error(self, mock_mon):
        from kiln.server import start_printer_health_monitoring

        mock_mon.return_value.start_monitoring.side_effect = RuntimeError("boom")

        result = start_printer_health_monitoring(printer_name="ender3")

        assert result["success"] is False


# ---------------------------------------------------------------------------
# TestStopPrinterHealthMonitoring
# ---------------------------------------------------------------------------


class TestStopPrinterHealthMonitoring:
    """Tests for stop_printer_health_monitoring()."""

    @patch("kiln.print_health_monitor.get_print_health_monitor")
    def test_happy_path(self, mock_mon):
        from kiln.server import stop_printer_health_monitoring

        result = stop_printer_health_monitoring(printer_name="ender3")

        assert result["success"] is True
        assert result["monitoring"] == "stopped"

    @patch("kiln.print_health_monitor.get_print_health_monitor")
    def test_error(self, mock_mon):
        from kiln.server import stop_printer_health_monitoring

        mock_mon.return_value.stop_monitoring.side_effect = RuntimeError("boom")

        result = stop_printer_health_monitoring(printer_name="ender3")

        assert result["success"] is False
