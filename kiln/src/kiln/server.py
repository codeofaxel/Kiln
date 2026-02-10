"""Kiln MCP Server -- exposes 3D printing capabilities to AI agents.

Provides a Model Context Protocol (MCP) server that lets agents monitor,
control, and manage a 3D printer through a clean set of tool-based
interactions.  The server loads printer configuration from environment
variables and delegates all hardware interaction to a
:class:`~kiln.printers.base.PrinterAdapter` backend.

Environment variables
---------------------
``KILN_PRINTER_HOST``
    Base URL of the printer server (e.g. ``http://octopi.local``).
``KILN_PRINTER_API_KEY``
    API key used for authenticating with the printer server.
``KILN_PRINTER_TYPE``
    Printer backend type.  Supported values: ``"octoprint"``,
    ``"moonraker"``, and ``"bambu"``.  Defaults to ``"octoprint"``.
``KILN_PRINTER_SERIAL``
    Bambu printer serial number (required when ``KILN_PRINTER_TYPE``
    is ``"bambu"``).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from kiln.printers import (
    BambuAdapter,
    MoonrakerAdapter,
    OctoPrintAdapter,
    PrinterAdapter,
    PrinterError,
    PrinterStatus,
)
from kiln.gcode import validate_gcode as _validate_gcode_impl
from kiln.registry import PrinterRegistry, PrinterNotFoundError
from kiln.queue import PrintQueue, JobStatus, JobNotFoundError
from kiln.events import Event, EventBus, EventType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_PRINTER_HOST: str = os.environ.get("KILN_PRINTER_HOST", "")
_PRINTER_API_KEY: str = os.environ.get("KILN_PRINTER_API_KEY", "")
_PRINTER_TYPE: str = os.environ.get("KILN_PRINTER_TYPE", "octoprint")
_PRINTER_SERIAL: str = os.environ.get("KILN_PRINTER_SERIAL", "")

# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "kiln",
    instructions=(
        "Agent infrastructure for physical fabrication via 3D printing. "
        "Provides tools to monitor printer status, manage files, control "
        "print jobs, adjust temperatures, send raw G-code, and run "
        "pre-flight safety checks.\n\n"
        "Start with `printer_status` to see what the printer is doing. "
        "Use `preflight_check` before printing. Use `fleet_status` to "
        "manage multiple printers. Use `validate_gcode` before `send_gcode` "
        "for raw commands. Submit jobs via `submit_job` for queued execution."
    ),
)

# ---------------------------------------------------------------------------
# Printer adapter singleton
# ---------------------------------------------------------------------------

_adapter: Optional[PrinterAdapter] = None


def _get_adapter() -> PrinterAdapter:
    """Return the lazily-initialised printer adapter.

    The adapter is created on first use so that the MCP server module can
    be imported without requiring environment variables to be set (useful
    for testing and introspection).

    Returns:
        The active :class:`PrinterAdapter` instance.

    Raises:
        RuntimeError: If required configuration is missing or the printer
            type is not supported.
    """
    global _adapter  # noqa: PLW0603

    if _adapter is not None:
        return _adapter

    host = _PRINTER_HOST
    api_key = _PRINTER_API_KEY
    printer_type = _PRINTER_TYPE

    if not host:
        raise RuntimeError(
            "KILN_PRINTER_HOST environment variable is not set.  "
            "Set it to the base URL of your printer server "
            "(e.g. http://octopi.local)."
        )
    if printer_type == "octoprint":
        if not api_key:
            raise RuntimeError(
                "KILN_PRINTER_API_KEY environment variable is not set.  "
                "Set it to your printer server's API key."
            )
        _adapter = OctoPrintAdapter(host=host, api_key=api_key)
    elif printer_type == "moonraker":
        # Moonraker typically does not require an API key, but one can
        # optionally be provided via KILN_PRINTER_API_KEY.
        _adapter = MoonrakerAdapter(host=host, api_key=api_key or None)
    elif printer_type == "bambu":
        if not api_key:
            raise RuntimeError(
                "KILN_PRINTER_API_KEY environment variable is not set.  "
                "Set it to your Bambu printer's LAN Access Code."
            )
        serial = _PRINTER_SERIAL
        if not serial:
            raise RuntimeError(
                "KILN_PRINTER_SERIAL environment variable is not set.  "
                "Set it to your Bambu printer's serial number."
            )
        _adapter = BambuAdapter(host=host, access_code=api_key, serial=serial)
    else:
        raise RuntimeError(
            f"Unsupported printer type: {printer_type!r}.  "
            f"Supported types are 'octoprint', 'moonraker', and 'bambu'."
        )

    logger.info(
        "Initialised %s adapter for %s",
        _adapter.name,
        host,
    )
    return _adapter


# ---------------------------------------------------------------------------
# Fleet singletons (registry, queue, event bus)
# ---------------------------------------------------------------------------

_registry = PrinterRegistry()
_queue = PrintQueue()
_event_bus = EventBus()


def _error_dict(message: str, code: str = "ERROR") -> Dict[str, Any]:
    """Build a standardised error response dict."""
    return {"success": False, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def printer_status() -> dict:
    """Get the current printer state, temperatures, and active job progress.

    Returns a JSON object with:
    - ``printer``: connection status, operational state, tool/bed temperatures
    - ``job``: current file name, completion percentage, elapsed and remaining time
    - ``capabilities``: what this printer backend supports

    Use this as the first call to understand what the printer is doing before
    taking any action.
    """
    try:
        adapter = _get_adapter()
        state = adapter.get_state()
        job = adapter.get_job()
        caps = adapter.capabilities

        return {
            "success": True,
            "printer": state.to_dict(),
            "job": job.to_dict(),
            "capabilities": caps.to_dict(),
        }
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in printer_status")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def printer_files() -> dict:
    """List all G-code files available on the printer.

    Returns a JSON array of file objects, each containing:
    - ``name``: file name
    - ``path``: full path on the printer
    - ``size_bytes``: file size (may be null)
    - ``date``: upload timestamp as Unix epoch (may be null)

    Use this to discover which files are ready to print.  Pass a file's
    ``name`` or ``path`` to ``start_print`` to begin printing it.
    """
    try:
        adapter = _get_adapter()
        files = adapter.list_files()
        return {
            "success": True,
            "files": [f.to_dict() for f in files],
            "count": len(files),
        }
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in printer_files")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def upload_file(file_path: str) -> dict:
    """Upload a local G-code file to the printer.

    Args:
        file_path: Absolute path to the G-code file on the local filesystem.
            The file must exist, be readable, and have a recognised extension
            (.gcode, .gco, or .g).

    After a successful upload the file will appear in ``printer_files()`` and
    can be started with ``start_print()``.
    """
    try:
        adapter = _get_adapter()
        result = adapter.upload_file(file_path)
        return result.to_dict()
    except FileNotFoundError as exc:
        return _error_dict(str(exc), code="FILE_NOT_FOUND")
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in upload_file")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def delete_file(file_path: str) -> dict:
    """Delete a G-code file from the printer's storage.

    Args:
        file_path: Path of the file as shown by ``printer_files()``.

    This is irreversible -- the file cannot be recovered once deleted.
    """
    try:
        adapter = _get_adapter()
        ok = adapter.delete_file(file_path)
        return {
            "success": ok,
            "message": f"Deleted {file_path}." if ok else f"Failed to delete {file_path}.",
        }
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in delete_file")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def start_print(file_name: str) -> dict:
    """Start printing a file that already exists on the printer.

    Args:
        file_name: Name or path of the file as shown by ``printer_files()``.

    The printer must be idle and connected.  Use ``preflight_check()`` first
    to verify the printer is ready.  This will select the file and
    immediately begin printing.
    """
    try:
        adapter = _get_adapter()
        result = adapter.start_print(file_name)
        return result.to_dict()
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in start_print")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def cancel_print() -> dict:
    """Cancel the currently running print job.

    The printer must have an active job (printing or paused).  After
    cancellation the printer will cool down and return to idle.

    WARNING: Cancellation is irreversible -- the print cannot be resumed
    from where it left off.
    """
    try:
        adapter = _get_adapter()
        result = adapter.cancel_print()
        return result.to_dict()
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in cancel_print")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def pause_print() -> dict:
    """Pause the currently running print job.

    Pausing lifts the nozzle and parks the head.  The heaters stay on.
    Use ``resume_print()`` to continue from where the print left off.
    """
    try:
        adapter = _get_adapter()
        result = adapter.pause_print()
        return result.to_dict()
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in pause_print")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def resume_print() -> dict:
    """Resume a paused print job.

    The printer must currently be in a paused state.  Resuming will return
    the nozzle to its previous position and continue extruding.
    """
    try:
        adapter = _get_adapter()
        result = adapter.resume_print()
        return result.to_dict()
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in resume_print")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def set_temperature(
    tool_temp: float | None = None,
    bed_temp: float | None = None,
) -> dict:
    """Set the target temperature for the hotend (tool) and/or heated bed.

    Args:
        tool_temp: Target hotend temperature in Celsius.  Pass ``0`` to turn
            the heater off.  Omit or pass ``null`` to leave unchanged.
        bed_temp: Target bed temperature in Celsius.  Pass ``0`` to turn
            the heater off.  Omit or pass ``null`` to leave unchanged.

    At least one of ``tool_temp`` or ``bed_temp`` must be provided.

    Common PLA temperatures: tool 200-210C, bed 60C.
    Common PETG temperatures: tool 230-250C, bed 80-85C.
    Common ABS temperatures: tool 240-260C, bed 100-110C.
    """
    if tool_temp is None and bed_temp is None:
        return _error_dict(
            "At least one of tool_temp or bed_temp must be provided.",
            code="INVALID_ARGS",
        )

    try:
        adapter = _get_adapter()
        results: Dict[str, Any] = {"success": True}

        if tool_temp is not None:
            ok = adapter.set_tool_temp(tool_temp)
            results["tool"] = {
                "target": tool_temp,
                "accepted": ok,
            }

        if bed_temp is not None:
            ok = adapter.set_bed_temp(bed_temp)
            results["bed"] = {
                "target": bed_temp,
                "accepted": ok,
            }

        return results
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in set_temperature")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def preflight_check(file_path: str | None = None) -> dict:
    """Run pre-print safety checks to verify the printer is ready.

    Checks performed:
    - Printer is connected and operational
    - Printer is not currently printing
    - No error flags are set
    - Temperatures are within safe limits
    - (Optional) Local G-code file is valid and readable

    Args:
        file_path: Optional path to a local G-code file to validate before
            upload.  If omitted, only printer-state checks are performed.

    Call this before ``start_print()`` to catch problems early.  The result
    includes a ``ready`` boolean and detailed per-check breakdowns.
    """
    try:
        adapter = _get_adapter()

        # -- Printer state checks ------------------------------------------
        state = adapter.get_state()
        checks: List[Dict[str, Any]] = []
        errors: List[str] = []

        # Connected
        is_connected = state.connected
        checks.append({
            "name": "printer_connected",
            "passed": is_connected,
            "message": "Printer is connected" if is_connected else "Printer is offline",
        })
        if not is_connected:
            errors.append("Printer is not connected / offline")

        # Idle (not printing or in error)
        idle_states = {PrinterStatus.IDLE}
        is_idle = state.state in idle_states
        checks.append({
            "name": "printer_idle",
            "passed": is_idle,
            "message": f"Printer state: {state.state.value}",
        })
        if not is_idle:
            errors.append(f"Printer is not idle (state: {state.state.value})")

        # No error
        no_error = state.state != PrinterStatus.ERROR
        checks.append({
            "name": "no_errors",
            "passed": no_error,
            "message": "No errors" if no_error else "Printer is in error state",
        })
        if not no_error:
            errors.append("Printer is in an error state")

        # -- Temperature checks --------------------------------------------
        temp_warnings: List[str] = []
        MAX_TOOL = 260.0
        MAX_BED = 110.0

        if state.tool_temp_actual is not None and state.tool_temp_actual > MAX_TOOL:
            temp_warnings.append(
                f"Tool temp ({state.tool_temp_actual:.1f}C) exceeds safe max ({MAX_TOOL:.0f}C)"
            )
        if state.bed_temp_actual is not None and state.bed_temp_actual > MAX_BED:
            temp_warnings.append(
                f"Bed temp ({state.bed_temp_actual:.1f}C) exceeds safe max ({MAX_BED:.0f}C)"
            )

        temps_safe = len(temp_warnings) == 0
        checks.append({
            "name": "temperatures_safe",
            "passed": temps_safe,
            "message": "Temperatures within limits" if temps_safe else "; ".join(temp_warnings),
        })
        if not temps_safe:
            errors.extend(temp_warnings)

        # -- File validation (optional) ------------------------------------
        file_result: Optional[Dict[str, Any]] = None
        if file_path is not None:
            file_result = _validate_local_file(file_path)
            file_ok = file_result.get("valid", False)
            checks.append({
                "name": "file_valid",
                "passed": file_ok,
                "message": "File OK" if file_ok else "; ".join(file_result.get("errors", [])),
            })
            if not file_ok:
                errors.extend(file_result.get("errors", []))

        # -- Summary -------------------------------------------------------
        ready = all(c["passed"] for c in checks)
        summary = (
            "All pre-flight checks passed. Ready to print."
            if ready
            else "Pre-flight checks failed: " + "; ".join(errors) + "."
        )

        result: Dict[str, Any] = {
            "success": True,
            "ready": ready,
            "checks": checks,
            "errors": errors,
            "summary": summary,
            "temperatures": {
                "tool_actual": state.tool_temp_actual,
                "tool_target": state.tool_temp_target,
                "bed_actual": state.bed_temp_actual,
                "bed_target": state.bed_temp_target,
            },
        }
        if file_result is not None:
            result["file"] = file_result

        return result

    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in preflight_check")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def send_gcode(commands: str) -> dict:
    """Send raw G-code commands directly to the printer.

    Args:
        commands: One or more G-code commands separated by newlines or spaces.
            Examples: ``"G28"`` (home all axes), ``"G28\\nG1 Z10 F300"``
            (home then move Z up 10mm), ``"M104 S200"`` (set hotend to 200C).

    The commands are sent sequentially in order.  The printer must be
    connected.

    G-code is validated before sending.  Commands that exceed temperature
    limits or modify firmware settings are blocked.  Use ``validate_gcode``
    to preview what would be allowed without actually sending.
    """
    try:
        adapter = _get_adapter()

        # Split on newlines and/or whitespace-separated commands, filtering
        # out empty strings.
        raw_lines = re.split(r"[\n\r]+", commands.strip())
        cmd_list: List[str] = []
        for line in raw_lines:
            stripped = line.strip()
            if stripped:
                cmd_list.append(stripped)

        if not cmd_list:
            return _error_dict("No commands provided.", code="INVALID_ARGS")

        # -- Safety validation -------------------------------------------------
        validation = _validate_gcode_impl(cmd_list)
        if not validation.valid:
            return {
                "success": False,
                "error": {
                    "code": "GCODE_BLOCKED",
                    "message": "G-code blocked by safety validator.",
                },
                "blocked_commands": validation.blocked_commands,
                "errors": validation.errors,
                "warnings": validation.warnings,
            }

        if not adapter.capabilities.can_send_gcode:
            return _error_dict(
                f"send_gcode is not supported by the {adapter.name} adapter.",
                code="UNSUPPORTED",
            )

        adapter.send_gcode(cmd_list)

        result: Dict[str, Any] = {
            "success": True,
            "commands_sent": cmd_list,
            "count": len(cmd_list),
            "message": f"Sent {len(cmd_list)} G-code command(s).",
        }
        if validation.warnings:
            result["warnings"] = validation.warnings
        return result

    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in send_gcode")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# G-code validation tool
# ---------------------------------------------------------------------------


@mcp.tool()
def validate_gcode(commands: str) -> dict:
    """Validate G-code commands without sending them to the printer.

    Args:
        commands: One or more G-code commands separated by newlines.

    Returns a JSON object with:
    - ``valid``: whether all commands passed safety checks
    - ``commands``: the parsed command list
    - ``errors``: blocking issues (temperature limits, firmware commands)
    - ``warnings``: non-blocking advisories (Z below bed, high feedrate)
    - ``blocked_commands``: specific commands that were blocked

    Use this to preview what ``send_gcode`` would accept or reject.
    """
    raw_lines = re.split(r"[\n\r]+", commands.strip())
    cmd_list = [line.strip() for line in raw_lines if line.strip()]

    if not cmd_list:
        return _error_dict("No commands provided.", code="INVALID_ARGS")

    result = _validate_gcode_impl(cmd_list)
    return {
        "success": True,
        "valid": result.valid,
        "commands": result.commands,
        "errors": result.errors,
        "warnings": result.warnings,
        "blocked_commands": result.blocked_commands,
    }


# ---------------------------------------------------------------------------
# Fleet management tools
# ---------------------------------------------------------------------------


@mcp.tool()
def fleet_status() -> dict:
    """Get the status of all registered printers in the fleet.

    Returns a list of printer snapshots including name, backend type,
    connection status, operational state, and temperatures.  Printers
    that fail to respond are reported as offline rather than raising.

    If no printers are registered yet, the current adapter (from env config)
    is auto-registered as "default".
    """
    try:
        # Auto-register the env-configured adapter if registry is empty
        if _registry.count == 0:
            try:
                adapter = _get_adapter()
                _registry.register("default", adapter)
            except RuntimeError:
                pass  # No adapter configured

        if _registry.count == 0:
            return {
                "success": True,
                "printers": [],
                "count": 0,
                "message": "No printers registered.",
            }

        status = _registry.get_fleet_status()
        idle = _registry.get_idle_printers()
        return {
            "success": True,
            "printers": status,
            "count": len(status),
            "idle_printers": idle,
        }
    except Exception as exc:
        logger.exception("Unexpected error in fleet_status")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def register_printer(
    name: str,
    printer_type: str,
    host: str,
    api_key: str | None = None,
    serial: str | None = None,
) -> dict:
    """Register a new printer in the fleet.

    Args:
        name: Unique human-readable name (e.g. "voron-350", "bambu-x1c").
        printer_type: Backend type -- "octoprint", "moonraker", or "bambu".
        host: Base URL or IP address of the printer.
        api_key: API key (required for OctoPrint and Bambu, optional for
            Moonraker).  For Bambu printers this is the LAN Access Code.
        serial: Printer serial number (required for Bambu printers).

    Once registered the printer appears in ``fleet_status()`` and can be
    targeted by ``submit_job()``.
    """
    try:
        if printer_type == "octoprint":
            if not api_key:
                return _error_dict(
                    "api_key is required for OctoPrint printers.",
                    code="INVALID_ARGS",
                )
            adapter = OctoPrintAdapter(host=host, api_key=api_key)
        elif printer_type == "moonraker":
            adapter = MoonrakerAdapter(host=host, api_key=api_key or None)
        elif printer_type == "bambu":
            if not api_key:
                return _error_dict(
                    "api_key (LAN Access Code) is required for Bambu printers.",
                    code="INVALID_ARGS",
                )
            if not serial:
                return _error_dict(
                    "serial is required for Bambu printers.",
                    code="INVALID_ARGS",
                )
            adapter = BambuAdapter(host=host, access_code=api_key, serial=serial)
        else:
            return _error_dict(
                f"Unsupported printer_type: {printer_type!r}. "
                "Supported: 'octoprint', 'moonraker', 'bambu'.",
                code="INVALID_ARGS",
            )

        _registry.register(name, adapter)
        return {
            "success": True,
            "message": f"Registered printer {name!r} ({printer_type} @ {host}).",
            "name": name,
        }
    except Exception as exc:
        logger.exception("Unexpected error in register_printer")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Job queue tools
# ---------------------------------------------------------------------------


@mcp.tool()
def submit_job(
    file_name: str,
    printer_name: str | None = None,
    priority: int = 0,
) -> dict:
    """Submit a print job to the queue.

    Args:
        file_name: G-code file name (must already exist on the printer).
        printer_name: Target printer name, or omit to let the scheduler
            pick any idle printer.
        priority: Higher values are scheduled first (default 0).

    Jobs are executed in priority order, with FIFO tie-breaking.
    Use ``job_status`` to check progress and ``queue_summary`` for an overview.
    """
    try:
        job_id = _queue.submit(
            file_name=file_name,
            printer_name=printer_name,
            submitted_by="mcp-agent",
            priority=priority,
        )
        _event_bus.publish(Event(
            type=EventType.JOB_QUEUED,
            data={"job_id": job_id, "file_name": file_name, "printer_name": printer_name},
            source="mcp",
        ))
        return {
            "success": True,
            "job_id": job_id,
            "message": f"Job {job_id} submitted to queue.",
        }
    except Exception as exc:
        logger.exception("Unexpected error in submit_job")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def job_status(job_id: str) -> dict:
    """Get the status of a queued or completed print job.

    Args:
        job_id: The job ID returned by ``submit_job``.

    Returns the full job record including status, timing, and metadata.
    """
    try:
        job = _queue.get_job(job_id)
        return {
            "success": True,
            "job": job.to_dict(),
        }
    except JobNotFoundError:
        return _error_dict(f"Job not found: {job_id!r}", code="NOT_FOUND")
    except Exception as exc:
        logger.exception("Unexpected error in job_status")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def queue_summary() -> dict:
    """Get an overview of the print job queue.

    Returns counts by status, next job to execute, and recent jobs.
    """
    try:
        summary = _queue.summary()
        next_job = _queue.next_job()
        recent = _queue.list_jobs(limit=10)
        return {
            "success": True,
            "counts": summary,
            "pending": _queue.pending_count(),
            "active": _queue.active_count(),
            "total": _queue.total_count,
            "next_job": next_job.to_dict() if next_job else None,
            "recent_jobs": [j.to_dict() for j in recent],
        }
    except Exception as exc:
        logger.exception("Unexpected error in queue_summary")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def cancel_job(job_id: str) -> dict:
    """Cancel a queued or running print job.

    Args:
        job_id: The job ID to cancel.

    Only jobs in QUEUED or PRINTING state can be cancelled.
    """
    try:
        job = _queue.cancel(job_id)
        _event_bus.publish(Event(
            type=EventType.JOB_CANCELLED,
            data={"job_id": job_id},
            source="mcp",
        ))
        return {
            "success": True,
            "job": job.to_dict(),
            "message": f"Job {job_id} cancelled.",
        }
    except JobNotFoundError:
        return _error_dict(f"Job not found: {job_id!r}", code="NOT_FOUND")
    except ValueError as exc:
        return _error_dict(str(exc), code="INVALID_STATE")
    except Exception as exc:
        logger.exception("Unexpected error in cancel_job")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Event tools
# ---------------------------------------------------------------------------


@mcp.tool()
def recent_events(limit: int = 20) -> dict:
    """Get recent events from the Kiln event bus.

    Args:
        limit: Maximum number of events to return (default 20, max 100).

    Returns events covering job lifecycle, printer state changes,
    safety warnings, and more.
    """
    try:
        capped = min(max(limit, 1), 100)
        events = _event_bus.recent_events(limit=capped)
        return {
            "success": True,
            "events": [e.to_dict() for e in events],
            "count": len(events),
        }
    except Exception as exc:
        logger.exception("Unexpected error in recent_events")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_GCODE_EXTENSIONS = {".gcode", ".gco", ".g"}
_MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB


def _validate_local_file(file_path: str) -> Dict[str, Any]:
    """Validate a local G-code file without depending on octoprint_cli.

    Returns a dict with ``valid`` (bool), ``errors``, ``warnings``, and
    ``info`` keys.
    """
    errors: List[str] = []
    warnings: List[str] = []
    info: Dict[str, Any] = {"size_bytes": 0, "extension": ""}

    path = Path(file_path)

    if not path.exists():
        errors.append(f"File not found: {file_path}")
        return {"valid": False, "errors": errors, "warnings": warnings, "info": info}

    if not path.is_file():
        errors.append(f"Path is not a regular file: {file_path}")
        return {"valid": False, "errors": errors, "warnings": warnings, "info": info}

    try:
        with path.open("rb") as fh:
            fh.read(1)
    except PermissionError:
        errors.append(f"File is not readable (permission denied): {file_path}")
        return {"valid": False, "errors": errors, "warnings": warnings, "info": info}
    except OSError as exc:
        errors.append(f"Cannot read file: {exc}")
        return {"valid": False, "errors": errors, "warnings": warnings, "info": info}

    ext = path.suffix.lower()
    info["extension"] = ext
    if ext not in _GCODE_EXTENSIONS:
        errors.append(
            f"Unsupported file extension '{ext}'. "
            f"Expected one of: {', '.join(sorted(_GCODE_EXTENSIONS))}"
        )

    try:
        size = path.stat().st_size
    except OSError as exc:
        errors.append(f"Could not determine file size: {exc}")
        return {"valid": False, "errors": errors, "warnings": warnings, "info": info}

    info["size_bytes"] = size

    if size == 0:
        errors.append("File is empty (0 bytes)")
    elif size >= _MAX_FILE_SIZE:
        errors.append(
            f"File is too large ({size} bytes). "
            f"Maximum allowed size is {_MAX_FILE_SIZE} bytes."
        )
    elif size >= 500 * 1024 * 1024:
        warnings.append(
            f"File is very large ({size} bytes). "
            "Upload may take a while."
        )

    valid = len(errors) == 0
    return {"valid": valid, "errors": errors, "warnings": warnings, "info": info}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Kiln MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
