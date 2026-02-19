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
    ``"moonraker"``, ``"bambu"``, ``"prusaconnect"``, and ``"serial"``.
    Defaults to ``"octoprint"``.
``KILN_PRINTER_PORT``
    Serial port path for USB printers (required when ``KILN_PRINTER_TYPE``
    is ``"serial"``).  E.g. ``"/dev/ttyUSB0"`` or ``"COM3"``.
``KILN_PRINTER_BAUDRATE``
    Baud rate for serial printers (default 115200).
``KILN_PRINTER_SERIAL``
    Bambu printer serial number (required when ``KILN_PRINTER_TYPE``
    is ``"bambu"``).
``KILN_THINGIVERSE_TOKEN``
    Thingiverse API app token for model search and download.
``KILN_AUTO_PRINT_MARKETPLACE``
    Set to ``"true"`` to auto-start printing after downloading and
    uploading marketplace models.  Default: ``"false"`` (upload only,
    require explicit ``start_print``).
``KILN_AUTO_PRINT_GENERATED``
    Set to ``"true"`` to auto-start printing AI-generated models after
    generation, validation, slicing, and upload.  Default: ``"false"``
    (upload only, require explicit ``start_print``).  **Higher risk than
    marketplace auto-print** — generated geometry is experimental.
"""

from __future__ import annotations

import atexit
import logging
import os
import re
import secrets
import shutil
import signal
import sys
import tempfile
import threading
import time
import uuid as _uuid_mod
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from kiln import parse_float_env, parse_int_env
from kiln.auth import AuthManager
from kiln.backup import BackupError
from kiln.backup import backup_database as _backup_db
from kiln.bed_leveling import BedLevelManager, LevelingPolicy
from kiln.billing import BillingLedger
from kiln.billing_alerts import BillingAlertManager
from kiln.cli.config import _validate_printer_url
from kiln.cloud_sync import CloudSyncManager, SyncConfig
from kiln.cost_estimator import CostEstimator
from kiln.events import Event, EventBus, EventType
from kiln.fulfillment import (
    FulfillmentError,
    FulfillmentProvider,
    OrderRequest,
    QuoteRequest,
)
from kiln.fulfillment import (
    get_provider as get_fulfillment_provider,
)
from kiln.fulfillment.intelligence import (
    QuoteValidation,
    _check_price_drift,
)
from kiln.fulfillment.intelligence import (
    validate_quote_for_order as _validate_quote_for_order,
)
from kiln.gateway.threedos import ThreeDOSClient
from kiln.gcode import validate_gcode as _validate_gcode_impl
from kiln.gcode import validate_gcode_for_printer
from kiln.generation import (
    GenerationAuthError,
    GenerationError,
    GenerationProvider,
    GenerationResult,
    GenerationStatus,
    MeshyProvider,
    OpenSCADProvider,
    convert_to_stl,
    validate_mesh,
)
from kiln.heater_watchdog import HeaterWatchdog
from kiln.licensing import (
    FREE_TIER_MAX_PRINTERS,
    LicenseTier,
    check_tier,
    get_tier,
    requires_tier,
)
from kiln.log_config import configure_logging as _configure_log_rotation
from kiln.marketplaces import (
    Cults3DAdapter,
    MarketplaceError,
    MarketplaceRegistry,
    MyMiniFactoryAdapter,
    ThingiverseAdapter,
)
from kiln.marketplaces import (
    MarketplaceNotFoundError as MktNotFoundError,
)
from kiln.materials import MaterialTracker
from kiln.payments.base import PaymentError
from kiln.payments.manager import PaymentManager
from kiln.persistence import get_db
from kiln.pipelines import (
    PipelineState as _PipelineState,
)
from kiln.pipelines import (
    benchmark as _pipeline_benchmark,
)
from kiln.pipelines import (
    calibrate as _pipeline_calibrate,
)
from kiln.pipelines import (
    get_execution as _get_execution,
)
from kiln.pipelines import (
    list_pipelines as _list_pipelines,
)
from kiln.pipelines import (
    quick_print as _pipeline_quick_print,
)
from kiln.plugin_loader import register_all_plugins
from kiln.plugins import PluginContext, PluginManager
from kiln.printer_intelligence import (
    diagnose_issue,
    get_material_settings,
    get_printer_intel,
    intel_to_dict,
)
from kiln.printers import (
    BambuAdapter,
    ElegooAdapter,
    MoonrakerAdapter,
    OctoPrintAdapter,
    PrinterAdapter,
    PrinterError,
    PrinterStatus,
    PrusaConnectAdapter,
    SerialPrinterAdapter,
)
from kiln.queue import JobNotFoundError, JobStatus, PrintQueue
from kiln.registry import PrinterNotFoundError, PrinterRegistry
from kiln.safety_profiles import (
    add_community_profile,
    get_profile,
    list_profiles,
    profile_to_dict,
    validate_safety_profile,
)
from kiln.safety_profiles import (
    export_profile as _export_profile,
)
from kiln.scheduler import JobScheduler
from kiln.slicer_profiles import (
    get_slicer_profile,
    list_slicer_profiles,
    resolve_slicer_profile,
    slicer_profile_to_dict,
    validate_profile_for_printer,
)
from kiln.streaming import MJPEGProxy
from kiln.thingiverse import (
    ThingiverseClient,
    ThingiverseError,
    ThingiverseNotFoundError,
)
from kiln.webhooks import WebhookManager


class _JsonLogFormatter(logging.Formatter):
    """Simple JSON-lines log formatter for structured log output.

    Produces one JSON object per log record with keys: timestamp, level,
    logger, message.  Activated when ``KILN_LOG_FORMAT=json``.
    """

    def format(self, record: logging.LogRecord) -> str:
        import json as _json_mod

        entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return _json_mod.dumps(entry)


def _configure_logging() -> None:
    """Configure root logger format based on ``KILN_LOG_FORMAT`` env var.

    Supported values:
        - ``"text"`` (default): standard human-readable log lines.
        - ``"json"``: structured JSON-lines output for log aggregators.
    """
    log_format = os.environ.get("KILN_LOG_FORMAT", "text").strip().lower()
    root = logging.getLogger()
    if log_format == "json":
        handler = logging.StreamHandler()
        handler.setFormatter(_JsonLogFormatter())
        root.handlers.clear()
        root.addHandler(handler)
        root.setLevel(logging.INFO)


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_PRINTER_HOST: str = os.environ.get("KILN_PRINTER_HOST", "")
_PRINTER_API_KEY: str = os.environ.get("KILN_PRINTER_API_KEY", "")
_PRINTER_TYPE: str = os.environ.get("KILN_PRINTER_TYPE", "octoprint")
_PRINTER_SERIAL: str = os.environ.get("KILN_PRINTER_SERIAL", "")
_PRINTER_MODEL: str = os.environ.get("KILN_PRINTER_MODEL", "")
_CONFIRM_UPLOAD: bool = os.environ.get("KILN_CONFIRM_UPLOAD", "").lower() in ("1", "true", "yes")
_CONFIRM_MODE: bool = os.environ.get("KILN_CONFIRM_MODE", "").lower() in ("1", "true", "yes")
_THINGIVERSE_TOKEN: str = os.environ.get("KILN_THINGIVERSE_TOKEN", "")
_THINGIVERSE_DEPRECATION_NOTICE: str = (
    "Thingiverse was acquired by MyMiniFactory in February 2026. "
    "The API may be sunset. Consider using MyMiniFactory "
    "(source: myminifactory) as the primary marketplace."
)
_MMF_API_KEY: str = os.environ.get("KILN_MMF_API_KEY", "")
_CULTS3D_USERNAME: str = os.environ.get("KILN_CULTS3D_USERNAME", "")
_CULTS3D_API_KEY: str = os.environ.get("KILN_CULTS3D_API_KEY", "")
_CRAFTCLOUD_API_KEY: str = os.environ.get("KILN_CRAFTCLOUD_API_KEY", "")
_FULFILLMENT_PROVIDER: str = os.environ.get("KILN_FULFILLMENT_PROVIDER", "")
_MESHY_API_KEY: str = os.environ.get("KILN_MESHY_API_KEY", "")

# Auto-print toggles: OFF by default for safety.  Generated models are
# higher risk than marketplace downloads — two independent toggles let
# users opt in to each separately.
_AUTO_PRINT_MARKETPLACE: bool = os.environ.get("KILN_AUTO_PRINT_MARKETPLACE", "").lower() in ("1", "true", "yes")
_AUTO_PRINT_GENERATED: bool = os.environ.get("KILN_AUTO_PRINT_GENERATED", "").lower() in ("1", "true", "yes")

# Heater watchdog: minutes of idle heater time before auto-cooldown (0=disabled).
_HEATER_TIMEOUT_MIN: float = parse_float_env("KILN_HEATER_TIMEOUT", 30.0)

# Default snapshot directory — use ~/.kiln/snapshots/ instead of /tmp to
# avoid macOS periodic /tmp cleanup deleting saved snapshots.
_DEFAULT_SNAPSHOT_DIR = os.path.join(os.path.expanduser("~"), ".kiln", "snapshots")


def _reload_env_config() -> None:
    """Re-read env-backed configuration globals after .env has been loaded.

    Module-level env reads happen at import time, which is before
    ``main()`` calls ``load_dotenv()``.  This function refreshes them
    so that settings from ``.env`` files are picked up correctly.
    """
    global _PRINTER_HOST, _PRINTER_API_KEY, _PRINTER_TYPE  # noqa: PLW0603
    global _PRINTER_SERIAL, _PRINTER_MODEL  # noqa: PLW0603
    global _CONFIRM_UPLOAD, _CONFIRM_MODE  # noqa: PLW0603
    global _THINGIVERSE_TOKEN, _MMF_API_KEY  # noqa: PLW0603
    global _CULTS3D_USERNAME, _CULTS3D_API_KEY, _CRAFTCLOUD_API_KEY  # noqa: PLW0603
    global _FULFILLMENT_PROVIDER, _MESHY_API_KEY  # noqa: PLW0603
    global _AUTO_PRINT_MARKETPLACE, _AUTO_PRINT_GENERATED  # noqa: PLW0603
    global _HEATER_TIMEOUT_MIN  # noqa: PLW0603

    _PRINTER_HOST = os.environ.get("KILN_PRINTER_HOST", "")
    _PRINTER_API_KEY = os.environ.get("KILN_PRINTER_API_KEY", "")
    _PRINTER_TYPE = os.environ.get("KILN_PRINTER_TYPE", "octoprint")
    _PRINTER_SERIAL = os.environ.get("KILN_PRINTER_SERIAL", "")
    _PRINTER_MODEL = os.environ.get("KILN_PRINTER_MODEL", "")
    _CONFIRM_UPLOAD = os.environ.get("KILN_CONFIRM_UPLOAD", "").lower() in ("1", "true", "yes")
    _CONFIRM_MODE = os.environ.get("KILN_CONFIRM_MODE", "").lower() in ("1", "true", "yes")
    _THINGIVERSE_TOKEN = os.environ.get("KILN_THINGIVERSE_TOKEN", "")
    _MMF_API_KEY = os.environ.get("KILN_MMF_API_KEY", "")
    _CULTS3D_USERNAME = os.environ.get("KILN_CULTS3D_USERNAME", "")
    _CULTS3D_API_KEY = os.environ.get("KILN_CULTS3D_API_KEY", "")
    _CRAFTCLOUD_API_KEY = os.environ.get("KILN_CRAFTCLOUD_API_KEY", "")
    _FULFILLMENT_PROVIDER = os.environ.get("KILN_FULFILLMENT_PROVIDER", "")
    _MESHY_API_KEY = os.environ.get("KILN_MESHY_API_KEY", "")
    _AUTO_PRINT_MARKETPLACE = os.environ.get("KILN_AUTO_PRINT_MARKETPLACE", "").lower() in ("1", "true", "yes")
    _AUTO_PRINT_GENERATED = os.environ.get("KILN_AUTO_PRINT_GENERATED", "").lower() in ("1", "true", "yes")
    _HEATER_TIMEOUT_MIN = parse_float_env("KILN_HEATER_TIMEOUT", 30.0)


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
        "lack the material or capacity.\n\n"
        "SAFETY: 3D printers are delicate hardware. Prefer downloading "
        "proven community models over generating new ones. AI-generated "
        "models are experimental and may damage hardware. By default, "
        "downloaded and generated models are uploaded but NOT auto-printed "
        "— you must call `start_print` separately. Users can opt in to "
        "auto-print via KILN_AUTO_PRINT_MARKETPLACE (for community models) "
        "or KILN_AUTO_PRINT_GENERATED (for AI models, higher risk). "
        "Use `safety_settings` to check current auto-print status."
    ),
)

# ---------------------------------------------------------------------------
# Printer adapter singleton
# ---------------------------------------------------------------------------

_adapter: PrinterAdapter | None = None


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
            "No printer configured. Set KILN_PRINTER_HOST environment variable "
            "to the printer URL (e.g. http://octopi.local). Also set "
            "KILN_PRINTER_API_KEY and optionally KILN_PRINTER_TYPE."
        )
    if printer_type == "octoprint":
        if not api_key:
            raise RuntimeError(
                "KILN_PRINTER_API_KEY environment variable is not set.  Set it to your printer server's API key."
            )
        _adapter = OctoPrintAdapter(host=host, api_key=api_key)
    elif printer_type == "moonraker":
        # Moonraker typically does not require an API key, but one can
        # optionally be provided via KILN_PRINTER_API_KEY.
        _adapter = MoonrakerAdapter(host=host, api_key=api_key or None)
    elif printer_type == "bambu":
        if BambuAdapter is None:
            raise RuntimeError(
                "Bambu support requires paho-mqtt.  Install it with: pip install 'kiln[bambu]' or pip install paho-mqtt"
            )
        if not api_key:
            raise RuntimeError(
                "KILN_PRINTER_API_KEY environment variable is not set.  Set it to your Bambu printer's LAN Access Code."
            )
        serial = _PRINTER_SERIAL
        if not serial:
            raise RuntimeError(
                "KILN_PRINTER_SERIAL environment variable is not set.  Set it to your Bambu printer's serial number."
            )
        _adapter = BambuAdapter(host=host, access_code=api_key, serial=serial)
    elif printer_type == "elegoo":
        if ElegooAdapter is None:
            raise RuntimeError(
                "Elegoo SDCP support requires websocket-client.  "
                "Install it with: pip install 'kiln[elegoo]' or pip install websocket-client"
            )
        mainboard_id = os.environ.get("KILN_PRINTER_MAINBOARD_ID", "")
        _adapter = ElegooAdapter(host=host, mainboard_id=mainboard_id)
    elif printer_type == "prusaconnect":
        _adapter = PrusaConnectAdapter(host=host, api_key=api_key or None)
    elif printer_type == "serial":
        port = os.environ.get("KILN_PRINTER_PORT", "")
        if not port:
            raise RuntimeError(
                "KILN_PRINTER_PORT environment variable is not set.  "
                "Set it to the serial port path (e.g. /dev/ttyUSB0, /dev/ttyACM0, COM3)."
            )
        baudrate = parse_int_env("KILN_PRINTER_BAUDRATE", 115200)
        _adapter = SerialPrinterAdapter(port=port, baudrate=baudrate)
    else:
        raise RuntimeError(
            f"Unsupported printer type: {printer_type!r}.  "
            f"Supported types are 'octoprint', 'moonraker', 'bambu', 'elegoo', 'prusaconnect', and 'serial'."
        )

    # Propagate safety profile to adapter for defense-in-depth temp limits.
    if _PRINTER_MODEL:
        _adapter.set_safety_profile(_PRINTER_MODEL)

    logger.info(
        "Initialised %s adapter for %s",
        _adapter.name,
        host,
    )
    return _adapter


# ---------------------------------------------------------------------------
# Per-printer temperature limits
# ---------------------------------------------------------------------------


def _get_temp_limits() -> tuple:
    """Return ``(max_tool, max_bed)`` from the printer's safety profile.

    When ``KILN_PRINTER_MODEL`` is set, loads the matching profile from the
    bundled database.  Falls back to conservative generic limits (300/130).
    """
    if _PRINTER_MODEL:
        try:
            from kiln.safety_profiles import get_profile  # noqa: E402

            profile = get_profile(_PRINTER_MODEL)
            return profile.max_hotend_temp, profile.max_bed_temp
        except (KeyError, ImportError):
            pass
    return 300.0, 130.0


# ---------------------------------------------------------------------------
# MCP tool rate limiter
# ---------------------------------------------------------------------------


class _ToolRateLimiter:
    """Per-tool rate limiter for MCP tool calls.

    Prevents agents from spamming physically-dangerous commands in tight
    retry loops.  Uses a simple minimum-interval + max-per-minute model.

    **Circuit breaker:** When the same tool is blocked 3+ times within 60
    seconds, the tool enters a 5-minute emergency cooldown.  This catches
    runaway agents that repeatedly retry forbidden operations.
    """

    # Circuit breaker thresholds
    _BLOCK_THRESHOLD: int = 3  # blocks within the window to trigger
    _BLOCK_WINDOW: float = 60.0  # seconds
    _COOLDOWN_DURATION: float = 300.0  # 5 minutes

    def __init__(self) -> None:
        self._last_call: dict[str, float] = {}
        self._call_history: dict[str, list[float]] = {}
        self._block_history: dict[str, list[float]] = {}
        self._cooldown_until: dict[str, float] = {}

    def record_block(self, tool_name: str) -> str | None:
        """Record a blocked attempt for the circuit breaker.

        Returns an escalation message if the threshold is hit, else ``None``.
        """
        now = time.monotonic()
        history = self._block_history.get(tool_name, [])
        cutoff = now - self._BLOCK_WINDOW
        history = [t for t in history if t > cutoff]
        history.append(now)
        self._block_history[tool_name] = history

        if len(history) >= self._BLOCK_THRESHOLD:
            self._cooldown_until[tool_name] = now + self._COOLDOWN_DURATION
            self._block_history[tool_name] = []  # Reset after escalation
            return (
                f"SAFETY ESCALATED: {tool_name} has been blocked "
                f"{len(history)} times in {self._BLOCK_WINDOW:.0f}s. "
                f"Tool is suspended for {self._COOLDOWN_DURATION / 60:.0f} "
                f"minutes. Please review your approach."
            )
        return None

    def check(self, tool_name: str, min_interval_ms: int = 0, max_per_minute: int = 0) -> str | None:
        """Return ``None`` if allowed, or an error message if rate-limited."""
        now = time.monotonic()

        # Check circuit breaker cooldown first.
        cooldown_end = self._cooldown_until.get(tool_name, 0.0)
        if now < cooldown_end:
            remaining = cooldown_end - now
            return (
                f"Tool {tool_name} is in emergency cooldown due to repeated "
                f"blocked attempts. Cooldown expires in {remaining:.0f}s."
            )

        # Minimum interval between consecutive calls.
        if min_interval_ms > 0:
            last = self._last_call.get(tool_name, 0.0)
            elapsed_ms = (now - last) * 1000
            if elapsed_ms < min_interval_ms:
                wait = (min_interval_ms - elapsed_ms) / 1000
                return f"Rate limited: {tool_name} called too rapidly. Wait {wait:.1f}s before retrying."

        # Max calls per rolling 60-second window.
        if max_per_minute > 0:
            history = self._call_history.get(tool_name, [])
            cutoff = now - 60.0
            history = [t for t in history if t > cutoff]
            if len(history) >= max_per_minute:
                return (
                    f"Rate limited: {tool_name} called {max_per_minute} times in the last minute. Wait before retrying."
                )
            self._call_history[tool_name] = history

        self._last_call[tool_name] = now
        self._call_history.setdefault(tool_name, []).append(now)
        return None


_tool_limiter = _ToolRateLimiter()

# Pending upload confirmations (token -> file_path).
# Only populated when KILN_CONFIRM_UPLOAD is enabled.
_pending_uploads: dict[str, str] = {}

# Rate limits: {tool_name: (min_interval_ms, max_per_minute)}.
# Read-only tools have no limits.  Physically-dangerous tools get cooldowns.
_TOOL_RATE_LIMITS: dict[str, tuple[int, int]] = {
    "set_temperature": (2000, 10),
    "send_gcode": (500, 30),
    "emergency_stop": (5000, 3),
    "cancel_print": (5000, 3),
    "start_print": (5000, 3),
    "upload_file": (2000, 10),
    "pause_print": (5000, 6),
    "resume_print": (5000, 6),
}


def _check_rate_limit(tool_name: str) -> dict | None:
    """Return an error dict if *tool_name* is rate-limited, else ``None``."""
    limits = _TOOL_RATE_LIMITS.get(tool_name)
    if not limits:
        return None
    msg = _tool_limiter.check(tool_name, limits[0], limits[1])
    if msg:
        _audit(tool_name, "rate_limited", details={"message": msg})
        return _error_dict(msg, code="RATE_LIMITED")
    return None


def _record_tool_block(tool_name: str) -> dict | None:
    """Record a blocked attempt for the circuit breaker.

    Returns an escalation error dict if the threshold is hit, else ``None``.
    """
    escalation_msg = _tool_limiter.record_block(tool_name)
    if escalation_msg:
        _audit(tool_name, "escalated", details={"message": escalation_msg})
        _event_bus.publish(
            EventType.SAFETY_ESCALATED,
            data={"tool": tool_name, "message": escalation_msg},
            source="rate_limiter",
        )
        return _error_dict(escalation_msg, code="SAFETY_ESCALATED")
    return None


# ---------------------------------------------------------------------------
# Safety audit logging
# ---------------------------------------------------------------------------

# Load tool safety classifications for audit metadata.
_TOOL_SAFETY: dict[str, dict[str, Any]] = {}
try:
    import json as _json

    _safety_data_path = Path(__file__).resolve().parent / "data" / "tool_safety.json"
    _raw_safety = _json.loads(_safety_data_path.read_text(encoding="utf-8"))
    _TOOL_SAFETY = _raw_safety.get("classifications", {})
except (FileNotFoundError, ValueError):
    pass

# Per-process session ID — groups all tool calls from one server run together.
# A new UUID is generated each time the MCP server starts.
_SESSION_ID: str = str(_uuid_mod.uuid4())


def _get_safety_level(tool_name: str) -> str:
    """Return the safety classification for a tool (default ``"safe"``)."""
    entry = _TOOL_SAFETY.get(tool_name, {})
    return entry.get("level", "safe")


def _audit(
    tool_name: str,
    action: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Record a safety audit event (fire-and-forget).

    This is non-blocking and will not raise if the DB write fails.
    """
    try:
        db = get_db()
        db.log_audit(
            tool_name=tool_name,
            safety_level=_get_safety_level(tool_name),
            action=action,
            printer_name=_PRINTER_MODEL or None,
            details=details,
            session_id=_SESSION_ID,
        )
    except Exception:
        logger.debug("Failed to write audit log for %s/%s", tool_name, action)


# ---------------------------------------------------------------------------
# Confirmation gate for destructive tools (KILN_CONFIRM_MODE)
# ---------------------------------------------------------------------------

# Pending confirmations: {token: {tool, args, created_at}}.
_pending_confirmations: dict[str, dict[str, Any]] = {}
_CONFIRM_TOKEN_TTL: float = 300.0  # 5 minutes


def _check_confirmation(tool_name: str, args: dict[str, Any]) -> dict | None:
    """If confirm mode is active and the tool is confirm/emergency level, return
    a confirmation-required response.  Otherwise return ``None`` to proceed.
    """
    if not _CONFIRM_MODE:
        return None
    level = _get_safety_level(tool_name)
    if level not in ("confirm", "emergency"):
        return None

    import hashlib

    token = hashlib.sha256(f"{tool_name}:{time.time()}:{id(args)}".encode()).hexdigest()[:16]

    _pending_confirmations[token] = {
        "tool": tool_name,
        "args": args,
        "created_at": time.time(),
    }

    # Prune expired tokens
    now = time.time()
    expired = [t for t, v in _pending_confirmations.items() if now - v["created_at"] > _CONFIRM_TOKEN_TTL]
    for t in expired:
        del _pending_confirmations[t]

    _audit(tool_name, "confirmation_required", details={"args": args})
    return {
        "confirmation_required": True,
        "token": token,
        "tool": tool_name,
        "args": args,
        "expires_in_seconds": int(_CONFIRM_TOKEN_TTL),
        "message": (
            f"{tool_name} requires confirmation (safety level: {level}). "
            f"Call confirm_action(token='{token}') to proceed. "
            f"Token expires in {int(_CONFIRM_TOKEN_TTL / 60)} minutes."
        ),
    }


# ---------------------------------------------------------------------------
# Fleet singletons (registry, queue, event bus)
# ---------------------------------------------------------------------------

_registry = PrinterRegistry()
_queue = PrintQueue(db_path=os.path.join(str(Path.home()), ".kiln", "queue.db"))
_event_bus = EventBus()
_scheduler = JobScheduler(_queue, _registry, _event_bus, persistence=get_db())
_webhook_mgr = WebhookManager(_event_bus)
_auth = AuthManager()
_billing = BillingLedger(db=get_db())
_payment_mgr: PaymentManager | None = None
_billing_alert_mgr: BillingAlertManager | None = None
_cost_estimator = CostEstimator()
_material_tracker = MaterialTracker(db=get_db(), event_bus=_event_bus)
_bed_level_mgr = BedLevelManager(
    db=get_db(),
    event_bus=_event_bus,
    registry=_registry,
)
_stream_proxy = MJPEGProxy()
_cloud_sync: CloudSyncManager | None = None
_heater_watchdog = HeaterWatchdog(
    get_adapter=lambda: _get_adapter(),
    timeout_minutes=_HEATER_TIMEOUT_MIN,
    event_bus=_event_bus,
)
# Subscribe watchdog to print lifecycle events from the scheduler/event bus.
_event_bus.subscribe(EventType.PRINT_STARTED, lambda _e: _heater_watchdog.notify_print_started())
_event_bus.subscribe(EventType.PRINT_COMPLETED, lambda _e: _heater_watchdog.notify_print_ended())
_event_bus.subscribe(EventType.PRINT_FAILED, lambda _e: _heater_watchdog.notify_print_ended())
_event_bus.subscribe(EventType.PRINT_CANCELLED, lambda _e: _heater_watchdog.notify_print_ended())
_plugin_mgr = PluginManager()
_start_time = time.time()

# Thingiverse client (lazy -- created on first use so the module can be
# imported without requiring the token env var).
_thingiverse: ThingiverseClient | None = None


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
            _marketplace_registry.register(Cults3DAdapter(username=_CULTS3D_USERNAME, api_key=_CULTS3D_API_KEY))
        except Exception:
            logger.debug("Could not register Cults3D adapter", exc_info=True)


_fulfillment: FulfillmentProvider | None = None


def _get_fulfillment() -> FulfillmentProvider:
    """Return the lazily-initialised fulfillment provider.

    Provider selection order:
    1. ``KILN_FULFILLMENT_PROVIDER`` env var (explicit choice)
    2. Auto-detect from provider-specific API key env vars
    3. Fall back to Craftcloud if ``KILN_CRAFTCLOUD_API_KEY`` is set
    """
    global _fulfillment  # noqa: PLW0603

    if _fulfillment is not None:
        return _fulfillment

    try:
        _fulfillment = get_fulfillment_provider()
    except (KeyError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            "No fulfillment provider configured.  "
            "Set KILN_FULFILLMENT_PROVIDER and the matching API key env var "
            "(e.g. KILN_CRAFTCLOUD_API_KEY or KILN_SCULPTEO_API_KEY)."
        ) from exc
    return _fulfillment


_fulfillment_monitor: Any | None = None


def _get_fulfillment_monitor() -> Any:
    """Return the lazily-initialised fulfillment monitor.

    Starts the background polling thread on first access.
    """
    global _fulfillment_monitor  # noqa: PLW0603

    if _fulfillment_monitor is not None:
        return _fulfillment_monitor

    from kiln.fulfillment_monitor import FulfillmentMonitor

    _fulfillment_monitor = FulfillmentMonitor(
        db=get_db(),
        event_bus=_event_bus,
    )
    _fulfillment_monitor.start()
    return _fulfillment_monitor


_threedos_client: ThreeDOSClient | None = None


def _get_threedos_client() -> ThreeDOSClient:
    """Return the lazily-initialised 3DOS gateway client.

    Requires ``KILN_3DOS_API_KEY`` to be set.
    """
    global _threedos_client  # noqa: PLW0603

    if _threedos_client is not None:
        return _threedos_client

    _threedos_client = ThreeDOSClient()
    return _threedos_client


def _get_payment_mgr() -> PaymentManager:
    """Return the lazily-initialised payment manager."""
    global _payment_mgr  # noqa: PLW0603

    if _payment_mgr is not None:
        return _payment_mgr

    from kiln.cli.config import get_billing_config

    config = get_billing_config()
    _payment_mgr = PaymentManager(
        db=get_db(),
        config=config,
        event_bus=_event_bus,
        ledger=_billing,
    )

    # Auto-register providers from env vars.
    stripe_key = os.environ.get("KILN_STRIPE_SECRET_KEY", "")
    if stripe_key:
        try:
            from kiln.payments.stripe_provider import StripeProvider

            customer_id = config.get("stripe_customer_id")
            payment_method_id = config.get("stripe_payment_method_id")
            _payment_mgr.register_provider(
                StripeProvider(
                    secret_key=stripe_key,
                    customer_id=customer_id,
                    payment_method_id=payment_method_id,
                ),
            )
        except Exception:
            logger.debug("Could not register Stripe provider")

    circle_key = os.environ.get("KILN_CIRCLE_API_KEY", "")
    if circle_key:
        try:
            from kiln.payments.circle_provider import CircleProvider

            circle_network = os.environ.get(
                "KILN_CIRCLE_NETWORK",
                config.get("circle_default_network", "solana"),
            )
            _payment_mgr.register_provider(
                CircleProvider(api_key=circle_key, default_network=circle_network),
            )
        except Exception:
            logger.debug("Could not register Circle provider")

    return _payment_mgr


def _get_billing_alert_mgr() -> BillingAlertManager:
    """Return the lazily-initialised billing alert manager."""
    global _billing_alert_mgr  # noqa: PLW0603

    if _billing_alert_mgr is None:
        _billing_alert_mgr = BillingAlertManager(event_bus=_event_bus)
        _billing_alert_mgr.subscribe()
    return _billing_alert_mgr


def _refund_after_order_failure(
    pay_result: Any | None,
    payment_hold_id: str,
) -> str | None:
    """Best-effort refund/cancel after a failed order placement.

    If a completed charge exists (``pay_result``), attempts to refund it
    via the provider that processed it.  If only a hold exists
    (``payment_hold_id``), cancels the hold instead.  Failures are
    logged at ERROR level and a PAYMENT_FAILED event is emitted so
    BillingAlertManager can pick it up.

    Returns:
        A warning string if the refund failed and manual intervention
        is required, or ``None`` if the refund succeeded.
    """
    if pay_result and getattr(pay_result, "payment_id", None):
        payment_id = pay_result.payment_id
        try:
            mgr = _get_payment_mgr()
            rail_name = mgr.get_active_rail()
            provider = mgr.get_provider(rail_name)
            if provider:
                provider.refund_payment(payment_id)
                logger.info(
                    "Auto-refunded payment %s after order failure",
                    payment_id,
                )
        except Exception as exc:
            logger.error(
                "CRITICAL: Failed to auto-refund payment %s after order failure. Manual refund required. Error: %s",
                payment_id,
                exc,
            )
            # Emit event for alert manager.
            try:
                _event_bus.publish(
                    EventType.PAYMENT_FAILED,
                    {
                        "payment_id": payment_id,
                        "error": f"Auto-refund failed: {exc}",
                        "requires_manual_refund": True,
                    },
                    source="fulfillment",
                )
            except Exception as exc2:
                logger.debug("Failed to publish refund failure event: %s", exc2)
            return f"WARNING: Automatic refund of payment {payment_id} failed. Manual refund may be required."
    elif payment_hold_id:
        try:
            mgr = _get_payment_mgr()
            mgr.cancel_fee(payment_hold_id)
            logger.info(
                "Cancelled hold %s after order failure",
                payment_hold_id,
            )
        except Exception as exc:
            logger.error(
                "CRITICAL: Failed to cancel hold %s after order failure. Manual cancellation required. Error: %s",
                payment_hold_id,
                exc,
            )
            # Emit event for alert manager.
            try:
                _event_bus.publish(
                    EventType.PAYMENT_FAILED,
                    {
                        "payment_id": payment_hold_id,
                        "error": f"Hold cancellation failed: {exc}",
                        "requires_manual_refund": True,
                    },
                    source="fulfillment",
                )
            except Exception as exc2:
                logger.debug("Failed to publish hold cancellation failure event: %s", exc2)
            return (
                f"WARNING: Cancellation of payment hold {payment_hold_id} failed. Manual cancellation may be required."
            )
    return None


# Error codes that represent transient failures the caller may retry.
_RETRYABLE_CODES = frozenset(
    {
        "ERROR",  # Generic printer / runtime errors are typically transient.
        "INTERNAL_ERROR",
        "GENERATION_TIMEOUT",
        "RATE_LIMIT",
    }
)

# Per-check remediation hints shown when mandatory preflight blocks start_print.
_PREFLIGHT_HINTS: dict[str, str] = {
    "printer_connected": "Check that the printer is powered on, connected to the network, and reachable at the configured host.",
    "printer_idle": "Wait for the current job to finish or cancel it with cancel_print() before starting a new print.",
    "no_errors": "Clear the error on the printer (power-cycle or acknowledge via the printer's UI) and retry.",
    "temperatures_safe": "Wait for temperatures to cool to safe levels or adjust the target temps before printing.",
    "filament_loaded": "Load filament into the printer and verify the runout sensor detects it.",
    "material_match": "Swap the loaded filament to match the expected material, or omit the expected_material parameter.",
    "material_compatible": "Use a validated material for this printer model, or set KILN_STRICT_MATERIAL_CHECK=false.",
    "file_valid": "Check the G-code file for corruption or invalid commands. Re-slice if necessary.",
    "file_on_printer": "Upload the file to the printer first using upload_file(), then retry start_print().",
}


# ---------------------------------------------------------------------------
# Safety helpers
# ---------------------------------------------------------------------------

# Environment variable names containing secrets — used to sanitize logs.
_SECRET_ENV_VARS = (
    "KILN_PRINTER_API_KEY",
    "KILN_THINGIVERSE_TOKEN",
    "KILN_MMF_API_KEY",
    "KILN_CULTS3D_API_KEY",
    "KILN_MESHY_API_KEY",
    "KILN_CRAFTCLOUD_API_KEY",
    "KILN_PRINTER_ACCESS_CODE",
    "KILN_CIRCLE_API_KEY",
    "KILN_STRIPE_API_KEY",
    "KILN_STRIPE_WEBHOOK_SECRET",
    "KILN_API_AUTH_TOKEN",
    "KILN_AUTH_TOKEN",
)


def _sanitize_log_msg(msg: str) -> str:
    """Replace any env var secret values in *msg* with ``***``."""
    for var in _SECRET_ENV_VARS:
        val = os.environ.get(var, "")
        if len(val) > 4:
            msg = msg.replace(val, "***")
    return msg


def _check_disk_space(path: str, required_mb: int = 100) -> dict[str, Any] | None:
    """Return an error dict if fewer than *required_mb* MB are free at *path*.

    Returns ``None`` if there's enough space.
    """
    try:
        usage = shutil.disk_usage(path)
        free_mb = usage.free / (1024 * 1024)
        if free_mb < required_mb:
            return _error_dict(
                f"Insufficient disk space: {free_mb:.0f} MB free, need at least {required_mb} MB.",
                code="DISK_FULL",
            )
    except OSError:
        pass  # Can't check — proceed optimistically
    return None


def _error_dict(
    message: str,
    code: str = "ERROR",
    *,
    retryable: bool | None = None,
) -> dict[str, Any]:
    """Build a standardised error response dict.

    If *retryable* is not supplied explicitly it is inferred from *code*:
    codes in ``_RETRYABLE_CODES`` are assumed retryable, everything else
    (auth, validation, not-found, unsupported) is not.
    """
    if retryable is None:
        retryable = code in _RETRYABLE_CODES
    return {
        "success": False,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
        },
    }


def _check_auth(scope: str) -> dict[str, Any] | None:
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


def _check_billing_auth(scope: str = "print") -> dict[str, Any] | None:
    """Check authentication for billable operations.

    Unlike :func:`_check_auth`, this ALWAYS requires authentication for
    operations that involve real money (fulfillment orders, payment
    setup, etc.) — even when global auth is disabled.
    """
    if not _auth.enabled:
        return {
            "error": (
                "Authentication required for paid operations. "
                "Enable auth with KILN_AUTH_ENABLED=1 and set "
                "KILN_AUTH_KEY=<your-key> before using fulfillment services. "
                "See: kiln auth setup"
            ),
            "status": "error",
            "code": "AUTH_REQUIRED",
        }
    return _check_auth(scope)


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
        EventType.JOB_QUEUED,
        EventType.JOB_STARTED,
        EventType.JOB_COMPLETED,
        EventType.JOB_FAILED,
        EventType.JOB_CANCELLED,
    }
    if event.type in job_events and "job_id" in event.data:
        try:
            job = _queue.get_job(event.data["job_id"])
            db = get_db()
            db.save_job(
                {
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
                }
            )
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
        fee_calc, _charge_id = _billing.calculate_and_record_fee(
            event.data["job_id"],
            float(network_cost),
        )
        logger.info(
            "Billing: job %s network_cost=%.2f fee=%.2f (waived=%s)",
            event.data["job_id"],
            network_cost,
            fee_calc.fee_amount,
            fee_calc.waived,
        )
    except Exception:
        logger.error(
            "Failed to record billing for job %s — "
            "this job may not appear in billing history. "
            "Check billing_history for accuracy.",
            event.data.get("job_id"),
            exc_info=True,
        )


def _log_print_completion(event: Event) -> None:
    """EventBus subscriber that logs completed/failed jobs to print_history."""
    try:
        job = _queue.get_job(event.data["job_id"])
        duration = None
        if job.started_at and job.completed_at:
            duration = job.completed_at - job.started_at

        record = {
            "job_id": job.id,
            "printer_name": job.printer_name or "unknown",
            "file_name": job.file_name,
            "status": "completed" if event.type == EventType.JOB_COMPLETED else "failed",
            "duration_seconds": duration,
            "material_type": event.data.get("material_type"),
            "file_hash": event.data.get("file_hash"),
            "slicer_profile": event.data.get("slicer_profile"),
            "agent_id": event.data.get("agent_id") or os.environ.get("KILN_AGENT_ID", "default"),
            "metadata": {
                k: v
                for k, v in event.data.items()
                if k not in ("job_id", "material_type", "file_hash", "slicer_profile", "agent_id")
            }
            or None,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
            "created_at": time.time(),
        }
        get_db().save_print_record(record)
    except Exception:
        logger.debug("Failed to log print completion for job %s", event.data.get("job_id"), exc_info=True)


# Wire subscribers (runs automatically on import)
_event_bus.subscribe(None, _persist_event)
_event_bus.subscribe(EventType.JOB_COMPLETED, _billing_hook)
_event_bus.subscribe(EventType.JOB_COMPLETED, _log_print_completion)
_event_bus.subscribe(EventType.JOB_FAILED, _log_print_completion)

# Wire billing alert manager (lazy init on first access).
try:
    _get_billing_alert_mgr()
except Exception:
    logger.debug("Billing alert manager not initialized", exc_info=True)

# Start fulfillment order monitor if fulfillment is available.
try:
    monitor = _get_fulfillment_monitor()
    monitor.start()
except Exception:
    logger.debug("Fulfillment monitor not started", exc_info=True)


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
        return _error_dict(
            f"Failed to get printer status: {exc}. Check that the printer is online and KILN_PRINTER_HOST is correct."
        )
    except Exception as exc:
        logger.exception("Unexpected error in printer_status")
        return _error_dict(f"Unexpected error in printer_status: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def printer_files() -> dict:
    """List all G-code files available on the printer.

    Returns a JSON array of file objects, each containing:
    - ``name``: file name
    - ``path``: full path on the printer
    - ``size_bytes``: file size (may be null)
    - ``date``: upload timestamp as Unix epoch (may be null)

    When G-code metadata is available, files may also include:
    - ``material``, ``estimated_time_seconds``, ``tool_temp``,
      ``bed_temp``, ``slicer``, ``layer_height``, ``filament_used_mm``

    Use this to discover which files are ready to print.  Pass a file's
    ``name`` or ``path`` to ``start_print`` to begin printing it.
    For detailed metadata on a specific file, use ``analyze_print_file()``.
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
        return _error_dict(
            f"Failed to list printer files: {exc}. Check that the printer is online and KILN_PRINTER_HOST is correct."
        )
    except Exception as exc:
        logger.exception("Unexpected error in printer_files")
        return _error_dict(f"Unexpected error in printer_files: {exc}", code="INTERNAL_ERROR")


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
    if err := _check_rate_limit("upload_file"):
        return err
    try:
        adapter = _get_adapter()

        # Check file exists and size before uploading
        if not os.path.isfile(file_path):
            return _error_dict(
                f"File not found: {file_path}",
                code="FILE_NOT_FOUND",
            )
        _MAX_UPLOAD_SIZE = 500 * 1024 * 1024  # 500MB
        file_size = os.path.getsize(file_path)
        if file_size > _MAX_UPLOAD_SIZE:
            return _error_dict(
                f"File too large ({file_size / 1024 / 1024:.1f}MB). "
                f"Maximum upload size is {_MAX_UPLOAD_SIZE / 1024 / 1024:.0f}MB.",
                code="VALIDATION_ERROR",
            )
        if file_size == 0:
            return _error_dict(
                "File is empty (0 bytes).",
                code="VALIDATION_ERROR",
            )

        # -- G-code safety scan (blocked commands + temperature limits) ------
        _GCODE_EXTENSIONS = {".gcode", ".gco", ".g"}
        scan_warnings: list[str] = []
        if os.path.splitext(file_path)[1].lower() in _GCODE_EXTENSIONS:
            try:
                from kiln.gcode import scan_gcode_file

                scan = scan_gcode_file(file_path, printer_id=_PRINTER_MODEL or None)
                if not scan.valid:
                    return {
                        "success": False,
                        "error": {
                            "code": "GCODE_BLOCKED",
                            "message": "File contains blocked G-code commands and was not uploaded.",
                        },
                        "blocked_commands": scan.blocked_commands[:10],
                        "errors": scan.errors[:10],
                    }
                scan_warnings = scan.warnings[:10]
            except (ImportError, FileNotFoundError, PermissionError):
                pass  # scan is best-effort; file existence was already verified above

        # -- Upload confirmation gate (when KILN_CONFIRM_UPLOAD is set) ------
        file_name = os.path.basename(file_path)
        if _CONFIRM_UPLOAD:
            import hashlib

            token = hashlib.sha256(f"{file_path}:{file_size}".encode()).hexdigest()[:16]
            # Store token for upload_file_confirm() to verify.
            _pending_uploads[token] = file_path
            summary: dict[str, Any] = {
                "confirmation_required": True,
                "token": token,
                "file_name": file_name,
                "file_size_bytes": file_size,
                "message": (
                    f"Upload of {file_name} ({file_size / 1024:.1f} KB) "
                    f"requires confirmation. Call upload_file_confirm(token='{token}') "
                    f"to proceed."
                ),
            }
            if scan_warnings:
                summary["warnings"] = scan_warnings
            return summary

        result = adapter.upload_file(file_path)
        resp = result.to_dict()
        if scan_warnings:
            resp["warnings"] = scan_warnings
        return resp
    except FileNotFoundError as exc:
        return _error_dict(f"Failed to upload file: {exc}", code="FILE_NOT_FOUND")
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(
            f"Failed to upload file: {exc}. Check that the printer is online and KILN_PRINTER_HOST is correct."
        )
    except Exception as exc:
        logger.exception("Unexpected error in upload_file")
        return _error_dict(f"Unexpected error in upload_file: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def upload_file_confirm(token: str) -> dict:
    """Confirm and execute a pending file upload.

    When ``KILN_CONFIRM_UPLOAD`` is enabled, ``upload_file()`` returns a
    confirmation token instead of uploading immediately.  Pass that token
    here to proceed with the upload.

    Args:
        token: The confirmation token returned by ``upload_file()``.
    """
    if err := _check_auth("files"):
        return err
    file_path = _pending_uploads.pop(token, None)
    if file_path is None:
        return _error_dict(
            f"Invalid or expired upload token: {token!r}. Call upload_file() again to get a new token.",
            code="INVALID_TOKEN",
        )
    try:
        adapter = _get_adapter()
        result = adapter.upload_file(file_path)
        return result.to_dict()
    except FileNotFoundError as exc:
        return _error_dict(f"Failed to confirm upload: {exc}", code="FILE_NOT_FOUND")
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(
            f"Failed to confirm upload: {exc}. Check that the printer is online and KILN_PRINTER_HOST is correct."
        )
    except Exception as exc:
        logger.exception("Unexpected error in upload_file_confirm")
        return _error_dict(f"Unexpected error in upload_file_confirm: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def analyze_print_file(filename: str) -> dict:
    """Analyze a G-code file on the printer and extract its metadata.

    Reads the file header to extract slicer-embedded metadata such as
    material type, estimated print time, temperatures, layer height,
    and filament usage.  This is especially useful when filenames are
    meaningless (e.g. ``test5112.gcode``) and the agent needs to
    understand what a file will print.

    Args:
        filename: Name or path of the file as shown by ``printer_files()``.

    Returns a JSON object with:
    - ``filename``: the file name
    - ``metadata``: extracted metadata (material, time, temps, slicer, etc.)
    - ``has_metadata``: whether any metadata was found
    """
    try:
        from kiln.gcode_metadata import extract_metadata_from_content  # noqa: E402

        adapter = _get_adapter()
        files = adapter.list_files()

        # Find the requested file
        target = None
        for f in files:
            if f.name == filename or f.path == filename:
                target = f
                break

        if target is None:
            return _error_dict(
                f"File not found on printer: {filename!r}. Use printer_files() to list available files.",
                code="FILE_NOT_FOUND",
            )

        # Try to download file content for metadata extraction.
        # Not all adapters support content download -- this is best-effort.
        metadata_dict: dict[str, Any] = {}
        try:
            if hasattr(adapter, "download_file_content"):
                content = adapter.download_file_content(target.path)
                if content:
                    meta = extract_metadata_from_content(content)
                    metadata_dict = meta.to_dict()
        except Exception as exc:
            logger.debug("Could not download file content for metadata: %s", exc)

        return {
            "success": True,
            "filename": target.name,
            "path": target.path,
            "size_bytes": target.size_bytes,
            "metadata": metadata_dict,
            "has_metadata": bool(metadata_dict),
        }
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to analyze print file: {exc}. Use printer_files() to list available files.")
    except Exception as exc:
        logger.exception("Unexpected error in analyze_print_file")
        return _error_dict(f"Unexpected error in analyze_print_file: {exc}", code="INTERNAL_ERROR")


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
        if not ok:
            return _error_dict(
                f"Failed to delete {file_path}. The printer may have rejected the request. Use printer_files() to verify the file exists."
            )
        return {
            "success": True,
            "message": f"Deleted {file_path}.",
        }
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to delete {file_path}: {exc}. Use printer_files() to verify the file exists.")
    except Exception as exc:
        logger.exception("Unexpected error in delete_file")
        return _error_dict(f"Unexpected error in delete_file: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def start_print(file_name: str) -> dict:
    """Start printing a file that already exists on the printer.

    Automatically runs pre-flight safety checks before starting.  If any
    check fails the print is blocked and the check results are returned
    so the agent can diagnose and fix the issue.

    Args:
        file_name: Name or path of the file as shown by ``printer_files()``.
    """
    if err := _check_auth("print"):
        return err
    if err := _check_rate_limit("start_print"):
        return err
    if conf := _check_confirmation("start_print", {"file_name": file_name}):
        return conf
    try:
        adapter = _get_adapter()

        # -- Automatic pre-flight safety gate ----------------------------------
        # Mandatory by default.  Set KILN_SKIP_PREFLIGHT=1 to bypass (advanced
        # users only — e.g. custom firmware that reports non-standard states).
        skip_preflight = os.environ.get("KILN_SKIP_PREFLIGHT", "").strip() in (
            "1",
            "true",
            "yes",
        )
        if skip_preflight:
            logger.warning(
                "KILN_SKIP_PREFLIGHT is set — skipping mandatory pre-flight "
                "safety checks for start_print(%s). This is unsafe and should "
                "only be used with custom firmware or during development.",
                file_name,
            )
            _audit("start_print", "preflight_skipped", details={"file": file_name})
        else:
            pf = preflight_check(remote_file=file_name)
            if not pf.get("ready", False):
                # Build a detailed remediation message from individual checks
                failed = [c for c in pf.get("checks", []) if not c.get("passed", False)]
                remediation_lines = []
                for chk in failed:
                    name = chk.get("name", "unknown")
                    msg = chk.get("message", "check failed")
                    hint = _PREFLIGHT_HINTS.get(name, "Investigate and resolve before retrying.")
                    remediation_lines.append(f"  - {name}: {msg}. Fix: {hint}")

                detail_text = "\n".join(remediation_lines) if remediation_lines else ""
                summary = pf.get("summary", "Pre-flight checks failed")
                full_message = (
                    (
                        f"{summary}\n\nFailed checks:\n{detail_text}\n\n"
                        "Resolve the issues above and retry. To bypass pre-flight "
                        "checks (advanced users only), set KILN_SKIP_PREFLIGHT=1."
                    )
                    if detail_text
                    else (f"{summary}\n\nTo bypass pre-flight checks (advanced users only), set KILN_SKIP_PREFLIGHT=1.")
                )

                _audit(
                    "start_print",
                    "preflight_failed",
                    details={
                        "file": file_name,
                        "summary": summary,
                        "failed_checks": [c.get("name") for c in failed],
                    },
                )
                result = _error_dict(full_message, code="PREFLIGHT_FAILED")
                result["preflight"] = pf
                return result

        result = adapter.start_print(file_name)
        _heater_watchdog.notify_print_started()
        _audit("start_print", "executed", details={"file": file_name})
        return result.to_dict()
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(
            f"Failed to start print: {exc}. Check that the printer is online and idle. Use printer_files() to verify the file exists."
        )
    except Exception as exc:
        logger.exception("Unexpected error in start_print")
        return _error_dict(f"Unexpected error in start_print: {exc}", code="INTERNAL_ERROR")


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
    if err := _check_rate_limit("cancel_print"):
        return err
    if conf := _check_confirmation("cancel_print", {}):
        return conf
    try:
        adapter = _get_adapter()
        result = adapter.cancel_print()
        _heater_watchdog.notify_print_ended()
        _audit("cancel_print", "executed")
        return result.to_dict()
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to cancel print: {exc}. Check that a print is currently active.")
    except Exception as exc:
        logger.exception("Unexpected error in cancel_print")
        return _error_dict(f"Unexpected error in cancel_print: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def emergency_stop(printer_name: str | None = None) -> dict:
    """Trigger an emergency stop on one or all printers.

    Sends M112 (emergency stop), turns off heaters, and disables steppers.
    Unlike ``cancel_print``, this does **not** allow a graceful cooldown —
    all motion ceases instantly.

    Use only in genuine safety emergencies (thermal runaway, collision,
    spaghetti failure threatening the hotend, etc.).

    WARNING: After an emergency stop the printer typically requires a
    power cycle or firmware restart before it can print again.

    Args:
        printer_name: Specific printer to stop. If None, stops ALL printers.
    """
    if err := _check_auth("print"):
        return err
    if err := _check_rate_limit("emergency_stop"):
        return err
    if conf := _check_confirmation("emergency_stop", {}):
        return conf
    try:
        from kiln.emergency import get_emergency_coordinator

        coord = get_emergency_coordinator()
        if printer_name:
            result = coord.emergency_stop(printer_name)
            _audit("emergency_stop", f"executed for {printer_name}")
            return {"success": True, "emergency_stop": result.to_dict()}
        else:
            results = coord.emergency_stop_all()
            _audit("emergency_stop", "executed for ALL printers")
            return {
                "success": True,
                "emergency_stop": [r.to_dict() for r in results],
            }
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to execute emergency stop: {exc}. Check that the printer is online.")
    except Exception as exc:
        logger.exception("Unexpected error in emergency_stop")
        return _error_dict(f"Unexpected error in emergency_stop: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def pause_print() -> dict:
    """Pause the currently running print job.

    Pausing lifts the nozzle and parks the head.  The heaters stay on.
    Use ``resume_print()`` to continue from where the print left off.
    """
    if err := _check_auth("print"):
        return err
    if err := _check_rate_limit("pause_print"):
        return err
    try:
        adapter = _get_adapter()
        result = adapter.pause_print()
        return result.to_dict()
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to pause print: {exc}. Check that a print is currently active.")
    except Exception as exc:
        logger.exception("Unexpected error in pause_print")
        return _error_dict(f"Unexpected error in pause_print: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def resume_print() -> dict:
    """Resume a paused print job.

    The printer must currently be in a paused state.  Resuming will return
    the nozzle to its previous position and continue extruding.
    """
    if err := _check_auth("print"):
        return err
    if err := _check_rate_limit("resume_print"):
        return err
    try:
        adapter = _get_adapter()
        result = adapter.resume_print()
        return result.to_dict()
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to resume print: {exc}. Check that the printer is in a paused state.")
    except Exception as exc:
        logger.exception("Unexpected error in resume_print")
        return _error_dict(f"Unexpected error in resume_print: {exc}", code="INTERNAL_ERROR")


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
    if err := _check_rate_limit("set_temperature"):
        return err
    if conf := _check_confirmation("set_temperature", {"tool_temp": tool_temp, "bed_temp": bed_temp}):
        return conf
    if tool_temp is None and bed_temp is None:
        return _error_dict(
            "At least one of tool_temp or bed_temp must be provided.",
            code="INVALID_ARGS",
        )

    # -- Temperature safety validation (per-printer when configured) ------
    _MAX_TOOL, _MAX_BED = _get_temp_limits()
    if tool_temp is not None:
        if tool_temp < 0:
            return _error_dict(
                f"Hotend temperature {tool_temp}°C is negative -- must be >= 0.",
                code="VALIDATION_ERROR",
            )
        if tool_temp > _MAX_TOOL:
            return _error_dict(
                f"Hotend temperature {tool_temp}°C exceeds safety limit ({_MAX_TOOL}°C).",
                code="VALIDATION_ERROR",
            )
    if bed_temp is not None:
        if bed_temp < 0:
            return _error_dict(
                f"Bed temperature {bed_temp}°C is negative -- must be >= 0.",
                code="VALIDATION_ERROR",
            )
        if bed_temp > _MAX_BED:
            return _error_dict(
                f"Bed temperature {bed_temp}°C exceeds safety limit ({_MAX_BED}°C).",
                code="VALIDATION_ERROR",
            )

    try:
        adapter = _get_adapter()
        results: dict[str, Any] = {"success": True}

        # -- Relative temperature change advisory (non-blocking) ----------
        _DELTA_WARN_TOOL = 10.0
        _DELTA_WARN_BED = 50.0
        rate_warnings: list[str] = []
        try:
            state = adapter.get_state()
            if tool_temp is not None and state.tool_temp_target is not None and state.tool_temp_target > 0:
                delta = abs(tool_temp - state.tool_temp_target)
                if delta > _DELTA_WARN_TOOL:
                    rate_warnings.append(
                        f"Large hotend temperature change: "
                        f"{state.tool_temp_target:.0f}°C -> {tool_temp:.0f}°C "
                        f"(delta {delta:.0f}°C)."
                    )
            if bed_temp is not None and state.bed_temp_target is not None and state.bed_temp_target > 0:
                delta = abs(bed_temp - state.bed_temp_target)
                if delta > _DELTA_WARN_BED:
                    rate_warnings.append(
                        f"Large bed temperature change: "
                        f"{state.bed_temp_target:.0f}°C -> {bed_temp:.0f}°C "
                        f"(delta {delta:.0f}°C)."
                    )
        except Exception as exc:
            logger.debug(
                "Failed to compute temperature rate warnings: %s", exc
            )  # Don't let warning logic block the actual operation.

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

        # -- Heater-off safety net ----------------------------------------
        # Some OctoPrint setups don't reliably turn off heaters at 0 deg C.
        # Send explicit G-code commands as a best-effort safety measure.
        if tool_temp == 0 or bed_temp == 0:
            try:
                gcode_cmds: list[str] = []
                if tool_temp == 0:
                    gcode_cmds.append("M104 S0")  # hotend off
                if bed_temp == 0:
                    gcode_cmds.append("M140 S0")  # bed off
                if gcode_cmds:
                    adapter.send_gcode(gcode_cmds)
                    results["heater_off_gcode_sent"] = True
            except Exception:
                # Best-effort -- don't fail the main set_temperature op
                logger.debug("Heater-off safety G-code failed (best-effort)")
                results["heater_off_gcode_sent"] = False

        if rate_warnings:
            results["warnings"] = rate_warnings

        # Notify heater watchdog when heaters are turned on.
        if (tool_temp is not None and tool_temp > 0) or (bed_temp is not None and bed_temp > 0):
            _heater_watchdog.notify_heater_set()

        _audit(
            "set_temperature",
            "executed",
            details={
                "tool_temp": tool_temp,
                "bed_temp": bed_temp,
            },
        )
        return results
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(
            f"Failed to set temperature: {exc}. Check that the printer is online and KILN_PRINTER_HOST is correct."
        )
    except Exception as exc:
        logger.exception("Unexpected error in set_temperature")
        return _error_dict(f"Unexpected error in set_temperature: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def preflight_check(
    file_path: str | None = None, expected_material: str | None = None, remote_file: str | None = None
) -> dict:
    """Run pre-print safety checks to verify the printer is ready.

    Checks performed:
    - Printer is connected and operational
    - Printer is not currently printing
    - No error flags are set
    - Temperatures are within safe limits
    - (Optional) Material loaded matches expected material
    - (Optional) Local G-code file is valid and readable
    - (Optional) Remote file exists on the printer

    Args:
        file_path: Optional path to a local G-code file to validate before
            upload.  If omitted, only printer-state checks are performed.
        expected_material: Optional material type (e.g. "PLA", "ABS", "PETG").
            If provided and a material is loaded, checks for a mismatch.

        remote_file: Optional filename to verify exists on the printer.
            If provided, checks the printer's file list for a matching file.
    Call this before ``start_print()`` to catch problems early.  The result
    includes a ``ready`` boolean and detailed per-check breakdowns.
    """
    try:
        adapter = _get_adapter()

        # -- Printer state checks ------------------------------------------
        state = adapter.get_state()
        checks: list[dict[str, Any]] = []
        errors: list[str] = []

        # Connected
        is_connected = state.connected
        checks.append(
            {
                "name": "printer_connected",
                "passed": is_connected,
                "message": "Printer is connected" if is_connected else "Printer is offline",
            }
        )
        if not is_connected:
            errors.append("Printer is not connected / offline")

        # Idle (not printing or in error)
        idle_states = {PrinterStatus.IDLE}
        is_idle = state.state in idle_states
        checks.append(
            {
                "name": "printer_idle",
                "passed": is_idle,
                "message": f"Printer state: {state.state.value}",
            }
        )
        if not is_idle:
            errors.append(f"Printer is not idle (state: {state.state.value})")

        # No error
        no_error = state.state != PrinterStatus.ERROR
        checks.append(
            {
                "name": "no_errors",
                "passed": no_error,
                "message": "No errors" if no_error else "Printer is in error state",
            }
        )
        if not no_error:
            errors.append("Printer is in an error state")

        # -- Temperature checks --------------------------------------------
        temp_warnings: list[str] = []
        MAX_TOOL, MAX_BED = _get_temp_limits()

        if state.tool_temp_actual is not None and state.tool_temp_actual > MAX_TOOL:
            temp_warnings.append(f"Tool temp ({state.tool_temp_actual:.1f}C) exceeds safe max ({MAX_TOOL:.0f}C)")
        if state.bed_temp_actual is not None and state.bed_temp_actual > MAX_BED:
            temp_warnings.append(f"Bed temp ({state.bed_temp_actual:.1f}C) exceeds safe max ({MAX_BED:.0f}C)")

        temps_safe = len(temp_warnings) == 0
        checks.append(
            {
                "name": "temperatures_safe",
                "passed": temps_safe,
                "message": "Temperatures within limits" if temps_safe else "; ".join(temp_warnings),
            }
        )
        if not temps_safe:
            errors.extend(temp_warnings)

        # -- Filament sensor check (optional) ----------------------------------
        if adapter.capabilities.can_detect_filament:
            try:
                filament_status = adapter.get_filament_status()
                if filament_status is not None:
                    filament_detected = filament_status.get("detected", False)
                    sensor_enabled = filament_status.get("sensor_enabled", False)
                    if sensor_enabled and not filament_detected:
                        checks.append(
                            {
                                "name": "filament_loaded",
                                "passed": True,  # Warning only -- does not block print
                                "message": (
                                    "WARNING: Filament not detected by runout sensor. "
                                    "Verify filament is loaded before printing."
                                ),
                                "advisory": True,
                            }
                        )
                    elif sensor_enabled and filament_detected:
                        checks.append(
                            {
                                "name": "filament_loaded",
                                "passed": True,
                                "message": "Filament detected by runout sensor",
                            }
                        )
                    # If sensor not enabled, skip silently
            except Exception as exc:
                logger.debug("Filament sensor check failed: %s", exc)  # Filament sensor not available -- skip silently

        # -- Material mismatch check (optional) ----------------------------
        _strict_material = os.environ.get("KILN_STRICT_MATERIAL_CHECK", "true").lower() in ("1", "true", "yes")

        if expected_material is not None:
            # 1) Check against loaded material (if material tracking is configured)
            try:
                printer_name = "default"
                if _registry.count > 0:
                    names = _registry.list_names()
                    if names:
                        printer_name = names[0]
                warning = _material_tracker.check_match(printer_name, expected_material)
                if warning is not None:
                    mat_msg = warning.message
                    checks.append(
                        {
                            "name": "material_match",
                            "passed": False,
                            "message": mat_msg,
                        }
                    )
                    errors.append(mat_msg)
                else:
                    checks.append(
                        {
                            "name": "material_match",
                            "passed": True,
                            "message": f"Loaded material matches expected ({expected_material.upper()})",
                        }
                    )
            except Exception as exc:
                # Material tracking not configured — skip silently
                logger.debug("Material match check failed: %s", exc)

            # 2) Check against printer intelligence DB (material compatibility)
            if _PRINTER_MODEL:
                try:
                    mat_settings = get_material_settings(_PRINTER_MODEL, expected_material)
                    if mat_settings is None:
                        msg = (
                            f"Material {expected_material.upper()} is not validated "
                            f"for printer model '{_PRINTER_MODEL}'. "
                            f"This material may damage the printer."
                        )
                        # Strict mode = blocking; non-strict = warning only
                        checks.append(
                            {
                                "name": "material_compatible",
                                "passed": not _strict_material,
                                "message": msg,
                            }
                        )
                        if _strict_material:
                            errors.append(msg)
                    else:
                        checks.append(
                            {
                                "name": "material_compatible",
                                "passed": True,
                                "message": (
                                    f"{expected_material.upper()} is validated for "
                                    f"'{_PRINTER_MODEL}' "
                                    f"(hotend {mat_settings.hotend_temp}C, bed {mat_settings.bed_temp}C)"
                                ),
                            }
                        )
                except Exception as exc:
                    logger.debug("Failed to check material compatibility via intelligence DB: %s", exc)

        # -- Outcome history advisory (learning database) ------------------
        # Query past outcomes for this printer + material combo to warn
        # about historically problematic combinations.  Advisory only —
        # never blocks a print.
        try:
            _printer_name = None
            if _registry.count > 0:
                names = _registry.list_names()
                if names:
                    _printer_name = names[0]

            if _printer_name:
                _db = get_db()
                _mat = expected_material

                # Use get_printer_learning_insights for aggregate data
                insights = _db.get_printer_learning_insights(_printer_name)

                if insights["total_outcomes"] >= 3:
                    success_rate = insights["success_rate"]

                    # Check material-specific failure rate if material provided
                    mat_warning = None
                    if _mat and _mat.upper() in insights.get("material_stats", {}):
                        mat_stats = insights["material_stats"][_mat.upper()]
                        mat_success = mat_stats["success_rate"]
                        mat_count = mat_stats["count"]
                        if mat_count >= 3 and mat_success < 0.3:
                            mat_warning = (
                                f"Warning: {_mat.upper()} has a {int(mat_success * 100)}% success rate "
                                f"on {_printer_name} ({mat_count} prints). "
                                f"Consider adjusting settings or trying a different printer."
                            )

                    # Check top failure modes
                    failure_info = insights.get("failure_breakdown", {})
                    top_failures = sorted(failure_info.items(), key=lambda x: x[1], reverse=True)[:3]

                    if mat_warning:
                        checks.append(
                            {
                                "name": "outcome_history",
                                "passed": True,  # Advisory — always passes
                                "message": mat_warning,
                                "advisory": True,
                            }
                        )
                    elif success_rate < 0.5 and insights["total_outcomes"] >= 5:
                        failure_summary = (
                            ", ".join(f"{m} ({c}x)" for m, c in top_failures) if top_failures else "unknown"
                        )
                        checks.append(
                            {
                                "name": "outcome_history",
                                "passed": True,  # Advisory — always passes
                                "message": (
                                    f"Advisory: {_printer_name} has a {int(success_rate * 100)}% overall success rate "
                                    f"({insights['total_outcomes']} prints). "
                                    f"Common failures: {failure_summary}."
                                ),
                                "advisory": True,
                            }
                        )
                    else:
                        checks.append(
                            {
                                "name": "outcome_history",
                                "passed": True,
                                "message": (
                                    f"Learning data: {int(success_rate * 100)}% success rate "
                                    f"({insights['total_outcomes']} outcomes recorded)"
                                ),
                            }
                        )
        except Exception as exc:
            logger.debug(
                "Learning DB outcome history check failed: %s", exc
            )  # Learning DB not available — skip silently

        # -- File validation (optional) ------------------------------------
        file_result: dict[str, Any] | None = None
        if file_path is not None:
            file_result = _validate_local_file(file_path)
            file_ok = file_result.get("valid", False)
            checks.append(
                {
                    "name": "file_valid",
                    "passed": file_ok,
                    "message": "File OK" if file_ok else "; ".join(file_result.get("errors", [])),
                }
            )
            if not file_ok:
                errors.extend(file_result.get("errors", []))

        # -- Remote file check (optional) ----------------------------------
        if remote_file is not None:
            try:
                printer_files = adapter.list_files()
                remote_lower = remote_file.lower()
                file_found = any(
                    f.name.lower() == remote_lower or f.path.lower() == remote_lower for f in printer_files
                )
                checks.append(
                    {
                        "name": "file_on_printer",
                        "passed": file_found,
                        "message": (
                            f"File '{remote_file}' found on printer"
                            if file_found
                            else f"File '{remote_file}' not found on printer"
                        ),
                    }
                )
                if not file_found:
                    errors.append(f"File '{remote_file}' not found on printer")
            except Exception as exc:
                logger.debug("Failed to verify remote file on printer: %s", exc)
                checks.append(
                    {
                        "name": "file_on_printer",
                        "passed": False,
                        "message": "Unable to list files on printer to verify remote file",
                    }
                )
                errors.append("Unable to list files on printer to verify remote file")

        # -- Summary -------------------------------------------------------
        ready = all(c["passed"] for c in checks)
        summary = (
            "All pre-flight checks passed. Ready to print."
            if ready
            else "Pre-flight checks failed: " + "; ".join(errors) + "."
        )

        result: dict[str, Any] = {
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
        return _error_dict(
            f"Failed to run preflight check: {exc}. Check that the printer is online and KILN_PRINTER_HOST is correct."
        )
    except Exception as exc:
        logger.exception("Unexpected error in preflight_check")
        return _error_dict(f"Unexpected error in preflight_check: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def send_gcode(commands: str, dry_run: bool = False) -> dict:
    """Send raw G-code commands directly to the printer.

    Args:
        commands: One or more G-code commands separated by newlines or spaces.
            Examples: ``"G28"`` (home all axes), ``"G28\\nG1 Z10 F300"``
            (home then move Z up 10mm), ``"M104 S200"`` (set hotend to 200C).
        dry_run: When ``True``, run the full validation pipeline (auth,
            rate-limit, G-code safety) but do **not** actually send commands
            to the printer.  Returns what *would* have been sent.

    The commands are sent sequentially in order.  The printer must be
    connected (unless ``dry_run`` is ``True``).

    G-code is validated before sending.  Commands that exceed temperature
    limits or modify firmware settings are blocked.  Use ``validate_gcode``
    to preview what would be allowed without actually sending.
    """
    if err := _check_auth("print"):
        return err
    if err := _check_rate_limit("send_gcode"):
        return err
    if not dry_run and (conf := _check_confirmation("send_gcode", {"commands": commands})):
        return conf
    try:
        adapter = _get_adapter()

        # Split on newlines and/or whitespace-separated commands, filtering
        # out empty strings.
        raw_lines = re.split(r"[\n\r]+", commands.strip())
        cmd_list: list[str] = []
        for line in raw_lines:
            stripped = line.strip()
            if stripped:
                cmd_list.append(stripped)

        if not cmd_list:
            return _error_dict("No commands provided.", code="INVALID_ARGS")

        # Limit batch size to prevent flooding the printer buffer
        _MAX_GCODE_BATCH = 100
        if len(cmd_list) > _MAX_GCODE_BATCH:
            return _error_dict(
                f"Too many commands ({len(cmd_list)}). Maximum {_MAX_GCODE_BATCH} "
                f"per batch. Split into multiple calls.",
                code="VALIDATION_ERROR",
            )

        # -- Safety validation -------------------------------------------------
        if _PRINTER_MODEL:
            validation = validate_gcode_for_printer(cmd_list, _PRINTER_MODEL)
        else:
            validation = _validate_gcode_impl(cmd_list)
        if not validation.valid:
            _audit(
                "send_gcode",
                "blocked",
                details={
                    "blocked_commands": validation.blocked_commands[:5],
                    "errors": validation.errors[:5],
                },
            )
            _record_tool_block("send_gcode")  # Track for circuit breaker
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

        # -- Dry-run mode: return validated commands without sending ----------
        if dry_run:
            _audit(
                "send_gcode",
                "dry_run",
                details={
                    "count": len(cmd_list),
                },
            )
            result: dict[str, Any] = {
                "success": True,
                "dry_run": True,
                "commands_validated": cmd_list,
                "count": len(cmd_list),
                "message": (
                    f"{len(cmd_list)} command(s) validated successfully. No commands were sent (dry-run mode)."
                ),
            }
            if validation.warnings:
                result["warnings"] = validation.warnings
            return result

        if not adapter.capabilities.can_send_gcode:
            return _error_dict(
                f"send_gcode is not supported by the {adapter.name} adapter.",
                code="UNSUPPORTED",
            )

        adapter.send_gcode(cmd_list)
        _audit("send_gcode", "executed", details={"count": len(cmd_list)})

        result = {
            "success": True,
            "commands_sent": cmd_list,
            "count": len(cmd_list),
            "message": f"Sent {len(cmd_list)} G-code command(s).",
        }
        if validation.warnings:
            result["warnings"] = validation.warnings
        return result

    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to send G-code: {exc}. Check that the printer is online and connected.")
    except Exception as exc:
        logger.exception("Unexpected error in send_gcode")
        return _error_dict(f"Unexpected error in send_gcode: {exc}", code="INTERNAL_ERROR")


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
# Safety audit tool
# ---------------------------------------------------------------------------


@mcp.tool()
def safety_audit(
    action: str | None = None,
    tool_name: str | None = None,
    limit: int = 25,
) -> dict:
    """Query the safety audit log.

    Returns a record of all safety-relevant operations: tool executions,
    blocked attempts, rate-limit violations, and preflight failures.

    Args:
        action: Filter by action type.  Options: ``"executed"``,
            ``"blocked"``, ``"rate_limited"``, ``"auth_denied"``,
            ``"preflight_failed"``, ``"dry_run"``.  Omit for all.
        tool_name: Filter by MCP tool name (e.g. ``"send_gcode"``).
        limit: Maximum number of records to return (default 25, max 100).
    """
    limit = min(max(1, limit), 100)
    try:
        db = get_db()
        entries = db.query_audit(action=action, tool_name=tool_name, limit=limit)
        summary = db.audit_summary()
        return {
            "success": True,
            "entries": entries,
            "summary": summary,
        }
    except Exception as exc:
        logger.exception("Unexpected error in safety_audit")
        return _error_dict(f"Unexpected error in safety_audit: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def get_session_log(
    session_id: str | None = None,
    limit: int = 100,
) -> dict:
    """Return the full audit log for an agent session.

    Every tool call made by an agent is recorded with a session ID — a UUID
    generated when the MCP server starts.  Use this tool to replay exactly
    what an agent issued during a session: every command, every safety check
    that fired, every blocked attempt.

    Args:
        session_id: Session UUID to query.  Omit to use the current session.
        limit: Maximum records to return (default 100, max 500).
    """
    limit = min(max(1, limit), 500)
    sid = session_id or _SESSION_ID
    try:
        db = get_db()
        entries = db.query_audit(session_id=sid, limit=limit)
        return {
            "success": True,
            "session_id": sid,
            "current_session": sid == _SESSION_ID,
            "count": len(entries),
            "entries": entries,
        }
    except Exception as exc:
        logger.exception("Unexpected error in get_session_log")
        return _error_dict(f"Unexpected error in get_session_log: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Confirmation action tool
# ---------------------------------------------------------------------------


@mcp.tool()
def confirm_action(token: str) -> dict:
    """Execute a previously requested action that requires confirmation.

    When ``KILN_CONFIRM_MODE`` is enabled, destructive tools (safety level
    ``"confirm"`` or ``"emergency"``) return a confirmation token instead of
    executing immediately.  Pass that token here to proceed.

    Args:
        token: The confirmation token returned by the original tool call.
    """
    pending = _pending_confirmations.pop(token, None)
    if pending is None:
        return _error_dict(
            "Invalid or expired confirmation token. Tokens expire after 5 minutes.",
            code="INVALID_TOKEN",
        )

    # Check expiry
    age = time.time() - pending["created_at"]
    if age > _CONFIRM_TOKEN_TTL:
        return _error_dict(
            f"Confirmation token expired ({age:.0f}s old, limit is "
            f"{_CONFIRM_TOKEN_TTL:.0f}s). Re-issue the original command.",
            code="TOKEN_EXPIRED",
        )

    tool = pending["tool"]
    args = pending["args"]
    _audit(tool, "confirmed", details={"token": token, "args": args})

    # Temporarily disable confirm mode to avoid recursive confirmation
    global _CONFIRM_MODE
    saved = _CONFIRM_MODE
    _CONFIRM_MODE = False
    try:
        # Dispatch to the actual tool function
        tool_fn = mcp._tool_manager._tools.get(tool)
        if tool_fn is None:
            return _error_dict(f"Unknown tool: {tool}", code="INTERNAL_ERROR")
        result = tool_fn.fn(**args)
        return result
    except Exception as exc:
        logger.exception("Error executing confirmed action %s", tool)
        return _error_dict(f"Error executing {tool}: {exc}", code="INTERNAL_ERROR")
    finally:
        _CONFIRM_MODE = saved


# ---------------------------------------------------------------------------
# Safety dashboard tool
# ---------------------------------------------------------------------------


@mcp.tool()
def safety_status() -> dict:
    """Get a comprehensive snapshot of all active safety measures.

    Returns a single summary showing: the active safety profile, temperature
    limits, rate-limit configuration, recent blocked actions, authentication
    status, and confirmation-mode status.  Use this to answer "is my printer
    safe right now?" in a single call.
    """
    try:
        # Active safety profile
        profile_info: dict[str, Any] = {"printer_model": _PRINTER_MODEL or "not configured"}
        max_tool, max_bed = _get_temp_limits()
        profile_info["max_hotend_temp"] = max_tool
        profile_info["max_bed_temp"] = max_bed
        if _PRINTER_MODEL:
            try:
                profile = get_profile(_PRINTER_MODEL)
                profile_info["profile_id"] = profile.id
                profile_info["display_name"] = profile.display_name
                profile_info["max_feedrate"] = profile.max_feedrate
                if profile.build_volume:
                    profile_info["build_volume"] = profile.build_volume
            except KeyError:
                profile_info["profile_id"] = "default (no specific profile found)"

        # Rate limit configuration
        rate_limits = {}
        for tool_name, (interval_ms, per_min) in _TOOL_RATE_LIMITS.items():
            rate_limits[tool_name] = f"{interval_ms}ms cooldown, {per_min}/min"

        # Confirm-level tools (from tool_safety.json)
        confirm_tools = sorted(
            name for name, meta in _TOOL_SAFETY.items() if meta.get("level") in ("confirm", "emergency")
        )

        # Auth status
        auth_info = {
            "enabled": _auth.enabled if hasattr(_auth, "enabled") else False,
        }

        # Confirm mode
        confirm_mode = os.environ.get("KILN_CONFIRM_MODE", "").lower() in (
            "1",
            "true",
            "yes",
        )

        # Recent blocked actions (from audit log)
        recent_blocked: list[dict[str, Any]] = []
        try:
            db = get_db()
            summary = db.audit_summary(window_seconds=3600.0)
            recent_blocked = summary.get("recent_blocked", [])
        except Exception as exc:
            logger.debug("Failed to fetch audit summary for safety status: %s", exc)

        # G-code blocked command list
        from kiln.gcode import _BLOCKED_COMMANDS  # noqa: E402

        blocked_gcode_commands = sorted(_BLOCKED_COMMANDS.keys())

        return {
            "success": True,
            "safety_profile": profile_info,
            "temperature_limits": {"max_hotend": max_tool, "max_bed": max_bed},
            "rate_limits": rate_limits,
            "confirm_level_tools": confirm_tools,
            "auth": auth_info,
            "confirm_mode_enabled": confirm_mode,
            "blocked_gcode_commands": blocked_gcode_commands,
            "recent_blocked_actions": recent_blocked,
            "summary": (
                f"Safety profile: {profile_info.get('display_name', _PRINTER_MODEL or 'default')}. "
                f"Temp limits: {max_tool}°C hotend / {max_bed}°C bed. "
                f"{len(rate_limits)} rate-limited tools. "
                f"{len(confirm_tools)} confirm-level tools. "
                f"{len(recent_blocked)} blocked action(s) in last hour."
            ),
        }
    except Exception as exc:
        logger.exception("Unexpected error in safety_status")
        return _error_dict(f"Unexpected error in safety_status: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Fleet management tools
# ---------------------------------------------------------------------------


@mcp.tool()
@requires_tier(LicenseTier.PRO)
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
        connected_count = sum(1 for p in status if p.get("connected"))
        disconnected_count = len(status) - connected_count

        state_counts: dict[str, int] = {}
        for p in status:
            state = str(p.get("state", "unknown"))
            state_counts[state] = state_counts.get(state, 0) + 1

        offline_printers = [
            p.get("name", "")
            for p in status
            if (not p.get("connected")) or str(p.get("state", "")).lower() == "offline"
        ]
        busy_states = {"printing", "busy", "starting", "cancelling", "paused"}
        busy_printers = [p.get("name", "") for p in status if str(p.get("state", "")).lower() in busy_states]
        return {
            "success": True,
            "printers": status,
            "count": len(status),
            "idle_printers": idle,
            "connected_count": connected_count,
            "disconnected_count": disconnected_count,
            "state_counts": state_counts,
            "offline_printers": [n for n in offline_printers if n],
            "busy_printers": [n for n in busy_printers if n],
        }
    except Exception as exc:
        logger.exception("Unexpected error in fleet_status")
        return _error_dict(f"Unexpected error in fleet_status: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
@requires_tier(LicenseTier.PRO)
def fleet_analytics() -> dict:
    """Get fleet-wide analytics: per-printer success rates, utilization, and job throughput.

    Returns statistics for every registered printer including total prints,
    success rate, average print duration, and total print hours.  Also
    includes fleet-wide aggregate metrics.

    Requires Kiln Pro or Business license.
    """
    try:
        if _registry.count == 0:
            return {
                "success": True,
                "printers": [],
                "fleet_totals": {"total_prints": 0, "total_hours": 0.0, "avg_success_rate": 0.0},
                "message": "No printers registered.",
            }

        db = get_db()
        printer_stats = []
        total_prints = 0
        total_hours = 0.0
        success_sum = 0.0
        printers_with_data = 0

        for name in _registry.list_names():
            stats = db.get_printer_stats(name)
            printer_stats.append(stats)
            total_prints += stats["total_prints"]
            total_hours += stats["total_print_hours"]
            if stats["total_prints"] > 0:
                success_sum += stats["success_rate"]
                printers_with_data += 1

        avg_success = round(success_sum / printers_with_data, 4) if printers_with_data > 0 else 0.0

        # Queue stats
        queue_counts = _queue.summary()

        return {
            "success": True,
            "printers": printer_stats,
            "fleet_totals": {
                "total_prints": total_prints,
                "total_hours": round(total_hours, 2),
                "avg_success_rate": avg_success,
                "printer_count": _registry.count,
            },
            "queue": queue_counts,
        }
    except Exception as exc:
        logger.exception("Unexpected error in fleet_analytics")
        return _error_dict(f"Unexpected error in fleet_analytics: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def register_printer(
    name: str,
    printer_type: str,
    host: str,
    api_key: str | None = None,
    serial: str | None = None,
    verify_ssl: bool = True,
) -> dict:
    """Register a new printer in the fleet.

    Free tier allows up to 2 printers with independent control.
    Pro tier unlocks unlimited printers with fleet orchestration.

    Args:
        name: Unique human-readable name (e.g. "voron-350", "bambu-x1c").
        printer_type: Backend type -- "octoprint", "moonraker", "bambu",
            "elegoo", "prusaconnect", or "serial".
        host: Base URL or IP address of the printer.  For serial printers,
            this is the port path (e.g. "/dev/ttyUSB0", "COM3").
        api_key: API key (required for OctoPrint and Bambu, optional for
            Moonraker, unused for serial).  For Bambu printers this is the
            LAN Access Code.
        serial: Printer serial number (required for Bambu printers).
        verify_ssl: Whether to verify SSL certificates (default True).
            Set to False for printers using self-signed certificates.
            For Bambu, True maps to TLS pin mode and False maps to
            insecure mode.

    Once registered the printer appears in ``fleet_status()`` and can be
    targeted by ``submit_job()``.
    """
    if err := _check_auth("admin"):
        return err
    try:
        # Free-tier printer cap: allow up to FREE_TIER_MAX_PRINTERS
        # without a Pro license.  Replacing an existing printer doesn't
        # count against the limit.
        current_tier = get_tier()
        if current_tier < LicenseTier.PRO and name not in _registry and _registry.count >= FREE_TIER_MAX_PRINTERS:
            return {
                "success": False,
                "error": (
                    f"Fleet registration is limited to {FREE_TIER_MAX_PRINTERS} printers on the Free tier "
                    f"(you have {_registry.count}). "
                    "Kiln Pro unlocks unlimited printers with fleet orchestration. "
                    "Upgrade at https://kiln3d.com/pro or run 'kiln upgrade'."
                ),
                "code": "FREE_TIER_LIMIT",
                "current_count": _registry.count,
                "max_allowed": FREE_TIER_MAX_PRINTERS,
                "upgrade_url": "https://kiln3d.com/pro",
            }
        # Validate and clean the printer URL
        host, url_warnings = _validate_printer_url(host, printer_type=printer_type)
        if not host:
            return _error_dict(
                "Invalid printer URL: " + "; ".join(url_warnings),
                code="INVALID_ARGS",
            )

        if printer_type == "octoprint":
            if not api_key:
                return _error_dict(
                    "api_key is required for OctoPrint printers.",
                    code="INVALID_ARGS",
                )
            adapter = OctoPrintAdapter(host=host, api_key=api_key, verify_ssl=verify_ssl)
        elif printer_type == "moonraker":
            adapter = MoonrakerAdapter(host=host, api_key=api_key or None, verify_ssl=verify_ssl)
        elif printer_type == "bambu":
            if BambuAdapter is None:
                return _error_dict(
                    "Bambu support requires paho-mqtt.  Install it with: pip install paho-mqtt",
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
            adapter = BambuAdapter(
                host=host,
                access_code=api_key,
                serial=serial,
                tls_mode="pin" if verify_ssl else "insecure",
            )
        elif printer_type == "elegoo":
            if ElegooAdapter is None:
                return _error_dict(
                    "Elegoo SDCP support requires websocket-client.  "
                    "Install it with: pip install websocket-client",
                    code="MISSING_DEPENDENCY",
                )
            adapter = ElegooAdapter(
                host=host,
                mainboard_id=serial or "",
            )
        elif printer_type == "prusaconnect":
            adapter = PrusaConnectAdapter(host=host, api_key=api_key or None)
        elif printer_type == "serial":
            # For serial printers, 'host' is the serial port path (e.g.
            # /dev/ttyUSB0) and 'api_key' is unused.
            baudrate = 115200
            adapter = SerialPrinterAdapter(port=host, baudrate=baudrate)
        else:
            return _error_dict(
                f"Unsupported printer_type: {printer_type!r}. "
                "Supported: 'octoprint', 'moonraker', 'bambu', 'elegoo', 'prusaconnect', 'serial'.",
                code="INVALID_ARGS",
            )

        _registry.register(name, adapter)
        result = {
            "success": True,
            "message": f"Registered printer {name!r} ({printer_type} @ {host}).",
            "name": name,
        }
        if url_warnings:
            result["warnings"] = url_warnings
        return result
    except Exception as exc:
        logger.exception("Unexpected error in register_printer")
        return _error_dict(f"Unexpected error in register_printer: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def discover_printers(timeout: float = 5.0) -> dict:
    """Scan the local network for 3D printers.

    Uses mDNS/Bonjour and HTTP subnet probing to find OctoPrint,
    Moonraker, Bambu Lab, and Elegoo printers on the local network.

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
        return _error_dict(f"Unexpected error in discover_printers: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Queue tools — moved to plugins/queue_tools.py
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Event tools
# ---------------------------------------------------------------------------


@mcp.tool()
def recent_events(limit: int = 20, *, type: str | None = None) -> dict:
    """Get recent events from the Kiln event bus.

    Args:
        limit: Maximum number of events to return (default 20, max 100).
        type: Filter by event type prefix (e.g. ``"print"`` matches
            ``print.started``, ``print.completed``; ``"job"`` matches
            ``job.submitted``, ``job.completed``).  Omit for all events.

    Returns events covering job lifecycle, printer state changes,
    safety warnings, and more.
    """
    try:
        capped = min(max(limit, 1), 100)
        events = _event_bus.recent_events(
            limit=capped,
            event_type_prefix=type,
        )
        return {
            "success": True,
            "events": [e.to_dict() for e in events],
            "count": len(events),
        }
    except Exception as exc:
        logger.exception("Unexpected error in recent_events")
        return _error_dict(f"Unexpected error in recent_events: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Billing tools
# ---------------------------------------------------------------------------


@mcp.tool()
def billing_summary() -> dict:
    """Get a summary of Kiln platform fees for the current month.

    Shows total fees collected, number of outsourced orders, free tier
    usage, and the current fee policy.  Only orders placed through
    external fulfillment services incur fees -- all local printing is free.

    Available on all tiers — anyone who transacts can view their billing.
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
        return _error_dict(f"Unexpected error in billing_summary: {exc}", code="INTERNAL_ERROR")


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
    if err := _check_billing_auth("billing"):
        return err
    try:
        mgr = _get_payment_mgr()
        url = mgr.get_setup_url(rail=rail)
        # Include setup_intent_id so the agent can poll for completion.
        setup_intent_id = None
        provider = mgr.get_provider(rail)
        if provider and hasattr(provider, "_pending_setup_intent_id"):
            setup_intent_id = provider._pending_setup_intent_id
        return {
            "success": True,
            "setup_url": url,
            "rail": rail,
            "setup_intent_id": setup_intent_id,
            "next_step": (
                "Open the setup_url in a browser to complete card setup. "
                "After the user finishes, call billing_check_setup to "
                "activate the payment method."
            ),
        }
    except PaymentError as exc:
        return _error_dict(f"Failed to generate billing setup URL: {exc}", code=getattr(exc, "code", "PAYMENT_ERROR"))
    except Exception as exc:
        logger.exception("Unexpected error in billing_setup_url")
        return _error_dict(f"Unexpected error in billing_setup_url: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def billing_status() -> dict:
    """Get enriched billing status including payment method info.

    Returns payment method details, monthly spend, spend limits,
    available payment rails, and fee policy.  More detailed than
    ``billing_summary`` — includes payment infrastructure state.
    """
    if err := _check_billing_auth("billing"):
        return err
    try:
        from kiln.cli.config import get_or_create_user_id

        user_id = get_or_create_user_id()
        mgr = _get_payment_mgr()
        data = mgr.get_billing_status(user_id)
        return {"success": True, **data}
    except Exception as exc:
        logger.exception("Unexpected error in billing_status")
        return _error_dict(f"Unexpected error in billing_status: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def billing_history(limit: int = 20) -> dict:
    """Get recent billing charge history with payment outcomes.

    Available on all tiers — anyone who transacts can view their history.

    Args:
        limit: Maximum number of records to return (default 20).

    Returns charge records including order cost, fee amount, payment
    rail, payment status, and timestamps.
    """
    if err := _check_billing_auth("billing"):
        return err
    try:
        mgr = _get_payment_mgr()
        charges = mgr.get_billing_history(limit=limit)
        return {"success": True, "charges": charges, "count": len(charges)}
    except Exception as exc:
        logger.exception("Unexpected error in billing_history")
        return _error_dict(f"Unexpected error in billing_history: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def billing_invoice(charge_id: str = "", job_id: str = "") -> dict:
    """Generate an invoice/receipt for a billing charge.

    Args:
        charge_id: The charge ID (from ``billing_history``).
        job_id: Or the job/order ID to look up.

    Returns the invoice as structured data with a human-readable
    receipt and tamper-detection checksum.
    """
    if err := _check_billing_auth("billing"):
        return err
    try:
        from kiln.billing_invoice import generate_invoice

        if charge_id:
            charges = _billing.list_charges(limit=500)
            charge = next((c for c in charges if c.get("id") == charge_id), None)
        elif job_id:
            charges = _billing.list_charges(limit=500)
            charge = next((c for c in charges if c.get("job_id") == job_id), None)
        else:
            return _error_dict(
                "billing_invoice requires either charge_id (from billing_history) or job_id (from fulfillment_order) to look up the charge."
            )

        if charge is None:
            return _error_dict("Charge not found.", code="NOT_FOUND")

        invoice = generate_invoice(charge)
        return {
            "success": True,
            "invoice": invoice.to_dict(),
            "receipt_text": invoice.to_receipt_text(),
        }
    except Exception as exc:
        logger.exception("Error generating invoice")
        return _error_dict(f"Failed to generate invoice: {exc}")


@mcp.tool()
def billing_export(format: str = "csv", limit: int = 100) -> dict:
    """Export billing history for accounting.

    Args:
        format: Export format — ``"csv"`` or ``"json"``.
        limit: Maximum charges to export (default 100).

    Returns billing data suitable for import into accounting
    software (QuickBooks, Xero, etc.).
    """
    if err := _check_billing_auth("billing"):
        return err
    try:
        from kiln.billing_invoice import export_billing_csv, generate_invoices

        charges = _billing.list_charges(limit=limit)

        if format == "csv":
            csv_data = export_billing_csv(charges)
            return {
                "success": True,
                "format": "csv",
                "data": csv_data,
                "count": len(charges),
            }
        else:
            invoices = generate_invoices(charges)
            return {
                "success": True,
                "format": "json",
                "invoices": [inv.to_dict() for inv in invoices],
                "count": len(invoices),
            }
    except Exception as exc:
        logger.exception("Error exporting billing data")
        return _error_dict(f"Failed to export billing data: {exc}")


@mcp.tool()
def check_payment_status(payment_id: str) -> dict:
    """Check the current status of a pending payment by ID.

    Use this after a payment returns ``processing`` status to poll
    for completion.  Works for both Stripe and Circle payments.

    Args:
        payment_id: The payment/transfer ID to check.
    """
    if err := _check_auth("billing"):
        return err
    try:
        mgr = _get_payment_mgr()
        # Try each registered provider until one recognises the ID
        for name in mgr.available_rails:
            provider = mgr.get_provider(name)
            if provider is None:
                continue
            try:
                result = provider.get_payment_status(payment_id)
                return {
                    "success": True,
                    "payment_id": result.payment_id,
                    "status": result.status.value,
                    "amount": result.amount,
                    "currency": result.currency.value,
                    "rail": result.rail.value if result.rail else name,
                    "tx_hash": result.tx_hash,
                    "provider": name,
                }
            except Exception as exc:
                logger.debug("Failed to check payment %s on provider %s: %s", payment_id, name, exc)
                continue
        return _error_dict(
            f"Payment {payment_id!r} not found on any registered provider.",
            code="NOT_FOUND",
        )
    except Exception as exc:
        logger.exception("Unexpected error in check_payment_status")
        return _error_dict(f"Unexpected error in check_payment_status: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def refund_payment(payment_id: str, reason: str = "") -> dict:
    """Request a refund for a completed payment.

    Args:
        payment_id: The payment ID from the original charge
            (found in ``billing_history`` or the ``fulfillment_order`` response).
        reason: Optional reason for the refund (for audit trail).

    Refunds are processed through the original payment rail (Stripe or
    Circle/USDC).  Stripe refunds are typically instant; USDC refunds
    may take a few minutes to confirm on-chain.

    Only completed payments can be refunded.  Authorized holds should
    be released via the fulfillment cancellation flow instead.
    """
    if err := _check_billing_auth("admin"):
        return err
    try:
        mgr = _get_payment_mgr()
        # Try each provider until one recognises the payment_id.
        for provider_name in mgr.available_rails:
            provider = mgr.get_provider(provider_name)
            if provider is None:
                continue
            try:
                result = provider.refund_payment(payment_id)
                # Emit refund event.
                _event_bus.publish(
                    EventType.PAYMENT_REFUNDED,
                    {
                        "payment_id": payment_id,
                        "amount": result.amount,
                        "rail": provider_name,
                        "reason": reason,
                        "status": result.status.value,
                    },
                    source="billing",
                )
                logger.info(
                    "Refund processed: payment=%s amount=%.2f rail=%s reason=%s",
                    payment_id,
                    result.amount,
                    provider_name,
                    reason or "(none)",
                )
                return {
                    "success": True,
                    "refund": result.to_dict(),
                    "message": (
                        f"Refund of ${result.amount:.2f} initiated via {provider_name}. "
                        "Stripe refunds are typically instant. "
                        "USDC refunds may take a few minutes to confirm."
                    ),
                }
            except PaymentError:
                continue  # Not this provider's payment.
            except Exception as exc:
                logger.debug("Failed to refund payment %s on provider %s: %s", payment_id, provider_name, exc)
                continue
        return _error_dict(
            f"Payment {payment_id!r} not found in any registered provider. Verify the payment_id from billing_history.",
            code="PAYMENT_NOT_FOUND",
        )
    except Exception as exc:
        logger.exception("Unexpected error in refund_payment")
        return _error_dict(f"Refund failed: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def billing_check_setup() -> dict:
    """Check if billing setup is complete after user visited the setup URL.

    After calling billing_setup_url and the user completes card setup in
    their browser, call this tool to activate the payment method.  Polls
    the Stripe SetupIntent for completion and configures the payment
    method for future charges.
    """
    try:
        mgr = _get_payment_mgr()
        provider = mgr.get_provider("stripe")
        if provider is None:
            return _error_dict(
                "Stripe provider not configured.",
                code="NO_PROVIDER",
            )
        if not hasattr(provider, "poll_setup_intent"):
            return _error_dict(
                "Provider does not support setup polling.",
                code="UNSUPPORTED",
            )
        pm_id = provider.poll_setup_intent()
        if pm_id is None:
            return {
                "success": False,
                "status": "pending",
                "message": (
                    "Setup not yet complete.  Ask the user to finish "
                    "card setup in their browser, then call this tool again."
                ),
            }
        # Activate the payment method on the provider.
        provider.set_payment_method(pm_id)
        # Persist to config so it survives restarts.
        from kiln.cli.config import save_billing_config

        save_billing_config(
            {
                "stripe_payment_method_id": pm_id,
                "stripe_customer_id": getattr(provider, "_customer_id", None),
            }
        )
        return {
            "success": True,
            "status": "active",
            "payment_method_id": pm_id,
            "message": "Payment method activated. Billing is now enabled.",
        }
    except Exception as exc:
        logger.exception("Unexpected error in billing_check_setup")
        return _error_dict(f"Unexpected error in billing_check_setup: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def billing_alerts() -> dict:
    """Check billing system health and active alerts.

    Returns payment failure alerts, spend limit violations, and
    overall payment system health metrics.
    """
    try:
        alert_mgr = _get_billing_alert_mgr()
        return {
            "success": True,
            "health": alert_mgr.get_health_summary(),
            "alerts": alert_mgr.get_alerts(),
        }
    except Exception as exc:
        logger.exception("Error checking billing alerts")
        return _error_dict(f"Failed to check billing alerts: {exc}")


@mcp.tool()
def billing_delete_data(confirm: str = "") -> dict:
    """Delete all your billing data (GDPR right-to-erasure).

    Args:
        confirm: Must be ``"DELETE"`` to confirm deletion.

    This permanently removes your payment methods and billing
    preferences.  Billing charge records are retained for 7 years
    per tax compliance requirements but can be anonymized on request.

    This action cannot be undone.
    """
    if err := _check_billing_auth("admin"):
        return err
    if confirm != "DELETE":
        return _error_dict(
            "Destructive operation requires confirmation. Call again with confirm='DELETE' to proceed.",
            code="CONFIRMATION_REQUIRED",
        )
    try:
        db = get_db()
        # Use a placeholder user_id since we're single-tenant.
        result = db.delete_user_billing_data("default")
        return {
            "success": True,
            "deleted": result,
            "message": (
                "Payment methods deleted. Billing charge records are "
                "retained for 7 years per tax compliance. Contact "
                "support to request full anonymization."
            ),
        }
    except Exception as exc:
        logger.exception("Error deleting billing data")
        return _error_dict(f"Failed to delete billing data: {exc}")


# ---------------------------------------------------------------------------
# License tools
# ---------------------------------------------------------------------------


@mcp.tool()
def license_status() -> dict:
    """Get the current license tier, validity, and key details.

    Returns the active tier (free/pro/business), whether the license is
    valid, expiration date, and how it was resolved (env/file/default).
    No authentication required.
    """
    try:
        from kiln.licensing import get_license_manager

        info = get_license_manager().get_info()
        return {"success": True, **info.to_dict()}
    except Exception as exc:
        logger.exception("Unexpected error in license_status")
        return _error_dict(f"Unexpected error in license_status: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def activate_license(key: str) -> dict:
    """Activate a Kiln Pro or Business license key.

    Writes the key to ``~/.kiln/license`` and returns the resolved
    tier info.  Use ``license_status`` to check the current tier first.

    Args:
        key: License key string (format: ``kiln_pro_...`` or ``kiln_biz_...``).
    """
    if not key or not key.strip():
        return _error_dict("License key is required.", code="INVALID_INPUT")
    try:
        from kiln.licensing import get_license_manager

        info = get_license_manager().activate_license(key.strip())
        return {"success": True, **info.to_dict()}
    except Exception as exc:
        logger.exception("Unexpected error in activate_license")
        return _error_dict(f"Failed to activate license: {exc}", code="LICENSE_ERROR")


@mcp.tool()
def get_upgrade_url(tier: str = "pro", billing: str = "monthly", email: str = "") -> dict:
    """Get a Stripe Checkout URL to purchase or subscribe to a Kiln license.

    Opens a payment page.  After completing payment, the license key can
    be retrieved with ``kiln upgrade --session <session_id>`` in the CLI.

    Args:
        tier: ``"pro"``, ``"business"``, or ``"enterprise"``.
        billing: ``"monthly"`` or ``"annual"``.
        email: Pre-fill the checkout email field (optional).
    """
    tier_lower = tier.lower().strip()
    billing_lower = billing.lower().strip()

    if tier_lower not in ("pro", "business", "enterprise"):
        return _error_dict(
            f"Invalid tier: {tier!r}. Use 'pro', 'business', or 'enterprise'.",
            code="INVALID_INPUT",
        )
    if billing_lower not in ("monthly", "annual"):
        return _error_dict(
            f"Invalid billing period: {billing!r}. Use 'monthly' or 'annual'.",
            code="INVALID_INPUT",
        )

    # Env var name -> lookup key mapping for each tier+billing combo.
    _PRICE_MAP: dict[tuple[str, str], tuple[str, str]] = {
        ("pro", "monthly"): ("KILN_STRIPE_PRICE_PRO", "pro_monthly"),
        ("pro", "annual"): ("KILN_STRIPE_PRICE_PRO_ANNUAL", "pro_annual"),
        ("business", "monthly"): ("KILN_STRIPE_PRICE_BUSINESS", "business_monthly"),
        ("business", "annual"): ("KILN_STRIPE_PRICE_BUSINESS_ANNUAL", "business_annual"),
        ("enterprise", "monthly"): ("KILN_STRIPE_PRICE_ENTERPRISE", "enterprise_monthly"),
        ("enterprise", "annual"): ("KILN_STRIPE_PRICE_ENTERPRISE_ANNUAL", "enterprise_annual"),
    }

    price_env, lookup_key = _PRICE_MAP[(tier_lower, billing_lower)]

    stripe_key = os.environ.get("KILN_STRIPE_SECRET_KEY", "")
    if not stripe_key:
        return _error_dict("Stripe not configured. Set KILN_STRIPE_SECRET_KEY.", code="CONFIG_MISSING")

    try:
        from kiln.payments.stripe_provider import StripeProvider

        provider = StripeProvider(secret_key=stripe_key)

        # Resolve price: env var first, then lookup key fallback.
        price_id = os.environ.get(price_env, "")
        if not price_id:
            price_id = provider.resolve_price_by_lookup_key(lookup_key) or ""
        if not price_id:
            return _error_dict(
                f"Stripe price not configured. Set {price_env} or add lookup key '{lookup_key}' in Stripe.",
                code="CONFIG_MISSING",
            )

        # Enterprise uses subscription mode (recurring) with optional metered overage.
        # Pro and Business use one-time payment mode.
        if tier_lower == "enterprise":
            overage_price_id = os.environ.get("KILN_STRIPE_PRICE_PRINTER_OVERAGE", "")
            if not overage_price_id:
                overage_price_id = provider.resolve_price_by_lookup_key("enterprise_printer_overage") or ""

            result = provider.create_subscription_session(
                price_id=price_id,
                customer_email=email or None,
                metadata={"tier": tier_lower, "billing": billing_lower},
                metered_price_id=overage_price_id or None,
            )
        else:
            result = provider.create_checkout_session(
                price_id=price_id,
                customer_email=email or None,
                metadata={"tier": tier_lower, "billing": billing_lower},
            )

        return {
            "success": True,
            "checkout_url": result["checkout_url"],
            "session_id": result["session_id"],
            "tier": tier_lower,
            "billing": billing_lower,
            "next_step": (
                "Open checkout_url in a browser to complete payment. "
                "After payment, run 'kiln upgrade --session <session_id>' to activate."
            ),
        }
    except Exception as exc:
        logger.exception("Unexpected error in get_upgrade_url")
        return _error_dict(f"Failed to create checkout: {exc}", code="PAYMENT_ERROR")


# ---------------------------------------------------------------------------
# Tax + donation tools — moved to plugins/consumer_tools.py
# ---------------------------------------------------------------------------


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
        resp = {
            "success": True,
            "query": query,
            "models": [r.to_dict() for r in results.models],
            "count": len(results.models),
            "page": page,
            "sources": _marketplace_registry.connected,
            "searched": results.searched,
            "skipped": results.skipped,
            "failed": results.failed,
            "health_summary": results.summary,
        }
        # Surface deprecation notice when Thingiverse results are included.
        _tv_sources = results.searched or _marketplace_registry.connected
        if "thingiverse" in _tv_sources:
            resp["deprecation_notices"] = {
                "thingiverse": _THINGIVERSE_DEPRECATION_NOTICE,
            }
        return resp
    except MarketplaceError as exc:
        return _error_dict(f"Failed to search models: {exc}. Check marketplace credentials are configured.")
    except Exception as exc:
        logger.exception("Unexpected error in search_all_models")
        return _error_dict(f"Unexpected error in search_all_models: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def health_check() -> dict:
    """Return system health information for monitoring.

    No authentication required.  Useful for container healthchecks,
    dashboards, and verifying the server is responsive.
    """
    import platform

    uptime_s = time.time() - _start_time
    hours = int(uptime_s // 3600)
    minutes = int((uptime_s % 3600) // 60)
    secs = int(uptime_s % 60)

    db_ok = False
    try:
        get_db()._conn.execute("SELECT 1")
        db_ok = True
    except Exception as exc:
        logger.debug("Database health check failed: %s", exc)

    health_data: dict[str, Any] = {
        "success": True,
        "status": "healthy",
        "uptime": f"{hours}h {minutes}m {secs}s",
        "uptime_seconds": round(uptime_s, 1),
        "printers_registered": _registry.count,
        "queue_pending": _queue.pending_count(),
        "queue_active": _queue.active_count(),
        "queue_total": _queue.total_count,
        "scheduler_running": _scheduler.is_running,
        "database_reachable": db_ok,
        "python_version": platform.python_version(),
        "platform": platform.system(),
        "auth_enabled": os.environ.get("KILN_AUTH_ENABLED", "").lower() in ("1", "true", "yes"),
    }

    try:
        alert_mgr = _get_billing_alert_mgr()
        health_data["billing_health"] = alert_mgr.get_health_summary()
    except Exception as exc:
        logger.debug("Failed to get billing health summary: %s", exc)
        health_data["billing_health"] = {"status": "unknown"}

    return health_data


@mcp.tool()
def safety_settings() -> dict:
    """Show current safety and auto-print settings.

    Displays whether auto-print is enabled for marketplace downloads
    and AI-generated models, along with guidance on how to change them.
    Call this early in a session to understand what safety protections
    are active.
    """
    return {
        "success": True,
        "auto_print_marketplace": {
            "enabled": _AUTO_PRINT_MARKETPLACE,
            "env_var": "KILN_AUTO_PRINT_MARKETPLACE",
            "risk_level": "moderate",
            "description": (
                "When enabled, marketplace models are auto-printed after "
                "download+upload. When disabled (default), models are "
                "uploaded but require explicit start_print call."
            ),
        },
        "auto_print_generated": {
            "enabled": _AUTO_PRINT_GENERATED,
            "env_var": "KILN_AUTO_PRINT_GENERATED",
            "risk_level": "high",
            "description": (
                "When enabled, AI-generated models are auto-printed after "
                "generation+validation+slicing+upload. When disabled "
                "(default), models are uploaded but require explicit "
                "start_print call. Higher risk than marketplace models."
            ),
        },
        "recommendations": [
            "Prefer downloading proven community models over generating new ones.",
            "Always validate meshes before printing (validate_generated_mesh).",
            "Review model dimensions against your printer's build volume.",
            "Keep auto-print disabled unless you understand the risks.",
            "AI model generation is experimental — generated geometry may "
            "have thin walls, non-manifold faces, or impossible overhangs.",
        ],
        "how_to_change": (
            "Set environment variables before starting the MCP server:\n"
            "  export KILN_AUTO_PRINT_MARKETPLACE=true   # moderate risk\n"
            "  export KILN_AUTO_PRINT_GENERATED=true     # higher risk\n"
            "Or run 'kiln setup' to configure interactively."
        ),
    }


@mcp.tool()
def get_autonomy_level() -> dict:
    """Return the current autonomy tier and constraints.

    Shows the autonomy level (0 = confirm all, 1 = pre-screened,
    2 = full trust) and any Level 1 constraints that are configured.
    Call this early in a session to understand how much freedom you have.
    """
    from kiln.autonomy import load_autonomy_config

    try:
        cfg = load_autonomy_config()
        return {"success": True, **cfg.to_dict()}
    except Exception as exc:
        logger.exception("Unexpected error in get_autonomy_level")
        return _error_dict(f"Unexpected error in get_autonomy_level: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def set_autonomy_level(level: int) -> dict:
    """Set the autonomy tier (0, 1, or 2).

    Level 0 (Confirm All): Every confirm-level tool requires approval.
    Level 1 (Pre-screened): Confirm-level tools allowed if constraints pass.
    Level 2 (Full Trust): All tools allowed except emergency-level.

    Changing this updates the config file.  Requires human confirmation
    because it affects how much control the agent has.
    """
    from kiln.autonomy import (
        AutonomyConfig,
        AutonomyLevel,
        load_autonomy_config,
        save_autonomy_config,
    )

    try:
        autonomy_level = AutonomyLevel(level)
    except (ValueError, KeyError):
        return _error_dict(
            f"Invalid autonomy level: {level}. Must be 0, 1, or 2.",
            code="VALIDATION_ERROR",
        )

    try:
        existing = load_autonomy_config()
        new_config = AutonomyConfig(level=autonomy_level, constraints=existing.constraints)
        save_autonomy_config(new_config)
        return {
            "success": True,
            "message": f"Autonomy level set to {level} ({autonomy_level.name.lower()})",
            **new_config.to_dict(),
        }
    except Exception as exc:
        logger.exception("Unexpected error in set_autonomy_level")
        return _error_dict(f"Unexpected error in set_autonomy_level: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def check_autonomy(
    tool_name: str,
    safety_level: str,
    material: str = "",
    estimated_time_seconds: int = 0,
    tool_temp: float = 0.0,
    bed_temp: float = 0.0,
) -> dict:
    """Check whether the agent may execute a tool without human confirmation.

    Pass the tool name, its safety level, and optional operation context
    (material, time, temperatures) to get a decision.  Use this before
    calling confirm-level tools to decide whether to proceed or ask.
    """
    from kiln.autonomy import check_autonomy as _check

    ctx: dict[str, Any] = {}
    if material:
        ctx["material"] = material
    if estimated_time_seconds > 0:
        ctx["estimated_time_seconds"] = estimated_time_seconds
    if tool_temp > 0:
        ctx["tool_temp"] = tool_temp
    if bed_temp > 0:
        ctx["bed_temp"] = bed_temp

    try:
        result = _check(tool_name, safety_level, operation_context=ctx or None)
        return {"success": True, **result}
    except Exception as exc:
        logger.exception("Unexpected error in check_autonomy")
        return _error_dict(f"Unexpected error in check_autonomy: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def get_started() -> dict:
    """Quick-start guide for AI agents using Kiln.

    Returns an onboarding summary: what Kiln is, core workflows,
    and the most useful tools to call first.  Call this at the start
    of a session if you're unfamiliar with the available capabilities.
    """
    from kiln.tool_tiers import TIERS

    # Build a concise tier summary
    tier_summary = {name: {"tool_count": len(tools), "examples": tools[:5]} for name, tools in TIERS.items()}

    return {
        "success": True,
        "overview": (
            "Kiln is agent infrastructure for 3D printing. It provides "
            "MCP tools to monitor printers, manage files, slice models, "
            "search marketplaces, queue print jobs, and more."
        ),
        "quick_start": [
            "1. Call `printer_status` to check if a printer is connected and its current state.",
            "2. Call `fleet_status` if managing multiple printers.",
            "3. Call `preflight_check` before starting any print to validate readiness.",
            "4. Use `search_all_models` to find 3D models across marketplaces.",
            "5. Use `slice_model` or `slice_and_print` to prepare and print files.",
            "6. Use `validate_gcode` before `send_gcode` for raw G-code commands.",
        ],
        "core_workflows": {
            "print_a_file": "upload_file → preflight_check → start_print",
            "marketplace_to_print": "search_all_models → download_and_upload → preflight_check → start_print",
            "slice_and_print": "upload_file (STL) → slice_and_print",
            "monitor": "printer_status, printer_snapshot, await_print_completion",
            "queue_jobs": "submit_job → job_status → queue_summary",
        },
        "safety_tools": [
            "preflight_check — validates printer readiness before printing",
            "validate_gcode — checks G-code for dangerous commands before sending",
            "safety_status — comprehensive safety dashboard (limits, rate-limits, blocked actions, auth)",
            "safety_settings — shows current auto-print and confirmation settings",
            "safety_audit — reviews recent safety-relevant actions",
        ],
        "tool_tiers": tier_summary,
        "session_recovery": {
            "description": "If resuming a previous session, call get_agent_context to restore your memory.",
            "tool": "get_agent_context",
            "usage": "Call get_agent_context() at session start to retrieve notes saved in prior sessions.",
        },
        "tip": (
            "Start with `printer_status` to see what's connected, then "
            "explore from there. Use `safety_status` for a full safety "
            "dashboard, or `safety_settings` to check auto-print settings."
        ),
    }


@mcp.tool()
def marketplace_info() -> dict:
    """Show which 3D model marketplaces are connected and available.

    Returns the list of connected marketplace sources and their
    capabilities (search, download support, etc.).  Configure
    marketplaces via environment variables.

    **Safety note:** Community-uploaded models are unverified.  Always
    review model dimensions and preview prints before starting.
    Proven, popular models with high download counts are safer choices
    than untested uploads.
    """
    try:
        if _marketplace_registry.count == 0:
            _init_marketplace_registry()

        sources = []
        for name in _marketplace_registry.connected:
            adapter = _marketplace_registry.get(name)
            sources.append(
                {
                    "name": adapter.name,
                    "display_name": adapter.display_name,
                    "supports_download": adapter.supports_download,
                }
            )

        env_hints = []
        if not _THINGIVERSE_TOKEN:
            env_hints.append("Set KILN_THINGIVERSE_TOKEN to enable Thingiverse")
        if not _MMF_API_KEY:
            env_hints.append("Set KILN_MMF_API_KEY to enable MyMiniFactory")
        if not (_CULTS3D_USERNAME and _CULTS3D_API_KEY):
            env_hints.append("Set KILN_CULTS3D_USERNAME + KILN_CULTS3D_API_KEY to enable Cults3D")

        return {
            "success": True,
            "connected": [s["name"] for s in sources],
            "sources": sources,
            "count": len(sources),
            "setup_hints": env_hints if env_hints else None,
        }
    except Exception as exc:
        logger.exception("Unexpected error in marketplace_info")
        return _error_dict(f"Unexpected error in marketplace_info: {exc}", code="INTERNAL_ERROR")


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
            "deprecation_notice": _THINGIVERSE_DEPRECATION_NOTICE,
        }
    except (ThingiverseError, RuntimeError) as exc:
        return _error_dict(f"Failed to search Thingiverse: {exc}. Check that KILN_THINGIVERSE_TOKEN is set.")
    except Exception as exc:
        logger.exception("Unexpected error in search_models")
        return _error_dict(f"Unexpected error in search_models: {exc}", code="INTERNAL_ERROR")


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
            "deprecation_notice": _THINGIVERSE_DEPRECATION_NOTICE,
        }
    except ThingiverseNotFoundError:
        return _error_dict(f"Model {thing_id} not found.", code="NOT_FOUND")
    except (ThingiverseError, RuntimeError) as exc:
        return _error_dict(f"Failed to get model details: {exc}. Check that KILN_THINGIVERSE_TOKEN is set.")
    except Exception as exc:
        logger.exception("Unexpected error in model_details")
        return _error_dict(f"Unexpected error in model_details: {exc}", code="INTERNAL_ERROR")


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
            "deprecation_notice": _THINGIVERSE_DEPRECATION_NOTICE,
        }
    except ThingiverseNotFoundError:
        return _error_dict(f"Model {thing_id} not found.", code="NOT_FOUND")
    except (ThingiverseError, RuntimeError) as exc:
        return _error_dict(f"Failed to list model files: {exc}. Check that KILN_THINGIVERSE_TOKEN is set.")
    except Exception as exc:
        logger.exception("Unexpected error in model_files")
        return _error_dict(f"Unexpected error in model_files: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def download_model(
    file_id: int | None = None,
    dest_dir: str = os.path.join(tempfile.gettempdir(), "kiln_downloads"),
    file_name: str | None = None,
    model_id: str | None = None,
    source: str = "thingiverse",
    download_all: bool = False,
) -> dict:
    """Download model file(s) from a marketplace to local storage.

    **Community models are unverified.** Always preview dimensions and
    validate the mesh (``validate_generated_mesh``) before printing.
    Models with high download counts and positive ratings are generally
    safer.  AI-generated or untested designs can damage delicate printer
    hardware — prefer proven blueprints when possible.

    Args:
        file_id: Numeric file ID (from ``model_files`` results).  If
            omitted and ``model_id`` is provided, downloads all files
            for the model.
        dest_dir: Local directory to save the file in (default:
            the system temp directory).
        file_name: Override the saved file name (single-file mode only).
            Defaults to the original name from the marketplace.
        model_id: Model/thing ID.  When ``file_id`` is omitted,
            all files for this model are downloaded.
        source: Marketplace source — ``"thingiverse"`` (default),
            ``"myminifactory"``, etc.
        download_all: When True, downloads all files for the model
            regardless of whether ``file_id`` is provided.

    After downloading, validate with ``validate_generated_mesh``, then
    upload to a printer with ``upload_file`` and print with ``start_print``.
    """
    if disk_err := _check_disk_space(dest_dir):
        return disk_err
    try:
        # Multi-file download: model_id provided without file_id, or download_all
        if (file_id is None or download_all) and model_id is not None:
            if _marketplace_registry.count == 0:
                _init_marketplace_registry()

            mkt = _marketplace_registry.get(source)
            if not mkt.supports_download:
                return _error_dict(
                    f"{mkt.display_name} does not support direct downloads.",
                    code="UNSUPPORTED",
                )

            files = mkt.get_files(str(model_id))
            if not files:
                return _error_dict(
                    f"No files found for model {model_id} on {source}.",
                    code="NOT_FOUND",
                )

            downloaded: list[dict] = []
            errors: list[dict] = []
            for mf in files:
                try:
                    path = mkt.download_file(
                        mf.id,
                        dest_dir,
                        file_name=None,
                    )
                    downloaded.append(
                        {
                            "file_id": mf.id,
                            "file_name": mf.name,
                            "local_path": path,
                        }
                    )
                except (MarketplaceError, RuntimeError) as exc:
                    errors.append(
                        {
                            "file_id": mf.id,
                            "file_name": mf.name,
                            "error": str(exc),
                        }
                    )

            dl_resp = {
                "success": len(downloaded) > 0,
                "model_id": model_id,
                "source": source,
                "downloaded": downloaded,
                "errors": errors,
                "total_files": len(files),
                "downloaded_count": len(downloaded),
                "verification_status": "unverified",
                "safety_notice": (
                    "These are community-uploaded models and have NOT been "
                    "verified for print safety or quality. Validate each mesh "
                    "with validate_generated_mesh before printing. Prefer "
                    "proven models with high download counts."
                ),
                "message": (f"Downloaded {len(downloaded)}/{len(files)} files from {source} to {dest_dir}"),
            }
            if source == "thingiverse":
                dl_resp["deprecation_notice"] = _THINGIVERSE_DEPRECATION_NOTICE
            return dl_resp

        # Single-file download (legacy Thingiverse path)
        if file_id is None:
            return _error_dict(
                "Either file_id or model_id must be provided.",
                code="INVALID_INPUT",
            )
        client = _get_thingiverse()
        path = client.download_file(file_id, dest_dir, file_name=file_name)
        return {
            "success": True,
            "file_id": file_id,
            "local_path": path,
            "verification_status": "unverified",
            "safety_notice": (
                "This is a community-uploaded model and has NOT been "
                "verified for print safety or quality. Validate the mesh "
                "with validate_generated_mesh before printing. Prefer "
                "proven models with high download counts."
            ),
            "deprecation_notice": _THINGIVERSE_DEPRECATION_NOTICE,
            "message": f"Downloaded to {path}",
        }
    except (ThingiverseNotFoundError, MktNotFoundError):
        return _error_dict(
            f"File {file_id or model_id} not found on {source}.",
            code="NOT_FOUND",
        )
    except (ThingiverseError, MarketplaceError, RuntimeError) as exc:
        return _error_dict(
            f"Failed to download model: {exc}. Check marketplace credentials and that the model/file ID is correct."
        )
    except Exception as exc:
        logger.exception("Unexpected error in download_model")
        return _error_dict(f"Unexpected error in download_model: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def download_and_upload(
    file_id: str | None = None,
    source: str = "thingiverse",
    printer_name: str | None = None,
    model_id: str | None = None,
) -> dict:
    """Download model file(s) from any marketplace and upload to a printer.

    **Community models are unverified.** This tool downloads and uploads
    but does NOT start printing automatically.  You must call
    ``start_print`` separately after reviewing the uploaded file.
    3D printers are delicate hardware — misconfigured or malformed models
    can cause physical damage.

    When ``file_id`` is provided, downloads and uploads that single file.
    When ``model_id`` is provided without ``file_id``, downloads and
    uploads all printable files (.stl, .gcode, .3mf) for the model.

    Args:
        file_id: File ID (from ``model_files`` results).  For Thingiverse
            this is a numeric ID; for MyMiniFactory it's the file ID string.
            If omitted and ``model_id`` is given, all printable files are
            downloaded and uploaded.
        source: Which marketplace to download from — "thingiverse" (default)
            or "myminifactory".  Cults3D does not support direct downloads.
        printer_name: Target printer name.  Omit to use the default printer.
        model_id: Model/thing ID.  When ``file_id`` is omitted, all
            printable files for this model are downloaded and uploaded.

    After uploading, review the model and call ``start_print`` to begin.
    """
    _dl_dir = os.path.join(tempfile.gettempdir(), "kiln_downloads")
    if err := _check_auth("files"):
        return err
    if disk_err := _check_disk_space(_dl_dir):
        return disk_err
    try:
        if _marketplace_registry.count == 0:
            _init_marketplace_registry()

        # Resolve printer adapter once
        if printer_name:
            adapter = _registry.get(printer_name)
        else:
            adapter = _get_adapter()

        # -----------------------------------------------------------------
        # Multi-file mode: model_id without file_id
        # -----------------------------------------------------------------
        if file_id is None and model_id is not None:
            mkt = _marketplace_registry.get(source)
            if not mkt.supports_download:
                return _error_dict(
                    f"{mkt.display_name} does not support direct downloads.",
                    code="UNSUPPORTED",
                )

            all_files = mkt.get_files(str(model_id))
            if not all_files:
                return _error_dict(
                    f"No files found for model {model_id} on {source}.",
                    code="NOT_FOUND",
                )

            # Filter to printable extensions
            _printable_exts = {"stl", "gcode", "gco", "g", "3mf"}
            printable_files = [
                mf
                for mf in all_files
                if (mf.name.rsplit(".", 1)[-1].lower() if "." in mf.name else "") in _printable_exts
            ]
            if not printable_files:
                return _error_dict(
                    f"No printable files (.stl, .gcode, .3mf) found for model {model_id} on {source}.",
                    code="NOT_FOUND",
                )

            uploaded: list[dict] = []
            errors: list[dict] = []
            for mf in printable_files:
                try:
                    local_path = mkt.download_file(mf.id, _dl_dir)
                    upload_result = adapter.upload_file(local_path)
                    up_name = upload_result.file_name or os.path.basename(local_path)
                    uploaded.append(
                        {
                            "file_id": mf.id,
                            "file_name": up_name,
                            "local_path": local_path,
                            "upload": upload_result.to_dict(),
                        }
                    )
                except (MarketplaceError, PrinterError, RuntimeError) as exc:
                    errors.append(
                        {
                            "file_id": mf.id,
                            "file_name": mf.name,
                            "error": str(exc),
                        }
                    )

            return {
                "success": len(uploaded) > 0,
                "model_id": model_id,
                "source": source,
                "uploaded": uploaded,
                "errors": errors,
                "total_printable_files": len(printable_files),
                "uploaded_count": len(uploaded),
                "verification_status": "unverified",
                "auto_print_enabled": _AUTO_PRINT_MARKETPLACE,
                "safety_notice": (
                    "Models uploaded but NOT started. Community models are "
                    "unverified — review before printing. Call start_print "
                    "to begin printing after review."
                ),
                "message": (
                    f"Downloaded and uploaded {len(uploaded)}/{len(printable_files)} printable files from {source}."
                ),
            }

        # -----------------------------------------------------------------
        # Single-file mode (original behavior)
        # -----------------------------------------------------------------
        if file_id is None:
            return _error_dict(
                "Either file_id or model_id must be provided.",
                code="INVALID_INPUT",
            )

        mkt = _marketplace_registry.get(source) if source != "thingiverse" else None

        # Step 1: Download from marketplace
        if mkt is not None:
            if not mkt.supports_download:
                return _error_dict(
                    f"{mkt.display_name} does not support direct downloads.",
                    code="UNSUPPORTED",
                )
            local_path = mkt.download_file(str(file_id), _dl_dir)
        else:
            # Fallback to legacy Thingiverse client
            client = _get_thingiverse()
            local_path = client.download_file(int(file_id), _dl_dir)

        # Step 2: Upload to printer
        upload_result = adapter.upload_file(local_path)
        file_name = upload_result.file_name or os.path.basename(local_path)

        # Auto-print only if user opted in via KILN_AUTO_PRINT_MARKETPLACE.
        print_data = None
        auto_printed = False
        if _AUTO_PRINT_MARKETPLACE:
            # Mandatory pre-flight safety gate before starting print.
            pf = preflight_check()
            if not pf.get("ready", False):
                _audit(
                    "download_and_upload",
                    "preflight_failed",
                    details={
                        "file": file_name,
                        "summary": pf.get("summary", ""),
                    },
                )
                return _error_dict(
                    pf.get("summary", "Pre-flight checks failed"),
                    code="PREFLIGHT_FAILED",
                )
            print_res = adapter.start_print(file_name)
            _heater_watchdog.notify_print_started()
            print_data = print_res.to_dict()
            auto_printed = True

        resp = {
            "success": True,
            "file_id": str(file_id),
            "source": source,
            "local_path": local_path,
            "upload": upload_result.to_dict(),
            "file_name": file_name,
            "verification_status": "unverified",
            "auto_print_enabled": _AUTO_PRINT_MARKETPLACE,
        }

        if auto_printed:
            resp["print"] = print_data
            resp["safety_notice"] = (
                "WARNING: Auto-print for marketplace models is enabled "
                "(KILN_AUTO_PRINT_MARKETPLACE=true). Community models "
                "are unverified and could cause print failures. "
                "Disable this setting unless you accept the risk."
            )
            resp["message"] = f"Downloaded from {source}, uploaded, and started printing (auto-print ON)."
        else:
            resp["safety_notice"] = (
                "Model uploaded but NOT started. Community models are "
                "unverified — review before printing. Call start_print "
                "to begin printing after review. Set "
                "KILN_AUTO_PRINT_MARKETPLACE=true to enable auto-print."
            )
            resp["message"] = (
                f"Downloaded from {source} and uploaded to printer. Call start_print('{file_name}') to begin printing."
            )

        return resp
    except (ThingiverseNotFoundError, MktNotFoundError):
        return _error_dict(
            f"File {file_id or model_id} not found on {source}.",
            code="NOT_FOUND",
        )
    except PrinterNotFoundError:
        return _error_dict(f"Printer {printer_name!r} not found.", code="NOT_FOUND")
    except (ThingiverseError, MarketplaceError, PrinterError, RuntimeError) as exc:
        return _error_dict(
            f"Failed to download and upload model: {exc}. Check marketplace credentials and printer connection."
        )
    except Exception as exc:
        logger.exception("Unexpected error in download_and_upload")
        return _error_dict(f"Unexpected error in download_and_upload: {exc}", code="INTERNAL_ERROR")


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
                f"Unknown browse_type: {browse_type!r}.  Supported: 'popular', 'newest', 'featured'.",
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
        return _error_dict(f"Failed to browse models: {exc}. Check that KILN_THINGIVERSE_TOKEN is set.")
    except Exception as exc:
        logger.exception("Unexpected error in browse_models")
        return _error_dict(f"Unexpected error in browse_models: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Failed to list categories: {exc}. Check that KILN_THINGIVERSE_TOKEN is set.")
    except Exception as exc:
        logger.exception("Unexpected error in list_model_categories")
        return _error_dict(f"Unexpected error in list_model_categories: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Slicer tools
# ---------------------------------------------------------------------------


def _map_printer_hint_to_profile_id(raw: str | None) -> str | None:
    """Map free-form model hints to bundled slicer profile IDs."""
    if not raw:
        return None
    hint = raw.strip().lower().replace("-", "_").replace(" ", "_")
    if not hint:
        return None
    hint_compact = hint.replace("_", "")

    if (
        hint in {"prusa_mini", "prusamini"}
        or hint_compact.startswith("prusamini")
        or ("prusa" in hint and "mini" in hint)
    ):
        return "prusa_mini"
    if "mk4" in hint:
        return "prusa_mk4"
    if "mk3" in hint:
        return "prusa_mk3s"
    if "prusa_xl" in hint or hint.endswith("_xl") or hint == "xl" or ("prusa" in hint and "xl" in hint):
        return "prusa_xl"
    if "ender3" in hint_compact:
        return "ender3"
    if hint in {"klipper", "moonraker"}:
        return "klipper_generic"
    return None


def _resolve_slice_profile_context(
    profile: str | None,
    printer_id: str | None,
) -> tuple[str | None, str | None]:
    """Resolve effective profile path for slicing."""
    effective_printer_id = _map_printer_hint_to_profile_id(printer_id) or _map_printer_hint_to_profile_id(
        _PRINTER_MODEL
    )
    effective_profile = profile
    if effective_profile is None and effective_printer_id:
        try:
            effective_profile = resolve_slicer_profile(effective_printer_id)
        except Exception as exc:
            logger.debug("Profile resolution failed for %s: %s", effective_printer_id, exc)
    return effective_printer_id, effective_profile


@mcp.tool()
def slice_model(
    input_path: str,
    output_dir: str | None = None,
    profile: str | None = None,
    printer_id: str | None = None,
    slicer_path: str | None = None,
) -> dict:
    """Slice a 3D model (STL/3MF/STEP) to G-code using PrusaSlicer or OrcaSlicer.

    Args:
        input_path: Path to the input file (STL, 3MF, STEP, OBJ, AMF).
        output_dir: Directory for the output G-code.  Defaults to
            the system temp directory.
        profile: Path to a slicer profile/config file (.ini or .json).
        printer_id: Optional printer model ID for bundled profile
            auto-selection (e.g. ``"prusa_mini"``).
        slicer_path: Explicit path to the slicer binary.  Auto-detected
            if omitted.

    Returns a JSON object with the output G-code path.  The output file
    can then be uploaded to a printer with ``upload_file`` and printed
    with ``start_print``.
    """
    try:
        from kiln.slicer import SlicerError, SlicerNotFoundError, slice_file

        effective_printer_id, effective_profile = _resolve_slice_profile_context(
            profile=profile,
            printer_id=printer_id,
        )
        result = slice_file(
            input_path,
            output_dir=output_dir,
            profile=effective_profile,
            slicer_path=slicer_path,
        )
        response: dict[str, Any] = {
            "success": True,
            **result.to_dict(),
        }
        if effective_printer_id:
            response["printer_id"] = effective_printer_id
        if effective_profile:
            response["profile_path"] = effective_profile

        # Cross-check slicer profile against printer safety limits
        if _PRINTER_MODEL and effective_profile:
            # Extract profile_id from the profile path or use printer model
            _profile_id = effective_printer_id or os.path.basename(effective_profile).split("_")[0]
            if _profile_id:
                validation = validate_profile_for_printer(_profile_id, _PRINTER_MODEL)
                if validation["warnings"] or validation["errors"]:
                    response["profile_validation"] = validation
                    if validation["errors"]:
                        response["profile_validation_warning"] = (
                            f"Slicer profile may be incompatible with {_PRINTER_MODEL}: "
                            + "; ".join(validation["errors"])
                        )
                    elif validation["warnings"]:
                        response["profile_validation_warning"] = "Profile compatibility note: " + "; ".join(
                            validation["warnings"]
                        )

        return response
    except SlicerNotFoundError as exc:
        return _error_dict(
            f"Failed to slice model: {exc}. Ensure PrusaSlicer or OrcaSlicer is installed.", code="SLICER_NOT_FOUND"
        )
    except SlicerError as exc:
        return _error_dict(f"Failed to slice model: {exc}", code="SLICER_ERROR")
    except FileNotFoundError as exc:
        return _error_dict(f"Failed to slice model: {exc}", code="FILE_NOT_FOUND")
    except Exception as exc:
        logger.exception("Unexpected error in slice_model")
        return _error_dict(f"Unexpected error in slice_model: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(
            f"Failed to find slicer: {exc}. Ensure PrusaSlicer or OrcaSlicer is installed.", code="SLICER_NOT_FOUND"
        )
    except Exception as exc:
        logger.exception("Unexpected error in find_slicer_tool")
        return _error_dict(f"Unexpected error in find_slicer_tool: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def slice_and_print(
    input_path: str,
    printer_name: str | None = None,
    profile: str | None = None,
    printer_id: str | None = None,
) -> dict:
    """Slice a 3D model and immediately upload + print it in one step.

    Args:
        input_path: Path to the 3D model file (STL, 3MF, STEP, etc.).
        printer_name: Target printer name.  Omit for the default printer.
        profile: Path to a slicer profile/config file.
        printer_id: Optional printer model ID for bundled profile
            auto-selection (e.g. ``"prusa_mini"``).

    Combines ``slice_model``, ``upload_file``, and ``start_print`` into
    a single action.
    """
    if err := _check_auth("print"):
        return err
    try:
        from kiln.slicer import SlicerError, SlicerNotFoundError, slice_file

        effective_printer_id, effective_profile = _resolve_slice_profile_context(
            profile=profile,
            printer_id=printer_id,
        )
        result = slice_file(
            input_path,
            profile=effective_profile,
        )

        if printer_name:
            adapter = _registry.get(printer_name)
        else:
            adapter = _get_adapter()

        upload = adapter.upload_file(result.output_path)
        file_name = upload.file_name or os.path.basename(result.output_path)

        # Mandatory pre-flight safety gate before starting print.
        pf = preflight_check()
        if not pf.get("ready", False):
            _audit(
                "slice_and_print",
                "preflight_failed",
                details={
                    "file": file_name,
                    "summary": pf.get("summary", ""),
                },
            )
            return _error_dict(
                pf.get("summary", "Pre-flight checks failed"),
                code="PREFLIGHT_FAILED",
            )

        print_result = adapter.start_print(file_name)
        _heater_watchdog.notify_print_started()

        return {
            "success": True,
            "slice": result.to_dict(),
            "upload": upload.to_dict(),
            "print": print_result.to_dict(),
            "printer_id": effective_printer_id,
            "profile_path": effective_profile,
            "message": f"Sliced, uploaded, and started printing {os.path.basename(input_path)}.",
        }
    except SlicerNotFoundError as exc:
        return _error_dict(
            f"Failed to slice and print: {exc}. Ensure PrusaSlicer or OrcaSlicer is installed.", code="SLICER_NOT_FOUND"
        )
    except SlicerError as exc:
        return _error_dict(f"Failed to slice and print: {exc}", code="SLICER_ERROR")
    except PrinterNotFoundError:
        return _error_dict(f"Printer {printer_name!r} not found.", code="NOT_FOUND")
    except (PrinterError, RuntimeError, FileNotFoundError) as exc:
        return _error_dict(f"Failed to slice and print: {exc}. Check the input file and printer connection.")
    except Exception as exc:
        logger.exception("Unexpected error in slice_and_print")
        return _error_dict(f"Unexpected error in slice_and_print: {exc}", code="INTERNAL_ERROR")


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

    Supports OctoPrint, Moonraker, and Bambu (via RTSP/ffmpeg) webcams.
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

        result: dict[str, Any] = {
            "success": True,
            "size_bytes": len(image_data),
        }

        if save_path:
            _safe = os.path.realpath(save_path)
            _home = os.path.expanduser("~")
            _tmpdir = os.path.realpath(tempfile.gettempdir())
            _allowed = (_home, _tmpdir)
            if not any(_safe.startswith(p) for p in _allowed):
                return _error_dict(
                    "save_path must be under home directory or a temp directory.",
                    code="VALIDATION_ERROR",
                )
            os.makedirs(os.path.dirname(_safe) or ".", exist_ok=True)
            with open(_safe, "wb") as f:
                f.write(image_data)
            result["saved_to"] = _safe
            result["message"] = f"Snapshot saved to {_safe}"
        else:
            import base64

            result["image_base64"] = base64.b64encode(image_data).decode("ascii")
            result["message"] = "Snapshot captured (base64 encoded)"

        return result

    except PrinterNotFoundError:
        return _error_dict(f"Printer {printer_name!r} not found.", code="NOT_FOUND")
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to capture snapshot: {exc}. Check that the printer has a webcam configured.")
    except Exception as exc:
        logger.exception("Unexpected error in printer_snapshot")
        return _error_dict(f"Unexpected error in printer_snapshot: {exc}", code="INTERNAL_ERROR")


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
            file_path,
            material=material,
            electricity_rate=electricity_rate,
            printer_wattage=printer_wattage,
        )
        return {"success": True, "estimate": estimate.to_dict()}
    except FileNotFoundError as exc:
        return _error_dict(f"Failed to estimate cost: {exc}", code="FILE_NOT_FOUND")
    except Exception as exc:
        logger.exception("Unexpected error in estimate_cost")
        return _error_dict(f"Unexpected error in estimate_cost: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Unexpected error in set_material: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Unexpected error in get_material: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Unexpected error in check_material_match: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Unexpected error in list_spools: {exc}", code="INTERNAL_ERROR")


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
            material_type=material,
            color=color,
            brand=brand,
            weight_grams=weight_grams,
            cost_usd=cost_usd,
        )
        return {"success": True, "spool": spool.to_dict()}
    except Exception as exc:
        logger.exception("Unexpected error in add_spool")
        return _error_dict(f"Unexpected error in add_spool: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Unexpected error in remove_spool: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Unexpected error in bed_level_status: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Failed to trigger bed leveling: {exc}. Check that the printer is online and idle.")
    except Exception as exc:
        logger.exception("Unexpected error in trigger_bed_level")
        return _error_dict(f"Unexpected error in trigger_bed_level: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Unexpected error in set_leveling_policy: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Failed to manage webcam stream: {exc}. Check that the printer has a webcam configured.")
    except Exception as exc:
        logger.exception("Unexpected error in webcam_stream")
        return _error_dict(f"Unexpected error in webcam_stream: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Unexpected error in cloud_sync_now: {exc}", code="INTERNAL_ERROR")


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
            cloud_url=cloud_url,
            api_key=api_key,
            sync_interval_seconds=interval,
        )
        if _cloud_sync is not None:
            _cloud_sync.stop()
        _cloud_sync = CloudSyncManager(
            db=get_db(),
            event_bus=_event_bus,
            config=config,
        )
        _cloud_sync.start()
        return {"success": True, "config": config.to_dict()}
    except Exception as exc:
        logger.exception("Unexpected error in cloud_sync_configure")
        return _error_dict(f"Unexpected error in cloud_sync_configure: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Failed to list fulfillment materials: {exc}. Check that KILN_CRAFTCLOUD_API_KEY is set.")
    except Exception as exc:
        logger.exception("Unexpected error in fulfillment_materials")
        return _error_dict(f"Unexpected error in fulfillment_materials: {exc}", code="INTERNAL_ERROR")


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
        quote = provider.get_quote(
            QuoteRequest(
                file_path=file_path,
                material_id=material_id,
                quantity=quantity,
                shipping_country=shipping_country,
            )
        )
        fee_calc = _billing.calculate_fee(
            quote.total_price,
            currency=quote.currency,
        )
        quote_data = quote.to_dict()
        quote_data["kiln_fee"] = fee_calc.to_dict()
        quote_data["total_with_fee"] = fee_calc.total_cost

        # Try to authorize (hold) the fee at quote time.
        try:
            mgr = _get_payment_mgr()
            if mgr.available_rails:
                auth_result = mgr.authorize_fee(
                    quote.quote_id,
                    fee_calc,
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
        return _error_dict(f"Failed to get fulfillment quote: {exc}", code="FILE_NOT_FOUND")
    except (FulfillmentError, RuntimeError) as exc:
        return _error_dict(f"Failed to get fulfillment quote: {exc}. Check that KILN_CRAFTCLOUD_API_KEY is set.")
    except Exception as exc:
        logger.exception("Unexpected error in fulfillment_quote")
        return _error_dict(f"Unexpected error in fulfillment_quote: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
@requires_tier(LicenseTier.BUSINESS)
def fulfillment_order(
    quote_id: str,
    shipping_option_id: str = "",
    payment_hold_id: str = "",
    quoted_price: float = 0.0,
    quoted_currency: str = "USD",
    jurisdiction: str = "",
    business_tax_id: str = "",
) -> dict:
    """Place a manufacturing order based on a previous quote.

    Charges the platform fee BEFORE placing the order to prevent
    unpaid orders.  If order placement fails after payment, the
    charge is automatically refunded.

    Args:
        quote_id: Quote ID from ``fulfillment_quote``.
        shipping_option_id: Shipping option ID from the quote's
            ``shipping_options`` list.
        payment_hold_id: PaymentIntent ID from the quote's
            ``payment_hold`` field.  If provided, the previously
            authorized hold is captured before placing the order.
            This is the preferred payment flow.
        quoted_price: Total price returned by ``fulfillment_quote``
            (used to calculate the fee when no ``payment_hold_id``
            is provided).  Required when ``payment_hold_id`` is
            empty and a payment rail is configured.
        quoted_currency: Currency of ``quoted_price`` (default USD).
        jurisdiction: Buyer's region (e.g. ``"US-CA"``, ``"DE"``, ``"AU"``).
            When provided, the response includes an accurate total with
            tax so the user sees exactly what they'll pay — no hidden
            fees.  Use ``tax_jurisdictions`` to see all supported codes.
        business_tax_id: If the buyer is a registered business, their
            tax ID (EU VAT number, AU ABN, etc.).  Businesses in the
            EU, UK, Australia, and Japan are tax-exempt via reverse
            charge — the tax line shows $0.00.

    Use ``fulfillment_order_status`` to track progress after placing.
    """
    if err := _check_billing_auth("print"):
        return err
    try:
        provider = _get_fulfillment()

        # 0. Validate quote is still valid (exists, not expired, provider up).
        quote_validation: QuoteValidation | None = None
        try:
            quote_validation = _validate_quote_for_order(
                quote_id,
                provider_name=provider.name,
            )
        except FulfillmentError as exc:
            return _error_dict(
                f"Quote validation failed: {exc}",
                code=getattr(exc, "code", None) or "QUOTE_INVALID",
            )

        # 1. Determine the price and calculate the fee BEFORE placing.
        estimated_price = quoted_price
        currency = quoted_currency
        pay_result = None
        fee_calc = None

        # 1a. Early spend limit check (before any work).
        if estimated_price and estimated_price > 0:
            fee_estimate = _billing.calculate_fee(
                estimated_price,
                currency=currency,
                jurisdiction=jurisdiction or None,
                business_tax_id=business_tax_id or None,
            )
            if not fee_estimate.waived and fee_estimate.fee_amount > 0:
                mgr = _get_payment_mgr()
                ok, reason = mgr.check_spend_limits(fee_estimate.fee_amount)
                if not ok:
                    return _error_dict(
                        f"Order would exceed spend limits: {reason}. "
                        "Adjust limits in billing settings before placing this order.",
                        code="SPEND_LIMIT",
                    )

        # 2. Charge / capture payment BEFORE placing the order.
        if payment_hold_id or estimated_price > 0:
            if estimated_price > 0:
                fee_calc = _billing.calculate_fee(
                    estimated_price,
                    currency=currency,
                    jurisdiction=jurisdiction or None,
                    business_tax_id=business_tax_id or None,
                )

            try:
                mgr = _get_payment_mgr()
                if mgr.available_rails:
                    if payment_hold_id:
                        # Capture the hold placed at quote time.
                        if fee_calc is None:
                            # Hold exists but no price given — capture
                            # will use the amount from the original auth.
                            fee_calc = _billing.calculate_fee(0.0)
                        pay_result = mgr.capture_fee(
                            payment_hold_id,
                            quote_id,
                            fee_calc,
                        )
                    elif fee_calc:
                        # No hold — one-shot charge before order.
                        pay_result = mgr.charge_fee(quote_id, fee_calc)
                    else:
                        return _error_dict(
                            "Cannot place order: no payment hold and no "
                            "quoted_price provided.  Re-run fulfillment_quote "
                            "to get pricing, then pass payment_hold_id or "
                            "quoted_price.",
                            code="MISSING_PRICE",
                        )
                else:
                    # No payment rails configured — atomically calculate
                    # and record the fee to prevent free-tier race
                    # conditions.
                    if estimated_price > 0:
                        fee_calc, _charge_id = _billing.calculate_and_record_fee(
                            quote_id,
                            estimated_price,
                            currency=currency,
                            jurisdiction=jurisdiction or None,
                            business_tax_id=business_tax_id or None,
                        )
            except PaymentError as pe:
                # Payment failed — do NOT place the order.
                return _error_dict(
                    f"Payment failed: {pe}. Order was NOT placed. Please update your payment method and try again.",
                    code="PAYMENT_ERROR",
                )

        # 3. Place the order AFTER payment succeeds.
        try:
            result = provider.place_order(
                OrderRequest(
                    quote_id=quote_id,
                    shipping_option_id=shipping_option_id,
                )
            )
        except (FulfillmentError, RuntimeError) as exc:
            # Order failed — refund the payment automatically.
            refund_warning = _refund_after_order_failure(
                pay_result,
                payment_hold_id,
            )
            msg = f"Order placement failed: {exc}. "
            if refund_warning:
                msg += refund_warning
            else:
                msg += "Your payment has been refunded automatically."
            return _error_dict(msg)

        # 4. Build response.
        order_data = result.to_dict()
        if fee_calc:
            order_data["kiln_fee"] = fee_calc.to_dict()
            order_data["total_with_fee"] = fee_calc.total_cost
        if pay_result:
            order_data["payment"] = pay_result.to_dict()

            # Re-link the charge to the real order_id if it differs
            # from the quote_id we used for the initial charge.
            if result.order_id and result.order_id != quote_id:
                try:
                    _billing.record_charge(
                        result.order_id,
                        fee_calc,
                        payment_id=pay_result.payment_id,
                        payment_rail=pay_result.rail.value,
                        payment_status=pay_result.status.value,
                    )
                except Exception:
                    logger.debug(
                        "Could not link charge to order %s",
                        result.order_id,
                    )

        # 5. Price-drift check: warn or block if actual order price
        #    diverges from the original quoted price.
        response_warnings: list[str] = []
        if quote_validation and quote_validation.warnings:
            response_warnings.extend(quote_validation.warnings)

        if result.total_price is not None and quote_validation:
            drift_warning, should_block = _check_price_drift(
                quote_validation.quoted_price,
                result.total_price,
            )
            if should_block:
                logger.error(
                    "Price drift BLOCKED order for quote %s: %s",
                    quote_id,
                    drift_warning,
                )
                # Refund the payment since the order went through at a
                # price the user did not agree to.
                refund_warning = _refund_after_order_failure(
                    pay_result,
                    payment_hold_id,
                )
                msg = drift_warning or "Price drift exceeded safety limit."
                if refund_warning:
                    msg += f" {refund_warning}"
                else:
                    msg += " Your payment has been refunded automatically."
                return _error_dict(msg, code="PRICE_DRIFT_BLOCKED")
            if drift_warning:
                logger.warning(
                    "Price drift detected for quote %s: %s",
                    quote_id,
                    drift_warning,
                )
                response_warnings.append(drift_warning)

        if response_warnings:
            order_data["warnings"] = response_warnings

        return {
            "success": True,
            "order": order_data,
        }
    except Exception as exc:
        logger.exception("Unexpected error in fulfillment_order")
        return _error_dict(f"Unexpected error in fulfillment_order: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Failed to check order status: {exc}. Verify the order_id is correct.")
    except Exception as exc:
        logger.exception("Unexpected error in fulfillment_order_status")
        return _error_dict(f"Unexpected error in fulfillment_order_status: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
@requires_tier(LicenseTier.BUSINESS)
def fulfillment_cancel(order_id: str) -> dict:
    """Cancel a fulfillment order (if still cancellable).

    Args:
        order_id: Order ID to cancel.

    Only orders that have not yet shipped can be cancelled.
    """
    if err := _check_billing_auth("print"):
        return err
    try:
        provider = _get_fulfillment()
        result = provider.cancel_order(order_id)
        return {
            "success": True,
            "order": result.to_dict(),
        }
    except (FulfillmentError, RuntimeError) as exc:
        return _error_dict(f"Failed to cancel order: {exc}. The order may have already shipped.")
    except Exception as exc:
        logger.exception("Unexpected error in fulfillment_cancel")
        return _error_dict(f"Unexpected error in fulfillment_cancel: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def fulfillment_alerts() -> dict:
    """Check for fulfillment order alerts (stalled, failed, cancelled orders).

    Returns any active alerts from the background fulfillment monitor.
    Alerts are generated when orders are cancelled/failed by the provider
    or have been stuck in processing longer than the expected lead time.
    """
    try:
        monitor = _get_fulfillment_monitor()
        alerts = monitor.get_alerts()
        return {"success": True, "alerts": alerts, "count": len(alerts)}
    except Exception as exc:
        return _error_dict(f"Failed to check fulfillment alerts: {exc}")


# ---------------------------------------------------------------------------
# Consumer workflow tools — moved to plugins/consumer_tools.py
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 3DOS Network tools — moved to plugins/network_tools.py
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_GCODE_EXTENSIONS = {".gcode", ".gco", ".g"}
_MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB


def _validate_local_file(file_path: str) -> dict[str, Any]:
    """Validate a local G-code file without depending on octoprint_cli.

    Returns a dict with ``valid`` (bool), ``errors``, ``warnings``, and
    ``info`` keys.
    """
    errors: list[str] = []
    warnings: list[str] = []
    info: dict[str, Any] = {"size_bytes": 0, "extension": ""}

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
        errors.append(f"Unsupported file extension '{ext}'. Expected one of: {', '.join(sorted(_GCODE_EXTENSIONS))}")

    try:
        size = path.stat().st_size
    except OSError as exc:
        errors.append(f"Could not determine file size: {exc}")
        return {"valid": False, "errors": errors, "warnings": warnings, "info": info}

    info["size_bytes"] = size

    if size == 0:
        errors.append("File is empty (0 bytes)")
    elif size >= _MAX_FILE_SIZE:
        errors.append(f"File is too large ({size} bytes). Maximum allowed size is {_MAX_FILE_SIZE} bytes.")
    elif size >= 500 * 1024 * 1024:
        warnings.append(f"File is very large ({size} bytes). Upload may take a while.")

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
        import kiln.printers.bambu  # noqa: F401 -- availability check only

        modules["bambu_available"] = True
    except ImportError:
        modules["bambu_available"] = False

    return {
        "success": True,
        "version": kiln.__version__,
        "uptime_seconds": int(uptime_secs),
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
        return _error_dict(f"Unexpected error in register_webhook: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Unexpected error in list_webhooks: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Unexpected error in delete_webhook: {exc}", code="INTERNAL_ERROR")


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
                    return _error_dict(f"Job {job_id!r} not found.", code="JOB_NOT_FOUND")

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
                progress_log.append(
                    {
                        "time": round(elapsed, 1),
                        "completion": pct,
                    }
                )
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
            return _error_dict(f"Failed to poll print status: {exc}. Check that the printer is online.")
        except Exception as exc:
            logger.exception("Unexpected error in await_print_completion")
            return _error_dict(f"Unexpected error in await_print_completion: {exc}", code="INTERNAL_ERROR")

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
    result: dict[str, Any] = {"success": True}

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
            quote = provider.get_quote(
                QuoteRequest(
                    file_path=file_path,
                    material_id=fulfillment_material_id,
                    quantity=quantity,
                    shipping_country=shipping_country,
                )
            )
            fee_calc = _billing.calculate_fee(
                quote.total_price,
                currency=quote.currency,
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


@mcp.tool()
def analyze_print_failure(job_id: str) -> dict:
    """Analyze a failed print job and suggest possible causes and fixes.

    Examines the job record, related events (retries, errors, progress),
    and printer state at the time of failure to produce a diagnosis.

    Args:
        job_id: The failed job's ID from ``job_history`` or ``job_status``.

    Returns a structured analysis with likely causes, observed symptoms,
    and recommended next steps.
    """
    try:
        try:
            job = _queue.get_job(job_id)
        except JobNotFoundError:
            return _error_dict(f"Job {job_id!r} not found.", code="JOB_NOT_FOUND")

        job_data = job.to_dict()

        if job.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
            return _error_dict(
                f"Job {job_id} is not in a failed state (status: {job.status.value}). "
                "Only failed or cancelled jobs can be analyzed.",
                code="NOT_FAILED",
            )

        # Gather related events for this job
        all_events = _event_bus.recent_events(limit=200)
        job_events = [e.to_dict() for e in all_events if e.data.get("job_id") == job_id]

        # Analyze symptoms
        symptoms: list[str] = []
        causes: list[str] = []
        recommendations: list[str] = []

        # Check for retries
        retry_events = [e for e in job_events if e.get("data", {}).get("retry")]
        if retry_events:
            max_retry = max(e["data"]["retry"] for e in retry_events)
            symptoms.append(f"Job was retried {max_retry} time(s) before final failure")
            causes.append("Persistent printer or communication error across multiple attempts")
            recommendations.append("Check printer connectivity and physical state before resubmitting")

        # Check error message
        error = job.error or ""
        if "error state" in error.lower():
            symptoms.append("Printer entered error state during print")
            causes.append("Hardware error (thermal runaway, endstop triggered, motor stall)")
            recommendations.append("Check printer display for specific error code")
            recommendations.append("Inspect nozzle for clogs or filament issues")
        elif "not registered" in error.lower() or "not found" in error.lower():
            symptoms.append("Printer was removed or became unreachable mid-print")
            causes.append("Network connectivity loss or printer power cycle")
            recommendations.append("Verify printer is powered on and network-accessible")
        elif "start_print" in error.lower():
            symptoms.append("Failed to start the print")
            causes.append("File may not exist on printer, or printer was not in an idle state")
            recommendations.append("Verify the file exists with printer_files() before retrying")
            recommendations.append("Check printer_status() to confirm idle state")
        elif error:
            symptoms.append(f"Error message: {error}")

        # Check timing
        if job.elapsed_seconds is not None and job.elapsed_seconds < 30:
            symptoms.append(f"Print failed very quickly ({job.elapsed_seconds:.0f}s)")
            causes.append("Likely a setup issue rather than a mid-print failure")
            recommendations.append("Run preflight_check() to validate printer readiness")

        if job.elapsed_seconds is not None and job.elapsed_seconds > 3600:
            symptoms.append(f"Print ran for {job.elapsed_seconds / 3600:.1f}h before failing")
            causes.append("May be a mid-print adhesion, filament, or thermal issue")
            recommendations.append("Check bed adhesion and first-layer settings")
            recommendations.append("Inspect filament spool for tangles or moisture")

        # Check progress events
        progress_events = [e for e in job_events if e.get("type") == EventType.PRINT_PROGRESS.value]
        if progress_events:
            max_pct = max(e.get("data", {}).get("completion", 0) for e in progress_events)
            symptoms.append(f"Reached {max_pct:.0f}% completion before failure")
            if max_pct < 5:
                causes.append("First-layer adhesion failure or nozzle clog")
                recommendations.append("Clean the bed surface and re-level")
            elif max_pct > 80:
                causes.append("Late-print failure — possibly cooling or overhang issue")
                recommendations.append("Review slicer support settings for the model")

        # Default if no specific analysis
        if not symptoms:
            symptoms.append("No detailed event data available for this job")
            recommendations.append("Re-run the print with monitoring via printer_status()")

        return {
            "success": True,
            "job": job_data,
            "analysis": {
                "symptoms": symptoms,
                "likely_causes": causes,
                "recommendations": recommendations,
                "retry_count": len(retry_events),
                "related_events": job_events[-20:],
            },
        }
    except Exception as exc:
        logger.exception("Unexpected error in analyze_print_failure")
        return _error_dict(f"Unexpected error in analyze_print_failure: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def validate_print_quality(
    job_id: str | None = None,
    printer_name: str | None = None,
    save_snapshot: str | None = None,
) -> dict:
    """Validate print quality after a completed print job.

    Captures a webcam snapshot (if available), examines the job record and
    events, and produces a quality assessment with recommendations.

    Args:
        job_id: The completed job's ID.  If omitted, uses the most recent
            completed job.
        printer_name: Target printer name (omit for default printer).
        save_snapshot: Optional file path to save the post-print snapshot.

    Returns a quality report with snapshot data, job metrics, and any
    detected issues.
    """
    try:
        import base64

        # Resolve the job
        target_job = None
        if job_id:
            try:
                target_job = _queue.get_job(job_id)
            except JobNotFoundError:
                return _error_dict(f"Job {job_id!r} not found.", code="JOB_NOT_FOUND")
        else:
            # Find most recent completed job
            recent = _queue.list_jobs(limit=20)
            for j in recent:
                if j.status == JobStatus.COMPLETED:
                    target_job = j
                    break
            if target_job is None:
                return _error_dict(
                    "No completed jobs found. Provide a job_id explicitly.",
                    code="NO_COMPLETED_JOB",
                )

        job_data = target_job.to_dict()

        # Gather adapter for snapshot
        if printer_name:
            adapter = _registry.get(printer_name)
        else:
            try:
                adapter = _get_adapter()
            except RuntimeError:
                adapter = None

        # Capture snapshot
        snapshot_info: dict[str, Any] = {"available": False}
        if adapter is not None:
            try:
                image_data = adapter.get_snapshot()
                if image_data is not None:
                    snapshot_info = {
                        "available": True,
                        "size_bytes": len(image_data),
                    }
                    if save_snapshot:
                        # Sanitise path — restrict to home dir or temp dir
                        _safe = os.path.abspath(save_snapshot)
                        _home = os.path.expanduser("~")
                        _tmpdir = os.path.realpath(tempfile.gettempdir())
                        if not (_safe.startswith(_home) or _safe.startswith(_tmpdir)):
                            return _error_dict(
                                "save_snapshot path must be under home directory or temp directory.",
                                code="VALIDATION_ERROR",
                            )
                        os.makedirs(os.path.dirname(_safe) or ".", exist_ok=True)
                        with open(_safe, "wb") as f:
                            f.write(image_data)
                        snapshot_info["saved_to"] = _safe
                    else:
                        snapshot_info["image_base64"] = base64.b64encode(image_data).decode("ascii")
            except Exception as snap_exc:
                snapshot_info = {"available": False, "error": str(snap_exc)}

        # Gather related events
        all_events = _event_bus.recent_events(limit=200)
        job_events = [e.to_dict() for e in all_events if e.data.get("job_id") == target_job.id]

        # Analyse quality indicators
        issues: list[str] = []
        metrics: dict[str, Any] = {}
        recommendations: list[str] = []

        # Duration analysis
        if target_job.elapsed_seconds is not None:
            metrics["print_duration_seconds"] = target_job.elapsed_seconds
            metrics["print_duration_hours"] = round(target_job.elapsed_seconds / 3600, 2)

        # Check for retries (may indicate intermittent problems)
        retry_events = [e for e in job_events if e.get("data", {}).get("retry")]
        if retry_events:
            issues.append(f"Job required {len(retry_events)} retry attempt(s) before completing")
            recommendations.append(
                "Retries during a print may indicate connectivity or mechanical issues. "
                "Inspect the print closely for layer shifts or gaps."
            )

        # Check progress consistency
        progress_events = [e for e in job_events if e.get("type") in ("print.progress", "job.progress")]
        if progress_events:
            completions = [e.get("data", {}).get("completion", 0) for e in progress_events]
            # Detect non-monotonic progress (resets may indicate issues)
            for i in range(1, len(completions)):
                if completions[i] < completions[i - 1] - 5:
                    issues.append(
                        f"Progress dropped from {completions[i - 1]:.0f}% to "
                        f"{completions[i]:.0f}% — possible restart or error recovery"
                    )
                    break

        # Snapshot-based hints (we can't do actual vision analysis here,
        # but we can note the snapshot is available for the agent to inspect)
        if snapshot_info.get("available"):
            recommendations.append(
                "A post-print snapshot was captured. Visually inspect it for: "
                "stringing, layer shifts, warping, incomplete layers, or "
                "spaghetti-like extrusion failures."
            )
        else:
            recommendations.append(
                "No webcam available for visual inspection. Consider adding a camera for automated quality checks."
            )

        # Overall quality grade
        if not issues:
            grade = "PASS"
            summary = "Print completed successfully with no detected issues."
        elif len(issues) <= 2:
            grade = "WARNING"
            summary = "Print completed but with potential quality concerns."
        else:
            grade = "REVIEW"
            summary = "Print completed with multiple issues detected. Manual inspection recommended."

        return {
            "success": True,
            "job": job_data,
            "quality": {
                "grade": grade,
                "summary": summary,
                "issues": issues,
                "recommendations": recommendations,
                "metrics": metrics,
            },
            "snapshot": snapshot_info,
            "related_events": job_events[-10:],
        }

    except PrinterNotFoundError:
        return _error_dict(f"Printer {printer_name!r} not found.", code="NOT_FOUND")
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to validate print quality: {exc}. Check that the printer is online.")
    except Exception as exc:
        logger.exception("Unexpected error in validate_print_quality")
        return _error_dict(f"Unexpected error in validate_print_quality: {exc}", code="INTERNAL_ERROR")


@mcp.resource("kiln://status")
def resource_status() -> str:
    """Live snapshot of the entire Kiln system: printers, queue, and recent events."""
    import json

    # Fleet
    printers: list[dict[str, Any]] = []
    if _registry.count > 0:
        printers = _registry.get_fleet_status()
    elif _PRINTER_HOST:
        try:
            adapter = _get_adapter()
            state = adapter.get_state()
            printers = [
                {
                    "name": "default",
                    "backend": adapter.name,
                    "connected": state.connected,
                    "state": state.state.value,
                }
            ]
        except Exception as exc:
            logger.debug("Failed to get default printer info for dashboard: %s", exc)

    # Queue
    q_summary = _queue.summary()

    # Events
    events = _event_bus.recent_events(limit=10)

    return json.dumps(
        {
            "printers": printers,
            "printer_count": len(printers),
            "queue": {
                "counts": q_summary,
                "pending": _queue.pending_count(),
                "active": _queue.active_count(),
                "total": _queue.total_count,
            },
            "recent_events": [e.to_dict() for e in events],
        },
        default=str,
    )


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

    return json.dumps(
        {
            "printers": printers,
            "count": len(printers),
            "idle_printers": idle,
        },
        default=str,
    )


@mcp.resource("kiln://printers/{printer_name}")
def resource_printer_detail(printer_name: str) -> str:
    """Detailed status for a specific printer by name."""
    import json

    try:
        adapter = _registry.get(printer_name)
        state = adapter.get_state()
        job = adapter.get_job()
        caps = adapter.capabilities
        return json.dumps(
            {
                "name": printer_name,
                "backend": adapter.name,
                "state": state.to_dict(),
                "job": job.to_dict(),
                "capabilities": caps.to_dict(),
            },
            default=str,
        )
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

    return json.dumps(
        {
            "counts": summary,
            "pending": _queue.pending_count(),
            "active": _queue.active_count(),
            "total": _queue.total_count,
            "next_job": next_job.to_dict() if next_job else None,
            "recent_jobs": [j.to_dict() for j in recent],
        },
        default=str,
    )


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
    return json.dumps(
        {
            "events": [e.to_dict() for e in events],
            "count": len(events),
        },
        default=str,
    )


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
        "2. Use `register_printer` to add new printers (octoprint, moonraker, bambu, elegoo, prusaconnect, or serial)\n"
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
# Model Generation
# ---------------------------------------------------------------------------


_generation_providers: dict[str, GenerationProvider] = {}


def _get_generation_provider(provider: str = "meshy") -> GenerationProvider:
    """Get or create a generation provider by name.

    Providers are cached so that state (model URLs, prompts) persists
    across MCP tool calls within the same server session.
    """
    if provider in _generation_providers:
        return _generation_providers[provider]

    if provider == "meshy":
        inst = MeshyProvider(api_key=_MESHY_API_KEY)
    elif provider == "openscad":
        inst = OpenSCADProvider()
    else:
        raise GenerationError(
            f"Unknown generation provider: {provider!r}.  Supported: meshy, openscad.",
            code="UNKNOWN_PROVIDER",
        )

    _generation_providers[provider] = inst
    return inst


@mcp.tool()
def list_generation_providers() -> dict:
    """List available text-to-3D generation providers.

    Returns details about each provider: name, description,
    available styles, and whether it requires an API key.
    Use this to discover providers before calling ``generate_model``.
    """
    providers = [
        {
            "name": "meshy",
            "display_name": "Meshy",
            "description": (
                "Cloud AI text-to-3D.  Generates 3D models from natural "
                "language descriptions.  Requires KILN_MESHY_API_KEY."
            ),
            "requires_api_key": True,
            "api_key_env": "KILN_MESHY_API_KEY",
            "api_key_set": bool(_MESHY_API_KEY),
            "styles": ["realistic", "sculpture"],
            "async": True,
            "typical_time_seconds": 60,
        },
        {
            "name": "openscad",
            "display_name": "OpenSCAD",
            "description": (
                "Local parametric generation.  Prompt must be valid "
                "OpenSCAD code.  Completes synchronously, no API key needed."
            ),
            "requires_api_key": False,
            "styles": [],
            "async": False,
            "typical_time_seconds": 5,
        },
    ]
    return {
        "success": True,
        "providers": providers,
    }


@mcp.tool()
def generate_model(
    prompt: str,
    provider: str = "meshy",
    format: str = "stl",
    style: str | None = None,
) -> dict:
    """Generate a 3D model from a text description.

    **EXPERIMENTAL:** AI-generated 3D models are experimental and may not
    be suitable for printing without manual review.  Generated geometry
    can have thin walls, non-manifold faces, floating islands, or
    dimensions that exceed printer build volume.  3D printers are delicate
    hardware — always validate the generated mesh before printing.

    **When possible, prefer downloading proven community models from
    marketplaces** (Thingiverse, MyMiniFactory) over generating new ones.
    Use generation for custom/unique objects only.

    Submits a generation job to the specified provider and returns a
    job ID for status tracking.  Use ``generation_status`` to poll for
    completion, then ``download_generated_model`` to retrieve the file.

    **Prompt tips for Meshy (text-to-3D AI):**
    - Describe the physical object clearly: shape, size, purpose.
    - Include material cues: "wooden", "metallic", "smooth plastic".
    - Specify printability: "solid base", "no overhangs", "flat bottom".
    - Keep prompts under 200 words for best results (max 600 chars).
    - Good example: "A phone stand with a curved cradle, flat rectangular
      base, and angled back support. Smooth plastic surface."
    - Bad example: "make me something cool" (too vague).

    **For OpenSCAD**, the prompt must be valid OpenSCAD code.  The job
    completes synchronously and the result is immediately available.

    Args:
        prompt: Text description (or OpenSCAD code for ``openscad``).
        provider: Generation backend — ``"meshy"`` (cloud AI) or
            ``"openscad"`` (local parametric).  Default: ``"meshy"``.
        format: Desired output format (``"stl"``).  Default: ``"stl"``.
        style: Optional style hint (``"realistic"`` or ``"sculpture"``
            for Meshy).  Ignored by OpenSCAD.
    """
    if err := _check_auth("generate"):
        return err
    try:
        gen = _get_generation_provider(provider)
        job = gen.generate(prompt, format=format, style=style)
        return {
            "success": True,
            "job": job.to_dict(),
            "experimental": True,
            "safety_notice": (
                "AI-generated models are experimental. Always validate "
                "the mesh with validate_generated_mesh and review "
                "dimensions before printing. Generated models may require "
                "manual refinement."
            ),
            "message": f"Generation job submitted to {gen.display_name}.",
        }
    except GenerationAuthError as exc:
        return _error_dict(
            f"Failed to generate model (auth): {exc}. Check that KILN_MESHY_API_KEY is set.", code="AUTH_ERROR"
        )
    except GenerationError as exc:
        return _error_dict(f"Failed to generate model: {exc}", code=exc.code or "GENERATION_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in generate_model")
        return _error_dict(f"Unexpected error in generate_model: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def generation_status(
    job_id: str,
    provider: str = "meshy",
) -> dict:
    """Check the status of a model generation job.

    Args:
        job_id: Job ID returned by ``generate_model``.
        provider: Provider that owns the job (``"meshy"`` or ``"openscad"``).
    """
    if err := _check_auth("generate"):
        return err
    try:
        gen = _get_generation_provider(provider)
        job = gen.get_job_status(job_id)
        return {
            "success": True,
            "job": job.to_dict(),
        }
    except GenerationAuthError as exc:
        return _error_dict(
            f"Failed to check generation status (auth): {exc}. Check that KILN_MESHY_API_KEY is set.", code="AUTH_ERROR"
        )
    except GenerationError as exc:
        return _error_dict(f"Failed to check generation status: {exc}", code=exc.code or "GENERATION_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in generation_status")
        return _error_dict(f"Unexpected error in generation_status: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def download_generated_model(
    job_id: str,
    provider: str = "meshy",
    output_path: str | None = None,
) -> dict:
    """Download a completed generated model and optionally validate it.

    Args:
        job_id: Job ID of a completed generation job.
        provider: Provider that owns the job (``"meshy"`` or ``"openscad"``).
        output_path: Directory to save the file.  Defaults to
            the system temp directory.
    """
    if err := _check_auth("generate"):
        return err
    output_dir = output_path or os.path.join(tempfile.gettempdir(), "kiln_generated")
    if disk_err := _check_disk_space(output_dir):
        return disk_err
    try:
        gen = _get_generation_provider(provider)
        result = gen.download_result(job_id, output_dir=output_dir)

        # Auto-convert OBJ to STL for maximum slicer compatibility.
        if result.format == "obj":
            try:
                stl_path = convert_to_stl(result.local_path)
                result = GenerationResult(
                    job_id=result.job_id,
                    provider=result.provider,
                    local_path=stl_path,
                    format="stl",
                    file_size_bytes=os.path.getsize(stl_path),
                    prompt=result.prompt,
                )
                logger.info("Auto-converted OBJ to STL: %s", stl_path)
            except Exception as exc:
                logger.warning("OBJ→STL conversion failed, keeping OBJ: %s", exc)

        # Validate the mesh if it's an STL or OBJ.
        validation = None
        dimensions = None
        if result.format in ("stl", "obj"):
            val = validate_mesh(result.local_path)
            validation = val.to_dict()
            if val.bounding_box:
                bb = val.bounding_box
                w = bb.get("x_max", 0) - bb.get("x_min", 0)
                d = bb.get("y_max", 0) - bb.get("y_min", 0)
                h = bb.get("z_max", 0) - bb.get("z_min", 0)
                dimensions = {
                    "width_mm": round(w, 2),
                    "depth_mm": round(d, 2),
                    "height_mm": round(h, 2),
                    "summary": f"{w:.1f} x {d:.1f} x {h:.1f} mm",
                }

        return {
            "success": True,
            "result": result.to_dict(),
            "validation": validation,
            "dimensions": dimensions,
            "experimental": True,
            "safety_notice": (
                "AI-generated model. Inspect validation results and "
                "dimensions carefully before printing. Generated geometry "
                "may have thin walls, overhangs, or non-manifold faces "
                "that can fail during printing or damage hardware."
            ),
            "message": f"Model downloaded to {result.local_path}.",
        }
    except GenerationAuthError as exc:
        return _error_dict(
            f"Failed to download generated model (auth): {exc}. Check that KILN_MESHY_API_KEY is set.",
            code="AUTH_ERROR",
        )
    except GenerationError as exc:
        return _error_dict(f"Failed to download generated model: {exc}", code=exc.code or "GENERATION_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in download_generated_model")
        return _error_dict(f"Unexpected error in download_generated_model: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def await_generation(
    job_id: str,
    provider: str = "meshy",
    timeout: int = 600,
    poll_interval: int = 10,
) -> dict:
    """Wait for a generation job to complete and return the final status.

    Polls the provider until the job reaches a terminal state or the
    timeout is exceeded.  Useful for agents that want to block until
    a model is ready.

    Args:
        job_id: Job ID from ``generate_model``.
        provider: Provider that owns the job.
        timeout: Max seconds to wait for generation (default 600 = 10 min).
        poll_interval: Seconds between polls (default 10).
    """
    if err := _check_auth("generate"):
        return err
    try:
        gen = _get_generation_provider(provider)
        start = time.time()
        progress_log: list[dict] = []

        while True:
            elapsed = time.time() - start
            if elapsed >= timeout:
                return {
                    "success": True,
                    "outcome": "timeout",
                    "elapsed_seconds": round(elapsed, 1),
                    "message": f"Timed out after {timeout}s waiting for generation.",
                    "progress_log": progress_log[-20:],
                }

            job = gen.get_job_status(job_id)

            progress_log.append(
                {
                    "time": round(elapsed, 1),
                    "status": job.status.value,
                    "progress": job.progress,
                }
            )

            if job.status == GenerationStatus.SUCCEEDED:
                return {
                    "success": True,
                    "outcome": "completed",
                    "job": job.to_dict(),
                    "elapsed_seconds": round(elapsed, 1),
                    "progress_log": progress_log[-20:],
                }
            if job.status == GenerationStatus.FAILED:
                return {
                    "success": True,
                    "outcome": "failed",
                    "job": job.to_dict(),
                    "error": job.error,
                    "elapsed_seconds": round(elapsed, 1),
                    "progress_log": progress_log[-20:],
                }
            if job.status == GenerationStatus.CANCELLED:
                return {
                    "success": True,
                    "outcome": "cancelled",
                    "job": job.to_dict(),
                    "elapsed_seconds": round(elapsed, 1),
                    "progress_log": progress_log[-20:],
                }

            time.sleep(poll_interval)

    except GenerationAuthError as exc:
        return _error_dict(
            f"Failed to await generation (auth): {exc}. Check that KILN_MESHY_API_KEY is set.", code="AUTH_ERROR"
        )
    except GenerationError as exc:
        return _error_dict(f"Failed to await generation: {exc}", code=exc.code or "GENERATION_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in await_generation")
        return _error_dict(f"Unexpected error in await_generation: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def generate_and_print(
    prompt: str,
    provider: str = "meshy",
    style: str | None = None,
    printer_name: str | None = None,
    profile: str | None = None,
    printer_id: str | None = None,
    timeout: int = 600,
) -> dict:
    """Full pipeline: generate a model, validate, slice, and upload (preview).

    **EXPERIMENTAL:** This generates a 3D model, validates it, slices it,
    and uploads it to the printer — but does NOT start printing.  3D
    printers are delicate hardware and AI-generated models are not
    guaranteed to be safe or printable.  You MUST call ``start_print``
    separately after reviewing the preview results.

    When possible, prefer downloading proven models from marketplaces
    (Thingiverse, MyMiniFactory) instead of generating new ones.

    Args:
        prompt: Text description of the 3D model to generate.
        provider: Generation provider (``"meshy"`` or ``"openscad"``).
        style: Optional style hint for cloud providers.
        printer_name: Target printer.  Omit for the default printer.
        profile: Slicer profile path.
        printer_id: Optional printer model ID for bundled profile
            auto-selection (e.g. ``"prusa_mini"``).
        timeout: Max seconds to wait for generation (default 600).
    """
    if err := _check_auth("print"):
        return err
    try:
        gen = _get_generation_provider(provider)

        # Step 1: Generate
        job = gen.generate(prompt, format="stl", style=style)
        logger.info("Generation job %s submitted to %s", job.id, gen.display_name)

        # Step 2: Wait for completion (skip polling for synchronous providers)
        if job.status != GenerationStatus.SUCCEEDED:
            start = time.time()
            while True:
                elapsed = time.time() - start
                if elapsed >= timeout:
                    return _error_dict(
                        f"Generation timed out after {timeout}s.",
                        code="GENERATION_TIMEOUT",
                    )
                job = gen.get_job_status(job.id)
                if job.status == GenerationStatus.SUCCEEDED:
                    break
                if job.status in (GenerationStatus.FAILED, GenerationStatus.CANCELLED):
                    return _error_dict(
                        f"Generation {job.status.value}: {job.error or 'unknown error'}",
                        code="GENERATION_FAILED",
                    )
                time.sleep(10)

        # Step 3: Download
        result = gen.download_result(job.id)

        # Step 3.5: Auto-convert OBJ → STL
        if result.format == "obj":
            try:
                stl_path = convert_to_stl(result.local_path)
                result = GenerationResult(
                    job_id=result.job_id,
                    provider=result.provider,
                    local_path=stl_path,
                    format="stl",
                    file_size_bytes=os.path.getsize(stl_path),
                    prompt=result.prompt,
                )
            except Exception as exc:
                logger.warning("OBJ→STL conversion failed, keeping OBJ: %s", exc)

        # Step 4: Validate
        if result.format in ("stl", "obj"):
            val = validate_mesh(result.local_path)
            if not val.valid:
                return _error_dict(
                    f"Generated mesh failed validation: {'; '.join(val.errors)}",
                    code="VALIDATION_FAILED",
                )

        # Step 5: Slice
        from kiln.slicer import slice_file

        effective_printer_id, effective_profile = _resolve_slice_profile_context(
            profile=profile,
            printer_id=printer_id,
        )
        slice_result = slice_file(
            result.local_path,
            profile=effective_profile,
        )

        # Step 6: Upload (but do NOT auto-start — require explicit start_print)
        if printer_name:
            adapter = _registry.get(printer_name)
        else:
            adapter = _get_adapter()

        upload = adapter.upload_file(slice_result.output_path)
        file_name = upload.file_name or os.path.basename(slice_result.output_path)

        # Compute dimensions for review
        gen_validation = None
        gen_dimensions = None
        if result.format in ("stl", "obj"):
            val_result = validate_mesh(result.local_path)
            gen_validation = val_result.to_dict()
            if val_result.bounding_box:
                bb = val_result.bounding_box
                w = bb.get("x_max", 0) - bb.get("x_min", 0)
                d = bb.get("y_max", 0) - bb.get("y_min", 0)
                h = bb.get("z_max", 0) - bb.get("z_min", 0)
                gen_dimensions = {
                    "width_mm": round(w, 2),
                    "depth_mm": round(d, 2),
                    "height_mm": round(h, 2),
                    "summary": f"{w:.1f} x {d:.1f} x {h:.1f} mm",
                }

        # Auto-print only if the user has opted in via KILN_AUTO_PRINT_GENERATED.
        print_data = None
        auto_printed = False
        if _AUTO_PRINT_GENERATED:
            # Mandatory pre-flight safety gate before starting print.
            pf = preflight_check()
            if not pf.get("ready", False):
                _audit(
                    "generate_and_print",
                    "preflight_failed",
                    details={
                        "file": file_name,
                        "summary": pf.get("summary", ""),
                    },
                )
                return _error_dict(
                    pf.get("summary", "Pre-flight checks failed"),
                    code="PREFLIGHT_FAILED",
                )
            print_result = adapter.start_print(file_name)
            _heater_watchdog.notify_print_started()
            print_data = print_result.to_dict()
            auto_printed = True

        resp = {
            "success": True,
            "generation": result.to_dict(),
            "slice": slice_result.to_dict(),
            "upload": upload.to_dict(),
            "file_name": file_name,
            "printer_id": effective_printer_id,
            "profile_path": effective_profile,
            "validation": gen_validation,
            "dimensions": gen_dimensions,
            "experimental": True,
            "auto_print_enabled": _AUTO_PRINT_GENERATED,
        }

        if auto_printed:
            resp["print"] = print_data
            resp["safety_notice"] = (
                "WARNING: Auto-print for generated models is enabled "
                "(KILN_AUTO_PRINT_GENERATED=true). AI-generated models "
                "are experimental and may damage printer hardware. "
                "Disable this setting unless you accept the risk."
            )
            resp["message"] = (
                f"Generated '{prompt[:80]}' via {gen.display_name}, sliced, and started printing (auto-print ON)."
            )
        else:
            resp["ready_to_print"] = True
            resp["safety_notice"] = (
                "Model generated, sliced, and uploaded but NOT started. "
                "AI-generated models are experimental — review the "
                "dimensions and validation results above. Call "
                "start_print to begin printing after review. "
                "Set KILN_AUTO_PRINT_GENERATED=true to enable auto-print."
            )
            resp["message"] = (
                f"Generated '{prompt[:80]}' via {gen.display_name}, "
                f"sliced, and uploaded. Call start_print('{file_name}') "
                f"to begin printing after review."
            )

        return resp
    except GenerationAuthError as exc:
        return _error_dict(
            f"Failed to generate and print (auth): {exc}. Check that KILN_MESHY_API_KEY is set.", code="AUTH_ERROR"
        )
    except GenerationError as exc:
        return _error_dict(f"Failed to generate and print: {exc}", code=exc.code or "GENERATION_ERROR")
    except PrinterNotFoundError:
        return _error_dict(f"Printer {printer_name!r} not found.", code="NOT_FOUND")
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to generate and print: {exc}. Check printer connection and slicer availability.")
    except Exception as exc:
        logger.exception("Unexpected error in generate_and_print")
        return _error_dict(f"Unexpected error in generate_and_print: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def validate_generated_mesh(file_path: str) -> dict:
    """Validate a 3D mesh file for printing readiness.

    Checks that the file is a valid STL or OBJ, has reasonable
    dimensions, an acceptable polygon count, and is manifold
    (watertight).

    Args:
        file_path: Path to an STL or OBJ file.
    """
    try:
        result = validate_mesh(file_path)
        return {
            "success": True,
            "validation": result.to_dict(),
            "message": "Mesh is valid." if result.valid else f"Mesh has issues: {'; '.join(result.errors)}",
        }
    except Exception as exc:
        logger.exception("Unexpected error in validate_generated_mesh")
        return _error_dict(f"Unexpected error in validate_generated_mesh: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Firmware update tools
# ---------------------------------------------------------------------------


@mcp.tool()
def firmware_status() -> dict:
    """Check for available firmware updates on the printer.

    Returns a list of firmware components (e.g. Klipper, Moonraker,
    OctoPrint) with their current and available versions, plus whether
    an update is available.

    Not all printer backends support firmware updates.  Bambu and
    PrusaConnect printers will return an ``UNSUPPORTED`` error.
    """
    try:
        adapter = _get_adapter()
        if not adapter.capabilities.can_update_firmware:
            return _error_dict(
                "This printer backend does not support firmware updates.",
                code="UNSUPPORTED",
            )
        status = adapter.get_firmware_status()
        if status is None:
            return _error_dict("Could not retrieve firmware status.", code="UNAVAILABLE")
        return {
            "success": True,
            "busy": status.busy,
            "updates_available": status.updates_available,
            "components": [
                {
                    "name": c.name,
                    "current_version": c.current_version,
                    "remote_version": c.remote_version,
                    "update_available": c.update_available,
                    "rollback_version": c.rollback_version,
                    "component_type": c.component_type,
                    "channel": c.channel,
                }
                for c in status.components
            ],
        }
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to get firmware status: {exc}. Check that the printer is online.")
    except Exception as exc:
        logger.exception("Unexpected error in firmware_status")
        return _error_dict(f"Unexpected error in firmware_status: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def update_firmware(component: str | None = None) -> dict:
    """Start a firmware update on the printer.

    For Moonraker printers, this triggers the Klipper update manager.
    For OctoPrint printers, this uses the Software Update plugin.

    Args:
        component: Optional component name to update (e.g. ``"klipper"``,
            ``"moonraker"``).  If omitted, all components with available
            updates will be upgraded.

    The printer must not be actively printing.  Check ``firmware_status``
    first to see which updates are available.
    """
    if err := _check_auth("firmware"):
        return err
    try:
        adapter = _get_adapter()
        if not adapter.capabilities.can_update_firmware:
            return _error_dict(
                "This printer backend does not support firmware updates.",
                code="UNSUPPORTED",
            )
        result = adapter.update_firmware(component=component)
        return {
            "success": result.success,
            "message": result.message,
            "component": result.component,
        }
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to update firmware: {exc}. Ensure the printer is idle and online.")
    except Exception as exc:
        logger.exception("Unexpected error in update_firmware")
        return _error_dict(f"Unexpected error in update_firmware: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def rollback_firmware(component: str) -> dict:
    """Roll back a firmware component to its previous version.

    Only supported on Moonraker printers.  The component must have a
    known rollback version (check ``firmware_status``).

    Args:
        component: Name of the component to roll back (e.g. ``"klipper"``).
    """
    if err := _check_auth("firmware"):
        return err
    try:
        adapter = _get_adapter()
        if not adapter.capabilities.can_update_firmware:
            return _error_dict(
                "This printer backend does not support firmware rollback.",
                code="UNSUPPORTED",
            )
        result = adapter.rollback_firmware(component)
        return {
            "success": result.success,
            "message": result.message,
            "component": result.component,
        }
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(
            f"Failed to rollback firmware: {exc}. Check firmware_status for available rollback versions."
        )
    except Exception as exc:
        logger.exception("Unexpected error in rollback_firmware")
        return _error_dict(f"Unexpected error in rollback_firmware: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Print History & Agent Memory
# ---------------------------------------------------------------------------


@mcp.tool()
def print_history(
    printer_name: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> dict:
    """Get recent print history with success/failure tracking.

    Args:
        printer_name: Filter by printer name, or all printers if omitted.
        status: Filter by status (``"completed"`` or ``"failed"``).
        limit: Maximum records to return (default 20).
    """
    if err := _check_auth("history"):
        return err
    try:
        capped = min(max(limit, 1), 200)
        records = get_db().list_print_history(
            printer_name=printer_name,
            status=status,
            limit=capped,
        )
        return {"success": True, "records": records, "count": len(records)}
    except Exception as exc:
        logger.exception("Unexpected error in print_history")
        return _error_dict(f"Unexpected error in print_history: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def printer_stats(printer_name: str) -> dict:
    """Get aggregate statistics for a printer: total prints, success rate, average duration.

    Args:
        printer_name: Name of the printer to get stats for.
    """
    if err := _check_auth("history"):
        return err
    try:
        stats = get_db().get_printer_stats(printer_name)
        return {"success": True, **stats}
    except Exception as exc:
        logger.exception("Unexpected error in printer_stats")
        return _error_dict(f"Unexpected error in printer_stats: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def annotate_print(job_id: str, notes: str) -> dict:
    """Add notes to a completed print record (e.g., quality observations, issues).

    Args:
        job_id: The job ID of the print to annotate.
        notes: The annotation text to attach.
    """
    if err := _check_auth("history"):
        return err
    try:
        record = get_db().get_print_record(job_id)
        if record is None:
            return _error_dict(
                f"No print history record found for job '{job_id}'.",
                code="NOT_FOUND",
            )
        updated = get_db().update_print_notes(job_id, notes)
        if not updated:
            return _error_dict(
                f"Failed to update notes for job '{job_id}'.",
                code="ERROR",
            )
        return {"success": True, "job_id": job_id, "notes": notes}
    except Exception as exc:
        logger.exception("Unexpected error in annotate_print")
        return _error_dict(f"Unexpected error in annotate_print: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Vision monitoring tools
# ---------------------------------------------------------------------------

_PHASE_HINTS = {
    "first_layers": [
        "Check bed adhesion — first layer should be firmly stuck",
        "Look for warping at corners or edges lifting from bed",
        "Verify extrusion is consistent (no gaps or blobs)",
    ],
    "mid_print": [
        "Check for spaghetti — filament not adhering to previous layers",
        "Look for layer shifting (misaligned layers)",
        "Check for stringing between features",
    ],
    "final_layers": [
        "Check for cooling artifacts on overhangs",
        "Look for stringing or blobs on fine details",
        "Verify top surface is smooth and complete",
    ],
    "unknown": [
        "Verify print is progressing normally",
        "Check for any visible defects",
    ],
}


def _detect_phase(completion: float | None) -> str:
    """Classify print phase from completion percentage."""
    if completion is None or completion < 0:
        return "unknown"
    if completion < 10:
        return "first_layers"
    if completion > 90:
        return "final_layers"
    return "mid_print"


@mcp.tool()
def monitor_print_vision(
    printer_name: str | None = None,
    include_snapshot: bool = True,
    save_snapshot: str | None = None,
    failure_type: str | None = None,
    failure_confidence: float | None = None,
    auto_pause: bool | None = None,
) -> dict:
    """Capture a snapshot and printer state for visual inspection of an in-progress print.

    Returns the webcam image alongside structured metadata (temperatures,
    progress, print phase, failure hints) so the agent can visually assess
    print quality and decide whether to intervene.

    This is the *during-print* counterpart to ``validate_print_quality``
    (which runs after a print finishes).

    Args:
        printer_name: Target printer.  Omit for the default printer.
        include_snapshot: Whether to capture a webcam snapshot (default True).
        save_snapshot: Optional path to save the snapshot image.
        failure_type: Optional detected failure type (e.g. "spaghetti",
            "layer_shift", "warping").  Reported by the agent after visual
            inspection of a previous snapshot.
        failure_confidence: Confidence score (0.0-1.0) of the failure detection.
        auto_pause: If True, automatically pause the print when a failure is
            detected with confidence >= 0.8.  Defaults to the value of the
            ``KILN_VISION_AUTO_PAUSE`` environment variable (default False).
    """
    if err := _check_auth("monitoring"):
        return err
    try:
        adapter = _registry.get(printer_name) if printer_name else _get_adapter()
        state = adapter.get_state()
        job = adapter.get_job()
        is_printing = state.state == PrinterStatus.PRINTING
        phase = _detect_phase(job.completion)
        hints = _PHASE_HINTS.get(phase, _PHASE_HINTS["unknown"])

        result: dict[str, Any] = {
            "success": True,
            "printer_state": state.to_dict(),
            "job_progress": job.to_dict(),
            "monitoring_context": {
                "is_printing": is_printing,
                "print_phase": phase,
                "completion_percent": job.completion,
                "failure_hints": hints,
            },
            "actions_available": {
                "pause": "pause_print",
                "cancel": "cancel_print",
                "annotate": "annotate_print",
            },
        }

        # Snapshot capture — respect can_snapshot capability
        if include_snapshot and not getattr(adapter.capabilities, "can_snapshot", False):
            result["snapshot"] = {"available": False, "reason": "no_capability"}
        elif include_snapshot:
            try:
                image_data = adapter.get_snapshot()
                if image_data and len(image_data) > 100:
                    import base64

                    snap: dict[str, Any] = {
                        "available": True,
                        "size_bytes": len(image_data),
                        "captured_at": time.time(),
                    }
                    if save_snapshot:
                        # Sanitise path — restrict to home dir or temp dir
                        _safe = os.path.abspath(save_snapshot)
                        _home = os.path.expanduser("~")
                        _tmpdir = os.path.realpath(tempfile.gettempdir())
                        if not (_safe.startswith(_home) or _safe.startswith(_tmpdir)):
                            return _error_dict(
                                "save_snapshot path must be under home directory or temp directory.",
                                code="VALIDATION_ERROR",
                            )
                        os.makedirs(os.path.dirname(_safe) or ".", exist_ok=True)
                        with open(_safe, "wb") as f:
                            f.write(image_data)
                        snap["saved_to"] = _safe
                    else:
                        snap["image_base64"] = base64.b64encode(image_data).decode("ascii")
                    result["snapshot"] = snap
                else:
                    result["snapshot"] = {"available": False}
            except Exception as exc:
                logger.debug("Failed to capture snapshot for vision monitoring: %s", exc)
                result["snapshot"] = {"available": False}
        else:
            result["snapshot"] = {"available": False, "reason": "not_requested"}

        # -- Auto-pause on failure detection ----------------------------
        _auto_pause = auto_pause
        if _auto_pause is None:
            _auto_pause = os.environ.get("KILN_VISION_AUTO_PAUSE", "").lower() in ("1", "true", "yes")

        auto_paused = False
        if failure_type and failure_confidence is not None:
            result["failure_detection"] = {
                "type": failure_type,
                "confidence": failure_confidence,
                "auto_pause_enabled": _auto_pause,
            }
            if _auto_pause and failure_confidence >= 0.8 and is_printing:
                try:
                    adapter.pause_print()
                    auto_paused = True
                    result["failure_detection"]["auto_paused"] = True
                    result["failure_detection"]["message"] = (
                        f"Print auto-paused due to detected {failure_type} (confidence: {failure_confidence:.0%})"
                    )
                    logger.warning(
                        "Vision auto-pause triggered: %s (confidence=%.2f) on printer %s",
                        failure_type,
                        failure_confidence,
                        printer_name or "default",
                    )
                except Exception as pause_exc:
                    result["failure_detection"]["auto_pause_error"] = str(pause_exc)
                    logger.error(
                        "Vision auto-pause failed: %s on printer %s",
                        pause_exc,
                        printer_name or "default",
                    )

        # Publish vision check event
        _event_bus.publish(
            EventType.VISION_CHECK,
            {
                "printer_name": printer_name or "default",
                "completion": job.completion,
                "phase": phase,
                "snapshot_captured": result["snapshot"].get("available", False),
                "auto_paused": auto_paused,
            },
            source="vision",
        )

        if auto_paused:
            _event_bus.publish(
                EventType.VISION_ALERT,
                {
                    "printer_name": printer_name or "default",
                    "alert_type": "auto_pause",
                    "failure_type": failure_type,
                    "failure_confidence": failure_confidence,
                    "completion": job.completion,
                },
                source="vision",
            )

        return result

    except PrinterNotFoundError:
        return _error_dict(f"Printer {printer_name!r} not found.", code="NOT_FOUND")
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(
            f"Failed to run vision monitoring: {exc}. Check that the printer is online and has a webcam."
        )
    except Exception as exc:
        logger.exception("Unexpected error in monitor_print_vision")
        return _error_dict(f"Unexpected error in monitor_print_vision: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Background print watcher
# ---------------------------------------------------------------------------

_watchers: dict[str, _PrintWatcher] = {}


class _PrintWatcher:
    """Background thread that monitors a running print.

    Polls printer state and captures snapshots in a daemon thread so
    that the MCP tool can return immediately.  Use :meth:`status` to
    read current progress and :meth:`stop` to cancel monitoring.
    """

    def __init__(
        self,
        watch_id: str,
        adapter: PrinterAdapter,
        printer_name: str,
        *,
        snapshot_interval: int = 60,
        max_snapshots: int = 5,
        timeout: int = 7200,
        poll_interval: int = 15,
        event_bus: Any | None = None,
        stall_timeout: int = 600,
        save_to_disk: bool = False,
    ) -> None:
        self._watch_id = watch_id
        self._adapter = adapter
        self._printer_name = printer_name
        self._snapshot_interval = snapshot_interval
        self._max_snapshots = max_snapshots
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._event_bus = event_bus
        self._stall_timeout = stall_timeout
        self._save_to_disk = save_to_disk

        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._snapshots: list[dict] = []
        self._progress_log: list[dict] = []
        self._snapshot_failures: int = 0
        self._result: dict | None = None
        self._outcome: str = "running"
        self._start_time: float = 0.0
        self._thread: threading.Thread | None = None
        self._save_dir: str | None = None
        if self._save_to_disk:
            self._save_dir = os.path.join(str(Path.home()), ".kiln", "timelapses", watch_id)

    # -- public API --------------------------------------------------------

    def start(self) -> None:
        """Start the background monitoring thread."""
        self._start_time = time.time()
        self._thread = threading.Thread(
            target=self._run,
            name=f"print-watcher-{self._watch_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> dict:
        """Signal the watcher thread to stop and return final state."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self._lock:
            if self._result is not None:
                return self._result
            elapsed = round(time.time() - self._start_time, 1)
            return {
                "success": True,
                "watch_id": self._watch_id,
                "outcome": "stopped",
                "elapsed_seconds": elapsed,
                "progress_log": list(self._progress_log[-20:]),
                "snapshots": list(self._snapshots),
                "snapshot_failures": self._snapshot_failures,
            }

    def status(self) -> dict:
        """Return current watcher state (thread-safe snapshot)."""
        with self._lock:
            elapsed = round(time.time() - self._start_time, 1)
            return {
                "watch_id": self._watch_id,
                "printer_name": self._printer_name,
                "outcome": self._outcome,
                "elapsed_seconds": elapsed,
                "snapshots_collected": len(self._snapshots),
                "snapshot_failures": self._snapshot_failures,
                "progress_log": list(self._progress_log[-20:]),
                "snapshots": list(self._snapshots),
                "finished": self._result is not None,
                "result": self._result,
            }

    # -- internal ----------------------------------------------------------

    def _finish(self, result: dict) -> None:
        """Store the final result and publish a completion event."""
        with self._lock:
            self._result = result
            self._outcome = result.get("outcome", "unknown")
        if self._event_bus is not None:
            try:
                self._event_bus.publish(
                    EventType.PRINT_TERMINAL,
                    {
                        "watch_id": self._watch_id,
                        "printer_name": self._printer_name,
                        "outcome": result.get("outcome"),
                        "elapsed_seconds": result.get("elapsed_seconds"),
                    },
                    source="watch_print",
                )
            except Exception as exc:
                logger.debug("Failed to publish print terminal event: %s", exc)  # event delivery is best-effort

    def _run(self) -> None:
        """Main monitoring loop — runs in a background thread."""
        adapter = self._adapter
        can_snap = getattr(adapter.capabilities, "can_snapshot", False)
        last_snapshot_time = 0.0

        # Stall detection state
        _last_completion: float | None = None
        _last_progress_time: float = time.time()

        try:
            while not self._stop_event.is_set():
                elapsed = time.time() - self._start_time
                if elapsed > self._timeout:
                    result = {
                        "success": True,
                        "watch_id": self._watch_id,
                        "outcome": "timeout",
                        "elapsed_seconds": round(elapsed, 1),
                        "progress_log": list(self._progress_log[-20:]),
                        "snapshots": list(self._snapshots),
                        "snapshot_failures": self._snapshot_failures,
                        "final_state": adapter.get_state().to_dict(),
                    }
                    self._finish(result)
                    return

                state = adapter.get_state()
                job = adapter.get_job()

                # Record progress
                if job.completion is not None:
                    with self._lock:
                        self._progress_log.append(
                            {
                                "time": round(elapsed, 1),
                                "completion": job.completion,
                            }
                        )

                # Stall detection — check if completion has changed
                if job.completion is not None:
                    if _last_completion is None or abs(job.completion - _last_completion) > 0.1:
                        _last_completion = job.completion
                        _last_progress_time = time.time()
                    elif self._stall_timeout > 0 and (time.time() - _last_progress_time) > self._stall_timeout:
                        stall_duration = round(time.time() - _last_progress_time, 1)
                        if self._event_bus is not None:
                            try:
                                self._event_bus.publish(
                                    EventType.VISION_ALERT,
                                    {
                                        "printer_name": self._printer_name,
                                        "alert_type": "stall",
                                        "completion": job.completion,
                                        "stall_duration_seconds": stall_duration,
                                        "elapsed_seconds": round(elapsed, 1),
                                    },
                                    source="watch_print",
                                )
                            except Exception as exc:
                                logger.debug("Failed to publish stall vision alert: %s", exc)
                        result = {
                            "success": True,
                            "watch_id": self._watch_id,
                            "outcome": "stalled",
                            "elapsed_seconds": round(elapsed, 1),
                            "stall_duration_seconds": stall_duration,
                            "stalled_at_completion": job.completion,
                            "progress_log": list(self._progress_log[-20:]),
                            "snapshots": list(self._snapshots),
                            "snapshot_failures": self._snapshot_failures,
                            "final_state": state.to_dict(),
                            "message": (
                                f"Print appears stalled at {job.completion:.1f}% "
                                f"for {stall_duration:.0f} seconds. "
                                "Consider checking the printer or cancelling the print."
                            ),
                        }
                        self._finish(result)
                        return

                # Check terminal states
                if state.state == PrinterStatus.IDLE and elapsed > 30:
                    result = {
                        "success": True,
                        "watch_id": self._watch_id,
                        "outcome": "completed",
                        "elapsed_seconds": round(elapsed, 1),
                        "progress_log": list(self._progress_log[-20:]),
                        "snapshots": list(self._snapshots),
                        "snapshot_failures": self._snapshot_failures,
                        "final_state": state.to_dict(),
                    }
                    self._finish(result)
                    return

                if state.state in (PrinterStatus.ERROR, PrinterStatus.OFFLINE):
                    if self._event_bus is not None:
                        try:
                            self._event_bus.publish(
                                EventType.VISION_ALERT,
                                {
                                    "printer_name": self._printer_name,
                                    "alert_type": "printer_state",
                                    "state": state.state.value,
                                    "completion": job.completion,
                                    "elapsed_seconds": round(elapsed, 1),
                                },
                                source="vision",
                            )
                        except Exception as exc:
                            logger.debug("Failed to publish printer state vision alert: %s", exc)
                    result = {
                        "success": True,
                        "watch_id": self._watch_id,
                        "outcome": "failed",
                        "elapsed_seconds": round(elapsed, 1),
                        "progress_log": list(self._progress_log[-20:]),
                        "snapshots": list(self._snapshots),
                        "snapshot_failures": self._snapshot_failures,
                        "final_state": state.to_dict(),
                        "error": f"Printer entered {state.state.value} state",
                    }
                    self._finish(result)
                    return

                if state.state == PrinterStatus.PAUSED:
                    result = {
                        "success": True,
                        "watch_id": self._watch_id,
                        "outcome": "paused",
                        "elapsed_seconds": round(elapsed, 1),
                        "progress_log": list(self._progress_log[-20:]),
                        "snapshots": list(self._snapshots),
                        "snapshot_failures": self._snapshot_failures,
                        "final_state": state.to_dict(),
                        "message": ("Print is paused. Call resume_print to continue, or cancel_print to abort."),
                    }
                    self._finish(result)
                    return

                if state.state == PrinterStatus.CANCELLING:
                    result = {
                        "success": True,
                        "watch_id": self._watch_id,
                        "outcome": "cancelling",
                        "elapsed_seconds": round(elapsed, 1),
                        "progress_log": list(self._progress_log[-20:]),
                        "snapshots": list(self._snapshots),
                        "snapshot_failures": self._snapshot_failures,
                        "final_state": state.to_dict(),
                    }
                    self._finish(result)
                    return

                # Snapshot capture
                now = time.time()
                if can_snap and (now - last_snapshot_time) >= self._snapshot_interval:
                    try:
                        image_data = adapter.get_snapshot()
                        if image_data and len(image_data) > 100:
                            import base64

                            phase = _detect_phase(job.completion)
                            snap = {
                                "captured_at": now,
                                "completion_percent": job.completion,
                                "print_phase": phase,
                                "image_base64": base64.b64encode(image_data).decode("ascii"),
                            }

                            # Persist to disk + DB when save_to_disk is enabled
                            if self._save_to_disk and self._save_dir is not None:
                                try:
                                    os.makedirs(self._save_dir, exist_ok=True)
                                    frame_idx = len(self._snapshots)
                                    fpath = os.path.join(self._save_dir, f"frame_{frame_idx:04d}.jpg")
                                    with open(fpath, "wb") as f:
                                        f.write(image_data)
                                    snap["saved_path"] = fpath
                                    get_db().save_snapshot(
                                        printer_name=self._printer_name,
                                        image_path=fpath,
                                        job_id=self._watch_id,
                                        phase=phase,
                                        image_size_bytes=len(image_data),
                                        completion_pct=job.completion,
                                    )
                                except Exception:
                                    logger.debug(
                                        "Failed to persist snapshot to disk/DB",
                                        exc_info=True,
                                    )

                            with self._lock:
                                self._snapshots.append(snap)
                            if self._event_bus is not None:
                                try:
                                    self._event_bus.publish(
                                        EventType.VISION_CHECK,
                                        {
                                            "printer_name": self._printer_name,
                                            "completion": job.completion,
                                            "phase": phase,
                                            "snapshot_index": len(self._snapshots),
                                        },
                                        source="vision",
                                    )
                                except Exception as exc:
                                    logger.debug("Failed to publish vision check event: %s", exc)
                        else:
                            with self._lock:
                                self._snapshot_failures += 1
                    except Exception as exc:
                        logger.debug("Failed to capture snapshot in print watcher: %s", exc)
                        with self._lock:
                            self._snapshot_failures += 1
                    last_snapshot_time = now

                # Return batch when enough snapshots accumulated
                with self._lock:
                    snap_count = len(self._snapshots)
                if snap_count >= self._max_snapshots:
                    result = {
                        "success": True,
                        "watch_id": self._watch_id,
                        "outcome": "snapshot_check",
                        "elapsed_seconds": round(time.time() - self._start_time, 1),
                        "progress_log": list(self._progress_log[-20:]),
                        "snapshots": list(self._snapshots),
                        "snapshot_failures": self._snapshot_failures,
                        "final_state": state.to_dict(),
                        "message": (
                            f"Captured {snap_count} snapshots. "
                            "Review them for print quality issues. "
                            "Call pause_print or cancel_print if problems are detected, "
                            "then call watch_print again to continue monitoring."
                        ),
                    }
                    self._finish(result)
                    return

                # Wait using the stop event so stop() can wake us
                self._stop_event.wait(self._poll_interval)

        except Exception as exc:
            logger.exception("Error in print watcher %s", self._watch_id)
            self._finish(
                {
                    "success": False,
                    "watch_id": self._watch_id,
                    "outcome": "error",
                    "error": str(exc),
                    "elapsed_seconds": round(time.time() - self._start_time, 1),
                    "progress_log": list(self._progress_log[-20:]),
                    "snapshots": list(self._snapshots),
                    "snapshot_failures": self._snapshot_failures,
                }
            )


@mcp.tool()
def watch_print(
    printer_name: str | None = None,
    snapshot_interval: int = 60,
    max_snapshots: int = 5,
    timeout: int = 7200,
    poll_interval: int = 15,
    stall_timeout: int = 600,
    save_to_disk: bool = False,
) -> dict:
    """Start background monitoring of an in-progress print.

    Launches a background thread that polls the printer state every
    *poll_interval* seconds and captures webcam snapshots every
    *snapshot_interval* seconds.  Returns immediately with a
    ``watch_id`` that can be used with ``watch_print_status`` and
    ``stop_watch_print``.

    The watcher finishes automatically when:

    1. **Print terminal state** — completed, failed, cancelled, or offline.
    2. **Snapshot batch ready** — *max_snapshots* images collected.
    3. **Timeout** — the print has not finished within *timeout* seconds.

    Args:
        printer_name: Target printer.  Omit for the default printer.
        snapshot_interval: Seconds between snapshot captures (default 60).
        max_snapshots: Return after this many snapshots (default 5).
        timeout: Maximum seconds to monitor (default 7200 = 2 hours).
        poll_interval: Seconds between state polls (default 15).
        stall_timeout: Seconds of zero progress before declaring stall
            (default 600 = 10 min).  Set to 0 to disable stall detection.
        save_to_disk: Save snapshots as JPEG files to
            ``~/.kiln/timelapses/<watch_id>/`` and persist metadata to the
            database.  Use ``list_snapshots`` to query saved frames after
            the print completes (default False).
    """
    if err := _check_auth("monitoring"):
        return err
    try:
        adapter = _registry.get(printer_name) if printer_name else _get_adapter()

        # Early exit: if printer is idle with no active job, don't start
        initial_state = adapter.get_state()
        initial_job = adapter.get_job()
        if initial_state.state == PrinterStatus.IDLE and initial_job.completion is None:
            return {
                "success": True,
                "outcome": "no_active_print",
                "elapsed_seconds": 0,
                "progress_log": [],
                "snapshots": [],
                "final_state": initial_state.to_dict(),
                "message": "Printer is idle with no active print job.",
            }

        watch_id = secrets.token_hex(6)
        watcher = _PrintWatcher(
            watch_id,
            adapter,
            printer_name or "default",
            snapshot_interval=snapshot_interval,
            max_snapshots=max_snapshots,
            timeout=timeout,
            poll_interval=poll_interval,
            event_bus=_event_bus,
            stall_timeout=stall_timeout,
            save_to_disk=save_to_disk,
        )
        _watchers[watch_id] = watcher
        watcher.start()

        resp: dict[str, Any] = {
            "success": True,
            "watch_id": watch_id,
            "status": "started",
            "printer_name": printer_name or "default",
            "snapshot_interval": snapshot_interval,
            "max_snapshots": max_snapshots,
            "timeout": timeout,
            "poll_interval": poll_interval,
            "stall_timeout": stall_timeout,
            "save_to_disk": save_to_disk,
            "message": (
                f"Background watcher started (id={watch_id}). "
                "Use watch_print_status to check progress, "
                "or stop_watch_print to cancel."
            ),
        }
        if save_to_disk and watcher._save_dir:
            resp["save_dir"] = watcher._save_dir
        return resp

    except PrinterNotFoundError:
        return _error_dict(f"Printer {printer_name!r} not found.", code="NOT_FOUND")
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to start print watcher: {exc}. Check that the printer is online.")
    except Exception as exc:
        logger.exception("Unexpected error in watch_print")
        return _error_dict(f"Unexpected error in watch_print: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def watch_print_status(watch_id: str) -> dict:
    """Check the current status of a background print watcher.

    Returns progress, collected snapshots, and whether the watcher
    has finished.

    Args:
        watch_id: The watcher ID returned by ``watch_print``.
    """
    if err := _check_auth("monitoring"):
        return err
    watcher = _watchers.get(watch_id)
    if watcher is None:
        return _error_dict(
            f"No active watcher with id {watch_id!r}. It may have already been stopped or never existed.",
            code="NOT_FOUND",
        )
    return {"success": True, **watcher.status()}


@mcp.tool()
def stop_watch_print(watch_id: str) -> dict:
    """Stop a background print watcher and return its final state.

    Signals the watcher thread to exit and removes it from the
    active watchers registry.

    Args:
        watch_id: The watcher ID returned by ``watch_print``.
    """
    if err := _check_auth("monitoring"):
        return err
    watcher = _watchers.pop(watch_id, None)
    if watcher is None:
        return _error_dict(
            f"No active watcher with id {watch_id!r}. It may have already been stopped or never existed.",
            code="NOT_FOUND",
        )
    result = watcher.stop()
    return {"success": True, **result}


# ---------------------------------------------------------------------------
# Monitored print (start + first-layer monitoring)
# ---------------------------------------------------------------------------

# Store active first-layer monitors so agents can check progress.
_first_layer_monitors: dict[str, Any] = {}


@mcp.tool()
def start_monitored_print(
    file_name: str,
    printer_name: str | None = None,
    first_layer_delay: int = 120,
    first_layer_checks: int = 3,
    first_layer_interval: int = 60,
    auto_pause: bool = True,
) -> dict:
    """Start a print and automatically monitor the first layer.

    This is the recommended way to start prints autonomously. It combines
    start_print with first-layer monitoring in a single operation:

    1. Starts the print
    2. Waits for the configured delay (default 2 minutes)
    3. Captures snapshots during first layers
    4. Returns snapshots for you to visually inspect
    5. Optionally auto-pauses if you report a failure

    Use this instead of start_print when operating autonomously (Level 1/2)
    to satisfy the first-layer monitoring safety requirement.

    Args:
        file_name: Name of the file to print (must exist on printer).
        printer_name: Target printer. Omit for default.
        first_layer_delay: Seconds to wait before first snapshot (default 120).
        first_layer_checks: Number of first-layer snapshots to capture (default 3).
        first_layer_interval: Seconds between snapshots (default 60).
        auto_pause: Auto-pause if snapshot analysis detects failure (default True).
    """
    if err := _check_auth("print"):
        return err
    if err := _check_rate_limit("start_monitored_print"):
        return err
    if conf := _check_confirmation("start_monitored_print", {"file_name": file_name}):
        return conf
    try:
        adapter = _registry.get(printer_name) if printer_name else _get_adapter()

        # -- Automatic pre-flight safety gate (mandatory) --
        pf = preflight_check()
        if not pf.get("ready", False):
            _audit(
                "start_monitored_print",
                "preflight_failed",
                details={
                    "file": file_name,
                    "summary": pf.get("summary", ""),
                },
            )
            result = _error_dict(
                pf.get("summary", "Pre-flight checks failed"),
                code="PREFLIGHT_FAILED",
            )
            result["preflight"] = pf
            return result

        # Start the print
        print_result = adapter.start_print(file_name)
        _heater_watchdog.notify_print_started()
        _audit("start_monitored_print", "print_started", details={"file": file_name})

        # Set up first-layer monitoring in background
        from kiln.print_monitor import FirstLayerMonitor, MonitorPolicy

        monitor_id = secrets.token_hex(6)
        policy = MonitorPolicy(
            delay_seconds=first_layer_delay,
            num_checks=first_layer_checks,
            interval_seconds=first_layer_interval,
            auto_pause=auto_pause,
        )
        monitor = FirstLayerMonitor(
            adapter,
            policy=policy,
            monitor_id=monitor_id,
        )
        _first_layer_monitors[monitor_id] = monitor
        monitor.start()

        return {
            "success": True,
            "print_result": print_result.to_dict(),
            "monitor_id": monitor_id,
            "monitor_status": "started",
            "first_layer_policy": policy.to_dict(),
            "message": (
                f"Print started and first-layer monitor launched (id={monitor_id}). "
                "Use watch_print_status or check back after "
                f"~{first_layer_delay + first_layer_checks * first_layer_interval}s "
                "for first-layer snapshots."
            ),
        }
    except PrinterNotFoundError:
        return _error_dict(f"Printer {printer_name!r} not found.", code="NOT_FOUND")
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to start monitored print: {exc}. Check that the printer is online and idle.")
    except Exception as exc:
        logger.exception("Unexpected error in start_monitored_print")
        return _error_dict(f"Unexpected error in start_monitored_print: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def first_layer_status(monitor_id: str) -> dict:
    """Check the status of a first-layer monitor.

    Returns the current monitoring state, including any captured snapshots
    once monitoring is complete.

    Args:
        monitor_id: The monitor ID returned by ``start_monitored_print``.
    """
    if err := _check_auth("monitoring"):
        return err
    monitor = _first_layer_monitors.get(monitor_id)
    if monitor is None:
        return _error_dict(
            f"No active first-layer monitor with id {monitor_id!r}. It may have already completed or never existed.",
            code="NOT_FOUND",
        )
    result = monitor.result()
    if result is not None:
        # Clean up completed monitors
        _first_layer_monitors.pop(monitor_id, None)
        return {"success": True, "monitor_id": monitor_id, "finished": True, **result.to_dict()}
    return {
        "success": True,
        "monitor_id": monitor_id,
        "finished": False,
        "message": "First-layer monitoring still in progress.",
    }


# ---------------------------------------------------------------------------
# Cross-printer learning + agent memory tools — moved to plugins/learning_tools.py
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Safety profile tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_safety_profiles() -> dict:
    """List all available printer safety profiles.

    Returns a list of profile IDs and display names from the bundled
    safety database.  Use with ``get_safety_profile`` to inspect limits
    for a specific printer, or ``validate_gcode_safe`` to validate
    commands against a printer's limits.
    """
    if err := _check_auth("safety"):
        return err
    try:
        ids = list_profiles()
        profiles = []
        for pid in ids:
            try:
                p = get_profile(pid)
                profiles.append(
                    {
                        "id": p.id,
                        "display_name": p.display_name,
                        "max_hotend_temp": p.max_hotend_temp,
                        "max_bed_temp": p.max_bed_temp,
                    }
                )
            except KeyError:
                continue
        return {"success": True, "count": len(profiles), "profiles": profiles}
    except Exception as exc:
        logger.exception("Unexpected error in list_safety_profiles")
        return _error_dict(f"Unexpected error in list_safety_profiles: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def get_safety_profile(printer_id: str) -> dict:
    """Get the full safety profile for a specific printer model.

    Returns temperature limits, feedrate limits, volumetric flow,
    build volume, and safety notes.  Falls back to the default
    profile if the printer_id is not found.

    Args:
        printer_id: Printer model identifier (e.g. ``"ender3"``,
            ``"bambu_x1c"``, ``"prusa_mk4"``).
    """
    if err := _check_auth("safety"):
        return err
    try:
        profile = get_profile(printer_id)
        return {"success": True, "profile": profile_to_dict(profile)}
    except KeyError:
        return _error_dict(
            f"No safety profile for '{printer_id}' and no default available.",
            code="NOT_FOUND",
        )
    except Exception as exc:
        logger.exception("Unexpected error in get_safety_profile")
        return _error_dict(f"Unexpected error in get_safety_profile: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def add_safety_profile(printer_model: str, profile: dict) -> dict:
    """Add a community safety profile for a printer model.

    Validates the profile and saves it to the user-local community
    profiles file (``~/.kiln/community_profiles.json``).  Community
    profiles take precedence over bundled profiles, allowing users to
    contribute limits for printers not in the built-in database.

    Args:
        printer_model: Short identifier for the printer (e.g.
            ``"my_custom_corexy"``).
        profile: Dict containing at least ``max_hotend_temp``,
            ``max_bed_temp``, ``max_feedrate``, and ``build_volume``
            (a list of 3 positive numbers ``[X, Y, Z]``).  Optional
            fields: ``display_name``, ``max_chamber_temp``, ``min_safe_z``,
            ``max_volumetric_flow``, ``notes``.
    """
    if err := _check_auth("safety"):
        return err
    try:
        errors = validate_safety_profile(profile)
        if errors:
            return _error_dict(
                f"Validation failed: {'; '.join(errors)}",
                code="VALIDATION_ERROR",
            )
        add_community_profile(printer_model, profile)
        return {
            "success": True,
            "printer_model": printer_model.lower().replace("-", "_").strip(),
            "message": "Community safety profile saved successfully.",
        }
    except ValueError as exc:
        return _error_dict(f"Failed to add safety profile: {exc}", code="VALIDATION_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in add_safety_profile")
        return _error_dict(f"Unexpected error in add_safety_profile: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def export_safety_profile(printer_model: str) -> dict:
    """Export a safety profile as a shareable JSON object.

    Returns the full safety limits for a printer model in a format
    suitable for sharing with other users.  Looks up community profiles
    first, then falls back to bundled profiles.

    Args:
        printer_model: Printer model identifier (e.g. ``"ender3"``,
            ``"bambu_x1c"``).
    """
    if err := _check_auth("safety"):
        return err
    try:
        exported = _export_profile(printer_model)
        return {"success": True, "printer_model": printer_model, "profile": exported}
    except KeyError:
        return _error_dict(
            f"No safety profile found for '{printer_model}'.",
            code="NOT_FOUND",
        )
    except Exception as exc:
        logger.exception("Unexpected error in export_safety_profile")
        return _error_dict(f"Unexpected error in export_safety_profile: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def validate_gcode_safe(
    commands: str,
    printer_id: str = "",
) -> dict:
    """Validate G-code commands with optional printer-specific safety limits.

    When *printer_id* is provided, uses that printer's safety profile
    (tighter limits for PTFE hotends, higher limits for high-speed
    printers, etc.).  Without a printer_id, uses conservative defaults.

    Args:
        commands: G-code commands separated by newlines.
        printer_id: Optional printer model ID for profile-aware validation.
    """
    if err := _check_auth("gcode"):
        return err
    try:
        if printer_id:
            result = validate_gcode_for_printer(commands, printer_id)
            profile = get_profile(printer_id)
            profile_info = {
                "id": profile.id,
                "display_name": profile.display_name,
            }
        else:
            result = _validate_gcode_impl(commands)
            profile_info = {"id": "default", "display_name": "Generic defaults"}

        return {
            "success": True,
            "valid": result.valid,
            "profile": profile_info,
            "commands_accepted": len(result.commands),
            "commands_blocked": len(result.blocked_commands),
            "warnings": result.warnings,
            "errors": result.errors,
            "blocked_commands": result.blocked_commands,
        }
    except Exception as exc:
        logger.exception("Unexpected error in validate_gcode_safe")
        return _error_dict(f"Unexpected error in validate_gcode_safe: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Slicer profile tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_slicer_profiles_tool() -> dict:
    """List all bundled slicer profiles for supported printers.

    Returns profile IDs, display names, recommended slicer, and the
    minimum license tier required for each.  Free-tier profiles can be
    used by everyone; PRO profiles require a Kiln Pro license.

    Use with ``get_slicer_profile_tool`` to see full settings, or
    ``slice_model`` with printer_id for auto-profile selection.
    """
    if err := _check_auth("slicer"):
        return err
    try:
        ids = list_slicer_profiles()
        profiles = []
        for pid in ids:
            try:
                p = get_slicer_profile(pid)
                profiles.append(
                    {
                        "id": p.id,
                        "display_name": p.display_name,
                        "slicer": p.slicer,
                        "tier": p.tier,
                    }
                )
            except KeyError:
                continue
        return {"success": True, "count": len(profiles), "profiles": profiles}
    except Exception as exc:
        logger.exception("Unexpected error in list_slicer_profiles_tool")
        return _error_dict(f"Unexpected error in list_slicer_profiles_tool: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def get_slicer_profile_tool(printer_id: str) -> dict:
    """Get the full bundled slicer profile for a printer model.

    Returns all INI settings (layer height, speeds, temps, retraction, etc.)
    and the recommended slicer.  Free-tier profiles (default, ender3,
    prusa_mk3s, klipper_generic) are available to all users.  Premium
    profiles require a Kiln Pro license.

    Args:
        printer_id: Printer model identifier (e.g. ``"ender3"``,
            ``"bambu_x1c"``).
    """
    if err := _check_auth("slicer"):
        return err
    try:
        profile = get_slicer_profile(printer_id)

        # Gate premium profiles behind PRO license
        if profile.tier == "pro":
            ok, message = check_tier(LicenseTier.PRO)
            if not ok:
                return {
                    "success": False,
                    "error": (
                        f"The '{profile.display_name}' slicer profile requires a Kiln Pro license. "
                        f"Free-tier profiles available: default, ender3, prusa_mk3s, klipper_generic. "
                        f"Upgrade at https://kiln3d.com/pro or run 'kiln upgrade'."
                    ),
                    "code": "LICENSE_REQUIRED",
                    "required_tier": "pro",
                    "upgrade_url": "https://kiln3d.com/pro",
                }

        return {"success": True, "profile": slicer_profile_to_dict(profile)}
    except KeyError:
        return _error_dict(
            f"No slicer profile for '{printer_id}' and no default available.",
            code="NOT_FOUND",
        )
    except Exception as exc:
        logger.exception("Unexpected error in get_slicer_profile_tool")
        return _error_dict(f"Unexpected error in get_slicer_profile_tool: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Printer intelligence tools
# ---------------------------------------------------------------------------


@mcp.tool()
def get_printer_intelligence(printer_id: str) -> dict:
    """Get operational intelligence for a printer: firmware quirks, material
    compatibility, calibration guidance, and known failure modes.

    This is the knowledge base that helps you make informed decisions about
    print settings, troubleshooting, and calibration without trial-and-error.

    Args:
        printer_id: Printer model identifier (e.g. ``"ender3"``,
            ``"bambu_x1c"``, ``"voron_2"``).
    """
    if err := _check_auth("intel"):
        return err
    try:
        intel = get_printer_intel(printer_id)
        return {"success": True, "intel": intel_to_dict(intel)}
    except KeyError:
        return _error_dict(
            f"No intelligence data for '{printer_id}'.",
            code="NOT_FOUND",
        )
    except Exception as exc:
        logger.exception("Unexpected error in get_printer_intelligence")
        return _error_dict(f"Unexpected error in get_printer_intelligence: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def get_material_recommendation(
    printer_id: str,
    material: str,
) -> dict:
    """Get recommended print settings for a specific material on a specific printer.

    Returns hotend temperature, bed temperature, fan speed, and
    material-specific tips.

    Args:
        printer_id: Printer model identifier.
        material: Material name (e.g. ``"PLA"``, ``"PETG"``, ``"ABS"``,
            ``"TPU"``).
    """
    if err := _check_auth("intel"):
        return err
    try:
        mp = get_material_settings(printer_id, material)
        if mp is None:
            intel = get_printer_intel(printer_id)
            available = list(intel.materials.keys())
            return _error_dict(
                f"No settings for '{material}' on {intel.display_name}. Available: {', '.join(available)}",
                code="NOT_FOUND",
            )
        intel = get_printer_intel(printer_id)
        return {
            "success": True,
            "printer": intel.display_name,
            "material": material.upper(),
            "hotend_temp": mp.hotend,
            "bed_temp": mp.bed,
            "fan_speed": mp.fan,
            "notes": mp.notes,
        }
    except KeyError:
        return _error_dict(
            f"No intelligence data for '{printer_id}'.",
            code="NOT_FOUND",
        )
    except Exception as exc:
        logger.exception("Unexpected error in get_material_recommendation")
        return _error_dict(f"Unexpected error in get_material_recommendation: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def troubleshoot_printer(
    printer_id: str,
    symptom: str,
) -> dict:
    """Diagnose a printer issue by searching the known failure modes database.

    Describe the symptom (e.g. ``"under-extrusion"``, ``"layer shifting"``,
    ``"stringing"``) and get possible causes and fixes specific to your
    printer model.

    Args:
        printer_id: Printer model identifier.
        symptom: Description of the problem.
    """
    if err := _check_auth("intel"):
        return err
    try:
        matches = diagnose_issue(printer_id, symptom)
        intel = get_printer_intel(printer_id)
        return {
            "success": True,
            "printer": intel.display_name,
            "symptom": symptom,
            "matches": matches,
            "count": len(matches),
            "quirks": intel.quirks,
        }
    except KeyError:
        return _error_dict(
            f"No intelligence data for '{printer_id}'.",
            code="NOT_FOUND",
        )
    except Exception as exc:
        logger.exception("Unexpected error in troubleshoot_printer")
        return _error_dict(f"Unexpected error in troubleshoot_printer: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Pipeline tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_print_pipelines() -> dict:
    """List all available pre-validated print pipelines.

    Pipelines are named command sequences that chain multiple operations
    into reliable one-shot workflows (e.g. quick_print, calibrate, benchmark).
    """
    if err := _check_auth("pipeline"):
        return err
    return {"success": True, "pipelines": _list_pipelines()}


@mcp.tool()
def run_quick_print(
    model_path: str,
    printer_name: str | None = None,
    printer_id: str | None = None,
    profile_path: str | None = None,
) -> dict:
    """Slice → validate → upload → print in one shot.

    The full quick-print pipeline:
    1. Resolve slicer profile (bundled, by printer_id)
    2. Slice the model to G-code
    3. Safety-validate the G-code against printer limits
    4. Upload G-code to the printer
    5. Run preflight checks (always — cannot be skipped)
    6. Start printing

    Args:
        model_path: Path to input model (STL, 3MF, STEP, OBJ).
        printer_name: Registered printer name in fleet.
        printer_id: Printer model ID for auto-profile selection
            (e.g. ``"ender3"``, ``"bambu_x1c"``, ``"klipper_generic"``).
        profile_path: Explicit slicer profile. Overrides printer_id auto-selection.
    """
    if err := _check_auth("print"):
        return err
    try:
        result = _pipeline_quick_print(
            model_path=model_path,
            printer_name=printer_name,
            printer_id=printer_id,
            profile_path=profile_path,
        )
        return {"success": result.success, **result.to_dict()}
    except Exception as exc:
        logger.exception("Unexpected error in run_quick_print")
        return _error_dict(f"Unexpected error in run_quick_print: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def run_calibrate(
    printer_name: str | None = None,
    printer_id: str | None = None,
) -> dict:
    """Run a printer calibration sequence: home → bed level → guidance.

    Performs physical calibration steps (homing, auto bed leveling) and
    returns printer-specific calibration guidance from the intelligence
    database.

    Args:
        printer_name: Registered printer name.
        printer_id: Printer model ID for calibration guidance.
    """
    if err := _check_auth("calibrate"):
        return err
    try:
        result = _pipeline_calibrate(
            printer_name=printer_name,
            printer_id=printer_id,
        )
        return {"success": result.success, **result.to_dict()}
    except Exception as exc:
        logger.exception("Unexpected error in run_calibrate")
        return _error_dict(f"Unexpected error in run_calibrate: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def run_benchmark(
    model_path: str,
    printer_name: str | None = None,
    printer_id: str | None = None,
    profile_path: str | None = None,
) -> dict:
    """Prepare a benchmark print: slice → upload → report stats.

    Slices a model with the printer's profile and uploads it, then
    reports printer stats from history. The print is NOT started
    automatically — benchmarks should be manually observed.

    Args:
        model_path: Path to benchmark model (STL).
        printer_name: Registered printer name.
        printer_id: Printer model ID for profile selection.
        profile_path: Explicit slicer profile path.
    """
    if err := _check_auth("print"):
        return err
    try:
        result = _pipeline_benchmark(
            model_path=model_path,
            printer_name=printer_name,
            printer_id=printer_id,
            profile_path=profile_path,
        )
        return {"success": result.success, **result.to_dict()}
    except Exception as exc:
        logger.exception("Unexpected error in run_benchmark")
        return _error_dict(f"Unexpected error in run_benchmark: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Pipeline execution control tools
# ---------------------------------------------------------------------------


@mcp.tool()
def pipeline_status(execution_id: str) -> dict:
    """Get the current state of a pipeline execution.

    Returns the execution state (running/paused/completed/failed/aborted),
    completed steps, and the name of the next step to run.

    Args:
        execution_id: The pipeline execution ID returned when starting a pipeline.
    """
    if err := _check_auth("pipeline"):
        return err
    ex = _get_execution(execution_id)
    if ex is None:
        return _error_dict(
            f"No pipeline execution found with id '{execution_id}'",
            code="NOT_FOUND",
        )
    return {"success": True, **ex.status_dict()}


@mcp.tool()
def pipeline_pause(execution_id: str) -> dict:
    """Pause a running pipeline at the next step boundary.

    The pipeline will finish the current step and then pause before
    starting the next one.  Use ``pipeline_resume`` to continue.

    Args:
        execution_id: The pipeline execution ID.
    """
    if err := _check_auth("pipeline"):
        return err
    ex = _get_execution(execution_id)
    if ex is None:
        return _error_dict(
            f"No pipeline execution found with id '{execution_id}'",
            code="NOT_FOUND",
        )
    if ex.state != _PipelineState.RUNNING:
        return _error_dict(
            f"Cannot pause: pipeline state is {ex.state.value}",
            code="INVALID_STATE",
        )
    ex.pause()
    return {"success": True, "message": "Pause requested. Pipeline will pause before the next step."}


@mcp.tool()
def pipeline_resume(execution_id: str) -> dict:
    """Resume a paused pipeline from where it stopped.

    Continues executing from the next unfinished step.

    Args:
        execution_id: The pipeline execution ID.
    """
    if err := _check_auth("pipeline"):
        return err
    ex = _get_execution(execution_id)
    if ex is None:
        return _error_dict(
            f"No pipeline execution found with id '{execution_id}'",
            code="NOT_FOUND",
        )
    if ex.state != _PipelineState.PAUSED:
        return _error_dict(
            f"Cannot resume: pipeline state is {ex.state.value}",
            code="INVALID_STATE",
        )
    result = ex.resume()
    return {"success": result.success, **result.to_dict()}


@mcp.tool()
def pipeline_abort(execution_id: str) -> dict:
    """Abort a running or paused pipeline.

    Immediately marks the pipeline as aborted. Any completed steps
    are preserved in the result.

    Args:
        execution_id: The pipeline execution ID.
    """
    if err := _check_auth("pipeline"):
        return err
    ex = _get_execution(execution_id)
    if ex is None:
        return _error_dict(
            f"No pipeline execution found with id '{execution_id}'",
            code="NOT_FOUND",
        )
    if ex.state in (_PipelineState.COMPLETED, _PipelineState.ABORTED):
        return _error_dict(
            f"Cannot abort: pipeline state is {ex.state.value}",
            code="INVALID_STATE",
        )
    result = ex.abort()
    return {"success": False, **result.to_dict()}


@mcp.tool()
def pipeline_retry_step(execution_id: str, step_index: int) -> dict:
    """Retry a specific failed step in a pipeline, then continue from there.

    Re-runs the step at the given index and, if it succeeds, continues
    executing the remaining steps.

    Args:
        execution_id: The pipeline execution ID.
        step_index: Zero-based index of the step to retry.
    """
    if err := _check_auth("pipeline"):
        return err
    ex = _get_execution(execution_id)
    if ex is None:
        return _error_dict(
            f"No pipeline execution found with id '{execution_id}'",
            code="NOT_FOUND",
        )
    if ex.state not in (_PipelineState.FAILED, _PipelineState.PAUSED):
        return _error_dict(
            f"Cannot retry: pipeline state is {ex.state.value} (must be failed or paused)",
            code="INVALID_STATE",
        )
    result = ex.retry_step(step_index)
    return {"success": result.success, **result.to_dict()}


# ---------------------------------------------------------------------------
# Model cache tools
# ---------------------------------------------------------------------------


@mcp.tool()
def cache_model(
    file_path: str,
    source: str,
    source_id: str | None = None,
    prompt: str | None = None,
    tags: str | None = None,
    dimensions: str | None = None,
    metadata: str | None = None,
) -> dict:
    """Add a 3D model file to the local cache for reuse across jobs.

    Copies the file into ``~/.kiln/model_cache/`` and stores metadata
    (source, prompt, tags, dimensions) in the database.  Duplicate files
    are detected automatically by SHA-256 hash.

    Args:
        file_path: Path to the model file on disk.
        source: Origin — ``"thingiverse"``, ``"myminifactory"``, ``"meshy"``,
            ``"openscad"``, ``"upload"``, etc.
        source_id: Marketplace thing ID or generation job ID.
        prompt: For generated models, the text prompt used.
        tags: Comma-separated tags (e.g. ``"benchy,calibration,test"``).
        dimensions: JSON object with bounding box in mm, e.g.
            ``'{"x": 60, "y": 31, "z": 48}'``.
        metadata: Optional JSON object with extra data.
    """
    if err := _check_auth("cache"):
        return err
    try:
        import json as _json

        from kiln.model_cache import get_model_cache

        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        dim_dict = _json.loads(dimensions) if dimensions else None
        meta_dict = _json.loads(metadata) if metadata else None

        cache = get_model_cache()
        entry = cache.add(
            file_path,
            source=source,
            source_id=source_id,
            prompt=prompt,
            tags=tag_list,
            dimensions=dim_dict,
            metadata=meta_dict,
        )
        return {"success": True, "entry": entry.to_dict()}
    except FileNotFoundError as exc:
        return _error_dict(f"Failed to cache model: {exc}", code="NOT_FOUND")
    except (ValueError, _json.JSONDecodeError) as exc:
        return _error_dict(f"Failed to cache model: {exc}", code="VALIDATION_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in cache_model")
        return _error_dict(f"Unexpected error in cache_model: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def search_cached_models(
    query: str | None = None,
    source: str | None = None,
    tags: str | None = None,
    limit: int = 20,
) -> dict:
    """Search the local model cache by name, source, tags, or prompt text.

    Args:
        query: Free-text search against file name, prompt, and tags.
        source: Filter by source (e.g. ``"thingiverse"``).
        tags: Comma-separated tags to filter by.
        limit: Maximum results (default 20).
    """
    if err := _check_auth("cache"):
        return err
    try:
        from kiln.model_cache import get_model_cache

        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        cache = get_model_cache()
        entries = cache.search(query=query, source=source, tags=tag_list, limit=limit)
        return {
            "success": True,
            "entries": [e.to_dict() for e in entries],
            "count": len(entries),
        }
    except Exception as exc:
        logger.exception("Unexpected error in search_cached_models")
        return _error_dict(f"Unexpected error in search_cached_models: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def get_cached_model(cache_id: str) -> dict:
    """Return details for a specific cached model.

    Args:
        cache_id: The unique cache ID of the model.
    """
    if err := _check_auth("cache"):
        return err
    try:
        from kiln.model_cache import get_model_cache

        entry = get_model_cache().get(cache_id)
        if entry is None:
            return _error_dict(f"No cached model with id {cache_id!r}.", code="NOT_FOUND")
        return {"success": True, "entry": entry.to_dict()}
    except Exception as exc:
        logger.exception("Unexpected error in get_cached_model")
        return _error_dict(f"Unexpected error in get_cached_model: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def list_cached_models(limit: int = 50, offset: int = 0) -> dict:
    """List all models in the local cache, newest first.

    Args:
        limit: Maximum results (default 50).
        offset: Number of entries to skip for pagination.
    """
    if err := _check_auth("cache"):
        return err
    try:
        from kiln.model_cache import get_model_cache

        entries = get_model_cache().list_all(limit=limit, offset=offset)
        return {
            "success": True,
            "entries": [e.to_dict() for e in entries],
            "count": len(entries),
        }
    except Exception as exc:
        logger.exception("Unexpected error in list_cached_models")
        return _error_dict(f"Unexpected error in list_cached_models: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def delete_cached_model(cache_id: str) -> dict:
    """Remove a model from the local cache (file and metadata).

    Args:
        cache_id: The unique cache ID of the model to delete.
    """
    if err := _check_auth("cache"):
        return err
    try:
        from kiln.model_cache import get_model_cache

        deleted = get_model_cache().delete(cache_id)
        if not deleted:
            return _error_dict(f"No cached model with id {cache_id!r}.", code="NOT_FOUND")
        return {"success": True, "cache_id": cache_id}
    except Exception as exc:
        logger.exception("Unexpected error in delete_cached_model")
        return _error_dict(f"Unexpected error in delete_cached_model: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Database backup tool
# ---------------------------------------------------------------------------


@mcp.tool()
def backup_database(
    output_path: str | None = None,
    redact: bool = True,
) -> dict:
    """Back up the Kiln database with optional credential redaction.

    Creates a copy of the SQLite database.  By default, sensitive fields
    (API keys, access codes, payment refs) are replaced with "REDACTED"
    in the backup.

    Args:
        output_path: Destination file path.  Defaults to
            ``~/.kiln/backups/kiln-YYYYMMDD-HHMMSS.db``.
        redact: If ``True`` (default), redact credentials in the backup.
    """
    auth_err = _check_auth("admin")
    if auth_err:
        return auth_err
    try:
        db = get_db()
        result_path = _backup_db(
            db.path,
            output_path,
            redact_credentials=redact,
        )
        return {
            "success": True,
            "backup_path": result_path,
            "redacted": redact,
        }
    except BackupError as exc:
        return _error_dict(f"Failed to back up database: {exc}", code="BACKUP_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in backup_database")
        return _error_dict(f"Unexpected error in backup_database: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Audit integrity verification tool
# ---------------------------------------------------------------------------


@mcp.tool()
def verify_audit_integrity() -> dict:
    """Verify HMAC signatures on all safety audit log entries.

    Checks each audit log row against its stored HMAC signature to
    detect tampering.  Returns counts of valid, invalid, and total
    entries along with an overall integrity status.
    """
    auth_err = _check_auth("admin")
    if auth_err:
        return auth_err
    try:
        db = get_db()
        result = db.verify_audit_log()
        return {
            "success": True,
            **result,
        }
    except Exception as exc:
        logger.exception("Unexpected error in verify_audit_integrity")
        return _error_dict(f"Unexpected error in verify_audit_integrity: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Trusted printers whitelist tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_trusted_printers() -> dict:
    """Return the list of trusted printer hostnames/IPs.

    Trusted printers are used to flag discovered printers that have been
    explicitly approved by the user, preventing spoofed-printer attacks.
    """
    if err := _check_auth("config"):
        return err
    try:
        from kiln.cli.config import get_trusted_printers

        trusted = get_trusted_printers()
        return {"success": True, "trusted_printers": trusted, "count": len(trusted)}
    except Exception as exc:
        logger.exception("Unexpected error in list_trusted_printers")
        return _error_dict(f"Unexpected error in list_trusted_printers: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def trust_printer(host: str) -> dict:
    """Add a printer hostname/IP to the trusted whitelist.

    Trusted printers are flagged during network discovery.  Connecting
    to an untrusted printer should raise a warning.

    Args:
        host: The hostname or IP address to trust.
    """
    if err := _check_auth("config"):
        return err
    try:
        from kiln.cli.config import add_trusted_printer

        add_trusted_printer(host)
        return {"success": True, "host": host}
    except ValueError as exc:
        return _error_dict(f"Failed to trust printer: {exc}", code="VALIDATION_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in trust_printer")
        return _error_dict(f"Unexpected error in trust_printer: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def untrust_printer(host: str) -> dict:
    """Remove a printer hostname/IP from the trusted whitelist.

    Args:
        host: The hostname or IP address to untrust.
    """
    if err := _check_auth("config"):
        return err
    try:
        from kiln.cli.config import remove_trusted_printer

        remove_trusted_printer(host)
        return {"success": True, "host": host}
    except ValueError as exc:
        return _error_dict(f"Failed to untrust printer: {exc}", code="NOT_FOUND")
    except Exception as exc:
        logger.exception("Unexpected error in untrust_printer")
        return _error_dict(f"Unexpected error in untrust_printer: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def get_skill_manifest() -> dict:
    """Get the Kiln skill manifest for agent self-discovery.

    Returns a machine-readable description of Kiln's capabilities,
    configuration requirements, available interfaces, and setup
    instructions.  Use this when first connecting to understand what
    Kiln can do and what configuration is needed.
    """
    try:
        from kiln.skill_manifest import generate_manifest

        manifest = generate_manifest()
        return {"status": "success", "data": manifest.to_dict()}
    except Exception as exc:
        logger.exception("Unexpected error in get_skill_manifest")
        return _error_dict(f"Failed to generate manifest: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Kiln MCP server."""
    # Load .env file if present (project root or ~/.kiln/.env).
    try:
        from dotenv import load_dotenv

        load_dotenv()  # loads .env from cwd first
        load_dotenv(Path.home() / ".kiln" / ".env")  # then ~/.kiln/.env
    except ImportError:
        pass

    # Re-snapshot env-backed config vars — they were read at import time
    # before .env was loaded, so they may have stale defaults.
    _reload_env_config()

    # Configure structured logging if requested (before any log calls).
    _configure_logging()

    # Set up log rotation and sensitive data scrubbing.
    _configure_log_rotation()

    # Auto-register the env-configured printer so the scheduler can
    # dispatch jobs even if no explicit register_printer call is made.
    if _PRINTER_HOST and _registry.count == 0:
        try:
            adapter = _get_adapter()
            _registry.register("default", adapter)
            logger.info("Auto-registered env-configured printer as 'default'")
        except Exception as exc:
            logger.debug(
                "Could not auto-register env-configured printer: %s",
                _sanitize_log_msg(str(exc)),
            )

    # Auto-register marketplace adapters from env credentials
    _init_marketplace_registry()
    if _marketplace_registry.count > 0:
        logger.info("Marketplace sources: %s", ", ".join(_marketplace_registry.connected))

    # Subscribe bed level manager to job events
    _bed_level_mgr.subscribe_events()

    # Discover and activate third-party plugins (entry-point based)
    _plugin_mgr.discover()
    _plugin_mgr.activate_all(
        PluginContext(
            event_bus=_event_bus,
            registry=_registry,
            queue=_queue,
            mcp=mcp,
            db=get_db(),
        )
    )

    # Load internal tool plugins from kiln/plugins/.
    # Tools are being migrated to kiln/plugins/ for modularity — each
    # plugin module registers its own tools via the ToolPlugin protocol.
    # See kiln/plugins/marketplace_tools.py for the migration pattern.
    register_all_plugins(mcp, plugin_package="kiln.plugins")

    # Initialise cloud sync from saved config
    global _cloud_sync
    _saved_sync = get_db().get_setting("cloud_sync_config")
    if _saved_sync:
        import json as _json

        try:
            _cloud_sync = CloudSyncManager(
                db=get_db(),
                event_bus=_event_bus,
                config=SyncConfig.from_dict(_json.loads(_saved_sync)),
            )
            _cloud_sync.start()
        except Exception:
            logger.debug("Could not restore cloud sync config", exc_info=True)

    # Warn if auth is disabled
    auth_enabled = os.environ.get("KILN_AUTH_ENABLED", "").lower() in ("1", "true", "yes")
    if not auth_enabled:
        msg = (
            "WARNING: Authentication is DISABLED. Anyone with network access "
            "can control your printer. Set KILN_AUTH_ENABLED=true and "
            "configure API keys for production use."
        )
        logger.warning(msg)
        print(f"\n  ⚠  {msg}\n", file=sys.stderr)

    # Start background services
    _scheduler.start()
    _webhook_mgr.start()
    _heater_watchdog.start()
    logger.info("Kiln scheduler, webhook delivery, and heater watchdog started")

    # Graceful shutdown handler
    def _shutdown_handler(signum: int, frame: Any) -> None:
        try:
            sig_name = signal.Signals(signum).name
        except (ValueError, AttributeError):
            sig_name = f"signal {signum}"
        logger.info("Received %s — shutting down gracefully...", sig_name)
        _scheduler.stop()
        _webhook_mgr.stop()
        _heater_watchdog.stop()
        _stream_proxy.stop()
        if _cloud_sync is not None:
            _cloud_sync.stop()
        # Stop all active print watchers
        for wid in list(_watchers):
            try:
                _watchers.pop(wid).stop()
            except Exception as exc:
                logger.debug("Failed to stop watcher %s during shutdown: %s", wid, exc)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    # Atexit as fallback
    atexit.register(_scheduler.stop)
    atexit.register(_webhook_mgr.stop)
    atexit.register(_heater_watchdog.stop)
    atexit.register(_stream_proxy.stop)
    if _cloud_sync is not None:
        atexit.register(_cloud_sync.stop)

    def _stop_all_watchers() -> None:
        for wid in list(_watchers):
            try:
                _watchers.pop(wid).stop()
            except Exception as exc:
                logger.debug("Failed to stop watcher %s during atexit: %s", wid, exc)

    atexit.register(_stop_all_watchers)

    mcp.run()


# ---------------------------------------------------------------------------
# Ported Forge tools — material substitution, recovery, health monitoring,
# credential management, design caching, job routing, fleet orchestration,
# file metadata, progress estimation, emergency stop, state locking,
# snapshot analysis, quote caching, firmware management
# ---------------------------------------------------------------------------


@mcp.tool()
def find_material_substitute(
    material: str,
    *,
    reason: str | None = None,
    min_score: float = 0.5,
) -> dict:
    """Find substitute filament materials when your preferred material is unavailable.

    Checks a built-in knowledge base of FDM filament compatibility and returns
    ranked alternatives with trade-off descriptions.

    Args:
        material: The original filament material (e.g. "PLA", "PETG", "ABS").
        reason: Optional filter — only return substitutions matching this reason
            (unavailable, cost, strength, finish_quality, heat_resistance, lead_time).
        min_score: Minimum compatibility score threshold (0.0–1.0).
    """
    try:
        from kiln.material_substitution import (
            SubstitutionReason,
            find_substitutes,
        )

        reason_enum = None
        if reason:
            try:
                reason_enum = SubstitutionReason(reason.lower())
            except ValueError:
                return _error_dict(
                    f"Invalid reason: {reason!r}. Valid: {[r.value for r in SubstitutionReason]}",
                    code="VALIDATION_ERROR",
                )

        subs = find_substitutes(material, "fdm", reason=reason_enum, min_score=min_score)
        return {
            "success": True,
            "material": material,
            "substitutes": [s.to_dict() for s in subs],
            "count": len(subs),
        }
    except Exception as exc:
        logger.exception("Error in find_material_substitute")
        return _error_dict(f"Failed to find material substitutes: {exc}", code="SUBSTITUTION_ERROR")


@mcp.tool()
def get_best_material_substitute(material: str) -> dict:
    """Get the single best substitute for a filament material.

    Args:
        material: The original filament material (e.g. "PLA", "PETG").
    """
    try:
        from kiln.material_substitution import get_best_substitute

        sub = get_best_substitute(material, "fdm")
        if sub is None:
            return {"success": True, "material": material, "substitute": None, "message": "No substitutes found"}
        return {"success": True, "material": material, "substitute": sub.to_dict()}
    except Exception as exc:
        logger.exception("Error in get_best_material_substitute")
        return _error_dict(f"Failed to get best material substitute: {exc}", code="SUBSTITUTION_ERROR")


@mcp.tool()
def extract_file_metadata(file_path: str) -> dict:
    """Extract metadata from a 3D printing file (.gcode, .3mf, .stl, .ufp).

    Parses file headers for estimated print time, layer count, filament usage,
    dimensions, slicer info, and material hints — without re-slicing.

    Args:
        file_path: Path to the print file.
    """
    try:
        from kiln.file_metadata import extract_metadata

        meta = extract_metadata(file_path)
        return {"success": True, "metadata": meta.to_dict()}
    except Exception as exc:
        logger.exception("Error in extract_file_metadata")
        return _error_dict(f"Failed to extract file metadata: {exc}", code="FILE_METADATA_ERROR")


@mcp.tool()
def save_print_checkpoint(
    printer_name: str,
    job_id: str,
    *,
    z_height: float | None = None,
    layer_number: int | None = None,
    hotend_temp: float | None = None,
    bed_temp: float | None = None,
    filament_used_mm: float | None = None,
) -> dict:
    """Save a checkpoint during an active print for crash recovery.

    If the print fails or power is lost, the checkpoint data can be used
    to plan a recovery strategy.

    Args:
        printer_name: Name of the printer running the job.
        job_id: Unique job identifier.
        z_height: Current Z height in mm.
        layer_number: Current layer number.
        hotend_temp: Hotend temperature at checkpoint time.
        bed_temp: Bed temperature at checkpoint time.
        filament_used_mm: Filament consumed so far in mm.
    """
    try:
        from kiln.recovery import get_recovery_manager

        mgr = get_recovery_manager()
        cp = mgr.save_checkpoint(
            printer_name=printer_name,
            job_id=job_id,
            z_height=z_height,
            layer_number=layer_number,
            hotend_temp=hotend_temp,
            bed_temp=bed_temp,
            filament_used_mm=filament_used_mm,
        )
        return {"success": True, "checkpoint": cp.to_dict()}
    except Exception as exc:
        logger.exception("Error in save_print_checkpoint")
        return _error_dict(f"Failed to save print checkpoint: {exc}", code="CHECKPOINT_ERROR")


@mcp.tool()
def plan_print_recovery(
    printer_name: str,
    job_id: str,
    *,
    failure_type: str | None = None,
) -> dict:
    """Plan a recovery strategy after a print failure.

    Analyzes the last checkpoint and failure type to recommend whether to
    resume, restart, or abort the print.

    Args:
        printer_name: Name of the printer that failed.
        job_id: The failed job's identifier.
        failure_type: Type of failure (power_loss, filament_runout, nozzle_clog,
            bed_adhesion, thermal_runaway, layer_shift, first_layer_failure).
    """
    try:
        from kiln.recovery import get_recovery_manager

        mgr = get_recovery_manager()
        rec = mgr.plan_recovery(
            printer_name=printer_name,
            job_id=job_id,
            failure_type=failure_type,
        )
        return {"success": True, "recommendation": rec.to_dict()}
    except Exception as exc:
        logger.exception("Error in plan_print_recovery")
        return _error_dict(f"Failed to plan print recovery: {exc}", code="RECOVERY_ERROR")


@mcp.tool()
def firmware_resume_print(
    printer_name: str,
    job_id: str,
    *,
    z_height_mm: float,
    hotend_temp_c: float,
    bed_temp_c: float,
    file_name: str,
    layer_number: int | None = None,
    fan_speed_pct: float = 100.0,
    flow_rate_pct: float = 100.0,
    prime_length_mm: float = 30.0,
    z_clearance_mm: float = 2.0,
) -> dict:
    """Execute firmware-level print resume for OctoPrint+Marlin printers.

    After a power loss or failure, this tool positions the printer at the
    last known checkpoint and prepares it to resume printing. Uses Marlin
    M413 power-loss recovery protocol: homes X/Y (never Z), re-heats bed
    then hotend, sets Z position from checkpoint, primes the nozzle, and
    restores fan/flow settings.

    The printer will be positioned and ready after this call. Use
    start_print with a re-sliced file (starting at the target layer) or
    let the printer resume from its own recovery buffer.

    Only works with OctoPrint printers running Marlin firmware. Moonraker/Klipper
    printers should use Klipper's SAVE_VARIABLE system instead (not yet supported).

    Args:
        printer_name: Name of the printer to resume on.
        job_id: The failed job's identifier (for checkpoint lookup).
        z_height_mm: Z height to resume from (from checkpoint).
        hotend_temp_c: Hotend temperature to restore.
        bed_temp_c: Bed temperature to restore.
        file_name: Original file name (for logging/tracking).
        layer_number: Layer number to resume from (informational).
        fan_speed_pct: Fan speed to restore (0-100).
        flow_rate_pct: Flow rate multiplier to restore (default 100).
        prime_length_mm: Filament to extrude for nozzle priming (mm).
        z_clearance_mm: How far above the part to raise the nozzle (mm).
    """
    try:
        adapter = _get_adapter(printer_name)

        # Verify this is an OctoPrint adapter (firmware resume is Marlin-specific)
        if adapter.name != "octoprint":
            return _error_dict(
                f"Firmware resume is only supported on OctoPrint+Marlin printers, "
                f"not {adapter.name}. For Klipper printers, use SAVE_VARIABLE.",
                code="UNSUPPORTED_ADAPTER",
            )

        result = adapter.firmware_resume_print(
            z_height_mm=z_height_mm,
            hotend_temp_c=hotend_temp_c,
            bed_temp_c=bed_temp_c,
            file_name=file_name,
            layer_number=layer_number,
            fan_speed_pct=fan_speed_pct,
            flow_rate_pct=flow_rate_pct,
            prime_length_mm=prime_length_mm,
            z_clearance_mm=z_clearance_mm,
        )

        # Log the recovery event
        from kiln.recovery import RecoveryStrategy, get_recovery_manager

        mgr = get_recovery_manager()
        mgr.execute_recovery(job_id, RecoveryStrategy.RESUME_FROM_CHECKPOINT)

        return {
            "success": True,
            "message": result.message,
            "printer": printer_name,
            "resumed_at_z": z_height_mm,
            "resumed_at_layer": layer_number,
            "file": file_name,
        }
    except Exception as exc:
        logger.exception("Error in firmware_resume_print")
        return _error_dict(f"Failed to resume print via firmware: {exc}", code="FIRMWARE_RESUME_ERROR")


@mcp.tool()
def check_printer_health(printer_name: str) -> dict:
    """Run a comprehensive health check on a printer.

    Monitors hotend/bed temperature stability, print progress, and
    detects anomalies like temperature drift or unexpected shutdowns.

    Args:
        printer_name: Name of the printer to check.
    """
    try:
        from kiln.print_health_monitor import get_print_health_monitor

        monitor = get_print_health_monitor()
        report = monitor.check_health(printer_name)
        return {"success": True, "health_report": report.to_dict()}
    except Exception as exc:
        logger.exception("Error in check_printer_health")
        return _error_dict(f"Failed to check printer health: {exc}", code="HEALTH_CHECK_ERROR")


@mcp.tool()
def start_printer_health_monitoring(
    printer_name: str,
    *,
    interval_seconds: int = 30,
) -> dict:
    """Start continuous background health monitoring for a printer.

    Args:
        printer_name: Printer to monitor.
        interval_seconds: Check interval in seconds (default 30).
    """
    try:
        from kiln.print_health_monitor import get_print_health_monitor

        monitor = get_print_health_monitor()
        monitor.start_monitoring(printer_name, interval_seconds=interval_seconds)
        return {"success": True, "printer": printer_name, "interval_seconds": interval_seconds}
    except Exception as exc:
        logger.exception("Error in start_printer_health_monitoring")
        return _error_dict(f"Failed to start health monitoring: {exc}", code="MONITORING_ERROR")


@mcp.tool()
def stop_printer_health_monitoring(printer_name: str) -> dict:
    """Stop background health monitoring for a printer.

    Args:
        printer_name: Printer to stop monitoring.
    """
    try:
        from kiln.print_health_monitor import get_print_health_monitor

        monitor = get_print_health_monitor()
        monitor.stop_monitoring(printer_name)
        return {"success": True, "printer": printer_name, "monitoring": "stopped"}
    except Exception as exc:
        logger.exception("Error in stop_printer_health_monitoring")
        return _error_dict(f"Failed to stop health monitoring: {exc}", code="MONITORING_ERROR")


@mcp.tool()
def estimate_print_progress(
    printer_name: str,
    *,
    elapsed_seconds: float | None = None,
    total_layers: int | None = None,
    current_layer: int | None = None,
) -> dict:
    """Estimate print progress with phase-aware time prediction.

    Breaks a print into phases (preparing, printing, cooling, post-processing)
    and estimates time remaining using historical data.

    Args:
        printer_name: Printer running the job.
        elapsed_seconds: Time elapsed since print start.
        total_layers: Total layer count for the job.
        current_layer: Current layer being printed.
    """
    try:
        from kiln.progress import get_progress_estimator

        estimator = get_progress_estimator()
        estimate = estimator.estimate(
            printer_name=printer_name,
            elapsed_seconds=elapsed_seconds,
            total_layers=total_layers,
            current_layer=current_layer,
        )
        return {"success": True, "progress": estimate.to_dict()}
    except Exception as exc:
        logger.exception("Error in estimate_print_progress")
        return _error_dict(f"Failed to estimate print progress: {exc}", code="PROGRESS_ERROR")


@mcp.tool()
def route_print_job(
    file_path: str,
    *,
    material: str | None = None,
    quality: str | None = None,
    priority: str | None = None,
) -> dict:
    """Route a print job to the best available printer in the fleet.

    Scores each printer based on material match, build volume, availability,
    and quality/speed preference, then recommends the optimal assignment.

    Args:
        file_path: Path to the file to print.
        material: Required filament material (e.g. "PLA", "PETG").
        quality: Quality preference — "draft", "standard", or "fine".
        priority: Job priority — "low", "normal", or "high".
    """
    try:
        from kiln.job_router import get_job_router

        router = get_job_router()
        result = router.route_job(
            file_path=file_path,
            material=material,
            quality=quality,
            priority=priority,
        )
        return {"success": True, "routing": result.to_dict()}
    except Exception as exc:
        logger.exception("Error in route_print_job")
        return _error_dict(f"Failed to route print job: {exc}", code="ROUTING_ERROR")


@mcp.tool()
def fleet_submit_job(
    file_path: str,
    *,
    printer_name: str | None = None,
    material: str | None = None,
    priority: str | None = None,
) -> dict:
    """Submit a print job to the fleet orchestrator.

    If no printer is specified, the orchestrator auto-assigns to the best
    available printer. Tracks the job through completion.

    Args:
        file_path: Path to the file to print.
        printer_name: Specific printer to assign to (auto-routes if None).
        material: Required filament material.
        priority: Job priority (low, normal, high).
    """
    try:
        from kiln.fleet_orchestrator import get_fleet_orchestrator

        orch = get_fleet_orchestrator()
        job = orch.submit_job(
            file_path=file_path,
            printer_name=printer_name,
            material=material,
            priority=priority,
        )
        return {"success": True, "job": job.to_dict()}
    except Exception as exc:
        logger.exception("Error in fleet_submit_job")
        return _error_dict(f"Failed to submit fleet job: {exc}", code="FLEET_ERROR")


@mcp.tool()
def fleet_job_status(job_id: str) -> dict:
    """Get the status of a fleet-managed print job.

    Args:
        job_id: The orchestrated job's identifier.
    """
    try:
        from kiln.fleet_orchestrator import get_fleet_orchestrator

        orch = get_fleet_orchestrator()
        job = orch.get_job_status(job_id)
        if job is None:
            return _error_dict(f"Job {job_id!r} not found", code="NOT_FOUND")
        return {"success": True, "job": job.to_dict()}
    except Exception as exc:
        logger.exception("Error in fleet_job_status")
        return _error_dict(f"Failed to get fleet job status: {exc}", code="FLEET_ERROR")


@mcp.tool()
def fleet_utilization() -> dict:
    """Get fleet utilization metrics across all registered printers.

    Returns busy/idle/offline counts and utilization percentage.
    """
    try:
        from kiln.fleet_orchestrator import get_fleet_orchestrator

        orch = get_fleet_orchestrator()
        util = orch.get_fleet_utilization()
        return {"success": True, "utilization": util}
    except Exception as exc:
        logger.exception("Error in fleet_utilization")
        return _error_dict(f"Failed to get fleet utilization: {exc}", code="FLEET_ERROR")


@mcp.tool()
def cache_design(
    file_path: str,
    *,
    label: str | None = None,
    material: str | None = None,
) -> dict:
    """Cache a 3D design file for faster access and version tracking.

    Args:
        file_path: Path to the design file to cache.
        label: Human-readable label for the cached design.
        material: Intended material for this design.
    """
    try:
        from kiln.design_cache import get_design_cache

        cache = get_design_cache()
        entry = cache.add(file_path, label=label, material=material)
        return {"success": True, "cached_design": entry.to_dict()}
    except Exception as exc:
        logger.exception("Error in cache_design")
        return _error_dict(f"Failed to cache design: {exc}", code="CACHE_ERROR")


@mcp.tool()
def list_cached_designs(
    *,
    material: str | None = None,
    limit: int = 50,
) -> dict:
    """List cached designs, optionally filtered by material.

    Args:
        material: Filter by material (e.g. "PLA", "PETG").
        limit: Maximum number of results.
    """
    try:
        from kiln.design_cache import get_design_cache

        cache = get_design_cache()
        designs = cache.list_designs(material=material, limit=limit)
        return {
            "success": True,
            "designs": [d.to_dict() for d in designs],
            "count": len(designs),
        }
    except Exception as exc:
        logger.exception("Error in list_cached_designs")
        return _error_dict(f"Failed to list cached designs: {exc}", code="CACHE_ERROR")


@mcp.tool()
def get_cached_design(design_id: str) -> dict:
    """Retrieve a cached design by ID.

    Args:
        design_id: The cached design's identifier.
    """
    try:
        from kiln.design_cache import get_design_cache

        cache = get_design_cache()
        entry = cache.get(design_id)
        if entry is None:
            return _error_dict(f"Design {design_id!r} not found", code="NOT_FOUND")
        return {"success": True, "design": entry.to_dict()}
    except Exception as exc:
        logger.exception("Error in get_cached_design")
        return _error_dict(f"Failed to get cached design: {exc}", code="CACHE_ERROR")


@mcp.tool()
def store_credential(
    credential_type: str,
    value: str,
    *,
    label: str = "",
) -> dict:
    """Encrypt and store a credential (API key, webhook secret, etc.).

    The value is encrypted at rest using PBKDF2 + XOR stream encryption.
    Only metadata is returned — the plaintext is never exposed.

    Args:
        credential_type: Type of credential (api_key, webhook_secret,
            stripe_key, marketplace_token, printer_password).
        value: The plaintext secret to store.
        label: Human-readable description.
    """
    try:
        from kiln.credential_store import CredentialType
        from kiln.credential_store import store_credential as _store

        try:
            ctype = CredentialType(credential_type)
        except ValueError:
            return _error_dict(
                f"Invalid type: {credential_type!r}. Valid: {[t.value for t in CredentialType]}",
                code="VALIDATION_ERROR",
            )
        cred = _store(ctype, value, label=label)
        return {"success": True, "credential": cred.to_dict()}
    except Exception as exc:
        logger.exception("Error in store_credential")
        return _error_dict(f"Failed to store credential: {exc}", code="CREDENTIAL_ERROR")


@mcp.tool()
def list_credentials() -> dict:
    """List all stored credentials (metadata only, no plaintext)."""
    try:
        from kiln.credential_store import get_credential_store

        store = get_credential_store()
        creds = store.list_credentials()
        return {
            "success": True,
            "credentials": [c.to_dict() for c in creds],
            "count": len(creds),
        }
    except Exception as exc:
        logger.exception("Error in list_credentials")
        return _error_dict(f"Failed to list credentials: {exc}", code="CREDENTIAL_ERROR")


@mcp.tool()
def retrieve_credential(credential_id: str) -> dict:
    """Decrypt and return a stored credential.

    Args:
        credential_id: The credential's unique identifier.
    """
    try:
        from kiln.credential_store import retrieve_credential as _retrieve

        value = _retrieve(credential_id)
        return {"success": True, "credential_id": credential_id, "value": value}
    except Exception as exc:
        logger.exception("Error in retrieve_credential")
        return _error_dict(f"Failed to retrieve credential: {exc}", code="CREDENTIAL_ERROR")


@mcp.tool()
def analyze_print_snapshot(file_path: str) -> dict:
    """Analyze a webcam snapshot for print monitoring quality.

    Checks image brightness, variance, resolution, and format to determine
    if the snapshot is usable for print monitoring.

    Args:
        file_path: Path to the snapshot image file.
    """
    try:
        from kiln.snapshot_analysis import analyze_snapshot

        result = analyze_snapshot(file_path)
        return {"success": True, "analysis": result.to_dict()}
    except Exception as exc:
        logger.exception("Error in analyze_print_snapshot")
        return _error_dict(f"Failed to analyze snapshot: {exc}", code="SNAPSHOT_ERROR")


@mcp.tool()
def acquire_printer_lock(
    printer_name: str,
    *,
    holder: str = "agent",
    timeout_seconds: float = 30.0,
) -> dict:
    """Acquire an exclusive lock on a printer for safe concurrent access.

    Prevents multiple agents from controlling the same printer simultaneously.

    Args:
        printer_name: Printer to lock.
        holder: Identifier of the lock holder.
        timeout_seconds: Maximum time to wait for the lock.
    """
    try:
        from kiln.state_lock import get_state_lock_manager

        mgr = get_state_lock_manager()
        acquired = mgr.acquire(printer_name, holder=holder, timeout=timeout_seconds)
        if not acquired:
            return _error_dict(
                f"Could not acquire lock on {printer_name!r} within {timeout_seconds}s",
                code="LOCK_TIMEOUT",
            )
        return {"success": True, "printer": printer_name, "holder": holder, "locked": True}
    except Exception as exc:
        logger.exception("Error in acquire_printer_lock")
        return _error_dict(f"Failed to acquire printer lock: {exc}", code="LOCK_ERROR")


@mcp.tool()
def release_printer_lock(printer_name: str, *, holder: str = "agent") -> dict:
    """Release an exclusive lock on a printer.

    Args:
        printer_name: Printer to unlock.
        holder: Identifier of the lock holder (must match acquire).
    """
    try:
        from kiln.state_lock import get_state_lock_manager

        mgr = get_state_lock_manager()
        released = mgr.release(printer_name, holder=holder)
        return {"success": True, "printer": printer_name, "released": released}
    except Exception as exc:
        logger.exception("Error in release_printer_lock")
        return _error_dict(f"Failed to release printer lock: {exc}", code="LOCK_ERROR")


@mcp.tool()
def get_fulfillment_quote_cached(
    file_path: str,
    *,
    provider: str | None = None,
    material: str | None = None,
) -> dict:
    """Get a cached fulfillment provider quote (or fetch fresh if expired).

    Uses TTL-based caching to avoid redundant provider API calls.

    Args:
        file_path: Path to the design file.
        provider: Fulfillment provider name.
        material: Material specification.
    """
    try:
        from kiln.quote_cache import get_quote_cache

        cache = get_quote_cache()
        quote = cache.get_quote(file_path, provider=provider, material=material)
        if quote is None:
            return {"success": True, "quote": None, "message": "No cached quote available"}
        return {"success": True, "quote": quote.to_dict()}
    except Exception as exc:
        logger.exception("Error in get_fulfillment_quote_cached")
        return _error_dict(f"Failed to get cached quote: {exc}", code="QUOTE_CACHE_ERROR")


@mcp.tool()
def check_firmware_status(printer_name: str) -> dict:
    """Check firmware version and update availability for a printer.

    Args:
        printer_name: Printer to check.
    """
    try:
        from kiln.firmware import get_firmware_manager

        mgr = get_firmware_manager()
        info = mgr.check_version(printer_name)
        return {"success": True, "firmware": info.to_dict()}
    except Exception as exc:
        logger.exception("Error in check_firmware_status")
        return _error_dict(f"Failed to check firmware status: {exc}", code="FIRMWARE_ERROR")


@mcp.tool()
def update_printer_firmware(
    printer_name: str,
    *,
    target_version: str | None = None,
) -> dict:
    """Start a firmware update on a printer.

    Args:
        printer_name: Printer to update.
        target_version: Specific version to update to (latest if None).
    """
    try:
        from kiln.firmware import get_firmware_manager

        mgr = get_firmware_manager()
        result = mgr.update_firmware(printer_name, target_version=target_version)
        return {"success": True, "update": result.to_dict()}
    except Exception as exc:
        logger.exception("Error in update_printer_firmware")
        return _error_dict(f"Failed to update printer firmware: {exc}", code="FIRMWARE_ERROR")


@mcp.tool()
def rollback_printer_firmware(
    printer_name: str,
    *,
    target_version: str | None = None,
) -> dict:
    """Rollback printer firmware to a previous version.

    Args:
        printer_name: Printer to rollback.
        target_version: Specific version to rollback to.
    """
    try:
        from kiln.firmware import get_firmware_manager

        mgr = get_firmware_manager()
        result = mgr.rollback_firmware(printer_name, target_version=target_version)
        return {"success": True, "rollback": result.to_dict()}
    except Exception as exc:
        logger.exception("Error in rollback_printer_firmware")
        return _error_dict(f"Failed to rollback printer firmware: {exc}", code="FIRMWARE_ERROR")


# ---------------------------------------------------------------------------
# Lightweight print status (token-efficient polling)
# ---------------------------------------------------------------------------


@mcp.tool()
def print_status_lite(printer_name: str | None = None) -> dict:
    """Lightweight print status for efficient agent polling.

    Returns only the essential fields an agent needs to monitor a print:
    state, completion percentage, and estimated time remaining.  Use this
    instead of ``get_printer_status`` when polling frequently to minimise
    token cost.

    Args:
        printer_name: Target printer.  Omit for the default printer.
    """
    try:
        adapter = _registry.get(printer_name) if printer_name else _get_adapter()
        state = adapter.get_state()
        job = adapter.get_job()

        result: dict[str, Any] = {
            "state": state.state.value,
            "completion_pct": job.completion,
            "file_name": job.file_name,
        }

        # Include ETA if available
        if job.time_left is not None:
            result["eta_seconds"] = job.time_left
        if job.time_elapsed is not None:
            result["elapsed_seconds"] = job.time_elapsed

        # Include temperatures if printing
        if state.state in (PrinterStatus.PRINTING, PrinterStatus.PAUSED):
            result["hotend_temp"] = state.hotend_temp
            result["bed_temp"] = state.bed_temp

        return result

    except PrinterNotFoundError:
        return {"state": "not_found", "error": f"Printer {printer_name!r} not found"}
    except (PrinterError, RuntimeError) as exc:
        return {"state": "error", "error": str(exc)}
    except Exception as exc:
        logger.exception("Error in print_status_lite")
        return {"state": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# Snapshot history
# ---------------------------------------------------------------------------


@mcp.tool()
def list_snapshots(
    printer_name: str | None = None,
    job_id: str | None = None,
    phase: str | None = None,
    limit: int = 20,
) -> dict:
    """List persisted snapshots from the database.

    Returns metadata for snapshots captured during print monitoring,
    timelapses, or manual captures.  Use this to review print history
    visually or correlate snapshots with print outcomes.

    Args:
        printer_name: Filter by printer name.
        job_id: Filter by job or timelapse ID.
        phase: Filter by capture phase (e.g. "first_layer", "timelapse", "mid_print").
        limit: Maximum records to return (default 20).
    """
    try:
        db = get_db()
        snapshots = db.get_snapshots(
            job_id=job_id,
            printer_name=printer_name,
            phase=phase,
            limit=limit,
        )
        return {
            "success": True,
            "snapshots": snapshots,
            "count": len(snapshots),
        }
    except Exception as exc:
        logger.exception("Error in list_snapshots")
        return _error_dict(f"Failed to list snapshots: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Enterprise tier tools
# ---------------------------------------------------------------------------


@mcp.tool()
@requires_tier(LicenseTier.ENTERPRISE)
def export_audit_trail(
    start_time: float = 0,
    end_time: float = 0,
    format: str = "json",
    tool_name: str = "",
    action: str = "",
    session_id: str = "",
) -> dict:
    """Export the safety audit trail as JSON or CSV.

    Enterprise feature. Returns the full audit log with optional filters
    for date range, tool name, action type, and session ID.

    Args:
        start_time: Unix timestamp lower bound (0 = no filter).
        end_time: Unix timestamp upper bound (0 = no filter).
        format: Output format, ``"json"`` or ``"csv"``.
        tool_name: Filter by MCP tool name.
        action: Filter by action (executed, blocked, etc.).
        session_id: Filter by agent session ID.
    """
    if err := _check_auth("admin"):
        return err
    try:
        db = get_db()
        exported = db.export_audit_trail(
            start_time=start_time if start_time > 0 else None,
            end_time=end_time if end_time > 0 else None,
            format=format,
            tool_name=tool_name or None,
            action=action or None,
            session_id=session_id or None,
        )
        return {
            "success": True,
            "format": format,
            "data": exported,
        }
    except Exception as exc:
        logger.exception("Error in export_audit_trail")
        return _error_dict(f"Failed to export audit trail: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
@requires_tier(LicenseTier.ENTERPRISE)
def lock_safety_profile(printer_model: str) -> dict:
    """Lock a safety profile so agents cannot modify its limits.

    Enterprise feature. When locked, community profile updates for this
    printer model are rejected. Only an admin can unlock.

    Args:
        printer_model: Profile identifier to lock (e.g. "ender3").
    """
    if err := _check_auth("admin"):
        return err
    try:
        from kiln.safety_profiles import lock_safety_profile as _lock

        _lock(printer_model)
        return {
            "success": True,
            "message": f"Safety profile '{printer_model}' is now locked.",
            "printer_model": printer_model,
        }
    except Exception as exc:
        logger.exception("Error in lock_safety_profile")
        return _error_dict(f"Failed to lock safety profile: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
@requires_tier(LicenseTier.ENTERPRISE)
def unlock_safety_profile(printer_model: str) -> dict:
    """Unlock a previously locked safety profile.

    Enterprise feature. Allows community profile modifications for
    this printer model again.

    Args:
        printer_model: Profile identifier to unlock.
    """
    if err := _check_auth("admin"):
        return err
    try:
        from kiln.safety_profiles import unlock_safety_profile as _unlock

        unlocked = _unlock(printer_model)
        if not unlocked:
            return {
                "success": True,
                "message": f"Profile '{printer_model}' was not locked.",
                "printer_model": printer_model,
            }
        return {
            "success": True,
            "message": f"Safety profile '{printer_model}' is now unlocked.",
            "printer_model": printer_model,
        }
    except Exception as exc:
        logger.exception("Error in unlock_safety_profile")
        return _error_dict(f"Failed to unlock safety profile: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
@requires_tier(LicenseTier.ENTERPRISE)
def manage_team_member(
    action: str,
    email: str,
    role: str = "engineer",
) -> dict:
    """Add, remove, or update a team member.

    Enterprise feature. Manages team seats and role assignments.
    Business tier supports up to 5 seats; Enterprise is unlimited.

    Args:
        action: One of ``"add"``, ``"remove"``, ``"set_role"``, ``"list"``.
        email: Member email address (ignored for ``"list"``).
        role: Role for add/set_role: ``"admin"``, ``"engineer"``, ``"operator"``.
    """
    if err := _check_auth("admin"):
        return err
    try:
        from kiln.licensing import get_tier
        from kiln.teams import TeamManager

        mgr = TeamManager()
        tier = get_tier().value

        if action == "list":
            members = mgr.list_members()
            seat_info = mgr.seat_status(tier=tier)
            return {
                "success": True,
                "members": [m.to_dict() for m in members],
                "seats": seat_info,
            }
        elif action == "add":
            member = mgr.add_member(email, role=role, tier=tier)
            return {
                "success": True,
                "message": f"Added {email} as {role}.",
                "member": member.to_dict(),
            }
        elif action == "remove":
            removed = mgr.remove_member(email)
            if not removed:
                return _error_dict(f"No active member with email {email!r}.", code="NOT_FOUND")
            return {
                "success": True,
                "message": f"Removed {email} from team.",
            }
        elif action == "set_role":
            member = mgr.set_member_role(email, role)
            return {
                "success": True,
                "message": f"Updated {email} role to {role}.",
                "member": member.to_dict(),
            }
        else:
            return _error_dict(
                f"Unknown action: {action!r}. Use add, remove, set_role, or list.",
                code="INVALID_INPUT",
            )
    except Exception as exc:
        logger.exception("Error in manage_team_member")
        return _error_dict(f"Team management failed: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
@requires_tier(LicenseTier.ENTERPRISE)
def printer_usage_summary() -> dict:
    """Show printer count, included allowance, and overage charges.

    Enterprise feature. Enterprise base includes 20 printers.
    Additional printers are $15/month each.
    """
    if err := _check_auth("read"):
        return err
    try:
        from kiln.printer_billing import PrinterUsageBilling

        billing = PrinterUsageBilling()
        active_count = _registry.count if _registry else 0
        usage = billing.usage_summary(active_count)
        estimate = billing.estimate_monthly_cost(active_count)

        return {
            "success": True,
            "usage": usage.to_dict(),
            "cost_estimate": estimate,
        }
    except Exception as exc:
        logger.exception("Error in printer_usage_summary")
        return _error_dict(f"Failed to get printer usage: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
@requires_tier(LicenseTier.ENTERPRISE)
def uptime_report() -> dict:
    """Get rolling uptime statistics and SLA status.

    Enterprise feature. Shows uptime percentages for 1h, 24h, 7d,
    and 30d windows, average response times, and whether the 99.9%
    SLA target is being met.
    """
    if err := _check_auth("read"):
        return err
    try:
        from kiln.uptime import get_uptime_tracker

        tracker = get_uptime_tracker()
        report = tracker.uptime_report()
        incidents = tracker.recent_incidents(limit=5)

        return {
            "success": True,
            "uptime": report,
            "recent_incidents": incidents,
        }
    except Exception as exc:
        logger.exception("Error in uptime_report")
        return _error_dict(f"Failed to get uptime report: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
@requires_tier(LicenseTier.ENTERPRISE)
def encryption_status() -> dict:
    """Check G-code encryption status and configuration.

    Enterprise feature. Reports whether encryption is active,
    whether the encryption key is configured, and whether the
    cryptography library is installed.
    """
    if err := _check_auth("read"):
        return err
    try:
        from kiln.gcode_encryption import get_gcode_encryption

        enc = get_gcode_encryption()
        return {
            "success": True,
            "encryption": enc.status(),
        }
    except Exception as exc:
        logger.exception("Error in encryption_status")
        return _error_dict(f"Failed to get encryption status: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
@requires_tier(LicenseTier.ENTERPRISE)
def report_printer_overage(subscription_item_id: str, active_printer_count: int) -> dict:
    """Report metered printer usage to Stripe for Enterprise billing.

    Enterprise feature. Reports the current active printer count to Stripe's
    metered billing system. The first 20 printers are included in the base
    Enterprise price; this tool reports the overage (count minus 20, minimum 0).

    Args:
        subscription_item_id: The Stripe SubscriptionItem ID (``si_...``) for
            the metered printer overage line item on the customer's subscription.
        active_printer_count: Total number of active printers in the fleet.
    """
    if err := _check_auth("admin"):
        return err
    try:
        from kiln.payments.stripe_provider import StripeProvider
        from kiln.printer_billing import INCLUDED_PRINTERS

        stripe_key = os.environ.get("KILN_STRIPE_SECRET_KEY", "")
        if not stripe_key:
            return _error_dict("Stripe not configured. Set KILN_STRIPE_SECRET_KEY.", code="CONFIG_MISSING")

        provider = StripeProvider(secret_key=stripe_key)
        overage = max(0, active_printer_count - INCLUDED_PRINTERS)
        result = provider.report_printer_usage(subscription_item_id, overage)

        return {
            "success": True,
            "active_printers": active_printer_count,
            "included": INCLUDED_PRINTERS,
            "overage": overage,
            "overage_cost": f"${overage * 15:.2f}/mo",
            "stripe_usage_record": result,
        }
    except Exception as exc:
        logger.exception("Error in report_printer_overage")
        return _error_dict(f"Failed to report usage: {exc}", code="PAYMENT_ERROR")


# ---------------------------------------------------------------------------
# SSO (Enterprise)
# ---------------------------------------------------------------------------


@mcp.tool()
@requires_tier(LicenseTier.ENTERPRISE)
def configure_sso(
    issuer_url: str,
    client_id: str,
    protocol: str = "oidc",
    client_secret: str = "",
    redirect_uri: str = "",
    allowed_domains: str = "",
    role_mapping: str = "",
) -> dict:
    """Configure SSO (OIDC or SAML) for Enterprise authentication.

    Enterprise feature. Sets up single sign-on with your identity provider
    (Okta, Google Workspace, Azure AD, Auth0, etc.).

    Args:
        issuer_url: IdP issuer URL (e.g. ``https://accounts.google.com``).
        client_id: OIDC client ID or SAML entity ID.
        protocol: ``"oidc"`` or ``"saml"``.
        client_secret: OIDC client secret (optional for public clients).
        redirect_uri: Callback URL after auth. Default: ``http://localhost:8741/sso/callback``.
        allowed_domains: Comma-separated email domains (e.g. ``"acme.com,partner.org"``).
        role_mapping: JSON string mapping IdP groups to Kiln roles
            (e.g. ``'{"admins":"admin","devs":"engineer"}'``).
    """
    if err := _check_auth("admin"):
        return err
    try:
        from kiln.sso import SSOConfig, SSOProtocol, get_sso_manager

        try:
            proto = SSOProtocol(protocol.lower())
        except ValueError:
            return _error_dict(
                f"Invalid protocol: {protocol!r}. Use 'oidc' or 'saml'.",
                code="INVALID_INPUT",
            )

        domains = [d.strip() for d in allowed_domains.split(",") if d.strip()] if allowed_domains else []
        mapping: dict[str, str] = {}
        if role_mapping:
            import json as _json

            try:
                mapping = _json.loads(role_mapping)
            except _json.JSONDecodeError:
                return _error_dict("role_mapping must be valid JSON.", code="INVALID_INPUT")

        config = SSOConfig(
            protocol=proto,
            issuer_url=issuer_url,
            client_id=client_id,
            client_secret=client_secret or None,
            redirect_uri=redirect_uri or "http://localhost:8741/sso/callback",
            allowed_domains=domains,
            role_mapping=mapping,
        )

        mgr = get_sso_manager()
        mgr.configure(config)

        return {
            "success": True,
            "protocol": proto.value,
            "issuer_url": issuer_url,
            "allowed_domains": domains,
            "next_step": (
                "SSO configured. Use 'sso_login_url' to get the IdP login URL, "
                "then exchange the auth code with 'sso_exchange_code'."
            ),
        }
    except Exception as exc:
        logger.exception("Error in configure_sso")
        return _error_dict(f"Failed to configure SSO: {exc}", code="SSO_ERROR")


@mcp.tool()
@requires_tier(LicenseTier.ENTERPRISE)
def sso_login_url(state: str = "") -> dict:
    """Get the SSO login URL to redirect users to the identity provider.

    Enterprise feature. Returns the IdP authorization URL for OIDC or
    the SAML AuthnRequest redirect URL.

    Args:
        state: Optional opaque state parameter for CSRF protection.
    """
    if err := _check_auth("read"):
        return err
    try:
        from kiln.sso import SSOProtocol, get_sso_manager

        mgr = get_sso_manager()
        config = mgr.get_config()
        if config is None:
            return _error_dict("SSO not configured. Use 'configure_sso' first.", code="CONFIG_MISSING")

        if config.protocol == SSOProtocol.OIDC:
            url = mgr.get_oidc_authorize_url(state=state or None)
        else:
            url = mgr.get_saml_login_url()

        return {
            "success": True,
            "login_url": url,
            "protocol": config.protocol.value,
            "next_step": "Redirect the user to login_url. After auth, exchange the code with 'sso_exchange_code'.",
        }
    except Exception as exc:
        logger.exception("Error in sso_login_url")
        return _error_dict(f"Failed to generate login URL: {exc}", code="SSO_ERROR")


@mcp.tool()
@requires_tier(LicenseTier.ENTERPRISE)
def sso_exchange_code(code: str) -> dict:
    """Exchange an SSO authorization code for user identity and role.

    Enterprise feature. After the user completes IdP login, exchange
    the auth code to get their identity, email, groups, and mapped
    Kiln role.

    Args:
        code: The authorization code from the IdP callback.
    """
    if err := _check_auth("read"):
        return err
    try:
        from kiln.sso import get_sso_manager, map_sso_user_to_role

        mgr = get_sso_manager()
        config = mgr.get_config()
        if config is None:
            return _error_dict("SSO not configured. Use 'configure_sso' first.", code="CONFIG_MISSING")

        user = mgr.exchange_oidc_code(code)
        kiln_role = map_sso_user_to_role(user)

        return {
            "success": True,
            "user": user.to_dict(),
            "kiln_role": kiln_role,
            "next_step": f"User authenticated as {user.email} with role '{kiln_role}'.",
        }
    except Exception as exc:
        logger.exception("Error in sso_exchange_code")
        return _error_dict(f"SSO authentication failed: {exc}", code="SSO_ERROR")


@mcp.tool()
@requires_tier(LicenseTier.ENTERPRISE)
def sso_status() -> dict:
    """Check current SSO configuration status.

    Enterprise feature. Returns whether SSO is configured, the protocol,
    issuer, allowed domains, and role mapping.
    """
    if err := _check_auth("read"):
        return err
    try:
        from kiln.sso import get_sso_manager

        mgr = get_sso_manager()
        config = mgr.get_config()
        if config is None:
            return {
                "success": True,
                "configured": False,
                "next_step": "SSO not configured. Use 'configure_sso' to set up OIDC or SAML.",
            }

        return {
            "success": True,
            "configured": True,
            "protocol": config.protocol.value,
            "issuer_url": config.issuer_url,
            "client_id": config.client_id,
            "allowed_domains": config.allowed_domains,
            "role_mapping": config.role_mapping,
            "redirect_uri": config.redirect_uri,
        }
    except Exception as exc:
        logger.exception("Error in sso_status")
        return _error_dict(f"Failed to get SSO status: {exc}", code="SSO_ERROR")


if __name__ == "__main__":
    main()
