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
    PrusaConnectAdapter,
)
from kiln.gcode import validate_gcode as _validate_gcode_impl
from kiln.registry import PrinterRegistry, PrinterNotFoundError
from kiln.queue import PrintQueue, JobStatus, JobNotFoundError
from kiln.events import Event, EventBus, EventType
from kiln.scheduler import JobScheduler
from kiln.persistence import get_db
from kiln.webhooks import WebhookManager
from kiln.auth import AuthManager
from kiln.billing import BillingLedger, FeeCalculation, FeePolicy
from kiln.payments.manager import PaymentManager
from kiln.payments.base import PaymentError
from kiln.cost_estimator import CostEstimator
from kiln.materials import MaterialTracker
from kiln.bed_leveling import BedLevelManager, LevelingPolicy
from kiln.streaming import MJPEGProxy
from kiln.cloud_sync import CloudSyncManager, SyncConfig
from kiln.plugins import PluginManager, PluginContext
from kiln.fulfillment import (
    CraftcloudProvider,
    FulfillmentError,
    FulfillmentProvider,
    OrderRequest,
    QuoteRequest,
)
from kiln.thingiverse import (
    ThingiverseClient,
    ThingiverseError,
    ThingiverseAuthError,
    ThingiverseNotFoundError,
    ThingiverseRateLimitError,
)
from kiln.marketplaces import (
    MarketplaceRegistry,
    MarketplaceError,
    MarketplaceNotFoundError as MktNotFoundError,
    ThingiverseAdapter,
    MyMiniFactoryAdapter,
    Cults3DAdapter,
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
_MMF_API_KEY: str = os.environ.get("KILN_MMF_API_KEY", "")
_CULTS3D_USERNAME: str = os.environ.get("KILN_CULTS3D_USERNAME", "")
_CULTS3D_API_KEY: str = os.environ.get("KILN_CULTS3D_API_KEY", "")
_CRAFTCLOUD_API_KEY: str = os.environ.get("KILN_CRAFTCLOUD_API_KEY", "")

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
        "Use `search_all_models` to search across Thingiverse, MyMiniFactory, "
        "and Cults3D simultaneously, or `search_models` for Thingiverse only. "
        "Use `download_model` to fetch files and `download_and_upload` to go "
        "straight from marketplace to printer. "
        "Use `fulfillment_materials` and `fulfillment_quote` to outsource "
        "prints to external services like Craftcloud when local printers "
        "lack the material or capacity."
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
    elif printer_type == "prusaconnect":
        _adapter = PrusaConnectAdapter(host=host, api_key=api_key or None)
    else:
        raise RuntimeError(
            f"Unsupported printer type: {printer_type!r}.  "
            f"Supported types are 'octoprint', 'moonraker', 'bambu', and 'prusaconnect'."
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
_billing = BillingLedger(db=get_db())
_payment_mgr: Optional[PaymentManager] = None
_cost_estimator = CostEstimator()
_material_tracker = MaterialTracker(db=get_db(), event_bus=_event_bus)
_bed_level_mgr = BedLevelManager(
    db=get_db(), event_bus=_event_bus, registry=_registry,
)
_stream_proxy = MJPEGProxy()
_cloud_sync: Optional[CloudSyncManager] = None
_plugin_mgr = PluginManager()
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


# Marketplace registry (auto-registers adapters based on env vars)
_marketplace_registry = MarketplaceRegistry()


def _init_marketplace_registry() -> None:
    """Register marketplace adapters based on available credentials."""
    if _THINGIVERSE_TOKEN:
        try:
            client = _get_thingiverse()
            _marketplace_registry.register(ThingiverseAdapter(client))
        except Exception:
            logger.debug("Could not register Thingiverse adapter", exc_info=True)
    if _MMF_API_KEY:
        try:
            _marketplace_registry.register(MyMiniFactoryAdapter(api_key=_MMF_API_KEY))
        except Exception:
            logger.debug("Could not register MyMiniFactory adapter", exc_info=True)
    if _CULTS3D_USERNAME and _CULTS3D_API_KEY:
        try:
            _marketplace_registry.register(
                Cults3DAdapter(username=_CULTS3D_USERNAME, api_key=_CULTS3D_API_KEY)
            )
        except Exception:
            logger.debug("Could not register Cults3D adapter", exc_info=True)


_fulfillment: Optional[FulfillmentProvider] = None


def _get_fulfillment() -> FulfillmentProvider:
    """Return the lazily-initialised fulfillment provider."""
    global _fulfillment  # noqa: PLW0603

    if _fulfillment is not None:
        return _fulfillment

    if not _CRAFTCLOUD_API_KEY:
        raise RuntimeError(
            "KILN_CRAFTCLOUD_API_KEY environment variable is not set.  "
            "Set it to your Craftcloud API key to use fulfillment services."
        )
    _fulfillment = CraftcloudProvider(api_key=_CRAFTCLOUD_API_KEY)
    return _fulfillment


def _get_payment_mgr() -> PaymentManager:
    """Return the lazily-initialised payment manager."""
    global _payment_mgr  # noqa: PLW0603

    if _payment_mgr is not None:
        return _payment_mgr

    from kiln.cli.config import get_billing_config
    config = get_billing_config()
    _payment_mgr = PaymentManager(
        db=get_db(), config=config, event_bus=_event_bus, ledger=_billing,
    )

    # Auto-register providers from env vars.
    stripe_key = os.environ.get("KILN_STRIPE_SECRET_KEY", "")
    if stripe_key:
        try:
            from kiln.payments.stripe_provider import StripeProvider
            customer_id = config.get("stripe_customer_id")
            _payment_mgr.register_provider(
                StripeProvider(secret_key=stripe_key, customer_id=customer_id),
            )
        except Exception:
            logger.debug("Could not register Stripe provider")

    circle_key = os.environ.get("KILN_CIRCLE_API_KEY", "")
    if circle_key:
        try:
            from kiln.payments.circle_provider import CircleProvider
            _payment_mgr.register_provider(CircleProvider(api_key=circle_key))
        except Exception:
            logger.debug("Could not register Circle provider")

    return _payment_mgr


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
def start_print(file_name: str, skip_preflight: bool = False) -> dict:
    """Start printing a file that already exists on the printer.

    Automatically runs pre-flight safety checks before starting.  If any
    check fails the print is blocked and the check results are returned
    so the agent can diagnose and fix the issue.

    Args:
        file_name: Name or path of the file as shown by ``printer_files()``.
        skip_preflight: Set to ``True`` to bypass the automatic pre-flight
            checks (not recommended).
    """
    if err := _check_auth("print"):
        return err
    try:
        adapter = _get_adapter()

        # -- Automatic pre-flight safety gate --------------------------------
        if not skip_preflight:
            pf = preflight_check()
            if not pf.get("ready", False):
                return {
                    "success": False,
                    "error": pf.get("summary", "Pre-flight checks failed"),
                    "code": "PREFLIGHT_FAILED",
                    "preflight": pf,
                }

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
        elif printer_type == "prusaconnect":
            adapter = PrusaConnectAdapter(host=host, api_key=api_key or None)
        else:
            return _error_dict(
                f"Unsupported printer_type: {printer_type!r}. "
                "Supported: 'octoprint', 'moonraker', 'bambu', 'prusaconnect'.",
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
    """Get a summary of Kiln platform fees for the current month.

    Shows total fees collected, number of outsourced orders, free tier
    usage, and the current fee policy.  Only orders placed through
    external fulfillment services incur fees -- all local printing is free.
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


@mcp.tool()
def billing_setup_url(rail: str = "stripe") -> dict:
    """Get a URL to link a payment method for Kiln platform fees.

    Args:
        rail: Payment rail — ``"stripe"`` for credit card, ``"crypto"``
            for USDC on Solana/Base.

    Returns the setup URL.  Open it in a browser to complete payment
    method setup.  After setup, Kiln automatically charges the platform
    fee on each outsourced manufacturing order.
    """
    try:
        mgr = _get_payment_mgr()
        url = mgr.get_setup_url(rail=rail)
        return {"success": True, "setup_url": url, "rail": rail}
    except PaymentError as exc:
        return _error_dict(str(exc), code=getattr(exc, "code", "PAYMENT_ERROR"))
    except Exception as exc:
        logger.exception("Unexpected error in billing_setup_url")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def billing_status() -> dict:
    """Get enriched billing status including payment method info.

    Returns payment method details, monthly spend, spend limits,
    available payment rails, and fee policy.  More detailed than
    ``billing_summary`` — includes payment infrastructure state.
    """
    try:
        from kiln.cli.config import get_or_create_user_id
        user_id = get_or_create_user_id()
        mgr = _get_payment_mgr()
        data = mgr.get_billing_status(user_id)
        return {"success": True, **data}
    except Exception as exc:
        logger.exception("Unexpected error in billing_status")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def billing_history(limit: int = 20) -> dict:
    """Get recent billing charge history with payment outcomes.

    Args:
        limit: Maximum number of records to return (default 20).

    Returns charge records including order cost, fee amount, payment
    rail, payment status, and timestamps.
    """
    try:
        mgr = _get_payment_mgr()
        charges = mgr.get_billing_history(limit=limit)
        return {"success": True, "charges": charges, "count": len(charges)}
    except Exception as exc:
        logger.exception("Unexpected error in billing_history")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Multi-marketplace tools — unified search across all sources
# ---------------------------------------------------------------------------


@mcp.tool()
def search_all_models(
    query: str,
    page: int = 1,
    per_page: int = 10,
    sort: str = "relevant",
    sources: list[str] | None = None,
) -> dict:
    """Search across all connected 3D model marketplaces simultaneously.

    Searches Thingiverse, MyMiniFactory, and Cults3D in parallel and
    returns interleaved results from all sources.

    Args:
        query: Search keywords (e.g. "raspberry pi case", "benchy").
        page: Page number (1-based, default 1).
        per_page: Results per source (default 10).
        sort: Sort order — "relevant", "popular", or "newest".
        sources: Optional list to restrict search (e.g. ["thingiverse",
            "myminifactory"]).  Omit to search all connected sources.

    Each result includes a ``source`` field identifying the marketplace.
    Results also include ``is_free``, ``has_printable_files`` (has G-code),
    and ``has_sliceable_files`` (has STL/3MF) hints.

    Use ``model_details`` with the ``id`` to inspect, ``model_files``
    to see downloadable files, and ``download_model`` to save locally.
    """
    try:
        if _marketplace_registry.count == 0:
            _init_marketplace_registry()

        if _marketplace_registry.count == 0:
            return _error_dict(
                "No marketplace credentials configured.  Set at least one of: "
                "KILN_THINGIVERSE_TOKEN, KILN_MMF_API_KEY, "
                "KILN_CULTS3D_USERNAME + KILN_CULTS3D_API_KEY.",
                code="NO_MARKETPLACES",
            )

        results = _marketplace_registry.search_all(
            query,
            page=page,
            per_page=per_page,
            sort=sort,
            sources=sources,
        )
        return {
            "success": True,
            "query": query,
            "models": [r.to_dict() for r in results],
            "count": len(results),
            "page": page,
            "sources": _marketplace_registry.connected,
        }
    except MarketplaceError as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in search_all_models")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def marketplace_info() -> dict:
    """Show which 3D model marketplaces are connected and available.

    Returns the list of connected marketplace sources and their
    capabilities (search, download support, etc.).  Configure
    marketplaces via environment variables.
    """
    try:
        if _marketplace_registry.count == 0:
            _init_marketplace_registry()

        sources = []
        for name in _marketplace_registry.connected:
            adapter = _marketplace_registry.get(name)
            sources.append({
                "name": adapter.name,
                "display_name": adapter.display_name,
                "supports_download": adapter.supports_download,
            })

        env_hints = []
        if not _THINGIVERSE_TOKEN:
            env_hints.append("Set KILN_THINGIVERSE_TOKEN to enable Thingiverse")
        if not _MMF_API_KEY:
            env_hints.append("Set KILN_MMF_API_KEY to enable MyMiniFactory")
        if not (_CULTS3D_USERNAME and _CULTS3D_API_KEY):
            env_hints.append(
                "Set KILN_CULTS3D_USERNAME + KILN_CULTS3D_API_KEY to enable Cults3D"
            )

        return {
            "success": True,
            "connected": [s["name"] for s in sources],
            "sources": sources,
            "count": len(sources),
            "setup_hints": env_hints if env_hints else None,
        }
    except Exception as exc:
        logger.exception("Unexpected error in marketplace_info")
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
    file_id: str,
    source: str = "thingiverse",
    printer_name: str | None = None,
) -> dict:
    """Download a model file from any marketplace and upload it to a printer.

    Args:
        file_id: File ID (from ``model_files`` results).  For Thingiverse
            this is a numeric ID; for MyMiniFactory it's the file ID string.
        source: Which marketplace to download from — "thingiverse" (default)
            or "myminifactory".  Cults3D does not support direct downloads.
        printer_name: Target printer name.  Omit to use the default printer.

    This is the fastest way to go from a marketplace model to a file on
    the printer ready to print.  Combines download and upload into one step.
    """
    if err := _check_auth("files"):
        return err
    try:
        if _marketplace_registry.count == 0:
            _init_marketplace_registry()

        mkt = _marketplace_registry.get(source) if source != "thingiverse" else None

        # Step 1: Download from marketplace
        if mkt is not None:
            if not mkt.supports_download:
                return _error_dict(
                    f"{mkt.display_name} does not support direct downloads.",
                    code="UNSUPPORTED",
                )
            local_path = mkt.download_file(str(file_id), "/tmp/kiln_downloads")
        else:
            # Fallback to legacy Thingiverse client
            client = _get_thingiverse()
            local_path = client.download_file(int(file_id), "/tmp/kiln_downloads")

        # Step 2: Upload to printer
        if printer_name:
            adapter = _registry.get(printer_name)
        else:
            adapter = _get_adapter()

        result = adapter.upload_file(local_path)

        return {
            "success": True,
            "file_id": str(file_id),
            "source": source,
            "local_path": local_path,
            "upload": result.to_dict(),
            "message": f"Downloaded from {source} and uploaded to printer.",
        }
    except (ThingiverseNotFoundError, MktNotFoundError):
        return _error_dict(f"File {file_id} not found on {source}.", code="NOT_FOUND")
    except PrinterNotFoundError:
        return _error_dict(f"Printer {printer_name!r} not found.", code="NOT_FOUND")
    except (ThingiverseError, MarketplaceError, PrinterError, RuntimeError) as exc:
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
# Slicer tools
# ---------------------------------------------------------------------------


@mcp.tool()
def slice_model(
    input_path: str,
    output_dir: str | None = None,
    profile: str | None = None,
    slicer_path: str | None = None,
) -> dict:
    """Slice a 3D model (STL/3MF/STEP) to G-code using PrusaSlicer or OrcaSlicer.

    Args:
        input_path: Path to the input file (STL, 3MF, STEP, OBJ, AMF).
        output_dir: Directory for the output G-code.  Defaults to
            ``/tmp/kiln_sliced``.
        profile: Path to a slicer profile/config file (.ini or .json).
        slicer_path: Explicit path to the slicer binary.  Auto-detected
            if omitted.

    Returns a JSON object with the output G-code path.  The output file
    can then be uploaded to a printer with ``upload_file`` and printed
    with ``start_print``.
    """
    try:
        from kiln.slicer import SlicerError, SlicerNotFoundError, slice_file

        result = slice_file(
            input_path,
            output_dir=output_dir,
            profile=profile,
            slicer_path=slicer_path,
        )
        return {
            "success": True,
            **result.to_dict(),
        }
    except SlicerNotFoundError as exc:
        return _error_dict(str(exc), code="SLICER_NOT_FOUND")
    except SlicerError as exc:
        return _error_dict(str(exc), code="SLICER_ERROR")
    except FileNotFoundError as exc:
        return _error_dict(str(exc), code="FILE_NOT_FOUND")
    except Exception as exc:
        logger.exception("Unexpected error in slice_model")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def find_slicer_tool() -> dict:
    """Check if a slicer (PrusaSlicer/OrcaSlicer) is available on the system.

    Returns the slicer path, name, and version if found.
    """
    try:
        from kiln.slicer import SlicerNotFoundError
        from kiln.slicer import find_slicer as _find_slicer

        info = _find_slicer()
        return {
            "success": True,
            **info.to_dict(),
        }
    except SlicerNotFoundError as exc:
        return _error_dict(str(exc), code="SLICER_NOT_FOUND")
    except Exception as exc:
        logger.exception("Unexpected error in find_slicer_tool")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def slice_and_print(
    input_path: str,
    printer_name: str | None = None,
    profile: str | None = None,
) -> dict:
    """Slice a 3D model and immediately upload + print it in one step.

    Args:
        input_path: Path to the 3D model file (STL, 3MF, STEP, etc.).
        printer_name: Target printer name.  Omit for the default printer.
        profile: Path to a slicer profile/config file.

    Combines ``slice_model``, ``upload_file``, and ``start_print`` into
    a single action.
    """
    if err := _check_auth("print"):
        return err
    try:
        from kiln.slicer import SlicerError, SlicerNotFoundError, slice_file

        result = slice_file(input_path, profile=profile)

        if printer_name:
            adapter = _registry.get(printer_name)
        else:
            adapter = _get_adapter()

        upload = adapter.upload_file(result.output_path)
        file_name = upload.file_name or os.path.basename(result.output_path)
        print_result = adapter.start_print(file_name)

        return {
            "success": True,
            "slice": result.to_dict(),
            "upload": upload.to_dict(),
            "print": print_result.to_dict(),
            "message": f"Sliced, uploaded, and started printing {os.path.basename(input_path)}.",
        }
    except SlicerNotFoundError as exc:
        return _error_dict(str(exc), code="SLICER_NOT_FOUND")
    except SlicerError as exc:
        return _error_dict(str(exc), code="SLICER_ERROR")
    except PrinterNotFoundError:
        return _error_dict(f"Printer {printer_name!r} not found.", code="NOT_FOUND")
    except (PrinterError, RuntimeError, FileNotFoundError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in slice_and_print")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Webcam snapshot tool
# ---------------------------------------------------------------------------


@mcp.tool()
def printer_snapshot(
    printer_name: str | None = None,
    save_path: str | None = None,
) -> dict:
    """Capture a webcam snapshot from the printer.

    Args:
        printer_name: Target printer name.  Omit for the default printer.
        save_path: Optional path to save the image file.  If omitted, the
            image is returned as a base64-encoded string.

    Supports OctoPrint and Moonraker webcams.  Bambu printers do not
    expose a webcam API over LAN.
    """
    try:
        if printer_name:
            adapter = _registry.get(printer_name)
        else:
            adapter = _get_adapter()

        image_data = adapter.get_snapshot()
        if image_data is None:
            return _error_dict(
                "Webcam not available or not supported by this printer backend.",
                code="NO_WEBCAM",
            )

        result: Dict[str, Any] = {
            "success": True,
            "size_bytes": len(image_data),
        }

        if save_path:
            os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
            with open(save_path, "wb") as f:
                f.write(image_data)
            result["saved_to"] = save_path
            result["message"] = f"Snapshot saved to {save_path}"
        else:
            import base64
            result["image_base64"] = base64.b64encode(image_data).decode("ascii")
            result["message"] = "Snapshot captured (base64 encoded)"

        return result

    except PrinterNotFoundError:
        return _error_dict(f"Printer {printer_name!r} not found.", code="NOT_FOUND")
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in printer_snapshot")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Cost estimation tools
# ---------------------------------------------------------------------------


@mcp.tool()
def estimate_cost(
    file_path: str,
    material: str = "PLA",
    electricity_rate: float = 0.12,
    printer_wattage: float = 200.0,
) -> dict:
    """Estimate the cost of a print job from a G-code file.

    Analyses G-code extrusion commands to calculate filament usage,
    material weight, filament cost, electricity cost, and total.

    Args:
        file_path: Path to the G-code file.
        material: Filament material (PLA, PETG, ABS, TPU, ASA, NYLON, PC).
        electricity_rate: Cost per kWh in USD (default 0.12).
        printer_wattage: Printer power consumption in watts (default 200).
    """
    try:
        estimate = _cost_estimator.estimate_from_file(
            file_path, material=material,
            electricity_rate=electricity_rate,
            printer_wattage=printer_wattage,
        )
        return {"success": True, "estimate": estimate.to_dict()}
    except FileNotFoundError as exc:
        return _error_dict(str(exc), code="FILE_NOT_FOUND")
    except Exception as exc:
        logger.exception("Unexpected error in estimate_cost")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def list_materials() -> dict:
    """List available filament material profiles.

    Returns built-in profiles for common materials including density,
    cost per kg, and recommended temperatures.
    """
    materials = _cost_estimator.materials
    return {
        "success": True,
        "materials": [m.to_dict() for m in materials.values()],
    }


# ---------------------------------------------------------------------------
# Material tracking tools
# ---------------------------------------------------------------------------


@mcp.tool()
def set_material(
    printer_name: str,
    material: str,
    color: str | None = None,
    spool_id: str | None = None,
    tool_index: int = 0,
) -> dict:
    """Record which filament material is loaded in a printer.

    Args:
        printer_name: Target printer name.
        material: Material type (PLA, PETG, ABS, etc.).
        color: Optional filament color.
        spool_id: Optional ID of a tracked spool.
        tool_index: Extruder index for multi-tool printers (default 0).
    """
    try:
        mat = _material_tracker.set_material(
            printer_name=printer_name,
            material_type=material,
            color=color,
            spool_id=spool_id,
            tool_index=tool_index,
        )
        return {"success": True, "material": mat.to_dict()}
    except Exception as exc:
        logger.exception("Unexpected error in set_material")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def get_material(printer_name: str | None = None) -> dict:
    """Get the material loaded in a printer.

    Args:
        printer_name: Target printer.  Omit for the default printer.
    """
    try:
        name = printer_name or "default"
        materials = _material_tracker.get_all_materials(name)
        return {
            "success": True,
            "materials": [m.to_dict() for m in materials],
        }
    except Exception as exc:
        logger.exception("Unexpected error in get_material")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def check_material_match(
    expected_material: str,
    printer_name: str | None = None,
) -> dict:
    """Check if the loaded material matches what a print expects.

    Args:
        expected_material: The material the print file requires.
        printer_name: Target printer.  Omit for the default printer.
    """
    try:
        name = printer_name or "default"
        warning = _material_tracker.check_match(name, expected_material)
        if warning:
            return {
                "success": True,
                "match": False,
                "warning": warning.to_dict(),
            }
        return {"success": True, "match": True}
    except Exception as exc:
        logger.exception("Unexpected error in check_material_match")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def list_spools() -> dict:
    """List all tracked filament spools in inventory."""
    try:
        spools = _material_tracker.list_spools()
        return {
            "success": True,
            "spools": [s.to_dict() for s in spools],
        }
    except Exception as exc:
        logger.exception("Unexpected error in list_spools")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def add_spool(
    material: str,
    color: str | None = None,
    brand: str | None = None,
    weight_grams: float = 1000.0,
    cost_usd: float | None = None,
) -> dict:
    """Add a new filament spool to inventory.

    Args:
        material: Material type (PLA, PETG, ABS, etc.).
        color: Filament color.
        brand: Manufacturer brand.
        weight_grams: Total spool weight in grams (default 1000).
        cost_usd: Cost of the spool in USD.
    """
    try:
        spool = _material_tracker.add_spool(
            material_type=material, color=color, brand=brand,
            weight_grams=weight_grams, cost_usd=cost_usd,
        )
        return {"success": True, "spool": spool.to_dict()}
    except Exception as exc:
        logger.exception("Unexpected error in add_spool")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def remove_spool(spool_id: str) -> dict:
    """Remove a filament spool from inventory.

    Args:
        spool_id: The spool's unique identifier.
    """
    try:
        removed = _material_tracker.remove_spool(spool_id)
        if removed:
            return {"success": True, "message": f"Spool {spool_id} removed."}
        return _error_dict(f"Spool {spool_id!r} not found.", code="NOT_FOUND")
    except Exception as exc:
        logger.exception("Unexpected error in remove_spool")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Bed leveling tools
# ---------------------------------------------------------------------------


@mcp.tool()
def bed_level_status(printer_name: str | None = None) -> dict:
    """Check bed leveling status and whether leveling is needed.

    Args:
        printer_name: Target printer.  Omit for the default printer.
    """
    try:
        name = printer_name or "default"
        status = _bed_level_mgr.check_needed(name)
        return {"success": True, "status": status.to_dict()}
    except Exception as exc:
        logger.exception("Unexpected error in bed_level_status")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def trigger_bed_level(printer_name: str | None = None) -> dict:
    """Trigger a bed leveling / mesh probe on the printer.

    Sends the configured G-code command (G29 or BED_MESH_CALIBRATE)
    to the printer.

    Args:
        printer_name: Target printer.  Omit for the default printer.
    """
    try:
        if printer_name:
            adapter = _registry.get(printer_name)
            name = printer_name
        else:
            adapter = _get_adapter()
            name = "default"

        result = _bed_level_mgr.trigger_level(name, adapter, triggered_by="manual")
        return {"success": result["success"], **result}
    except PrinterNotFoundError:
        return _error_dict(f"Printer {printer_name!r} not found.", code="NOT_FOUND")
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in trigger_bed_level")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def set_leveling_policy(
    enabled: bool = True,
    max_prints: int = 10,
    max_hours: float = 48.0,
    gcode_command: str = "G29",
    printer_name: str | None = None,
) -> dict:
    """Configure automatic bed leveling policy for a printer.

    Args:
        enabled: Enable/disable auto-leveling checks.
        max_prints: Trigger leveling after this many prints.
        max_hours: Trigger leveling after this many hours.
        gcode_command: G-code command to send (G29 or BED_MESH_CALIBRATE).
        printer_name: Target printer.  Omit for the default printer.
    """
    try:
        name = printer_name or "default"
        policy = LevelingPolicy(
            enabled=enabled,
            max_prints_between_levels=max_prints,
            max_hours_between_levels=max_hours,
            gcode_command=gcode_command,
        )
        _bed_level_mgr.set_policy(name, policy)
        return {"success": True, "policy": policy.to_dict()}
    except Exception as exc:
        logger.exception("Unexpected error in set_leveling_policy")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Webcam streaming tools
# ---------------------------------------------------------------------------


@mcp.tool()
def webcam_stream(
    printer_name: str | None = None,
    action: str = "status",
    port: int = 8081,
) -> dict:
    """Control the MJPEG webcam streaming proxy.

    Args:
        printer_name: Target printer.  Omit for the default printer.
        action: One of ``"start"``, ``"stop"``, or ``"status"``.
        port: Local port for the stream server (default 8081).
    """
    try:
        if action == "status":
            return {"success": True, "stream": _stream_proxy.status().to_dict()}

        if action == "stop":
            info = _stream_proxy.stop()
            return {"success": True, "stream": info.to_dict()}

        if action == "start":
            if printer_name:
                adapter = _registry.get(printer_name)
            else:
                adapter = _get_adapter()

            stream_url = adapter.get_stream_url()
            if stream_url is None:
                return _error_dict(
                    "Webcam streaming not available for this printer.",
                    code="NO_STREAM",
                )

            info = _stream_proxy.start(
                source_url=stream_url,
                port=port,
                printer_name=printer_name or "default",
            )
            return {"success": True, "stream": info.to_dict()}

        return _error_dict(
            f"Unknown action {action!r}. Use 'start', 'stop', or 'status'.",
            code="BAD_REQUEST",
        )
    except PrinterNotFoundError:
        return _error_dict(f"Printer {printer_name!r} not found.", code="NOT_FOUND")
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in webcam_stream")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Cloud sync tools
# ---------------------------------------------------------------------------


@mcp.tool()
def cloud_sync_status() -> dict:
    """Get the current cloud sync status."""
    global _cloud_sync
    if _cloud_sync is None:
        return {"success": True, "status": {"enabled": False, "last_sync_status": "not_configured"}}
    return {"success": True, "status": _cloud_sync.status().to_dict()}


@mcp.tool()
def cloud_sync_now() -> dict:
    """Trigger an immediate cloud sync cycle."""
    global _cloud_sync
    if _cloud_sync is None:
        return _error_dict("Cloud sync not configured.", code="NOT_CONFIGURED")
    try:
        result = _cloud_sync.sync_now()
        return {"success": True, **result}
    except Exception as exc:
        logger.exception("Unexpected error in cloud_sync_now")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def cloud_sync_configure(
    cloud_url: str,
    api_key: str,
    interval: float = 60.0,
) -> dict:
    """Configure and start cloud sync.

    Args:
        cloud_url: Base URL of the cloud sync endpoint.
        api_key: API key for authentication.
        interval: Sync interval in seconds (default 60).
    """
    global _cloud_sync
    try:
        config = SyncConfig(
            cloud_url=cloud_url, api_key=api_key,
            sync_interval_seconds=interval,
        )
        if _cloud_sync is not None:
            _cloud_sync.stop()
        _cloud_sync = CloudSyncManager(
            db=get_db(), event_bus=_event_bus, config=config,
        )
        _cloud_sync.start()
        return {"success": True, "config": config.to_dict()}
    except Exception as exc:
        logger.exception("Unexpected error in cloud_sync_configure")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Plugin tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_plugins() -> dict:
    """List all discovered plugins and their status."""
    plugins = _plugin_mgr.list_plugins()
    return {
        "success": True,
        "plugins": [p.to_dict() for p in plugins],
    }


@mcp.tool()
def plugin_info(name: str) -> dict:
    """Get detailed information about a specific plugin.

    Args:
        name: Plugin name.
    """
    info = _plugin_mgr.get_plugin_info(name)
    if info is None:
        return _error_dict(f"Plugin {name!r} not found.", code="NOT_FOUND")
    return {"success": True, "plugin": info.to_dict()}


# ---------------------------------------------------------------------------
# Fulfillment tools — outsource prints to external services
# ---------------------------------------------------------------------------


@mcp.tool()
def fulfillment_materials() -> dict:
    """List available materials from external manufacturing services.

    Returns materials with technology (FDM, SLA, SLS, etc.), color,
    finish, and pricing.  Use the material ``id`` when requesting a quote
    with ``fulfillment_quote``.

    Requires ``KILN_CRAFTCLOUD_API_KEY`` to be set.
    """
    try:
        provider = _get_fulfillment()
        materials = provider.list_materials()
        return {
            "success": True,
            "provider": provider.name,
            "materials": [m.to_dict() for m in materials],
            "count": len(materials),
        }
    except (FulfillmentError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in fulfillment_materials")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def fulfillment_quote(
    file_path: str,
    material_id: str,
    quantity: int = 1,
    shipping_country: str = "US",
) -> dict:
    """Get a manufacturing quote for a 3D model from Craftcloud.

    Args:
        file_path: Absolute path to the model file (STL, 3MF, OBJ).
        material_id: Material ID from ``fulfillment_materials``.
        quantity: Number of copies to print (default 1).
        shipping_country: ISO country code for shipping (default "US").

    Uploads the model, returns pricing from Craftcloud's network of 150+
    print services, including unit price, total, lead time, and shipping
    options.  A Kiln platform fee is shown separately so the user sees
    the full cost before committing.

    If a payment method is linked, a hold is placed on the fee amount
    at quote time (Stripe auth-and-capture).  The hold is captured
    when the order is placed via ``fulfillment_order``, or released
    if the user doesn't proceed.

    Use the returned ``quote_id`` with ``fulfillment_order`` to place the
    order.
    """
    try:
        provider = _get_fulfillment()
        quote = provider.get_quote(QuoteRequest(
            file_path=file_path,
            material_id=material_id,
            quantity=quantity,
            shipping_country=shipping_country,
        ))
        fee_calc = _billing.calculate_fee(
            quote.total_price, currency=quote.currency,
        )
        quote_data = quote.to_dict()
        quote_data["kiln_fee"] = fee_calc.to_dict()
        quote_data["total_with_fee"] = fee_calc.total_cost

        # Try to authorize (hold) the fee at quote time.
        try:
            mgr = _get_payment_mgr()
            if mgr.available_rails:
                auth_result = mgr.authorize_fee(
                    quote.quote_id, fee_calc,
                )
                if auth_result.payment_id:
                    quote_data["payment_hold"] = {
                        "payment_id": auth_result.payment_id,
                        "status": auth_result.status.value,
                    }
        except (PaymentError, Exception):
            # Hold failed — fee will be collected at order time.
            pass

        return {
            "success": True,
            "quote": quote_data,
        }
    except FileNotFoundError as exc:
        return _error_dict(str(exc), code="FILE_NOT_FOUND")
    except (FulfillmentError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in fulfillment_quote")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def fulfillment_order(
    quote_id: str,
    shipping_option_id: str = "",
    payment_hold_id: str = "",
) -> dict:
    """Place a manufacturing order based on a previous quote.

    Args:
        quote_id: Quote ID from ``fulfillment_quote``.
        shipping_option_id: Shipping option ID from the quote's
            ``shipping_options`` list.
        payment_hold_id: PaymentIntent ID from the quote's
            ``payment_hold`` field (optional).  If provided, captures
            the previously authorized hold instead of creating a new
            charge.

    Places the order, then captures the fee hold (or creates a new
    charge if no hold exists).  If the order fails after the hold is
    captured, a refund is issued automatically.
    Use ``fulfillment_order_status`` to track.
    """
    if err := _check_auth("print"):
        return err
    try:
        provider = _get_fulfillment()
        result = provider.place_order(OrderRequest(
            quote_id=quote_id,
            shipping_option_id=shipping_option_id,
        ))
        order_data = result.to_dict()

        # Calculate and collect the platform fee.
        if result.total_price and result.total_price > 0:
            fee_calc = _billing.calculate_fee(
                result.total_price, currency=result.currency,
            )
            order_data["kiln_fee"] = fee_calc.to_dict()
            order_data["total_with_fee"] = fee_calc.total_cost

            try:
                mgr = _get_payment_mgr()
                if mgr.available_rails:
                    if payment_hold_id:
                        # Capture the hold placed at quote time.
                        pay_result = mgr.capture_fee(
                            payment_hold_id, result.order_id, fee_calc,
                        )
                    else:
                        # No hold — one-shot charge.
                        pay_result = mgr.charge_fee(result.order_id, fee_calc)
                    order_data["payment"] = pay_result.to_dict()
                else:
                    _billing.record_charge(result.order_id, fee_calc)
                    order_data["payment"] = {"status": "no_payment_method"}
            except PaymentError as pe:
                _billing.record_charge(
                    result.order_id, fee_calc,
                    payment_status="failed",
                )
                order_data["payment"] = {
                    "status": "failed",
                    "error": str(pe),
                }
        return {
            "success": True,
            "order": order_data,
        }
    except (FulfillmentError, RuntimeError) as exc:
        # If we had a hold and the order failed, release it.
        if payment_hold_id:
            try:
                mgr = _get_payment_mgr()
                mgr.cancel_fee(payment_hold_id)
            except Exception:
                logger.debug("Could not cancel hold %s", payment_hold_id)
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in fulfillment_order")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def fulfillment_order_status(order_id: str) -> dict:
    """Check the status of a fulfillment order.

    Args:
        order_id: Order ID from ``fulfillment_order``.

    Returns current order state, tracking info, and estimated delivery.
    """
    try:
        provider = _get_fulfillment()
        result = provider.get_order_status(order_id)
        return {
            "success": True,
            "order": result.to_dict(),
        }
    except (FulfillmentError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in fulfillment_order_status")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def fulfillment_cancel(order_id: str) -> dict:
    """Cancel a fulfillment order (if still cancellable).

    Args:
        order_id: Order ID to cancel.

    Only orders that have not yet shipped can be cancelled.
    """
    if err := _check_auth("print"):
        return err
    try:
        provider = _get_fulfillment()
        result = provider.cancel_order(order_id)
        return {
            "success": True,
            "order": result.to_dict(),
        }
    except (FulfillmentError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in fulfillment_cancel")
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


@mcp.tool()
def await_print_completion(
    job_id: str | None = None,
    timeout: int = 7200,
    poll_interval: int = 15,
) -> dict:
    """Wait for the current print to finish and return the final status.

    Polls the printer (or a specific queued job) until it reaches a
    terminal state: completed, failed, cancelled, or the timeout is
    exceeded.  This lets agents fire-and-forget a print and pick up the
    result later without managing their own polling loop.

    Args:
        job_id: Optional job ID from ``submit_job()``.  When provided,
            tracks that specific job through the queue/scheduler.  When
            omitted, monitors the printer directly for idle/error state.
        timeout: Maximum seconds to wait (default 7200 = 2 hours).
        poll_interval: Seconds between status checks (default 15).

    Returns a dict with ``outcome`` (completed / failed / cancelled /
    timeout), final printer state, elapsed time, and completion
    percentage history.
    """
    start = time.time()
    progress_log: list[dict] = []
    last_pct: float | None = None

    while True:
        elapsed = time.time() - start
        if elapsed >= timeout:
            return {
                "success": True,
                "outcome": "timeout",
                "elapsed_seconds": round(elapsed, 1),
                "message": f"Timed out after {timeout}s waiting for print to finish.",
                "progress_log": progress_log[-20:],
            }

        try:
            # --- Job-based tracking (via queue) ---
            if job_id is not None:
                try:
                    job = _queue.get_job(job_id)
                except JobNotFoundError:
                    return _error_dict(
                        f"Job {job_id!r} not found.", code="JOB_NOT_FOUND"
                    )

                if job.status == JobStatus.COMPLETED:
                    return {
                        "success": True,
                        "outcome": "completed",
                        "job": job.to_dict(),
                        "elapsed_seconds": round(elapsed, 1),
                        "progress_log": progress_log[-20:],
                    }
                if job.status == JobStatus.FAILED:
                    return {
                        "success": True,
                        "outcome": "failed",
                        "job": job.to_dict(),
                        "error": job.error,
                        "elapsed_seconds": round(elapsed, 1),
                        "progress_log": progress_log[-20:],
                    }
                if job.status == JobStatus.CANCELLED:
                    return {
                        "success": True,
                        "outcome": "cancelled",
                        "job": job.to_dict(),
                        "elapsed_seconds": round(elapsed, 1),
                        "progress_log": progress_log[-20:],
                    }

                # Still running — log progress
                time.sleep(poll_interval)
                continue

            # --- Direct printer tracking (no job_id) ---
            adapter = _get_adapter()
            state = adapter.get_state()
            job_progress = adapter.get_job()

            pct = job_progress.completion
            if pct is not None and pct != last_pct:
                progress_log.append({
                    "time": round(elapsed, 1),
                    "completion": pct,
                })
                last_pct = pct

            if state.state == PrinterStatus.IDLE:
                return {
                    "success": True,
                    "outcome": "completed",
                    "state": state.state.value,
                    "elapsed_seconds": round(elapsed, 1),
                    "progress_log": progress_log[-20:],
                }
            if state.state == PrinterStatus.ERROR:
                return {
                    "success": True,
                    "outcome": "failed",
                    "state": state.state.value,
                    "elapsed_seconds": round(elapsed, 1),
                    "progress_log": progress_log[-20:],
                }
            if state.state == PrinterStatus.OFFLINE:
                return {
                    "success": True,
                    "outcome": "failed",
                    "state": state.state.value,
                    "error": "Printer went offline during print.",
                    "elapsed_seconds": round(elapsed, 1),
                    "progress_log": progress_log[-20:],
                }

        except (PrinterError, RuntimeError) as exc:
            return _error_dict(str(exc))
        except Exception as exc:
            logger.exception("Unexpected error in await_print_completion")
            return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")

        time.sleep(poll_interval)


@mcp.tool()
def compare_print_options(
    file_path: str,
    material: str = "PLA",
    fulfillment_material_id: str | None = None,
    quantity: int = 1,
    electricity_rate: float = 0.12,
    printer_wattage: float = 200.0,
    shipping_country: str = "US",
) -> dict:
    """Compare local printing cost vs. outsourced manufacturing.

    Runs a local cost estimate and (if Craftcloud is configured) fetches
    a fulfillment quote, then returns a side-by-side comparison to help
    agents recommend the best option.

    Args:
        file_path: Path to the G-code file (for local) or model file
            (STL/3MF for fulfillment).  If a G-code file is provided,
            only local estimate is returned.
        material: Filament material for local estimate (PLA, PETG, etc.).
        fulfillment_material_id: Material ID from ``fulfillment_materials``
            for the outsourced quote.  If omitted, the fulfillment quote
            is skipped.
        quantity: Number of copies for fulfillment (default 1).
        electricity_rate: Cost per kWh in USD (default 0.12).
        printer_wattage: Printer power consumption in watts (default 200).
        shipping_country: ISO country code for fulfillment shipping.
    """
    result: Dict[str, Any] = {"success": True}

    # --- Local estimate ---------------------------------------------------
    local_estimate = None
    local_error = None
    try:
        estimate = _cost_estimator.estimate_from_file(
            file_path,
            material=material,
            electricity_rate=electricity_rate,
            printer_wattage=printer_wattage,
        )
        local_estimate = estimate.to_dict()
    except FileNotFoundError:
        local_error = "G-code file not found"
    except Exception as exc:
        local_error = str(exc)

    result["local"] = {
        "available": local_estimate is not None,
        "estimate": local_estimate,
        "error": local_error,
    }

    # --- Fulfillment quote ------------------------------------------------
    fulfillment_quote_data = None
    fulfillment_error = None
    if fulfillment_material_id:
        try:
            provider = _get_fulfillment()
            quote = provider.get_quote(QuoteRequest(
                file_path=file_path,
                material_id=fulfillment_material_id,
                quantity=quantity,
                shipping_country=shipping_country,
            ))
            fee_calc = _billing.calculate_fee(
                quote.total_price, currency=quote.currency,
            )
            fulfillment_quote_data = quote.to_dict()
            fulfillment_quote_data["kiln_fee"] = fee_calc.to_dict()
            fulfillment_quote_data["total_with_fee"] = fee_calc.total_cost
        except (FulfillmentError, RuntimeError) as exc:
            fulfillment_error = str(exc)
        except Exception as exc:
            fulfillment_error = str(exc)

    result["fulfillment"] = {
        "available": fulfillment_quote_data is not None,
        "quote": fulfillment_quote_data,
        "error": fulfillment_error,
    }

    # --- Comparison summary -----------------------------------------------
    if local_estimate and fulfillment_quote_data:
        local_cost = local_estimate.get("total_cost_usd", 0)
        fulfillment_cost = fulfillment_quote_data.get("total_with_fee", fulfillment_quote_data.get("total_price", 0))
        cheapest_shipping = None
        if fulfillment_quote_data.get("shipping_options"):
            cheapest_shipping = min(
                fulfillment_quote_data["shipping_options"],
                key=lambda s: s.get("price", float("inf")),
            )
        fulfillment_total = fulfillment_cost + (cheapest_shipping.get("price", 0) if cheapest_shipping else 0)

        local_time_h = None
        if local_estimate.get("estimated_time_seconds"):
            local_time_h = round(local_estimate["estimated_time_seconds"] / 3600, 1)

        fulfillment_days = fulfillment_quote_data.get("lead_time_days")
        if cheapest_shipping and cheapest_shipping.get("estimated_days"):
            fulfillment_days = (fulfillment_days or 0) + cheapest_shipping["estimated_days"]

        result["comparison"] = {
            "local_cost_usd": round(local_cost, 2),
            "fulfillment_cost_usd": round(fulfillment_total, 2),
            "savings_usd": round(fulfillment_total - local_cost, 2),
            "cheaper": "local" if local_cost < fulfillment_total else "fulfillment",
            "local_time_hours": local_time_h,
            "fulfillment_time_days": fulfillment_days,
            "recommendation": (
                "Local printing is cheaper and faster."
                if local_cost < fulfillment_total
                else "Outsourced manufacturing may offer better quality or materials."
            ),
        }

    return result


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

    # Auto-register marketplace adapters from env credentials
    _init_marketplace_registry()
    if _marketplace_registry.count > 0:
        logger.info(
            "Marketplace sources: %s", ", ".join(_marketplace_registry.connected)
        )

    # Subscribe bed level manager to job events
    _bed_level_mgr.subscribe_events()

    # Discover and activate plugins
    _plugin_mgr.discover()
    _plugin_mgr.activate_all(PluginContext(
        event_bus=_event_bus,
        registry=_registry,
        queue=_queue,
        mcp=mcp,
        db=get_db(),
    ))

    # Initialise cloud sync from saved config
    global _cloud_sync
    _saved_sync = get_db().get_setting("cloud_sync_config")
    if _saved_sync:
        import json as _json
        try:
            _cloud_sync = CloudSyncManager(
                db=get_db(), event_bus=_event_bus,
                config=SyncConfig.from_dict(_json.loads(_saved_sync)),
            )
            _cloud_sync.start()
        except Exception:
            logger.debug("Could not restore cloud sync config", exc_info=True)

    # Start background services
    _scheduler.start()
    _webhook_mgr.start()
    logger.info("Kiln scheduler and webhook delivery started")

    # Graceful shutdown on exit
    atexit.register(_scheduler.stop)
    atexit.register(_webhook_mgr.stop)
    atexit.register(_stream_proxy.stop)
    if _cloud_sync is not None:
        atexit.register(_cloud_sync.stop)

    mcp.run()


if __name__ == "__main__":
    main()
