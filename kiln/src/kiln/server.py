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
``KILN_THINGIVERSE_TOKEN``
    Thingiverse API app token for model search and download.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import time
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
from kiln.scheduler import JobScheduler
from kiln.persistence import get_db
from kiln.webhooks import WebhookManager
from kiln.auth import AuthManager
from kiln.billing import BillingLedger, FeePolicy
from kiln.thingiverse import (
    ThingiverseClient,
    ThingiverseError,
    ThingiverseAuthError,
    ThingiverseNotFoundError,
    ThingiverseRateLimitError,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_PRINTER_HOST: str = os.environ.get("KILN_PRINTER_HOST", "")
_PRINTER_API_KEY: str = os.environ.get("KILN_PRINTER_API_KEY", "")
_PRINTER_TYPE: str = os.environ.get("KILN_PRINTER_TYPE", "octoprint")
_PRINTER_SERIAL: str = os.environ.get("KILN_PRINTER_SERIAL", "")
_THINGIVERSE_TOKEN: str = os.environ.get("KILN_THINGIVERSE_TOKEN", "")

# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "kiln",
    instructions=(
        "Agent infrastructure for physical fabrication via 3D printing. "
        "Provides tools to monitor printer status, manage files, control "
        "print jobs, adjust temperatures, send raw G-code, run "
        "pre-flight safety checks, and discover 3D models on Thingiverse.\n\n"
        "Start with `printer_status` to see what the printer is doing. "
        "Use `preflight_check` before printing. Use `fleet_status` to "
        "manage multiple printers. Use `validate_gcode` before `send_gcode` "
        "for raw commands. Submit jobs via `submit_job` for queued execution. "
        "Use `search_models` to find printable models on Thingiverse, "
        "`model_details` to inspect them, and `download_model` to fetch "
        "files for printing."
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
        if BambuAdapter is None:
            raise RuntimeError(
                "Bambu support requires paho-mqtt.  "
                "Install it with: pip install 'kiln[bambu]' or pip install paho-mqtt"
            )
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
_scheduler = JobScheduler(_queue, _registry, _event_bus)
_webhook_mgr = WebhookManager(_event_bus)
_auth = AuthManager()
_billing = BillingLedger()
_start_time = time.time()

# Thingiverse client (lazy -- created on first use so the module can be
# imported without requiring the token env var).
_thingiverse: Optional[ThingiverseClient] = None


def _get_thingiverse() -> ThingiverseClient:
    """Return the lazily-initialised Thingiverse client."""
    global _thingiverse  # noqa: PLW0603

    if _thingiverse is not None:
        return _thingiverse

    token = _THINGIVERSE_TOKEN
    if not token:
        raise RuntimeError(
            "KILN_THINGIVERSE_TOKEN environment variable is not set.  "
            "Set it to your Thingiverse API app token "
            "(create one at https://www.thingiverse.com/apps/create)."
        )
    _thingiverse = ThingiverseClient(token=token)
    return _thingiverse


def _error_dict(message: str, code: str = "ERROR") -> Dict[str, Any]:
    """Build a standardised error response dict."""
    return {"success": False, "error": {"code": code, "message": message}}


def _check_auth(scope: str) -> Optional[Dict[str, Any]]:
    """Check authentication for a tool invocation.

    Returns ``None`` if the request is allowed (either auth is disabled or the
    token is valid with the required *scope*).  Returns an error dict suitable
    for direct return from a tool handler when the request must be rejected.

    This is intentionally a no-op when authentication is not configured so
    that existing deployments continue to work without changes.
    """
    if not _auth.enabled:
        return None

    token = os.environ.get("KILN_MCP_AUTH_TOKEN", "")
    result = _auth.check_request(key=token, scope=scope)
    if result.get("authenticated"):
        return None
    return _error_dict(
        result.get("error", "Authentication failed."),
        code="AUTH_ERROR",
    )


# ---------------------------------------------------------------------------
# Persistence hooks — save job/event changes to SQLite
# ---------------------------------------------------------------------------

def _persist_event(event: Event) -> None:
    """EventBus subscriber that writes every event to SQLite."""
    try:
        db = get_db()
        db.log_event(
            event_type=event.type.value,
            data=event.data,
            source=event.source,
            timestamp=event.timestamp,
        )
    except Exception:
        logger.debug("Failed to persist event %s", event.type.value, exc_info=True)

    # Also persist job state changes
    job_events = {
        EventType.JOB_QUEUED, EventType.JOB_STARTED,
        EventType.JOB_COMPLETED, EventType.JOB_FAILED, EventType.JOB_CANCELLED,
    }
    if event.type in job_events and "job_id" in event.data:
        try:
            job = _queue.get_job(event.data["job_id"])
            db = get_db()
            db.save_job({
                "id": job.id,
                "file_name": job.file_name,
                "printer_name": job.printer_name,
                "status": job.status.value,
                "priority": job.priority,
                "submitted_by": job.submitted_by,
                "submitted_at": job.created_at,
                "started_at": job.started_at,
                "completed_at": job.completed_at,
                "error_message": job.error,
            })
        except Exception:
            logger.debug("Failed to persist job %s", event.data.get("job_id"), exc_info=True)


def _billing_hook(event: Event) -> None:
    """EventBus subscriber that records fees for completed network jobs.

    Only jobs with ``network_cost`` in event data are billable — all
    local printing is free.
    """
    if event.type != EventType.JOB_COMPLETED:
        return
    network_cost = event.data.get("network_cost")
    if network_cost is None:
        return  # Local job — free
    try:
        fee_calc = _billing.calculate_fee(float(network_cost))
        _billing.record_charge(event.data["job_id"], fee_calc)
        logger.info(
            "Billing: job %s network_cost=%.2f fee=%.2f (waived=%s)",
            event.data["job_id"],
            network_cost,
            fee_calc.fee_amount,
            fee_calc.waived,
        )
    except Exception:
        logger.debug("Failed to record billing for job %s", event.data.get("job_id"), exc_info=True)


# Wire subscribers (runs automatically on import)
_event_bus.subscribe(None, _persist_event)
_event_bus.subscribe(EventType.JOB_COMPLETED, _billing_hook)


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
    if err := _check_auth("files"):
        return err
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
    if err := _check_auth("files"):
        return err
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
    if err := _check_auth("print"):
        return err
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
    if err := _check_auth("print"):
        return err
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
    if err := _check_auth("print"):
        return err
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
    if err := _check_auth("print"):
        return err
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
    if err := _check_auth("temperature"):
        return err
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
    if err := _check_auth("print"):
        return err
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
    if err := _check_auth("admin"):
        return err
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
            if BambuAdapter is None:
                return _error_dict(
                    "Bambu support requires paho-mqtt.  "
                    "Install it with: pip install paho-mqtt",
                    code="MISSING_DEPENDENCY",
                )
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


@mcp.tool()
def discover_printers(timeout: float = 5.0) -> dict:
    """Scan the local network for 3D printers.

    Uses mDNS/Bonjour and HTTP subnet probing to find OctoPrint,
    Moonraker, and Bambu Lab printers on the local network.

    Args:
        timeout: Maximum scan duration in seconds (default 5).

    Returns a list of discovered printers with host, port, type, and
    whether the API is reachable.  Use ``register_printer`` to add
    discovered printers to the fleet.
    """
    try:
        from kiln.discovery import discover_printers as _discover
        results = _discover(timeout=timeout)
        return {
            "success": True,
            "printers": [p.to_dict() for p in results],
            "count": len(results),
            "message": f"Found {len(results)} printer(s) on the network.",
        }
    except Exception as exc:
        logger.exception("Unexpected error in discover_printers")
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
    if err := _check_auth("queue"):
        return err
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
    if err := _check_auth("queue"):
        return err
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


@mcp.tool()
def job_history(limit: int = 20, status: str | None = None) -> dict:
    """Get history of completed, failed, and cancelled print jobs.

    Args:
        limit: Maximum number of jobs to return (default 20, max 100).
        status: Optional filter by status -- "completed", "failed", or
            "cancelled".  Omit to show all finished jobs.

    Returns recent job records from newest to oldest.
    """
    try:
        capped = min(max(limit, 1), 100)
        all_jobs = _queue.list_jobs(limit=capped)

        finished_statuses = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
        if status:
            status_map = {
                "completed": JobStatus.COMPLETED,
                "failed": JobStatus.FAILED,
                "cancelled": JobStatus.CANCELLED,
            }
            target = status_map.get(status.lower())
            if target is None:
                return _error_dict(
                    f"Invalid status filter: {status!r}. Use 'completed', 'failed', or 'cancelled'.",
                    code="INVALID_ARGS",
                )
            jobs = [j for j in all_jobs if j.status == target]
        else:
            jobs = [j for j in all_jobs if j.status in finished_statuses]

        return {
            "success": True,
            "jobs": [j.to_dict() for j in jobs],
            "count": len(jobs),
        }
    except Exception as exc:
        logger.exception("Unexpected error in job_history")
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
# Billing tools
# ---------------------------------------------------------------------------


@mcp.tool()
def billing_summary() -> dict:
    """Get a summary of Kiln network job fees for the current month.

    Shows total fees collected, number of network jobs, free tier usage,
    and the current fee policy.  Only network-routed jobs (e.g. 3DOS)
    incur fees -- all local printing is free.
    """
    try:
        revenue = _billing.monthly_revenue()
        policy = _billing._policy
        return {
            "success": True,
            "month_revenue": revenue,
            "fee_policy": {
                "network_fee_percent": policy.network_fee_percent,
                "min_fee_usd": policy.min_fee_usd,
                "max_fee_usd": policy.max_fee_usd,
                "free_tier_jobs": policy.free_tier_jobs,
                "currency": policy.currency,
            },
            "network_jobs_this_month": _billing.network_jobs_this_month(),
        }
    except Exception as exc:
        logger.exception("Unexpected error in billing_summary")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Thingiverse tools — 3D model discovery and download
# ---------------------------------------------------------------------------


@mcp.tool()
def search_models(
    query: str,
    page: int = 1,
    per_page: int = 10,
    sort: str = "relevant",
) -> dict:
    """Search Thingiverse for 3D-printable models.

    Args:
        query: Search keywords (e.g. "raspberry pi case", "benchy").
        page: Page number for pagination (1-based, default 1).
        per_page: Results per page (default 10, max 100).
        sort: Sort order — "relevant", "popular", "newest", or "makes".

    Returns a list of model summaries including name, creator, thumbnail,
    and download/like counts.  Use ``model_details`` with the ``id`` to
    get full information, and ``model_files`` to see downloadable files.
    """
    try:
        client = _get_thingiverse()
        results = client.search(query, page=page, per_page=per_page, sort=sort)
        return {
            "success": True,
            "query": query,
            "models": [r.to_dict() for r in results],
            "count": len(results),
            "page": page,
        }
    except (ThingiverseError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in search_models")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def model_details(thing_id: int) -> dict:
    """Get full details for a Thingiverse model.

    Args:
        thing_id: Numeric thing ID (from ``search_models`` results).

    Returns comprehensive metadata including description, instructions,
    license, tags, and file count.
    """
    try:
        client = _get_thingiverse()
        thing = client.get_thing(thing_id)
        return {
            "success": True,
            "model": thing.to_dict(),
        }
    except ThingiverseNotFoundError:
        return _error_dict(f"Model {thing_id} not found.", code="NOT_FOUND")
    except (ThingiverseError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in model_details")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def model_files(thing_id: int) -> dict:
    """List downloadable files for a Thingiverse model.

    Args:
        thing_id: Numeric thing ID.

    Returns a list of files with name, size, and download URL.
    Use ``download_model`` with the ``file_id`` to save a file locally.
    """
    try:
        client = _get_thingiverse()
        files = client.get_files(thing_id)
        return {
            "success": True,
            "thing_id": thing_id,
            "files": [f.to_dict() for f in files],
            "count": len(files),
        }
    except ThingiverseNotFoundError:
        return _error_dict(f"Model {thing_id} not found.", code="NOT_FOUND")
    except (ThingiverseError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in model_files")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def download_model(
    file_id: int,
    dest_dir: str = "/tmp/kiln_downloads",
    file_name: str | None = None,
) -> dict:
    """Download a model file from Thingiverse to local storage.

    Args:
        file_id: Numeric file ID (from ``model_files`` results).
        dest_dir: Local directory to save the file in (default:
            ``/tmp/kiln_downloads``).
        file_name: Override the saved file name.  Defaults to the
            original name from Thingiverse.

    After downloading, the file can be uploaded to a printer with
    ``upload_file`` and then printed with ``start_print``.
    """
    try:
        client = _get_thingiverse()
        path = client.download_file(file_id, dest_dir, file_name=file_name)
        return {
            "success": True,
            "file_id": file_id,
            "local_path": path,
            "message": f"Downloaded to {path}",
        }
    except ThingiverseNotFoundError:
        return _error_dict(f"File {file_id} not found.", code="NOT_FOUND")
    except (ThingiverseError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in download_model")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def download_and_upload(
    file_id: int,
    printer_name: str | None = None,
) -> dict:
    """Download a Thingiverse file and upload it to the printer in one step.

    Args:
        file_id: Numeric file ID (from ``model_files`` results).
        printer_name: Target printer name.  Omit to use the default
            (env-configured) printer.

    This is the fastest way to go from a Thingiverse model to a file on
    the printer ready to print.  Combines ``download_model`` and
    ``upload_file`` into a single action.
    """
    if err := _check_auth("files"):
        return err
    try:
        # Step 1: Download from Thingiverse
        client = _get_thingiverse()
        local_path = client.download_file(file_id, "/tmp/kiln_downloads")

        # Step 2: Upload to printer
        if printer_name:
            adapter = _registry.get(printer_name)
        else:
            adapter = _get_adapter()

        result = adapter.upload_file(local_path)

        return {
            "success": True,
            "file_id": file_id,
            "local_path": local_path,
            "upload": result.to_dict(),
            "message": f"Downloaded and uploaded to printer.",
        }
    except ThingiverseNotFoundError:
        return _error_dict(f"File {file_id} not found on Thingiverse.", code="NOT_FOUND")
    except PrinterNotFoundError:
        return _error_dict(f"Printer {printer_name!r} not found.", code="NOT_FOUND")
    except (ThingiverseError, PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in download_and_upload")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def browse_models(
    browse_type: str = "popular",
    page: int = 1,
    per_page: int = 10,
    category: str | None = None,
) -> dict:
    """Browse Thingiverse models by popularity, recency, or category.

    Args:
        browse_type: One of "popular", "newest", or "featured".
        page: Page number (1-based, default 1).
        per_page: Results per page (default 10, max 100).
        category: Optional category slug to filter by (e.g. "3d-printing",
            "art").  Use ``list_categories`` to see available slugs.

    Returns model summaries similar to ``search_models``.
    """
    try:
        client = _get_thingiverse()

        if category:
            results = client.category_things(category, page=page, per_page=per_page)
        elif browse_type == "popular":
            results = client.popular(page=page, per_page=per_page)
        elif browse_type == "newest":
            results = client.newest(page=page, per_page=per_page)
        elif browse_type == "featured":
            results = client.featured(page=page, per_page=per_page)
        else:
            return _error_dict(
                f"Unknown browse_type: {browse_type!r}.  "
                "Supported: 'popular', 'newest', 'featured'.",
                code="INVALID_ARGS",
            )

        return {
            "success": True,
            "browse_type": browse_type if not category else f"category:{category}",
            "models": [r.to_dict() for r in results],
            "count": len(results),
            "page": page,
        }
    except (ThingiverseError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in browse_models")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def list_model_categories() -> dict:
    """List available Thingiverse content categories.

    Returns category names and slugs.  Pass a slug to
    ``browse_models(category=...)`` to browse models in that category.
    """
    try:
        client = _get_thingiverse()
        cats = client.list_categories()
        return {
            "success": True,
            "categories": [c.to_dict() for c in cats],
            "count": len(cats),
        }
    except (ThingiverseError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in list_model_categories")
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
# MCP Resources — read-only data endpoints for agent context
# ---------------------------------------------------------------------------


@mcp.tool()
def kiln_health() -> dict:
    """Get a health check for the Kiln system.

    Returns versions, uptime, module availability, scheduler status,
    webhook status, and overall system health.  Use this to verify the
    system is running correctly.
    """
    import kiln

    uptime_secs = time.time() - _start_time
    hours, rem = divmod(int(uptime_secs), 3600)
    mins, secs = divmod(rem, 60)

    modules = {
        "scheduler": _scheduler.is_running,
        "webhooks": _webhook_mgr.is_running,
        "persistence": True,
        "auth_enabled": _auth.enabled,
        "billing": True,
        "thingiverse": bool(_THINGIVERSE_TOKEN),
    }

    try:
        from kiln.printers.bambu import BambuAdapter as _Bambu
        modules["bambu_available"] = True
    except ImportError:
        modules["bambu_available"] = False

    return {
        "success": True,
        "version": kiln.__version__,
        "uptime_seconds": round(uptime_secs, 1),
        "uptime_human": f"{hours}h {mins}m {secs}s",
        "printers_registered": _registry.count,
        "queue_depth": _queue.total_count,
        "scheduler_running": _scheduler.is_running,
        "webhook_endpoints": len(_webhook_mgr.list_endpoints()),
        "modules": modules,
        "healthy": True,
    }


@mcp.tool()
def register_webhook(
    url: str,
    events: list[str] | None = None,
    secret: str | None = None,
    description: str = "",
) -> dict:
    """Register a webhook endpoint to receive Kiln event notifications.

    Args:
        url: The HTTPS URL that will receive POST requests with event payloads.
        events: Optional list of event types to subscribe to (e.g.
            ["job.completed", "print.failed"]).  If omitted, all events are sent.
        secret: Optional shared secret for HMAC-SHA256 payload signing.
        description: Human-readable label for this endpoint.

    Returns the registered endpoint ID.  Use ``list_webhooks`` to see all
    endpoints and ``delete_webhook`` to remove one.
    """
    if err := _check_auth("admin"):
        return err
    try:
        endpoint = _webhook_mgr.register(
            url=url,
            events=events,
            secret=secret,
            description=description,
        )
        return {
            "success": True,
            "endpoint_id": endpoint.id,
            "url": endpoint.url,
            "events": sorted(endpoint.events),
            "message": f"Webhook registered: {endpoint.id}",
        }
    except Exception as exc:
        logger.exception("Unexpected error in register_webhook")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def list_webhooks() -> dict:
    """List all registered webhook endpoints.

    Returns endpoint details including URL, subscribed events, and
    delivery statistics.
    """
    try:
        endpoints = _webhook_mgr.list_endpoints()
        return {
            "success": True,
            "endpoints": [
                {
                    "id": ep.id,
                    "url": ep.url,
                    "events": sorted(ep.events),
                    "description": ep.description,
                    "active": ep.active,
                }
                for ep in endpoints
            ],
            "count": len(endpoints),
        }
    except Exception as exc:
        logger.exception("Unexpected error in list_webhooks")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def delete_webhook(endpoint_id: str) -> dict:
    """Delete a registered webhook endpoint.

    Args:
        endpoint_id: The endpoint ID returned by ``register_webhook``.

    Once deleted, the endpoint will no longer receive event notifications.
    """
    if err := _check_auth("admin"):
        return err
    try:
        removed = _webhook_mgr.unregister(endpoint_id)
        if removed:
            return {
                "success": True,
                "message": f"Webhook {endpoint_id} deleted.",
            }
        return _error_dict(
            f"Webhook {endpoint_id!r} not found.",
            code="NOT_FOUND",
        )
    except Exception as exc:
        logger.exception("Unexpected error in delete_webhook")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.resource("kiln://status")
def resource_status() -> str:
    """Live snapshot of the entire Kiln system: printers, queue, and recent events."""
    import json

    # Fleet
    printers: List[Dict[str, Any]] = []
    if _registry.count > 0:
        printers = _registry.get_fleet_status()
    elif _PRINTER_HOST:
        try:
            adapter = _get_adapter()
            state = adapter.get_state()
            printers = [{
                "name": "default",
                "backend": adapter.name,
                "connected": state.connected,
                "state": state.state.value,
            }]
        except Exception:
            pass

    # Queue
    q_summary = _queue.summary()

    # Events
    events = _event_bus.recent_events(limit=10)

    return json.dumps({
        "printers": printers,
        "printer_count": len(printers),
        "queue": {
            "counts": q_summary,
            "pending": _queue.pending_count(),
            "active": _queue.active_count(),
            "total": _queue.total_count,
        },
        "recent_events": [e.to_dict() for e in events],
    }, default=str)


@mcp.resource("kiln://printers")
def resource_printers() -> str:
    """Fleet status for all registered printers."""
    import json

    if _registry.count == 0:
        try:
            adapter = _get_adapter()
            _registry.register("default", adapter)
        except RuntimeError:
            pass

    printers = _registry.get_fleet_status() if _registry.count > 0 else []
    idle = _registry.get_idle_printers() if _registry.count > 0 else []

    return json.dumps({
        "printers": printers,
        "count": len(printers),
        "idle_printers": idle,
    }, default=str)


@mcp.resource("kiln://printers/{printer_name}")
def resource_printer_detail(printer_name: str) -> str:
    """Detailed status for a specific printer by name."""
    import json

    try:
        adapter = _registry.get(printer_name)
        state = adapter.get_state()
        job = adapter.get_job()
        caps = adapter.capabilities
        return json.dumps({
            "name": printer_name,
            "backend": adapter.name,
            "state": state.to_dict(),
            "job": job.to_dict(),
            "capabilities": caps.to_dict(),
        }, default=str)
    except PrinterNotFoundError:
        return json.dumps({"error": f"Printer {printer_name!r} not found"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@mcp.resource("kiln://queue")
def resource_queue() -> str:
    """Current job queue summary and recent jobs."""
    import json

    summary = _queue.summary()
    next_job = _queue.next_job()
    recent = _queue.list_jobs(limit=20)

    return json.dumps({
        "counts": summary,
        "pending": _queue.pending_count(),
        "active": _queue.active_count(),
        "total": _queue.total_count,
        "next_job": next_job.to_dict() if next_job else None,
        "recent_jobs": [j.to_dict() for j in recent],
    }, default=str)


@mcp.resource("kiln://queue/{job_id}")
def resource_job_detail(job_id: str) -> str:
    """Detailed status for a specific job by ID."""
    import json

    try:
        job = _queue.get_job(job_id)
        return json.dumps({"job": job.to_dict()}, default=str)
    except JobNotFoundError:
        return json.dumps({"error": f"Job {job_id!r} not found"})


@mcp.resource("kiln://events")
def resource_events() -> str:
    """Recent events from the Kiln event bus (last 50)."""
    import json

    events = _event_bus.recent_events(limit=50)
    return json.dumps({
        "events": [e.to_dict() for e in events],
        "count": len(events),
    }, default=str)


# ---------------------------------------------------------------------------
# MCP Prompt templates — multi-step workflow guides for agents
# ---------------------------------------------------------------------------


@mcp.prompt()
def print_workflow() -> str:
    """Step-by-step guide for printing a file on a 3D printer."""
    return (
        "To print a file on a 3D printer, follow these steps:\n\n"
        "1. Call `printer_status` to check the printer is connected and idle\n"
        "2. Call `preflight_check` to verify the printer is ready\n"
        "3. Call `printer_files` to see available files, or `upload_file` to upload a new one\n"
        "4. Call `start_print` with the file name to begin printing\n"
        "5. Call `printer_status` periodically to monitor progress\n\n"
        "If you need to find a model first, use `search_models` to search Thingiverse, "
        "then `download_model` to save it locally, then `upload_file` to send it to the printer."
    )


@mcp.prompt()
def fleet_workflow() -> str:
    """Guide for managing multiple printers in a fleet."""
    return (
        "To manage a fleet of printers:\n\n"
        "1. Call `fleet_status` to see all registered printers and their states\n"
        "2. Use `register_printer` to add new printers (octoprint, moonraker, or bambu)\n"
        "3. Submit jobs with `submit_job` — the scheduler auto-dispatches to idle printers\n"
        "4. Monitor via `queue_summary` and `job_status`\n"
        "5. Check `recent_events` for lifecycle updates\n\n"
        "The scheduler runs in the background, automatically assigning queued jobs "
        "to available printers based on priority."
    )


@mcp.prompt()
def troubleshooting() -> str:
    """Common troubleshooting steps for 3D printing issues."""
    return (
        "Common troubleshooting steps:\n\n"
        "1. Call `kiln_health` to verify the system is healthy\n"
        "2. Call `printer_status` to check connection and state\n"
        "3. If printer shows 'error', check temperatures with `printer_status`\n"
        "4. Use `send_gcode` with 'M999' to reset the printer from error state\n"
        "5. Use `preflight_check` to run a full readiness diagnosis\n"
        "6. Check `recent_events` for error history\n\n"
        "For temperature issues:\n"
        "- PLA: hotend 200-210C, bed 60C\n"
        "- PETG: hotend 230-250C, bed 80-85C\n"
        "- ABS: hotend 240-260C, bed 100-110C"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Kiln MCP server."""
    # Auto-register the env-configured printer so the scheduler can
    # dispatch jobs even if no explicit register_printer call is made.
    if _PRINTER_HOST and _registry.count == 0:
        try:
            adapter = _get_adapter()
            _registry.register("default", adapter)
            logger.info("Auto-registered env-configured printer as 'default'")
        except Exception:
            logger.debug(
                "Could not auto-register env-configured printer", exc_info=True
            )

    # Start background services
    _scheduler.start()
    _webhook_mgr.start()
    logger.info("Kiln scheduler and webhook delivery started")

    # Graceful shutdown on exit
    atexit.register(_scheduler.stop)
    atexit.register(_webhook_mgr.stop)

    mcp.run()


if __name__ == "__main__":
    main()
