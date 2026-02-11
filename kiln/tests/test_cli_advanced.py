"""Tests for advanced CLI commands not covered by test_cli_main.py.

Covers: snapshot, wait, history, cost, compare-cost, slice, material/*,
level, stream, sync/*, plugins/*, order/*, billing/*.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from kiln.cli.main import cli
from kiln.printers.base import (
    JobProgress,
    PrinterFile,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_adapter():
    """A mock PrinterAdapter with sensible defaults."""
    adapter = MagicMock()
    adapter.name = "mock"
    adapter.get_state.return_value = PrinterState(
        state=PrinterStatus.IDLE, connected=True,
        tool_temp_actual=22.0, tool_temp_target=0.0,
        bed_temp_actual=21.0, bed_temp_target=0.0,
    )
    adapter.get_job.return_value = JobProgress(
        file_name=None, completion=None,
        print_time_seconds=None, print_time_left_seconds=None,
    )
    adapter.list_files.return_value = [
        PrinterFile(name="test.gcode", path="/test.gcode", size_bytes=1024, date=None),
    ]
    adapter.upload_file.return_value = UploadResult(
        success=True, message="Uploaded", file_name="test.gcode",
    )
    adapter.start_print.return_value = PrintResult(
        success=True, message="Print started",
    )
    adapter.capabilities = MagicMock(can_send_gcode=True)
    adapter.get_snapshot.return_value = b"\x89PNG\x00fake_image"
    adapter.get_stream_url.return_value = "http://localhost:8080/webcam/?action=stream"
    return adapter


@pytest.fixture
def config_file(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "active_printer": "test-printer",
        "printers": {
            "test-printer": {
                "type": "moonraker",
                "host": "http://test.local:7125",
            },
        },
        "settings": {"timeout": 30, "retries": 3},
    }))
    return cfg_path


def _patch_adapter(mock_adapter, config_file):
    return (
        patch("kiln.cli.main._make_adapter", return_value=mock_adapter),
        patch("kiln.cli.main.load_printer_config", return_value={
            "type": "moonraker", "host": "http://test.local:7125",
            "timeout": 30, "retries": 3,
        }),
        patch("kiln.cli.main.validate_printer_config", return_value=(True, None)),
    )


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_snapshot_json(self, runner, mock_adapter, config_file):
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["snapshot", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert "image_base64" in data["data"]

    def test_snapshot_save_to_file(self, runner, mock_adapter, config_file, tmp_path):
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        out_path = str(tmp_path / "snap.jpg")
        with p1, p2, p3:
            result = runner.invoke(cli, ["snapshot", "-o", out_path, "--json"])
        assert result.exit_code == 0
        assert Path(out_path).exists()
        assert Path(out_path).read_bytes() == b"\x89PNG\x00fake_image"

    def test_snapshot_no_webcam(self, runner, mock_adapter, config_file):
        mock_adapter.get_snapshot.return_value = None
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["snapshot", "--json"])
        assert result.exit_code != 0

    def test_snapshot_human_output(self, runner, mock_adapter, config_file):
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["snapshot"])
        assert result.exit_code == 0
        assert "Snapshot saved" in result.output


# ---------------------------------------------------------------------------
# wait
# ---------------------------------------------------------------------------


class TestWait:
    def test_wait_immediate_idle(self, runner, mock_adapter, config_file):
        """Printer already idle -> exit 0 immediately."""
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["wait", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["final_state"] == "idle"

    def test_wait_error_state(self, runner, mock_adapter, config_file):
        mock_adapter.get_state.return_value = PrinterState(
            state=PrinterStatus.ERROR, connected=True,
        )
        mock_adapter.get_job.return_value = JobProgress(
            file_name="test.gcode", completion=10.0,
            print_time_seconds=60, print_time_left_seconds=500,
        )
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3:
            result = runner.invoke(cli, ["wait", "--json"])
        assert result.exit_code != 0

    def test_wait_transitions_to_idle(self, runner, mock_adapter, config_file):
        """Printer is printing, then goes idle."""
        mock_adapter.get_state.side_effect = [
            PrinterState(state=PrinterStatus.PRINTING, connected=True,
                         tool_temp_actual=210.0, tool_temp_target=210.0,
                         bed_temp_actual=60.0, bed_temp_target=60.0),
            PrinterState(state=PrinterStatus.IDLE, connected=True,
                         tool_temp_actual=30.0, tool_temp_target=0.0,
                         bed_temp_actual=25.0, bed_temp_target=0.0),
        ]
        mock_adapter.get_job.side_effect = [
            JobProgress(file_name="test.gcode", completion=80.0,
                        print_time_seconds=2000, print_time_left_seconds=500),
            JobProgress(file_name=None, completion=None,
                        print_time_seconds=None, print_time_left_seconds=None),
        ]
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3, patch("time.sleep"):
            result = runner.invoke(cli, ["wait", "--interval", "0.01", "--json"])
        assert result.exit_code == 0
        assert '"idle"' in result.output


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


class TestHistory:
    def test_history_json(self, runner):
        mock_db = MagicMock()
        mock_db.list_jobs.return_value = [
            {"id": "j1", "file_name": "benchy.gcode", "status": "completed"},
        ]
        with patch("kiln.persistence.get_db", return_value=mock_db):
            result = runner.invoke(cli, ["history", "--json"])
        assert result.exit_code == 0
        mock_db.list_jobs.assert_called_once_with(status=None, limit=20)

    def test_history_with_filter(self, runner):
        mock_db = MagicMock()
        mock_db.list_jobs.return_value = []
        with patch("kiln.persistence.get_db", return_value=mock_db):
            result = runner.invoke(cli, ["history", "--status", "failed", "--json"])
        assert result.exit_code == 0
        mock_db.list_jobs.assert_called_once_with(status="failed", limit=20)

    def test_history_with_limit(self, runner):
        mock_db = MagicMock()
        mock_db.list_jobs.return_value = []
        with patch("kiln.persistence.get_db", return_value=mock_db):
            result = runner.invoke(cli, ["history", "-n", "5", "--json"])
        assert result.exit_code == 0
        mock_db.list_jobs.assert_called_once_with(status=None, limit=5)


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------


class TestCost:
    def test_cost_json(self, runner, tmp_path):
        gcode = tmp_path / "model.gcode"
        gcode.write_text("G28\nG1 X10 E5\n")
        mock_estimate = MagicMock()
        mock_estimate.to_dict.return_value = {
            "file_name": "model.gcode", "material": "PLA",
            "filament_length_meters": 12.5, "filament_weight_grams": 37.8,
            "filament_cost_usd": 0.76, "electricity_cost_usd": 0.08,
            "total_cost_usd": 0.84, "estimated_time_seconds": 2400,
            "warnings": [],
        }
        mock_estimator = MagicMock()
        mock_estimator.estimate_from_file.return_value = mock_estimate
        with patch("kiln.cost_estimator.CostEstimator", return_value=mock_estimator):
            result = runner.invoke(cli, ["cost", str(gcode), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["data"]["total_cost_usd"] == 0.84

    def test_cost_human(self, runner, tmp_path):
        gcode = tmp_path / "model.gcode"
        gcode.write_text("G28\nG1 X10 E5\n")
        mock_estimate = MagicMock()
        mock_estimate.file_name = "model.gcode"
        mock_estimate.material = "PLA"
        mock_estimate.filament_length_meters = 12.5
        mock_estimate.filament_weight_grams = 37.8
        mock_estimate.filament_cost_usd = 0.76
        mock_estimate.electricity_cost_usd = 0.08
        mock_estimate.total_cost_usd = 0.84
        mock_estimate.estimated_time_seconds = 2400
        mock_estimate.warnings = []
        mock_estimator = MagicMock()
        mock_estimator.estimate_from_file.return_value = mock_estimate
        with patch("kiln.cost_estimator.CostEstimator", return_value=mock_estimator):
            result = runner.invoke(cli, ["cost", str(gcode)])
        assert result.exit_code == 0
        assert "PLA" in result.output
        assert "$0.84" in result.output

    def test_cost_file_not_found(self, runner):
        result = runner.invoke(cli, ["cost", "/nonexistent.gcode", "--json"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# compare-cost
# ---------------------------------------------------------------------------


class TestCompareCost:
    def test_compare_cost_local_only_json(self, runner, tmp_path):
        gcode = tmp_path / "model.gcode"
        gcode.write_text("G28\n")
        mock_estimate = MagicMock()
        mock_estimate.to_dict.return_value = {"material": "PLA", "total_cost_usd": 1.50}
        mock_estimator = MagicMock()
        mock_estimator.estimate_from_file.return_value = mock_estimate
        with patch("kiln.cost_estimator.CostEstimator", return_value=mock_estimator):
            result = runner.invoke(cli, ["compare-cost", str(gcode), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["local"]["available"] is True
        assert data["data"]["fulfillment"]["available"] is False


# ---------------------------------------------------------------------------
# slice
# ---------------------------------------------------------------------------


class TestSlice:
    def test_slice_json(self, runner, tmp_path):
        stl = tmp_path / "cube.stl"
        stl.write_text("solid cube\nendsolid cube\n")
        mock_result = MagicMock()
        mock_result.message = "Sliced cube.stl"
        mock_result.output_path = str(tmp_path / "cube.gcode")
        mock_result.to_dict.return_value = {
            "input_file": str(stl), "output_path": mock_result.output_path,
            "message": mock_result.message,
        }
        with patch("kiln.slicer.slice_file", return_value=mock_result):
            result = runner.invoke(cli, ["slice", str(stl), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"

    def test_slice_slicer_not_found(self, runner, tmp_path):
        stl = tmp_path / "cube.stl"
        stl.write_text("solid cube\nendsolid cube\n")
        from kiln.slicer import SlicerNotFoundError
        with patch("kiln.slicer.slice_file", side_effect=SlicerNotFoundError("No slicer")):
            result = runner.invoke(cli, ["slice", str(stl), "--json"])
        assert result.exit_code != 0
        assert "SLICER_NOT_FOUND" in result.output

    def test_slice_print_after(self, runner, mock_adapter, config_file, tmp_path):
        stl = tmp_path / "model.stl"
        stl.write_text("solid\nendsolid\n")
        gcode_out = tmp_path / "model.gcode"
        gcode_out.write_text("G28\n")
        mock_result = MagicMock()
        mock_result.message = "Sliced"
        mock_result.output_path = str(gcode_out)
        mock_result.to_dict.return_value = {"output_path": str(gcode_out)}
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3, patch("kiln.slicer.slice_file", return_value=mock_result):
            result = runner.invoke(cli, ["slice", str(stl), "--print-after", "--json"])
        assert result.exit_code == 0
        mock_adapter.upload_file.assert_called_once()
        mock_adapter.start_print.assert_called_once()


# ---------------------------------------------------------------------------
# material subcommands
# ---------------------------------------------------------------------------


class TestMaterial:
    def test_material_set_json(self, runner):
        mock_tracker = MagicMock()
        mock_mat = MagicMock()
        mock_mat.to_dict.return_value = {
            "material_type": "PLA", "color": "red", "tool_index": 0,
        }
        mock_tracker.set_material.return_value = mock_mat
        with patch("kiln.materials.MaterialTracker", return_value=mock_tracker), \
             patch("kiln.persistence.get_db"):
            result = runner.invoke(cli, [
                "material", "set", "-t", "PLA", "-c", "red", "--json",
            ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["data"]["material_type"] == "PLA"

    def test_material_show_json(self, runner):
        mock_tracker = MagicMock()
        mock_mat = MagicMock()
        mock_mat.to_dict.return_value = {"material_type": "PETG", "tool_index": 0}
        mock_tracker.get_all_materials.return_value = [mock_mat]
        with patch("kiln.materials.MaterialTracker", return_value=mock_tracker), \
             patch("kiln.persistence.get_db"):
            result = runner.invoke(cli, ["material", "show", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"

    def test_material_show_empty(self, runner):
        mock_tracker = MagicMock()
        mock_tracker.get_all_materials.return_value = []
        with patch("kiln.materials.MaterialTracker", return_value=mock_tracker), \
             patch("kiln.persistence.get_db"):
            result = runner.invoke(cli, ["material", "show"])
        assert result.exit_code == 0
        assert "No materials loaded" in result.output

    def test_material_spools_json(self, runner):
        mock_tracker = MagicMock()
        mock_tracker.list_spools.return_value = []
        with patch("kiln.materials.MaterialTracker", return_value=mock_tracker), \
             patch("kiln.persistence.get_db"):
            result = runner.invoke(cli, ["material", "spools", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"] == []

    def test_material_add_spool_json(self, runner):
        mock_tracker = MagicMock()
        mock_spool = MagicMock()
        mock_spool.id = "spool-123"
        mock_spool.to_dict.return_value = {
            "id": "spool-123", "material_type": "PLA",
            "weight_grams": 1000.0, "remaining_grams": 1000.0,
        }
        mock_tracker.add_spool.return_value = mock_spool
        with patch("kiln.materials.MaterialTracker", return_value=mock_tracker), \
             patch("kiln.persistence.get_db"):
            result = runner.invoke(cli, [
                "material", "add-spool", "-t", "PLA", "--weight", "1000", "--json",
            ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["id"] == "spool-123"


# ---------------------------------------------------------------------------
# level
# ---------------------------------------------------------------------------


class TestLevel:
    def test_level_status_json(self, runner):
        mock_status = MagicMock()
        mock_status.to_dict.return_value = {
            "printer_name": "default", "needs_leveling": False,
            "trigger_reason": None, "prints_since_level": 3,
            "last_leveled_at": None,
        }
        mock_mgr = MagicMock()
        mock_mgr.check_needed.return_value = mock_status
        with patch("kiln.bed_leveling.BedLevelManager", return_value=mock_mgr), \
             patch("kiln.persistence.get_db"):
            result = runner.invoke(cli, ["level", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["needs_leveling"] is False

    def test_level_set_prints(self, runner):
        mock_policy = MagicMock()
        mock_mgr = MagicMock()
        mock_mgr.get_policy.return_value = mock_policy
        mock_status = MagicMock()
        mock_status.to_dict.return_value = {
            "needs_leveling": False, "prints_since_level": 0,
            "printer_name": "default", "trigger_reason": None,
            "last_leveled_at": None,
        }
        mock_mgr.check_needed.return_value = mock_status
        with patch("kiln.bed_leveling.BedLevelManager", return_value=mock_mgr), \
             patch("kiln.persistence.get_db"):
            result = runner.invoke(cli, ["level", "--set-prints", "10", "--json"])
        assert result.exit_code == 0
        mock_mgr.set_policy.assert_called_once()

    def test_level_trigger(self, runner, mock_adapter, config_file):
        mock_mgr = MagicMock()
        mock_mgr.trigger_level.return_value = {"message": "Bed leveling triggered"}
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3, \
             patch("kiln.bed_leveling.BedLevelManager", return_value=mock_mgr), \
             patch("kiln.persistence.get_db"):
            result = runner.invoke(cli, ["level", "--trigger", "--json"])
        assert result.exit_code == 0
        mock_mgr.trigger_level.assert_called_once()


# ---------------------------------------------------------------------------
# stream
# ---------------------------------------------------------------------------


class TestStream:
    def test_stream_stop(self, runner):
        mock_info = MagicMock()
        mock_info.to_dict.return_value = {"active": False, "clients": 0}
        mock_proxy = MagicMock()
        mock_proxy.stop.return_value = mock_info
        with patch("kiln.streaming.MJPEGProxy", return_value=mock_proxy):
            result = runner.invoke(cli, ["stream", "--stop", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"

    def test_stream_no_webcam(self, runner, mock_adapter, config_file):
        mock_adapter.get_stream_url.return_value = None
        p1, p2, p3 = _patch_adapter(mock_adapter, config_file)
        with p1, p2, p3, patch("kiln.streaming.MJPEGProxy", return_value=MagicMock()):
            result = runner.invoke(cli, ["stream", "--json"])
        assert result.exit_code != 0
        assert "NO_STREAM" in result.output or "not available" in result.output.lower()


# ---------------------------------------------------------------------------
# sync subcommands
# ---------------------------------------------------------------------------


class TestSync:
    def test_sync_status_json(self, runner):
        result = runner.invoke(cli, ["sync", "status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"

    def test_sync_now(self, runner):
        result = runner.invoke(cli, ["sync", "now"])
        assert result.exit_code == 0

    def test_sync_configure_json(self, runner):
        mock_db = MagicMock()
        config_dict = {
            "cloud_url": "https://api.example.com",
            "api_key": "key123", "sync_interval_seconds": 30.0,
        }
        mock_config = MagicMock()
        mock_config.to_dict.return_value = config_dict
        with patch("kiln.persistence.get_db", return_value=mock_db), \
             patch("kiln.cloud_sync.SyncConfig", return_value=mock_config), \
             patch("dataclasses.asdict", return_value=config_dict):
            result = runner.invoke(cli, [
                "sync", "configure",
                "--url", "https://api.example.com",
                "--api-key", "key123",
                "--interval", "30", "--json",
            ])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# plugins subcommands
# ---------------------------------------------------------------------------


class TestPlugins:
    def test_plugins_list_empty(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.discover.return_value = []
        with patch("kiln.plugins.PluginManager", return_value=mock_mgr):
            result = runner.invoke(cli, ["plugins", "list"])
        assert result.exit_code == 0
        assert "No plugins found" in result.output

    def test_plugins_list_json(self, runner):
        mock_plugin = MagicMock()
        mock_plugin.to_dict.return_value = {
            "name": "test-plugin", "version": "1.0", "active": True,
        }
        mock_mgr = MagicMock()
        mock_mgr.discover.return_value = [mock_plugin]
        with patch("kiln.plugins.PluginManager", return_value=mock_mgr):
            result = runner.invoke(cli, ["plugins", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["data"]) == 1

    def test_plugins_info_not_found(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.discover.return_value = []
        mock_mgr.get_plugin_info.return_value = None
        with patch("kiln.plugins.PluginManager", return_value=mock_mgr):
            result = runner.invoke(cli, ["plugins", "info", "nonexistent", "--json"])
        assert result.exit_code != 0

    def test_plugins_info_found(self, runner):
        mock_info = MagicMock()
        mock_info.name = "my-plugin"
        mock_info.version = "2.0"
        mock_info.active = True
        mock_info.description = "A test plugin"
        mock_info.hooks = ["pre_print"]
        mock_info.error = None
        mock_info.to_dict.return_value = {
            "name": "my-plugin", "version": "2.0", "active": True,
            "description": "A test plugin", "hooks": ["pre_print"],
            "error": None,
        }
        mock_mgr = MagicMock()
        mock_mgr.discover.return_value = []
        mock_mgr.get_plugin_info.return_value = mock_info
        with patch("kiln.plugins.PluginManager", return_value=mock_mgr):
            result = runner.invoke(cli, ["plugins", "info", "my-plugin", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["name"] == "my-plugin"


# ---------------------------------------------------------------------------
# order subcommands
# ---------------------------------------------------------------------------


class TestOrder:
    def test_order_materials_json(self, runner):
        provider = MagicMock()
        mock_mat = MagicMock()
        mock_mat.to_dict.return_value = {"id": "pla-white", "name": "PLA White"}
        provider.list_materials.return_value = [mock_mat]
        with patch("kiln.cli.main._get_fulfillment_provider", return_value=provider):
            result = runner.invoke(cli, ["order", "materials", "--json"])
        assert result.exit_code == 0

    def test_order_materials_no_api_key(self, runner):
        with patch("kiln.cli.main._get_fulfillment_provider",
                   side_effect=SystemExit(1)):
            result = runner.invoke(cli, ["order", "materials", "--json"])
        assert result.exit_code != 0

    def test_order_quote_json(self, runner, tmp_path):
        gcode = tmp_path / "model.gcode"
        gcode.write_text("G28\n")
        provider = MagicMock()
        mock_quote = MagicMock()
        mock_quote.total_price = 25.00
        mock_quote.currency = "USD"
        mock_quote.to_dict.return_value = {
            "total_price": 25.00, "unit_price": 25.00,
            "material": "PLA", "lead_time_days": 5,
        }
        provider.get_quote.return_value = mock_quote
        mock_ledger = MagicMock()
        mock_fee = MagicMock()
        mock_fee.to_dict.return_value = {"fee_usd": 2.50, "rate": 0.10}
        mock_fee.total_cost = 27.50  # Must be a real number for JSON serialization
        mock_ledger.calculate_fee.return_value = mock_fee
        with patch("kiln.cli.main._get_fulfillment_provider", return_value=provider), \
             patch("kiln.billing.BillingLedger", return_value=mock_ledger):
            result = runner.invoke(cli, [
                "order", "quote", str(gcode), "-m", "pla-white", "--json",
            ])
        assert result.exit_code == 0

    def test_order_status_json(self, runner):
        provider = MagicMock()
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {"order_id": "ord-1", "status": "in_production"}
        provider.get_order_status.return_value = mock_result
        with patch("kiln.cli.main._get_fulfillment_provider", return_value=provider):
            result = runner.invoke(cli, ["order", "status", "ord-1", "--json"])
        assert result.exit_code == 0

    def test_order_cancel_json(self, runner):
        provider = MagicMock()
        mock_result = MagicMock()
        mock_result.to_dict.return_value = {"order_id": "ord-1", "status": "cancelled"}
        provider.cancel_order.return_value = mock_result
        with patch("kiln.cli.main._get_fulfillment_provider", return_value=provider):
            result = runner.invoke(cli, ["order", "cancel", "ord-1", "--json"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# billing subcommands
# ---------------------------------------------------------------------------


class TestBilling:
    def test_billing_setup_stripe_json(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_setup_url.return_value = "https://checkout.stripe.com/test"
        mock_provider = MagicMock()
        with patch("kiln.cli.config.get_billing_config", return_value={"user_id": "u1"}), \
             patch("kiln.cli.config.get_or_create_user_id", return_value="u1"), \
             patch("kiln.persistence.get_db"), \
             patch("kiln.payments.manager.PaymentManager", return_value=mock_mgr), \
             patch("kiln.payments.stripe_provider.StripeProvider", return_value=mock_provider):
            result = runner.invoke(cli, ["billing", "setup", "--json"])
        assert result.exit_code == 0
        mock_mgr.register_provider.assert_called_once()

    def test_billing_status_json(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_billing_status.return_value = {
            "user_id": "u1", "has_payment_method": True,
            "monthly_spend_usd": 12.50,
        }
        with patch("kiln.cli.config.get_billing_config", return_value={"user_id": "u1"}), \
             patch("kiln.cli.config.get_or_create_user_id", return_value="u1"), \
             patch("kiln.persistence.get_db"), \
             patch("kiln.payments.manager.PaymentManager", return_value=mock_mgr):
            result = runner.invoke(cli, ["billing", "status", "--json"])
        assert result.exit_code == 0

    def test_billing_history_json(self, runner):
        mock_mgr = MagicMock()
        mock_mgr.get_billing_history.return_value = []
        with patch("kiln.cli.config.get_billing_config", return_value={}), \
             patch("kiln.persistence.get_db"), \
             patch("kiln.payments.manager.PaymentManager", return_value=mock_mgr):
            result = runner.invoke(cli, ["billing", "history", "--json"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# help for all new commands
# ---------------------------------------------------------------------------


class TestCommandHelp:
    """Verify all advanced commands accept --help without error."""

    @pytest.mark.parametrize("cmd", [
        ["snapshot", "--help"],
        ["wait", "--help"],
        ["history", "--help"],
        ["cost", "--help"],
        ["compare-cost", "--help"],
        ["slice", "--help"],
        ["level", "--help"],
        ["stream", "--help"],
        ["material", "--help"],
        ["material", "set", "--help"],
        ["material", "show", "--help"],
        ["material", "spools", "--help"],
        ["material", "add-spool", "--help"],
        ["sync", "--help"],
        ["sync", "status", "--help"],
        ["sync", "now", "--help"],
        ["sync", "configure", "--help"],
        ["plugins", "--help"],
        ["plugins", "list", "--help"],
        ["plugins", "info", "--help"],
        ["order", "--help"],
        ["order", "materials", "--help"],
        ["order", "quote", "--help"],
        ["order", "place", "--help"],
        ["order", "status", "--help"],
        ["order", "cancel", "--help"],
        ["billing", "--help"],
        ["billing", "setup", "--help"],
        ["billing", "status", "--help"],
        ["billing", "history", "--help"],
    ])
    def test_help(self, runner, cmd):
        result = runner.invoke(cli, cmd)
        assert result.exit_code == 0, f"{' '.join(cmd)} failed: {result.output}"
