"""End-to-end integration tests covering the full agent workflow and
cross-subsystem seams.

Part 1 — CLI Pipeline:
    discover → configure → slice → upload → print → wait → history

Part 2 — Subsystem Seams:
    Queue → Scheduler → Printer (job lifecycle)
    Billing → Fulfillment → Refund (saga)
    Event bus → Webhook delivery
    Safety → Print flow (preflight gating)
    Fleet registry → Scheduling (capability matching)

Each piece works in isolation (unit-tested elsewhere), but these tests
verify the modules compose correctly across subsystem boundaries.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from kiln.billing import BillingLedger, FeeCalculation, FeePolicy
from kiln.cli.main import cli
from kiln.events import Event, EventBus, EventType
from kiln.gcode import validate_gcode, validate_gcode_for_printer
from kiln.printers.base import (
    JobProgress,
    PrinterCapabilities,
    PrinterFile,
    PrinterState,
    PrinterStatus,
    PrintResult,
    UploadResult,
)
from kiln.queue import JobStatus, PrintQueue
from kiln.registry import PrinterRegistry
from kiln.scheduler import JobScheduler
from kiln.webhooks import WebhookManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def tmp_config(tmp_path):
    """A temporary config directory with no printers configured."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "printers": {},
        "settings": {"timeout": 30, "retries": 3},
    }))
    return cfg_path


@pytest.fixture
def mock_adapter():
    """A mock PrinterAdapter that simulates a working printer."""
    adapter = MagicMock()
    adapter.name = "mock"
    adapter.get_state.return_value = PrinterState(
        state=PrinterStatus.IDLE,
        connected=True,
        tool_temp_actual=22.0,
        tool_temp_target=0.0,
        bed_temp_actual=21.0,
        bed_temp_target=0.0,
    )
    adapter.get_job.return_value = JobProgress(
        file_name=None,
        completion=None,
        print_time_seconds=None,
        print_time_left_seconds=None,
    )
    adapter.list_files.return_value = [
        PrinterFile(name="benchy.gcode", path="/benchy.gcode", size_bytes=50000, date=None),
    ]
    adapter.upload_file.return_value = UploadResult(
        success=True, message="Uploaded model.gcode", file_name="model.gcode",
    )
    adapter.start_print.return_value = PrintResult(
        success=True, message="Print started: model.gcode",
    )
    adapter.cancel_print.return_value = PrintResult(
        success=True, message="Print cancelled.",
    )
    adapter.pause_print.return_value = PrintResult(
        success=True, message="Print paused.",
    )
    adapter.resume_print.return_value = PrintResult(
        success=True, message="Print resumed.",
    )
    adapter.set_tool_temp.return_value = True
    adapter.set_bed_temp.return_value = True
    adapter.send_gcode.return_value = None
    adapter.capabilities = MagicMock(can_send_gcode=True)
    adapter.get_snapshot.return_value = b"\x89PNG\x00fake_image_data"
    adapter.get_stream_url.return_value = None
    return adapter


def _make_patches(mock_adapter, config_file):
    """Return context managers patching adapter creation and config loading."""
    return (
        patch("kiln.cli.main._make_adapter", return_value=mock_adapter),
        patch("kiln.cli.main.load_printer_config", return_value={
            "type": "moonraker",
            "host": "http://test.local:7125",
            "timeout": 30,
            "retries": 3,
        }),
        patch("kiln.cli.main.validate_printer_config", return_value=(True, None)),
    )


# ---------------------------------------------------------------------------
# Integration test: full pipeline
# ---------------------------------------------------------------------------


class TestFullPipeline:
    """Simulate the agent workflow: discover → auth → preflight → upload → print → wait."""

    def test_discover_then_auth(self, runner, tmp_path):
        """Step 1-2: Discover a printer, then authenticate with it."""
        from kiln.cli.discovery import DiscoveredPrinter

        # Step 1: Discover
        found = [DiscoveredPrinter(
            name="Voron", printer_type="moonraker",
            host="http://192.168.1.50:7125", port=7125,
        )]
        with patch("kiln.cli.discovery.discover_printers", return_value=found):
            result = runner.invoke(cli, ["discover", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["data"]["count"] == 1
        printer = data["data"]["printers"][0]
        assert printer["name"] == "Voron"
        assert printer["printer_type"] == "moonraker"

        # Step 2: Auth with discovered printer
        cfg_path = tmp_path / "config.yaml"
        with patch("kiln.cli.main.save_printer", return_value=cfg_path):
            result = runner.invoke(cli, [
                "auth",
                "--name", printer["name"],
                "--host", printer["host"],
                "--type", printer["printer_type"],
                "--json",
            ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert data["data"]["name"] == "Voron"

    def test_preflight_then_upload_then_print(self, runner, mock_adapter, tmp_path):
        """Step 3-5: Preflight check, upload a file, start printing."""
        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        with p1, p2, p3:
            # Step 3: Preflight
            result = runner.invoke(cli, ["preflight", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["data"]["ready"] is True

            # Step 4: Upload a file
            gcode = tmp_path / "model.gcode"
            gcode.write_text(";TYPE:External perimeter\nG28\nG1 X10 Y10 Z0.2 F3000\n")
            result = runner.invoke(cli, ["upload", str(gcode), "--json"])
            assert result.exit_code == 0
            mock_adapter.upload_file.assert_called_once_with(str(gcode))

            # Step 5: Start the print
            result = runner.invoke(cli, ["print", "model.gcode", "--json"])
            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["status"] == "success"
            mock_adapter.start_print.assert_called_once_with("model.gcode")

    def test_wait_completes_when_idle(self, runner, mock_adapter, tmp_path):
        """Step 6: Wait for print to complete (printer reports IDLE)."""
        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        # Simulate: first poll → printing, second poll → idle (print done)
        mock_adapter.get_state.side_effect = [
            PrinterState(
                state=PrinterStatus.PRINTING, connected=True,
                tool_temp_actual=210.0, tool_temp_target=210.0,
                bed_temp_actual=60.0, bed_temp_target=60.0,
            ),
            PrinterState(
                state=PrinterStatus.IDLE, connected=True,
                tool_temp_actual=35.0, tool_temp_target=0.0,
                bed_temp_actual=30.0, bed_temp_target=0.0,
            ),
        ]
        mock_adapter.get_job.side_effect = [
            JobProgress(file_name="model.gcode", completion=42.5,
                        print_time_seconds=600, print_time_left_seconds=800),
            JobProgress(file_name=None, completion=None,
                        print_time_seconds=None, print_time_left_seconds=None),
        ]

        with p1, p2, p3, patch("time.sleep"):
            result = runner.invoke(cli, ["wait", "--interval", "0.01", "--json"])

        assert result.exit_code == 0
        assert '"idle"' in result.output
        # The output is multi-line JSON from format_response; parse it
        data = json.loads(result.output)
        assert data["data"]["final_state"] == "idle"

    def test_wait_exits_on_error(self, runner, mock_adapter, tmp_path):
        """Wait exits with code 1 if printer enters error state."""
        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        mock_adapter.get_state.return_value = PrinterState(
            state=PrinterStatus.ERROR, connected=True,
        )
        mock_adapter.get_job.return_value = JobProgress(
            file_name="model.gcode", completion=15.0,
            print_time_seconds=200, print_time_left_seconds=None,
        )

        with p1, p2, p3:
            result = runner.invoke(cli, ["wait", "--json"])

        assert result.exit_code != 0

    def test_history_shows_past_jobs(self, runner):
        """Step 7: View print history."""
        mock_db = MagicMock()
        mock_db.list_jobs.return_value = [
            {"id": "j1", "file_name": "model.gcode", "status": "completed",
             "started_at": 1700000000, "completed_at": 1700003600,
             "printer_name": "voron"},
        ]
        with patch("kiln.persistence.get_db", return_value=mock_db):
            result = runner.invoke(cli, ["history", "--json"])
        assert result.exit_code == 0

    def test_complete_pipeline_slice_upload_print(self, runner, mock_adapter, tmp_path):
        """Full pipeline: slice → upload → print in one shot via --print-after."""
        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        # Create a fake STL file (slice_file will be mocked)
        stl = tmp_path / "model.stl"
        stl.write_text("solid model\nendsolid model\n")

        mock_slice_result = MagicMock()
        mock_slice_result.message = "Sliced model.stl → model.gcode"
        mock_slice_result.output_path = str(tmp_path / "model.gcode")
        mock_slice_result.to_dict.return_value = {
            "input_file": str(stl),
            "output_path": str(tmp_path / "model.gcode"),
            "message": "Sliced model.stl → model.gcode",
        }
        # Create the output file so upload can reference it
        Path(mock_slice_result.output_path).write_text("G28\n")

        with p1, p2, p3, \
             patch("kiln.slicer.slice_file", return_value=mock_slice_result):
            result = runner.invoke(cli, [
                "slice", str(stl), "--print-after", "--json",
            ])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert "slice" in data["data"]
        assert "upload" in data["data"]
        assert "print" in data["data"]
        mock_adapter.upload_file.assert_called_once()
        mock_adapter.start_print.assert_called_once()

    def test_snapshot_after_print(self, runner, mock_adapter, tmp_path):
        """After printing, capture a webcam snapshot."""
        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        with p1, p2, p3:
            result = runner.invoke(cli, ["snapshot", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "success"
        assert "image_base64" in data["data"]

    def test_auto_upload_and_print_local_file(self, runner, mock_adapter, tmp_path):
        """Print command auto-uploads local .gcode files before starting."""
        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        gcode_file = tmp_path / "test_print.gcode"
        gcode_file.write_text("G28\nG1 X10\n")

        with p1, p2, p3:
            result = runner.invoke(cli, ["print", str(gcode_file), "--json"])

        assert result.exit_code == 0
        # Should have uploaded then started
        mock_adapter.upload_file.assert_called_once_with(str(gcode_file))
        mock_adapter.start_print.assert_called_once_with("model.gcode")


# ---------------------------------------------------------------------------
# Edge cases: error propagation through pipeline
# ---------------------------------------------------------------------------


class TestPipelineErrorPropagation:
    """Verify errors at each pipeline stage propagate correctly."""

    def test_preflight_blocks_on_printer_error(self, runner, mock_adapter, tmp_path):
        """If printer is in error state, preflight should fail."""
        mock_adapter.get_state.return_value = PrinterState(
            state=PrinterStatus.ERROR, connected=True,
        )
        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        with p1, p2, p3:
            result = runner.invoke(cli, ["preflight", "--json"])

        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["data"]["ready"] is False

    def test_upload_failure_prevents_print(self, runner, mock_adapter, tmp_path):
        """If upload fails, print should not start."""
        mock_adapter.upload_file.return_value = UploadResult(
            success=False, message="Disk full", file_name=None,
        )
        gcode = tmp_path / "model.gcode"
        gcode.write_text("G28\n")

        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        with p1, p2, p3:
            result = runner.invoke(cli, ["print", str(gcode), "--json"])

        assert result.exit_code != 0
        mock_adapter.start_print.assert_not_called()

    def test_adapter_connection_failure(self, runner, mock_adapter, tmp_path):
        """If printer is unreachable, status should report error."""
        mock_adapter.get_state.side_effect = Exception("Connection refused")
        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        with p1, p2, p3:
            result = runner.invoke(cli, ["status", "--json"])

        assert result.exit_code != 0
        assert "Connection refused" in result.output

    def test_printer_offline_during_wait(self, runner, mock_adapter, tmp_path):
        """Wait should fail if printer goes offline."""
        mock_adapter.get_state.return_value = PrinterState(
            state=PrinterStatus.OFFLINE, connected=False,
        )
        mock_adapter.get_job.return_value = JobProgress(
            file_name=None, completion=None,
            print_time_seconds=None, print_time_left_seconds=None,
        )

        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        with p1, p2, p3:
            result = runner.invoke(cli, ["wait", "--json"])

        assert result.exit_code != 0

    def test_no_webcam_returns_error(self, runner, mock_adapter, tmp_path):
        """Snapshot should fail gracefully if webcam is not available."""
        mock_adapter.get_snapshot.return_value = None
        cfg_path = tmp_path / "config.yaml"
        p1, p2, p3 = _make_patches(mock_adapter, cfg_path)

        with p1, p2, p3:
            result = runner.invoke(cli, ["snapshot", "--json"])

        assert result.exit_code != 0
        assert "NO_WEBCAM" in result.output or "not available" in result.output.lower()


# ===========================================================================
# Part 2 — Cross-subsystem integration tests
# ===========================================================================


# ---------------------------------------------------------------------------
# Shared fixtures for subsystem tests
# ---------------------------------------------------------------------------


def _make_idle_adapter(name: str = "mock") -> MagicMock:
    """Build a MagicMock printer adapter that reports IDLE."""
    adapter = MagicMock()
    adapter.name = name
    adapter.get_state.return_value = PrinterState(
        state=PrinterStatus.IDLE,
        connected=True,
        tool_temp_actual=22.0,
        tool_temp_target=0.0,
        bed_temp_actual=21.0,
        bed_temp_target=0.0,
    )
    adapter.get_job.return_value = JobProgress(
        file_name=None,
        completion=None,
        print_time_seconds=None,
        print_time_left_seconds=None,
    )
    adapter.start_print.return_value = PrintResult(
        success=True, message="Print started",
    )
    adapter.capabilities = PrinterCapabilities(
        can_upload=True,
        can_set_temp=True,
        can_send_gcode=True,
    )
    return adapter


@pytest.fixture
def event_bus():
    """Fresh EventBus instance for each test."""
    return EventBus()


@pytest.fixture
def print_queue(event_bus):
    """In-memory PrintQueue wired to the test event bus."""
    return PrintQueue(event_bus=event_bus)


@pytest.fixture
def registry():
    """Empty PrinterRegistry."""
    return PrinterRegistry()


# ---------------------------------------------------------------------------
# 1. Queue → Scheduler → Printer flow
# ---------------------------------------------------------------------------


class TestQueueSchedulerPrinterFlow:
    """Verify the full job lifecycle across Queue, Scheduler, and Printer.

    Covers: QUEUED → STARTING → PRINTING → COMPLETED transitions,
    scheduler dispatch, and event publication.
    """

    def test_job_transitions_queued_to_completed(self, event_bus, print_queue, registry):
        """Submit a job, run scheduler ticks, verify state transitions and events."""
        # Wire up a mock printer that starts IDLE, then reports PRINTING,
        # then returns to IDLE (indicating job completion).
        adapter = _make_idle_adapter("voron")
        registry.register("voron-350", adapter)

        scheduler = JobScheduler(
            queue=print_queue,
            registry=registry,
            event_bus=event_bus,
            poll_interval=0.01,
        )

        # Track events
        received_events: List[Event] = []
        event_bus.subscribe(None, lambda e: received_events.append(e))

        # Submit a job
        job_id = print_queue.submit(
            file_name="benchy.gcode",
            printer_name="voron-350",
            submitted_by="test-agent",
        )
        job = print_queue.get_job(job_id)
        assert job.status == JobStatus.QUEUED

        # Tick 1: Scheduler dispatches job to idle printer
        result = scheduler.tick()
        assert len(result["dispatched"]) == 1
        assert result["dispatched"][0]["job_id"] == job_id
        assert result["dispatched"][0]["printer_name"] == "voron-350"

        # Job should now be PRINTING
        job = print_queue.get_job(job_id)
        assert job.status == JobStatus.PRINTING
        adapter.start_print.assert_called_once_with("benchy.gcode")

        # Simulate printer reporting PRINTING state on next check
        adapter.get_state.return_value = PrinterState(
            state=PrinterStatus.PRINTING, connected=True,
            tool_temp_actual=210.0, tool_temp_target=210.0,
            bed_temp_actual=60.0, bed_temp_target=60.0,
        )
        adapter.get_job.return_value = JobProgress(
            file_name="benchy.gcode", completion=50.0,
            print_time_seconds=300, print_time_left_seconds=300,
        )
        result = scheduler.tick()
        assert result["checked"] == 1

        # Simulate printer returning to IDLE (print complete)
        adapter.get_state.return_value = PrinterState(
            state=PrinterStatus.IDLE, connected=True,
            tool_temp_actual=35.0, tool_temp_target=0.0,
            bed_temp_actual=30.0, bed_temp_target=0.0,
        )
        result = scheduler.tick()
        assert job_id in result["completed"]

        # Final job state should be COMPLETED
        job = print_queue.get_job(job_id)
        assert job.status == JobStatus.COMPLETED
        assert job.completed_at is not None

        # Verify JOB_STARTED and JOB_COMPLETED events were published
        event_types = [e.type for e in received_events]
        assert EventType.JOB_STARTED in event_types
        assert EventType.JOB_COMPLETED in event_types

    def test_printer_error_fails_job_after_retries(self, event_bus, print_queue, registry):
        """When the printer enters error state, scheduler fails the job after retries."""
        adapter = _make_idle_adapter("ender")
        registry.register("ender-3", adapter)

        scheduler = JobScheduler(
            queue=print_queue,
            registry=registry,
            event_bus=event_bus,
            poll_interval=0.01,
            max_retries=0,  # No retries — fail immediately
        )

        job_id = print_queue.submit(
            file_name="cube.gcode",
            printer_name="ender-3",
            submitted_by="test-agent",
        )

        # Tick 1: Dispatch
        scheduler.tick()
        job = print_queue.get_job(job_id)
        assert job.status == JobStatus.PRINTING

        # Printer enters error state
        adapter.get_state.return_value = PrinterState(
            state=PrinterStatus.ERROR, connected=True,
        )

        # Tick 2: Detect error, fail job (no retries)
        result = scheduler.tick()
        assert len(result["failed"]) == 1
        assert result["failed"][0]["job_id"] == job_id

        job = print_queue.get_job(job_id)
        assert job.status == JobStatus.FAILED
        assert job.error is not None

    def test_scheduler_skips_busy_printers(self, event_bus, print_queue, registry):
        """Scheduler should not dispatch a second job to a printer with an active job."""
        adapter = _make_idle_adapter("voron")
        registry.register("voron-350", adapter)

        scheduler = JobScheduler(
            queue=print_queue,
            registry=registry,
            event_bus=event_bus,
        )

        # Submit two jobs
        job1_id = print_queue.submit(
            file_name="part1.gcode",
            printer_name="voron-350",
            submitted_by="test-agent",
        )
        job2_id = print_queue.submit(
            file_name="part2.gcode",
            printer_name="voron-350",
            submitted_by="test-agent",
        )

        # Tick 1: Only first job should be dispatched
        scheduler.tick()

        # Printer now reports PRINTING (not idle)
        adapter.get_state.return_value = PrinterState(
            state=PrinterStatus.PRINTING, connected=True,
        )

        # Tick 2: Second job should NOT be dispatched (printer busy)
        result = scheduler.tick()
        assert len(result["dispatched"]) == 0

        job2 = print_queue.get_job(job2_id)
        assert job2.status == JobStatus.QUEUED

    def test_job_priority_ordering(self, event_bus, print_queue, registry):
        """Higher-priority jobs should be dispatched first."""
        adapter = _make_idle_adapter("voron")
        registry.register("voron-350", adapter)

        scheduler = JobScheduler(
            queue=print_queue,
            registry=registry,
            event_bus=event_bus,
        )

        # Submit low-priority, then high-priority
        low_id = print_queue.submit(
            file_name="low.gcode",
            printer_name="voron-350",
            submitted_by="test-agent",
            priority=1,
        )
        high_id = print_queue.submit(
            file_name="high.gcode",
            printer_name="voron-350",
            submitted_by="test-agent",
            priority=10,
        )

        # The high-priority job should be dispatched first
        result = scheduler.tick()
        assert len(result["dispatched"]) == 1
        assert result["dispatched"][0]["job_id"] == high_id


# ---------------------------------------------------------------------------
# 2. Billing → Fulfillment → Refund saga
# ---------------------------------------------------------------------------


class TestBillingFulfillmentRefundSaga:
    """Test the billing lifecycle for outsourced fulfillment orders.

    Covers: fee calculation, charge recording, job failure,
    refund/waiver verification, and event publication.
    """

    def test_charge_then_job_failure_records_refund_event(self, event_bus):
        """Create a charge, simulate failure, verify refund event is published."""
        received_events: List[Event] = []
        event_bus.subscribe(None, lambda e: received_events.append(e))

        # Set up billing with a policy that charges fees (exhaust free tier)
        policy = FeePolicy(
            network_fee_percent=5.0,
            free_tier_jobs=0,  # No free tier — always charge
        )
        ledger = BillingLedger(fee_policy=policy)

        # Calculate and record a fee for an outsourced order
        fee = ledger.calculate_fee(job_cost=100.00)
        assert fee.fee_amount == 5.0
        assert fee.total_cost == 105.0
        assert fee.waived is False

        charge_id = ledger.record_charge("order-001", fee)
        assert charge_id  # non-empty

        # Verify the charge is recorded in the ledger
        recorded = ledger.get_job_charges("order-001")
        assert recorded is not None
        assert recorded.fee_amount == 5.0

        # Simulate job failure — publish event
        event_bus.publish(Event(
            type=EventType.JOB_FAILED,
            data={"job_id": "order-001", "error": "printer_jam"},
            source="scheduler",
        ))

        # Simulate refund — publish payment refund event
        event_bus.publish(Event(
            type=EventType.PAYMENT_REFUNDED,
            data={
                "job_id": "order-001",
                "charge_id": charge_id,
                "refund_amount": fee.fee_amount,
            },
            source="billing",
        ))

        # Verify both events were published and received
        event_types = [e.type for e in received_events]
        assert EventType.JOB_FAILED in event_types
        assert EventType.PAYMENT_REFUNDED in event_types

        # Verify refund event carries correct data
        refund_event = [e for e in received_events if e.type == EventType.PAYMENT_REFUNDED][0]
        assert refund_event.data["charge_id"] == charge_id
        assert refund_event.data["refund_amount"] == 5.0

    def test_free_tier_waives_fee(self, event_bus):
        """Within the free tier, fees should be waived."""
        policy = FeePolicy(
            network_fee_percent=5.0,
            free_tier_jobs=3,
        )
        ledger = BillingLedger(fee_policy=policy)

        # First 3 jobs are free
        for i in range(3):
            fee = ledger.calculate_fee(job_cost=50.00)
            assert fee.waived is True
            assert fee.fee_amount == 0.0
            assert fee.total_cost == 50.0
            ledger.record_charge(f"job-{i}", fee)

        # 4th job should be charged
        fee = ledger.calculate_fee(job_cost=50.00)
        assert fee.waived is False
        assert fee.fee_amount == 2.50

    def test_spend_limits_block_excessive_fees(self):
        """Spend limits should prevent charges above the configured cap."""
        from kiln.billing import SpendLimits

        policy = FeePolicy(
            network_fee_percent=10.0,
            free_tier_jobs=0,
        )
        ledger = BillingLedger(fee_policy=policy)

        limits = SpendLimits(max_per_order_usd=20.0, monthly_cap_usd=100.0)
        fee = ledger.calculate_fee(job_cost=500.0)

        # Fee is $50, which exceeds the $20 per-order limit
        ok, reason = ledger.check_spend_limits(fee.fee_amount, limits)
        assert ok is False
        assert "per-order limit" in reason

    def test_monthly_revenue_tracks_charges(self):
        """Monthly revenue summary should reflect all recorded charges."""
        policy = FeePolicy(
            network_fee_percent=5.0,
            free_tier_jobs=0,
        )
        ledger = BillingLedger(fee_policy=policy)

        for i in range(5):
            fee = ledger.calculate_fee(job_cost=100.0)
            ledger.record_charge(f"job-{i}", fee)

        summary = ledger.monthly_revenue()
        assert summary["job_count"] == 5
        assert summary["total_fees"] == 25.0  # 5 * $5.00


# ---------------------------------------------------------------------------
# 3. Event bus → Webhook delivery
# ---------------------------------------------------------------------------


class TestEventBusWebhookDelivery:
    """Verify the event bus correctly dispatches to webhook subscribers.

    Covers: subscription, event publication, payload delivery,
    HMAC signing, and wildcard subscriptions.
    """

    def test_subscriber_receives_event_with_correct_payload(self, event_bus):
        """Subscribe to an event type, publish, verify callback payload."""
        received: List[Event] = []

        def handler(event: Event) -> None:
            received.append(event)

        event_bus.subscribe(EventType.JOB_COMPLETED, handler)

        # Publish a JOB_COMPLETED event
        event_bus.publish(Event(
            type=EventType.JOB_COMPLETED,
            data={"job_id": "j-123", "printer_name": "voron-350"},
            source="scheduler",
        ))

        assert len(received) == 1
        assert received[0].type == EventType.JOB_COMPLETED
        assert received[0].data["job_id"] == "j-123"
        assert received[0].data["printer_name"] == "voron-350"
        assert received[0].source == "scheduler"

    def test_wildcard_subscriber_receives_all_events(self, event_bus):
        """A wildcard subscriber (event_type=None) receives every event."""
        received: List[Event] = []
        event_bus.subscribe(None, lambda e: received.append(e))

        event_bus.publish(Event(type=EventType.JOB_SUBMITTED, data={"a": 1}))
        event_bus.publish(Event(type=EventType.PRINT_COMPLETED, data={"b": 2}))
        event_bus.publish(Event(type=EventType.TEMPERATURE_WARNING, data={"c": 3}))

        assert len(received) == 3

    def test_subscriber_only_receives_matching_events(self, event_bus):
        """A typed subscriber should not receive events of other types."""
        received: List[Event] = []
        event_bus.subscribe(EventType.JOB_FAILED, lambda e: received.append(e))

        event_bus.publish(Event(type=EventType.JOB_COMPLETED, data={}))
        event_bus.publish(Event(type=EventType.JOB_FAILED, data={"error": "timeout"}))

        assert len(received) == 1
        assert received[0].type == EventType.JOB_FAILED

    def test_handler_exception_does_not_block_other_handlers(self, event_bus):
        """If one handler raises, other handlers should still execute."""
        results: List[str] = []

        def bad_handler(event: Event) -> None:
            raise RuntimeError("handler exploded")

        def good_handler(event: Event) -> None:
            results.append("ok")

        event_bus.subscribe(EventType.JOB_COMPLETED, bad_handler)
        event_bus.subscribe(EventType.JOB_COMPLETED, good_handler)

        event_bus.publish(Event(type=EventType.JOB_COMPLETED, data={}))

        # The good handler should still have run
        assert results == ["ok"]

    def test_event_history_is_recorded(self, event_bus):
        """Published events should appear in the event history."""
        event_bus.publish(Event(
            type=EventType.PRINT_STARTED,
            data={"file": "benchy.gcode"},
            source="printer:voron",
        ))
        event_bus.publish(Event(
            type=EventType.PRINT_COMPLETED,
            data={"file": "benchy.gcode"},
            source="printer:voron",
        ))

        history = event_bus.recent_events(limit=10)
        assert len(history) == 2
        # Newest first
        assert history[0].type == EventType.PRINT_COMPLETED
        assert history[1].type == EventType.PRINT_STARTED

    def test_webhook_manager_enqueues_matching_events(self, event_bus):
        """WebhookManager should enqueue deliveries for matching events."""
        # Use a custom send function to capture deliveries without HTTP
        delivered: List[Dict[str, Any]] = []

        def mock_send(url, payload, headers, timeout):
            delivered.append({
                "url": url,
                "payload": json.loads(payload),
                "headers": headers,
            })
            return 200

        manager = WebhookManager(event_bus, max_retries=1, retry_delay=0.01)
        manager._send_func = mock_send

        # Register endpoint bypassing SSRF validation for test
        with patch("kiln.webhooks._validate_webhook_url", return_value=(True, "")):
            endpoint = manager.register(
                url="https://hooks.example.com/kiln",
                events=["job.completed", "job.failed"],
                secret="test-secret-123",
            )

        manager.start()
        try:
            # Publish a matching event
            event_bus.publish(Event(
                type=EventType.JOB_COMPLETED,
                data={"job_id": "j-456"},
                source="scheduler",
            ))

            # Give the delivery thread a moment
            time.sleep(0.2)

            assert len(delivered) == 1
            assert delivered[0]["url"] == "https://hooks.example.com/kiln"
            assert delivered[0]["payload"]["type"] == "job.completed"
            assert delivered[0]["payload"]["data"]["job_id"] == "j-456"
            assert "X-Kiln-Signature" in delivered[0]["headers"]

            # Publish a non-matching event — should NOT be delivered
            event_bus.publish(Event(
                type=EventType.TEMPERATURE_WARNING,
                data={"temp": 300},
            ))
            time.sleep(0.1)
            assert len(delivered) == 1  # still 1

        finally:
            manager.stop()

    def test_webhook_hmac_signature_is_valid(self, event_bus):
        """Verify that the HMAC signature in the webhook header is correct."""
        import hashlib
        import hmac as hmac_mod

        delivered_headers: List[Dict[str, str]] = []
        delivered_payloads: List[str] = []

        def capture_send(url, payload, headers, timeout):
            delivered_headers.append(headers)
            delivered_payloads.append(payload)
            return 200

        manager = WebhookManager(event_bus, max_retries=1)
        manager._send_func = capture_send

        secret = "my-webhook-secret"
        with patch("kiln.webhooks._validate_webhook_url", return_value=(True, "")):
            manager.register(
                url="https://hooks.example.com/verify",
                events=["job.started"],
                secret=secret,
            )

        manager.start()
        try:
            event_bus.publish(Event(
                type=EventType.JOB_STARTED,
                data={"job_id": "j-789"},
            ))
            time.sleep(0.2)

            assert len(delivered_payloads) == 1
            payload = delivered_payloads[0]
            sig_header = delivered_headers[0]["X-Kiln-Signature"]

            expected = "sha256=" + hmac_mod.new(
                secret.encode(), payload.encode(), hashlib.sha256
            ).hexdigest()
            assert sig_header == expected

        finally:
            manager.stop()


# ---------------------------------------------------------------------------
# 4. Safety → Print flow
# ---------------------------------------------------------------------------


class TestSafetyPrintFlow:
    """Verify that the G-code safety validator gates the print flow.

    Covers: temperature validation, blocked commands, preflight pass/fail,
    and printer-specific safety limits.
    """

    def test_excessive_temperature_blocks_print(self):
        """G-code with temperature above safe max should be rejected."""
        result = validate_gcode("M104 S999")
        assert result.valid is False
        assert len(result.errors) > 0
        assert len(result.blocked_commands) > 0

    def test_safe_temperature_passes_validation(self):
        """G-code with normal temperature should pass."""
        result = validate_gcode("M104 S200\nG28\nG1 X10 Y10 Z0.2 F3000")
        assert result.valid is True
        assert len(result.errors) == 0
        assert len(result.commands) == 3

    def test_blocked_command_is_rejected(self):
        """Firmware-modifying commands (M500, M502, etc.) should be blocked."""
        result = validate_gcode("M500")
        assert result.valid is False
        assert "M500" in result.blocked_commands[0]

    def test_emergency_stop_is_blocked(self):
        """M112 (emergency stop) should be blocked through the validator."""
        result = validate_gcode("M112")
        assert result.valid is False

    def test_excessive_bed_temp_is_blocked(self):
        """Bed temperature above safe max should be rejected."""
        result = validate_gcode("M140 S200")
        assert result.valid is False

    def test_safe_bed_temp_passes(self):
        """Normal bed temperature should pass."""
        result = validate_gcode("M140 S60")
        assert result.valid is True

    def test_mixed_safe_and_unsafe_commands(self):
        """A batch with one unsafe command should fail the entire validation."""
        gcode = "G28\nM104 S200\nM500\nG1 X10\n"
        result = validate_gcode(gcode)
        assert result.valid is False
        # Safe commands should still be in the commands list
        assert "G28" in result.commands
        # The blocked one should be noted
        assert len(result.blocked_commands) > 0

    def test_warnings_do_not_block_print(self):
        """Commands that generate warnings should still be valid."""
        # G28 generates a warning ("ensure bed is clear")
        result = validate_gcode("G28")
        assert result.valid is True
        assert len(result.warnings) > 0

    def test_printer_specific_validation_uses_profile(self):
        """validate_gcode_for_printer should use printer-specific limits."""
        # Use a known profile that has tighter limits than generic
        # If profile not found, it falls back to generic — still valid test
        result = validate_gcode_for_printer(
            "M104 S200\nG28",
            printer_id="ender3",
        )
        assert result.valid is True
        assert len(result.commands) >= 1

    def test_printer_specific_rejects_above_profile_limit(self):
        """Printer-specific validation should reject temps above the profile max."""
        # Ender 3 has PTFE hotend, max ~260C. Try 290C.
        result = validate_gcode_for_printer(
            "M104 S290",
            printer_id="ender3",
        )
        # If ender3 profile exists with max < 290, should fail.
        # If profile not found, falls back to generic (max 300) — would pass.
        # Either outcome is valid; the important thing is the code path works.
        # We test both branches:
        from kiln.safety_profiles import get_profile
        try:
            profile = get_profile("ender3")
            if profile.max_hotend_temp < 290:
                assert result.valid is False
            else:
                assert result.valid is True
        except KeyError:
            # No profile — falls back to generic (300C max)
            assert result.valid is True

    def test_safety_integrates_with_queue_flow(self, event_bus, print_queue, registry):
        """Safety rejection should prevent job from being dispatched."""
        # This tests the conceptual flow: validate G-code BEFORE submitting
        # to the queue. If validation fails, the job should not be submitted.
        result = validate_gcode("M104 S999")
        assert result.valid is False

        # Agent should NOT submit the job if validation fails
        # (This is the enforcement point — agents check before submitting)
        initial_count = print_queue.pending_count()

        if not result.valid:
            # Publish a safety event
            event_bus.publish(Event(
                type=EventType.SAFETY_BLOCKED,
                data={
                    "reason": result.errors[0],
                    "commands": result.blocked_commands,
                },
                source="gcode_validator",
            ))

        # Queue should still be empty
        assert print_queue.pending_count() == initial_count

        # Verify the safety event was recorded
        history = event_bus.recent_events(event_type=EventType.SAFETY_BLOCKED)
        assert len(history) == 1


# ---------------------------------------------------------------------------
# 5. Fleet registry → Scheduling
# ---------------------------------------------------------------------------


class TestFleetRegistryScheduling:
    """Verify fleet registry integrates with the scheduler for
    capability matching and multi-printer dispatch.

    Covers: multi-printer registration, fleet status queries,
    idle printer detection, capability-based routing.
    """

    def test_fleet_status_reports_all_printers(self, registry):
        """get_fleet_status should report state for every registered printer."""
        voron = _make_idle_adapter("voron")
        ender = _make_idle_adapter("ender")
        ender.get_state.return_value = PrinterState(
            state=PrinterStatus.PRINTING, connected=True,
            tool_temp_actual=210.0, tool_temp_target=210.0,
            bed_temp_actual=60.0, bed_temp_target=60.0,
        )

        registry.register("voron-350", voron)
        registry.register("ender-3", ender)

        status = registry.get_fleet_status()
        assert len(status) == 2

        names = {s["name"] for s in status}
        assert names == {"voron-350", "ender-3"}

        voron_status = [s for s in status if s["name"] == "voron-350"][0]
        assert voron_status["state"] == "idle"
        assert voron_status["connected"] is True

        ender_status = [s for s in status if s["name"] == "ender-3"][0]
        assert ender_status["state"] == "printing"

    def test_idle_printers_returns_only_idle(self, registry):
        """get_idle_printers should only return printers in IDLE state."""
        idle_adapter = _make_idle_adapter("idle")
        busy_adapter = _make_idle_adapter("busy")
        busy_adapter.get_state.return_value = PrinterState(
            state=PrinterStatus.PRINTING, connected=True,
        )
        offline_adapter = _make_idle_adapter("offline")
        offline_adapter.get_state.side_effect = Exception("Connection refused")

        registry.register("idle-printer", idle_adapter)
        registry.register("busy-printer", busy_adapter)
        registry.register("offline-printer", offline_adapter)

        idle = registry.get_idle_printers()
        assert idle == ["idle-printer"]

    def test_scheduler_dispatches_to_correct_printer(self, event_bus, registry):
        """When a job targets a specific printer, scheduler should use it."""
        queue = PrintQueue(event_bus=event_bus)
        voron = _make_idle_adapter("voron")
        ender = _make_idle_adapter("ender")

        registry.register("voron-350", voron)
        registry.register("ender-3", ender)

        scheduler = JobScheduler(
            queue=queue,
            registry=registry,
            event_bus=event_bus,
        )

        # Submit job targeting voron-350 specifically
        job_id = queue.submit(
            file_name="part.gcode",
            printer_name="voron-350",
            submitted_by="test-agent",
        )

        result = scheduler.tick()
        assert len(result["dispatched"]) == 1
        assert result["dispatched"][0]["printer_name"] == "voron-350"
        voron.start_print.assert_called_once_with("part.gcode")
        ender.start_print.assert_not_called()

    def test_any_printer_job_dispatches_to_first_idle(self, event_bus, registry):
        """Jobs with printer_name=None should dispatch to any idle printer."""
        queue = PrintQueue(event_bus=event_bus)
        voron = _make_idle_adapter("voron")
        ender = _make_idle_adapter("ender")

        registry.register("ender-3", ender)
        registry.register("voron-350", voron)

        scheduler = JobScheduler(
            queue=queue,
            registry=registry,
            event_bus=event_bus,
        )

        job_id = queue.submit(
            file_name="part.gcode",
            printer_name=None,  # Any printer
            submitted_by="test-agent",
        )

        result = scheduler.tick()
        assert len(result["dispatched"]) == 1
        dispatched_printer = result["dispatched"][0]["printer_name"]
        assert dispatched_printer in ("voron-350", "ender-3")

    def test_multi_printer_parallel_dispatch(self, event_bus, registry):
        """Multiple idle printers should each receive a job in a single tick."""
        queue = PrintQueue(event_bus=event_bus)

        printers = {}
        for name in ["printer-1", "printer-2", "printer-3"]:
            adapter = _make_idle_adapter(name)
            registry.register(name, adapter)
            printers[name] = adapter

        scheduler = JobScheduler(
            queue=queue,
            registry=registry,
            event_bus=event_bus,
        )

        # Submit 3 jobs, one per printer
        for i, name in enumerate(printers.keys()):
            queue.submit(
                file_name=f"part{i}.gcode",
                printer_name=name,
                submitted_by="test-agent",
            )

        result = scheduler.tick()
        assert len(result["dispatched"]) == 3
        dispatched_printers = {d["printer_name"] for d in result["dispatched"]}
        assert dispatched_printers == {"printer-1", "printer-2", "printer-3"}

    def test_offline_printer_excluded_from_dispatch(self, event_bus, registry):
        """Printers that fail to respond should not receive jobs."""
        queue = PrintQueue(event_bus=event_bus)

        good_adapter = _make_idle_adapter("good")
        bad_adapter = _make_idle_adapter("bad")
        bad_adapter.get_state.side_effect = Exception("Connection timeout")

        registry.register("good-printer", good_adapter)
        registry.register("bad-printer", bad_adapter)

        scheduler = JobScheduler(
            queue=queue,
            registry=registry,
            event_bus=event_bus,
        )

        job_id = queue.submit(
            file_name="test.gcode",
            printer_name=None,
            submitted_by="test-agent",
        )

        result = scheduler.tick()
        if result["dispatched"]:
            assert result["dispatched"][0]["printer_name"] == "good-printer"

    def test_fleet_status_handles_connection_errors(self, registry):
        """Fleet status should report OFFLINE for printers that error out."""
        good = _make_idle_adapter("good")
        broken = _make_idle_adapter("broken")
        broken.get_state.side_effect = Exception("Network unreachable")

        registry.register("good-printer", good)
        registry.register("broken-printer", broken)

        status = registry.get_fleet_status()
        assert len(status) == 2

        broken_status = [s for s in status if s["name"] == "broken-printer"][0]
        assert broken_status["state"] == "offline"
        assert broken_status["connected"] is False

    def test_unregister_removes_printer_from_fleet(self, registry):
        """Unregistering a printer should remove it from all fleet queries."""
        adapter = _make_idle_adapter("temp")
        registry.register("temp-printer", adapter)
        assert "temp-printer" in registry
        assert registry.count == 1

        registry.unregister("temp-printer")
        assert "temp-printer" not in registry
        assert registry.count == 0
        assert registry.get_fleet_status() == []
