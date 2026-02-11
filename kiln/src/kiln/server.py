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
import shutil
import signal
import sys
import tempfile
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
from kiln.gcode import validate_gcode as _validate_gcode_impl, validate_gcode_for_printer
from kiln.safety_profiles import get_profile, list_profiles, profile_to_dict
from kiln.slicer_profiles import (
    get_slicer_profile, list_slicer_profiles, resolve_slicer_profile,
    slicer_profile_to_dict,
)
from kiln.printer_intelligence import (
    get_printer_intel, list_intel_profiles, get_material_settings,
    diagnose_issue, intel_to_dict,
)
from kiln.pipelines import (
    quick_print as _pipeline_quick_print,
    calibrate as _pipeline_calibrate,
    benchmark as _pipeline_benchmark,
    list_pipelines as _list_pipelines,
)
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
    FulfillmentError,
    FulfillmentProvider,
    OrderRequest,
    QuoteRequest,
    get_provider as get_fulfillment_provider,
    list_providers as list_fulfillment_providers,
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
from kiln.generation import (
    GenerationError,
    GenerationAuthError,
    GenerationProvider,
    GenerationJob,
    GenerationResult,
    GenerationStatus,
    MeshyProvider,
    OpenSCADProvider,
    convert_to_stl,
    validate_mesh,
)



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
_CONFIRM_UPLOAD: bool = (
    os.environ.get("KILN_CONFIRM_UPLOAD", "").lower() in ("1", "true", "yes")
)
_CONFIRM_MODE: bool = (
    os.environ.get("KILN_CONFIRM_MODE", "").lower() in ("1", "true", "yes")
)
_THINGIVERSE_TOKEN: str = os.environ.get("KILN_THINGIVERSE_TOKEN", "")
_MMF_API_KEY: str = os.environ.get("KILN_MMF_API_KEY", "")
_CULTS3D_USERNAME: str = os.environ.get("KILN_CULTS3D_USERNAME", "")
_CULTS3D_API_KEY: str = os.environ.get("KILN_CULTS3D_API_KEY", "")
_CRAFTCLOUD_API_KEY: str = os.environ.get("KILN_CRAFTCLOUD_API_KEY", "")
_FULFILLMENT_PROVIDER: str = os.environ.get("KILN_FULFILLMENT_PROVIDER", "")
_MESHY_API_KEY: str = os.environ.get("KILN_MESHY_API_KEY", "")

# Auto-print toggles: OFF by default for safety.  Generated models are
# higher risk than marketplace downloads — two independent toggles let
# users opt in to each separately.
_AUTO_PRINT_MARKETPLACE: bool = (
    os.environ.get("KILN_AUTO_PRINT_MARKETPLACE", "").lower() in ("1", "true", "yes")
)
_AUTO_PRINT_GENERATED: bool = (
    os.environ.get("KILN_AUTO_PRINT_GENERATED", "").lower() in ("1", "true", "yes")
)

# Default snapshot directory — use ~/.kiln/snapshots/ instead of /tmp to
# avoid macOS periodic /tmp cleanup deleting saved snapshots.
_DEFAULT_SNAPSHOT_DIR = os.path.join(os.path.expanduser("~"), ".kiln", "snapshots")

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
            "No printer configured. Set KILN_PRINTER_HOST environment variable "
            "to the printer URL (e.g. http://octopi.local). Also set "
            "KILN_PRINTER_API_KEY and optionally KILN_PRINTER_TYPE."
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
    _BLOCK_THRESHOLD: int = 3      # blocks within the window to trigger
    _BLOCK_WINDOW: float = 60.0    # seconds
    _COOLDOWN_DURATION: float = 300.0  # 5 minutes

    def __init__(self) -> None:
        self._last_call: dict[str, float] = {}
        self._call_history: dict[str, list[float]] = {}
        self._block_history: dict[str, list[float]] = {}
        self._cooldown_until: dict[str, float] = {}

    def record_block(self, tool_name: str) -> Optional[str]:
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

    def check(
        self, tool_name: str, min_interval_ms: int = 0, max_per_minute: int = 0
    ) -> Optional[str]:
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
                return (
                    f"Rate limited: {tool_name} called too rapidly. "
                    f"Wait {wait:.1f}s before retrying."
                )

        # Max calls per rolling 60-second window.
        if max_per_minute > 0:
            history = self._call_history.get(tool_name, [])
            cutoff = now - 60.0
            history = [t for t in history if t > cutoff]
            if len(history) >= max_per_minute:
                return (
                    f"Rate limited: {tool_name} called {max_per_minute} times "
                    f"in the last minute. Wait before retrying."
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
    "send_gcode":      (500, 30),
    "emergency_stop":  (5000, 3),
    "cancel_print":    (5000, 3),
    "start_print":     (5000, 3),
    "upload_file":     (2000, 10),
    "pause_print":     (5000, 6),
    "resume_print":    (5000, 6),
}


def _check_rate_limit(tool_name: str) -> Optional[dict]:
    """Return an error dict if *tool_name* is rate-limited, else ``None``."""
    limits = _TOOL_RATE_LIMITS.get(tool_name)
    if not limits:
        return None
    msg = _tool_limiter.check(tool_name, limits[0], limits[1])
    if msg:
        _audit(tool_name, "rate_limited", details={"message": msg})
        return _error_dict(msg, code="RATE_LIMITED")
    return None


def _record_tool_block(tool_name: str) -> Optional[dict]:
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
_TOOL_SAFETY: Dict[str, Dict[str, Any]] = {}
try:
    import json as _json
    _safety_data_path = Path(__file__).resolve().parent / "data" / "tool_safety.json"
    _raw_safety = _json.loads(_safety_data_path.read_text(encoding="utf-8"))
    _TOOL_SAFETY = _raw_safety.get("classifications", {})
except (FileNotFoundError, ValueError):
    pass


def _get_safety_level(tool_name: str) -> str:
    """Return the safety classification for a tool (default ``"safe"``)."""
    entry = _TOOL_SAFETY.get(tool_name, {})
    return entry.get("level", "safe")


def _audit(
    tool_name: str,
    action: str,
    details: Optional[Dict[str, Any]] = None,
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
        )
    except Exception:
        logger.debug("Failed to write audit log for %s/%s", tool_name, action)


# ---------------------------------------------------------------------------
# Confirmation gate for destructive tools (KILN_CONFIRM_MODE)
# ---------------------------------------------------------------------------

# Pending confirmations: {token: {tool, args, created_at}}.
_pending_confirmations: dict[str, Dict[str, Any]] = {}
_CONFIRM_TOKEN_TTL: float = 300.0  # 5 minutes


def _check_confirmation(tool_name: str, args: Dict[str, Any]) -> Optional[dict]:
    """If confirm mode is active and the tool is confirm/emergency level, return
    a confirmation-required response.  Otherwise return ``None`` to proceed.
    """
    if not _CONFIRM_MODE:
        return None
    level = _get_safety_level(tool_name)
    if level not in ("confirm", "emergency"):
        return None

    import hashlib
    token = hashlib.sha256(
        f"{tool_name}:{time.time()}:{id(args)}".encode()
    ).hexdigest()[:16]

    _pending_confirmations[token] = {
        "tool": tool_name,
        "args": args,
        "created_at": time.time(),
    }

    # Prune expired tokens
    now = time.time()
    expired = [t for t, v in _pending_confirmations.items()
               if now - v["created_at"] > _CONFIRM_TOKEN_TTL]
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
            "(e.g. KILN_CRAFTCLOUD_API_KEY, KILN_SHAPEWAYS_CLIENT_ID + "
            "KILN_SHAPEWAYS_CLIENT_SECRET, or KILN_SCULPTEO_API_KEY)."
        ) from exc
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


# Error codes that represent transient failures the caller may retry.
_RETRYABLE_CODES = frozenset({
    "ERROR",  # Generic printer / runtime errors are typically transient.
    "INTERNAL_ERROR",
    "GENERATION_TIMEOUT",
    "RATE_LIMIT",
})


# ---------------------------------------------------------------------------
# Safety helpers
# ---------------------------------------------------------------------------

# Environment variable names containing secrets — used to sanitize logs.
_SECRET_ENV_VARS = (
    "KILN_PRINTER_API_KEY", "KILN_THINGIVERSE_TOKEN", "KILN_MMF_API_KEY",
    "KILN_CULTS3D_API_KEY", "KILN_MESHY_API_KEY", "KILN_CRAFTCLOUD_API_KEY",
    "KILN_PRINTER_ACCESS_CODE", "KILN_CIRCLE_API_KEY", "KILN_STRIPE_API_KEY",
    "KILN_STRIPE_WEBHOOK_SECRET", "KILN_API_AUTH_TOKEN",
)


def _sanitize_log_msg(msg: str) -> str:
    """Replace any env var secret values in *msg* with ``***``."""
    for var in _SECRET_ENV_VARS:
        val = os.environ.get(var, "")
        if len(val) > 4:
            msg = msg.replace(val, "***")
    return msg


def _check_disk_space(path: str, required_mb: int = 100) -> Optional[Dict[str, Any]]:
    """Return an error dict if fewer than *required_mb* MB are free at *path*.

    Returns ``None`` if there's enough space.
    """
    try:
        usage = shutil.disk_usage(path)
        free_mb = usage.free / (1024 * 1024)
        if free_mb < required_mb:
            return _error_dict(
                f"Insufficient disk space: {free_mb:.0f} MB free, "
                f"need at least {required_mb} MB.",
                code="DISK_FULL",
            )
    except OSError:
        pass  # Can't check — proceed optimistically
    return None


def _error_dict(
    message: str,
    code: str = "ERROR",
    *,
    retryable: Optional[bool] = None,
) -> Dict[str, Any]:
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
                k: v for k, v in event.data.items()
                if k not in ("job_id", "material_type", "file_hash", "slicer_profile", "agent_id")
            } or None,
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

            token = hashlib.sha256(
                f"{file_path}:{file_size}".encode()
            ).hexdigest()[:16]
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
        return _error_dict(str(exc), code="FILE_NOT_FOUND")
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in upload_file")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
            f"Invalid or expired upload token: {token!r}. "
            f"Call upload_file() again to get a new token.",
            code="INVALID_TOKEN",
        )
    try:
        adapter = _get_adapter()
        result = adapter.upload_file(file_path)
        return result.to_dict()
    except FileNotFoundError as exc:
        return _error_dict(str(exc), code="FILE_NOT_FOUND")
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in upload_file_confirm")
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
        if not ok:
            return _error_dict(f"Failed to delete {file_path}.")
        return {
            "success": True,
            "message": f"Deleted {file_path}.",
        }
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in delete_file")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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

        # -- Automatic pre-flight safety gate (mandatory, cannot be skipped) --
        pf = preflight_check()
        if not pf.get("ready", False):
            _audit("start_print", "preflight_failed", details={
                "file": file_name, "summary": pf.get("summary", ""),
            })
            return {
                "success": False,
                "error": pf.get("summary", "Pre-flight checks failed"),
                "code": "PREFLIGHT_FAILED",
                "preflight": pf,
            }

        result = adapter.start_print(file_name)
        _audit("start_print", "executed", details={"file": file_name})
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
    if err := _check_rate_limit("cancel_print"):
        return err
    if conf := _check_confirmation("cancel_print", {}):
        return conf
    try:
        adapter = _get_adapter()
        result = adapter.cancel_print()
        _audit("cancel_print", "executed")
        return result.to_dict()
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in cancel_print")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def emergency_stop() -> dict:
    """Perform an immediate emergency stop on the printer.

    Sends a firmware-level halt (M112 or equivalent) that immediately
    cuts power to heaters and stepper motors.  Unlike ``cancel_print``,
    this does **not** allow a graceful cooldown — all motion ceases
    instantly.

    Use only in genuine safety emergencies (thermal runaway, collision,
    spaghetti failure threatening the hotend, etc.).

    WARNING: After an emergency stop the printer typically requires a
    power cycle or firmware restart before it can print again.
    """
    if err := _check_auth("print"):
        return err
    if err := _check_rate_limit("emergency_stop"):
        return err
    if conf := _check_confirmation("emergency_stop", {}):
        return conf
    try:
        adapter = _get_adapter()
        result = adapter.emergency_stop()
        _audit("emergency_stop", "executed")
        return result.to_dict()
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in emergency_stop")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
    if err := _check_rate_limit("resume_print"):
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
                f"Hotend temperature {tool_temp}°C exceeds safety limit "
                f"({_MAX_TOOL}°C).",
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
                f"Bed temperature {bed_temp}°C exceeds safety limit "
                f"({_MAX_BED}°C).",
                code="VALIDATION_ERROR",
            )

    try:
        adapter = _get_adapter()
        results: Dict[str, Any] = {"success": True}

        # -- Relative temperature change advisory (non-blocking) ----------
        _DELTA_WARN_TOOL = 10.0
        _DELTA_WARN_BED = 50.0
        rate_warnings: list[str] = []
        try:
            state = adapter.get_state()
            if (
                tool_temp is not None
                and state.tool_temp_target is not None
                and state.tool_temp_target > 0
            ):
                delta = abs(tool_temp - state.tool_temp_target)
                if delta > _DELTA_WARN_TOOL:
                    rate_warnings.append(
                        f"Large hotend temperature change: "
                        f"{state.tool_temp_target:.0f}°C -> {tool_temp:.0f}°C "
                        f"(delta {delta:.0f}°C)."
                    )
            if (
                bed_temp is not None
                and state.bed_temp_target is not None
                and state.bed_temp_target > 0
            ):
                delta = abs(bed_temp - state.bed_temp_target)
                if delta > _DELTA_WARN_BED:
                    rate_warnings.append(
                        f"Large bed temperature change: "
                        f"{state.bed_temp_target:.0f}°C -> {bed_temp:.0f}°C "
                        f"(delta {delta:.0f}°C)."
                    )
        except Exception:
            pass  # Don't let warning logic block the actual operation.

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

        if rate_warnings:
            results["warnings"] = rate_warnings

        _audit("set_temperature", "executed", details={
            "tool_temp": tool_temp, "bed_temp": bed_temp,
        })
        return results
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in set_temperature")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def preflight_check(file_path: str | None = None, expected_material: str | None = None, remote_file: str | None = None) -> dict:
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
        MAX_TOOL, MAX_BED = _get_temp_limits()

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

        # -- Material mismatch check (optional) ----------------------------
        _strict_material = os.environ.get(
            "KILN_STRICT_MATERIAL_CHECK", "true"
        ).lower() in ("1", "true", "yes")

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
                    checks.append({
                        "name": "material_match",
                        "passed": False,
                        "message": mat_msg,
                    })
                    errors.append(mat_msg)
                else:
                    checks.append({
                        "name": "material_match",
                        "passed": True,
                        "message": f"Loaded material matches expected ({expected_material.upper()})",
                    })
            except Exception:
                # Material tracking not configured — skip silently
                pass

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
                        checks.append({
                            "name": "material_compatible",
                            "passed": not _strict_material,
                            "message": msg,
                        })
                        if _strict_material:
                            errors.append(msg)
                    else:
                        checks.append({
                            "name": "material_compatible",
                            "passed": True,
                            "message": (
                                f"{expected_material.upper()} is validated for "
                                f"'{_PRINTER_MODEL}' "
                                f"(hotend {mat_settings.hotend_temp}C, bed {mat_settings.bed_temp}C)"
                            ),
                        })
                except Exception:
                    pass

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


        # -- Remote file check (optional) ----------------------------------
        if remote_file is not None:
            try:
                printer_files = adapter.list_files()
                remote_lower = remote_file.lower()
                file_found = any(
                    f.name.lower() == remote_lower or f.path.lower() == remote_lower
                    for f in printer_files
                )
                checks.append({
                    "name": "file_on_printer",
                    "passed": file_found,
                    "message": (
                        f"File '{remote_file}' found on printer"
                        if file_found
                        else f"File '{remote_file}' not found on printer"
                    ),
                })
                if not file_found:
                    errors.append(f"File '{remote_file}' not found on printer")
            except Exception:
                checks.append({
                    "name": "file_on_printer",
                    "passed": False,
                    "message": "Unable to list files on printer to verify remote file",
                })
                errors.append("Unable to list files on printer to verify remote file")

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
    if not dry_run:
        if conf := _check_confirmation("send_gcode", {"commands": commands}):
            return conf
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
            _audit("send_gcode", "blocked", details={
                "blocked_commands": validation.blocked_commands[:5],
                "errors": validation.errors[:5],
            })
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
            _audit("send_gcode", "dry_run", details={
                "count": len(cmd_list),
            })
            result: Dict[str, Any] = {
                "success": True,
                "dry_run": True,
                "commands_validated": cmd_list,
                "count": len(cmd_list),
                "message": (
                    f"{len(cmd_list)} command(s) validated successfully. "
                    f"No commands were sent (dry-run mode)."
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
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
            "Invalid or expired confirmation token. "
            "Tokens expire after 5 minutes.",
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
        profile_info: Dict[str, Any] = {"printer_model": _PRINTER_MODEL or "not configured"}
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
            name for name, meta in _TOOL_SAFETY.items()
            if meta.get("level") in ("confirm", "emergency")
        )

        # Auth status
        auth_info = {
            "enabled": _auth.enabled if hasattr(_auth, "enabled") else False,
        }

        # Confirm mode
        confirm_mode = os.environ.get("KILN_CONFIRM_MODE", "").lower() in (
            "1", "true", "yes",
        )

        # Recent blocked actions (from audit log)
        recent_blocked: List[Dict[str, Any]] = []
        try:
            db = get_db()
            summary = db.audit_summary(window_seconds=3600.0)
            recent_blocked = summary.get("recent_blocked", [])
        except Exception:
            pass

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
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
    verify_ssl: bool = True,
) -> dict:
    """Register a new printer in the fleet.

    Args:
        name: Unique human-readable name (e.g. "voron-350", "bambu-x1c").
        printer_type: Backend type -- "octoprint", "moonraker", or "bambu".
        host: Base URL or IP address of the printer.
        api_key: API key (required for OctoPrint and Bambu, optional for
            Moonraker).  For Bambu printers this is the LAN Access Code.
        serial: Printer serial number (required for Bambu printers).
        verify_ssl: Whether to verify SSL certificates (default True).
            Set to False for printers using self-signed certificates.

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
            adapter = OctoPrintAdapter(host=host, api_key=api_key, verify_ssl=verify_ssl)
        elif printer_type == "moonraker":
            adapter = MoonrakerAdapter(host=host, api_key=api_key or None, verify_ssl=verify_ssl)
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
    except Exception:
        pass

    return {
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
        "auth_enabled": os.environ.get("KILN_AUTH_ENABLED", "").lower()
            in ("1", "true", "yes"),
    }


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
def get_started() -> dict:
    """Quick-start guide for AI agents using Kiln.

    Returns an onboarding summary: what Kiln is, core workflows,
    and the most useful tools to call first.  Call this at the start
    of a session if you're unfamiliar with the available capabilities.
    """
    from kiln.tool_tiers import TIERS, suggest_tier

    # Build a concise tier summary
    tier_summary = {
        name: {"tool_count": len(tools), "examples": tools[:5]}
        for name, tools in TIERS.items()
    }

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
                        mf.id, dest_dir, file_name=None,
                    )
                    downloaded.append({
                        "file_id": mf.id,
                        "file_name": mf.name,
                        "local_path": path,
                    })
                except (MarketplaceError, RuntimeError) as exc:
                    errors.append({
                        "file_id": mf.id,
                        "file_name": mf.name,
                        "error": str(exc),
                    })

            return {
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
                "message": (
                    f"Downloaded {len(downloaded)}/{len(files)} files "
                    f"from {source} to {dest_dir}"
                ),
            }

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
            "message": f"Downloaded to {path}",
        }
    except (ThingiverseNotFoundError, MktNotFoundError):
        return _error_dict(
            f"File {file_id or model_id} not found on {source}.",
            code="NOT_FOUND",
        )
    except (ThingiverseError, MarketplaceError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in download_model")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
                mf for mf in all_files
                if (
                    mf.name.rsplit(".", 1)[-1].lower() if "." in mf.name else ""
                ) in _printable_exts
            ]
            if not printable_files:
                return _error_dict(
                    f"No printable files (.stl, .gcode, .3mf) found for "
                    f"model {model_id} on {source}.",
                    code="NOT_FOUND",
                )

            uploaded: list[dict] = []
            errors: list[dict] = []
            for mf in printable_files:
                try:
                    local_path = mkt.download_file(mf.id, _dl_dir)
                    upload_result = adapter.upload_file(local_path)
                    up_name = upload_result.file_name or os.path.basename(local_path)
                    uploaded.append({
                        "file_id": mf.id,
                        "file_name": up_name,
                        "local_path": local_path,
                        "upload": upload_result.to_dict(),
                    })
                except (MarketplaceError, PrinterError, RuntimeError) as exc:
                    errors.append({
                        "file_id": mf.id,
                        "file_name": mf.name,
                        "error": str(exc),
                    })

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
                    f"Downloaded and uploaded {len(uploaded)}/"
                    f"{len(printable_files)} printable files from {source}."
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
            print_res = adapter.start_print(file_name)
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
            resp["message"] = (
                f"Downloaded from {source}, uploaded, and started "
                f"printing (auto-print ON)."
            )
        else:
            resp["safety_notice"] = (
                "Model uploaded but NOT started. Community models are "
                "unverified — review before printing. Call start_print "
                "to begin printing after review. Set "
                "KILN_AUTO_PRINT_MARKETPLACE=true to enable auto-print."
            )
            resp["message"] = (
                f"Downloaded from {source} and uploaded to printer. "
                f"Call start_print(\'{file_name}\') to begin printing."
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
            the system temp directory.
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
        job_events = [
            e.to_dict() for e in all_events
            if e.data.get("job_id") == job_id
        ]

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
        progress_events = [
            e for e in job_events
            if e.get("type") == EventType.PRINT_PROGRESS.value
        ]
        if progress_events:
            max_pct = max(
                e.get("data", {}).get("completion", 0)
                for e in progress_events
            )
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
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
        snapshot_info: Dict[str, Any] = {"available": False}
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
                        snapshot_info["image_base64"] = base64.b64encode(
                            image_data
                        ).decode("ascii")
            except Exception as snap_exc:
                snapshot_info = {"available": False, "error": str(snap_exc)}

        # Gather related events
        all_events = _event_bus.recent_events(limit=200)
        job_events = [
            e.to_dict() for e in all_events
            if e.data.get("job_id") == target_job.id
        ]

        # Analyse quality indicators
        issues: list[str] = []
        metrics: Dict[str, Any] = {}
        recommendations: list[str] = []

        # Duration analysis
        if target_job.elapsed_seconds is not None:
            metrics["print_duration_seconds"] = target_job.elapsed_seconds
            metrics["print_duration_hours"] = round(
                target_job.elapsed_seconds / 3600, 2
            )

        # Check for retries (may indicate intermittent problems)
        retry_events = [
            e for e in job_events if e.get("data", {}).get("retry")
        ]
        if retry_events:
            issues.append(
                f"Job required {len(retry_events)} retry attempt(s) before completing"
            )
            recommendations.append(
                "Retries during a print may indicate connectivity or mechanical issues. "
                "Inspect the print closely for layer shifts or gaps."
            )

        # Check progress consistency
        progress_events = [
            e for e in job_events
            if e.get("type") in ("print.progress", "job.progress")
        ]
        if progress_events:
            completions = [
                e.get("data", {}).get("completion", 0) for e in progress_events
            ]
            # Detect non-monotonic progress (resets may indicate issues)
            for i in range(1, len(completions)):
                if completions[i] < completions[i - 1] - 5:
                    issues.append(
                        f"Progress dropped from {completions[i-1]:.0f}% to "
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
                "No webcam available for visual inspection. Consider adding a "
                "camera for automated quality checks."
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
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in validate_print_quality")
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
            f"Unknown generation provider: {provider!r}.  "
            f"Supported: meshy, openscad.",
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
        return _error_dict(str(exc), code="AUTH_ERROR")
    except GenerationError as exc:
        return _error_dict(str(exc), code=exc.code or "GENERATION_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in generate_model")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(str(exc), code="AUTH_ERROR")
    except GenerationError as exc:
        return _error_dict(str(exc), code=exc.code or "GENERATION_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in generation_status")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(str(exc), code="AUTH_ERROR")
    except GenerationError as exc:
        return _error_dict(str(exc), code=exc.code or "GENERATION_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in download_generated_model")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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

            progress_log.append({
                "time": round(elapsed, 1),
                "status": job.status.value,
                "progress": job.progress,
            })

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
        return _error_dict(str(exc), code="AUTH_ERROR")
    except GenerationError as exc:
        return _error_dict(str(exc), code=exc.code or "GENERATION_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in await_generation")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def generate_and_print(
    prompt: str,
    provider: str = "meshy",
    style: str | None = None,
    printer_name: str | None = None,
    profile: str | None = None,
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

        slice_result = slice_file(result.local_path, profile=profile)

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
            print_result = adapter.start_print(file_name)
            print_data = print_result.to_dict()
            auto_printed = True

        resp = {
            "success": True,
            "generation": result.to_dict(),
            "slice": slice_result.to_dict(),
            "upload": upload.to_dict(),
            "file_name": file_name,
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
                f"Generated '{prompt[:80]}' via {gen.display_name}, "
                f"sliced, and started printing (auto-print ON)."
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
        return _error_dict(str(exc), code="AUTH_ERROR")
    except GenerationError as exc:
        return _error_dict(str(exc), code=exc.code or "GENERATION_ERROR")
    except PrinterNotFoundError:
        return _error_dict(
            f"Printer {printer_name!r} not found.", code="NOT_FOUND"
        )
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in generate_and_print")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
            "message": "Mesh is valid." if result.valid else
                       f"Mesh has issues: {'; '.join(result.errors)}",
        }
    except Exception as exc:
        logger.exception("Unexpected error in validate_generated_mesh")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
            return _error_dict(
                "Could not retrieve firmware status.", code="UNAVAILABLE"
            )
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
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in firmware_status")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in update_firmware")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in rollback_firmware")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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

        result: Dict[str, Any] = {
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

                    snap: Dict[str, Any] = {
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
            except Exception:
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
                        f"Print auto-paused due to detected {failure_type} "
                        f"(confidence: {failure_confidence:.0%})"
                    )
                    logger.warning(
                        "Vision auto-pause triggered: %s (confidence=%.2f) on printer %s",
                        failure_type, failure_confidence, printer_name or "default",
                    )
                except Exception as pause_exc:
                    result["failure_detection"]["auto_pause_error"] = str(pause_exc)
                    logger.error(
                        "Vision auto-pause failed: %s on printer %s",
                        pause_exc, printer_name or "default",
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
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in monitor_print_vision")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def watch_print(
    printer_name: str | None = None,
    snapshot_interval: int = 60,
    max_snapshots: int = 5,
    timeout: int = 7200,
    poll_interval: int = 15,
) -> dict:
    """Monitor an in-progress print with periodic snapshot capture.

    Polls the printer state every *poll_interval* seconds.  Every
    *snapshot_interval* seconds a webcam snapshot is captured and
    accumulated.  The tool returns in one of three cases:

    1. **Print terminal state** — completed, failed, cancelled, or offline.
    2. **Snapshot batch ready** — *max_snapshots* images have been collected
       (outcome ``"snapshot_check"``).  The agent should review them and
       call ``pause_print`` / ``cancel_print`` if issues are detected,
       then call ``watch_print`` again to continue monitoring.
    3. **Timeout** — the print has not finished within *timeout* seconds.

    This creates a closed-loop workflow::

        watch_print → agent reviews snapshots → pause if bad → watch_print again

    Args:
        printer_name: Target printer.  Omit for the default printer.
        snapshot_interval: Seconds between snapshot captures (default 60).
        max_snapshots: Return after this many snapshots (default 5).
        timeout: Maximum seconds to monitor (default 7200 = 2 hours).
        poll_interval: Seconds between state polls (default 15).
    """
    if err := _check_auth("monitoring"):
        return err
    try:
        adapter = _registry.get(printer_name) if printer_name else _get_adapter()
        can_snap = getattr(adapter.capabilities, "can_snapshot", False)

        # Early exit: if printer is idle with no active job, don't block
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

        start_time = time.time()
        last_snapshot_time = 0.0
        snapshots: list[dict] = []
        progress_log: list[dict] = []
        snapshot_failures = 0

        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout:
                return {
                    "success": True,
                    "outcome": "timeout",
                    "elapsed_seconds": round(elapsed, 1),
                    "progress_log": progress_log[-20:],
                    "snapshots": snapshots,
                    "snapshot_failures": snapshot_failures,
                    "final_state": adapter.get_state().to_dict(),
                }

            state = adapter.get_state()
            job = adapter.get_job()

            # Record progress
            if job.completion is not None:
                progress_log.append({
                    "time": round(elapsed, 1),
                    "completion": job.completion,
                })

            # Check terminal states
            if state.state == PrinterStatus.IDLE and elapsed > 30:
                return {
                    "success": True,
                    "outcome": "completed",
                    "elapsed_seconds": round(elapsed, 1),
                    "progress_log": progress_log[-20:],
                    "snapshots": snapshots,
                    "snapshot_failures": snapshot_failures,
                    "final_state": state.to_dict(),
                }
            if state.state in (PrinterStatus.ERROR, PrinterStatus.OFFLINE):
                _event_bus.publish(
                    EventType.VISION_ALERT,
                    {
                        "printer_name": printer_name or "default",
                        "alert_type": "printer_state",
                        "state": state.state.value,
                        "completion": job.completion,
                        "elapsed_seconds": round(elapsed, 1),
                    },
                    source="vision",
                )
                return {
                    "success": True,
                    "outcome": "failed",
                    "elapsed_seconds": round(elapsed, 1),
                    "progress_log": progress_log[-20:],
                    "snapshots": snapshots,
                    "snapshot_failures": snapshot_failures,
                    "final_state": state.to_dict(),
                    "error": f"Printer entered {state.state.value} state",
                }
            if state.state == PrinterStatus.PAUSED:
                return {
                    "success": True,
                    "outcome": "paused",
                    "elapsed_seconds": round(elapsed, 1),
                    "progress_log": progress_log[-20:],
                    "snapshots": snapshots,
                    "snapshot_failures": snapshot_failures,
                    "final_state": state.to_dict(),
                    "message": (
                        "Print is paused. Call resume_print to continue, "
                        "or cancel_print to abort."
                    ),
                }
            if state.state == PrinterStatus.CANCELLING:
                return {
                    "success": True,
                    "outcome": "cancelling",
                    "elapsed_seconds": round(elapsed, 1),
                    "progress_log": progress_log[-20:],
                    "snapshots": snapshots,
                    "snapshot_failures": snapshot_failures,
                    "final_state": state.to_dict(),
                }

            # Snapshot capture — only if adapter supports it
            now = time.time()
            if can_snap and (now - last_snapshot_time) >= snapshot_interval:
                try:
                    image_data = adapter.get_snapshot()
                    if image_data and len(image_data) > 100:
                        import base64

                        phase = _detect_phase(job.completion)
                        snapshots.append({
                            "captured_at": now,
                            "completion_percent": job.completion,
                            "print_phase": phase,
                            "image_base64": base64.b64encode(image_data).decode("ascii"),
                        })
                        _event_bus.publish(
                            EventType.VISION_CHECK,
                            {
                                "printer_name": printer_name or "default",
                                "completion": job.completion,
                                "phase": phase,
                                "snapshot_index": len(snapshots),
                            },
                            source="vision",
                        )
                    else:
                        snapshot_failures += 1
                except Exception:
                    snapshot_failures += 1
                last_snapshot_time = now

            # Return batch when enough snapshots accumulated
            if len(snapshots) >= max_snapshots:
                return {
                    "success": True,
                    "outcome": "snapshot_check",
                    "elapsed_seconds": round(time.time() - start_time, 1),
                    "progress_log": progress_log[-20:],
                    "snapshots": snapshots,
                    "snapshot_failures": snapshot_failures,
                    "final_state": state.to_dict(),
                    "message": (
                        f"Captured {len(snapshots)} snapshots. "
                        "Review them for print quality issues. "
                        "Call pause_print or cancel_print if problems are detected, "
                        "then call watch_print again to continue monitoring."
                    ),
                }

            time.sleep(poll_interval)

    except PrinterNotFoundError:
        return _error_dict(f"Printer {printer_name!r} not found.", code="NOT_FOUND")
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in watch_print")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Cross-printer learning tools
# ---------------------------------------------------------------------------

# Allowed values for outcome recording — prevents garbage data.
_VALID_OUTCOMES = frozenset({"success", "failed", "partial"})
_VALID_QUALITY_GRADES = frozenset({"excellent", "good", "acceptable", "poor"})
_VALID_FAILURE_MODES = frozenset({
    "spaghetti", "layer_shift", "warping", "adhesion", "stringing",
    "under_extrusion", "over_extrusion", "clog", "thermal_runaway",
    "power_loss", "mechanical", "other",
})

# Hard safety limits — recorded settings cannot exceed these.
# Prevents malicious agents from poisoning the learning database
# with dangerous temperature data that could damage printers.
_MAX_SAFE_TOOL_TEMP: float = 320.0   # Above this, even high-temp materials are dangerous
_MAX_SAFE_BED_TEMP: float = 140.0
_MAX_SAFE_SPEED: float = 500.0       # mm/s — beyond any consumer printer

_LEARNING_SAFETY_NOTICE = (
    "These insights are advisory only. They do NOT override safety limits. "
    "Always run preflight checks before starting a print. Temperature and "
    "G-code safety enforcement applies regardless of learning data."
)


@mcp.tool()
def record_print_outcome(
    job_id: str,
    outcome: str,
    quality_grade: str | None = None,
    failure_mode: str | None = None,
    settings: dict | None = None,
    environment: dict | None = None,
    notes: str | None = None,
    printer_name: str | None = None,
    file_name: str | None = None,
    file_hash: str | None = None,
    material_type: str | None = None,
) -> dict:
    """Record the outcome of a print for cross-printer learning.

    The learning database helps agents make better decisions about which
    printer to use for a given job and material.  Outcomes are agent-curated
    quality data — separate from the auto-populated print history.

    **Safety**: Settings are validated against hard safety limits.  Outcomes
    with temperatures exceeding safe maximums are rejected to prevent
    poisoning the learning database with dangerous data.

    Args:
        job_id: The job ID from the print queue.
        outcome: One of ``"success"``, ``"failed"``, or ``"partial"``.
        quality_grade: Optional — ``"excellent"``, ``"good"``, ``"acceptable"``, ``"poor"``.
        failure_mode: Optional — e.g. ``"spaghetti"``, ``"layer_shift"``, ``"warping"``.
        settings: Optional dict of print settings used (temp_tool, temp_bed, speed, etc.).
        environment: Optional dict of environment conditions (ambient_temp, humidity).
        notes: Optional free-text notes about the print.
        printer_name: Printer used.  Auto-resolved from job if omitted.
        file_name: File printed.  Auto-resolved from job if omitted.
        file_hash: Optional hash of the file for cross-printer comparison.
        material_type: Material used (e.g. ``"PLA"``, ``"PETG"``).
    """
    if err := _check_auth("learning"):
        return err

    # --- Validate enums ---
    if outcome not in _VALID_OUTCOMES:
        return _error_dict(
            f"Invalid outcome {outcome!r}. Must be one of: {', '.join(sorted(_VALID_OUTCOMES))}",
            code="VALIDATION_ERROR",
        )
    if quality_grade and quality_grade not in _VALID_QUALITY_GRADES:
        return _error_dict(
            f"Invalid quality_grade {quality_grade!r}. "
            f"Must be one of: {', '.join(sorted(_VALID_QUALITY_GRADES))}",
            code="VALIDATION_ERROR",
        )
    if failure_mode and failure_mode not in _VALID_FAILURE_MODES:
        return _error_dict(
            f"Invalid failure_mode {failure_mode!r}. "
            f"Must be one of: {', '.join(sorted(_VALID_FAILURE_MODES))}",
            code="VALIDATION_ERROR",
        )

    # --- Safety: validate settings against hard limits ---
    if settings:
        _SETTING_LIMITS = {
            "temp_tool": (0.0, _MAX_SAFE_TOOL_TEMP, "°C"),
            "temp_bed": (0.0, _MAX_SAFE_BED_TEMP, "°C"),
            "speed": (0.0, _MAX_SAFE_SPEED, "mm/s"),
        }
        for key, (lo, hi, unit) in _SETTING_LIMITS.items():
            raw = settings.get(key)
            if raw is None:
                continue
            try:
                val = float(raw)
            except (ValueError, TypeError):
                return _error_dict(
                    f"Setting {key!r} value {raw!r} is not a valid number.",
                    code="VALIDATION_ERROR",
                )
            if val < lo or val > hi:
                return _error_dict(
                    f"Recorded {key} {val}{unit} is outside safe range "
                    f"({lo}–{hi}{unit}). Outcome rejected to protect hardware.",
                    code="SAFETY_VIOLATION",
                )

    # --- Resolve printer/file from job if not provided ---
    try:
        job_record = get_db().get_print_record(job_id)
        if job_record and not printer_name:
            printer_name = job_record.get("printer_name", "unknown")
        if job_record and not file_name:
            file_name = job_record.get("file_name")
    except Exception:
        pass  # Best-effort resolution

    if not printer_name:
        printer_name = "unknown"

    try:
        row_id = get_db().save_print_outcome({
            "job_id": job_id,
            "printer_name": printer_name,
            "file_name": file_name,
            "file_hash": file_hash,
            "material_type": material_type,
            "outcome": outcome,
            "quality_grade": quality_grade,
            "failure_mode": failure_mode,
            "settings": settings,
            "environment": environment,
            "notes": notes,
            "agent_id": "mcp",
            "created_at": time.time(),
        })
        return {
            "success": True,
            "outcome_id": row_id,
            "job_id": job_id,
            "printer_name": printer_name,
            "outcome": outcome,
            "quality_grade": quality_grade,
        }
    except Exception as exc:
        logger.exception("Unexpected error in record_print_outcome")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def get_printer_insights(
    printer_name: str,
    limit: int = 20,
) -> dict:
    """Query cross-printer learning insights for a specific printer.

    Returns success rates, failure mode breakdown, and per-material
    statistics based on previously recorded outcomes.

    **Note**: Insights are advisory.  They do NOT override safety limits
    or preflight checks.

    Args:
        printer_name: The printer to get insights for.
        limit: Maximum recent outcomes to include (default 20).
    """
    if err := _check_auth("learning"):
        return err
    try:
        insights = get_db().get_printer_learning_insights(printer_name)
        recent = get_db().list_print_outcomes(printer_name=printer_name, limit=limit)

        # Confidence level based on sample size
        total = insights.get("total_outcomes", 0)
        if total < 5:
            confidence = "low"
        elif total < 20:
            confidence = "medium"
        else:
            confidence = "high"

        return {
            "success": True,
            "printer_name": printer_name,
            "insights": insights,
            "recent_outcomes": recent,
            "confidence": confidence,
            "safety_notice": _LEARNING_SAFETY_NOTICE,
        }
    except Exception as exc:
        logger.exception("Unexpected error in get_printer_insights")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def suggest_printer_for_job(
    file_hash: str | None = None,
    material_type: str | None = None,
    file_name: str | None = None,
) -> dict:
    """Suggest the best printer for a job based on historical outcomes.

    Rankings are based on success rates from previously recorded outcomes,
    optionally filtered by file hash or material type.  Cross-references
    the printer registry for current availability.

    **Note**: Suggestions are advisory.  They do NOT override safety limits
    or preflight checks.  Always run preflight validation before starting
    a print regardless of learning data.

    Args:
        file_hash: Optional hash of the file to match previous prints.
        material_type: Optional material type to filter by (e.g. ``"PLA"``).
        file_name: Optional file name (informational, not used for matching).
    """
    if err := _check_auth("learning"):
        return err
    try:
        ranked = get_db().suggest_printer_for_outcome(
            file_hash=file_hash, material_type=material_type,
        )

        # Cross-reference availability from registry
        try:
            idle = set(_registry.get_idle_printers())
        except Exception:
            idle = set()

        suggestions = []
        for entry in ranked:
            pname = entry["printer_name"]
            rate = entry["success_rate"]
            total = entry["total_prints"]
            suggestions.append({
                "printer_name": pname,
                "success_rate": rate,
                "total_prints": total,
                "score": round(rate * (1 - 1 / (1 + total)), 2),  # Confidence scales with log-ish sample size
                "reason": f"{int(rate * 100)}% success rate ({total} prints)",
                "currently_available": pname in idle,
            })

        # Sort by score descending
        suggestions.sort(key=lambda s: s["score"], reverse=True)

        total_outcomes = sum(e["total_prints"] for e in ranked)
        confidence = "low" if total_outcomes < 5 else ("medium" if total_outcomes < 20 else "high")

        return {
            "success": True,
            "suggestions": suggestions,
            "query": {
                "file_hash": file_hash,
                "material_type": material_type,
                "file_name": file_name,
            },
            "data_quality": {
                "total_outcomes": total_outcomes,
                "printers_with_data": len(ranked),
                "confidence": confidence,
            },
            "safety_notice": _LEARNING_SAFETY_NOTICE,
        }
    except Exception as exc:
        logger.exception("Unexpected error in suggest_printer_for_job")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def save_agent_note(
    key: str,
    value: str,
    scope: str = "global",
    printer_name: str | None = None,
) -> dict:
    """Save a persistent note or preference that survives across sessions.

    Use this to remember printer quirks, calibration findings, material
    preferences, or any operational knowledge worth preserving.

    Args:
        key: Name for this memory (e.g., ``"z_offset_adjustment"``, ``"pla_temp_notes"``).
        value: The information to store.
        scope: Namespace — ``"global"``, ``"fleet"``, or use *printer_name* for printer-specific.
        printer_name: If provided, scope is automatically set to ``"printer:<name>"``.
    """
    if err := _check_auth("memory"):
        return err
    try:
        agent_id = os.environ.get("KILN_AGENT_ID", "default")
        effective_scope = f"printer:{printer_name}" if printer_name else scope
        get_db().save_memory(agent_id, effective_scope, key, value)
        return {
            "success": True,
            "agent_id": agent_id,
            "scope": effective_scope,
            "key": key,
        }
    except Exception as exc:
        logger.exception("Unexpected error in save_agent_note")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def get_agent_context(
    printer_name: str | None = None,
    scope: str | None = None,
) -> dict:
    """Retrieve all stored agent memory for context.

    Call this at the start of a session to recall what you've learned
    about printers, materials, and past print outcomes.

    Args:
        printer_name: If provided, retrieves printer-specific memory.
        scope: Filter by scope (e.g., ``"global"``, ``"fleet"``).
    """
    if err := _check_auth("memory"):
        return err
    try:
        agent_id = os.environ.get("KILN_AGENT_ID", "default")
        effective_scope = f"printer:{printer_name}" if printer_name else scope
        entries = get_db().list_memory(agent_id, scope=effective_scope)
        return {"success": True, "agent_id": agent_id, "entries": entries, "count": len(entries)}
    except Exception as exc:
        logger.exception("Unexpected error in get_agent_context")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def delete_agent_note(
    key: str,
    scope: str = "global",
    printer_name: str | None = None,
) -> dict:
    """Remove a stored note or preference.

    Args:
        key: The key of the note to delete.
        scope: The scope namespace (default ``"global"``).
        printer_name: If provided, targets ``"printer:<name>"`` scope.
    """
    if err := _check_auth("memory"):
        return err
    try:
        agent_id = os.environ.get("KILN_AGENT_ID", "default")
        effective_scope = f"printer:{printer_name}" if printer_name else scope
        deleted = get_db().delete_memory(agent_id, effective_scope, key)
        if not deleted:
            return _error_dict(
                f"No memory entry found for key '{key}' in scope '{effective_scope}'.",
                code="NOT_FOUND",
            )
        return {"success": True, "key": key, "scope": effective_scope}
    except Exception as exc:
        logger.exception("Unexpected error in delete_agent_note")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
                profiles.append({
                    "id": p.id,
                    "display_name": p.display_name,
                    "max_hotend_temp": p.max_hotend_temp,
                    "max_bed_temp": p.max_bed_temp,
                })
            except KeyError:
                continue
        return {"success": True, "count": len(profiles), "profiles": profiles}
    except Exception as exc:
        logger.exception("Unexpected error in list_safety_profiles")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Slicer profile tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_slicer_profiles_tool() -> dict:
    """List all bundled slicer profiles for supported printers.

    Returns profile IDs, display names, and recommended slicer for each.
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
                profiles.append({
                    "id": p.id,
                    "display_name": p.display_name,
                    "slicer": p.slicer,
                })
            except KeyError:
                continue
        return {"success": True, "count": len(profiles), "profiles": profiles}
    except Exception as exc:
        logger.exception("Unexpected error in list_slicer_profiles_tool")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def get_slicer_profile_tool(printer_id: str) -> dict:
    """Get the full bundled slicer profile for a printer model.

    Returns all INI settings (layer height, speeds, temps, retraction, etc.)
    and the recommended slicer.

    Args:
        printer_id: Printer model identifier (e.g. ``"ender3"``,
            ``"bambu_x1c"``).
    """
    if err := _check_auth("slicer"):
        return err
    try:
        profile = get_slicer_profile(printer_id)
        return {"success": True, "profile": slicer_profile_to_dict(profile)}
    except KeyError:
        return _error_dict(
            f"No slicer profile for '{printer_id}' and no default available.",
            code="NOT_FOUND",
        )
    except Exception as exc:
        logger.exception("Unexpected error in get_slicer_profile_tool")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
                f"No settings for '{material}' on {intel.display_name}. "
                f"Available: {', '.join(available)}",
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
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


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
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Kiln MCP server."""
    # Configure structured logging if requested (before any log calls).
    _configure_logging()

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
    logger.info("Kiln scheduler and webhook delivery started")

    # Graceful shutdown handler
    def _shutdown_handler(signum: int, frame: Any) -> None:
        try:
            sig_name = signal.Signals(signum).name
        except (ValueError, AttributeError):
            sig_name = f"signal {signum}"
        logger.info("Received %s — shutting down gracefully...", sig_name)
        _scheduler.stop()
        _webhook_mgr.stop()
        _stream_proxy.stop()
        if _cloud_sync is not None:
            _cloud_sync.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    # Atexit as fallback
    atexit.register(_scheduler.stop)
    atexit.register(_webhook_mgr.stop)
    atexit.register(_stream_proxy.stop)
    if _cloud_sync is not None:
        atexit.register(_cloud_sync.stop)

    mcp.run()


if __name__ == "__main__":
    main()
