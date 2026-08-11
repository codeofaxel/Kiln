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
    ``"moonraker"``, ``"creality"``, ``"bambu"``, ``"elegoo"``,
    ``"prusalink"``, ``"duet"``, and ``"usb"``.
    Defaults to ``"octoprint"``.
    ``"serial"`` is accepted as a legacy alias for ``"usb"``.
``KILN_PRINTER_PORT``
    Serial port path for USB printers (required when ``KILN_PRINTER_TYPE``
    is ``"usb"``).  E.g. ``"/dev/ttyUSB0"`` or ``"COM3"``.
``KILN_PRINTER_BAUDRATE``
    Baud rate for USB printers (default 115200; many Marlin boards are
    flashed for 250000).
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

# SECURITY NOTE:
# All mutating tools should call _check_auth(...) or _check_billing_auth(...).
# _check_auth reads per-request MCP metadata first (when available), then
# falls back to KILN_MCP_AUTH_TOKEN for compatibility with existing clients.

from __future__ import annotations

import atexit

# Import kiln-pro early so compatibility shims are installed before
# any try/except imports of pro modules (kiln.billing, kiln.licensing, etc.).
import contextlib
import functools
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
import uuid as _uuid_mod
from contextvars import ContextVar
from pathlib import Path
from types import MethodType
from typing import Any

from kiln.mcp_compat import FastMCP, set_instructions

with contextlib.suppress(ImportError):
    import kiln_pro  # noqa: F401 — triggers compat shim installation


from kiln import parse_float_env, parse_int_env
from kiln.auth import AuthManager
from kiln.bed_leveling import BedLevelManager, LevelingPolicy

try:
    from kiln.billing import BillingLedger
except ImportError:
    BillingLedger = None  # Available in kiln-pro

try:
    from kiln.billing_alerts import BillingAlertManager
except ImportError:
    BillingAlertManager = None  # Available in kiln-pro
from kiln.cli.config import _normalize_printer_type, _validate_printer_url, save_printer
from kiln.cloud_sync import CloudSyncManager, SyncConfig
from kiln.cost_estimator import CostEstimator
from kiln.errors import HostedUnavailableError
from kiln.events import Event, EventBus, EventType

try:
    from kiln.fulfillment import (
        FulfillmentError,
        FulfillmentProvider,
        QuoteRequest,
    )
    from kiln.fulfillment import (
        get_provider as get_fulfillment_provider,
    )
except ImportError:
    FulfillmentError = None  # Available in kiln-pro
    FulfillmentProvider = None
    QuoteRequest = None
    get_fulfillment_provider = None
from kiln.gateway.threedos import ThreeDOSClient
from kiln.gcode import validate_gcode as _validate_gcode_impl
from kiln.gcode import validate_gcode_for_printer
from kiln.generation import (
    GeminiDeepThinkProvider,
    GenerationError,
    GenerationProvider,
    MeshyProvider,
    OpenSCADProvider,
    StabilityProvider,
    Tripo3DProvider,
    validate_mesh,
)
from kiln.heater_watchdog import HeaterWatchdog
from kiln.tool_results import unwrap_tool_result

try:
    from kiln.licensing import (
        FREE_TIER_MAX_PRINTERS,
        LicenseTier,
        check_tier,
        get_tier,
        requires_tier,
    )
    # Pro / Business caps are supplied by kiln-pro's licensing module
    # when installed.  Free users never have kiln-pro so the fallback
    # block below is their runtime state.
    try:
        from kiln.licensing import (
            BUSINESS_TIER_MAX_PRINTERS,
            PRO_TIER_MAX_PRINTERS,
            max_printers_for_tier,
        )
    except ImportError:
        PRO_TIER_MAX_PRINTERS = 1
        BUSINESS_TIER_MAX_PRINTERS = 50

        def max_printers_for_tier(tier: object) -> int | None:
            value = getattr(tier, "value", tier)
            if value == "free":
                return FREE_TIER_MAX_PRINTERS
            if value == "pro":
                return PRO_TIER_MAX_PRINTERS
            if value == "business":
                return BUSINESS_TIER_MAX_PRINTERS
            return None  # enterprise → unlimited

except ImportError:
    # Free-tier fallback when kiln-pro is not installed — which is nearly every
    # install, so THIS is the cap most users actually run under, not the branch
    # above.  That asymmetry is what let it rot: the numbers here read 2 and 5
    # against an enforced 1 and 1 for months, and could not be noticed locally
    # by anyone who HAD kiln-pro installed, because they never execute there.
    # Public CI runs this branch on every job and asserted nothing about it.
    # Pinned now by kiln-pro's scripts/audit_tier_claims.py, which compares
    # these literals to the licensing constants across the repo boundary.
    FREE_TIER_MAX_PRINTERS = 1
    PRO_TIER_MAX_PRINTERS = 1
    BUSINESS_TIER_MAX_PRINTERS = 50

    def max_printers_for_tier(tier: object) -> int | None:  # type: ignore[no-redef]
        value = getattr(tier, "value", tier)
        if value == "free":
            return FREE_TIER_MAX_PRINTERS
        if value == "pro":
            return PRO_TIER_MAX_PRINTERS
        if value == "business":
            return BUSINESS_TIER_MAX_PRINTERS
        return None

    class _DummyTier:
        """Stub for LicenseTier when licensing module is not installed."""

        PRO = "pro"
        ENTERPRISE = "enterprise"
        BUSINESS = "business"
        FREE = "free"

    LicenseTier = _DummyTier  # type: ignore[misc]

    def check_tier(required, *_a, **_kw):
        # Imported locally: this stub is defined above the module's import
        # block, and the message is the one a caller shows a person.  The
        # (ok, message) tuple is an established contract with no room for an
        # agent-addressed field, so callers that build a response dict around
        # it splat ``signin_hint_fields()`` in themselves.
        from kiln.tiers_and_terms import tier_required_message

        tier_label = getattr(required, "value", required) if required else "pro"
        return (False, tier_required_message("This feature", str(tier_label)))

    def get_tier(*_a, **_kw):
        return "free"

    def requires_tier(_tier):
        """Gate pro/enterprise features when kiln-pro is not installed."""
        tier_label = getattr(_tier, "value", _tier) if _tier else "pro"

        def decorator(fn):
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                tool_name = fn.__name__
                # Funnel-leak telemetry: every TIER_REQUIRED on this
                # path is a user reaching for a locked door.  Counted
                # in daily_stats and rolled up in the heartbeat so we
                # can see which tools are driving "I paid but my agent
                # doesn't know" support volume.  Best-effort; never
                # blocks the error path.
                try:
                    from kiln.daily_stats import record_tier_denial
                    record_tier_denial(tool_name)
                except Exception:
                    pass
                from kiln.tiers_and_terms import (
                    signin_hint_fields,
                    tier_required_message,
                )

                return {
                    "success": False,
                    "error": tier_required_message(tool_name, str(tier_label)),
                    "code": "TIER_REQUIRED",
                    "required_tier": str(tier_label),
                    "tool": tool_name,
                    "retryable": False,
                    "upgrade_url": "https://kiln3d.com/pricing",
                    **signin_hint_fields(),
                }

            return wrapper

        return decorator


from kiln.log_config import configure_logging as _configure_log_rotation
from kiln.marketplaces import (
    Cults3DAdapter,
    MakerWorldAdapter,
    MarketplaceError,
    MarketplaceRegistry,
    MyMiniFactoryAdapter,
    ThingiverseAdapter,
)
from kiln.marketplaces import (
    MarketplaceNotFoundError as MktNotFoundError,
)
from kiln.materials import MaterialTracker

try:
    from kiln_pro.payments.base import PaymentError
except ImportError:
    PaymentError = None  # Available in kiln-pro

try:
    from kiln_pro.payments.manager import PaymentManager
except ImportError:
    PaymentManager = None  # Available in kiln-pro
from kiln.persistence import get_db
from kiln.pipelines import (
    PipelineState as _PipelineState,  # noqa: F401 — used by plugins/pipeline_tools.py via _srv
)
from kiln.pipelines import (
    benchmark as _pipeline_benchmark,
)
from kiln.pipelines import (
    calibrate as _pipeline_calibrate,
)
from kiln.pipelines import (
    get_execution as _get_execution,  # noqa: F401 — used by plugins/pipeline_tools.py via _srv
)
from kiln.pipelines import (
    list_pipelines as _list_pipelines,  # noqa: F401 — used by plugins/pipeline_tools.py via _srv
)
from kiln.pipelines import (
    quick_print as _pipeline_quick_print,
)
from kiln.pipelines import (
    reslice_and_print as _pipeline_reslice_and_print,
)
from kiln.plugin_loader import register_all_plugins
from kiln.plugins import PluginContext, PluginManager
from kiln.printer_backends import DEFAULT_SERIAL_BAUDRATE, format_printer_types
from kiln.printer_intelligence import (
    diagnose_issue,
    get_material_settings,
    get_printer_intel,
    intel_to_dict,
)
from kiln.printers import (
    BambuAdapter,
    CrealityAdapter,
    DuetAdapter,
    ElegooAdapter,
    MoonrakerAdapter,
    OctoPrintAdapter,
    PrinterAdapter,
    PrinterError,
    PrinterStatus,
    PrusaLinkAdapter,
    SerialPrinterAdapter,
)
from kiln.queue import JobNotFoundError, JobStatus, PrintQueue
from kiln.registry import PrinterNotFoundError, PrinterRegistry
from kiln.safety_profiles import export_profile as _export_profile
from kiln.scheduler import JobScheduler
from kiln.slicer_profiles import (
    resolve_slicer_profile,
)
from kiln.streaming import MJPEGProxy
from kiln.thingiverse import (
    ThingiverseClient,
    ThingiverseError,
    ThingiverseNotFoundError,
)
from kiln.tiers_and_terms import (
    AGENT_ACCOUNT_NUDGE,
    TIERS_AND_TERMS,
    account_required_message,
    session_expired_message,
    signin_hint_fields,
    tier_required_message,
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
_PRINTER_TYPE: str = _normalize_printer_type(os.environ.get("KILN_PRINTER_TYPE", "octoprint"))
_PRINTER_SERIAL: str = os.environ.get("KILN_PRINTER_SERIAL", "")
_PRINTER_MODEL: str = os.environ.get("KILN_PRINTER_MODEL", "")

# Provenance string for the active printer config — set by
# ``_reload_env_config``. Logged prominently at startup so users can
# see whether credentials came from the environment or ~/.kiln/config.yaml
# (critical for debugging stale env vars that silently shadow config edits).
_PRINTER_CONFIG_SOURCE: str = "unset"
# PrusaSlicer defaults to conservative speeds (~45mm/s) designed for
# generic printers. Modern printers (especially Bambu with input shaping)
# can handle much higher speeds safely. These overrides are injected
# automatically when slicing via slice_and_print / run_reslice_and_print.
_PRINTER_SPEED_OVERRIDES: dict[str, dict[str, str]] = {
    "bambu": {
        "perimeter_speed": "150",
        "external_perimeter_speed": "100",
        "infill_speed": "250",
        "solid_infill_speed": "150",
        "top_solid_infill_speed": "100",
        "first_layer_speed": "50",
        "travel_speed": "350",
        "max_print_speed": "300",
        "default_acceleration": "5000",
        "infill_acceleration": "10000",
        "first_layer_acceleration": "1000",
    },
    "octoprint": {
        # Conservative: most OctoPrint users have Ender 3 / similar
        "perimeter_speed": "50",
        "external_perimeter_speed": "35",
        "infill_speed": "80",
        "solid_infill_speed": "50",
        "top_solid_infill_speed": "40",
        "first_layer_speed": "30",
        "travel_speed": "150",
        "max_print_speed": "100",
    },
    "moonraker": {
        # Klipper with input shaping — moderately fast
        "perimeter_speed": "100",
        "external_perimeter_speed": "70",
        "infill_speed": "150",
        "solid_infill_speed": "100",
        "top_solid_infill_speed": "70",
        "first_layer_speed": "40",
        "travel_speed": "250",
        "max_print_speed": "200",
        "default_acceleration": "3000",
    },
    "creality": {
        # Creality K/Hi/Ender V3 KE class machines expose Klipper through Moonraker.
        "perimeter_speed": "160",
        "external_perimeter_speed": "100",
        "infill_speed": "220",
        "solid_infill_speed": "140",
        "top_solid_infill_speed": "90",
        "first_layer_speed": "45",
        "travel_speed": "350",
        "max_print_speed": "250",
        "default_acceleration": "5000",
    },
}

_CONFIRM_UPLOAD: bool = os.environ.get("KILN_CONFIRM_UPLOAD", "").lower() in ("1", "true", "yes")
_CONFIRM_MODE: bool = os.environ.get("KILN_CONFIRM_MODE", "").lower() in ("1", "true", "yes")
_THINGIVERSE_TOKEN: str = os.environ.get("KILN_THINGIVERSE_TOKEN", "")
_THINGIVERSE_DEPRECATION_NOTICE: str = (
    "Thingiverse was acquired by MyMiniFactory in February 2026. "
    "The API may be sunset. Consider using MyMiniFactory "
    "(source: myminifactory) as the primary marketplace."
)
_MMF_API_KEY: str = os.environ.get("KILN_MMF_API_KEY", "") or __import__("base64").b64decode(
    b"NGUxMzhkZmQtOTliNC00YjlmLWJkMmYtOTQ4OTQ1ZDYyOTNh"
).decode()  # Kiln app key for MMF model search — users can override via env var
_CULTS3D_USERNAME: str = os.environ.get("KILN_CULTS3D_USERNAME", "")
_CULTS3D_API_KEY: str = os.environ.get("KILN_CULTS3D_API_KEY", "")

# Actionable setup guide shown when no marketplaces are configured.
_MARKETPLACE_SETUP_GUIDE = (
    "No marketplace credentials configured. To enable model search, set API keys for at least one:\n"
    "\n"
    "1. MyMiniFactory (recommended) — get your API key at https://myminifactory.com/settings/developer"
    " → export KILN_MMF_API_KEY=your_key\n"
    "2. Cults3D (search only, no downloads) — get your API key at https://cults3d.com/en/api/keys"
    " → export KILN_CULTS3D_USERNAME=your_username && export KILN_CULTS3D_API_KEY=your_key\n"
    "3. Thingiverse (deprecated — acquired by MyMiniFactory, Feb 2026) — create an app at"
    " https://www.thingiverse.com/apps/create → export KILN_THINGIVERSE_TOKEN=your_token"
)

_CRAFTCLOUD_API_KEY: str = os.environ.get("KILN_CRAFTCLOUD_API_KEY", "")
_FULFILLMENT_PROVIDER: str = os.environ.get("KILN_FULFILLMENT_PROVIDER", "")
_MESHY_API_KEY: str = os.environ.get("KILN_MESHY_API_KEY", "")
_GEMINI_API_KEY: str = os.environ.get("KILN_GEMINI_API_KEY", "")
_ESTOP_INPUT_TOKEN: str = os.environ.get("KILN_ESTOP_INPUT_TOKEN", "")

# Auto-print toggles: OFF by default for safety.  Generated models are
# higher risk than marketplace downloads — two independent toggles let
# users opt in to each separately.
_AUTO_PRINT_MARKETPLACE: bool = os.environ.get("KILN_AUTO_PRINT_MARKETPLACE", "").lower() in ("1", "true", "yes")
_AUTO_PRINT_GENERATED: bool = os.environ.get("KILN_AUTO_PRINT_GENERATED", "").lower() in ("1", "true", "yes")

# Heater watchdog: minutes of idle heater time before auto-cooldown (0=disabled).
_HEATER_TIMEOUT_MIN: float = parse_float_env("KILN_HEATER_TIMEOUT", 30.0)

# Default snapshot directory — use ~/.kiln/snapshots/ instead of /tmp to
# avoid macOS periodic /tmp cleanup deleting saved snapshots.


def _key_fingerprint(key: str) -> str:
    """Return a short non-reversible fingerprint of *key* for log output.

    Logs need to answer "which key won?" and "do env and YAML disagree?"
    without exposing key material.  A truncated prefix is not safe here:
    Bambu LAN access codes are only 8 characters, so even a 4-character
    prefix halves the secret.  A hash digest keeps keys comparable in
    logs while revealing nothing about their content.
    """
    if not key:
        return "(empty)"
    import hashlib

    # SHA-256 (not a slow KDF like bcrypt/argon2) is intentional and safe
    # here: this is a log-correlation fingerprint, not password storage.
    # The digest is never persisted and never used to authenticate — it
    # only masks a key in logs and lets us compare whether env and YAML
    # resolved to the same key.  A salted slow hash would be
    # non-deterministic and break that comparison, for no security gain.
    # (Same rationale as auth.py:_hash_key.)
    return "sha256:" + hashlib.sha256(key.encode()).hexdigest()[:8]


def _reload_env_config() -> None:
    """Re-read env-backed configuration globals after .env has been loaded.

    Module-level env reads happen at import time, which is before
    ``main()`` calls ``load_dotenv()``.  This function refreshes them
    so that settings from ``.env`` files are picked up correctly.
    """
    global _PRINTER_HOST, _PRINTER_API_KEY, _PRINTER_TYPE  # noqa: PLW0603
    global _PRINTER_SERIAL, _PRINTER_MODEL  # noqa: PLW0603
    global _PRINTER_CONFIG_SOURCE  # noqa: PLW0603
    global _CONFIRM_UPLOAD, _CONFIRM_MODE  # noqa: PLW0603
    global _THINGIVERSE_TOKEN, _MMF_API_KEY  # noqa: PLW0603
    global _CULTS3D_USERNAME, _CULTS3D_API_KEY, _CRAFTCLOUD_API_KEY  # noqa: PLW0603
    global _FULFILLMENT_PROVIDER, _MESHY_API_KEY  # noqa: PLW0603
    global _ESTOP_INPUT_TOKEN  # noqa: PLW0603
    global _AUTO_PRINT_MARKETPLACE, _AUTO_PRINT_GENERATED  # noqa: PLW0603
    global _HEATER_TIMEOUT_MIN  # noqa: PLW0603

    _PRINTER_HOST = os.environ.get("KILN_PRINTER_HOST", "")
    _PRINTER_API_KEY = os.environ.get("KILN_PRINTER_API_KEY", "")
    _PRINTER_TYPE = _normalize_printer_type(os.environ.get("KILN_PRINTER_TYPE", "octoprint"))
    _PRINTER_SERIAL = os.environ.get("KILN_PRINTER_SERIAL", "")
    _PRINTER_MODEL = os.environ.get("KILN_PRINTER_MODEL", "")

    # Printer credential resolution — ONE SOURCE OF TRUTH:
    #   ``~/.kiln/config.yaml`` WINS when it has a printer with a host.
    #   ``KILN_PRINTER_*`` env vars are only used when the YAML file is
    #   absent or has no active printer configured (fresh installs, CI).
    #
    # Why we inverted the documented "env > yaml" precedence:
    #   Stale ``KILN_PRINTER_*`` env vars inherited from a long-dead shell
    #   session (or a parent MCP-host process that captured them at its
    #   launch time) would silently shadow every config.yaml edit for the
    #   life of the parent process.  Users would edit the YAML, see no
    #   effect, and burn hours chasing a phantom.  Making YAML authoritative
    #   means editing the file always works and ``kiln`` CLI + MCP server
    #   always see the same printer.
    #
    # Escape hatch: set ``KILN_PRINTER_CONFIG_IGNORE_YAML=1`` to force the
    # old env-first behaviour (for CI pipelines that deliberately override
    # a checked-in config.yaml).  Otherwise, edit the YAML or delete it.
    #
    # Even when YAML wins, we still surface any env/YAML disagreement as
    # a warning at startup so the ghost-env footgun is impossible to miss.
    _yaml_cfg: dict = {}
    try:
        from kiln.cli.config import _read_config_file as _read_yaml
        from kiln.cli.config import get_config_path as _get_cfg_path

        _yaml_full = _read_yaml(_get_cfg_path()) or {}
        _active = _yaml_full.get("active_printer") or "default"
        _printers = _yaml_full.get("printers") or {}
        _yaml_cfg = _printers.get(_active, {}) or _printers.get("default", {}) or {}
    except Exception:  # noqa: BLE001
        _yaml_cfg = {}

    _force_env = os.environ.get("KILN_PRINTER_CONFIG_IGNORE_YAML", "").lower() in (
        "1",
        "true",
        "yes",
    )
    _yaml_has_printer = bool(_yaml_cfg.get("host"))

    if _yaml_has_printer and not _force_env:
        # YAML wins — override any env-derived values we just read.
        _PRINTER_HOST = str(_yaml_cfg.get("host", ""))
        # Normalize like the env path above: a config.yaml pinning a renamed
        # type (prusaconnect, serial) reaches the dispatcher through here,
        # and without this the alias table simply never ran on the YAML path.
        _PRINTER_TYPE = _normalize_printer_type(str(_yaml_cfg.get("type", "octoprint")))
        # Bambu stores the LAN Access Code under `access_code`; every
        # other backend uses `api_key`.  Internally the server treats
        # them interchangeably via `_PRINTER_API_KEY`.
        _PRINTER_API_KEY = str(
            _yaml_cfg.get("api_key") or _yaml_cfg.get("access_code") or ""
        )
        _PRINTER_SERIAL = str(_yaml_cfg.get("serial", ""))
        if not _PRINTER_MODEL:
            _PRINTER_MODEL = str(_yaml_cfg.get("printer_model", ""))
        masked_key = _key_fingerprint(_PRINTER_API_KEY)
        _PRINTER_CONFIG_SOURCE = (
            f"~/.kiln/config.yaml (host={_PRINTER_HOST}, "
            f"type={_PRINTER_TYPE}, api_key={masked_key}, "
            f"serial={_PRINTER_SERIAL or '(none)'})"
        )
    elif _PRINTER_HOST:
        masked_key = _key_fingerprint(_PRINTER_API_KEY)
        _PRINTER_CONFIG_SOURCE = (
            f"env vars (KILN_PRINTER_HOST={_PRINTER_HOST}, "
            f"type={_PRINTER_TYPE}, api_key={masked_key}, "
            f"serial={_PRINTER_SERIAL or '(none)'})"
            + (" [forced via KILN_PRINTER_CONFIG_IGNORE_YAML]" if _force_env else "")
        )
    else:
        _PRINTER_CONFIG_SOURCE = "unset (no ~/.kiln/config.yaml printer, no env vars)"
    _CONFIRM_UPLOAD = os.environ.get("KILN_CONFIRM_UPLOAD", "").lower() in ("1", "true", "yes")
    _CONFIRM_MODE = os.environ.get("KILN_CONFIRM_MODE", "").lower() in ("1", "true", "yes")
    _THINGIVERSE_TOKEN = os.environ.get("KILN_THINGIVERSE_TOKEN", "")
    _MMF_API_KEY = os.environ.get("KILN_MMF_API_KEY", "")
    _CULTS3D_USERNAME = os.environ.get("KILN_CULTS3D_USERNAME", "")
    _CULTS3D_API_KEY = os.environ.get("KILN_CULTS3D_API_KEY", "")
    _CRAFTCLOUD_API_KEY = os.environ.get("KILN_CRAFTCLOUD_API_KEY", "")
    _FULFILLMENT_PROVIDER = os.environ.get("KILN_FULFILLMENT_PROVIDER", "")
    _MESHY_API_KEY = os.environ.get("KILN_MESHY_API_KEY", "")
    _GEMINI_API_KEY = os.environ.get("KILN_GEMINI_API_KEY", "")
    _ESTOP_INPUT_TOKEN = os.environ.get("KILN_ESTOP_INPUT_TOKEN", "")
    _AUTO_PRINT_MARKETPLACE = os.environ.get("KILN_AUTO_PRINT_MARKETPLACE", "").lower() in ("1", "true", "yes")
    _AUTO_PRINT_GENERATED = os.environ.get("KILN_AUTO_PRINT_GENERATED", "").lower() in ("1", "true", "yes")
    _HEATER_TIMEOUT_MIN = parse_float_env("KILN_HEATER_TIMEOUT", 30.0)


# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------


# Tiers & terms guidance surfaced in the server instructions.  The text now
# lives in ``kiln.tiers_and_terms`` because the MCP server is only one of the
# surfaces that must carry it (the agent loop, the hosted remote-MCP
# connector, and the desktop chat prompt are the others); a literal here
# meant three of the four silently shipped without it.  Aliased so both the
# dynamic builder (``_build_instructions``) and the static FastMCP fallback
# below keep reading one definition.
_TIERS_AND_TERMS = TIERS_AND_TERMS


def _build_instructions() -> str:
    """Build context-aware MCP instructions based on the user's config.

    Reads registered printers, marketplace API keys, slicer availability,
    generation providers, and plugin state to produce a concise capability
    summary.  Called once in ``main()`` after env config is loaded.

    When nothing is configured (fresh install), the instructions guide the
    agent through a conversational first-time setup so normie users never
    need to touch env vars or config files.
    """
    printer_names = _get_registry().list_names()
    sources = _marketplace_registry.connected
    has_printer = len(printer_names) > 0
    has_marketplace = len(sources) > 0
    is_fresh = not has_printer and not has_marketplace

    # Live tool count — single source of truth; re-computed on every
    # instructions build so agents see the current registry, never a stale
    # number hardcoded into a string literal.
    try:
        from kiln.skill_manifest import get_tool_count

        _tool_count = get_tool_count()
    except Exception:
        _tool_count = 0
    _count_phrase = f"{_tool_count} tools" if _tool_count else "hundreds of tools"

    parts: list[str] = [
        "START HERE: before anything else, call `get_started()` and "
        "`get_skill_manifest()`. These return the full capability map, "
        "agent rules, common workflows, and current tool count — "
        "everything else is downstream. "
        f"This server has {_count_phrase}; use `ToolSearch(keyword)` to "
        "load schemas on demand instead of guessing names.",
        "Kiln — AI agent infrastructure for 3D printing. "
        "You control physical printers, search model marketplaces, "
        "slice files, and manage print jobs through these tools.",
    ]

    # Tiers & terms guidance, surfaced early so agents read it before they
    # reach for tier-gated tools.  Shared constant -- see the static fallback.
    parts.append(_TIERS_AND_TERMS)

    # --- Update nudge (non-blocking; reads the cached PyPI check) ---
    # A new-version notice an agent reads on connect, so it can pass the
    # heads-up to the user.  Wrapped defensively: a nudge must never break
    # the instructions build.
    try:
        from kiln.version_check import update_banner_line

        _update_line = update_banner_line()
        if _update_line:
            parts.append(
                f"UPDATE AVAILABLE: {_update_line} "
                "Offer to handle it for them — ask 'want me to update Kiln for "
                "you now?' and, on yes, call the upgrade_kiln tool (never while "
                "a print is active). Don't just tell them to run a command."
            )
    except Exception:  # noqa: BLE001 -- nudge is best-effort, never fatal
        pass

    # --- Account nudge (only when signed out) ---
    # Surfaced once at connect so the agent knows a free account exists and
    # why it helps, and can offer it at the natural moment (a save / share)
    # rather than nagging. Reads the same token file the CLI writes; a signed
    # in user never sees this line. Best-effort — never break the build.
    try:
        from kiln.cli.auth_commands import _read_tokens

        if not _read_tokens().get("access_token"):
            parts.append(f"ACCOUNT: {AGENT_ACCOUNT_NUDGE}")
    except Exception:  # noqa: BLE001 -- nudge is best-effort, never fatal
        pass

    # --- Fresh install: first-time setup guidance ---
    if is_fresh:
        parts.append(
            "FIRST-TIME SETUP: No printer or marketplace is configured yet. "
            "Welcome the user and walk them through setup conversationally:\n"
            "1. Ask what 3D printer they have (brand/model) and how it connects "
            "(OctoPrint, Klipper/Moonraker, Bambu Lab, or USB serial).\n"
            "2. Run `discover_printers` to auto-detect printers on their network.\n"
            "3. Once you have the host URL and API key, use `register_printer` "
            "to connect it — no config files needed.\n"
            "4. Confirm the connection with `printer_status`.\n"
            "5. Optionally ask if they want to search for 3D models online "
            "(needs a free Thingiverse token — guide them to get one if interested).\n"
            "Keep it casual and brief. Most users just need steps 1-4."
        )
    else:
        # --- Printer status ---
        if has_printer:
            ptype = _PRINTER_TYPE or "octoprint"
            if len(printer_names) == 1:
                parts.append(
                    f'PRINTER: 1 {ptype} printer registered ("{printer_names[0]}"). Use `printer_status` to check it.'
                )
            else:
                parts.append(
                    f"FLEET: {len(printer_names)} printers registered. "
                    "Use `fleet_status` for overview, `printer_status` for details."
                )

        # --- Model marketplaces ---
        if has_marketplace:
            parts.append(
                f"MODELS: {', '.join(sources)} connected. "
                "Use `search_all_models` to find printable designs, "
                "`download_and_upload` to send directly to printer."
            )

    # --- 3D model generation ---
    gen_providers: list[str] = []
    if _MESHY_API_KEY:
        gen_providers.append("Meshy")
    if _GEMINI_API_KEY:
        gen_providers.append("Gemini")
    if gen_providers:
        parts.append(
            f"GENERATION: {', '.join(gen_providers)} available. "
            "Use `generate_model` to create 3D models from text descriptions."
        )

    # --- Slicer ---
    try:
        from kiln.slicer import find_slicer

        slicer_info = find_slicer()
        parts.append(f"SLICER: {slicer_info.name} found. Use `slice_model` to convert STL/3MF to G-code.")
    except Exception:
        pass  # No slicer — omit rather than add noise

    # --- Fulfillment ---
    if _CRAFTCLOUD_API_KEY:
        parts.append("FULFILLMENT: Craftcloud connected. Use `fulfillment_quote` to outsource prints.")

    # --- Safety summary ---
    safety_notes: list[str] = []
    if _AUTO_PRINT_MARKETPLACE:
        safety_notes.append("auto-print ON for marketplace downloads")
    if _AUTO_PRINT_GENERATED:
        safety_notes.append("auto-print ON for generated models")
    if not safety_notes:
        safety_notes.append("auto-print OFF (safe default)")
    parts.append(
        "SAFETY: Always run `preflight_check` before printing. "
        + "; ".join(safety_notes)
        + ". Use `safety_settings` to review."
    )

    # --- Monitoring & reporting ---
    parts.append(
        "MONITORING: During print jobs, use `monitor_print()` for status — "
        "NOT CLI commands. It returns progress, temperatures, time remaining, "
        "cost estimate, and a camera snapshot path. You MUST:\n"
        "  1. Display the FULL report text to the user — never summarize or omit fields.\n"
        "  2. Read the snapshot image file and show it inline — "
        "never just print a file path.\n"
        "  3. Always include the estimated cost line.\n"
        "  4. Prefer MCP tools over CLI for ALL Kiln operations."
    )

    # --- Design intelligence ---
    parts.append(
        "DESIGN INTELLIGENCE: Kiln has a comprehensive design knowledge system "
        "(25 materials with 45 brand-specific filament profiles, 18 design templates). Key tools:\n"
        "  - `design_session(verb=\"start\", idea=\"...\")` — user-facing entry point for any new design (captures saved goal at duty / environment / materials / safety layer)\n"
        "  - `analyze_design_requirements(requirements)` — internal functional-analysis lookup `design_session` calls into\n"
        "  - `recommend_design_material(use_case)` — intelligent material selection\n"
        "  - `find_design_templates(use_case)` — proven design templates\n"
        "  - `get_material_design_profile(material)` — material-specific rules\n"
        "  - `estimate_structural_load(...)` — load capacity analysis\n"
        "  - `validate_design_for_requirements(...)` — design verification\n"
        "  - `troubleshoot_print_issue(...)` — issue diagnosis\n"
        "  - `get_post_processing_guide(material)` — finishing guidance\n"
        "Call `get_skill_manifest()` for the full capability map."
    )

    # --- Generation workflow ---
    if gen_providers:
        parts.append(
            "GENERATION WORKFLOW: After generating a model, you MUST call "
            "`preview_generated_model` to render multi-angle previews (including "
            "bottom view for bed adhesion) BEFORE printing. Show the preview "
            "images to the user and check for issues. Never skip preview.\n"
            "Start any new design with `design_session(verb=\"start\", idea=\"...\")` so "
            "the saved goal drives the audit and the post-print review.\n"
            "Use `build_generation_prompt()` to enhance prompts with design intelligence."
        )

    # --- Visualization ---
    parts.append(
        "VISUALIZATION: Before printing ANY model (generated, downloaded, or custom), "
        "call `visualize_model(file_path)` to render 6-angle previews (iso, front, "
        "right, top, bottom, back). Show ALL preview images to the user. "
        "Auto-detects optimal camera distance from model bounding box. "
        "Works with STL, 3MF, OBJ, and SCAD files. Never skip visualization."
    )

    # --- Recovery ---
    parts.append(
        "FAILURE RECOVERY: If a print fails, use `analyze_print_failure_smart()`"
        "for root cause analysis, then `get_recovery_plan()` for options. "
        "Use `retry_print_with_fix()` to re-slice with corrections applied."
    )

    # --- Server management ---
    parts.append(
        "SERVER: Call `restart_server()` to hot-restart the Kiln MCP server "
        "without closing the client app. Use after plugin updates, env var "
        "changes, or code edits. The client auto-reconnects in ~1 second."
    )

    # --- Multi-color ---
    if has_printer:
        parts.append(
            "MULTI-COLOR: Use `ams_status()` to check loaded AMS filaments. "
            "Use `multi_material_print()` for different objects in different materials, "
            "or `kiln slice model.stl --copies N --ams-mapping 0,1,2 --print-after` "
            "for same-object multi-color copies."
        )

    # --- Natural language guidance ---
    parts.append(
        'The user may ask in plain language (e.g. "what\'s my printer doing?", '
        '"find me a phone stand", "print that benchy"). Map their intent '
        "to the appropriate tool. When uncertain, ask — don't guess on "
        "physical operations."
    )

    # --- Agent rules & tool reference (from skill manifest) ---
    # Pull directly from the skill manifest so we have one source of truth.
    # This ensures agents see the rules and tool map on first connect without
    # having to discover or call get_skill_manifest() themselves.
    try:
        from kiln.skill_manifest import generate_manifest

        manifest = generate_manifest()

        # Agent behavioral rules
        if manifest.agent_rules:
            rules_str = "\n".join(f"  {i}. {rule}" for i, rule in enumerate(manifest.agent_rules, 1))
            parts.append(f"AGENT RULES (MUST FOLLOW):\n{rules_str}")

        # Tool quick-reference by use case
        if manifest.tool_recommendations:
            recs_str = "\n".join(f"  {use_case}: {tool}" for use_case, tool in manifest.tool_recommendations.items())
            parts.append(f"TOOL QUICK-REFERENCE:\n{recs_str}")

        # Key workflows
        if manifest.workflows:
            workflow_lines: list[str] = []
            for wf_name, steps in manifest.workflows.items():
                step_str = " → ".join(
                    s.split(" — ")[0]
                    for s in steps  # just the function names
                )
                workflow_lines.append(f"  {wf_name}: {step_str}")
            parts.append("WORKFLOWS:\n" + "\n".join(workflow_lines))
    except Exception:
        # Manifest not available — fall back to minimal guidance.
        parts.append(
            "AGENT RULES: Always use MCP tools (not CLI commands). "
            "Call get_skill_manifest() for the full capability map."
        )

    return "\n\n".join(parts)


# Static fallback for when the module is imported but main() hasn't run.
# main() replaces this with _build_instructions() after config is loaded.
mcp = FastMCP(
    "kiln",
    instructions=(
        # Static fallback seen ONLY during the MCP initialize handshake
        # before main() runs and replaces this with the full dynamic
        # instructions built by _build_instructions().  Kept short and
        # directive so the first tool call an agent makes lands on
        # get_started() instead of blind probing.
        "START HERE: before anything else, call `get_started()` and "
        "`get_skill_manifest()`. They return the full capability map, "
        "current tool count, and session context. This server has "
        "hundreds of tools — use `ToolSearch(keyword)` to load schemas "
        "on demand instead of guessing names. Kiln — AI agent "
        "infrastructure for 3D printing.\n\n" + _TIERS_AND_TERMS
    ),
)

_current_mcp_request_context: ContextVar[Any | None] = ContextVar(
    "kiln_current_mcp_request_context",
    default=None,
)


def _record_local_tool_call(name: str, result: Any = None) -> None:
    """Best-effort: feed the on-device usage ledger after a tool call.

    Exactly one recorder fires per machine, so local agent work counts on
    the user's ``/stats`` dashboard — not just web-app activity — without
    double-counting:

    * kiln-pro installed → ``pro_features.record_local_tool_call`` tallies
      the call in ``~/.kiln`` and syncs it when the user is signed in.
    * free install (no kiln-pro) → the public ``usage_ledger`` records the
      call locally and flushes to ``/api/me/stats/record`` when the user
      is signed in via ``kiln signin``.

    Two independent channels fire here:
    * the ANONYMOUS aggregate counter (``daily_stats.record_tool_call``) —
      always, regardless of kiln-pro / sign-in state; feeds the daily
      heartbeat, no identity attached.
    * the PER-USER ledger — exactly one recorder (kiln-pro OR the public
      ledger, never both) so signed-in work counts once on ``/stats``.

    NEVER raises: a stats hook must never break a tool call.
    """
    # Anonymous per-tool counter for the daily heartbeat — identity-free,
    # separate from the per-user ledger below.
    with contextlib.suppress(Exception):
        from kiln.daily_stats import record_tool_call

        record_tool_call(name)

    # Category counters (generations / decorations / textures /
    # downloads) for tools in daily_stats.TOOL_EVENT_MAP — the dispatch
    # chokepoint counts whole tool families, kiln-pro's included, so a
    # counter can't silently cover only the one tool that remembered to
    # record itself.  Failure-shaped results are skipped inside.
    with contextlib.suppress(Exception):
        from kiln.daily_stats import record_tool_event

        record_tool_event(name, result)

    try:
        from kiln_pro.bridge import pro_features
    except Exception:
        pro_features = None
    if pro_features is not None:
        # kiln-pro owns recording on this machine; do NOT also run the
        # public ledger below or the same call would be counted twice.
        with contextlib.suppress(Exception):
            pro_features.record_local_tool_call(name)
        return
    try:
        from kiln import usage_ledger

        usage_ledger.record(name)
        usage_ledger.maybe_flush()
    except Exception:
        pass


# --- Terms-of-Use gate (MCP) ------------------------------------------------
#
# The first substantive MCP tool call by an un-accepted identity short-circuits
# with a one-time consent gate, raised so the lowlevel MCP handler surfaces it to
# the agent as a tool error to relay.  Crucially, the agent CANNOT accept on the
# user's behalf: there is no accept tool, and an MCP agent has no browser and no
# shell — so the only paths to acceptance are human actions the agent can't take:
# tapping the account-scoped one-tap accept LINK (account / licensed installs) or
# running `kiln accept-terms` in their own terminal (no-account installs).  A
# small whitelist stays reachable so a new user can orient.  Once accepted,
# is_current() short-circuits on the local record and this never fires again.

_TERMS_GATE_WHITELIST = frozenset({"get_started", "check_my_tier", "kiln_health"})

# In-process: after the gate mints an accept link we force is_current() to poll
# the server fresh (bypassing its recheck throttle) for this window, so the
# user's tap is seen on the very next tool call instead of waiting it out.
_accept_link_pending_until = 0.0
_ACCEPT_LINK_PENDING_WINDOW_S = 900.0


def _mint_accept_link() -> str | None:
    """Mint a one-tap accept URL for this install's account, or ``None``.

    Reuses kiln.terms' bearer + hosted-API plumbing.  ``None`` when there's no
    account bearer (a no-account install) or the mint is offline/unavailable.
    """
    try:
        from kiln.terms import _account_bearer, _server_request

        bearer = _account_bearer()
        if not bearer:
            return None
        resp = _server_request("/api/terms/accept-link", "POST", bearer, {})
        if isinstance(resp, dict) and resp.get("url"):
            return str(resp["url"])
    except Exception:
        logger.debug("terms gate: accept-link mint failed", exc_info=True)
    return None


def _terms_consent_message() -> str:
    """The one-time consent gate the agent relays to the user.

    Offers a human action the agent cannot perform — tap the account-scoped
    accept link, or (no account) run ``kiln accept-terms``.  There is deliberately
    no in-chat accept path.
    """
    global _accept_link_pending_until
    from kiln.terms import _CURRENT_TERMS_VERSION, _TERMS_SUMMARY

    head = (
        f"One-time setup — Kiln Terms of Use acceptance required "
        f"(v{_CURRENT_TERMS_VERSION}).\n\n"
        "Before I can run Kiln tools for you, here are the terms:\n\n"
        f"{_TERMS_SUMMARY}\n\n"
    )

    link = _mint_accept_link()
    if link:
        import time as _time

        _accept_link_pending_until = _time.time() + _ACCEPT_LINK_PENDING_WINDOW_S
        return head + (
            "To accept, tap this link and click \"I Agree\" — about 5 seconds:\n"
            f"    {link}\n\n"
            "Then just tell me to continue and I'll pick right back up. "
            "(I can't accept for you — it has to be your tap.)"
        )

    # No account on this install — point the human at an action the agent also
    # can't take: a one-line terminal command, or signing in to accept in a
    # browser.
    return head + (
        "To accept, run this once in your terminal (about 5 seconds):\n"
        "    kiln accept-terms\n\n"
        "…or sign in at https://kiln3d.com and accept there. Then tell me to "
        "continue. (I can't accept for you — it has to be you.)"
    )


def _terms_gate_blocks(tool_name: str) -> bool:
    """True if this MCP tool call must be blocked pending terms acceptance."""
    if tool_name in _TERMS_GATE_WHITELIST:
        return False
    try:
        import time as _time

        from kiln.terms import is_current

        force = _time.time() < _accept_link_pending_until
        return not is_current(force_server=force)
    except Exception:
        # Never block a tool over a terms-check infrastructure error.
        return False


def _install_mcp_request_context_capture() -> None:
    """Capture current MCP request context so auth can read per-request metadata."""
    tool_mgr = mcp._tool_manager
    if getattr(tool_mgr, "_kiln_request_context_capture_installed", False):
        return

    original_call_tool = tool_mgr.call_tool

    async def _call_tool_with_context(
        self,
        name: str,
        arguments: dict[str, Any],
        context=None,
        convert_result: bool = False,
    ):
        token = _current_mcp_request_context.set(context)
        try:
            if _terms_gate_blocks(name):
                # One-time consent gate — raised so the lowlevel handler returns
                # it to the agent as a tool error to relay (see _terms_* above).
                raise RuntimeError(_terms_consent_message())
            result = await original_call_tool(
                name,
                arguments,
                context=context,
                convert_result=convert_result,
            )
            # Best-effort usage tally — only after a call that returned
            # (a tool that raised is not counted); cannot affect the
            # result and cannot raise.  The result rides along so mapped
            # outcome counters can skip failure-shaped returns.
            _record_local_tool_call(name, result)
            # Backstop for the turn-it-over link: the render path already
            # attaches one (model_visualizer), which covers every tool that
            # shows a preview.  This catches the rest — a tool that produces
            # a mesh without rendering it — so the capability is not a
            # per-tool branch anybody can forget.  The content-addressed
            # cache makes the already-attached case free.  Only a dict
            # result can carry a new key; once the MCP layer has serialised
            # a result into content blocks there is nothing left to attach
            # to, and rewriting serialised text to sneak one in is how a
            # wire format gets corrupted.
            # NOTE: no stage wiring here.  This hook runs with
            # convert_result=True, so `result` is already a list of content
            # blocks and any dict mutation is silently dropped — measured,
            # after first writing it here and watching it do nothing.  The
            # turn-it-over link is attached inside the render path
            # (model_visualizer), where the value is still a dict; the
            # inline-stage token is attached at the lowlevel CallToolRequest
            # handler (kiln.local_stage), where the result object is real.
            return result
        finally:
            _current_mcp_request_context.reset(token)

    tool_mgr.call_tool = MethodType(_call_tool_with_context, tool_mgr)
    tool_mgr._kiln_request_context_capture_installed = True


_install_mcp_request_context_capture()


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
    elif printer_type == "duet":
        # RepRapFirmware authenticates with a machine password (set by M551),
        # carried in the generic API-key slot.  Boards with no password
        # configured accept the firmware default, which the adapter supplies.
        _adapter = DuetAdapter(host=host, **({"password": api_key} if api_key else {}))
    elif printer_type == "creality":
        _adapter = CrealityAdapter(
            host=host,
            api_key=api_key or None,
            model=_PRINTER_MODEL or None,
        )
    elif printer_type == "bambu":
        if BambuAdapter is None:
            raise RuntimeError(
                # paho-mqtt is a core dependency, so reaching here means a
                # broken install rather than a missing extra: `kiln3d[bambu]`
                # is empty and would install nothing.
                "Bambu support requires paho-mqtt.  Install it with: pip install paho-mqtt"
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
        # Thread the configured model through, like the Creality branch
        # above and _build_adapter_from_config_entry both do — bed-aware
        # planners (e.g. split-to-fit) resolve the machine's envelope from
        # the adapter's model, so dropping it here strands the default
        # printer with no known bed.
        _adapter = BambuAdapter(
            host=host,
            access_code=api_key,
            serial=serial,
            printer_model=_PRINTER_MODEL or None,
        )
    elif printer_type == "elegoo":
        if ElegooAdapter is None:
            raise RuntimeError(
                "Elegoo SDCP support requires websocket-client.  "
                "Install it with: pip install 'kiln3d[elegoo]' or pip install websocket-client"
            )
        mainboard_id = os.environ.get("KILN_PRINTER_MAINBOARD_ID", "")
        _adapter = ElegooAdapter(host=host, mainboard_id=mainboard_id)
    elif printer_type == "prusalink":
        _adapter = PrusaLinkAdapter(host=host, api_key=api_key or None)
    elif printer_type == "usb":
        port = os.environ.get("KILN_PRINTER_PORT", "")
        if not port:
            raise RuntimeError(
                "KILN_PRINTER_PORT environment variable is not set.  "
                "Set it to the serial port path (e.g. /dev/ttyUSB0, /dev/ttyACM0, COM3)."
            )
        baudrate = parse_int_env("KILN_PRINTER_BAUDRATE", DEFAULT_SERIAL_BAUDRATE)
        _adapter = SerialPrinterAdapter(port=port, baudrate=baudrate)
    else:
        raise RuntimeError(
            f"Unsupported printer type: {printer_type!r}.  "
            f"Supported types are {format_printer_types(conjunction='and')}."
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


def _resolve_printer_model_live() -> str:
    """Return the current printer_id preferring the live config.yaml
    resolver over the frozen module global.  This lets safety gates
    always see the latest user config without requiring a server
    restart — and it gives them a fighting chance when the user never
    set _PRINTER_MODEL explicitly.  Returns empty string when no
    source has a value.
    """
    try:
        from kiln.printer_model_resolver import resolve_printer_model
        live = resolve_printer_model()
        if live:
            return live
    except Exception as exc:
        logger.debug("live printer-model resolution failed: %s", exc)
    return _PRINTER_MODEL or ""


def _get_temp_limits() -> tuple:
    """Return ``(max_tool, max_bed)`` from the printer's safety profile.

    Resolution order (via :func:`_resolve_printer_model_live`):
      1. ``printer_model`` field in ``~/.kiln/config.yaml``
      2. Bambu serial-prefix inference / host-pattern inference
      3. ``KILN_PRINTER_MODEL`` environment variable
      4. The least-capable machine in the registry (see
         ``_UNKNOWN_PRINTER_MAX_HOTEND_C``) if all of the above fail.
         This was 300/130 until 2026-07-20 — described as conservative
         while being the LOOSEST ceiling in the fleet, which let an
         unidentified (possibly PTFE-lined) hotend be driven to 300C.

    PTFE-lined hotends (non-all-metal) are additionally clamped to 240°C
    regardless of the profile's stated maximum — PTFE burns above ~245°C
    and releases toxic fumes + permanently deforms the extruder geometry.
    Set ``KILN_OVERRIDE_PTFE_LIMIT=1`` to disable this clamp (only do this
    if you've physically replaced the PTFE with an all-metal conversion).
    """
    live_model = _resolve_printer_model_live()
    if live_model:
        # Temporarily rebind _PRINTER_MODEL for the PTFE clamp branch
        # below so the existing logic reads the live value.
        _resolved = live_model
    else:
        _resolved = _PRINTER_MODEL
    if _resolved:
        try:
            from kiln.safety_profiles import get_profile  # noqa: E402

            profile = get_profile(_resolved)
            max_tool = profile.max_hotend_temp
            max_bed = profile.max_bed_temp
            # PTFE clamp — look up hotend_type from printer_intelligence.
            # ``get_printer_intel`` returns a ``PrinterIntel`` DATACLASS, not
            # a dict — the original ``(intel or {}).get(...)`` raised
            # AttributeError silently inside the bare ``except Exception``
            # below, so the clamp never fired for any PTFE-lined hotend
            # (Ender 3, Ender 5, etc.) and users were getting the profile's
            # raw 260°C ceiling instead of the PTFE-safe 240°C.  Fix:
            # use ``getattr`` against the dataclass attribute.
            if os.environ.get("KILN_OVERRIDE_PTFE_LIMIT", "").strip() not in ("1", "true", "yes"):
                try:
                    from kiln.printer_intelligence import get_printer_intel
                    intel = get_printer_intel(_resolved)
                    hotend_type = (
                        getattr(intel, "hotend_type", "") or ""
                    ).lower()
                    if hotend_type and hotend_type != "all_metal":
                        # Common values: "ptfe", "ptfe_lined", "hybrid", ""
                        _PTFE_SAFE_MAX = 240.0
                        if max_tool > _PTFE_SAFE_MAX:
                            logger.info(
                                "Clamping hotend limit from %.0f°C to %.0f°C "
                                "for %s (hotend_type=%s, non-all-metal). "
                                "Override with KILN_OVERRIDE_PTFE_LIMIT=1.",
                                max_tool, _PTFE_SAFE_MAX, _resolved, hotend_type,
                            )
                            max_tool = _PTFE_SAFE_MAX
                except Exception:
                    pass
            return max_tool, max_bed
        except (KeyError, ImportError):
            pass
    from kiln.safety_profiles import (
        _UNKNOWN_PRINTER_MAX_BED_C,
        _UNKNOWN_PRINTER_MAX_HOTEND_C,
    )
    return _UNKNOWN_PRINTER_MAX_HOTEND_C, _UNKNOWN_PRINTER_MAX_BED_C


def _is_resume_mode_3mf(file_name: str) -> bool:
    """Return True if ``file_name`` looks like a mid-print resume 3MF.

    Thin alias — the definition lives in :mod:`kiln.printers.base`, which
    the adapter layer also reads so a resumed print isn't counted as a
    second print.  See :func:`kiln.printers.base.is_resume_mode_3mf` for
    the naming convention this matches.
    """
    from kiln.printers.base import is_resume_mode_3mf

    return is_resume_mode_3mf(file_name)


def _resolve_effective_printer_name(printer_name: str | None = None) -> str:
    """Resolve the printer identifier used for emergency latch checks."""
    if printer_name:
        return printer_name
    try:
        names = _get_registry().list_names()
        if "default" in names:
            return "default"
        if names:
            return sorted(names)[0]
    except Exception:
        pass
    return "default"


def _read_config_printers() -> dict[str, dict[str, Any]]:
    """Return ``{name: entry}`` for every printer in ``~/.kiln/config.yaml``.

    Empty dict when no YAML file, no printers block, or parse failure —
    callers treat an empty result as "no config-level printers" rather
    than raising.  This makes config.yaml the single source of truth
    for which printer names exist, independent of what's currently
    loaded into the live :class:`PrinterRegistry`.
    """
    try:
        from kiln.cli.config import _read_config_file, get_config_path

        cfg = _read_config_file(get_config_path()) or {}
        printers = cfg.get("printers") or {}
        return {name: entry for name, entry in printers.items() if isinstance(entry, dict)}
    except Exception as exc:  # noqa: BLE001 — YAML is best-effort
        logger.debug("Could not read config.yaml printers: %s", exc)
        return {}


def _build_adapter_from_config_entry(name: str, entry: dict[str, Any]) -> PrinterAdapter:
    """Build a :class:`PrinterAdapter` from a single config.yaml printer entry.

    Pure factory — reads only from *entry* and a small set of optional
    environment variables for fields the YAML doesn't model (serial
    port path, mainboard id).  Does not mutate module globals.  Use
    this when you need an adapter for a *named* config entry other
    than the env-resolved default; ``_get_adapter()`` continues to
    own the env/YAML-resolved default path.

    Raises ``RuntimeError`` when the entry is missing required fields
    or requests an unsupported printer type, so the caller can emit
    a targeted warning rather than a generic "not found".
    """
    host = str(entry.get("host") or "").strip()
    api_key = str(entry.get("api_key") or entry.get("access_code") or "").strip()
    printer_type = _normalize_printer_type(
        str(entry.get("type") or entry.get("printer_type") or "").strip().lower()
    )
    serial = str(entry.get("serial") or "").strip()
    printer_model = str(entry.get("printer_model") or "").strip()

    if not host:
        raise RuntimeError(f"Config entry {name!r} is missing 'host'.")
    if not printer_type:
        raise RuntimeError(f"Config entry {name!r} is missing 'type'.")

    adapter: PrinterAdapter
    if printer_type == "octoprint":
        if not api_key:
            raise RuntimeError(f"Config entry {name!r} (OctoPrint) is missing 'api_key'.")
        adapter = OctoPrintAdapter(host=host, api_key=api_key)
    elif printer_type == "moonraker":
        adapter = MoonrakerAdapter(host=host, api_key=api_key or None)
    elif printer_type == "duet":
        adapter = DuetAdapter(host=host, **({"password": api_key} if api_key else {}))
    elif printer_type == "creality":
        adapter = CrealityAdapter(host=host, api_key=api_key or None, model=printer_model or None)
    elif printer_type == "bambu":
        if BambuAdapter is None:
            raise RuntimeError(
                f"Config entry {name!r} is type 'bambu' but paho-mqtt is not installed."
            )
        if not api_key:
            raise RuntimeError(f"Config entry {name!r} (Bambu) is missing 'access_code'.")
        if not serial:
            raise RuntimeError(f"Config entry {name!r} (Bambu) is missing 'serial'.")
        adapter = BambuAdapter(
            host=host, access_code=api_key, serial=serial,
            printer_model=printer_model or None,
        )
    elif printer_type == "elegoo":
        if ElegooAdapter is None:
            raise RuntimeError(
                f"Config entry {name!r} is type 'elegoo' but websocket-client is not installed."
            )
        mainboard_id = str(entry.get("mainboard_id") or os.environ.get("KILN_PRINTER_MAINBOARD_ID", ""))
        adapter = ElegooAdapter(host=host, mainboard_id=mainboard_id)
    elif printer_type == "prusalink":
        adapter = PrusaLinkAdapter(host=host, api_key=api_key or None)
    elif printer_type == "usb":
        # register_printer() persists a serial printer's port path as
        # `host`, so accept that too rather than demanding a `port` key
        # the tool that wrote the entry never emits.
        port = str(entry.get("port") or host or os.environ.get("KILN_PRINTER_PORT", ""))
        if not port:
            raise RuntimeError(f"Config entry {name!r} (serial) is missing 'port'.")
        baudrate = int(entry.get("baudrate") or parse_int_env("KILN_PRINTER_BAUDRATE", DEFAULT_SERIAL_BAUDRATE))
        adapter = SerialPrinterAdapter(port=port, baudrate=baudrate)
    else:
        raise RuntimeError(
            f"Config entry {name!r} has unsupported printer type {printer_type!r}.  "
            f"Supported types are {format_printer_types(conjunction='and')}."
        )

    if printer_model:
        adapter.set_safety_profile(printer_model)

    return adapter


def _resolve_adapter(printer_name: str | None = None) -> PrinterAdapter:
    """Resolve a :class:`PrinterAdapter` from an optional printer name.

    config.yaml is the source of truth for which printer names exist;
    the :class:`PrinterRegistry` is a cache of *live* adapter instances
    in front of it.  When the registry misses — because startup
    auto-register silently failed, or because the server has been
    running long enough that an adapter got evicted — we fall back
    to config.yaml and lazily build + register the adapter, so the
    next call hits the fast path.

    Behaviour:
      * ``printer_name=None``: return ``_get_adapter()`` — the env/YAML
        resolved default adapter, same path used by tools that don't
        expose a printer_name arg.
      * Registry hit: return the live adapter.
      * Registry miss, *name* is the effective default:
        use ``_get_adapter()`` and self-heal the registry entry.
      * Registry miss, *name* is in config.yaml: build the adapter
        from the config entry, register it, return.
      * Registry miss, *name* not in config.yaml: raise
        :class:`PrinterNotFoundError` — we never silently redirect
        an unknown name to the default adapter.
    """
    from kiln.registry import PrinterNotFoundError

    if not printer_name:
        return _get_adapter()

    registry = _get_registry()
    try:
        return registry.get(printer_name)
    except PrinterNotFoundError:
        pass

    # Registry miss — consult config.yaml.
    config_printers = _read_config_printers()
    effective_default = _resolve_effective_printer_name(None)

    if printer_name == effective_default:
        adapter = _get_adapter()
    elif printer_name in config_printers:
        try:
            adapter = _build_adapter_from_config_entry(
                printer_name, config_printers[printer_name],
            )
        except Exception as exc:
            logger.warning(
                "Failed to build adapter for config-listed printer %r: %s",
                printer_name,
                _sanitize_log_msg(str(exc)),
            )
            raise PrinterNotFoundError(printer_name) from exc
    else:
        raise PrinterNotFoundError(printer_name)

    with contextlib.suppress(Exception):  # best-effort self-heal
        registry.register(printer_name, adapter)
    return adapter


def _get_emergency_latch_status(printer_name: str) -> dict[str, Any] | None:
    """Best-effort emergency latch status lookup for a printer."""
    try:
        from kiln.emergency import get_emergency_coordinator

        return get_emergency_coordinator().get_latch_status(printer_name)
    except Exception as exc:
        logger.debug("Emergency latch lookup failed for %s: %s", printer_name, exc)
        return None


def _emergency_latch_error(tool_name: str, printer_name: str) -> dict | None:
    """Return E_STOP_LATCHED error when a printer is emergency-latched."""
    status = _get_emergency_latch_status(printer_name)
    if not status or not bool(status.get("latched")):
        return None

    blockers = status.get("critical_interlocks_pending") or []
    msg = f"Emergency latch is active for printer '{printer_name}'."
    if blockers:
        msg += " Critical interlocks pending: " + ", ".join(str(x) for x in blockers) + "."
    msg += " Resolve hazards, acknowledge, then clear via clear_emergency_stop()."

    _audit(
        tool_name,
        "blocked_emergency_latch",
        details={
            "printer_name": printer_name,
            "critical_interlocks_pending": blockers,
        },
    )
    data = _error_dict(msg, code="E_STOP_LATCHED", retryable=False)
    data["emergency_status"] = status
    return data


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
    "emergency_trip_input": (1000, 20),
    "cancel_print": (5000, 3),
    "start_print": (5000, 3),
    "upload_file": (2000, 10),
    "pause_print": (5000, 6),
    "resume_print": (5000, 6),
    "calibrate_direct": (10000, 2),
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
        _get_event_bus().publish(
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


# ---------------------------------------------------------------------------
# Pause keep-alive — fights firmware idle-cooldown during long pauses
# ---------------------------------------------------------------------------
# Bambu A1 firmware (and several other FDM firmwares) drops the hotend
# heater after a few minutes of pause regardless of the slicer's targets.
# When the agent plans a mid-print decoration swap that takes 5+ minutes
# (sign-off, planning, slicing, upload), the printer is stone-cold by the
# time we resume.  This re-asserts the captured pre-pause targets every
# ``_PAUSE_KEEPALIVE_INTERVAL_S`` seconds via the existing adapter API.
#
# Design:
#   - One daemon thread per process.  Idempotent ``start`` so repeat
#     pauses don't compound threads.
#   - ``stop()`` is called from resume/cancel/error paths.
#   - All adapter calls are best-effort; a failed re-assert is logged
#     once at INFO level and the loop keeps trying.
_PAUSE_KEEPALIVE_INTERVAL_S: float = 120.0  # 2 min — well under firmware idle threshold


class _PauseKeepAlive:
    """Daemon thread that re-asserts heater targets across long pauses.

    Singleton-ish: one instance lives in the module.  ``start`` is
    idempotent; calling it twice while the thread is running just
    refreshes the stored targets.  ``stop`` is fast and side-effect
    free if the thread isn't running.
    """

    def __init__(self) -> None:
        import threading
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._targets: dict[str, float] = {}

    def start(self, tool_target: float, bed_target: float) -> bool:
        """Begin re-asserting ``tool_target`` and ``bed_target`` every 2 min.

        If the thread is already running, just update the stored targets.
        Returns ``True`` if a new thread was spawned, ``False`` if an
        existing thread was just refreshed.
        """
        import threading
        with self._lock:
            self._targets = {
                "tool": float(tool_target or 0.0),
                "bed": float(bed_target or 0.0),
            }
            if self._thread is not None and self._thread.is_alive():
                return False  # Refreshed in place, no new thread.
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="kiln-pause-keepalive",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self) -> None:
        """Signal the thread to exit.  Safe to call when not running."""
        with self._lock:
            self._stop_event.set()
            self._thread = None
            self._targets = {}

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        """Re-assert targets until stopped or the printer leaves PAUSED."""
        # Wait first — the immediate post-pause state already has the
        # targets set by the slicer/firmware; we only need to fight the
        # cooldown that kicks in a few minutes later.
        while not self._stop_event.wait(_PAUSE_KEEPALIVE_INTERVAL_S):
            try:
                from kiln.printers.base import PrinterStatus
                adapter = _get_adapter()
                # Stop if the printer left PAUSED on its own (resume,
                # cancel, error, or operator pressed buttons on the printer).
                try:
                    state = adapter.get_state()
                    if state.state != PrinterStatus.PAUSED:
                        logger.debug(
                            "Pause keep-alive: printer state is %s, not paused — exiting loop",
                            state.state,
                        )
                        return
                except Exception as exc:
                    logger.debug("Pause keep-alive: state read failed (%s); continuing", exc)

                with self._lock:
                    targets = dict(self._targets)
                if targets.get("tool", 0) > 0:
                    try:
                        adapter.set_tool_temp(targets["tool"])
                    except Exception as exc:
                        logger.info(
                            "Pause keep-alive: set_tool_temp(%s) failed: %s",
                            targets["tool"], exc,
                        )
                if targets.get("bed", 0) > 0:
                    try:
                        adapter.set_bed_temp(targets["bed"])
                    except Exception as exc:
                        logger.info(
                            "Pause keep-alive: set_bed_temp(%s) failed: %s",
                            targets["bed"], exc,
                        )
            except Exception as exc:  # noqa: BLE001 — never let a daemon die silently
                logger.warning("Pause keep-alive loop error (continuing): %s", exc)


_pause_keepalive = _PauseKeepAlive()


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

_registry: PrinterRegistry | None = None
_queue: PrintQueue | None = None
_event_bus: EventBus | None = None
_scheduler: JobScheduler | None = None
_webhook_mgr: WebhookManager | None = None
_auth: AuthManager | None = None
_billing: BillingLedger | None = None
_payment_mgr = None  # PaymentManager, lazily initialized
_billing_alert_mgr = None  # BillingAlertManager, lazily initialized
_cost_estimator: CostEstimator | None = None
_material_tracker: MaterialTracker | None = None
_bed_level_mgr: BedLevelManager | None = None
_stream_proxy: MJPEGProxy | None = None
_cloud_sync: CloudSyncManager | None = None


# Materials that readily absorb moisture from the air.  A loaded filament whose
# type matches any of these tokens (case-insensitive substring) is treated as
# physically plausible for a wet-filament failure or pre-print drying nudge;
# anything else is non-hygroscopic and a wet-filament finding requires stronger
# symptom evidence.  Shared by preflight_check and analyze_print_failure.
_HYGROSCOPIC_MATERIAL_HINTS: tuple[str, ...] = (
    "nylon", "pa6", "pa11", "pa12", "paht", "pa-",
    "pva", "tpu", "tpe", "pc", "polycarbonate",
    "pet-cf", "petg-cf", "-cf", "-gf", "carbon", "glass",
    "pps", "ppa", "peek", "pekk", "pvb", "bvoh",
)

# Wet-filament keyword bar: a non-hygroscopic material needs at least this many
# distinct symptom keywords (popping/stringing/oozing/etc.) in the failure text
# before wet filament is added to the possible-causes list.  Hygroscopic
# materials trip the flag on a single symptom because moisture is physically
# plausible there; an explicit moisture mention ("wet", "humid", "damp",
# "moisture") always trips the flag regardless.
_WET_MIN_HITS: int = 2


def _get_cloud_sync() -> CloudSyncManager | None:
    """Return the current cloud sync manager (may be None if not configured)."""
    return _cloud_sync


def _set_cloud_sync(manager: CloudSyncManager | None) -> None:
    """Replace the cloud sync manager singleton."""
    global _cloud_sync  # noqa: PLW0603
    if _cloud_sync is not None:
        _cloud_sync.stop()
    _cloud_sync = manager


_heater_watchdog: HeaterWatchdog | None = None
_plugin_mgr: PluginManager | None = None
_start_time = time.time()

# Track whether event bus subscriptions have been wired (once-only guard).
_event_subs_wired: bool = False

# Layer 5: per-printer PrintWatchdog registry.  Keyed by printer_name so a
# fleet deployment can have one watchdog per printer.  Daemon threads —
# destroyed when the server process exits.

_print_watchdogs: dict[str, Any] = {}
_print_watchdogs_lock = threading.Lock()


def _spawn_print_watchdog(adapter: Any, file_name: str) -> None:
    """Spawn a PrintWatchdog for the printer that just started a print.

    If a watchdog already exists for this printer, stop the old one
    and replace it.  On any anomaly, logs the crash envelope to
    ``~/.kiln/incidents/`` via the incident_recorder.
    """
    from kiln.print_watchdog import PrintWatchdog

    printer_name = _resolve_effective_printer_name() or "default"

    def _on_anomaly(flag):
        logger.error(
            "PrintWatchdog anomaly on %s: %s", printer_name, flag,
        )
        # Auto-capture incident envelope (Layer 6) — local only, no upload.
        try:
            from kiln import incident_recorder
            incident_recorder.record_incident(
                incident_type="watchdog_red_flag",
                printer_status={"printer_name": printer_name, "file": file_name},
                user_description=str(flag),
                tags=["watchdog", "auto"],
            )
        except Exception as _rec_exc:
            logger.debug(
                "incident_recorder auto-capture failed: %s", _rec_exc,
            )

    with _print_watchdogs_lock:
        # Replace any prior watchdog for this printer.
        old = _print_watchdogs.get(printer_name)
        if old is not None:
            with contextlib.suppress(Exception):
                old.stop(timeout=1.0)
        wd = PrintWatchdog(
            adapter=adapter,
            poll_interval_sec=2.5,
            on_anomaly=_on_anomaly,
        )
        wd.start()
        _print_watchdogs[printer_name] = wd


def _stop_print_watchdog(printer_name: str | None = None) -> None:
    """Stop the PrintWatchdog for a printer (e.g., on cancel / finish)."""
    with _print_watchdogs_lock:
        name = printer_name or _resolve_effective_printer_name() or "default"
        wd = _print_watchdogs.pop(name, None)
    if wd is not None:
        try:
            wd.stop(timeout=1.5)
        except Exception as exc:
            logger.debug("PrintWatchdog stop failed: %s", exc)


def _get_registry() -> PrinterRegistry:
    """Return the lazily-initialised printer registry.

    Converges with :func:`kiln.registry.get_printer_registry` so that
    callers outside ``kiln.server`` (print_health_monitor, heartbeat,
    auto_recover_engine, etc.) see the same instance the server has
    populated with adapters.
    """
    global _registry  # noqa: PLW0603
    if _registry is None:
        _registry = PrinterRegistry()
        # Publish to the canonical singleton so non-server callers
        # (kiln.registry.get_printer_registry) see the populated
        # registry, not an empty one.
        try:
            from kiln.registry import register_default_singleton

            register_default_singleton(_registry)
        except ImportError:
            # Defensive — registry module changes shouldn't break server boot.
            pass
    return _registry


#: Said when the queue cannot be answered for from here.  A refusal that
#: does not name where the thing DOES work reads as Kiln being broken.
_QUEUE_HOSTED_REFUSAL = (
    "Your print queue is not available on the hosted Kiln API: it lives on "
    "the machine attached to your printer, and this server keeps no "
    "per-account queue. Run this from your local Kiln install or the CLI, "
    "or connect that machine through the Kiln bridge and your queue "
    "follows."
)


def _get_queue() -> PrintQueue:
    """Return the lazily-initialised job queue.

    Refuses on the hosted multi-tenant deploy.  ``~/.kiln/queue.db`` has
    schema ``jobs(id, file_name, printer_name, status, submitted_by, ...)``
    with no tenant column, and the hosted server runs ONE ``~/.kiln`` for
    every customer — so a job submitted there is listed back to every other
    tenant (a file name carries client names and part numbers) and can be
    cancelled by any of them.  Measured, not theorised.

    The guard sits HERE rather than on a list of tool names because this is
    the one resolver every reader passes through.  A name list was the
    first attempt and it was already incomplete: ``await_print_completion``
    and ``analyze_print_failure`` both read a job by id and return the full
    record, and three MCP resources read the queue too.  The name list is
    kept as a fast path in kiln-pro's dispatcher — it answers before any
    work happens and can word the refusal per tool — but the boundary is
    this function, so a door added next year inherits it.

    Inert on a local install: the operator IS the caller there, nothing
    arms the hosted flag, and the queue works exactly as before.  The
    hosted app never constructs the queue or the scheduler at boot (only
    ``main()`` does, and that is the local stdio server), so this can only
    fire on a real hosted tool call.
    """
    global _queue  # noqa: PLW0603
    from kiln.errors import HostedUnavailableError
    from kiln.runtime_env import is_hosted_multitenant

    if is_hosted_multitenant():
        raise HostedUnavailableError(_QUEUE_HOSTED_REFUSAL)
    if _queue is None:
        _queue = PrintQueue(db_path=os.path.join(str(Path.home()), ".kiln", "queue.db"))
    return _queue


def _get_event_bus() -> EventBus:
    """Return the lazily-initialised event bus, wiring subscriptions on first access."""
    global _event_bus, _event_subs_wired  # noqa: PLW0603
    if _event_bus is None:
        _event_bus = EventBus()
    if not _event_subs_wired:
        _event_subs_wired = True
        # Heater watchdog lifecycle subscriptions (use getters to avoid circular init).
        _event_bus.subscribe(EventType.PRINT_STARTED, lambda _e: _get_heater_watchdog().notify_print_started())
        _event_bus.subscribe(EventType.PRINT_COMPLETED, lambda _e: _get_heater_watchdog().notify_print_ended())
        _event_bus.subscribe(EventType.PRINT_FAILED, lambda _e: _get_heater_watchdog().notify_print_ended())
        _event_bus.subscribe(EventType.PRINT_CANCELLED, lambda _e: _get_heater_watchdog().notify_print_ended())
        # Layer 5: tear down the PrintWatchdog when a print ends, by any path.
        _event_bus.subscribe(EventType.PRINT_COMPLETED, lambda _e: _stop_print_watchdog())
        _event_bus.subscribe(EventType.PRINT_FAILED, lambda _e: _stop_print_watchdog())
        _event_bus.subscribe(EventType.PRINT_CANCELLED, lambda _e: _stop_print_watchdog())
        # Persistence and billing subscribers (previously wired at import time).
        _event_bus.subscribe(None, _persist_event)
        _event_bus.subscribe(EventType.JOB_COMPLETED, _billing_hook)
        _event_bus.subscribe(EventType.JOB_COMPLETED, _log_print_completion)
        _event_bus.subscribe(EventType.JOB_FAILED, _log_print_completion)
    return _event_bus


def _get_scheduler() -> JobScheduler:
    """Return the lazily-initialised job scheduler."""
    global _scheduler  # noqa: PLW0603
    if _scheduler is None:
        _scheduler = JobScheduler(
            _get_queue(),
            _get_registry(),
            _get_event_bus(),
            persistence=get_db(),
        )
    return _scheduler


def _get_webhook_mgr() -> WebhookManager:
    """Return the lazily-initialised webhook manager."""
    global _webhook_mgr  # noqa: PLW0603
    if _webhook_mgr is None:
        _webhook_mgr = WebhookManager(_get_event_bus())
    return _webhook_mgr


def _get_auth() -> AuthManager:
    """Return the lazily-initialised auth manager."""
    global _auth  # noqa: PLW0603
    if _auth is None:
        _auth = AuthManager()
    return _auth


def _get_billing() -> BillingLedger | None:
    """Return the lazily-initialised billing ledger (None if unavailable)."""
    global _billing  # noqa: PLW0603
    if _billing is None and BillingLedger is not None:
        _billing = BillingLedger(db=get_db())
    return _billing


def _get_cost_estimator() -> CostEstimator:
    """Return the lazily-initialised cost estimator."""
    global _cost_estimator  # noqa: PLW0603
    if _cost_estimator is None:
        _cost_estimator = CostEstimator()
    return _cost_estimator


def _get_material_tracker() -> MaterialTracker:
    """Return the lazily-initialised material tracker."""
    global _material_tracker  # noqa: PLW0603
    if _material_tracker is None:
        _material_tracker = MaterialTracker(db=get_db(), event_bus=_get_event_bus())
    return _material_tracker


def _get_bed_level_mgr() -> BedLevelManager:
    """Return the lazily-initialised bed level manager."""
    global _bed_level_mgr  # noqa: PLW0603
    if _bed_level_mgr is None:
        _bed_level_mgr = BedLevelManager(
            db=get_db(),
            event_bus=_get_event_bus(),
            registry=_get_registry(),
        )
    return _bed_level_mgr


def _get_stream_proxy() -> MJPEGProxy:
    """Return the lazily-initialised MJPEG stream proxy."""
    global _stream_proxy  # noqa: PLW0603
    if _stream_proxy is None:
        _stream_proxy = MJPEGProxy()
    return _stream_proxy


def _get_heater_watchdog() -> HeaterWatchdog:
    """Return the lazily-initialised heater watchdog."""
    global _heater_watchdog  # noqa: PLW0603
    if _heater_watchdog is None:
        _heater_watchdog = HeaterWatchdog(
            get_adapter=lambda: _get_adapter(),
            timeout_minutes=_HEATER_TIMEOUT_MIN,
            event_bus=_get_event_bus(),
        )
    return _heater_watchdog


def _get_plugin_mgr() -> PluginManager:
    """Return the lazily-initialised plugin manager."""
    global _plugin_mgr  # noqa: PLW0603
    if _plugin_mgr is None:
        _plugin_mgr = PluginManager()
    return _plugin_mgr


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
    # MakerWorld is always available (no credentials required — metadata-only)
    try:
        _marketplace_registry.register(MakerWorldAdapter())
    except Exception:
        logger.debug("Could not register MakerWorld adapter", exc_info=True)


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
            "Set KILN_FULFILLMENT_PROVIDER and its required credentials."
        ) from exc
    return _fulfillment


_fulfillment_monitor: Any | None = None


def _get_fulfillment_monitor() -> Any | None:
    """Return the lazily-initialised fulfillment monitor.

    Starts the background polling thread on first access.
    Returns None if the fulfillment_monitor module is not installed.
    """
    global _fulfillment_monitor  # noqa: PLW0603

    if _fulfillment_monitor is not None:
        return _fulfillment_monitor

    try:
        from kiln.fulfillment_monitor import FulfillmentMonitor
    except ImportError:
        return None

    _fulfillment_monitor = FulfillmentMonitor(
        db=get_db(),
        event_bus=_get_event_bus(),
    )
    _fulfillment_monitor.start()
    return _fulfillment_monitor


def _validate_quote_for_order(
    quote_id: str,
    *,
    provider_name: str | None = None,
) -> Any:
    """Validate a cached fulfillment quote before order placement."""
    try:
        from kiln.fulfillment.intelligence import validate_quote_for_order
    except ImportError:
        from kiln_pro.fulfillment.intelligence import validate_quote_for_order

    return validate_quote_for_order(
        quote_id,
        provider_name=provider_name,
    )


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


_PROVIDER_TERMS_URLS: dict[str, str] = {
    "craftcloud": "https://craftcloud3d.com/terms-and-conditions",
    "3dos": "https://www.3dos.io/terms",
}


def _provider_routing_metadata(
    provider_name: str,
    *,
    provider_order_id: str = "",
) -> dict[str, str]:
    """Return normalized routing metadata for provider-managed orders.

    Kiln is orchestration infrastructure. For provider-routed jobs, the
    provider remains merchant of record and support owner.
    """
    normalized = (provider_name or "").strip().lower()
    return {
        "provider_name": provider_name,
        "provider_order_id": provider_order_id,
        "provider_terms_url": _PROVIDER_TERMS_URLS.get(normalized, ""),
        "support_owner": "provider",
        "merchant_of_record": "provider",
    }


def _get_payment_mgr():
    """Return the lazily-initialised payment manager."""
    global _payment_mgr  # noqa: PLW0603

    if PaymentManager is None:
        return None

    if _payment_mgr is not None:
        return _payment_mgr

    from kiln.cli.config import get_billing_config

    config = get_billing_config()
    _payment_mgr = PaymentManager(
        db=get_db(),
        config=config,
        event_bus=_get_event_bus(),
        ledger=_get_billing(),
    )

    # Auto-register providers from env vars.
    stripe_key = os.environ.get("KILN_STRIPE_SECRET_KEY", "")
    if stripe_key:
        try:
            from kiln_pro.payments.stripe_provider import StripeProvider

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
            from kiln_pro.payments.circle_provider import CircleProvider

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


def _get_billing_alert_mgr():
    """Return the lazily-initialised billing alert manager."""
    global _billing_alert_mgr  # noqa: PLW0603

    if BillingAlertManager is None:
        return None

    if _billing_alert_mgr is None:
        _billing_alert_mgr = BillingAlertManager(event_bus=_get_event_bus())
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
                _get_event_bus().publish(
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
                _get_event_bus().publish(
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
    "KILN_GEMINI_API_KEY",
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
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a standardised error response dict.

    If *retryable* is not supplied explicitly it is inferred from *code*:
    codes in ``_RETRYABLE_CODES`` are assumed retryable, everything else
    (auth, validation, not-found, unsupported) is not.

    *extra* merges top-level keys alongside ``error`` — for refusals that
    carry an agent-addressed field next to the human-readable ``message``
    (``**signin_hint_fields()``).  It cannot reach inside ``error``, so the
    envelope every existing caller depends on keeps its exact shape.
    """
    if retryable is None:
        retryable = code in _RETRYABLE_CODES
    payload: dict[str, Any] = {
        "success": False,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
        },
    }
    if extra:
        payload.update(extra)
    return payload


def _flush_restart_stdio() -> int:
    """Flush outbound MCP framing and drain queued inbound frames before exec."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(OSError, AttributeError, ValueError):
            stream.flush()

    return _drain_restart_stdin()


def _drain_restart_stdin(*, max_bytes: int = 1_048_576) -> int:
    """Drain currently queued stdin bytes without blocking."""
    try:
        fd = sys.stdin.fileno()
    except (OSError, AttributeError, ValueError):
        return 0

    try:
        was_blocking = os.get_blocking(fd)
    except (AttributeError, OSError):
        was_blocking = None

    try:
        os.set_blocking(fd, False)
    except (AttributeError, OSError):
        return 0

    drained = 0
    try:
        while drained < max_bytes:
            try:
                chunk = os.read(fd, min(65_536, max_bytes - drained))
            except BlockingIOError:
                break
            except InterruptedError:
                continue
            except OSError:
                break
            if not chunk:
                break
            drained += len(chunk)
    finally:
        if was_blocking is not None:
            with contextlib.suppress(OSError):
                os.set_blocking(fd, was_blocking)

    return drained


def _extract_bearer_or_raw_token(value: Any) -> str:
    """Extract a token from either ``Bearer <token>`` or raw token input."""
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return raw


def _token_from_mcp_context() -> str:
    """Best-effort token extraction from the current MCP request context."""
    ctx = _current_mcp_request_context.get()
    if ctx is None:
        return ""

    request_context = getattr(ctx, "request_context", None)
    if request_context is None:
        return ""

    # Prefer explicit request metadata tokens when present.
    meta = getattr(request_context, "meta", None)
    meta_data: dict[str, Any] = {}
    if meta is not None:
        if isinstance(meta, dict):
            meta_data = meta
        elif hasattr(meta, "model_dump"):
            try:
                meta_data = dict(meta.model_dump(exclude_none=True))
            except Exception:
                meta_data = {}
        else:
            meta_data = dict(getattr(meta, "__dict__", {}))

    for key in ("authorization", "Authorization", "auth_token", "authToken", "api_key", "apiKey", "token"):
        token = _extract_bearer_or_raw_token(meta_data.get(key))
        if token:
            return token

    # Fall back to request headers (transport-dependent).
    request = getattr(request_context, "request", None)
    headers = getattr(request, "headers", None)
    if headers is not None:
        try:
            header_token = _extract_bearer_or_raw_token(headers.get("authorization") or headers.get("Authorization"))
            if header_token:
                return header_token
        except Exception:
            pass

    return ""


def _resolve_auth_token() -> str:
    """Resolve auth token from request context, then environment fallback."""
    context_token = _token_from_mcp_context()
    if context_token:
        return context_token

    # Compatibility fallback for deployments that inject auth via env.
    for env_name in ("KILN_MCP_AUTH_TOKEN", "KILN_API_AUTH_TOKEN", "KILN_AUTH_TOKEN"):
        token = _extract_bearer_or_raw_token(os.environ.get(env_name, ""))
        if token:
            return token
    return ""


def _check_auth(scope: str) -> dict[str, Any] | None:
    """Check authentication for a tool invocation.

    Returns ``None`` if the request is allowed (either auth is disabled or the
    token is valid with the required *scope*).  Returns an error dict suitable
    for direct return from a tool handler when the request must be rejected.

    This is intentionally a no-op when authentication is not configured so
    that existing deployments continue to work without changes.
    """
    if not _get_auth().enabled:
        return None

    token = _resolve_auth_token()
    result = _get_auth().check_request(key=token, scope=scope)
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
    if not _get_auth().enabled:
        return _error_dict(
            "Authentication required for paid operations. "
            "Enable auth with KILN_AUTH_ENABLED=1 and set "
            "KILN_AUTH_KEY=<your-key> before using fulfillment services. "
            "See: kiln auth setup",
            code="AUTH_REQUIRED",
        )
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
            job = _get_queue().get_job(event.data["job_id"])
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
        fee_calc, _charge_id = _get_billing().calculate_and_record_fee(
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
        job = _get_queue().get_job(event.data["job_id"])
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


# NOTE: Event subscribers (_persist_event, _billing_hook, _log_print_completion)
# and heater watchdog subscriptions are wired lazily inside _get_event_bus()
# on first access, so importing this module no longer triggers DB connections
# or network subscriptions.


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def printer_status() -> dict:
    """Get full printer state, temperatures, job progress, and capabilities (detailed).

    Returns a comprehensive JSON object with:
    - ``printer``: connection status, operational state, tool/bed temperatures
    - ``job``: current file name, completion percentage, elapsed and remaining time
    - ``capabilities``: what this printer backend supports

    Use this as the first call to understand what the printer is doing.
    For lightweight polling during prints, use ``print_status_lite`` instead
    (fewer tokens, accepts printer name).
    """
    try:
        adapter = _get_adapter()
        state = adapter.get_state()
        job = adapter.get_job()
        caps = adapter.capabilities

        response = {
            "success": True,
            "printer": state.to_dict(),
            "job": job.to_dict(),
            "capabilities": caps.to_dict(),
        }
        from kiln.safety_gap_warning import attach_safety_warning
        return attach_safety_warning(response)
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(
            f"Failed to get printer status: {exc}. Check that the printer is online and KILN_PRINTER_HOST is correct."
        )
    except Exception as exc:
        logger.exception("Unexpected error in printer_status")
        return _error_dict(f"Unexpected error in printer_status: {exc}", code="INTERNAL_ERROR")


# -- Material cost estimation --------------------------------------------------
# Mirrors _MATERIAL_DB from kiln.generation.validation but kept local to avoid
# importing the heavy mesh-analysis module at server startup.
_MATERIAL_COST_PER_KG: dict[str, float] = {
    "pla": 20.0,
    "pla+": 22.0,
    "petg": 22.0,
    "abs": 18.0,
    "tpu": 30.0,
    "asa": 25.0,
    "nylon": 35.0,
    "pc": 40.0,
    "carbon_fiber_pla": 45.0,
}

# Average FDM filament consumption: ~7.5 g/hour is a reasonable mid-range
# estimate across typical desktop prints (5-10 g/hr range).
_AVG_FILAMENT_G_PER_HOUR: float = 7.5

# Average electricity cost for an FDM printer: ~$0.05-0.15/kWh in the US.
# Typical desktop FDM printers draw ~100-200W (Bambu A1 ~120W average).
_PRINTER_WATTS: float = 120.0
_ELECTRICITY_COST_PER_KWH: float = 0.12  # US average

# Bambu AMS purge waste per tool change: ~0.3-0.7g, use 0.5g average.
_AMS_PURGE_WASTE_G: float = 0.5


def _estimate_print_cost(
    elapsed_s: int | float | None,
    remaining_s: int | float | None,
    *,
    material: str | None = None,
    filament_weight_g: float | None = None,
    tool_changes: int = 0,
) -> dict[str, Any] | None:
    """Estimate print cost from filament weight and electricity.

    Prefers *filament_weight_g* (from gcode metadata) when available.
    Falls back to the time-based heuristic (g/hr) when no metadata exists.

    Includes electricity cost based on printer wattage and total time.
    For multicolor prints, adds AMS purge waste per tool change.

    Returns a dict with cost breakdown, or ``None`` if estimation is not
    possible (e.g. both time values and weight are missing).
    """
    elapsed = elapsed_s if elapsed_s is not None and elapsed_s >= 0 else 0
    remaining = remaining_s if remaining_s is not None and remaining_s >= 0 else 0
    total_s = elapsed + remaining
    if total_s <= 0 and filament_weight_g is None:
        return None

    mat_key = (material or "pla").lower().strip()
    cost_per_kg = _MATERIAL_COST_PER_KG.get(mat_key, _MATERIAL_COST_PER_KG["pla"])
    mat_label = mat_key.upper()

    # --- Filament weight ---
    if filament_weight_g is not None and filament_weight_g > 0:
        estimated_weight_g = filament_weight_g
        weight_source = "gcode"
    else:
        # Fallback to time-based heuristic
        total_hours = total_s / 3600.0
        estimated_weight_g = total_hours * _AVG_FILAMENT_G_PER_HOUR
        weight_source = "time_estimate"

    # Add AMS purge waste for multicolor
    purge_waste_g = tool_changes * _AMS_PURGE_WASTE_G
    total_weight_g = estimated_weight_g + purge_waste_g

    # --- Material cost ---
    material_cost = (total_weight_g / 1000.0) * cost_per_kg

    # --- Electricity cost ---
    total_hours = total_s / 3600.0 if total_s > 0 else 0
    electricity_kwh = (_PRINTER_WATTS / 1000.0) * total_hours
    electricity_cost = electricity_kwh * _ELECTRICITY_COST_PER_KWH

    total_cost = material_cost + electricity_cost

    return {
        "material": mat_label,
        "estimated_weight_g": round(total_weight_g, 1),
        "print_weight_g": round(estimated_weight_g, 1),
        "purge_waste_g": round(purge_waste_g, 1),
        "material_cost_usd": round(material_cost, 2),
        "electricity_cost_usd": round(electricity_cost, 2),
        "total_cost_usd": round(total_cost, 2),
        "cost_per_kg_usd": cost_per_kg,
        "total_print_time_hours": round(total_hours, 2),
        "tool_changes": tool_changes,
        "weight_source": weight_source,
    }


def _format_duration(seconds: int | float | None) -> str:
    """Format seconds as a human-readable duration string."""
    if seconds is None or seconds < 0:
        return "N/A"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"~{hours}h {minutes}min"
    if minutes > 0:
        return f"~{minutes} min"
    return f"~{secs}s"


def _generate_print_comment(
    state_str: str,
    *,
    completion: float | None,
    tool_actual: float | None,
    tool_target: float | None,
    bed_actual: float | None,
    bed_target: float | None,
    print_error: int | None,
) -> str:
    """Generate an auto-observation comment about print health."""
    if print_error and print_error > 0:
        return f"Error detected (code {print_error}). Check printer."
    if state_str == "paused":
        return "Print is paused."
    if state_str not in ("printing", "preparing"):
        return f"Printer state: {state_str}."

    comments: list[str] = []

    # Temperature deviations
    if tool_actual is not None and tool_target is not None and tool_target > 0:
        diff = abs(tool_actual - tool_target)
        if diff > 10:
            if tool_actual < tool_target:
                comments.append("Nozzle still heating up.")
            else:
                comments.append("Nozzle temperature deviation detected.")
    if bed_actual is not None and bed_target is not None and bed_target > 0:
        diff = abs(bed_actual - bed_target)
        if diff > 10:
            if bed_actual < bed_target:
                comments.append("Bed still heating up.")
            else:
                comments.append("Bed temperature deviation detected.")

    if completion is not None and completion >= 90:
        comments.append("Almost done!")
    elif completion is not None and completion < 5 and state_str == "printing":
        comments.append("Print just started.")

    if not comments:
        comments.append("Print progressing normally.")

    return " ".join(comments)


def _resolve_brief_context(brief_id: str) -> dict | None:
    """Load the saved-goal context for *brief_id* via kiln-pro, or None.

    Used by ``monitor_print`` and ``await_print_completion`` to surface a
    "Goal: ..." line / dict alongside their primary print reporting.
    Best-effort: kiln-pro absent silently returns None.  Same broad
    except as the audit honor-gate hook in ``original_design`` — a
    print-reporting tool must never fail because of an optional
    goal-context lookup.
    """
    if not brief_id:
        return None
    try:
        from kiln_pro.design_brief.context import brief_context_dict
        return brief_context_dict(brief_id)
    except Exception:
        logger.debug(
            "_resolve_brief_context: skipped (best-effort)",
            exc_info=True,
        )
        return None


def _auto_derive_brief_id(printer_file_name: str | None) -> str:
    """D3: derive brief_id from the printed file's intent sidecar.

    Pipeline:
      printer file_name → upload manifest → source path → intent sidecar
      → ``generator`` starts with ``design_brief:`` → return the id.

    Returns ``""`` (not None) when ANY step fails, matching the rest of
    the brief_id surface (the empty-string-means-absent convention used
    by every ``brief_id: str = ""`` param).  Best-effort throughout.

    Why this exists: B10 design call — the user shouldn't have
    to remember which goal a print belongs to when they call
    ``monitor_print()``.  The upload manifest (populated by
    ``upload_file``) provides the source-path link; the source's intent
    sidecar provides the brief_id.
    """
    if not printer_file_name or printer_file_name == "N/A":
        return ""
    try:
        from kiln.upload_manifest import resolve_source_path
        source_path = resolve_source_path(printer_file_name)
    except Exception:
        logger.debug(
            "_auto_derive_brief_id: manifest lookup skipped (best-effort)",
            exc_info=True,
        )
        return ""
    if not source_path:
        return ""
    try:
        from kiln_pro.intent_verification import load_intent_sidecar
        intent = load_intent_sidecar(source_path)
        if (
            intent is not None
            and isinstance(intent.generator, str)
            and intent.generator.startswith("design_brief:")
        ):
            candidate = intent.generator.split(":", 1)[1].strip()
            return candidate or ""
    except Exception:
        logger.debug(
            "_auto_derive_brief_id: sidecar read skipped (best-effort)",
            exc_info=True,
        )
    return ""


def _format_goal_line_for_monitor(brief_id: str) -> str:
    """Render the saved-goal context as a single ``Goal: ...`` line.

    Empty string when there's nothing to render — caller appends only
    when non-empty.  Format mirrors the rest of ``monitor_print``'s
    plain-English report.
    """
    ctx = _resolve_brief_context(brief_id)
    if ctx is None:
        return ""
    duty = ctx.get("duty_label") or ctx.get("duty") or ""
    env = ctx.get("environment") or []
    if duty and env:
        return f"Goal: {duty} design for {', '.join(env)}"
    if duty:
        return f"Goal: {duty} design"
    return ""


@mcp.tool()
def monitor_print(
    printer_name: str | None = None,
    include_snapshot: bool = True,
    brief_id: str = "",
) -> str | dict:
    """One-shot print status report (human-readable text: progress, temps, speed, cost, ETA).

    Use for quick status checks. Returns a fixed-format text report with
    progress, temps, speed, errors, cost estimate, camera snapshot, and
    health commentary. For structured data + AI vision inspection, use
    ``monitor_print_vision``. For persistent background monitoring, use
    ``watch_print``.

    :param printer_name: Target printer name.  Omit for the default printer.
    :param include_snapshot: Whether to capture and save a camera snapshot.
    :param brief_id: Optional saved-goal id from ``design_session``.  When
        the brief resolves, the report appends a single ``Goal:`` line
        with the design's duty and environment — so the agent watching
        a print can answer "is this the right design for the goal?"
        without a separate lookup.  Best-effort: a missing kiln-pro
        install or an unresolvable brief silently skips the line.
    """
    try:
        if printer_name:
            adapter = _get_registry().get(printer_name)
        else:
            adapter = _get_adapter()

        state = adapter.get_state()
        job = adapter.get_job()
        sd = state.to_dict()
        jd = job.to_dict()

        # --- Extract fields ---
        state_str = sd.get("state", "unknown")
        completion = jd.get("completion")
        file_name = jd.get("file_name") or "N/A"
        current_layer = jd.get("current_layer")
        total_layers = jd.get("total_layers")
        elapsed_s = jd.get("print_time_seconds")
        remaining_s = jd.get("print_time_left_seconds")
        tool_actual = sd.get("tool_temp_actual")
        tool_target = sd.get("tool_temp_target")
        bed_actual = sd.get("bed_temp_actual")
        bed_target = sd.get("bed_temp_target")
        chamber_actual = sd.get("chamber_temp_actual")
        speed_profile = sd.get("speed_profile")
        speed_magnitude = sd.get("speed_magnitude")
        print_error = sd.get("print_error", 0)

        # --- Format values ---
        progress_str = f"{completion:.0f}" if completion is not None else "N/A"
        layer_str = (
            f"{current_layer} / {total_layers}" if current_layer is not None and total_layers is not None else "N/A"
        )
        elapsed_str = _format_duration(elapsed_s)
        remaining_str = _format_duration(remaining_s)
        nozzle_str = (
            f"{tool_actual:.0f}°C → {tool_target:.0f}°C target"
            if tool_actual is not None and tool_target is not None
            else "N/A"
        )
        bed_str = (
            f"{bed_actual:.0f}°C → {bed_target:.0f}°C target"
            if bed_actual is not None and bed_target is not None
            else "N/A"
        )
        if speed_profile is not None and speed_magnitude is not None:
            speed_str = f"{speed_profile} ({speed_magnitude}%)"
        else:
            speed_str = "N/A"
        error_str = f"Code {print_error}" if print_error and print_error > 0 else "None"

        # --- Snapshot ---
        snapshot_line = "No camera available"
        if include_snapshot:
            try:
                image_data = adapter.get_snapshot()
                if image_data is not None:
                    import tempfile as _tmpmod
                    import uuid as _uuid

                    snap_dir = _tmpmod.gettempdir()
                    snap_path = os.path.join(snap_dir, f"kiln_monitor_{_uuid.uuid4().hex[:12]}.jpg")
                    with open(snap_path, "wb") as f:
                        f.write(image_data)
                    snapshot_line = snap_path
            except Exception as snap_exc:
                logger.debug("Snapshot capture failed: %s", snap_exc)
                snapshot_line = "Snapshot capture failed"

        # --- MQTT staleness detection ---
        # Bambu A1 MQTT telemetry can lag 30-60s behind reality on long
        # first layers.  Track elapsed_s across calls; if wall-clock time
        # advances but printer-reported elapsed doesn't, warn the agent.
        import time as _time_mod
        _now = _time_mod.time()
        _stale_warning = ""
        if elapsed_s is not None and state_str == "printing":
            _prev = getattr(monitor_print, "_last_elapsed", None)
            _prev_ts = getattr(monitor_print, "_last_ts", None)
            if _prev is not None and _prev_ts is not None:
                wall_delta = _now - _prev_ts
                printer_delta = (elapsed_s or 0) - (_prev or 0)
                if wall_delta > 30 and printer_delta < 5:
                    _stale_warning = (
                        f"MQTT telemetry may be stale — {wall_delta:.0f}s of "
                        f"wall-clock time passed but printer-reported elapsed "
                        f"only advanced {printer_delta:.0f}s. Visual check "
                        f"recommended."
                    )
            monitor_print._last_elapsed = elapsed_s  # type: ignore[attr-defined]
            monitor_print._last_ts = _now  # type: ignore[attr-defined]

        # --- Smart-monitoring summary (Tier-1 fields) ---
        # Five conditional lines surfacing the new monitoring/recovery
        # wiring (predictive, detective, auto_recover, auto_pause,
        # reroute).  Each line only appears when its underlying state
        # is present, so a healthy print with no active monitoring
        # shows the same compact format as before.  Best-effort
        # throughout — any helper failure debug-logs and skips its
        # line; never blocks monitor_print.
        _resolved_printer = (
            printer_name or _resolve_effective_printer_name(printer_name)
        )
        _monitoring_line = ""
        _risk_line = ""
        _predictive_line = ""
        _detective_line = ""
        _auto_recover_line = ""
        _auto_pause_line = ""
        _reroute_line = ""

        # 1) Public Kiln side — monitoring summary (predictive, detective,
        #    auto-pause, headline risk score).
        try:
            from kiln.print_health_monitor import get_print_health_monitor

            _signals = get_print_health_monitor().get_latest_signals(
                _resolved_printer,
            )
            if _signals.get("monitoring_active"):
                _sid = _signals.get("session_id") or ""
                _sid_short = _sid[:8] if _sid else "?"
                _monitoring_line = (
                    f"MONITORING: active "
                    f"({_signals.get('report_count', 0)} reports, "
                    f"{_signals.get('issue_count', 0)} issues, "
                    f"session {_sid_short})"
                )

                _risk = _signals.get("risk")
                if _risk:
                    _kinds = _risk.get("kinds") or []
                    _kinds_str = ", ".join(_kinds) if _kinds else "(no signals)"
                    _risk_line = (
                        f"RISK: {_risk.get('score', 0.0):.2f} "
                        f"{_risk.get('severity', '?')} "
                        f"({_kinds_str})"
                    )

                _pred = _signals.get("predictive")
                if _pred:
                    _predictive_line = (
                        f"PREDICTIVE: {_pred.get('severity', '?')} "
                        f"{_pred.get('kind', 'signal')} — "
                        f"{_pred.get('detail', '') or '(no detail)'}"
                    )

                _det = _signals.get("detective")
                if _det:
                    _det_age = ""
                    _reported_at = _det.get("reported_at")
                    if _reported_at:
                        try:
                            import time as _time_d
                            _age_s = max(0.0, _time_d.time() - float(_reported_at))
                            _det_age = f" ({_age_s / 60.0:.0f}m ago)"
                        except (TypeError, ValueError):
                            pass
                    _detective_line = (
                        f"DETECTIVE: {_det.get('severity', '?')} "
                        f"{_det.get('failure_type', 'failure')} "
                        f"(failure_id "
                        f"{(_det.get('failure_id') or 'n/a')[:8]}"
                        f"{_det_age})"
                    )

                _ap = _signals.get("auto_pause")
                if _ap:
                    _ap_age_s = float(_ap.get("age_seconds") or 0.0)
                    _ap_age = (
                        f"{_ap_age_s / 60.0:.0f}m ago"
                        if _ap_age_s >= 60
                        else f"{_ap_age_s:.0f}s ago"
                    )
                    _ap_status = "paused"
                    if _ap.get("skipped"):
                        _ap_status = f"skipped ({_ap['skipped']})"
                    elif _ap.get("error"):
                        _ap_status = f"error ({_ap['error']})"
                    _auto_pause_line = (
                        f"AUTO_PAUSE: {_ap_age} "
                        f"({_ap.get('issue_type', 'issue')} -> {_ap_status})"
                    )
        except Exception as _sig_exc:
            logger.debug("Signal surfacing skipped: %s", _sig_exc)

        # 2) kiln-pro side — auto_recover + reroute (if installed).
        try:
            from kiln_pro.recovery.auto_recover_engine import (
                AutoRecoverStatus as _AR_Status,
            )
            from kiln_pro.recovery.auto_recover_engine import (
                list_sessions as _ar_list_sessions,
            )

            _ar_sessions = _ar_list_sessions(printer_name=_resolved_printer)
            if _ar_sessions:
                # Pick the most recently-started session.  Active
                # sessions take priority over completed ones for the
                # AUTO_RECOVER line; reroute scans across both.
                _ar_active = [
                    s for s in _ar_sessions
                    if s.status not in (
                        _AR_Status.DONE_SUCCESS,
                        _AR_Status.DONE_FAILURE,
                        _AR_Status.NO_FAILURE,
                        _AR_Status.CANCELLED,
                        _AR_Status.ERRORED,
                    )
                ]
                _candidate_for_active = (
                    max(_ar_active, key=lambda s: s.started_at)
                    if _ar_active else None
                )
                if _candidate_for_active is not None:
                    _ar_id_short = _candidate_for_active.auto_recover_id[:8]
                    _auto_recover_line = (
                        f"AUTO_RECOVER: {_candidate_for_active.status.value} "
                        f"(id {_ar_id_short})"
                    )

                # Pick the most-recently-completed session that carries
                # a reroute recommendation (across active + completed).
                _ar_with_reroute = [
                    s for s in _ar_sessions if s.reroute_recommendation
                ]
                if _ar_with_reroute:
                    _latest = max(_ar_with_reroute, key=lambda s: s.started_at)
                    _r = _latest.reroute_recommendation or {}
                    if _r.get("should_reroute"):
                        _target = _r.get("target_printer_id") or "?"
                        _success = _r.get("estimated_waste_pct")
                        _reroute_line = (
                            f"REROUTE: {_target} ready "
                            f"(was {_resolved_printer})"
                        )
                    else:
                        _blocked = _r.get("blocked_by_rule") or "blocked"
                        _reroute_line = (
                            f"REROUTE: blocked ({_blocked} — "
                            f"{(_r.get('reason') or '')[:60]})"
                        )
        except ImportError:
            pass  # kiln-pro not installed
        except Exception as _ar_exc:
            logger.debug("auto_recover surfacing skipped: %s", _ar_exc)

        # --- Comments ---
        comment = _generate_print_comment(
            state_str,
            completion=completion,
            tool_actual=tool_actual,
            tool_target=tool_target,
            bed_actual=bed_actual,
            bed_target=bed_target,
            print_error=print_error,
        )
        if _stale_warning:
            comment = f"{_stale_warning} {comment}"

        # --- Assemble report ---
        lines = [
            f"Print Status — {progress_str}% complete",
            f"- File: {file_name}",
            f"- Layer: {layer_str}",
            f"- Time elapsed: {elapsed_str} | Remaining: {remaining_str}",
            f"- Nozzle: {nozzle_str}",
            f"- Bed: {bed_str}",
        ]
        if chamber_actual is not None:
            lines.append(f"- Chamber: {chamber_actual:.0f}°C")
        lines.extend(
            [
                f"- Speed: {speed_str}",
                f"- Errors: {error_str}",
            ]
        )

        # --- Material usage & cost estimate ---
        # Try to get filament weight from gcode metadata (more accurate
        # than time-based heuristic).  Also count tool changes for purge waste.
        _filament_weight_g: float | None = None
        _tool_changes = 0
        if file_name:
            try:
                from kiln.printers.base import PrinterFile

                # Check if we have the file locally (in prints dir or temp)
                _local_paths = []
                try:
                    from kiln.cli.config import get_prints_dir

                    _prints = get_prints_dir()
                    import glob as _globmod

                    _local_paths = _globmod.glob(
                        str(_prints / "**" / "gcode" / "*.gcode"),
                        recursive=True,
                    )
                except Exception:
                    pass

                # Also check the uploaded file metadata from the printer
                _pf = PrinterFile(name=file_name)
                _meta = adapter.get_file_metadata(file_name)
                if _meta and hasattr(_meta, "filament_used_mm") and _meta.filament_used_mm:
                    # Convert mm to grams: volume = pi * (d/2)^2 * length
                    import math as _math

                    _d = 1.75  # mm filament diameter
                    _vol_mm3 = _math.pi * (_d / 2) ** 2 * _meta.filament_used_mm
                    _density = 0.00124  # PLA g/mm³
                    _filament_weight_g = _vol_mm3 * _density
            except Exception:
                pass  # Fallback to time-based

            # Count T commands in merged gcode to estimate tool changes
            try:
                for _lp in _local_paths:
                    if "merged" in _lp.lower() or "multicolor" in _lp.lower():
                        import re as _re_mod

                        with open(_lp) as _f:
                            _gc = _f.read()
                        _tool_changes = len(_re_mod.findall(r"^T\d+$", _gc, _re_mod.MULTILINE))
                        # Also try to get filament weight from gcode comments
                        if _filament_weight_g is None:
                            _fil_g_m = _re_mod.search(r"filament used \[g\]\s*=\s*([\d.]+)", _gc)
                            if _fil_g_m:
                                _filament_weight_g = float(_fil_g_m.group(1))
                        break
            except Exception:
                pass

        cost_info = _estimate_print_cost(
            elapsed_s,
            remaining_s,
            filament_weight_g=_filament_weight_g,
            tool_changes=_tool_changes,
        )
        if cost_info is not None:
            weight_str = f"~{cost_info['estimated_weight_g']:.0f}g {cost_info['material']}"
            if cost_info["purge_waste_g"] > 0:
                weight_str += f" ({cost_info['print_weight_g']:.0f}g print + {cost_info['purge_waste_g']:.0f}g purge)"
            lines.append(f"- Material used: {weight_str}")
            lines.append(
                f"- Cost: ~${cost_info['material_cost_usd']:.2f} material"
                f" + ~${cost_info['electricity_cost_usd']:.2f} electricity"
                f" = ~${cost_info['total_cost_usd']:.2f} total"
            )

        # Smart-monitoring lines, in stable order so the format is
        # predictable for prompt-engineering.  Order: state line ->
        # headline number -> raw signals -> orchestration state.
        if _monitoring_line:
            lines.append(f"- {_monitoring_line}")
        if _risk_line:
            lines.append(f"- {_risk_line}")
        if _predictive_line:
            lines.append(f"- {_predictive_line}")
        if _detective_line:
            lines.append(f"- {_detective_line}")
        if _auto_recover_line:
            lines.append(f"- {_auto_recover_line}")
        if _auto_pause_line:
            lines.append(f"- {_auto_pause_line}")
        if _reroute_line:
            lines.append(f"- {_reroute_line}")
        # B10 brief tail + D3 sidecar auto-derivation: surface the
        # saved-goal context when either the caller supplied a brief_id
        # OR the printed file's intent sidecar resolves to one (via the
        # upload manifest).  Best-effort — absent / unresolvable brief
        # silently omits the line so the report stays clean.
        effective_brief_id = brief_id or _auto_derive_brief_id(file_name)
        goal_line = _format_goal_line_for_monitor(effective_brief_id)
        if goal_line:
            lines.append(goal_line)

        lines.extend(
            [
                f"Camera: {snapshot_line}",
                f"Comments: {comment}",
            ]
        )
        report = "\n".join(lines)

        return report

    except PrinterNotFoundError:
        return _error_dict(
            f"Printer {printer_name!r} not found in registry.",
            code="PRINTER_NOT_FOUND",
        )
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to monitor print: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error in monitor_print")
        return _error_dict(
            f"Unexpected error in monitor_print: {exc}",
            code="INTERNAL_ERROR",
        )


@mcp.tool()
def printer_files() -> dict:
    """List all G-code files available on the printer.

    Handles FTPS directory listing (Bambu), REST file API
    (OctoPrint/Moonraker) automatically.

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

    Handles FTPS (Bambu), REST multipart upload (OctoPrint/Moonraker), and
    serial file transfer automatically.

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
                from kiln.gcode import GCodeDialect, scan_gcode_file

                # Use dialect-aware scanning so that commands standard for
                # the printer firmware (e.g. M500 for Bambu bed-leveling)
                # are not falsely blocked.
                _dialect = (
                    GCodeDialect.BAMBU
                    if _PRINTER_TYPE == "bambu"
                    else GCodeDialect.KLIPPER
                    if _PRINTER_TYPE in ("moonraker", "creality")
                    else GCodeDialect.GENERIC
                )
                scan = scan_gcode_file(
                    file_path,
                    printer_id=_PRINTER_MODEL or None,
                    dialect=_dialect,
                )
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

        # -- Bed-fit safety gate (Layer 2 — last-line-of-defense) -------------
        # Incident #0 (2026-04-15, Bambu A1): sliced gcode had X=-12.5
        # moves that drove the nozzle into the purge tool.  This gate
        # inspects the ACTUAL bytes being uploaded to the printer — it's
        # the last chance to reject dangerous coordinates before the
        # physical device sees them.  Runs on both raw .gcode and
        # .gcode.3mf (Bambu).
        try:
            from kiln.printers.bed_fit import (
                validate_3mf_for_printer,
                validate_gcode_for_printer,
            )
            _ext = os.path.splitext(file_path)[1].lower()
            bed_fit_result: dict[str, Any] | None = None
            _live_model = _resolve_printer_model_live() or _PRINTER_MODEL
            if _ext in _GCODE_EXTENSIONS:
                bed_fit_result = validate_gcode_for_printer(
                    file_path, _live_model,
                )
            elif _ext == ".3mf" or file_path.lower().endswith(".gcode.3mf"):
                bed_fit_result = validate_3mf_for_printer(
                    file_path, _live_model,
                )
            if bed_fit_result and not bed_fit_result.get("ok", True):
                code = bed_fit_result.get("error_code", "BED_FIT_ERROR")
                if code in ("OFF_BED_GEOMETRY", "EXCEEDS_BED", "NO_HOMING_SEQUENCE"):
                    _audit(
                        "upload_file",
                        "bed_fit_blocked",
                        details={
                            "file": os.path.basename(file_path),
                            "error_code": code,
                            "bbox": bed_fit_result.get("bbox"),
                        },
                    )
                    return {
                        "success": False,
                        "error": {
                            "code": code,
                            "message": (
                                f"Upload blocked: {bed_fit_result.get('error_message', 'off-bed geometry')}. "
                                f"This would have driven the nozzle into the printer frame. "
                                f"Re-slice with auto_center=True or call center_model_on_bed first."
                            ),
                        },
                        "bed_fit": bed_fit_result,
                    }
        except Exception as exc:
            # Bed-fit check is defense-in-depth; don't block uploads on
            # a check failure — slice_model/slice_and_print already ran
            # their own gate upstream.  Log and continue.
            logger.debug("Bed-fit check skipped due to error: %s", exc)

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

        # D3: record source_path → printer_file_name in the upload
        # manifest so monitor_print / await_print_completion can later
        # auto-derive the brief_id by reading the source's intent
        # sidecar.  Best-effort — manifest IO failures must not break
        # the upload return.  The printer-reported file name lives at
        # ``resp["file_name"]`` (or ``resp["filename"]`` on some
        # adapters); fall back to the basename of file_path otherwise.
        try:
            from kiln.upload_manifest import record_upload
            printer_file_name = (
                resp.get("file_name")
                or resp.get("filename")
                or os.path.basename(file_path)
            )
            record_upload(file_path, printer_file_name)
        except Exception:
            logger.debug(
                "upload_file: manifest record failed (best-effort)",
                exc_info=True,
            )

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

    .. note::
        For multi-object .gcode.3mf files, also consider using
        ``list_plate_objects()`` to see individual objects on the plate.

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


def _ams_selection_record(
    slot: int,
    tray_type: str,
    ams_info: dict[str, Any],
) -> dict[str, Any]:
    """Build a human-readable AMS selection record ``{slot, type, color}``.

    ``color`` is looked up from the (already-fetched) ``ams_info`` by slot
    so callers can render "AMS slot 1 — black PLA" without another MQTT
    round-trip.  Returns ``color=""`` when the tray reports no color.
    """
    color = ""
    for unit in ams_info.get("units", []):
        for tray in unit.get("trays", []):
            try:
                if int(tray.get("slot", -1)) == int(slot):
                    color = str(tray.get("tray_color", "") or "")
                    break
            except (TypeError, ValueError):
                continue
        if color:
            break
    return {"slot": int(slot), "type": tray_type, "color": color}


def _resolve_use_ams(
    use_ams: str | bool,
    ams_mapping: list[int] | None,
    adapter: PrinterAdapter,
    material: str | None = None,
) -> dict[str, Any]:
    """Resolve tri-state use_ams into a concrete routing decision.

    :param use_ams: ``"auto"``, ``"true"``/``True``, or ``"false"``/``False``.
    :param ams_mapping: Explicit AMS mapping from the caller (may be None).
    :param adapter: Printer adapter to probe for AMS presence.
    :param material: Optional material hint (e.g. ``"PLA"``).  When set,
        auto-routing prefers a tray whose ``tray_type`` matches (case-
        insensitive), falling back to the first loaded tray and
        attaching a warning when no match is found.
    :returns: Dict with ``use_ams`` (bool), optional ``ams_mapping`` (list),
        and optional ``warnings`` (list of human-readable strings).
    """
    # Normalize string/bool input
    if isinstance(use_ams, bool):
        return {"use_ams": use_ams, "ams_mapping": None, "warnings": []}

    val = str(use_ams).lower().strip()
    if val in ("true", "1", "yes"):
        return {"use_ams": True, "ams_mapping": None, "warnings": []}
    if val in ("false", "0", "no"):
        return {"use_ams": False, "ams_mapping": None, "warnings": []}

    # "auto" — probe the printer for AMS
    if val != "auto":
        logger.warning("Unknown use_ams value %r, treating as 'auto'", use_ams)

    # Check if the adapter supports AMS queries
    if not hasattr(adapter, "get_ams_status"):
        logger.debug("Adapter has no get_ams_status — AMS auto-detect disabled.")
        return {"use_ams": False, "ams_mapping": None, "warnings": []}

    try:
        ams_info = adapter.get_ams_status()
    except Exception as exc:
        logger.warning(
            "AMS auto-detect probe failed: %s — external spool fallback, "
            "but this may be wrong if AMS is attached.  Pass use_ams='true' "
            "with an explicit ams_mapping to force AMS routing.",
            exc,
        )
        return {
            "use_ams": False,
            "ams_mapping": None,
            "warnings": [
                f"AMS auto-detect probe failed: {exc}.  Falling back to "
                "external spool — pass use_ams='true' with an explicit "
                "ams_mapping=[<slot>] if you want AMS."
            ],
            "ambiguous": True,
        }

    # Check if AMS exists.  Bambu's MQTT can return zeroed AMS state for
    # a few seconds after a cancel / disconnect / adapter reconnect —
    # the AMS is still physically attached but the cached state hasn't
    # repopulated yet.  Retrying once picks up the republished state
    # and prevents the silent external-spool fallthrough that used to
    # route every post-cancel reslice to the wrong filament path
    # (memory: "always route to AMS when printer has one").
    units = ams_info.get("units", [])
    ams_exist_bits = str(ams_info.get("ams_exist_bits", "0")).strip()
    tray_exist_bits = str(ams_info.get("tray_exist_bits", "0")).strip()
    if not units:
        import time as _time
        _time.sleep(1.2)
        try:
            ams_info_retry = adapter.get_ams_status()
            retry_units = ams_info_retry.get("units", [])
            if retry_units:
                ams_info = ams_info_retry
                units = retry_units
                ams_exist_bits = str(ams_info.get("ams_exist_bits", "0")).strip()
                tray_exist_bits = str(ams_info.get("tray_exist_bits", "0")).strip()
                logger.info(
                    "AMS auto-detect: initial probe returned no units, retry "
                    "picked up %d unit(s).  (Transient MQTT cache — normal "
                    "after cancel/reconnect.)",
                    len(units),
                )
        except Exception:
            pass

    # Still no units after retry — is AMS hardware actually present?
    if not units:
        hw_present = ams_exist_bits != "0" or tray_exist_bits != "0"
        if hw_present:
            # Hardware bits say AMS exists but no tray state.  This
            # is almost always stale cache or an unplugged AMS cable.
            # Either way, refusing to silent-route is the correct
            # behaviour per the "always route to AMS" memory rule.
            logger.warning(
                "AMS hardware present (ams_exist_bits=%s, tray_exist_bits=%s) "
                "but no tray state available after retry.  Refusing silent "
                "external-spool fallthrough.  Pass use_ams='true' with an "
                "explicit ams_mapping=[<slot>] to force AMS routing.",
                ams_exist_bits,
                tray_exist_bits,
            )
            return {
                "use_ams": False,
                "ams_mapping": None,
                "warnings": [
                    f"AMS hardware detected (ams_bits={ams_exist_bits}, "
                    f"tray_bits={tray_exist_bits}) but no tray state was "
                    "reported after retry.  Routing would silently fall "
                    "through to the external spool — pass use_ams='true' "
                    "with an explicit ams_mapping=[<slot>] to force AMS, "
                    "or wait a few seconds for the MQTT cache to refresh."
                ],
                "ambiguous": True,
            }
        # No AMS hardware bits set either — genuinely no AMS on this
        # printer.  External spool is correct.
        logger.info("AMS auto-detect: no AMS hardware — external spool.")
        return {"use_ams": False, "ams_mapping": None, "warnings": []}

    # Collect loaded trays.  Trust ``tray_type`` as the loaded-indicator —
    # the A1/AMS Lite reports ``remain: 0`` even for full spools (RFID
    # capacity is only tracked on Bambu-branded spools with tag readers),
    # so requiring ``remain > 0`` would incorrectly reject valid trays
    # and force a broken external-spool route.
    #
    # Bambu's JSON sometimes returns slot IDs as strings (``"0"``) — coerce
    # here so downstream ``%d`` logging and ``int`` arithmetic don't trip.
    loaded_trays: list[dict[str, Any]] = []
    for unit in units:
        for tray in unit.get("trays", []):
            tray_type = str(tray.get("tray_type", "") or "").strip()
            if not tray_type:
                continue
            try:
                slot_idx = int(tray.get("slot", 0))
            except (TypeError, ValueError):
                continue
            loaded_trays.append({
                "slot": slot_idx,
                "tray_type": tray_type,
            })

    if not loaded_trays:
        logger.warning(
            "AMS present but no trays report loaded filament — routing to "
            "external spool.  If the external-spool feeder is empty the "
            "print will pause with error 0300-8015.",
        )
        return {
            "use_ams": False,
            "ams_mapping": None,
            "warnings": [
                "AMS is attached but no trays report loaded filament. "
                "Print will use the external-spool feed path — if nothing "
                "is loaded there the print will pause with error 0300-8015."
            ],
        }

    warnings_out: list[str] = []

    # Material-aware tray selection when caller hints at the material.
    chosen = None
    if material:
        mat_norm = material.strip().upper()
        for tray in loaded_trays:
            if tray["tray_type"].upper() == mat_norm:
                chosen = tray
                break
        if chosen is None:
            chosen = loaded_trays[0]
            warnings_out.append(
                f"No AMS tray matches material {material!r}; using tray "
                f"{chosen['slot']} ({chosen['tray_type']}) instead."
            )
    else:
        chosen = loaded_trays[0]

    logger.info(
        "AMS auto-detect: routing to tray %d (%s) — %d loaded slot(s) available.",
        chosen["slot"],
        chosen["tray_type"],
        len(loaded_trays),
    )

    # Auto-generate mapping only if caller didn't provide one
    auto_mapping = None
    if ams_mapping is None:
        auto_mapping = [chosen["slot"]]  # already coerced to int above

    # Human-readable selection record so callers can surface
    # "printing from AMS slot 1 — black PLA" without re-querying.
    # Recovered from ams_info because loaded_trays kept only slot+type.
    selection = _ams_selection_record(chosen["slot"], chosen["tray_type"], ams_info)

    return {
        "use_ams": True,
        "ams_mapping": auto_mapping,
        "warnings": warnings_out,
        "selection": selection,
    }


@mcp.tool()
def start_print(
    file_name: str,
    use_ams: str = "auto",
    ams_mapping: list[int] | None = None,
    timelapse: bool = False,
    bed_leveling: bool = True,
    flow_cali: bool = True,
    vibration_cali: bool = True,
    layer_inspect: bool = False,
    nozzle_clog_detect: bool = True,
    bed_type: str = "auto",
    plate_number: int = 1,
    resume_from_paused: bool = False,
    skip_preheat_reassert: bool = False,
    preview_token: str | None = None,
) -> dict:
    """Start printing a file already uploaded to the printer (file must exist on printer).

    Use ``upload_file`` first, or use ``slice_and_print`` / ``run_quick_print``
    to slice + upload + print in one step. Automatically runs pre-flight safety
    checks before starting.  If any check fails the print is blocked and the
    check results are returned so the agent can diagnose and fix the issue.

    Args:
        file_name: Name or path of the file as shown by ``printer_files()``.
        use_ams: AMS filament feeding mode (Bambu only).  Tri-state:

            - ``"auto"`` (default): auto-detect AMS by probing the printer.
              If an AMS is connected with loaded trays, enables AMS
              automatically and selects the first loaded slot if no
              ``ams_mapping`` is provided.  Falls back to external spool
              if no AMS is detected.
            - ``"true"`` / ``True``: Force AMS on.  Use when you know AMS
              is connected.
            - ``"false"`` / ``False``: Force AMS off.  Use external spool.

        ams_mapping: Slot mapping per extruder (Bambu only).  Default
            ``[0]``.  Use ``[-1]`` for unused slots.  Check ``ams_status()``
            to see which slots have filament.
        timelapse: Record a timelapse video (Bambu only).  Default ``False``.
        bed_leveling: Run automatic bed leveling before print (Bambu only).
            Default ``True``.  Set ``False`` to skip for reprints (~2 min saved).
        flow_cali: Run flow calibration (Bambu only).  Default ``True``.
        vibration_cali: Run vibration/resonance calibration (Bambu only).
            Default ``True``.
        layer_inspect: Enable first-layer lidar inspection pause (Bambu only).
            Default ``False``.
        nozzle_clog_detect: Enable nozzle clumping / blob detection by
            probing (Bambu only).  Default ``True``.  Set ``False`` to
            bypass HMS 0300-8014 errors on models that trigger false
            positives (thin first-layer geometry, certain grip/case models).
            Disables the A1/A1-mini eddy-current clump probe (first after
            the layer-3 walls, then once per ~8 g of filament; A1 series only).
        bed_type: Bed surface type (Bambu only).  Default ``"auto"``.
        plate_number: Plate index in multi-plate 3MF files (Bambu only).
            Default ``1``.
        resume_from_paused: When ``True``, the pre-flight ``printer_idle``
            check accepts ``paused`` as a valid state.  Use this when
            starting a resume-mode 3MF (mid-print decoration swap):
            the printer is paused, you upload the resume 3MF, and then
            ``start_print`` it with this flag.  The file is treated as
            a fresh print from the firmware's POV — the resume gcode
            carries its own preamble (heat → safety lift → home X/Y →
            travel → descend to resume Z).  Default ``False``.

            Auto-detected for files whose name contains ``_resume_``
            (case-insensitive) or starts with ``transformed_resume`` /
            ``original_resume`` — these are the conventional names
            produced by ``decorate_during_print`` and ``revert_mid_print``.
        skip_preheat_reassert: When the file is a resume-mode 3MF, the
            tool re-asserts the printer's pre-start hotend + bed targets
            immediately after the MQTT start command, because Bambu's
            resume 3MFs strip the M140/M190 pre-heat block (the original
            print already heated the bed) and the firmware's
            cool-on-new-job policy will otherwise drop the bed to 0
            before the resume preamble executes.  Set ``True`` to
            disable this safety net.  Default ``False``.
    """
    if err := _check_auth("print"):
        return err
    if err := _check_rate_limit("start_print"):
        return err
    if conf := _check_confirmation("start_print", {"file_name": file_name}):
        return conf
    if block := _emergency_latch_error("start_print", _resolve_effective_printer_name()):
        return block

    # -- Preview confirmation gate -----------------------------------------
    # Refuse to start a print unless the agent has demonstrated a preview
    # was rendered and the user approved.  Bypass via
    # KILN_SKIP_PREVIEW_GATE=1 for CI / advanced users.  Skipped for
    # resume-mode 3MFs because the print is already in progress.
    _skip_preview_gate = os.environ.get("KILN_SKIP_PREVIEW_GATE", "").strip() in (
        "1", "true", "yes",
    )
    _is_resume = resume_from_paused or _is_resume_mode_3mf(file_name)
    if not _skip_preview_gate and not _is_resume:
        if not preview_token:
            return _error_dict(
                "start_print refuses to proceed without a preview confirmation. "
                "Render a preview with visualize_model(), show it to the user, "
                "and call issue_preview_token(file_path) to get a token. "
                "Pass the token as preview_token=<token>. To bypass (advanced / "
                "CI only), set KILN_SKIP_PREVIEW_GATE=1.",
                code="PREVIEW_NOT_CONFIRMED",
            )
        try:
            from kiln.preview_gate import get_preview_gate
            # Find the local file path corresponding to this printer file
            # name so we can validate the token against the actual bytes.
            # We can't hash a file on the printer; match by file_name hash
            # instead (token must have been issued with file_name as path
            # argument or as a pre-computed hash).
            ok, reason = get_preview_gate().validate(
                preview_token, file_name, printer_id=_PRINTER_MODEL,
            )
            if not ok:
                return _error_dict(
                    f"Preview token rejected: {reason}. Re-render the preview "
                    f"and issue a fresh token.",
                    code="PREVIEW_TOKEN_INVALID",
                )
        except Exception as exc:
            logger.warning("Preview gate validation failed: %s", exc)
    elif _skip_preview_gate and not _is_resume:
        logger.warning(
            "KILN_SKIP_PREVIEW_GATE is set — skipping mandatory preview "
            "confirmation for start_print(%s).  Only do this in CI.",
            file_name,
        )
        _audit("start_print", "preview_gate_skipped", details={"file": file_name})

    # Auto-detect resume-mode 3MFs by filename.  Convention: files
    # produced by decorate_during_print / revert_mid_print are named
    # ``transformed_resume_<sid>.3mf`` or ``original_resume_<sid>.3mf``,
    # both contain the substring ``_resume_``.  When detected, we flip
    # ``resume_from_paused`` so the printer_idle preflight check accepts
    # ``paused`` as a valid state.  Caller can still pass it explicitly.
    is_resume_3mf = _is_resume_mode_3mf(file_name)
    if is_resume_3mf and not resume_from_paused:
        resume_from_paused = True
        logger.info(
            "start_print: auto-detected resume-mode 3MF %r — accepting paused state in preflight",
            file_name,
        )

    try:
        adapter = _get_adapter()

        # Snapshot pre-start temperature targets BEFORE the cancel/start
        # so we can re-assert them after the MQTT start command kicks
        # off the cool-on-new-job sequence.  Bambu resume-mode 3MFs
        # omit the M140/M190 pre-heat block (the original print
        # already heated the bed), so without this re-assert the bed
        # drops to 0 before the resume preamble runs and adhesion
        # fails.  Only relevant for resume-mode 3MFs.
        pre_start_targets: dict[str, float] | None = None
        if is_resume_3mf and not skip_preheat_reassert:
            try:
                _state = adapter.get_state()
                pre_start_targets = {
                    "tool": float(_state.tool_temp_target or 0.0),
                    "bed": float(_state.bed_temp_target or 0.0),
                }
            except Exception as exc:
                logger.debug(
                    "start_print: pre-start state snapshot failed (%s); skipping reassert",
                    exc,
                )
                pre_start_targets = None

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
            pf = unwrap_tool_result(
                preflight_check(remote_file=file_name, accept_paused=resume_from_paused)
            )
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

        # Build kwargs for Bambu-specific print parameters.
        print_kwargs: dict[str, Any] = {}

        # Resolve tri-state use_ams: "auto" | "true"/"false" | bool
        _ams_decision = _resolve_use_ams(use_ams, ams_mapping, adapter)
        if _ams_decision["use_ams"]:
            print_kwargs["use_ams"] = True
        if _ams_decision.get("ams_mapping") is not None:
            ams_mapping = _ams_decision["ams_mapping"]
        if ams_mapping is not None:
            print_kwargs["ams_mapping"] = ams_mapping
        if timelapse:
            print_kwargs["timelapse"] = True
        if not bed_leveling:
            print_kwargs["bed_leveling"] = False
        if not flow_cali:
            print_kwargs["flow_cali"] = False
        if not vibration_cali:
            print_kwargs["vibration_cali"] = False
        if layer_inspect:
            print_kwargs["layer_inspect"] = True
        if not nozzle_clog_detect:
            print_kwargs["nozzle_clog_detect"] = False
        if bed_type != "auto":
            print_kwargs["bed_type"] = bed_type
        if plate_number != 1:
            print_kwargs["plate_number"] = plate_number

        # -- Independent nozzle capacity check -----------------------------
        # Consult kiln-pro's nozzle wear model for the active printer
        # against the planned print's filament weight.  Free-tier
        # installs without kiln-pro silently skip via the bridge's
        # available() guard.
        #
        # Why run this here separately from preflight: preflight's
        # nozzle check only fires when planned_grams > 0, and
        # preflight's planned_grams source (local file_result) is only
        # populated for local file_path inputs.  start_print works with
        # a remote file_name, so we look up filament_used_mm directly
        # from the adapter's file listing.  This also keeps the
        # consultation alive when KILN_SKIP_PREFLIGHT bypasses the
        # full preflight gate.
        #
        # Verdict handling:
        # - exceeded_p90 -> refuse the print unless
        #   KILN_SKIP_NOZZLE_CHECK=1 (matches KILN_SKIP_PREFLIGHT's
        #   override convention).
        # - approaching / exceeded_p50 -> advisory only, attached to
        #   the success response under "nozzle_advisory" so the agent
        #   can surface it without blocking.
        # - unknown_* or absent -> silent skip.
        nozzle_advisory: dict[str, Any] | None = None
        try:
            from kiln import _pro_nozzle_bridge

            if _pro_nozzle_bridge.available():
                _printer_id = ""
                if _get_registry().count > 0:
                    _names = _get_registry().list_names()
                    if _names:
                        _printer_id = _names[0]

                _planned_grams = 0.0
                _filament_material = ""
                try:
                    _files_for_nozzle = adapter.list_files()
                    for _pf in _files_for_nozzle:
                        if (
                            _pf.name.lower() == file_name.lower()
                            or _pf.path.lower() == file_name.lower()
                        ):
                            if _pf.filament_used_mm:
                                import math as _m

                                # 1.75 mm filament, PLA density
                                # 0.00124 g/mm^3 — same baseline used
                                # in slice_and_print's gcode metadata
                                # parser (see _filament_weight_g logic).
                                _vol_mm3 = (
                                    _m.pi
                                    * (1.75 / 2) ** 2
                                    * _pf.filament_used_mm
                                )
                                _planned_grams = _vol_mm3 * 0.00124
                            if _pf.material:
                                _filament_material = _pf.material
                            break
                except Exception:
                    pass

                if _printer_id and _planned_grams > 0:
                    _nozzle_verdict = _pro_nozzle_bridge.consult_capacity(
                        printer_id=_printer_id,
                        planned_grams=_planned_grams,
                        filament_material=_filament_material,
                    )
                    if _nozzle_verdict is not None:
                        _nz_status = _nozzle_verdict.get("status")
                        if _nz_status == "exceeded_p90":
                            _skip_nozzle = os.environ.get(
                                "KILN_SKIP_NOZZLE_CHECK", ""
                            ).strip() in ("1", "true", "yes")
                            if not _skip_nozzle:
                                _audit(
                                    "start_print",
                                    "nozzle_capacity_blocked",
                                    details={
                                        "file": file_name,
                                        "status": _nz_status,
                                        "narrative": _nozzle_verdict.get(
                                            "narrative", ""
                                        ),
                                    },
                                )
                                return _error_dict(
                                    "Nozzle wear exceeds population p90: "
                                    f"{_nozzle_verdict.get('narrative', 'nozzle capacity exceeded')}. "
                                    "Replace the nozzle before starting "
                                    "this print, or set "
                                    "KILN_SKIP_NOZZLE_CHECK=1 to override.",
                                    code="NOZZLE_CAPACITY_EXCEEDED",
                                )
                            logger.warning(
                                "KILN_SKIP_NOZZLE_CHECK is set — proceeding "
                                "with start_print(%s) despite exceeded_p90 "
                                "wear: %s",
                                file_name,
                                _nozzle_verdict.get("narrative", ""),
                            )
                        if _nz_status in (
                            "approaching",
                            "exceeded_p50",
                            "exceeded_p90",
                        ):
                            nozzle_advisory = {
                                "status": _nz_status,
                                "narrative": _nozzle_verdict.get(
                                    "narrative", ""
                                ),
                                "percent_used": _nozzle_verdict.get(
                                    "percent_used"
                                ),
                            }
        except Exception as exc:
            logger.debug("Nozzle capacity check skipped: %s", exc)

        result = adapter.start_print(file_name, **print_kwargs)
        _get_heater_watchdog().notify_print_started()

        # Layer 5: spawn in-process PrintWatchdog to catch HMS codes,
        # thermal anomalies, stuck-layer conditions.  Agent-driven
        # polling (~60s) is too slow to catch clogs / crashes in time.
        # The watchdog polls every 2.5s and calls adapter.emergency_stop
        # on red flags.  Best-effort — a failure here must not abort
        # the print start.
        try:
            _spawn_print_watchdog(adapter, file_name)
        except Exception as _wd_exc:
            logger.warning(
                "PrintWatchdog spawn failed for %s (print continues without watchdog): %s",
                file_name, _wd_exc,
            )

        # Stop any pause keep-alive thread now that the print is back
        # under firmware control.  Safe to call when nothing's running.
        try:
            _pause_keepalive.stop()
        except Exception as exc:
            logger.debug("start_print: keep-alive stop failed (best-effort): %s", exc)

        # Re-assert the pre-start temperature targets immediately after
        # the start command so the firmware's cool-on-new-job sequence
        # doesn't drop the bed/tool to 0 before the resume preamble
        # runs.  Only fires for resume-mode 3MFs (which strip M140/M190)
        # AND when the caller hasn't opted out via skip_preheat_reassert.
        reasserted: dict[str, float] | None = None
        if (
            is_resume_3mf
            and not skip_preheat_reassert
            and pre_start_targets is not None
            and (pre_start_targets["tool"] > 0 or pre_start_targets["bed"] > 0)
        ):
            try:
                if pre_start_targets["tool"] > 0:
                    adapter.set_tool_temp(pre_start_targets["tool"])
                if pre_start_targets["bed"] > 0:
                    adapter.set_bed_temp(pre_start_targets["bed"])
                reasserted = pre_start_targets
            except Exception as exc:
                logger.warning(
                    "start_print: failed to re-assert pre-start temps "
                    "after resume-3MF start (%s): %s",
                    pre_start_targets, exc,
                )

        _audit(
            "start_print", "executed",
            details={
                "file": file_name,
                "resume_from_paused": resume_from_paused,
                "is_resume_3mf": is_resume_3mf,
                **print_kwargs,
            },
        )
        out = result.to_dict()
        if is_resume_3mf:
            out["resume_3mf_detected"] = True
        if reasserted is not None:
            out["preheat_reasserted"] = reasserted
        if nozzle_advisory is not None:
            out["nozzle_advisory"] = nozzle_advisory
        return out
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(
            f"Failed to start print: {exc}. Check that the printer is online and idle. Use printer_files() to verify the file exists."
        )
    except Exception as exc:
        logger.exception("Unexpected error in start_print")
        return _error_dict(f"Unexpected error in start_print: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def cancel_print(
    preserve_temperatures: bool = False,
    expected_tool_target: float | None = None,
    expected_bed_target: float | None = None,
    expected_chamber_target: float | None = None,
) -> dict:
    """Cancel the currently running print job.

    Sends cancel via MQTT (Bambu) or REST API (OctoPrint/Moonraker)
    automatically.

    The printer must have an active job (printing or paused).

    :param preserve_temperatures: When ``True``, re-asserts the pre-cancel
        hotend + bed (+ chamber, if expected_chamber_target is provided)
        targets immediately after the cancel command, so the printer
        does NOT cool down.  Use this when you plan to swap in a
        different file (e.g., a mid-print decoration resume 3MF) and need
        bed adhesion + nozzle temperature held across the cancel-then-
        start-print transition.  Without this, Bambu firmware defaults
        to cooling on cancel, which can warp the existing part or kill
        bed adhesion on a partial print you're about to resume.
        Default ``False`` preserves legacy behaviour (cool down to idle).

    :param expected_tool_target: Optional caller-supplied tool target to
        preserve.  When provided AND ``preserve_temperatures=True``,
        this overrides the introspected ``state.tool_temp_target``.
        Useful when the printer was paused and the firmware has already
        cleared the target (so a fresh state read returns 0) but the
        caller knows what the pre-pause target was.
    :param expected_bed_target: Same as above, for the bed.  This is
        the primary fix for Bambu A1 long-pause-then-cancel: the bed
        target sometimes reads back as 0 from MQTT cache after a long
        pause, and without an explicit override the cancel preservation
        skips the bed restore.
    :param expected_chamber_target: Optional chamber target (M141) to
        re-assert via raw G-code.  Not all printers expose chamber
        heating via the adapter API, so this is sent as a raw M141
        command best-effort.  Pass ``None`` to skip chamber preservation.

    WARNING: Cancellation is irreversible -- the print cannot be resumed
    from where it left off UNLESS a resume-mode 3MF has been pre-staged
    (see ``decorate_during_print`` and ``revert_mid_print``).
    """
    if err := _check_auth("print"):
        return err
    if err := _check_rate_limit("cancel_print"):
        return err
    if conf := _check_confirmation("cancel_print", {}):
        return conf
    try:
        adapter = _get_adapter()

        # Snapshot pre-cancel targets so we can restore them below.
        # Caller-supplied ``expected_*`` values take precedence over the
        # introspected state — this is the escape hatch for the case
        # where MQTT cache lag or firmware idle-cooldown has already
        # zeroed out the bed target before we can read it.
        preserved: dict[str, float] | None = None
        if preserve_temperatures:
            tool_t = 0.0
            bed_t = 0.0
            try:
                state = adapter.get_state()
                tool_t = float(state.tool_temp_target or 0.0)
                bed_t = float(state.bed_temp_target or 0.0)
            except Exception:
                # Best-effort — fall through to caller overrides.
                pass
            if expected_tool_target is not None:
                tool_t = float(expected_tool_target)
            if expected_bed_target is not None:
                bed_t = float(expected_bed_target)
            preserved = {
                "tool_target": tool_t,
                "bed_target": bed_t,
            }

        # Stop any pause keep-alive thread BEFORE the cancel — the
        # cancel will leave the printer in IDLE/CANCELLING and the
        # keep-alive's get_state() check would exit anyway, but
        # explicit shutdown avoids a possible race where set_*_temp
        # fights the cancel-cool sequence.
        try:
            _pause_keepalive.stop()
        except Exception as exc:
            logger.debug("cancel_print: keep-alive stop failed (best-effort): %s", exc)

        # Bug #10: register the cancel intent BEFORE issuing the cancel
        # command.  Bambu firmware has no "cancelled" gcode_state — a
        # successful cancel transitions the printer to "idle", which
        # looks identical to a natural finish.  The intent flag lets
        # auto_record_hook classify the next idle transition as a
        # cancel rather than a success, so the learning DB gets
        # ``outcome="cancelled"`` instead of a bogus ``"success"``.
        try:
            from kiln.auto_record_hook import register_cancel_intent
            register_cancel_intent(_resolve_effective_printer_name(None))
        except Exception as exc:  # pragma: no cover — best-effort
            logger.debug("cancel_print: register_cancel_intent failed: %s", exc)

        # Layer 6: if the print is being cancelled very early (before
        # layer 5), auto-capture an incident envelope to ~/.kiln/incidents/.
        # That's usually a crash, nozzle issue, or user stopping a bad
        # start — exactly when we want to preserve evidence.  Local only,
        # no upload.
        try:
            _state_at_cancel = adapter.get_state()
            _layer_at_cancel = getattr(
                getattr(_state_at_cancel, "job", None), "current_layer", 0,
            ) or 0
            if _layer_at_cancel < 5:
                from kiln import incident_recorder
                incident_recorder.record_incident(
                    incident_type="user_cancel_pre_layer_5",
                    printer_status={
                        "printer_name": _resolve_effective_printer_name(None),
                        "layer_at_cancel": _layer_at_cancel,
                        "state": getattr(_state_at_cancel, "state", None),
                    },
                    tags=["cancel", "early", "auto"],
                )
        except Exception as _inc_exc:
            logger.debug(
                "Early-cancel incident auto-capture skipped: %s", _inc_exc,
            )

        result = adapter.cancel_print()
        _get_heater_watchdog().notify_print_ended()
        # Layer 5: tear down the PrintWatchdog for this printer.
        _stop_print_watchdog()

        # Re-assert targets AFTER cancel so the firmware's default-cool
        # behaviour is overridden.  Only fires when the caller asked for
        # it AND the pre-cancel targets were non-zero (no point restoring
        # a printer that was already idle).  Uses the adapter's split
        # ``set_tool_temp`` / ``set_bed_temp`` methods — present on every
        # adapter subclass (base, bambu, octoprint, moonraker, creality,
        # serial, elegoo, prusalink).  Chamber temp (rare) is sent as raw
        # M141 G-code since most adapters don't expose a chamber setter.
        restored: dict[str, float] | None = None
        chamber_restored: float | None = None
        if preserve_temperatures and preserved is not None:
            if preserved["tool_target"] > 0 or preserved["bed_target"] > 0:
                try:
                    if preserved["tool_target"] > 0:
                        adapter.set_tool_temp(preserved["tool_target"])
                    if preserved["bed_target"] > 0:
                        adapter.set_bed_temp(preserved["bed_target"])
                    restored = preserved
                except Exception as exc:
                    logger.warning(
                        "cancel_print: failed to restore temperatures "
                        "after cancel (%s): %s",
                        preserved, exc,
                    )
            if expected_chamber_target is not None and expected_chamber_target > 0:
                # Best-effort — not every printer supports M141.  Bambu
                # X1 enclosed printers and some Voron/Ratrig setups do.
                try:
                    adapter.send_gcode([f"M141 S{int(expected_chamber_target)}"])
                    chamber_restored = float(expected_chamber_target)
                except Exception as exc:
                    logger.info(
                        "cancel_print: chamber temp re-assert (M141 S%s) failed (printer may not support chamber heating): %s",
                        expected_chamber_target, exc,
                    )

        _audit("cancel_print", "executed")

        out = result.to_dict()
        if preserve_temperatures:
            out["preserved_temperatures"] = restored
            if chamber_restored is not None:
                out["preserved_chamber"] = chamber_restored
        return out
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to cancel print: {exc}. Check that a print is currently active.")
    except Exception as exc:
        logger.exception("Unexpected error in cancel_print")
        return _error_dict(f"Unexpected error in cancel_print: {exc}", code="INTERNAL_ERROR")


@mcp.tool(name="calibrate_direct")
def run_calibration(options: list[str] | None = None) -> dict:
    """Send calibration commands directly to the printer adapter.

    For a full guided calibration pipeline (home + bed level + intelligence
    guidance), use ``run_calibrate`` instead. This tool sends raw calibration commands via
    MQTT (Bambu) or G-code (OctoPrint/Moonraker) automatically. The printer
    must be idle — calibration cannot run during a print.

    Available options (printer-specific -- not all printers support all):
    - ``"bed_leveling"``: Auto bed mesh probing and Z offset calibration
    - ``"vibration"``: Input shaper / vibration compensation tuning
    - ``"flow"``: Extrusion flow / first-layer inspection calibration
    - ``"all"``: Run all available calibration routines

    When no options specified, defaults to bed leveling only.

    Bambu printers support all options.  OctoPrint/Moonraker support
    ``bed_leveling`` and ``vibration``.  Other printers may not support
    remote calibration.
    """
    if err := _check_auth("print"):
        return err
    if err := _check_rate_limit("calibrate_direct"):
        return err
    try:
        adapter = _get_adapter()
        result = adapter.run_calibration(options=options)
        _audit("run_calibration", "executed", extra={"options": options})
        return result.to_dict()
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to run calibration: {exc}. Check that the printer is idle.")
    except Exception as exc:
        logger.exception("Unexpected error in run_calibration")
        return _error_dict(f"Unexpected error in run_calibration: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def emergency_stop(
    printer_name: str | None = None,
    reason: str = "user_request",
    source: str = "mcp",
    note: str | None = None,
) -> dict:
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
        reason: Reason code (e.g. ``user_request``, ``thermal_runaway``).
        source: Trigger source label for audit context.
        note: Optional operator note.
    """
    if err := _check_auth("print"):
        return err
    if err := _check_rate_limit("emergency_stop"):
        return err
    if conf := _check_confirmation("emergency_stop", {}):
        return conf
    try:
        from kiln.emergency import EmergencyReason, get_emergency_coordinator

        try:
            reason_enum = EmergencyReason(str(reason or "user_request").strip().lower())
        except ValueError:
            valid = ", ".join(r.value for r in EmergencyReason)
            return _error_dict(
                f"Invalid emergency reason {reason!r}. Valid reasons: {valid}.",
                code="INVALID_ARGS",
                retryable=False,
            )

        coord = get_emergency_coordinator()
        if printer_name:
            result = coord.emergency_stop(printer_name, reason=reason_enum, source=source, note=note)
            _audit("emergency_stop", f"executed for {printer_name}")
            return {"success": True, "emergency_stop": result.to_dict()}
        else:
            results = coord.emergency_stop_all(reason=reason_enum, source=source, note=note)
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
def emergency_status(printer_name: str | None = None, include_unlatched: bool = False) -> dict:
    """Get the emergency stop latch status for one printer or the entire fleet.

    Returns whether an emergency stop is active and whether the printer is
    locked from printing operations.  When an e-stop is active, all print
    commands are blocked until ``clear_emergency_stop()`` is called with an
    acknowledgement note.

    :param printer_name: Query a specific printer, or omit for all printers.
    :param include_unlatched: When True, include printers that have no active
        latch.  Defaults to False (only active latches).

    :returns: Latch state per printer: ``active`` (bool), ``reason``,
        ``source``, ``timestamp``, and whether critical interlocks prevent
        clearing.

    See also: ``emergency_stop()``, ``clear_emergency_stop()``.
    """
    if err := _check_auth("print"):
        return err
    try:
        from kiln.emergency import get_emergency_coordinator

        coord = get_emergency_coordinator()
        if printer_name:
            return {
                "success": True,
                "printer_name": printer_name,
                "emergency_status": coord.get_latch_status(printer_name),
            }
        rows = coord.list_latch_statuses(include_unlatched=include_unlatched)
        return {
            "success": True,
            "count": len(rows),
            "emergency_status": rows,
            "include_unlatched": include_unlatched,
        }
    except Exception as exc:
        logger.exception("Unexpected error in emergency_status")
        return _error_dict(f"Unexpected error in emergency_status: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def clear_emergency_stop(
    printer_name: str,
    acknowledgement_note: str,
    acknowledged_by: str = "operator",
) -> dict:
    """Acknowledge and clear a printer's emergency stop latch.

    This is a safety-critical operation.  The latch may be blocked from
    clearing if critical interlocks are still active (e.g. thermal sensor
    failure).  Call ``emergency_status()`` first to check whether clearing
    is possible.

    :param printer_name: Printer whose latch to clear.
    :param acknowledgement_note: Free-text note explaining why the e-stop is
        being cleared (required -- cannot be empty).
    :param acknowledged_by: Identity of the person or system clearing the
        latch (default ``"operator"``).

    :returns: Updated latch state, or an error if critical interlocks prevent
        clearing.

    See also: ``emergency_status()``, ``emergency_stop()``.
    """
    if err := _check_auth("print"):
        return err
    if conf := _check_confirmation(
        "clear_emergency_stop",
        {"printer_name": printer_name, "acknowledged_by": acknowledged_by},
    ):
        return conf
    if not (acknowledgement_note or "").strip():
        return _error_dict("acknowledgement_note is required.", code="INVALID_ARGS", retryable=False)
    try:
        from kiln.emergency import get_emergency_coordinator

        coord = get_emergency_coordinator()
        result = coord.clear_stop_with_ack(
            printer_name,
            acknowledged_by=acknowledged_by,
            ack_note=acknowledgement_note,
        )
        if not result.get("success"):
            reason = str(result.get("reason") or "")
            code = "E_STOP_CLEAR_BLOCKED" if reason == "critical_interlocks_pending" else "INVALID_STATE"
            payload = _error_dict(
                str(result.get("message") or "Failed to clear emergency latch."), code=code, retryable=False
            )
            payload["emergency_status"] = result.get("status")
            return payload
        _audit(
            "clear_emergency_stop",
            "executed",
            details={
                "printer_name": printer_name,
                "acknowledged_by": acknowledged_by,
            },
        )
        return {
            "success": True,
            "printer_name": printer_name,
            "cleared": True,
            "emergency_status": result.get("status"),
            "message": str(result.get("message") or "Emergency latch cleared."),
        }
    except Exception as exc:
        logger.exception("Unexpected error in clear_emergency_stop")
        return _error_dict(f"Unexpected error in clear_emergency_stop: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def force_print_oversize(printer_id: str = "", ttl_minutes: int = 5) -> dict:
    """Briefly override the pre-print impossibility gate for ONE printer.

    Kiln refuses a print that physically cannot succeed on the target
    printer — geometry that exceeds the build volume (the nozzle would
    crash) or a material whose minimum nozzle temperature exceeds the
    printer's hotend ceiling (it cannot melt the filament).  Those are
    hard physical limits, not warnings, so a normal print call is blocked.

    This is the human's "I understand — print it anyway" escape hatch, e.g.
    when you are deliberately sending the file to a *different* printer than
    the one connected.  It grants a short, per-printer override that lets the
    NEXT otherwise-blocked print through, then expires.

    Safety: classified ``confirm`` (see ``data/tool_safety.json``).  An
    autonomous agent cannot self-approve it — the confirmation layer keeps a
    human in the loop, exactly like ``emergency_stop``.  Designing and slicing
    any size is never blocked; only the final print-to-hardware step is.

    :param printer_id: Printer model to override (e.g. ``"bambu_a1"``).
        Empty resolves to the active printer.
    :param ttl_minutes: Minutes the override stays active (default 5, max 60).
    :returns: Grant confirmation, or a confirmation-required challenge.
    """
    if err := _check_auth("print"):
        return err
    if conf := _check_confirmation(
        "force_print_oversize",
        {"printer_id": printer_id, "ttl_minutes": ttl_minutes},
    ):
        return conf

    ttl = max(1, min(int(ttl_minutes or 5), 60))
    pid = (printer_id or "").strip()
    if not pid:
        try:
            from kiln.printer_model_resolver import resolve_printer_model

            pid = resolve_printer_model() or ""
        except Exception:
            logger.debug("force_print_oversize: could not resolve active model", exc_info=True)
            pid = ""

    try:
        from kiln.printers.print_gate import grant_oversize_override

        grant_oversize_override(pid, ttl_seconds=ttl * 60)
    except Exception as exc:
        return _error_dict(f"Could not grant override: {exc}", code="INTERNAL_ERROR")

    _audit(
        "force_print_oversize",
        "granted",
        details={"printer_id": pid, "ttl_minutes": ttl},
    )
    return {
        "status": "success",
        "printer_id": pid or "active printer",
        "expires_in_minutes": ttl,
        "message": (
            f"Override granted for {pid or 'the active printer'} for {ttl} "
            f"minute(s). The next print the safety gate would have blocked "
            f"(oversize or over-temp) will proceed. This action is logged."
        ),
    }


@mcp.tool()
def emergency_trip_input(
    printer_name: str,
    input_name: str = "external_button",
    token: str | None = None,
    note: str | None = None,
) -> dict:
    """Trip emergency stop from an external hardware bridge.

    Designed for physical input devices (ESP32, PLC, wired push buttons)
    that call this endpoint over HTTP to trigger a software e-stop.  This
    is different from ``emergency_stop()`` which is for agent/software-
    initiated stops.

    If ``KILN_ESTOP_INPUT_TOKEN`` is configured, the request must include
    a matching ``token`` or it will be rejected.

    :param printer_name: Printer to emergency-stop.
    :param input_name: Label for the input source (default
        ``"external_button"``).
    :param token: Authorization token -- required when
        ``KILN_ESTOP_INPUT_TOKEN`` is set.
    :param note: Optional free-text note describing the trigger reason.

    See also: ``emergency_stop()``, ``emergency_status()``.
    """
    if err := _check_auth("print"):
        return err
    if err := _check_rate_limit("emergency_trip_input"):
        return err
    if _ESTOP_INPUT_TOKEN and token != _ESTOP_INPUT_TOKEN:
        return _error_dict(
            "Invalid emergency input token.",
            code="AUTH_ERROR",
            retryable=False,
        )
    try:
        from kiln.emergency import EmergencyReason, get_emergency_coordinator

        source_label = f"input:{(input_name or 'external_button').strip() or 'external_button'}"
        coord = get_emergency_coordinator()
        record = coord.emergency_stop(
            printer_name,
            reason=EmergencyReason.USER_REQUEST,
            source=source_label,
            note=note,
        )
        _audit(
            "emergency_trip_input",
            "executed",
            details={
                "printer_name": printer_name,
                "input_name": input_name,
            },
        )
        return {
            "success": True,
            "printer_name": printer_name,
            "emergency_stop": record.to_dict(),
            "source": source_label,
        }
    except Exception as exc:
        logger.exception("Unexpected error in emergency_trip_input")
        return _error_dict(f"Unexpected error in emergency_trip_input: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def pause_print(keep_temps: bool = True) -> dict:
    """Pause the currently running print job.

    Pausing lifts the nozzle and parks the head.

    Heater behaviour during pause varies by firmware:

      - Bambu A1 / A1 mini: the firmware drops the **hotend** target
        ~3-5 minutes into a pause regardless of slicer settings (bed
        target survives).  An untreated 25-min pause cools the nozzle
        from 220°C to ~90°C, which means the resume can't extrude
        until you re-heat — and bed adhesion can fail in the meantime.
      - Bambu X1/P1 series: typically holds both targets, but a long
        idle can still trigger cooldown.
      - OctoPrint / Moonraker / Klipper: depends on firmware config;
        most hold targets across pause.

    To fight this, ``pause_print`` spawns a best-effort daemon thread
    that re-asserts the pre-pause hotend + bed targets every 2 minutes
    until the printer leaves the PAUSED state (resume, cancel, error,
    or manual button press).  This is enabled by default.

    Args:
        keep_temps: When ``True`` (default), capture the pre-pause tool
            and bed targets and re-assert them every ~2 minutes via a
            background daemon thread.  Set ``False`` to skip the
            keep-alive (legacy behaviour — printer may cool during long
            pauses).  The keep-alive thread is idempotent: repeat
            pause/resume cycles do not compound threads.

    Use ``resume_print()`` to continue from where the print left off.
    The keep-alive thread is automatically stopped on resume or cancel.
    """
    if err := _check_auth("print"):
        return err
    if err := _check_rate_limit("pause_print"):
        return err
    try:
        adapter = _get_adapter()

        # Snapshot pre-pause targets BEFORE issuing the pause command —
        # some firmwares clear them within seconds of the pause being
        # accepted.
        snapshot: dict[str, float] | None = None
        if keep_temps:
            try:
                state = adapter.get_state()
                snapshot = {
                    "tool": float(state.tool_temp_target or 0.0),
                    "bed": float(state.bed_temp_target or 0.0),
                }
            except Exception as exc:
                logger.debug("pause_print: state snapshot failed (%s); keep-alive disabled", exc)
                snapshot = None

        result = adapter.pause_print()

        # Start (or refresh) the keep-alive daemon.  Idempotent: if a
        # thread is already running from a previous pause that wasn't
        # cleanly resumed, the targets are refreshed in place.
        keepalive_started = False
        if keep_temps and snapshot is not None and (snapshot["tool"] > 0 or snapshot["bed"] > 0):
            try:
                keepalive_started = _pause_keepalive.start(
                    tool_target=snapshot["tool"],
                    bed_target=snapshot["bed"],
                )
            except Exception as exc:
                logger.warning("pause_print: failed to start keep-alive: %s", exc)

        out = result.to_dict()
        if keep_temps and snapshot is not None:
            out["keep_alive"] = {
                "active": _pause_keepalive.is_running(),
                "started_new_thread": keepalive_started,
                "interval_seconds": _PAUSE_KEEPALIVE_INTERVAL_S,
                "targets": snapshot,
            }
        return out
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to pause print: {exc}. Check that a print is currently active.")
    except Exception as exc:
        logger.exception("Unexpected error in pause_print")
        return _error_dict(f"Unexpected error in pause_print: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
@requires_tier(LicenseTier.PRO)
def skip_print_objects(object_ids: list[str], plate_number: int = 1) -> dict:
    """Abandon one or more failed objects on a multi-object plate, mid-print.

    When one part on a full plate fails — spaghetti, a knocked-loose object, a
    detached corner — this tells the printer to stop printing just those
    objects and finish the rest of the plate.  One bad part no longer scraps
    the whole run.  A Kiln Pro feature.

    The identifier is backend-specific — pass it as a string, Kiln routes it:

    * **Bambu** — the ``label_id`` from ``list_plate_objects`` (e.g. ``"757"``).
    * **Klipper / Moonraker / Creality** — the object NAME the slicer labelled
      (e.g. ``"Part1"``); the file must have been sliced with object labelling.
    * **OctoPrint** — the zero-based ``M486`` object index (needs firmware
      M486 support).

    Discover Bambu ids first::

        list_plate_objects("my_plate.gcode.3mf")   # -> objects[].label_id

    Printer support (honest): Bambu and any Klipper/Moonraker printer can skip
    (Voron, RatRig, Qidi, and Klipper-based Creality and Elegoo Neptune /
    OrangeStorm); Marlin printers via OctoPrint or direct USB can if the
    firmware speaks M486.  Prusa via Prusa Link can't be skipped remotely — an
    API limitation, not the printer (it can cancel objects from its own
    screen).  The Elegoo SDCP protocol (e.g. Centauri Carbon) has no skip
    command.  A Klipper printer on a non-Klipper connection just needs
    reconnecting as Moonraker.

    AGENT DISPLAY CONTRACT: skipping is IRREVERSIBLE for the objects named —
    confirm the exact objects with the user before calling, and only while a
    multi-object plate is actively printing.  Skips are cumulative: an object
    already skipped stays skipped.

    Args:
        object_ids: Backend-specific object identifiers to abandon (see above).
        plate_number: Which plate the ids came from (1-based, default 1).
            Recorded for context; the ids are what the printer acts on.

    Returns:
        Dict with the skipped objects and a confirmation message, or an error
        dict if no print is active or the printer can't skip objects.
    """
    if err := _check_auth("print"):
        return err
    if err := _check_rate_limit("skip_print_objects"):
        return err
    if not object_ids:
        return _error_dict(
            "skip_print_objects needs at least one object id. "
            "Run list_plate_objects() on the printing file to find them.",
            code="NO_OBJECTS",
        )
    try:
        adapter = _get_adapter()
        skip = getattr(adapter, "skip_objects", None)
        if not callable(skip):
            return _error_dict(
                "This printer connection can't skip individual objects "
                "mid-print. Kiln can skip on Bambu, any Klipper/Moonraker "
                "printer, and Marlin printers via OctoPrint or USB when the "
                "firmware speaks M486. If this is a Klipper-based printer "
                "(many Creality and Elegoo Neptune / OrangeStorm models) on a "
                "non-Klipper connection, reconnect it as Moonraker. Prusa via "
                "Prusa Link can't be skipped remotely (the API exposes no "
                "per-object control, though the printer can cancel objects "
                "from its screen); the Elegoo SDCP protocol (e.g. Centauri "
                "Carbon) has no skip command.",
                code="UNSUPPORTED",
            )
        # Pass identifiers through as-is — each adapter coerces to its native
        # type (Bambu/OctoPrint ints, Klipper object-name strings).
        skip(list(object_ids))
        return {
            "success": True,
            "skipped_objects": list(object_ids),
            "plate_number": plate_number,
            "message": (
                f"Asked the printer to skip {len(object_ids)} object(s): "
                f"{list(object_ids)}. The rest of the plate keeps printing."
            ),
        }
    except (PrinterError, RuntimeError) as exc:
        # The most common mistake is the wrong id TYPE for the backend — guide
        # to the right one instead of blaming the print state.
        if "integer" in str(exc).lower():
            return _error_dict(
                f"Couldn't skip: {exc} Bambu uses the numeric label_id from "
                "list_plate_objects; OctoPrint/USB use the numeric M486 index; "
                "Klipper/Moonraker uses the object NAME (a string).",
                code="BAD_OBJECT_ID",
            )
        return _error_dict(
            f"Couldn't skip those objects: {exc} "
            "(skipping only works during a live multi-object print).",
        )
    except Exception as exc:
        logger.exception("Unexpected error in skip_print_objects")
        return _error_dict(f"Unexpected error in skip_print_objects: {exc}", code="INTERNAL_ERROR")


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
    if block := _emergency_latch_error("resume_print", _resolve_effective_printer_name()):
        return block
    try:
        adapter = _get_adapter()
        result = adapter.resume_print()
        # Stop the pause keep-alive thread if one was running — the print
        # is back under firmware control and re-asserting targets here
        # would race with the resume preamble gcode.
        try:
            _pause_keepalive.stop()
        except Exception as exc:
            logger.debug("resume_print: keep-alive stop failed (best-effort): %s", exc)
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
    if block := _emergency_latch_error("set_temperature", _resolve_effective_printer_name()):
        tool_heating = tool_temp is not None and tool_temp > 0
        bed_heating = bed_temp is not None and bed_temp > 0
        # Allow heater-off/cooldown commands while latched.
        if tool_heating or bed_heating:
            return block

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
            _get_heater_watchdog().notify_heater_set()

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


# ---------------------------------------------------------------------------
# Bambu-specific tools (AMS, speed profiles, LED control)
# ---------------------------------------------------------------------------


@mcp.tool()
def ams_status() -> dict:
    """Full AMS hardware dump — all trays, humidity, RFID (Bambu Lab only).

    For just the currently-active material, use ``get_active_material``
    instead. For Kiln's software material tracker, use ``get_material``.

    Returns what's loaded in each AMS tray: filament type, color, remaining
    percentage, RFID tag, temperature ranges, and humidity.

    The ``tray_now`` field usually shows which tray is currently active
    (``"255"`` means none / external spool on X1/P1-style reports).  A1 /
    AMS Lite reports may keep ``tray_now`` at ``"255"`` while exposing
    loaded AMS trays and selected/target tray fields such as ``tray_pre``
    or ``tray_tar``.  The ``ams_exist_bits`` and ``tray_exist_bits`` fields
    are bitmasks showing which AMS units and trays are physically present.

    Use this to check filament levels before printing, verify the correct
    material is loaded, or select the right ``ams_mapping`` for
    ``start_print()``.
    """
    if err := _check_auth("read"):
        return err
    if err := _check_rate_limit("ams_status"):
        return err
    try:
        adapter = _get_adapter()
        if not hasattr(adapter, "get_ams_status"):
            return _error_dict(
                "AMS status is only available on Bambu Lab printers with AMS.",
                code="UNSUPPORTED",
            )
        result = adapter.get_ams_status()
        _audit("ams_status", "queried")
        return {"success": True, **result}
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to query AMS status: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error in ams_status")
        return _error_dict(f"Unexpected error in ams_status: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def cfs_status() -> dict:
    """Discover Creality CFS/CFS-C status through local Moonraker.

    This is the Creality counterpart to ``ams_status()``, but the public
    protocol is not equivalent to Bambu AMS. Creality documents CFS control
    through Creality Print and printer UI; Kiln therefore performs read-only
    Moonraker discovery (`/printer/objects/list`, candidate object queries,
    and `/printer/gcode/help`) and reports any visible CFS slots/macros.

    The response includes ``hardware_unverified=True`` and
    ``active_slot_control_supported=False`` until slot load/unload/mapping
    commands are validated against real Creality hardware or official API docs.
    """
    if err := _check_auth("read"):
        return err
    if err := _check_rate_limit("cfs_status"):
        return err
    try:
        adapter = _get_adapter()
        if not hasattr(adapter, "get_cfs_status"):
            return _error_dict(
                "CFS status is only available on Creality printers that expose CFS/CFS-C through local Moonraker.",
                code="UNSUPPORTED",
            )
        result = adapter.get_cfs_status()
        _audit("cfs_status", "queried")
        return {"success": True, **result}
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to query CFS status: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error in cfs_status")
        return _error_dict(f"Unexpected error in cfs_status: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def set_speed_profile(profile: str) -> dict:
    """Set the printer speed profile (Bambu Lab printers only).

    Args:
        profile: Speed profile name — one of ``"silent"`` (50% speed,
            quiet), ``"standard"`` (100%, default), ``"sport"`` (124%,
            faster), or ``"ludicrous"`` (166%, maximum speed).

    Sport and Ludicrous modes automatically increase nozzle temperature
    to prevent under-extrusion at higher flow rates.

    Use ``printer_status()`` to see the current speed profile in the
    response's ``printer.speed_profile`` field.
    """
    if err := _check_auth("printer_control"):
        return err
    if err := _check_rate_limit("set_speed_profile"):
        return err
    try:
        adapter = _get_adapter()
        if not hasattr(adapter, "set_speed_profile"):
            return _error_dict(
                "Speed profile control is only available on Bambu Lab printers.",
                code="UNSUPPORTED",
            )
        ok = adapter.set_speed_profile(profile)
        _audit("set_speed_profile", "executed", details={"profile": profile})
        return {
            "success": True,
            "profile": profile.strip().lower(),
            "accepted": ok,
        }
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to set speed profile: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error in set_speed_profile")
        return _error_dict(f"Unexpected error in set_speed_profile: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def get_speed_profile() -> dict:
    """Get the current speed profile (Bambu Lab printers only).

    Returns the active speed profile with:
    - ``level``: numeric level 1–4
    - ``name``: profile name — ``"silent"`` (50%), ``"standard"`` (100%),
      ``"sport"`` (124%), or ``"ludicrous"`` (166%)
    - ``speed_magnitude``: actual speed multiplier percentage reported by the
      printer firmware

    Use this to check the current speed before adjusting it with
    ``set_speed_profile()``.
    """
    if err := _check_auth("read"):
        return err
    if err := _check_rate_limit("get_speed_profile"):
        return err
    try:
        adapter = _get_adapter()
        if not hasattr(adapter, "get_speed_profile"):
            return _error_dict(
                "Speed profile is only available on Bambu Lab printers.",
                code="UNSUPPORTED",
            )
        result = adapter.get_speed_profile()
        _audit("get_speed_profile", "queried")
        return {"success": True, **result}
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to get speed profile: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error in get_speed_profile")
        return _error_dict(f"Unexpected error in get_speed_profile: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def set_printer_light(node: str = "chamber_light", mode: str = "on") -> dict:
    """Control the printer's LED lights (Bambu Lab printers only).

    Args:
        node: Which light to control — ``"chamber_light"`` (main
            illumination) or ``"work_light"`` (nozzle area).
            Defaults to ``"chamber_light"``.
        mode: Light mode — ``"on"``, ``"off"``, or ``"flashing"``.
            Defaults to ``"on"``.

    Use this to improve camera visibility, signal print completion
    (flashing), or turn lights off for overnight prints.
    """
    if err := _check_auth("printer_control"):
        return err
    if err := _check_rate_limit("set_printer_light"):
        return err
    try:
        adapter = _get_adapter()
        if not hasattr(adapter, "set_light"):
            return _error_dict(
                "Light control is only available on Bambu Lab printers.",
                code="UNSUPPORTED",
            )
        ok = adapter.set_light(node, mode)
        _audit("set_printer_light", "executed", details={"node": node, "mode": mode})
        return {
            "success": True,
            "node": node.strip().lower(),
            "mode": mode.strip().lower(),
            "accepted": ok,
        }
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to set printer light: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error in set_printer_light")
        return _error_dict(f"Unexpected error in set_printer_light: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def set_fan(node: str = "part", percent: int = 100) -> dict:
    """Set the speed of a printer fan.

    Supported on Bambu Lab, OctoPrint, Moonraker/Klipper printers, and
    Elegoo's Centauri Carbon (FDM). Prusa Link has no raw G-code endpoint, so
    fan control isn't available there
    (https://github.com/prusa3d/Prusa-Link/issues/832). Elegoo's resin/MSLA
    printers (Saturn, Mars) have no part-cooling fan and are refused.

    Args:
        node: Which fan to set. ``"part"`` (part-cooling / model fan, the
            one that cools each layer) works on every supported printer.
            ``"aux"`` (auxiliary / big fan) and ``"chamber"`` (chamber /
            exhaust fan) are Bambu-only — generic Marlin/Klipper firmware has
            no standard auxiliary or chamber fan Kiln can address without
            knowing that machine's own G-code macros. Defaults to ``"part"``.
        percent: Fan speed 0-100. ``0`` turns the fan off, ``100`` is full
            speed. Defaults to ``100``.

    Use this to add cooling for bridges and overhangs (part fan), or — on
    Bambu — pull heat with the auxiliary fan or run the chamber/exhaust fan
    for materials like ABS/ASA. The Bambu chamber fan only exists on
    enclosed models — X1 Carbon, X1E, P1S, P2S, H2S — not on open-frame
    models (A1, A1 Mini, A2L, P1P), where a chamber command is a no-op. The
    printer's own thermal management may override a manual fan speed during
    a print.
    """
    if err := _check_auth("printer_control"):
        return err
    if err := _check_rate_limit("set_fan"):
        return err
    try:
        adapter = _get_adapter()
        if not hasattr(adapter, "set_fan"):
            return _error_dict(
                "Fan control isn't available on this printer type.",
                code="UNSUPPORTED",
            )
        ok = adapter.set_fan(node, percent)
        _audit("set_fan", "executed", details={"node": node, "percent": percent})
        return {
            "success": True,
            "node": node.strip().lower(),
            "percent": int(percent),
            "accepted": ok,
        }
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to set fan: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error in set_fan")
        return _error_dict(f"Unexpected error in set_fan: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def wrap_gcode_as_3mf(
    gcode_path: str,
    hotend_temp: int = 220,
    bed_temp: int = 65,
    filament_type: str = "PLA",
    source_3mf_path: str | None = None,
    num_filaments: int = 1,
    filament_colors: list[str] | None = None,
    filament_types: list[str] | None = None,
    thumbnail_path: str | None = None,
    stl_path: str | None = None,
    resume_mode: bool = False,
) -> dict:
    """Wrap raw PrusaSlicer G-code in a Bambu-compatible 3MF (Bambu Lab only).

    Bambu printers require the proprietary BambuStudio start/end sequences
    (motor enable, AMS load, extrusion calibration) to function correctly.
    This tool takes PrusaSlicer G-code output and packages it into a 3MF
    that the printer will accept.

    Args:
        gcode_path: Absolute path to a PrusaSlicer ``.gcode`` file on the
            local filesystem.  The file must have been sliced with
            ``--use-relative-e-distances`` and empty start/end G-code.
        hotend_temp: Hotend temperature in °C (default 220 for PLA).
        bed_temp: Bed temperature in °C (default 65 for PLA).
        filament_type: Filament type string — ``"PLA"``, ``"PETG"``,
            ``"ABS"``, etc.
        source_3mf_path: Optional path to a source 3MF to copy
            thumbnails and geometry from.
        num_filaments: Number of filaments (>1 for multi-color prints).
        filament_colors: List of hex color strings per filament
            (e.g. ``["#898989FF", "#161616FF"]``).
        filament_types: List of filament type strings per filament
            (e.g. ``["PLA", "PLA"]``).
        thumbnail_path: Optional path to a PNG image to embed as the
            3MF thumbnail (shown on the printer's display).
        stl_path: Optional path to the source STL file.  When provided,
            a thumbnail is auto-generated from the model geometry via
            OpenSCAD (512x512, shown on the Bambu printer screen).

    Returns a dict with ``output_path`` pointing to the generated 3MF.
    Use ``upload_file()`` to send it to the printer, then ``start_print()``
    to begin printing.
    """
    if err := _check_auth("files"):
        return err
    if err := _check_rate_limit("wrap_gcode_as_3mf"):
        return err
    try:
        adapter = _get_adapter()
        if not hasattr(adapter, "wrap_gcode_as_3mf"):
            return _error_dict(
                "3MF wrapping is only available on Bambu Lab printers. "
                "Other printers accept G-code files directly via upload_file().",
                code="UNSUPPORTED",
            )
        stl_paths = [stl_path] if stl_path and os.path.isfile(stl_path) else None
        output_path = adapter.wrap_gcode_as_3mf(
            gcode_path,
            hotend_temp=hotend_temp,
            bed_temp=bed_temp,
            filament_type=filament_type,
            source_3mf_path=source_3mf_path,
            num_filaments=num_filaments,
            filament_colors=filament_colors,
            filament_types=filament_types,
            stl_paths=stl_paths,
            resume_mode=resume_mode,
        )
        # Inject thumbnail PNG if provided and not already in the 3MF.
        # Bambu printers read from Auxiliaries/.thumbnails/ — not just
        # Metadata/.  Each path expects a specific resolution matching
        # BambuStudio's output format.
        if thumbnail_path and os.path.isfile(thumbnail_path):
            import zipfile

            try:
                from io import BytesIO

                from PIL import Image

                src_img = Image.open(thumbnail_path)

                # BambuStudio thumbnail spec: path → (width, height)
                _THUMB_SPECS: dict[str, tuple[int, int]] = {
                    "Metadata/plate_1.png": (512, 512),
                    "Metadata/plate_1_small.png": (128, 128),
                    "Metadata/top_1.png": (512, 512),
                    "Metadata/pick_1.png": (512, 512),
                    "Auxiliaries/.thumbnails/thumbnail_3mf.png": (240, 180),
                    "Auxiliaries/.thumbnails/thumbnail_middle.png": (680, 510),
                    "Auxiliaries/.thumbnails/thumbnail_small.png": (251, 188),
                }

                with zipfile.ZipFile(output_path, "a") as zf:
                    existing = {n.lower() for n in zf.namelist()}
                    for thumb_name, (tw, th) in _THUMB_SPECS.items():
                        if thumb_name.lower() not in existing:
                            resized = src_img.resize((tw, th), Image.LANCZOS)
                            buf = BytesIO()
                            resized.save(buf, format="PNG")
                            zf.writestr(thumb_name, buf.getvalue())
            except ImportError:
                # Pillow not available — fall back to raw copy
                thumb_data = Path(thumbnail_path).read_bytes()
                with zipfile.ZipFile(output_path, "a") as zf:
                    existing = {n.lower() for n in zf.namelist()}
                    for thumb_name in (
                        "Metadata/plate_1.png",
                        "Metadata/top_1.png",
                        "Auxiliaries/.thumbnails/thumbnail_3mf.png",
                        "Auxiliaries/.thumbnails/thumbnail_middle.png",
                        "Auxiliaries/.thumbnails/thumbnail_small.png",
                    ):
                        if thumb_name.lower() not in existing:
                            zf.writestr(thumb_name, thumb_data)
        _audit("wrap_gcode_as_3mf", "executed", details={"gcode_path": gcode_path})
        return {
            "success": True,
            "output_path": output_path,
            "gcode_path": gcode_path,
            "filament_type": filament_type,
            "num_filaments": num_filaments,
        }
    except FileNotFoundError as exc:
        return _error_dict(f"G-code file not found: {exc}")
    except ValueError as exc:
        return _error_dict(f"Invalid G-code: {exc}")
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to wrap G-code as 3MF: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error in wrap_gcode_as_3mf")
        return _error_dict(f"Unexpected error in wrap_gcode_as_3mf: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Optional adapter tools (bed mesh, filament sensor, tool position)
# ---------------------------------------------------------------------------


@mcp.tool()
def get_bed_mesh() -> dict:
    """Get the bed mesh / probe data (OctoPrint and Moonraker only).

    Returns the probed bed leveling mesh including:
    - ``probed_matrix``: 2D array of Z-offset measurements across the bed
    - ``mesh_min`` / ``mesh_max``: bounding coordinates of the probed area
    - ``variance``: overall variance of the mesh (lower = flatter bed)

    Use this to diagnose first-layer adhesion issues.  High variance or
    significant dips/peaks indicate a warped bed or loose leveling screws.

    Not supported on Bambu Lab printers — Bambu handles bed leveling
    internally and does not expose mesh data.
    """
    if err := _check_auth("read"):
        return err
    if err := _check_rate_limit("get_bed_mesh"):
        return err
    try:
        adapter = _get_adapter()
        result = adapter.get_bed_mesh()
        if result is None:
            return _error_dict(
                "This printer does not support bed mesh data. "
                "Bambu printers handle leveling internally. "
                "OctoPrint and Moonraker printers expose mesh data after a G29 probe.",
                code="UNSUPPORTED",
            )
        _audit("get_bed_mesh", "queried")
        return {"success": True, **result}
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to get bed mesh: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error in get_bed_mesh")
        return _error_dict(f"Unexpected error in get_bed_mesh: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def get_filament_status() -> dict:
    """Get the filament runout sensor status (OctoPrint and Moonraker only).

    Returns sensor information including:
    - ``detected``: whether filament is currently detected by the sensor
    - ``sensor_enabled``: whether the runout sensor is active

    Use this to verify filament is loaded before starting a print on
    non-Bambu printers.

    For Bambu Lab printers, use ``ams_status()`` instead — it provides
    per-tray filament presence, type, color, and remaining percentage.
    """
    if err := _check_auth("read"):
        return err
    if err := _check_rate_limit("get_filament_status"):
        return err
    try:
        adapter = _get_adapter()
        result = adapter.get_filament_status()
        if result is None:
            return _error_dict(
                "This printer does not support filament sensor queries. "
                "For Bambu Lab printers, use ams_status() to check filament.",
                code="UNSUPPORTED",
            )
        _audit("get_filament_status", "queried")
        return {"success": True, **result}
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to get filament status: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error in get_filament_status")
        return _error_dict(f"Unexpected error in get_filament_status: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def get_tool_position() -> dict:
    """Get the current nozzle / tool-head XYZ position (Moonraker and Serial).

    Returns a dict with at least ``x``, ``y``, ``z`` coordinates in mm
    relative to the printer's home position.  Some printers also report
    ``e`` (extruder position).

    Use this for:
    - Verifying the printer has been homed (coordinates are valid only
      after homing)
    - Calibration sequences that need to know the current position
    - Move planning when issuing manual jog commands

    Not all adapters support this — returns an error if position data is
    not available.
    """
    if err := _check_auth("read"):
        return err
    if err := _check_rate_limit("get_tool_position"):
        return err
    try:
        adapter = _get_adapter()
        result = adapter.get_tool_position()
        if result is None:
            return _error_dict(
                "This printer does not support tool position queries. "
                "Try using printer_status() for general state information.",
                code="UNSUPPORTED",
            )
        _audit("get_tool_position", "queried")
        return {"success": True, "position": result}
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(f"Failed to get tool position: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error in get_tool_position")
        return _error_dict(f"Unexpected error in get_tool_position: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def preflight_check(
    file_path: str | None = None,
    expected_material: str | None = None,
    remote_file: str | None = None,
    accept_paused: bool = False,
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
        accept_paused: When ``True``, the ``printer_idle`` check accepts
            the ``paused`` state in addition to ``idle``.  Used by
            ``start_print(resume_from_paused=True)`` for mid-print
            resume 3MFs (which start from a paused-state printer).
            Default ``False`` — only ``idle`` is accepted.

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

        # Idle (not printing or in error).  Resume-mode 3MFs need to
        # start from a PAUSED printer (the original print was paused
        # by decorate_during_print before slicing the resume), so
        # callers may opt to also accept PAUSED here.
        idle_states = {PrinterStatus.IDLE}
        if accept_paused:
            idle_states.add(PrinterStatus.PAUSED)
        is_idle = state.state in idle_states
        msg_suffix = " (paused accepted for resume-mode start)" if accept_paused and state.state == PrinterStatus.PAUSED else ""
        checks.append(
            {
                "name": "printer_idle",
                "passed": is_idle,
                "message": f"Printer state: {state.state.value}{msg_suffix}",
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
                if _get_registry().count > 0:
                    names = _get_registry().list_names()
                    if names:
                        printer_name = names[0]
                warning = _get_material_tracker().check_match(printer_name, expected_material)
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

        # -- Moisture advisory (non-blocking) ------------------------------
        # Hygroscopic materials print rough/weak when the spool is wet.  This
        # is an advisory nudge, never a block — the user owns the call.  The
        # drying_advisor tool (kiln-pro, https://kiln3d.com) gives the safe
        # per-material drying recipe.
        if expected_material and any(
            tok in expected_material.lower() for tok in _HYGROSCOPIC_MATERIAL_HINTS
        ):
            checks.append(
                {
                    "name": "filament_moisture",
                    "passed": True,
                    "advisory": True,
                    "message": (
                        f"{expected_material.upper()} readily absorbs moisture. "
                        "If the spool has been open or stored in humid air, a wet "
                        "spool prints rough/weak — consider drying first. "
                        "drying_advisor (kiln-pro) gives the safe temp and time "
                        "for your material."
                    ),
                }
            )

        # -- Outcome history advisory (learning database) ------------------
        # Query past outcomes for this printer + material combo to warn
        # about historically problematic combinations.  Advisory only —
        # never blocks a print.
        try:
            _printer_name = None
            if _get_registry().count > 0:
                names = _get_registry().list_names()
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

            # -- Missing temperature check (warning, not blocking) ---------
            if file_ok and Path(file_path).suffix.lower() in _GCODE_EXTENSIONS:
                try:
                    from kiln.gcode import check_missing_temperatures

                    with open(file_path, errors="replace") as _fh:
                        gcode_content = _fh.read()
                    temp_warnings = check_missing_temperatures(gcode_content)
                    if temp_warnings:
                        checks.append(
                            {
                                "name": "temperature_commands",
                                "passed": True,  # Warning only -- does not block
                                "message": "; ".join(temp_warnings),
                                "advisory": True,
                            }
                        )
                except Exception as exc:
                    logger.debug("Missing temperature check failed: %s", exc)

        # -- SCAD flip-readability check (advisory) -------------------------
        # When a local file_path is provided, look for a companion .scad
        # source in the same directory.  If found, run the static analyzer
        # to catch backwards bottom-face text or too-shallow engravings
        # BEFORE the print starts — saving hours of wasted print time.
        if file_path is not None:
            try:
                from kiln.scad_verification import verify_flip_readability

                _scad_candidates = []
                _file_dir = Path(file_path).parent
                _stem = Path(file_path).stem.split(".")[0]  # handle model.gcode.3mf
                # Explicit stem match first, then any .scad in directory
                _stem_scad = _file_dir / f"{_stem}.scad"
                if _stem_scad.exists():
                    _scad_candidates.append(str(_stem_scad))
                else:
                    _scad_candidates.extend(
                        str(p) for p in sorted(_file_dir.glob("*.scad"))[:1]
                    )

                for _scad_path in _scad_candidates:
                    _flip_report = verify_flip_readability(_scad_path)
                    if _flip_report.get("issues"):
                        _flip_errors = _flip_report.get("errors", [])
                        _flip_warnings = _flip_report.get("warnings", [])
                        _msgs = [
                            f"[{i['severity'].upper()}] {i['message']}"
                            for i in _flip_report["issues"]
                        ]
                        checks.append(
                            {
                                "name": "scad_flip_readability",
                                "passed": not _flip_errors,
                                "message": "; ".join(_msgs),
                                "advisory": not _flip_errors,
                                "scad_path": _scad_path,
                            }
                        )
                        if _flip_errors:
                            errors.append(
                                "SCAD verification: bottom-face text may print reversed. "
                                "See scad_flip_readability check details."
                            )
                    elif _flip_report.get("text_entries_checked", 0) > 0:
                        checks.append(
                            {
                                "name": "scad_flip_readability",
                                "passed": True,
                                "message": (
                                    f"SCAD flip-readability verified — "
                                    f"{_flip_report['text_entries_checked']} text entries checked, "
                                    f"all correctly mirrored for flip-reading."
                                ),
                            }
                        )
            except ImportError:
                pass  # scad_verification not available — skip silently
            except Exception as exc:
                logger.debug("SCAD flip-readability check failed: %s", exc)

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

        # -- Cost estimate (advisory) --------------------------------------
        cost_estimate: dict[str, Any] | None = None
        _cost_time_s: int | float | None = None

        # 1) Try remote file metadata (has estimated_time_seconds)
        if remote_file is not None:
            try:
                for pf in adapter.list_files():
                    if pf.name.lower() == remote_file.lower() or pf.path.lower() == remote_file.lower():
                        if pf.estimated_time_seconds:
                            _cost_time_s = pf.estimated_time_seconds
                        break
            except Exception:
                pass  # Already handled in remote file check above

        # 2) Try local gcode file metadata (parse estimated time)
        if _cost_time_s is None and file_path is not None:
            try:
                _gcode_ext = {".gcode", ".gco", ".g", ".3mf"}
                if Path(file_path).suffix.lower() in _gcode_ext:
                    with open(file_path, errors="replace") as _fh:
                        # Read first 200 lines for slicer time estimates
                        for _i, _line in enumerate(_fh):
                            if _i > 200:
                                break
                            _line_lower = _line.lower()
                            # PrusaSlicer/OrcaSlicer: ; estimated printing time
                            if "estimated printing time" in _line_lower:
                                _time_match = re.findall(r"(\d+)\s*h", _line)
                                _min_match = re.findall(r"(\d+)\s*m", _line)
                                _sec_match = re.findall(r"(\d+)\s*s", _line)
                                _cost_time_s = (
                                    sum(int(h) * 3600 for h in _time_match)
                                    + sum(int(m) * 60 for m in _min_match)
                                    + sum(int(s) for s in _sec_match)
                                )
                                break
                            # Cura: ;TIME:seconds
                            if _line.startswith(";TIME:"):
                                _cost_time_s = float(_line.split(":")[1].strip())
                                break
            except Exception as exc:
                logger.debug("Cost estimate gcode parse failed: %s", exc)

        if _cost_time_s is not None and _cost_time_s > 0:
            cost_estimate = _estimate_print_cost(_cost_time_s, 0, material=expected_material)

        # -- Brand filament compatibility (advisory) ----------------------
        if expected_material is not None:
            try:
                from kiln.design_intelligence import resolve_filament

                resolved = resolve_filament(
                    expected_material,
                    printer_id=_PRINTER_MODEL,
                )
                if resolved.warnings:
                    for w in resolved.warnings:
                        # Skip the generic "pass a brand ID" hint
                        if "Generic material profile" in w:
                            continue
                        checks.append(
                            {
                                "name": "filament_compatibility",
                                "passed": True,  # Advisory — never blocks
                                "message": w,
                                "advisory": True,
                            }
                        )
            except Exception as exc:
                logger.debug("Brand filament compat check skipped: %s", exc)

        # -- Nozzle capacity check (advisory) ------------------------------
        # When kiln-pro is installed and the active printer has a
        # confirmed nozzle state, project the planned print against the
        # nozzle's lifetime envelope.  Free-tier installs without
        # kiln-pro silently skip this check (bridge.available() returns
        # False).  When the verdict surfaces, it joins the checks list
        # as advisory — never blocks ready=True on its own.  The user
        # decides whether to swap the nozzle or proceed.
        try:
            from kiln import _pro_nozzle_bridge

            _planned_grams = 0.0
            if file_result is not None:
                _planned_grams = float(file_result.get("filament_grams") or 0)
            _printer_id = ""
            if _get_registry().count > 0:
                _names = _get_registry().list_names()
                if _names:
                    _printer_id = _names[0]
            if _printer_id and _planned_grams > 0:
                _nozzle_verdict = _pro_nozzle_bridge.consult_capacity(
                    printer_id=_printer_id,
                    planned_grams=_planned_grams,
                    filament_material=expected_material or "",
                )
                if _nozzle_verdict is not None and _nozzle_verdict.get("status") not in (None, "unknown_baseline", "unknown_nozzle", "invalid_input"):
                    checks.append(
                        {
                            "name": "nozzle_capacity",
                            "passed": _nozzle_verdict["status"] != "exceeded_p90",
                            "message": _nozzle_verdict.get("narrative", ""),
                            "advisory": True,
                        }
                    )
        except Exception as exc:
            logger.debug("Nozzle capacity check skipped: %s", exc)

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
        if cost_estimate is not None:
            result["estimated_cost"] = cost_estimate

        from kiln.safety_gap_warning import attach_safety_warning
        result = attach_safety_warning(result)

        # Attach a FULL inspection bundle (rendered views + section +
        # measurements + printability) to the response so agents render
        # the mesh inline before the user commits filament + hours.
        # Preflight is the conversion moment — the user is about to
        # spend $5-30 of material and 2-12 hours of print time.  Visual
        # confirmation of what they're committing to is core to the
        # tool's purpose.  Helper is failure-closed: a render failure
        # decorates the response with an envelope flag but cannot
        # disrupt the preflight check itself.  Free-tier installs
        # (no kiln-pro) fall through to the bare result unchanged.
        try:
            from kiln_pro.plugins.git_render_tools import (
                attach_inspect_bundle,
            )

            return attach_inspect_bundle(result, level="full")
        except ImportError:
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
    if not dry_run and (block := _emergency_latch_error("send_gcode", _resolve_effective_printer_name())):
        return block
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
        # Use the live resolver so users who haven't manually set
        # _PRINTER_MODEL (via env or printer_model yaml field) still get
        # the bounds/temperature checks.
        _live_model = _resolve_printer_model_live() or _PRINTER_MODEL
        if _live_model:
            validation = validate_gcode_for_printer(cmd_list, _live_model)
        else:
            validation = _validate_gcode_impl(cmd_list)

        # Incident #0 hardening: promote bounds-violation warnings to
        # blockers.  Before this, "G1 X-50 Y-50" passed through with a
        # build-volume warning but executed anyway, risking nozzle
        # crash into the printer frame.  Negative/out-of-bounds X/Y
        # moves are now refused unless KILN_SKIP_BOUNDS_CHECK=1.
        _skip_bounds = os.environ.get("KILN_SKIP_BOUNDS_CHECK", "").strip() in (
            "1", "true", "yes",
        )
        if not _skip_bounds:
            bounds_warnings = [
                w for w in validation.warnings
                if " is outside " in w and "build volume" in w
            ]
            if bounds_warnings:
                _audit(
                    "send_gcode",
                    "bounds_blocked",
                    details={"bounds_warnings": bounds_warnings[:5]},
                )
                _record_tool_block("send_gcode")
                return {
                    "success": False,
                    "error": {
                        "code": "GCODE_OUT_OF_BOUNDS",
                        "message": (
                            f"Refused to send G-code: moves outside the "
                            f"printer's build volume would drive the nozzle "
                            f"into the printer frame.  Offending commands: "
                            f"{'; '.join(bounds_warnings[:3])}. "
                            f"Bypass with KILN_SKIP_BOUNDS_CHECK=1 for "
                            f"advanced use."
                        ),
                    },
                    "bounds_violations": bounds_warnings,
                }

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


# validate_gcode — moved to plugins/gcode_validation_tools.py

# safety_audit — moved to plugins/safety_tools.py

# get_session_log — moved to plugins/utility_tools.py


# ---------------------------------------------------------------------------
# Preview confirmation token issuance
# ---------------------------------------------------------------------------


@mcp.tool()
def issue_preview_token(
    file_path: str,
    printer_id: str | None = None,
    ttl_seconds: int = 600,
) -> dict:
    """Issue a preview-confirmation token for a file about to be printed.

    Call this AFTER rendering a preview (``visualize_model`` /
    ``preview_generated_model``) and showing it to the user.  The user
    approves → you call this tool → you pass the returned token as
    ``preview_token`` to ``start_print`` or ``fulfillment_order``.

    Without a valid token, ``start_print`` refuses to execute (unless
    ``KILN_SKIP_PREVIEW_GATE=1``).  This is the deepest safety gate
    that prevents an agent from sending a print to the physical printer
    without the user ever seeing what's about to be printed.

    Tokens are single-use and expire after ``ttl_seconds`` (default 600
    seconds / 10 minutes).  Scoped to the specific file hash and
    optionally to a specific printer_id so a token for one file can't
    be reused to approve a different file.

    Args:
        file_path: Path to the file to be printed (STL, 3MF, or .gcode).
            Hashed to bind the token to specific bytes.  If the file
            changes between issuing and using the token, the token is
            rejected.
        printer_id: Optional printer model ID to scope the token to a
            specific printer.  When set, using the token with a different
            printer will be rejected.
        ttl_seconds: Lifetime of the token (default 600).

    Returns:
        Dict with ``token`` and ``expires_at`` (unix timestamp).
    """
    if err := _check_auth("print"):
        return err
    try:
        from kiln.preview_gate import get_preview_gate
        t = get_preview_gate().issue(
            file_path, printer_id=printer_id, ttl_seconds=ttl_seconds,
        )
        return {
            "success": True,
            "token": t.token,
            "file_hash": t.file_hash,
            "expires_at": t.issued_at + t.ttl_seconds,
            "ttl_seconds": ttl_seconds,
            "usage_hint": (
                "Pass this token as preview_token=<token> to start_print or fulfillment_order. "
                "Single-use, expires in ~10 minutes."
            ),
        }
    except Exception as exc:
        return _error_dict(f"Failed to issue preview token: {exc}")


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
    if err := _check_auth("write"):
        return err

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


# safety_status — moved to plugins/safety_tools.py


# ---------------------------------------------------------------------------
# Fleet management tools
# ---------------------------------------------------------------------------


@mcp.tool()
@requires_tier(LicenseTier.PRO)
def fleet_status() -> dict:
    """Get live status of all fleet printers (state, temps, connection — current snapshot).

    For historical analytics (success rates, throughput), use ``fleet_analytics``.
    For grouping by physical location, use ``fleet_status_by_site``.
    Returns a list of printer snapshots including name, backend type,
    connection status, operational state, and temperatures.  Printers
    that fail to respond are reported as offline rather than raising.

    If no printers are registered yet, the current adapter (from env config)
    is auto-registered as "default".
    """
    try:
        # Auto-register the env-configured adapter if registry is empty
        if _get_registry().count == 0:
            try:
                adapter = _get_adapter()
                _get_registry().register("default", adapter)
            except RuntimeError:
                pass  # No adapter configured

        if _get_registry().count == 0:
            return {
                "success": True,
                "printers": [],
                "count": 0,
                "message": "No printers registered.",
            }

        status = _get_registry().get_fleet_status()
        idle = _get_registry().get_idle_printers()
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


# fleet_analytics — extracted to plugins/fleet_tools.py


@mcp.tool()
def register_printer(
    name: str,
    printer_type: str,
    host: str,
    api_key: str | None = None,
    serial: str | None = None,
    verify_ssl: bool = True,
    printer_model: str | None = None,
    persist: bool = True,
    verify_connection: bool = True,
    baudrate: int | None = None,
) -> dict:
    """Register a new printer in the fleet.

    Free and Pro each allow 1 printer. Fleet starts at Business (3 printers
    included, $15/mo per additional to a cap of 50); Enterprise is uncapped.

    Args:
        name: Unique human-readable name (e.g. "voron-350", "bambu-x1c").
        printer_type: Backend type -- "octoprint", "moonraker", "bambu",
            "creality", "elegoo", "prusalink", "duet", or "usb".
            "serial" is accepted as a legacy alias for "usb".
        host: Base URL or IP address of the printer.  For USB printers,
            this is the port path (e.g. "/dev/ttyUSB0", "COM3").
        api_key: API key (required for OctoPrint and Bambu, optional for
            Moonraker/Creality, unused for USB).  For Bambu printers
            this is the LAN Access Code.
        serial: Printer serial number (required for Bambu printers).
        verify_ssl: Whether to verify SSL certificates (default True).
            Set to False for printers using self-signed certificates.
            For Bambu, True maps to TLS pin mode and False maps to
            insecure mode.
        printer_model: Optional safety/profile key (e.g. "k1_max").
        persist: Save the printer to ``~/.kiln/config.yaml`` so future MCP
            sessions load the same printer. Default ``True``.
        verify_connection: For Bambu printers, immediately query AMS status
            after registration and return a proof summary. Default ``True``.
        baudrate: Baud rate for USB printers.  Defaults to
            ``DEFAULT_SERIAL_BAUDRATE``; many Marlin boards are flashed
            for 250000 and will not talk at the default.

    Once registered the printer appears in ``fleet_status()`` and can be
    targeted by ``submit_job()``.
    """
    if err := _check_auth("admin"):
        return err
    try:
        # Tier-aware printer cap, read from the licensing constants rather
        # than restated here: Free and Pro are 1, Business 50, Enterprise
        # uncapped.  Replacing an existing printer doesn't count against the
        # limit (only NEW registrations do).
        current_tier = get_tier()
        tier_cap = max_printers_for_tier(current_tier)
        tier_label = str(
            getattr(current_tier, "value", current_tier) or "free"
        ).title()
        # Registering a printer is FREE at every tier — what the fleet tier
        # sells is running printers in PARALLEL, and that is enforced at
        # print start (``printers/print_gate._concurrent_fleet_verdict``),
        # the one chokepoint every entry point reaches.  Refusing the
        # registration here instead was both too strict and too loose: it
        # blocked a user who simply owns two machines and uses them one at
        # a time, while `kiln config add-printer` and config.yaml auto-load
        # registered as many printers as they liked, uncapped (2026-07-27).
        #
        # tier-claims: business — the note names the tier being RECOMMENDED.
        fleet_note: str | None = None
        if (
            tier_cap is not None
            and name not in _get_registry()
            and _get_registry().count >= tier_cap
        ):
            fleet_note = (
                f"Registered. On the {tier_label} tier Kiln runs "
                f"{tier_cap} printer at a time — this machine is ready to "
                "use whenever your others are idle. Kiln Business runs "
                "printers in parallel (3 included, up to 50): "
                "https://kiln3d.com/pricing"
            )
        # Agents call this with whatever the docs they read said, and older
        # docs say "serial".  Normalize before anything branches on it, so
        # the tool honours the same aliases config.yaml and the env var do.
        printer_type = _normalize_printer_type(printer_type)

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
        elif printer_type == "duet":
            adapter = DuetAdapter(
                host=host,
                verify_ssl=verify_ssl,
                **({"password": api_key} if api_key else {}),
            )
        elif printer_type == "creality":
            adapter = CrealityAdapter(
                host=host,
                api_key=api_key or None,
                model=printer_model or None,
                verify_ssl=verify_ssl,
            )
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
                # Like the Creality branch: the declared model is the
                # adapter's identity too, not just a safety-profile hint —
                # bed-aware planners read it back off the registry.
                printer_model=printer_model or None,
                tls_mode="pin" if verify_ssl else "insecure",
            )
        elif printer_type == "elegoo":
            if ElegooAdapter is None:
                return _error_dict(
                    "Elegoo SDCP support requires websocket-client.  Install it with: pip install websocket-client",
                    code="MISSING_DEPENDENCY",
                )
            adapter = ElegooAdapter(
                host=host,
                mainboard_id=serial or "",
            )
        elif printer_type == "prusalink":
            adapter = PrusaLinkAdapter(host=host, api_key=api_key or None)
        elif printer_type == "usb":
            # For USB printers, 'host' is the serial port path (e.g.
            # /dev/ttyUSB0) and 'api_key' is unused.
            adapter = SerialPrinterAdapter(
                port=host,
                baudrate=baudrate or DEFAULT_SERIAL_BAUDRATE,
            )
        else:
            return _error_dict(
                f"Unsupported printer_type: {printer_type!r}. "
                f"Supported: {format_printer_types()}.",
                code="INVALID_ARGS",
            )

        if printer_model:
            adapter.set_safety_profile(printer_model)

        warnings_out: list[str] = list(url_warnings)
        persisted_path: str | None = None
        if persist:
            try:
                persisted = save_printer(
                    name,
                    printer_type,
                    host,
                    api_key=api_key if printer_type != "bambu" else None,
                    access_code=api_key if printer_type == "bambu" else None,
                    serial=serial,
                    printer_model=printer_model,
                    baudrate=baudrate,
                    set_active=True,
                )
                persisted_path = str(persisted)
            except Exception as exc:
                warnings_out.append(f"Printer registered for this session but could not be saved: {exc}")

        # Disconnect the boot-time default adapter if it's a separate
        # object not managed by the registry.  The registry's own
        # register() handles disconnecting its old adapter.
        global _adapter  # noqa: PLW0603
        old_default = _adapter
        _adapter = adapter  # Update immediately so tools use the new one.

        _get_registry().register(name, adapter)

        # If the boot-time adapter was a different object (not in the
        # registry), disconnect it to stop orphaned MQTT threads.
        if old_default is not None and old_default is not adapter:
            in_registry = any(old_default is a for a in _get_registry().list_all().values())
            if not in_registry:
                _disc = getattr(old_default, "disconnect", None)
                if _disc is not None:
                    try:
                        _disc()
                    except Exception:
                        logger.debug("Failed to disconnect old default adapter", exc_info=True)

        result = {
            "success": True,
            "message": f"Registered printer {name!r} ({printer_type} @ {host}).",
            "name": name,
        }
        if fleet_note:
            result["fleet_note"] = fleet_note
            result["upgrade_url"] = "https://kiln3d.com/pricing"
        if persisted_path:
            result["persisted"] = True
            result["config_path"] = persisted_path

        if printer_type == "bambu" and verify_connection:
            try:
                ams_info = adapter.get_ams_status()
                loaded_trays: list[dict[str, Any]] = []
                for unit in ams_info.get("units", []):
                    if not isinstance(unit, dict):
                        continue
                    for tray in unit.get("trays", []):
                        if not isinstance(tray, dict):
                            continue
                        tray_type = str(tray.get("tray_type", "") or "").strip()
                        if not tray_type:
                            continue
                        loaded_trays.append({
                            "slot": tray.get("slot"),
                            "tray_type": tray_type,
                            "tray_color": tray.get("tray_color"),
                        })
                result["bambu_ready"] = True
                result["ams_summary"] = {
                    "units": len(ams_info.get("units", [])),
                    "loaded_tray_count": len(loaded_trays),
                    "loaded_trays": loaded_trays,
                    "tray_now": ams_info.get("tray_now"),
                    "tray_pre": ams_info.get("tray_pre"),
                    "tray_tar": ams_info.get("tray_tar"),
                }
            except Exception as exc:
                result["bambu_ready"] = False
                result["ams_summary"] = {
                    "error": str(exc),
                    "retryable": True,
                }
                warnings_out.append(
                    "Bambu printer was registered, but the live AMS verification failed. "
                    "Check LAN mode, host, serial, and access code, then call ams_status()."
                )

        if warnings_out:
            result["warnings"] = warnings_out
        return result
    except Exception as exc:
        logger.exception("Unexpected error in register_printer")
        return _error_dict(f"Unexpected error in register_printer: {exc}", code="INTERNAL_ERROR")


# discover_printers — extracted to plugins/printer_management_tools.py


# list_fleet_sites, fleet_status_by_site, update_printer_site
# — extracted to plugins/fleet_tools.py


# ---------------------------------------------------------------------------
# Per-project cost tracking tools (Enterprise)
# ---------------------------------------------------------------------------


@mcp.tool()
@requires_tier(LicenseTier.ENTERPRISE)
def create_project(
    name: str,
    client: str,
    description: str = "",
    budget: float | None = None,
) -> dict:
    """Create a project for cost tracking.

    Manufacturing bureaus use projects to allocate printer time, material
    costs, and fulfillment fees to specific client engagements.

    Args:
        name: Project name (e.g. ``"Widget Batch 42"``).
        client: Client or cost-center identifier.
        description: Optional project description.
        budget: Optional budget cap in the configured currency.

    Requires Enterprise license.
    """
    if err := _check_auth("write"):
        return err
    try:
        from kiln.project_costs import get_project_cost_tracker

        tracker = get_project_cost_tracker()
        info = tracker.create_project(name=name, client=client, description=description, budget=budget)
        return {"success": True, "project": info.to_dict(), "message": f"Project {name!r} created."}
    except Exception as exc:
        logger.exception("Unexpected error in create_project")
        return _error_dict(f"Unexpected error in create_project: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
@requires_tier(LicenseTier.ENTERPRISE)
def log_project_cost(
    project_id: str,
    category: str,
    amount: float,
    description: str = "",
    printer_name: str | None = None,
    job_id: str | None = None,
) -> dict:
    """Log a cost entry against a project.

    Args:
        project_id: The project ID returned by ``create_project``.
        category: Cost category — ``"material"``, ``"printer_time"``,
            ``"fulfillment_fee"``, ``"labor"``, or ``"other"``.
        amount: Cost amount in the configured currency.
        description: What this cost entry is for.
        printer_name: Optional printer that incurred the cost.
        job_id: Optional job ID for traceability.

    Requires Enterprise license.
    """
    if err := _check_auth("write"):
        return err
    try:
        from kiln.project_costs import get_project_cost_tracker

        tracker = get_project_cost_tracker()
        entry = tracker.log_cost(
            project_id=project_id,
            category=category,
            amount=amount,
            description=description,
            printer_name=printer_name,
            job_id=job_id,
        )
        return {"success": True, "entry": entry.to_dict(), "message": "Cost logged."}
    except ValueError as exc:
        return _error_dict(str(exc), code="INVALID_ARGS")
    except Exception as exc:
        logger.exception("Unexpected error in log_project_cost")
        return _error_dict(f"Unexpected error in log_project_cost: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
@requires_tier(LicenseTier.ENTERPRISE)
def project_cost_summary(project_id: str) -> dict:
    """Get cost breakdown for a project.

    Returns total costs, per-category breakdown, and budget utilization
    for a given project.

    Args:
        project_id: The project ID returned by ``create_project``.

    Requires Enterprise license.
    """
    try:
        from kiln.project_costs import get_project_cost_tracker

        tracker = get_project_cost_tracker()
        summary = tracker.project_summary(project_id)
        return {"success": True, "summary": summary.to_dict()}
    except ValueError as exc:
        return _error_dict(str(exc), code="NOT_FOUND")
    except Exception as exc:
        logger.exception("Unexpected error in project_cost_summary")
        return _error_dict(f"Unexpected error in project_cost_summary: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
@requires_tier(LicenseTier.ENTERPRISE)
def client_cost_report(client: str) -> dict:
    """Get a cost report for all projects belonging to a client.

    Aggregates costs across all projects for a given client identifier,
    useful for invoicing and chargeback.

    Args:
        client: Client or cost-center identifier.

    Requires Enterprise license.
    """
    try:
        from kiln.project_costs import get_project_cost_tracker

        tracker = get_project_cost_tracker()
        report = tracker.client_summary(client)
        return {"success": True, "client": client, "report": report}
    except Exception as exc:
        logger.exception("Unexpected error in client_cost_report")
        return _error_dict(f"Unexpected error in client_cost_report: {exc}", code="INTERNAL_ERROR")


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
        events = _get_event_bus().recent_events(
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


# Billing tools -- removed from public repo (proprietary, lives in kiln-pro).
# Stub tools registered via pro_tool_manifest.json.


# ---------------------------------------------------------------------------
# License tools
# ---------------------------------------------------------------------------


def _annotate_session_liveness(payload: dict) -> None:
    """Tell the truth when the tier came from a sign-in that has lapsed.

    A tier resolved from a paired sign-in IS that session: once the
    session can no longer be refreshed, every hosted call fails.  A
    status tool that still answers "valid" then sends the user off to
    debug the feature when the whole fix is signing in again — which is
    exactly how an hour went on 2026-07-29, chasing a "broken" cloud
    push while this tool reported a valid Enterprise licence.

    Only the OAuth case is annotated.  An operator-supplied key in the
    environment or ``~/.kiln/license`` does not depend on a session and
    must keep reporting precisely what it reported before.

    Entitlement is untouched: tier decisions route through
    ``check_tier`` / ``check_pro``, never through this report, so this
    can only change what a human is TOLD, never what they may do.
    """
    if payload.get("source") != "oauth":
        return
    try:
        from kiln.auth_session import resolve_session_bearer

        session = resolve_session_bearer()
    except Exception:  # noqa: BLE001 — a diagnostic must never break the report
        return
    payload["session_state"] = session.state
    # Usability is the EMPTY TOKEN, not a list of state names.
    # ``SessionBearer`` documents that the token is empty only for
    # ``needs_signin`` / ``signed_out``; ``refreshed`` (just renewed) and
    # ``degraded`` (serving on offline grace) are working sessions.
    # Reading it this way also means a state added later is judged by
    # whether it can actually authenticate, not by whether someone
    # remembered to add it here — the first draft tested
    # ``state == "live"`` and declared a freshly refreshed session
    # invalid, which is the same false alarm in the other direction.
    if session.token:
        return
    # The tier stays reported — it is what the account holds once the
    # user signs back in.  Validity is the part that actually changed.
    payload["is_valid"] = False
    # ``action_required`` is read by a person looking at their own licence
    # status, so it names the situation and not the command.  The command
    # rides alongside for the agent, which can just do it.
    payload["action_required"] = session.detail or session_expired_message()
    payload.update(signin_hint_fields())


@mcp.tool()
def license_status() -> dict:
    """Get the current license tier, validity, and key details.

    Returns the active tier (free/pro/business), whether the license is
    valid, expiration date, and how it was resolved (env/file/default).
    No authentication required.

    When the tier comes from a sign-in session (``source`` is
    ``"oauth"``), the answer also carries ``session_state``. If that
    session has lapsed, ``is_valid`` is ``false`` and
    ``action_required`` says how to fix it — surface that line to the
    user verbatim, because every hosted call will fail until they do.
    """
    try:
        from kiln.licensing import get_license_manager

        info = get_license_manager().get_info()
        payload = {"success": True, **info.to_dict()}
        _annotate_session_liveness(payload)
        return payload
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
    if err := _check_auth("admin"):
        return err
    if not key or not key.strip():
        return _error_dict("License key is required.", code="INVALID_INPUT")
    try:
        from kiln.licensing import get_license_manager

        info = get_license_manager().activate_license(key.strip())
        return {"success": True, **info.to_dict()}
    except Exception as exc:
        logger.exception("Unexpected error in activate_license")
        return _error_dict(f"Failed to activate license: {exc}", code="LICENSE_ERROR")


# ---------------------------------------------------------------------------
# Tax + donation tools — moved to plugins/consumer_tools.py
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Multi-marketplace search tools — moved to plugins/marketplace_tools.py
# ---------------------------------------------------------------------------


# health_check — moved to plugins/utility_tools.py


@mcp.tool()
def restart_server(clean_env: bool = True) -> dict:
    """Restart the Kiln MCP server process in-place.

    Replaces the current process with a fresh instance using
    ``os.execve``.  The MCP client (Claude Code, etc.) should detect
    the connection drop and automatically reconnect, picking up any
    code changes made since the last startup.

    Use after installing or updating kiln-pro plugins, changing
    environment variables, or modifying server code — avoids the
    need to fully restart the MCP client application.

    :param clean_env: When ``True`` (default), strips ``KILN_PRINTER_*``
        environment variables from the child process if
        ``~/.kiln/config.yaml`` has a printer configured.  This defeats
        the "ghost env" footgun where a stale ``KILN_PRINTER_API_KEY``
        inherited from a past shell session silently shadows config.yaml
        edits for the lifetime of the MCP parent process.  Without this,
        every edit to config.yaml looks like it does nothing and the
        printer rejects MQTT auth with no hint why.  Set to ``False`` to
        preserve the full env (useful for CI or pure env-driven workflows
        where config.yaml is absent or deliberately overridden).
    :returns: Confirmation that the restart is imminent, plus the list
        of env vars that were stripped (for debugging transparency).
        The connection will drop within ~0.5 seconds.
    """
    import threading

    new_env = os.environ.copy()
    stripped: list[str] = []
    if clean_env:
        # Only strip when config.yaml has a printer — otherwise the env
        # is the sole source of truth and stripping would break the server.
        has_yaml_printer = False
        try:
            from kiln.cli.config import load_printer_config

            cfg = load_printer_config()
            has_yaml_printer = bool(cfg.get("host"))
        except Exception:  # noqa: BLE001 — never fail restart on config read
            has_yaml_printer = False

        if has_yaml_printer:
            for key in list(new_env):
                if key.startswith("KILN_PRINTER_"):
                    stripped.append(key)
                    del new_env[key]

    def _do_restart() -> None:
        # Bumped from 0.3 -> 0.5 so the tool response has more headroom
        # to round-trip back to the client before stdio gets exec'd over.
        time.sleep(0.5)
        if stripped:
            logger.info(
                "Kiln MCP server restarting; stripped %d stale env var(s) so "
                "config.yaml wins: %s",
                len(stripped),
                ", ".join(sorted(stripped)),
            )
        else:
            logger.info("Kiln MCP server restarting via restart_server tool...")
        # Hard-flush outbound framing and drain any queued inbound MCP
        # frames so the fresh process does not inherit stale JSON-RPC bytes.
        drained = _flush_restart_stdio()
        if drained:
            logger.info(
                "Kiln MCP server restart drained %d queued stdin byte(s) before exec.",
                drained,
            )
        _flush_restart_stdio()
        # ``serve`` is the only subcommand that runs the MCP server; without
        # it ``python -m kiln`` just prints help and exits, which makes the
        # MCP host think the child died and spawn a fresh one with its own
        # (ghost-laden) environment — defeating ``clean_env`` entirely.
        os.execve(
            sys.executable, [sys.executable, "-m", "kiln", "serve"], new_env
        )

    threading.Thread(target=_do_restart, daemon=True).start()
    msg = "Kiln server restarting in ~0.3s."
    if stripped:
        msg += (
            f" Stripped {len(stripped)} stale KILN_PRINTER_* env var(s) so "
            f"~/.kiln/config.yaml wins."
        )
    msg += " MCP connection will drop and the client should auto-reconnect."
    return {
        "success": True,
        "stripped_env_vars": sorted(stripped),
        "message": msg,
    }


# safety_settings — moved to plugins/safety_tools.py


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
    if err := _check_auth("admin"):
        return err

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


# get_started — moved to plugins/utility_tools.py


@mcp.tool()
def marketplace_info() -> dict:
    """Show which 3D model marketplaces are connected and available.

    Returns the list of connected marketplace sources and their
    capabilities (search, download support, etc.).  Configure
    marketplaces via environment variables.

    **See also:** ``marketplace_status`` for per-credential diagnostics,
    or ``marketplace_diagnostics`` for live connectivity probes.

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
        if not _MMF_API_KEY:
            env_hints.append(
                "MyMiniFactory (recommended): get API key at https://myminifactory.com/settings/developer"
                " → export KILN_MMF_API_KEY=your_key"
            )
        if not (_CULTS3D_USERNAME and _CULTS3D_API_KEY):
            env_hints.append(
                "Cults3D (search only, no downloads): get API key at https://cults3d.com/en/api/keys"
                " → export KILN_CULTS3D_USERNAME=your_username && export KILN_CULTS3D_API_KEY=your_key"
            )
        if not _THINGIVERSE_TOKEN:
            env_hints.append(
                "Thingiverse (deprecated): create app at https://www.thingiverse.com/apps/create"
                " → export KILN_THINGIVERSE_TOKEN=your_token"
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
        return _error_dict(f"Unexpected error in marketplace_info: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Thingiverse tools — moved to plugins/marketplace_tools.py
# (search_models, model_details, model_files, download_model)
# ---------------------------------------------------------------------------


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
            adapter = _get_registry().get(printer_name)
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
            safety_printer = _resolve_effective_printer_name(printer_name)
            if block := _emergency_latch_error("download_and_upload", safety_printer):
                return block
            # Mandatory pre-flight safety gate before starting print.
            pf = unwrap_tool_result(preflight_check())
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
            _get_heater_watchdog().notify_print_started()
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

        # Telemetry: count marketplace download by source
        try:
            from kiln.daily_stats import record_event
            record_event("downloads", detail=source or "unknown")
        except Exception:
            pass

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


# browse_models, list_model_categories — moved to plugins/marketplace_tools.py


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
    if "sparkxi7" in hint_compact or "sparkx" in hint_compact:
        return "sparkx_i7"
    if "ender3" in hint_compact:
        if "v4" in hint_compact:
            return "ender3_v4"
        if "v3ke" in hint_compact:
            return "ender3_v3_ke"
        if "v3se" in hint_compact:
            return "ender3_v3_se"
        if "v3" in hint_compact:
            return "ender3_v3"
        if "v2" in hint_compact:
            return "ender3_v2"
        return "ender3"
    if "k1max" in hint_compact:
        return "k1_max"
    if "k1c" in hint_compact:
        return "k1c"
    if "k1se" in hint_compact:
        return "k1_se"
    if hint_compact == "k1" or "crealityk1" in hint_compact:
        return "k1"
    if "k2plus" in hint_compact:
        return "k2_plus"
    if "k2pro" in hint_compact:
        return "k2_pro"
    if "k2se" in hint_compact:
        return "k2_se"
    if hint_compact == "k2" or "crealityk2" in hint_compact:
        return "k2"
    if hint_compact in {"hi", "crealityhi"}:
        return "creality_hi"
    if "ender5max" in hint_compact:
        return "ender5_max"
    if "cr10se" in hint_compact:
        return "cr10_se"
    if hint in {"klipper", "moonraker"}:
        return "klipper_generic"

    # Bambu Lab printers
    if "a1" in hint and "mini" in hint:
        return "bambu_a1_mini"
    if hint in {"bambu_a1", "a1", "a1_combo"} or ("bambu" in hint and "a1" in hint):
        return "bambu_a1"
    if "a2l" in hint:
        return "bambu_a2l"
    if "h2s" in hint:
        return "bambu_h2s"
    if "x1e" in hint or "x1e" in hint_compact:
        return "bambu_x1e"
    if "x1c" in hint or "x1_carbon" in hint_compact or ("bambu" in hint and "x1" in hint):
        return "bambu_x1c"
    if "p2s" in hint:
        return "bambu_p2s"
    if "p1s" in hint or ("bambu" in hint and "p1" in hint and "s" in hint):
        return "bambu_p1s"
    if "p1p" in hint or ("bambu" in hint and "p1" in hint):
        return "bambu_p1p"

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


# slice_model — moved to plugins/slicer_tools.py


# reslice_with_overrides — moved to plugins/slicer_tools.py


@mcp.tool()
def rotate_model(
    input_path: str,
    rotation_z: float = 0.0,
    rotation_x: float = 0.0,
    rotation_y: float = 0.0,
    output_path: str | None = None,
) -> dict:
    """Rotate a 3D model file (STL or 3MF) by specified angles before slicing.

    Useful for improving print quality — rotating a tall narrow part 45° around
    the Z axis can reduce toolhead-induced wobble and ringing artifacts.

    Args:
        input_path: Path to the STL or 3MF file to rotate.
        rotation_z: Rotation around Z axis in degrees (most common — rotates
            on the build plate).
        rotation_x: Rotation around X axis in degrees.
        rotation_y: Rotation around Y axis in degrees.
        output_path: Where to save the rotated file.  Defaults to
            ``<input>_rotated.<ext>``.

    Returns dict with ``output_path`` (path to rotated file) and
    ``rotations_applied``.

    Pair with ``reslice_with_overrides`` to re-slice the rotated model with
    adjusted settings (e.g., stronger brim after rotation).
    """
    if err := _check_auth("slicer"):
        return err

    try:
        from kiln.auto_orient import rotate_3mf_file, rotate_stl_file

        if not os.path.isfile(input_path):
            return _error_dict(
                f"File not found: {input_path}",
                code="FILE_NOT_FOUND",
            )

        ext = Path(input_path).suffix.lower()
        if ext not in (".stl", ".3mf"):
            return _error_dict(
                f"Unsupported file format {ext!r}. rotate_model supports .stl and .3mf files.",
                code="UNSUPPORTED",
            )

        if output_path is None:
            p = Path(input_path)
            output_path = str(p.with_stem(p.stem + "_rotated"))

        if ext == ".stl":
            rotate_stl_file(
                input_path,
                output_path,
                rotation_z=rotation_z,
                rotation_x=rotation_x,
                rotation_y=rotation_y,
            )
        else:
            rotate_3mf_file(
                input_path,
                output_path,
                rotation_z=rotation_z,
                rotation_x=rotation_x,
                rotation_y=rotation_y,
            )

        response = {
            "success": True,
            "output_path": output_path,
            "rotations_applied": {
                "x": rotation_x,
                "y": rotation_y,
                "z": rotation_z,
            },
        }
        try:
            from kiln_pro.plugins.git_render_tools import attach_inspect_bundle

            return attach_inspect_bundle(response, level="quick")
        except ImportError:
            return response
    except (ValueError, FileNotFoundError) as exc:
        return _error_dict(f"Failed to rotate model: {exc}", code="ROTATE_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in rotate_model")
        return _error_dict(f"Unexpected error in rotate_model: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
async def check_orientation(
    model_path: str,
) -> dict:
    """Check if a model's orientation is stable for printing.

    Analyzes the height-to-base ratio and warns if the model is likely to
    wobble or fail mid-print.  Suggests reorientation if needed.

    :param model_path: Path to the STL or OBJ model file.
    :returns: Dict with stability assessment.
    """
    # STEP in, mesh out — the one shared door, never a per-tool branch, so
    # the CAD format engineering customers actually send works here instead
    # of failing several layers down.
    from kiln.step_import import resolve_mesh_input

    model_path, _conversion, _refusal = resolve_mesh_input(model_path)
    if _refusal:
        return _refusal

    try:
        from kiln.auto_orient import check_stability

        result = check_stability(model_path)
        return {"success": True, **result.to_dict()}
    except Exception as e:
        return _error_dict(f"Orientation check failed: {e}", code="ORIENTATION_ERROR")


# find_slicer — moved to plugins/slicer_tools.py
# slice_and_print — moved to plugins/slicer_tools.py


# ---------------------------------------------------------------------------
# Webcam snapshot tool
# ---------------------------------------------------------------------------


@mcp.tool()
def printer_snapshot(
    printer_name: str | None = None,
    save_path: str | None = None,
) -> dict:
    """Capture a webcam snapshot from the printer.

    Handles TLS+JPEG camera protocol (Bambu A1/P1), MJPEG stream capture
    (OctoPrint/Moonraker), and RTSPS (Bambu X1) automatically.

    :param printer_name: Target printer name.  Omit for the default printer.
    :param save_path: Optional path to save the image file.  If omitted, the
        image is returned as a base64-encoded string.
    """
    try:
        if printer_name:
            adapter = _get_registry().get(printer_name)
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


# estimate_cost — moved to plugins/estimate_tools.py


@mcp.tool()
def list_materials() -> dict:
    """List built-in filament material profiles (density, cost, temps).

    Returns Kiln's bundled material database — NOT what is physically loaded.
    For loaded material, use ``get_material`` (software tracker) or
    ``get_active_material`` (live AMS hardware query).
    """
    materials = _get_cost_estimator().materials
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
    if err := _check_auth("write"):
        return err
    try:
        mat = _get_material_tracker().set_material(
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
    """Get material loaded in a printer (from Kiln's software tracker).

    Returns what the user/agent told Kiln is loaded via ``set_material``.
    For live AMS hardware reading (Bambu Lab), use ``get_active_material``.

    Args:
        printer_name: Target printer.  Omit for the default printer.
    """
    try:
        name = printer_name or "default"
        materials = _get_material_tracker().get_all_materials(name)
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
        warning = _get_material_tracker().check_match(name, expected_material)
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
        spools = _get_material_tracker().list_spools()
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
    if err := _check_auth("write"):
        return err
    try:
        spool = _get_material_tracker().add_spool(
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
    if err := _check_auth("write"):
        return err
    try:
        removed = _get_material_tracker().remove_spool(spool_id)
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
        status = _get_bed_level_mgr().check_needed(name)
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
    if err := _check_auth("calibrate"):
        return err
    try:
        if printer_name:
            adapter = _get_registry().get(printer_name)
            name = printer_name
        else:
            adapter = _get_adapter()
            name = "default"

        result = _get_bed_level_mgr().trigger_level(name, adapter, triggered_by="manual")
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
    if err := _check_auth("calibrate"):
        return err
    try:
        name = printer_name or "default"
        policy = LevelingPolicy(
            enabled=enabled,
            max_prints_between_levels=max_prints,
            max_hours_between_levels=max_hours,
            gcode_command=gcode_command,
        )
        _get_bed_level_mgr().set_policy(name, policy)
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
            return {"success": True, "stream": _get_stream_proxy().status().to_dict()}

        if action == "stop":
            info = _get_stream_proxy().stop()
            return {"success": True, "stream": info.to_dict()}

        if action == "start":
            if printer_name:
                adapter = _get_registry().get(printer_name)
            else:
                adapter = _get_adapter()

            stream_url = adapter.get_stream_url()
            if stream_url is None:
                return _error_dict(
                    "Webcam streaming not available for this printer.",
                    code="NO_STREAM",
                )

            info = _get_stream_proxy().start(
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


# Cloud sync tools — moved to plugins/cloud_sync_tools.py


# ---------------------------------------------------------------------------
# Plugin tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_plugins() -> dict:
    """List all discovered plugins and their status."""
    plugins = _get_plugin_mgr().list_plugins()
    return {
        "success": True,
        "plugins": [p.to_dict() for p in plugins],
    }


# plugin_info — moved to plugins/utility_tools.py


# ---------------------------------------------------------------------------
# Fulfillment tools — moved to plugins/fulfillment_tools.py
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Consumer workflow tools — moved to plugins/consumer_tools.py
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 3DOS Network tools — moved to plugins/network_tools.py
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_GCODE_EXTENSIONS = {".gcode", ".gco", ".g", ".3mf"}
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


# kiln_health — moved to plugins/utility_tools.py


@mcp.tool()
@requires_tier(LicenseTier.BUSINESS)
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
        endpoint = _get_webhook_mgr().register(
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
@requires_tier(LicenseTier.BUSINESS)
def list_webhooks() -> dict:
    """List all registered webhook endpoints.

    Returns endpoint details including URL, subscribed events, and
    delivery statistics.
    """
    try:
        endpoints = _get_webhook_mgr().list_endpoints()
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
@requires_tier(LicenseTier.BUSINESS)
def delete_webhook(endpoint_id: str) -> dict:
    """Delete a registered webhook endpoint.

    Args:
        endpoint_id: The endpoint ID returned by ``register_webhook``.

    Once deleted, the endpoint will no longer receive event notifications.
    """
    if err := _check_auth("admin"):
        return err
    try:
        removed = _get_webhook_mgr().unregister(endpoint_id)
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
    brief_id: str = "",
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
        brief_id: Optional saved-goal id from ``design_session``.  When
            the brief resolves, the terminal-outcome response gains a
            ``design_goal`` block with the design's duty / environment /
            safety notes — so the agent surfacing the print result can
            answer "did this match the goal?" without a separate
            lookup.  Best-effort: missing kiln-pro silently skips.

    Returns a dict with ``outcome`` (completed / failed / cancelled /
    timeout), final printer state, elapsed time, completion percentage
    history, and (when ``brief_id`` resolves) a ``design_goal`` block.
    """
    if err := _check_auth("print"):
        return err

    start = time.time()
    progress_log: list[dict] = []
    last_pct: float | None = None
    # True once this call has seen the printer actively printing —
    # the gate on recording print-hours at the terminal transition.
    _saw_active_print = False
    # B10 + D3: resolve once at entry — the brief context is stable for
    # the lifetime of this poll loop.  We attach the same dict to every
    # terminal-state response so the agent always sees the goal
    # alongside the outcome.  When the caller didn't supply a brief_id,
    # try to derive one from the currently-printing file's intent
    # sidecar (via the upload manifest) — design call from the
    # original B10 review.  Best-effort throughout.
    # Resolve the print context once at entry — stable for the poll loop, and
    # reused both for brief auto-derive and for the silent community
    # auto-contribution fired on a terminal outcome.
    file_name_at_entry: str | None = None
    printer_model_at_entry: str | None = None
    material_at_entry: str | None = None
    try:
        adapter = _get_adapter()
        jd_at_entry = adapter.get_job().to_dict()
        file_name_at_entry = jd_at_entry.get("file_name")
        material_at_entry = jd_at_entry.get("material") or (
            jd_at_entry.get("settings") or {}
        ).get("material")
        try:
            from kiln.community_autofire import resolve_adapter_model

            printer_model_at_entry = resolve_adapter_model(adapter)
        except Exception:
            logger.debug(
                "await_print_completion: printer model lookup skipped", exc_info=True
            )
    except Exception:
        logger.debug(
            "await_print_completion: entry context fetch skipped (best-effort)",
            exc_info=True,
        )
    effective_brief_id = brief_id or _auto_derive_brief_id(file_name_at_entry)
    _goal_ctx = _resolve_brief_context(effective_brief_id)

    def _attach_goal(result: dict) -> dict:
        """Add the design_goal block to a terminal-state result, and silently
        contribute the outcome to the community pool.  Both are best-effort;
        the auto-contribution is opt-in-gated and skips non-quality outcomes
        (timeout/cancelled) inside the helper."""
        if _goal_ctx is not None:
            result["design_goal"] = _goal_ctx
        try:
            from kiln import community_autofire

            jd = result.get("job") or {}
            community_autofire.auto_contribute_completion(
                outcome=result.get("outcome", ""),
                printer_file_name=file_name_at_entry,
                job_id=job_id,
                printer_model=printer_model_at_entry,
                material=material_at_entry or jd.get("material"),
                print_time_seconds=jd.get("print_time_seconds"),
            )
        except Exception:
            logger.debug(
                "await_print_completion: auto-contribute skipped (best-effort)",
                exc_info=True,
            )
        return result

    while True:
        elapsed = time.time() - start
        if elapsed >= timeout:
            return _attach_goal({
                "success": True,
                "outcome": "timeout",
                "elapsed_seconds": round(elapsed, 1),
                "message": f"Timed out after {timeout}s waiting for print to finish.",
                "progress_log": progress_log[-20:],
            })

        try:
            # --- Job-based tracking (via queue) ---
            if job_id is not None:
                try:
                    job = _get_queue().get_job(job_id)
                except JobNotFoundError:
                    return _error_dict(f"Job {job_id!r} not found.", code="JOB_NOT_FOUND")

                if job.status == JobStatus.COMPLETED:
                    return _attach_goal({
                        "success": True,
                        "outcome": "completed",
                        "job": job.to_dict(),
                        "elapsed_seconds": round(elapsed, 1),
                        "progress_log": progress_log[-20:],
                    })
                if job.status == JobStatus.FAILED:
                    return _attach_goal({
                        "success": True,
                        "outcome": "failed",
                        "job": job.to_dict(),
                        "error": job.error,
                        "elapsed_seconds": round(elapsed, 1),
                        "progress_log": progress_log[-20:],
                    })
                if job.status == JobStatus.CANCELLED:
                    return _attach_goal({
                        "success": True,
                        "outcome": "cancelled",
                        "job": job.to_dict(),
                        "elapsed_seconds": round(elapsed, 1),
                        "progress_log": progress_log[-20:],
                    })

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

            if state.state in (PrinterStatus.PRINTING, PrinterStatus.PAUSED):
                _saw_active_print = True

            if state.state == PrinterStatus.IDLE:
                # Telemetry: hours for a print WE watched finish.  Only
                # when this call actually observed the print running —
                # re-awaiting an idle printer re-reads the firmware's
                # most-recent-job stats and would count the same hours
                # twice.  No queue job record exists on this path, so
                # record_print_outcome can't double-report it later
                # (its hours read requires one).
                if _saw_active_print:
                    try:
                        _elapsed_print_s = job_progress.print_time_seconds
                        if _elapsed_print_s and _elapsed_print_s > 0:
                            from kiln.daily_stats import record_print_hours

                            record_print_hours(_elapsed_print_s / 3600.0)
                    except Exception:
                        logger.debug(
                            "await_print_completion: print-hours telemetry "
                            "skipped",
                            exc_info=True,
                        )
                return _attach_goal({
                    "success": True,
                    "outcome": "completed",
                    "state": state.state.value,
                    "elapsed_seconds": round(elapsed, 1),
                    "progress_log": progress_log[-20:],
                })
            if state.state == PrinterStatus.ERROR:
                return _attach_goal({
                    "success": True,
                    "outcome": "failed",
                    "state": state.state.value,
                    "elapsed_seconds": round(elapsed, 1),
                    "progress_log": progress_log[-20:],
                })
            if state.state == PrinterStatus.OFFLINE:
                return _attach_goal({
                    "success": True,
                    "outcome": "failed",
                    "state": state.state.value,
                    "error": "Printer went offline during print.",
                    "elapsed_seconds": round(elapsed, 1),
                    "progress_log": progress_log[-20:],
                })

        except HostedUnavailableError as exc:
            # Only the job_id branch reads the queue; printer-based tracking
            # never gets here.  Answer with the queue's own reason rather
            # than an "unexpected error".
            return _error_dict(str(exc), code="HOSTED_UNAVAILABLE")
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
        estimate = _get_cost_estimator().estimate_from_file(
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
            fee_calc = _get_billing().calculate_fee(
                quote.total_price,
                currency=quote.currency,
            )
            fulfillment_quote_data = quote.to_dict()
            fulfillment_quote_data["kiln_fee"] = fee_calc.to_dict()
            fulfillment_quote_data["total_with_fee"] = float(fee_calc.total_cost)
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
            job = _get_queue().get_job(job_id)
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
        all_events = _get_event_bus().recent_events(limit=200)
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

        # Wet-filament check: moisture in the spool causes popping, stringing,
        # rough surfaces, and weak layers.  Uses the kiln-pro fingerprint when
        # installed (https://kiln3d.com), else a lightweight in-tree heuristic;
        # either way points at the Pro drying advisor for the safe recipe.
        # Symptom keywords that point at moisture but each has other plausible
        # causes (stringing -> retraction, oozing -> temperature, etc.) — a
        # single hit is circumstantial, multiple hits are corroborating.
        _wet_symptom_terms = (
            "popping", "crackling", "stringing", "oozing", "bubbles", "steam",
            "rough surface", "weak layer", "delamination",
        )
        # Explicit mentions that name moisture directly — these trip the flag
        # on a single hit regardless of material, because the user (or another
        # tool in the pipeline) has already named the cause.
        _wet_explicit_terms = ("moisture", "wet", "humid", "damp")
        haystack = " ".join([error.lower(), *(s.lower() for s in symptoms)])
        symptom_hits = sum(1 for t in _wet_symptom_terms if t in haystack)
        explicit_mention = any(t in haystack for t in _wet_explicit_terms)

        # Best-effort: read the printer's currently-loaded material to gate the
        # symptom bar.  May differ from what was loaded during the failed print
        # (the user could have swapped since); treated as a hint, not a fact.
        loaded_material: str | None = None
        try:
            loaded = _get_material_tracker().get_material(job.printer_name)
            if loaded is not None and loaded.material_type:
                loaded_material = loaded.material_type.lower()
        except Exception:  # noqa: BLE001 — tracker access must never break analysis
            pass
        hygroscopic = bool(loaded_material and any(
            tok in loaded_material for tok in _HYGROSCOPIC_MATERIAL_HINTS
        ))

        # Material-aware rule:
        #   - explicit moisture mention -> flag (always)
        #   - hygroscopic material -> 1 symptom suffices (moisture is the
        #     physically plausible default for these filaments)
        #   - non-hygroscopic / unknown material -> _WET_MIN_HITS (=2)
        #     distinct symptoms required (single keyword is too noisy)
        wet = explicit_mention or (
            (hygroscopic and symptom_hits >= 1)
            or symptom_hits >= _WET_MIN_HITS
        )

        if wet:
            causes.append(
                "Possible wet/moist filament — popping, stringing, rough surfaces, "
                "and weak layers are classic moisture symptoms."
            )
            recommendations.append(
                "If you see stringing, popping, or weak/rough layers, the filament "
                "may be wet. drying_advisor (kiln-pro, https://kiln3d.com) gives the "
                "safe drying temperature and time for your exact material before a "
                "reprint — drying protects future prints but can't restore strength "
                "already lost in the failed part."
            )

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
    except HostedUnavailableError as exc:
        # The queue's refusal, verbatim — not an "unexpected error": it
        # names the machine where the job record actually lives.
        return _error_dict(str(exc), code="HOSTED_UNAVAILABLE")
    except Exception as exc:
        logger.exception("Unexpected error in analyze_print_failure")
        return _error_dict(f"Unexpected error in analyze_print_failure: {exc}", code="INTERNAL_ERROR")


# validate_print_quality — moved to plugins/gcode_validation_tools.py


@mcp.resource("kiln://status")
def resource_status() -> str:
    """Live snapshot of the entire Kiln system: printers, queue, and recent events."""
    import json

    # Fleet
    printers: list[dict[str, Any]] = []
    if _get_registry().count > 0:
        printers = _get_registry().get_fleet_status()
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
    q_summary = _get_queue().summary()

    # Events
    events = _get_event_bus().recent_events(limit=10)

    return json.dumps(
        {
            "printers": printers,
            "printer_count": len(printers),
            "queue": {
                "counts": q_summary,
                "pending": _get_queue().pending_count(),
                "active": _get_queue().active_count(),
                "total": _get_queue().total_count,
            },
            "recent_events": [e.to_dict() for e in events],
        },
        default=str,
    )


@mcp.resource("kiln://printers")
def resource_printers() -> str:
    """Fleet status for all registered printers."""
    import json

    if _get_registry().count == 0:
        try:
            adapter = _get_adapter()
            _get_registry().register("default", adapter)
        except RuntimeError:
            pass

    printers = _get_registry().get_fleet_status() if _get_registry().count > 0 else []
    idle = _get_registry().get_idle_printers() if _get_registry().count > 0 else []

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
        adapter = _get_registry().get(printer_name)
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

    summary = _get_queue().summary()
    next_job = _get_queue().next_job()
    recent = _get_queue().list_jobs(limit=20)

    return json.dumps(
        {
            "counts": summary,
            "pending": _get_queue().pending_count(),
            "active": _get_queue().active_count(),
            "total": _get_queue().total_count,
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
        job = _get_queue().get_job(job_id)
        return json.dumps({"job": job.to_dict()}, default=str)
    except JobNotFoundError:
        return json.dumps({"error": f"Job {job_id!r} not found"})


@mcp.resource("kiln://events")
def resource_events() -> str:
    """Recent events from the Kiln event bus (last 50)."""
    import json

    events = _get_event_bus().recent_events(limit=50)
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
        "2. Use `register_printer` to add new printers ("
        + format_printer_types(quote="", conjunction="or")
        + ")\n"
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
    elif provider == "gemini":
        inst = GeminiDeepThinkProvider(api_key=_GEMINI_API_KEY)
    elif provider == "tripo3d":
        inst = Tripo3DProvider(api_key=os.environ.get("KILN_TRIPO3D_API_KEY", ""))
    elif provider == "stability":
        inst = StabilityProvider(api_key=os.environ.get("KILN_STABILITY_API_KEY", ""))
    else:
        raise GenerationError(
            f"Unknown generation provider: {provider!r}.  Supported: meshy, openscad, gemini, tripo3d, stability.",
            code="UNKNOWN_PROVIDER",
        )

    _generation_providers[provider] = inst
    return inst


# list_generation_providers — moved to plugins/generation_ai_tools.py
# generate_model — moved to plugins/generation_ai_tools.py
# generate_model_from_image — moved to plugins/generation_ai_tools.py
# generation_status — moved to plugins/generation_ai_tools.py
# download_generated_model — moved to plugins/generation_ai_tools.py
# await_generation — moved to plugins/generation_ai_tools.py
# generate_and_print — moved to plugins/generation_ai_tools.py


_GENERATION_AI_TOOLS_MOVED = True  # noqa: F841  — breadcrumb for grep


@mcp.tool()
def render_model_preview(
    file_path: str,
    width: int = 800,
    height: int = 600,
    color: str = "",
) -> dict:
    """DEPRECATED — use visualize_model instead. This renders only 1 angle; visualize_model renders 6 angles with auto-framing, colored 3MF support, and quality scores.

    This tool is a thin wrapper around ``visualize_model`` with a single
    isometric angle.  Prefer ``visualize_model`` directly for multi-angle
    previews with proper auto-framing.

    Args:
        file_path: Path to an ``.stl``, ``.3mf``, ``.obj``, or ``.scad`` file.
        width: Image width in pixels (default 800).
        height: Image height in pixels (default 600).
        color: Hex color for the model (e.g. ``"#F72323"``).
    """
    # Redirect to visualize_model — this tool is deprecated, but we
    # preserve the ORIGINAL return-dict shape (preview_path, width, height)
    # for backward compatibility with any existing consumers.
    from kiln.model_visualizer import visualize_model as _viz

    result = _viz(
        file_path=file_path,
        angles=["isometric"],
        width=width,
        height=height,
        color=color if color else "",
    )
    if not result.get("success"):
        return result
    views = result.get("views", [])
    if views:
        return {
            "success": True,
            "preview_path": views[0]["path"],  # legacy key — DO NOT remove
            "width": width,
            "height": height,
            "message": (
                f"Preview rendered to {views[0]['path']}. "
                f"TIP: Use visualize_model() for multi-angle previews."
            ),
        }
    return result


@mcp.tool()
def visualize_model(
    file_path: str,
    angles: list[str] | None = None,
    width: int = 800,
    height: int = 600,
    color: str = "",
) -> dict:
    """Primary 3D preview tool — renders high-quality PNGs from multiple camera angles via OpenSCAD.

    Universal visualization tool that works with ANY 3D file — STL, 3MF,
    OBJ, or SCAD.  Returns PNG images from 6 angles: isometric, front,
    right, top, bottom, and back.

    **Colored 3MF support:** Multicolor 3MF files (with per-face color
    groups from BambuStudio, PrusaSlicer, or procedural textures) are
    automatically rendered with per-face colors — no slicer needed to
    see what the multicolor print will look like.  Colorless 3MF and
    STL/OBJ files render in uniform color via OpenSCAD as before.
    Dark models get an adaptive lighter background for visibility.
    Each view includes a ``quality_score`` and ``dark_material`` flag.

    Use this BEFORE printing to verify the model looks correct from all
    sides.  Both agents and humans should review the output.

    **When to use this vs other preview tools:**
    - ``visualize_model`` — any file, 6 angles, universal (USE THIS ONE)
    - ``preview_generated_model`` — after AI generation, includes bottom check
    - ``render_model_preview`` — single angle, quick check

    Args:
        file_path: Path to an STL, 3MF, OBJ, or SCAD file.
        angles: Optional subset of angles to render. Valid values:
            ``isometric``, ``front``, ``right``, ``top``, ``bottom``, ``back``.
            Defaults to all 6.
        width: Image width in pixels (default 800).
        height: Image height in pixels (default 600).
        color: Hex color for the model (e.g. ``"#F72323"`` for red).
            Defaults to neutral grey.  Pass the filament color to see
            a realistic preview matching the printed result.
            Ignored for colored 3MF files (per-face colors used instead).
    """
    try:
        from kiln.model_visualizer import visualize_model as _visualize

        kwargs: dict[str, Any] = {}
        if color:
            kwargs["color"] = color
        return _visualize(
            file_path,
            angles=angles,
            width=width,
            height=height,
            **kwargs,
        )
    except Exception as exc:
        logger.exception("Unexpected error in visualize_model")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def compare_renders(
    paths: list[str],
    labels: list[str] | None = None,
    angle: str = "isometric",
    width: int = 800,
    height: int = 600,
    colors: list[str] | None = None,
) -> dict:
    """Render 2-4 models side by side in a single comparison image.

    General-purpose visual diff tool — compare any 3D models at the same
    camera angle in one image.  Each model is rendered individually then
    stitched together with labels.

    **Use cases:**
    - Texture or decoration variants (compare 3 pattern options)
    - Design iterations (before vs after)
    - Material color comparisons
    - Parameter sweeps (small / medium / large)

    Returns a single PNG image path that can be displayed inline.
    Supports 2-4 models per comparison.  When 4 models are provided
    they are arranged in a 2x2 grid; otherwise a single row.

    Args:
        paths: 2-4 file paths (STL, 3MF, OBJ, or SCAD).
        labels: Custom labels for each model.  Defaults to A, B, C, D.
        angle: Camera angle for all renders.  One of ``isometric``,
            ``front``, ``right``, ``top``, ``bottom``, ``back``.
        width: Per-model image width in pixels (default 800).
        height: Per-model image height in pixels (default 600).
        colors: Optional hex color per model (e.g. ``["#F72323", "#2323F7"]``).
    """
    try:
        from kiln.model_visualizer import compare_renders as _compare_renders

        return _compare_renders(
            paths,
            labels=labels,
            angle=angle,
            width=width,
            height=height,
            colors=colors,
        )
    except Exception as exc:
        logger.exception("Unexpected error in compare_renders")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def get_feedback_loop_status(model_id: str) -> dict:
    """Get the feedback loop history for a generated model.

    Returns iteration data, whether the design was resolved, and
    which iteration produced the best result.

    Args:
        model_id: Model/job ID from a generation job.
    """
    try:
        from kiln.generation_feedback import get_feedback_loop

        loop = get_feedback_loop(model_id)
        if loop is None:
            return _error_dict(f"No feedback loop found for {model_id!r}.", code="NOT_FOUND")
        return {
            "success": True,
            "feedback_loop": loop.to_dict(),
        }
    except Exception as exc:
        logger.exception("Unexpected error in get_feedback_loop_status")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def list_design_templates() -> dict:
    """List available parametric design templates for common objects.

    Templates provide ready-to-use OpenSCAD code with customizable
    parameters.  Use ``generate_from_template`` to render one into
    a printable STL.

    Each template includes:
    - Customizable parameters with defaults, ranges, and descriptions
    - Pre-validated OpenSCAD code (prints without supports)
    - Category and description
    """
    try:
        import json as _json
        from pathlib import Path as _Path

        tpl_path = _Path(__file__).parent / "data" / "design_templates.json"
        with open(tpl_path) as fh:
            data = _json.load(fh)

        templates = []
        for key, tpl in data.items():
            if key.startswith("_"):
                continue
            templates.append(
                {
                    "id": key,
                    "name": tpl["display_name"],
                    "description": tpl["description"],
                    "category": tpl.get("category", "general"),
                    "parameters": {
                        k: {
                            "default": v["default"],
                            "description": v.get("description", ""),
                            "unit": v.get("unit", ""),
                        }
                        for k, v in tpl.get("parameters", {}).items()
                    },
                }
            )

        return {
            "success": True,
            "templates": templates,
            "count": len(templates),
            "message": f"{len(templates)} design templates available.",
        }
    except Exception as exc:
        logger.exception("Unexpected error in list_design_templates")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def generate_from_template(
    template_id: str,
    parameters: dict | None = None,
) -> dict:
    """Generate a 3D model from a parametric template with explicit parameters (local, no AI API).

    Use when you know which template and parameter values to use. For AI-assisted
    parameter inference + structural analysis, use ``smart_generate_from_template``.
    Renders the template's OpenSCAD code with custom parameter values
    into a printable STL.  Use ``list_design_templates`` to see
    available templates and their parameters.

    When the kiln-pro package is installed (Pro+ tier), the result MAY
    carry an ``intent`` block describing the geometric assertions the
    template parameters implied, and a sidecar ``<mesh>.intent.json``
    is written next to the produced STL.  Free / public installs see
    the result unchanged.  See https://kiln3d.com for tier details.

    Args:
        template_id: Template ID from ``list_design_templates``.
        parameters: Optional dict of parameter overrides
            (e.g., ``{"phone_width": 80, "angle": 70}``).
    """
    if err := _check_auth("generate"):
        return err
    try:
        import json as _json
        from pathlib import Path as _Path
        from string import Template

        tpl_path = _Path(__file__).parent / "data" / "design_templates.json"
        with open(tpl_path) as fh:
            data = _json.load(fh)

        tpl = data.get(template_id)
        if not tpl or template_id.startswith("_"):
            available = [k for k in data if not k.startswith("_")]
            return _error_dict(
                f"Template {template_id!r} not found. Available: {', '.join(available)}",
                code="NOT_FOUND",
            )

        # Merge defaults with provided parameters
        defaults = {k: v["default"] for k, v in tpl.get("parameters", {}).items()}
        params = {**defaults, **(parameters or {})}

        # Substitute parameters into SCAD code
        scad_code = Template(tpl["scad_template"]).safe_substitute(params)

        # Generate via OpenSCAD provider
        gen = _get_generation_provider("openscad")
        job = gen.generate(scad_code, format="stl")

        if job.status.value == "failed":
            return _error_dict(
                f"Template compilation failed: {job.error}",
                code="COMPILATION_ERROR",
            )

        result_dict: dict[str, Any] = {
            "success": True,
            "job": job.to_dict(),
            "template": template_id,
            "parameters_used": params,
            "message": f"Generated {tpl['display_name']} from template.",
        }

        # If succeeded, also validate the mesh
        if job.status.value == "succeeded":
            dl = gen.download_result(job.id)
            val = validate_mesh(dl.local_path)
            result_dict["result"] = dl.to_dict()
            result_dict["validation"] = val.to_dict()
            if val.bounding_box:
                bb = val.bounding_box
                w = bb.get("x_max", 0) - bb.get("x_min", 0)
                d = bb.get("y_max", 0) - bb.get("y_min", 0)
                h = bb.get("z_max", 0) - bb.get("z_min", 0)
                result_dict["dimensions"] = {
                    "width_mm": round(w, 2),
                    "depth_mm": round(d, 2),
                    "height_mm": round(h, 2),
                    "summary": f"{w:.1f} x {d:.1f} x {h:.1f} mm",
                }

            # Optional kiln-pro emission: when the kiln-pro package is
            # installed, derive a DeclaredIntent from the template
            # parameters and write a ``<mesh>.intent.json`` sidecar
            # next to the produced STL.  A later ``audit_original_design``
            # call against the same path picks the sidecar up and
            # verifies the mesh matches what was declared.  Free /
            # public installs hit the ImportError branch and emit
            # nothing.  Intent verification is a kiln-pro Pro+ feature —
            # see https://kiln3d.com for tier details.
            try:
                from kiln_pro.bridge import pro_features
            except ImportError:
                pass
            else:
                if pro_features.is_available("intent_verification"):
                    try:
                        iv = pro_features.intent_verification
                        intent = iv.derive_intent_from_template(
                            template_id,
                            params,
                            dimensions_mm=result_dict.get("dimensions"),
                        )
                        iv.write_intent_sidecar(intent, dl.local_path)
                        result_dict["intent"] = intent.to_dict()
                    except Exception:  # noqa: BLE001
                        # Intent emission is best-effort — never break
                        # the generator path on overlay failure.
                        pass

            try:
                from kiln_pro.plugins.git_render_tools import attach_inspect_bundle
                return attach_inspect_bundle(
                    result_dict, source_path=dl.local_path, level="quick",
                )
            except ImportError:
                return result_dict

        return result_dict
    except GenerationError as exc:
        return _error_dict(f"Template generation failed: {exc}", code=exc.code or "GENERATION_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in generate_from_template")
        return _error_dict(f"Unexpected error: {exc}", code="INTERNAL_ERROR")


# smart_generate_from_template — moved to plugins/generation_ai_tools.py


@mcp.tool()
def list_plate_objects(file_path: str, plate_number: int = 1) -> dict:
    """List named objects on the build plate of a Bambu .gcode.3mf file.

    Parses the plate metadata embedded in .gcode.3mf files exported by
    Bambu Studio or OrcaSlicer.  Returns every object that was on the
    plate when the file was sliced, with its name, bounding box, area,
    and layer height.

    Works even when the 3MF contains NO mesh geometry (common for
    .gcode.3mf exports).

    Bambu Studio supports multiple plates (plate_1, plate_2, etc.).
    Use ``plate_number`` to select which plate to inspect.  The response
    includes a ``plates_available`` field listing all plate numbers found
    in the archive.

    Use this to discover which parts are in a multi-object file before
    calling ``extract_plate_object`` to isolate one — or, if a part fails
    mid-print, to get its ``label_id`` for ``skip_print_objects`` so you can
    abandon just that object and save the rest of the plate.

    :param file_path: Path to the .3mf or .gcode.3mf file.
    :param plate_number: Which plate to inspect (1-based, default 1).
    :returns: Dict with ``objects`` list (each with a ``label_id`` that
        ``skip_print_objects`` consumes), plate metadata (bed type,
        filament colours, nozzle diameter, sequential print flag),
        and ``plates_available``.
    """
    if err := _check_auth("generate"):
        return err
    try:
        from kiln.generation.validation import (
            list_plate_objects as _list_plate,
        )

        return {"success": True, **_list_plate(file_path, plate_number=plate_number)}
    except FileNotFoundError as exc:
        return _error_dict(str(exc), code="FILE_NOT_FOUND")
    except Exception as exc:
        return _error_dict(f"Failed to list plate objects: {exc}", code="PLATE_PARSE_ERROR")


@mcp.tool()
def extract_plate_object(
    file_path: str,
    object_name: str,
    output_dir: str = "",
    plate_number: int = 1,
) -> dict:
    """Extract a single object's G-code from a multi-object Bambu .gcode.3mf.

    When a .gcode.3mf contains multiple objects (e.g. a lid and a body),
    this tool extracts ONLY the G-code for the requested object, producing
    a standalone .gcode file that can be printed directly.

    The machine start-up (homing, levelling, heating) and end (cool-down,
    park) sequences are preserved.  Only the per-layer toolpath sections
    for other objects are removed.

    Bambu Studio supports multiple plates (plate_1, plate_2, etc.).
    Use ``plate_number`` to select which plate to extract from.

    Object matching is case-insensitive and supports partial names:
    ``"cap"`` will match ``"TreatHolder - cap.stl"``.

    Use ``list_plate_objects`` first to see available object names.

    :param file_path: Path to the .gcode.3mf file.
    :param object_name: Name (or partial name) of the object to extract.
    :param output_dir: Directory for the output .gcode file. Defaults to
        the same directory as the input file.
    :param plate_number: Which plate to extract from (1-based, default 1).
    :returns: Dict with output path, matched object info, and line counts.
    """
    if err := _check_auth("generate"):
        return err
    try:
        from kiln.generation.validation import extract_plate_object_gcode

        # Let the implementation handle output path generation; if
        # output_dir is specified, build a path in that directory using
        # the object name (sanitised by the implementation).
        output_path = None
        if output_dir:
            from pathlib import Path as _Path

            _Path(output_dir).mkdir(parents=True, exist_ok=True)
            # Pass output_dir as parent; the implementation sanitises
            # the object name for the filename.
            safe_name = object_name.rsplit(".", 1)[0]
            safe_name = (
                "".join(c if c.isalnum() or c in " _-" else "_" for c in safe_name).strip() or "extracted_object"
            )
            output_path = str(_Path(output_dir) / f"{safe_name}.gcode")

        result = extract_plate_object_gcode(
            file_path,
            object_name,
            output_path=output_path,
            plate_number=plate_number,
        )
        return {"success": True, **result}
    except FileNotFoundError as exc:
        return _error_dict(str(exc), code="FILE_NOT_FOUND")
    except ValueError as exc:
        msg = str(exc)
        if "absolute extrusion" in msg or "M82" in msg:
            code = "EXTRUSION_MODE_ERROR"
        elif "No object matching" in msg:
            code = "OBJECT_NOT_FOUND"
        else:
            code = "VALIDATION_ERROR"
        return _error_dict(msg, code=code)
    except Exception as exc:
        return _error_dict(f"Failed to extract plate object: {exc}", code="EXTRACT_ERROR")


@mcp.tool()
def print_plate_object(
    file_path: str,
    object_name: str,
    use_ams: str = "auto",
    ams_mapping: list[int] | None = None,
    bed_leveling: bool = True,
    flow_cali: bool = True,
    vibration_cali: bool = True,
    bed_type: str = "auto",
    plate_number: int = 1,
) -> dict:
    """Extract a single object from a multi-object .gcode.3mf and print it.

    This is a compound workflow tool that performs the complete pipeline
    in one call:

    1. **Extract** the requested object's G-code (``extract_plate_object``)
    2. **Upload** the extracted G-code to the printer (``upload_file``)
    3. **Preflight + Start** the print (``start_print``, which runs its
       own preflight safety check)

    Bambu Studio supports multiple plates (plate_1, plate_2, etc.).
    Use ``plate_number`` to select which plate to extract and print from.

    Object matching is case-insensitive and supports partial names:
    ``"cap"`` matches ``"TreatHolder - cap.stl"``.

    Use ``list_plate_objects`` first if you want to preview what's
    available before committing to a print.

    :param file_path: Path to the .gcode.3mf file.
    :param object_name: Name (or partial name) of the object to print.
    :param use_ams: AMS mode — ``"auto"``, ``"true"``, or ``"false"``.
    :param ams_mapping: AMS slot mapping (e.g. ``[0]`` for slot 1).
    :param bed_leveling: Run bed leveling before print.
    :param flow_cali: Run flow calibration before print.
    :param vibration_cali: Run vibration calibration before print.
    :param bed_type: Bed surface type — ``"auto"``, ``"textured_plate"``,
        ``"cool_plate"``, or ``"engineering_plate"`` (Bambu only).
    :param plate_number: Which plate to extract from (1-based, default 1).
    :returns: Dict with extraction info and print start status.
    """
    if err := _check_auth("print"):
        return err

    import tempfile

    # Step 1: Extract the object's gcode
    try:
        from kiln.generation.validation import extract_plate_object_gcode

        output_dir = os.path.join(tempfile.gettempdir(), "kiln_plate_extract")
        os.makedirs(output_dir, mode=0o700, exist_ok=True)

        extract_result = extract_plate_object_gcode(
            file_path,
            object_name,
            output_path=os.path.join(
                output_dir,
                _safe_filename(object_name) + ".gcode",
            ),
            plate_number=plate_number,
        )
    except FileNotFoundError as exc:
        return _error_dict(str(exc), code="FILE_NOT_FOUND")
    except ValueError as exc:
        return _error_dict(str(exc), code="OBJECT_NOT_FOUND")
    except Exception as exc:
        return _error_dict(f"Failed to extract object: {exc}", code="EXTRACT_ERROR")

    extracted_path = extract_result["output_path"]
    matched_object = extract_result["matched_object"]

    # Step 1b: For Bambu printers, wrap extracted gcode in a 3MF container.
    # Bambu firmware ignores the ``gcode_file`` MQTT command for raw .gcode
    # files — it only responds to ``project_file`` which requires a .3mf.
    # Wrapping also avoids false-positive safety blocks (e.g. M500 in the
    # standard Bambu start gcode) since the scanner skips 3MF archives.
    upload_path = extracted_path
    if _PRINTER_TYPE == "bambu":
        try:
            from kiln.printers.bambu_3mf import repackage_gcode_as_bambu_3mf

            threemf_path = extracted_path.rsplit(".", 1)[0] + ".gcode.3mf"
            repackage_gcode_as_bambu_3mf(
                extracted_path,
                threemf_path,
                source_3mf_path=file_path,
                estimated_time_minutes=extract_result.get("estimated_time_minutes", 0),
            )
            upload_path = threemf_path
        except Exception as exc:
            logger.warning("3MF repackaging failed, falling back to raw gcode: %s", exc)

    # Step 2: Upload
    try:
        upload_result = upload_file(upload_path)
        if not upload_result.get("success", False):
            return {
                "status": "upload_failed",
                "phase": "upload",
                "message": (
                    f"Upload failed. The extracted G-code is at: {extracted_path}. Try upload_file() manually."
                ),
                "extracted_gcode": extracted_path,
                "matched_object": matched_object,
                "upload_error": upload_result,
            }
    except Exception as exc:
        return _error_dict(
            f"Upload failed: {exc}. Extracted gcode at: {extracted_path}",
            code="UPLOAD_ERROR",
        )

    # Step 3: Start print (start_print has its own preflight gate built in)
    uploaded_name = upload_result.get("file_name") or os.path.basename(extracted_path)
    try:
        print_result = start_print(
            file_name=uploaded_name,
            use_ams=use_ams,
            ams_mapping=ams_mapping,
            bed_leveling=bed_leveling,
            flow_cali=flow_cali,
            vibration_cali=vibration_cali,
            bed_type=bed_type,
        )
    except Exception as exc:
        return _error_dict(
            f"Start print failed: {exc}. File uploaded as: {uploaded_name}",
            code="PRINT_ERROR",
        )

    # Check if start_print returned an error (rate limit, confirmation, etc.)
    if print_result.get("error"):
        return {
            "status": "print_blocked",
            "phase": "start_print",
            "message": (
                f"Print could not start. The file was uploaded as: {uploaded_name}. "
                f"Resolve the issue and call start_print() manually."
            ),
            "extracted_gcode": extracted_path,
            "matched_object": matched_object,
            "upload": upload_result,
            "print": print_result,
        }

    return {
        "success": True,
        "message": (f"Printing '{matched_object['name']}' extracted from '{os.path.basename(file_path)}'."),
        "matched_object": matched_object,
        "all_objects": extract_result["all_objects"],
        "extracted_gcode": extracted_path,
        "skipped_lines": extract_result["skipped_lines"],
        "upload": upload_result,
        "print": print_result,
    }


def _safe_filename(name: str) -> str:
    """Sanitise a string for use as a filename."""
    safe = name.rsplit(".", 1)[0] if "." in name else name
    safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in safe).strip()
    return safe or "extracted_object"


@mcp.tool()
def resolve_model_source(file_path: str) -> dict:
    """Identify where a .3mf or .gcode.3mf file was downloaded from.

    Reads embedded metadata to determine the original marketplace source.
    Supports MakerWorld metadata and generic 3MF metadata (Title,
    Designer, Application, License, etc.).

    Returns the model title, designer, model URL (if available), slicer
    application name, and a list of objects on the plate.

    Use this when you need to trace a file back to its source — for
    example, to find the original STL files on MakerWorld when the
    .gcode.3mf only contains pre-sliced G-code without mesh geometry.

    :param file_path: Path to the .3mf or .gcode.3mf file.
    :returns: Dict with source marketplace, model URL, designer info,
        and plate object names.
    """
    if err := _check_auth("generate"):
        return err
    try:
        from kiln.marketplaces.makerworld import resolve_makerworld_source

        # Try MakerWorld first
        result = resolve_makerworld_source(file_path)
        if result is not None:
            return {"success": True, **result}

        # Fall back to generic 3MF metadata extraction
        import json as _json
        import xml.etree.ElementTree as ET
        import zipfile

        model_xml: str | None = None
        plate_json_raw: bytes | None = None

        with zipfile.ZipFile(file_path, "r") as zf:
            for candidate in ["3D/3dmodel.model", "3d/3dmodel.model"]:
                if candidate in zf.namelist():
                    model_xml = zf.read(candidate).decode("utf-8")
                    break
            if model_xml is None:
                for name in zf.namelist():
                    if name.lower().endswith(".model"):
                        model_xml = zf.read(name).decode("utf-8")
                        break
            for candidate in ["Metadata/plate_1.json", "metadata/plate_1.json"]:
                if candidate in zf.namelist():
                    plate_json_raw = zf.read(candidate)
                    break

        if model_xml is None:
            return _error_dict(
                f"No 3D model metadata found in {file_path}",
                code="SOURCE_NOT_FOUND",
            )

        root = ET.fromstring(model_xml)
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"

        meta: dict[str, str] = {}
        for el in root.findall(f"{ns}metadata"):
            name_attr = el.get("name", "")
            text = el.text or ""
            if name_attr and text:
                meta[name_attr] = text

        if not meta:
            return _error_dict(
                f"No metadata found in {file_path}",
                code="SOURCE_NOT_FOUND",
            )

        generic: dict[str, Any] = {
            "source": "unknown",
            "title": meta.get("Title", ""),
            "designer": meta.get("Designer", ""),
            "license": meta.get("License", ""),
            "application": meta.get("Application", ""),
            "creation_date": meta.get("CreationDate", ""),
            "modification_date": meta.get("ModificationDate", ""),
            "description": meta.get("Description", ""),
        }

        if plate_json_raw is not None:
            plate_data = _json.loads(plate_json_raw.decode("utf-8"))
            objects = plate_data.get("bbox_objects", [])
            generic["plate_objects"] = [obj.get("name", f"object_{i}") for i, obj in enumerate(objects)]

        return {"success": True, **generic}
    except FileNotFoundError as exc:
        return _error_dict(str(exc), code="FILE_NOT_FOUND")
    except ValueError as exc:
        return _error_dict(str(exc), code="SOURCE_NOT_FOUND")
    except Exception as exc:
        return _error_dict(f"Failed to resolve model source: {exc}", code="RESOLVE_ERROR")


@mcp.tool()
def validate_openscad_code(code: str) -> dict:
    """Validate OpenSCAD code without generating geometry.

    Compiles the code and returns structured error/warning information
    with line numbers.  Use this to check code before calling
    generate_model with OpenSCAD.

    :param code: OpenSCAD source code to validate.
    :returns: Dict with ``valid``, ``errors``, and ``warnings``.
    """
    try:
        gen = _get_generation_provider("openscad")
        result = gen.validate_scad(code)
        return {"success": True, **result}
    except GenerationError as exc:
        return _error_dict(f"OpenSCAD validation failed: {exc}", code=exc.code or "VALIDATION_ERROR")
    except Exception as exc:
        return _error_dict(f"Validation error: {exc}", code="VALIDATION_ERROR")


# estimate_print_time — moved to plugins/estimate_tools.py

# iterate_design — moved to plugins/design_reasoning_tools.py
# optimize_print_orientation — moved to plugins/design_reasoning_tools.py
# estimate_support_material — moved to plugins/design_reasoning_tools.py
# generate_template_variations — moved to plugins/generation_ai_tools.py
# design_advisor — moved to plugins/design_reasoning_tools.py


# ---------------------------------------------------------------------------
# Phase 4: Mesh tools extracted → plugins/mesh_tools.py
# Remaining: failure prediction, material cost
# ---------------------------------------------------------------------------


@mcp.tool()
def predict_print_failure(
    file_path: str,
    min_wall_mm: float = 0.8,
    max_bridge_mm: float = 15.0,
    max_overhang_deg: float = 55.0,
) -> dict:
    """Predict common 3D printing failure modes from mesh geometry.

    Analyzes the mesh for thin walls, long unsupported bridges,
    severe overhangs, top-heavy geometry, small features, and
    non-manifold issues.  Returns a risk score (0-100) and
    per-failure details with fix suggestions.

    :param file_path: Path to mesh file (.stl, .obj, or .glb).
    :param min_wall_mm: Minimum printable wall thickness (default 0.8).
    :param max_bridge_mm: Maximum unsupported bridge length (default 15).
    :param max_overhang_deg: Maximum overhang angle before failure (default 55).
    :returns: Dict with verdict, risk score, and failure list.
    """
    try:
        from kiln.generation.validation import predict_print_failures

        return {
            "success": True,
            **predict_print_failures(
                file_path,
                min_wall_mm=min_wall_mm,
                max_bridge_mm=max_bridge_mm,
                max_overhang_deg=max_overhang_deg,
            ),
        }
    except Exception as exc:
        return _error_dict(f"Failure prediction failed: {exc}")


# estimate_material_cost — moved to plugins/estimate_tools.py

# check_print_readiness — moved to plugins/design_reasoning_tools.py

# ---------------------------------------------------------------------------
# Design reasoning tools extracted → plugins/design_reasoning_tools.py
# (analyze_structural_risks, recommend_design_reinforcements, assess_load_bearing,
#  design_improvement_plan, apply_design_reinforcements, infer_print_settings,
#  optimize_template_params, arrange_parts_on_plate)
# ---------------------------------------------------------------------------


@mcp.tool()
def search_design_templates(
    query: str,
    max_results: int = 10,
    category_filter: str = "",
) -> dict:
    """Search the template library by natural-language description.

    Fuzzy keyword matching against template IDs, descriptions, categories,
    and tags.  Returns scored matches ranked by relevance.

    :param query: Natural-language search string (e.g. "phone stand", "hook").
    :param max_results: Maximum number of results (default 10).
    :param category_filter: Optional category to limit results (e.g. "hardware").
    :returns: Dict with matches list, each containing template_id, score,
              description, and category.
    """
    _check_auth("design:search")
    try:
        from kiln.design_reasoning import search_templates

        result = search_templates(
            query,
            max_results=max_results,
            category_filter=category_filter,
        )
        return {"success": True, **result.to_dict()}
    except Exception as exc:
        return _error_dict(f"Template search failed: {exc}")


@mcp.tool()
def design_to_gcode_pipeline(
    description: str,
    output_dir: str = "",
    material: str = "PLA",
    printer_model: str = "",
    infill_percent: float = 20.0,
) -> dict:
    """End-to-end pipeline: description → template → STL → analysis → GCode.

    One-call pipeline that:
    1. Searches templates for best match
    2. Generates STL via OpenSCAD
    3. Runs structural risk analysis
    4. Estimates weight
    5. Slices to G-code (if slicer available)

    :param description: Natural-language design description.
    :param output_dir: Directory for output files (uses tempdir if empty).
    :param material: Material for weight estimation and slicing.
    :param printer_model: Printer model for slicer profile lookup.
    :param infill_percent: Infill percentage for weight estimation.
    :returns: Dict with paths to SCAD, STL, G-code files, weight, risks.
    """
    _check_auth("design:generate")
    try:
        from kiln.design_reasoning import design_to_gcode

        result = design_to_gcode(
            description,
            output_dir=output_dir,
            material=material,
            printer_model=printer_model,
            infill_percent=infill_percent,
        )
        return {"success": result.success, **result.to_dict()}
    except Exception as exc:
        return _error_dict(f"Design-to-gcode pipeline failed: {exc}")


@mcp.tool()
def merge_stl(
    file_paths: str,
    output_path: str,
    positions: str = "",
) -> dict:
    """Merge multiple STL files into a single mesh (supports positional offsets).

    Use this when you need to position parts relative to each other.
    For simple concatenation without positioning, ``merge_mesh_files`` also works.
    Combines triangle data from multiple STL files into one output file.
    Optionally translates each part to a specified position before merging.

    :param file_paths: JSON array of STL file paths.
    :param output_path: Where to write the merged STL.
    :param positions: Optional JSON array of {"x", "y", "z"} offsets per file.
    :returns: Dict with output_path, total_triangles, bounding_box.
    """
    _check_auth("design:merge")
    import json as _json

    try:
        paths = _json.loads(file_paths) if isinstance(file_paths, str) else file_paths
    except _json.JSONDecodeError:
        return _error_dict("file_paths must be a valid JSON array.", code="INVALID_ARGS")

    pos_list = None
    if positions:
        try:
            pos_list = _json.loads(positions) if isinstance(positions, str) else positions
        except _json.JSONDecodeError:
            return _error_dict("positions must be a valid JSON array.", code="INVALID_ARGS")

    try:
        from kiln.design_reasoning import merge_stl_files

        result = merge_stl_files(paths, output_path, positions=pos_list)
        if result.errors:
            return _error_dict("; ".join(result.errors), code="MERGE_FAILED")
        response = {"success": True, **result.to_dict()}
        try:
            from kiln_pro.plugins.git_render_tools import attach_inspect_bundle
            return attach_inspect_bundle(response, level="quick")
        except ImportError:
            return response
    except Exception as exc:
        return _error_dict(f"STL merge failed: {exc}")


@mcp.tool()
def compose_multicolor_3mf(
    parts: list[dict],
    output_path: str = "",
) -> dict:
    """Compose a multi-color / multi-material .3mf from multiple STL files.

    Creates a **single print-ready .3mf** containing all parts with per-part
    AMS/extruder slot assignments.  This is the correct way to send a
    multi-color design to any FDM printer — the printer receives one file,
    not multiple.

    Compatible with:
    * **BambuStudio / Bambu A1, X1, P1 + AMS** — reads ``Metadata/model_settings.config``
    * **PrusaSlicer / MMU** — reads ``slic3rpe:extruder`` on each ``<item>``
    * **Cura** and any 3MF-capable slicer

    Typical two-color workflow::

        # 1. Export body STL (main color, e.g. grey PLA)
        # 2. Export accent STL (second color, same coordinate origin)
        # 3. Compose:
        result = compose_multicolor_3mf(parts=[
            {"stl_path": "/tmp/body.stl",    "extruder": 1,
             "name": "body",    "color": "#AAAAAA", "material": "PLA Grey"},
            {"stl_path": "/tmp/qr_pads.stl", "extruder": 2,
             "name": "qr_code", "color": "#111111", "material": "PLA Black"},
        ])
        # 4. Upload and print:
        upload_file(result["output_path"])
        start_print(result["output_path"])

    Args:
        parts: List of part dicts.  Each dict requires:

            * ``stl_path`` (str) — absolute path to the STL for this part
            * ``extruder`` (int) — 1-indexed AMS slot (1 = AMS tray 1 on Bambu)

            Optional per-part keys:

            * ``name`` (str) — label shown in the slicer object list
            * ``color`` (str) — hex preview color e.g. ``"#AAAAAA"`` (display only)
            * ``material`` (str) — filament label e.g. ``"PLA Grey"`` (display only)

        output_path: Where to write the .3mf.  Defaults to a temp file whose
            path is returned in the result.

    Returns:
        Dict with ``success``, ``output_path``, ``parts``, ``total_triangles``,
        ``total_vertices``, ``extruder_map``, and ``message``.
    """
    _check_auth("design:compose")

    try:
        from kiln.multicolor_3mf import ColorPart
        from kiln.multicolor_3mf import compose_multicolor_3mf as _compose
    except ImportError as exc:
        return {"success": False, "error": f"multicolor_3mf module unavailable: {exc}"}

    color_parts = []
    for i, p in enumerate(parts):
        if not isinstance(p, dict) or "stl_path" not in p:
            return {
                "success": False,
                "error": f"Part {i + 1} missing required key 'stl_path'.",
            }
        color_parts.append(
            ColorPart(
                stl_path=str(p["stl_path"]),
                extruder=int(p.get("extruder", 1)),
                # Passed through blank rather than defaulted here: the composer
                # names a nameless part, so the same label ("Part 1") reaches
                # the user whichever door built the plate.
                name=str(p.get("name") or ""),
                color=p.get("color"),
                material=p.get("material"),
                x=float(p.get("x", 0.0)),
                y=float(p.get("y", 0.0)),
                z=float(p.get("z", 0.0)),
            )
        )

    return _compose(color_parts, output_path=output_path or None)


# auto_arrange_parts_on_plate — moved to plugins/design_reasoning_tools.py
# solve_template_constraints — moved to plugins/design_reasoning_tools.py


# firmware_status — extracted to plugins/firmware_tools.py


@mcp.tool()
def update_firmware(component: str | None = None) -> dict:
    """Start a firmware update on the default/connected printer (adapter-level, by component).

    For fleet setups where you need to update a specific printer by name
    or pin a target version, use ``update_printer_firmware`` instead.
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
    """Roll back firmware on the default/connected printer (adapter-level, by component).

    For fleet setups where you need to rollback a specific printer by name
    or target a specific version, use ``rollback_printer_firmware`` instead.
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


# _PHASE_HINTS, _detect_phase — moved to plugins/monitoring_tools.py

# monitor_print_vision — moved to plugins/monitoring_tools.py

# ---------------------------------------------------------------------------
# Background print watcher state — shared with plugins/monitoring_tools.py.
# The atexit handler in main() iterates _watchers to stop daemon threads.
# ---------------------------------------------------------------------------

_watchers: dict[str, Any] = {}

# _PrintWatcher, watch_print, watch_print_status, stop_watch_print
# — moved to plugins/monitoring_tools.py

# ---------------------------------------------------------------------------
# Monitored print state — shared with plugins/monitoring_tools.py.
# ---------------------------------------------------------------------------

# Store active first-layer monitors so agents can check progress.
_first_layer_monitors: dict[str, Any] = {}

# start_monitored_print, first_layer_status — moved to plugins/monitoring_tools.py


# ---------------------------------------------------------------------------
# Cross-printer learning + agent memory tools — moved to plugins/learning_tools.py
# ---------------------------------------------------------------------------


# list_safety_profiles, get_safety_profile, add_safety_profile — moved to plugins/safety_tools.py


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


# validate_gcode_safe — moved to plugins/gcode_validation_tools.py

# list_slicer_profiles, get_slicer_profile — moved to plugins/slicer_tools.py


# ---------------------------------------------------------------------------
# Printer intelligence tools
# ---------------------------------------------------------------------------


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
    """Get printer-specific slicer settings for a material you have already chosen.

    Use AFTER selecting a material — returns hotend/bed temps, fan speed, and
    tips tuned to a specific printer model. For help choosing which material
    to use, see ``recommend_material`` or ``recommend_design_material``.

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


# Bambu printers report failures as HMS codes: four 4-hex-digit groups, e.g.
# ``0300_1A00_0002_0001``.  The raw code plus a pointer to Bambu's own HMS
# wiki page is the free-tier floor; kiln-pro decodes the code to a cited
# cause / fix / severity for Pro+ callers at the REST boundary (no curated fix
# text lives in this public repo).
_HMS_WIKI_CODE_URL = "https://wiki.bambulab.com/en/x1/troubleshooting/hmscode/{code}"


def _normalize_hms_code(raw: str) -> str:
    """Normalize a Bambu HMS code to uppercase 4-hex groups joined by ``_``.

    Accepts hyphen / underscore / space separated or unseparated input in any
    case.  Returns ``""`` when the input holds fewer than 8 hex digits, so a
    stray word can't be misread as a code — a real HMS code is at least the
    8-hex module/attr prefix (e.g. ``0300_1A00``), usually the full 16 hex
    digits.
    """
    hex_only = "".join(c for c in raw.upper() if c in "0123456789ABCDEF")
    if len(hex_only) < 8:
        return ""
    return "_".join(hex_only[i : i + 4] for i in range(0, len(hex_only), 4))


@mcp.tool()
def troubleshoot_printer(
    printer_id: str,
    symptom: str = "",
    hms_code: str = "",
) -> dict:
    """Diagnose a printer issue by searching the known failure modes database.

    Describe the symptom (e.g. ``"under-extrusion"``, ``"layer shifting"``,
    ``"stringing"``) and get possible causes and fixes specific to your
    printer model.

    On Bambu Lab printers you can also pass ``hms_code`` — the HMS error code
    the printer's screen or app shows (e.g. ``"0300_1A00_0002_0001"``, in any
    separator or case).  The response echoes the normalized code and a link to
    Bambu's HMS wiki page for it.  With Kiln Pro (https://kiln3d.com/pricing)
    the response also carries a decoded cause, fix, and severity for the code.

    Args:
        printer_id: Printer model identifier.
        symptom: Description of the problem.  Optional when ``hms_code`` is
            given.
        hms_code: Optional Bambu HMS error code to look up.
    """
    if err := _check_auth("intel"):
        return err
    if not symptom.strip() and not hms_code.strip():
        return _error_dict(
            "Provide a symptom describing the problem, or an hms_code to look up.",
            code="INVALID_INPUT",
        )
    try:
        matches = diagnose_issue(printer_id, symptom) if symptom.strip() else []
        intel = get_printer_intel(printer_id)
        # Free-tier honest signal: the private printer overlay contributes
        # quirks, calibration, and failure-mode playbooks. Check those fields
        # on this printer instead of probing an unrelated material overlay.
        has_private_depth = bool(
            intel.quirks or intel.calibration or intel.failure_modes
        )
        upgrade_hint = (
            ""
            if has_private_depth
            else (
                "Kiln Pro adds per-printer firmware quirks + "
                "failure-mode playbooks. See https://kiln3d.com/pricing"
            )
        )
        result = {
            "success": True,
            "printer": intel.display_name,
            "symptom": symptom,
            "matches": matches,
            "count": len(matches),
            "upgrade_hint": upgrade_hint,
        }
        # Bambu HMS code lookup: the normalized raw code + a wiki pointer are
        # the free floor.  kiln-pro adds a decoded ``hms_decoded`` block (cause /
        # fix / severity, cited) for Pro+ callers at the REST boundary.
        code = _normalize_hms_code(hms_code)
        if code:
            result["hms_code"] = code
            result["hms_wiki_url"] = _HMS_WIKI_CODE_URL.format(code=code)
        return result
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


# list_print_pipelines — moved to plugins/pipeline_tools.py


@mcp.tool()
def run_quick_print(
    model_path: str,
    printer_name: str | None = None,
    printer_id: str | None = None,
    profile_path: str | None = None,
    material: str | None = None,
    use_ams: str | None = None,
    ams_mapping: str | None = None,
    skip_validation: bool = False,
) -> dict:
    """Full print pipeline: validate + slice + safety-check + upload + print (recommended one-shot tool).

    Preferred over ``slice_and_print`` — adds mesh-level pre-print
    validation, G-code safety validation, and auto-detected bundled
    slicer profiles.  For custom slicer parameter overrides, use
    ``run_reslice_and_print`` instead.  The full quick-print pipeline:
    1. Validate mesh (printability, manifold, walls, bridges, bed-fit)
    2. Resolve slicer profile (bundled, by printer_id)
    3. Slice the (possibly auto-repaired) mesh to G-code
    4. Safety-validate the G-code against printer limits
    5. Upload G-code to the printer
    6. Run preflight checks (always — cannot be skipped)
    7. Start printing

    Args:
        model_path: Path to input model (STL, 3MF, STEP, OBJ).
        printer_name: Registered printer name in fleet.
        printer_id: Printer model ID for auto-profile selection
            (e.g. ``"ender3"``, ``"bambu_x1c"``, ``"klipper_generic"``).
        profile_path: Explicit slicer profile. Overrides printer_id auto-selection.
        material: Filament material hint (e.g. ``"PLA"``).  When set, AMS
            auto-routing prefers a loaded tray whose type matches.
        use_ams: AMS feeding mode (Bambu): ``"auto"`` (default — detect and
            route to a loaded tray), ``"true"``, or ``"false"``.
        ams_mapping: Explicit AMS slot mapping as a JSON array string,
            e.g. ``"[0]"`` or ``"[0, 2]"``.  Overrides auto-selection.
        skip_validation: Bypass the mesh-level pre-print validation step.
            Defaults to False — designs are pre-tested for printability
            before they reach the printer.  Use True for already-validated
            inputs or pre-sliced 3MFs the validator can't introspect.

    On Bambu AMS printers the response carries ``ams_selection``
    (``{slot, type, color}``) naming the tray actually used — routing is
    never silent.
    """
    if err := _check_auth("print"):
        return err
    try:
        parsed_ams_mapping: list[int] | None = None
        if ams_mapping:
            import json as _json_ams
            try:
                parsed_ams_mapping = _json_ams.loads(ams_mapping)
            except _json_ams.JSONDecodeError as exc:
                return _error_dict(
                    f"Invalid JSON in ams_mapping: {exc}",
                    code="VALIDATION_ERROR",
                )
            if not isinstance(parsed_ams_mapping, list):
                return _error_dict(
                    "ams_mapping must be a JSON array of integers (e.g. [0, 2])",
                    code="VALIDATION_ERROR",
                )

        # Tri-state use_ams: "auto"/None -> None (pipeline auto-resolves),
        # "true"/"false" -> bool.
        resolved_use_ams: bool | None = None
        if use_ams is not None:
            _v = str(use_ams).strip().lower()
            if _v in ("true", "1", "yes"):
                resolved_use_ams = True
            elif _v in ("false", "0", "no"):
                resolved_use_ams = False

        result = _pipeline_quick_print(
            model_path=model_path,
            printer_name=printer_name,
            printer_id=printer_id,
            profile_path=profile_path,
            material=material,
            use_ams=resolved_use_ams,
            ams_mapping=parsed_ams_mapping,
            skip_validation=skip_validation,
        )
        resp = {"success": result.success, **result.to_dict()}
        # Hoist the AMS selection from the start_print step to the top
        # level so callers can say "AMS slot 1 — black PLA".  Never silent.
        for _step in result.steps:
            if _step.name == "start_print" and _step.data:
                if "ams_selection" in _step.data:
                    resp["ams_selection"] = _step.data["ams_selection"]
                if "ams_warnings" in _step.data:
                    resp["ams_warnings"] = _step.data["ams_warnings"]
                break
        return resp
    except Exception as exc:
        logger.exception("Unexpected error in run_quick_print")
        return _error_dict(f"Unexpected error in run_quick_print: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def run_reslice_and_print(
    model_path: str,
    printer_name: str | None = None,
    printer_id: str | None = None,
    overrides: str | None = None,
    profile_path: str | None = None,
    slicer_path: str | None = None,
    material: str | None = None,
    use_ams: bool | None = None,
    ams_mapping: str | None = None,
    skip_validation: bool = False,
) -> dict:
    """Reslice with custom slicer overrides + print (use for retries with adjusted settings).

    Use this when you need to tweak slicer parameters (speed, brim, infill, temps).
    For standard prints without overrides, use ``run_quick_print`` instead.
    One-shot pipeline: validate mesh → resolve profile with overrides →
    slice → safety check → upload to printer → start print.

    The overrides parameter is a JSON string of PrusaSlicer INI key-value pairs:
      {"brim_width": "8", "perimeter_speed": "30", "fill_density": "25%"}

    Common override keys:
      Adhesion: brim_width (mm), skirts (count)
      Temperature: temperature, bed_temperature (degrees C)
      Speed: perimeter_speed, infill_speed, first_layer_speed (mm/s)
      Structure: fill_density (%), fill_pattern, layer_height (mm)
      Support: support_material (0/1)

    Requires PrusaSlicer or OrcaSlicer installed locally.
    The printer must be idle and connected.

    Args:
        model_path: Path to input model (STL, 3MF, STEP, OBJ).
        printer_name: Registered printer name in fleet.
        printer_id: Printer model ID for auto-profile selection
            (e.g. ``"ender3"``, ``"bambu_x1c"``, ``"klipper_generic"``).
        overrides: JSON string of PrusaSlicer INI key-value pairs to override.
        profile_path: Explicit slicer profile. Overrides printer_id auto-selection.
        slicer_path: Explicit path to the slicer binary.
        material: Filament material hint (e.g. ``"PLA"``).  For fully-auto
            raw-gcode reslices, AMS routing prefers a loaded tray of this
            material.  (3MF plates carry their own filament map, so routing
            defers to the adapter there.)
        use_ams: Enable AMS filament feeding (Bambu printers). If omitted,
            auto-detected from 3MF metadata.
        ams_mapping: JSON string of AMS slot indices (e.g. ``"[0, 2]"``).
            Maps each extruder/filament to an AMS tray position.
        skip_validation: Bypass the mesh-level pre-print validation step.
            Defaults to False — designs are pre-tested for printability
            before they reach the printer.
    """
    if err := _check_auth("print"):
        return err
    try:
        parsed_overrides: dict[str, str] | None = None
        if overrides:
            import json

            try:
                parsed_overrides = json.loads(overrides)
                if not isinstance(parsed_overrides, dict):
                    return _error_dict(
                        'overrides must be a JSON object (e.g. {"brim_width": "8"})',
                        code="VALIDATION_ERROR",
                    )
            except json.JSONDecodeError as exc:
                return _error_dict(
                    f"Invalid JSON in overrides: {exc}",
                    code="VALIDATION_ERROR",
                )

        # Parse ams_mapping JSON if provided
        parsed_ams_mapping: list[int] | None = None
        if ams_mapping:
            import json as _json_ams

            try:
                parsed_ams_mapping = _json_ams.loads(ams_mapping)
                if not isinstance(parsed_ams_mapping, list):
                    return _error_dict(
                        "ams_mapping must be a JSON array of integers (e.g. [0, 2])",
                        code="VALIDATION_ERROR",
                    )
            except _json_ams.JSONDecodeError as exc:
                return _error_dict(
                    f"Invalid JSON in ams_mapping: {exc}",
                    code="VALIDATION_ERROR",
                )

        # Prefer per-model speeds when printer_id is available
        if printer_id:
            try:
                from kiln.printer_intelligence import get_slicer_speed_overrides

                model_speeds = get_slicer_speed_overrides(printer_id)
                if model_speeds:
                    if parsed_overrides is None:
                        parsed_overrides = {}
                    for k, v in model_speeds.items():
                        if k not in parsed_overrides:
                            parsed_overrides[k] = v
            except (ImportError, Exception):
                pass  # fall through to per-type defaults below

        # Inject printer-aware speed overrides (don't override explicit user settings)
        if _PRINTER_TYPE in _PRINTER_SPEED_OVERRIDES:
            if parsed_overrides is None:
                parsed_overrides = {}
            for k, v in _PRINTER_SPEED_OVERRIDES[_PRINTER_TYPE].items():
                if k not in parsed_overrides:
                    parsed_overrides[k] = v

        result = _pipeline_reslice_and_print(
            model_path=model_path,
            printer_name=printer_name,
            printer_id=printer_id,
            overrides=parsed_overrides,
            profile_path=profile_path,
            slicer_path=slicer_path,
            material=material,
            use_ams=use_ams,
            ams_mapping=parsed_ams_mapping,
            skip_validation=skip_validation,
        )
        resp = {"success": result.success, **result.to_dict()}
        # Surface the AMS tray selection (parity with run_quick_print).
        for _step in result.steps:
            if _step.name == "start_print" and _step.data:
                if "ams_selection" in _step.data:
                    resp["ams_selection"] = _step.data["ams_selection"]
                if "ams_warnings" in _step.data:
                    resp["ams_warnings"] = _step.data["ams_warnings"]
                break
        return resp
    except Exception as exc:
        logger.exception("Unexpected error in run_reslice_and_print")
        return _error_dict(f"Unexpected error in run_reslice_and_print: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def multi_copy_print(
    model_path: str,
    copies: int,
    printer_name: str | None = None,
    printer_id: str | None = None,
    spacing_mm: float = 10.0,
    overrides: str | None = None,
    slicer_path: str | None = None,
) -> dict:
    """Print multiple copies of a model arranged on one build plate.

    Automatically arranges copies in a grid with spacing so they don't
    overlap, slices the plate as a single job, and prints.

    Uses PrusaSlicer's ``--duplicate`` flag when available (handles placement,
    collision avoidance, and travel optimization). Falls back to STL mesh
    duplication for OrcaSlicer or when fine control is needed.

    Requires a slicer (PrusaSlicer or OrcaSlicer) installed locally.
    The printer must be idle and connected.

    Args:
        model_path: Path to input model (STL, OBJ).
        copies: Number of copies to print (2-20).
        printer_name: Registered printer name in fleet.
        printer_id: Printer model ID for auto-profile selection.
        spacing_mm: Gap between copies in mm (default 10).
        overrides: JSON string of slicer parameter overrides.
        slicer_path: Explicit path to the slicer binary.
    """
    if err := _check_auth("print"):
        return err

    # --- Validation ---
    if copies < 2:
        return _error_dict("copies must be >= 2.", code="VALIDATION_ERROR")
    if copies > 20:
        return _error_dict("copies must be <= 20.", code="VALIDATION_ERROR")
    if spacing_mm < 0:
        return _error_dict("spacing_mm must be >= 0.", code="VALIDATION_ERROR")

    ext = os.path.splitext(model_path)[1].lower()
    if ext not in (".stl", ".obj"):
        return _error_dict(
            f"multi_copy_print requires an STL or OBJ file, got {ext!r}.",
            code="VALIDATION_ERROR",
        )

    if not os.path.isfile(model_path):
        return _error_dict(f"File not found: {model_path}", code="FILE_NOT_FOUND")

    try:
        # --- Parse overrides ---
        parsed_overrides: dict[str, str] | None = None
        if overrides:
            import json

            try:
                parsed_overrides = json.loads(overrides)
                if not isinstance(parsed_overrides, dict):
                    return _error_dict(
                        "overrides must be a JSON object.",
                        code="VALIDATION_ERROR",
                    )
            except json.JSONDecodeError as exc:
                return _error_dict(
                    f"Invalid JSON in overrides: {exc}",
                    code="VALIDATION_ERROR",
                )

        # --- Detect slicer and choose strategy ---
        from kiln.slicer import find_slicer

        slicer_info = find_slicer(slicer_path)
        slicer_name = slicer_info.name.lower()

        use_duplicate_flag = "prusaslicer" in slicer_name or "prusa" in slicer_name

        if use_duplicate_flag:
            # PrusaSlicer path: use --duplicate flag
            extra_args = [
                "--duplicate",
                str(copies),
                "--duplicate-distance",
                str(spacing_mm),
            ]
            result = _pipeline_reslice_and_print(
                model_path=model_path,
                printer_name=printer_name,
                printer_id=printer_id,
                overrides=parsed_overrides,
                slicer_path=slicer_path,
                extra_args=extra_args,
            )
        else:
            # Fallback: STL mesh duplication
            from kiln.auto_orient import duplicate_stl_on_plate

            # Get bed dimensions from safety profiles if printer_id available
            bed_w = 256.0
            bed_d = 256.0
            if printer_id:
                try:
                    from kiln.safety_profiles import get_profile

                    profile = get_profile(printer_id)
                    if profile and profile.build_volume:
                        bed_w = float(profile.build_volume[0])
                        bed_d = float(profile.build_volume[1])
                except Exception:
                    pass

            merged_path = duplicate_stl_on_plate(
                model_path,
                copies,
                spacing_mm=spacing_mm,
                bed_width_mm=bed_w,
                bed_depth_mm=bed_d,
            )
            result = _pipeline_reslice_and_print(
                model_path=merged_path,
                printer_name=printer_name,
                printer_id=printer_id,
                overrides=parsed_overrides,
                slicer_path=slicer_path,
            )

        summary = {"success": result.success, **result.to_dict()}
        summary["copies"] = copies
        summary["strategy"] = "prusaslicer_duplicate" if use_duplicate_flag else "stl_mesh_duplication"
        return summary

    except ValueError as exc:
        return _error_dict(str(exc), code="VALIDATION_ERROR")
    except Exception as exc:
        logger.exception("Unexpected error in multi_copy_print")
        return _error_dict(f"Unexpected error in multi_copy_print: {exc}", code="INTERNAL_ERROR")


@mcp.tool()
def run_calibrate(
    printer_name: str | None = None,
    printer_id: str | None = None,
) -> dict:
    """Full calibration pipeline: home + bed level + printer-specific guidance (recommended).

    Higher-level than ``calibrate_direct`` — orchestrates the full sequence and
    returns intelligence-based calibration tips. Performs physical calibration
    steps (homing, auto bed leveling) and returns printer-specific calibration
    guidance from the intelligence database.

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
    skip_validation: bool = False,
) -> dict:
    """Prepare a benchmark print: validate → slice → upload → report stats.

    Slices a model with the printer's profile and uploads it, then
    reports printer stats from history. The print is NOT started
    automatically — benchmarks should be manually observed.

    Args:
        model_path: Path to benchmark model (STL).
        printer_name: Registered printer name.
        printer_id: Printer model ID for profile selection.
        profile_path: Explicit slicer profile path.
        skip_validation: Bypass the pre-print mesh validation step.
            Defaults to False — user-supplied benchmark meshes are
            pre-tested for printability.  Set to True for known-good
            fixed reference benchmark models.
    """
    if err := _check_auth("print"):
        return err
    try:
        result = _pipeline_benchmark(
            model_path=model_path,
            printer_name=printer_name,
            printer_id=printer_id,
            profile_path=profile_path,
            skip_validation=skip_validation,
        )
        return {"success": result.success, **result.to_dict()}
    except Exception as exc:
        logger.exception("Unexpected error in run_benchmark")
        return _error_dict(f"Unexpected error in run_benchmark: {exc}", code="INTERNAL_ERROR")


# ---------------------------------------------------------------------------
# Pipeline execution control tools
# ---------------------------------------------------------------------------


# pipeline_status, pipeline_pause, pipeline_resume, pipeline_abort,
# pipeline_retry_step — moved to plugins/pipeline_tools.py


# cache_model, search_cached_models, get_cached_model, list_cached_models,
# delete_cached_model — extracted to plugins/cache_tools.py


# backup_database — moved to plugins/utility_tools.py
# verify_audit_integrity — moved to plugins/utility_tools.py

# list_trusted_printers, trust_printer, untrust_printer
# — extracted to plugins/printer_management_tools.py

# get_skill_manifest — moved to plugins/utility_tools.py


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


_INTERNAL_TOOL_PLUGINS_REGISTERED = False


class _DedupingToolRegistrationProxy:
    """Proxy FastMCP tool registration and skip names that already exist."""

    def __init__(self, base_mcp: Any) -> None:
        self._base_mcp = base_mcp

    def tool(self, *args: Any, **kwargs: Any):
        base_decorator = self._base_mcp.tool(*args, **kwargs)

        def decorator(fn):
            existing = getattr(self._base_mcp._tool_manager, "_tools", {})
            if fn.__name__ in existing:
                return fn
            return base_decorator(fn)

        return decorator


_HOSTED_KILN_API_URL = "https://api.kiln3d.com"


def _auth_tokens_path() -> Path:
    auth_home = os.environ.get("KILN_AUTH_HOME") or str(Path.home())
    return Path(auth_home) / ".kiln" / "auth_tokens.json"


def _raw_paired_access_token() -> str:
    """The stored access token, unrefreshed — last-resort fallback only.

    Used when :mod:`kiln.auth_session` can't be reached at all (a broken
    install).  A possibly-stale bearer that the server may reject beats
    telling a signed-in user they were never paired.
    """
    try:
        import json

        data = json.loads(_auth_tokens_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return str(data.get("access_token") or "").strip()
    except Exception:
        pass
    return ""


def _paired_access_token() -> str:
    """Live session bearer ('' when signed out or unrecoverable).

    Delegates to :mod:`kiln.auth_session`, which transparently refreshes
    a near-expiry token through ``/api/auth/refresh`` — reading the file
    raw handed out hour-old dead bearers that downstream services
    rejected with an unexplained 401 / silent free-tier fallback.
    """
    try:
        from kiln.auth_session import get_paired_access_token

        return get_paired_access_token()
    except Exception:
        return _raw_paired_access_token()


# Tier required by each pro tool, keyed by tool name and filled in by
# ``_register_pro_tool_stubs`` from the manifest.  The manifest has
# always carried a ``tier`` per tool; nothing read it, so all 345 paid
# capabilities presented to an agent looking exactly like the 54 free
# ones and no refusal could say which tier it needed.
_PRO_TOOL_TIERS: dict[str, str] = {}

# Free monthly allowance per metered pro tool — ``{"bucket", "limit", "noun",
# "period"}`` straight from the manifest, filled in by the same loop.  Only
# tools the server actually meters have an entry, so a lookup miss means "this
# tool has no declared allowance" and the copy below says nothing about one.
#
# It is here because the account wall is enforced LOCALLY: ``_pro_api_call``
# refuses before the request leaves the machine, so the server's own
# "free includes N a month" — the one place that number was ever written —
# reached almost nobody.
#
# The cost of moving it client-side is that an old install states an old
# figure.  Bounded, and deliberately accepted: this copy is only ever read
# while SIGNED OUT, before anything has been metered, and the server's own
# response is authoritative from the first real call onward.
_PRO_TOOL_QUOTA: dict[str, dict] = {}


def _with_local_log_tail(kwargs: dict) -> dict:
    """Attach the redacted local log tail to a forwarded bug report.

    The ``report_issue`` stub runs HERE, on the user's machine — the
    hosted server the report lands on can never read this disk, so the
    crash evidence in ``~/.kiln/logs/kiln.log`` has to ride in the
    payload (the server accepts it as ``context.log_tail`` and
    re-redacts it on arrival).  ``read_log_tail`` strips secrets,
    private IPs, and home-directory usernames BEFORE the text leaves
    the process.

    A caller-supplied tail is never overwritten, and a report must
    never fail because its attachment did.
    """
    try:
        context = kwargs.get("context") or {}
        if not isinstance(context, dict) or context.get("log_tail"):
            return kwargs
        from kiln.log_config import read_log_tail

        tail = read_log_tail()
        if not tail:
            return kwargs
        return {**kwargs, "context": {**context, "log_tail": tail}}
    except Exception:  # noqa: BLE001 — the report matters more than its attachment
        return kwargs


#: Tools that reach the hosted API WITHOUT a bearer, via their own
#: unauthenticated intake.  Deliberately a set of one, and it should stay
#: tiny: every name here is a capability an anonymous stranger can drive.
#: ``report_issue`` earns it because the alternative is worse — an install
#: that cannot pair is exactly the install with something to report, and a
#: bug report that requires an account is a bug report we do not receive.
_ANONYMOUS_OK_TOOLS = frozenset({"report_issue"})

#: Where those tools go instead of ``/api/tools/<name>``.  A separate,
#: narrow route rather than an auth exemption on the tool dispatcher: an
#: unauthenticated caller should reach one hand-audited handler, not the
#: whole tool surface with a null tenant.
_ANONYMOUS_TOOL_ROUTES = {"report_issue": "/api/public/report"}


def _anonymous_api_call(tool_name: str, **kwargs) -> dict:
    """Forward an ``_ANONYMOUS_OK_TOOLS`` call with no Authorization header.

    Mirrors :func:`_pro_api_call`'s transport and error handling, minus every
    identity-bearing header: no bearer, and no device fingerprint either — an
    anonymous report has no account to meter and no device worth correlating,
    so sending one would collect something the report does not need.

    ``app_version`` and ``os`` ARE filled into the context when the caller did
    not set them, because the server's anti-flood bucket for anonymous
    reporters is derived from exactly those two fields plus the source.  Left
    empty, every anonymous report in the world would share one bucket and a
    single noisy install could shut the door for everyone.
    """
    import json
    import platform
    import urllib.error
    import urllib.request

    from kiln.version_check import _current_version

    api_url = (
        os.environ.get("KILN_API_URL") or ""
    ).strip() or _HOSTED_KILN_API_URL
    route = _ANONYMOUS_TOOL_ROUTES[tool_name]

    payload = _with_local_log_tail(dict(kwargs))
    context = payload.get("context")
    context = dict(context) if isinstance(context, dict) else {}
    context.setdefault("app_version", _current_version())
    context.setdefault("os", platform.system().lower())
    payload["context"] = context
    # contact_ok cannot be honoured without a verified identity, and the
    # server ignores it on this route.  Dropping it here too keeps the
    # request honest rather than sending a preference nothing can act on.
    payload.pop("contact_ok", None)

    try:
        req = urllib.request.Request(
            f"{api_url.rstrip('/')}{route}",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Kiln-Client-Version": _current_version(),
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
            if isinstance(body, dict):
                return body
        except Exception:
            pass
        return {
            "status": "error",
            "error": f"Kiln API rejected '{tool_name}' (HTTP {exc.code}).",
            "code": "KILN_API_HTTP_ERROR",
            "tool": tool_name,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": f"Failed to reach Kiln server: {exc}",
            "code": "SERVER_UNREACHABLE",
        }


def _pro_api_call(tool_name: str, **kwargs) -> dict:
    """Call a hosted kiln-pro tool through the public REST API.

    Bearer-token resolution order:
      1. ``KILN_LICENSE_KEY`` env var (operator-supplied license)
      2. OAuth access token from ``$KILN_AUTH_HOME/.kiln/auth_tokens.json``,
         set by ``kiln signin`` / ``kiln pair <code>``
      3. None — return ``KILN_ACCOUNT_NOT_PAIRED`` with the pairing
         instructions so the agent can guide the user

    URL resolution: ``KILN_API_URL`` env var (operator override, e.g.
    pointing at a self-hosted instance) OR ``https://api.kiln3d.com``
    (the hosted default — paired users hit this without any env config).
    """
    api_url = (os.environ.get("KILN_API_URL") or "").strip() or _HOSTED_KILN_API_URL
    try:
        from kiln.auth_session import resolve_api_bearer

        resolved = resolve_api_bearer()
    except Exception:
        resolved = None
    if resolved is None:
        # Resolver unreachable (broken install).  Fall back to the raw
        # stored token rather than claiming an unpaired account at a
        # machine that plainly has a session on disk.
        bearer = (
            os.environ.get("KILN_LICENSE_KEY", "").strip()
            or _raw_paired_access_token()
        )
    elif resolved.token:
        bearer = resolved.token
    elif resolved.state == "needs_signin":
        # A session exists but its refresh token was rejected —
        # distinct from never having paired, and the fix is one
        # command.  Without this branch the stale bearer used to
        # ride to the server and come back as an unexplained 401
        # or a silent free-tier downgrade.
        return {
            "status": "error",
            "error": resolved.detail,
            "code": "KILN_SESSION_EXPIRED",
            "tool": tool_name,
            **signin_hint_fields(),
        }
    else:
        bearer = ""
    if not bearer and tool_name in _ANONYMOUS_OK_TOOLS:
        # Telling us Kiln is broken must not require an account.  Everything
        # else here can wait for a sign-in; a bug report cannot, because the
        # install least able to pair is the one with the most to report — and
        # a first session going badly is exactly when the account wall lands.
        # The pipeline has always accepted an anonymous report; the two doors
        # to it were shut, this one locally and /api/tools/* behind auth.  So
        # this call skips the wall and goes to the public intake instead.
        return _anonymous_api_call(tool_name, **kwargs)
    if not bearer:
        # The most-hit refusal in the product, and until now the only one
        # that recorded nothing: it returns here without ever reaching a
        # server, so no server-side counter could see it.  Best-effort.
        try:
            from kiln.daily_stats import record_account_wall

            record_account_wall(tool_name)
        except Exception:
            pass
        required_tier = _PRO_TOOL_TIERS.get(tool_name, "")
        allowance = _PRO_TOOL_QUOTA.get(tool_name)
        # Two audiences, two fields — the same split ``_tier_required_error``
        # uses in kiln-pro.  ``error`` is read by a PERSON: it says what they
        # reached for and what it costs them to continue, and contains no
        # command, because a person who wanted a textured coaster should not
        # be handed a terminal.  Both halves come from
        # ``kiln.tiers_and_terms`` — the whole tool surface shares one
        # definition of them now, so this refusal cannot drift away from the
        # thirteen others that say the same thing.
        #
        # This used to read "pair a Kiln account, run `python3 -m kiln pair
        # <code>`" — and a test asserted that exact string, so the worst copy
        # in the product was the one line nobody could fix by accident.
        payload = {
            "status": "error",
            "error": account_required_message(
                tool_name, tier=required_tier, allowance=allowance,
            ),
            "code": "KILN_ACCOUNT_NOT_PAIRED",
            "tool": tool_name,
            "required_tier": required_tier or "free",
            "upgrade_url": "https://kiln3d.com/pricing",
            **signin_hint_fields(),
        }
        # Machine-readable twin of the sentence, for a caller that would rather
        # render the allowance its own way than parse prose.  Omitted entirely
        # when unknown — an absent key cannot be misread as "no allowance".
        if allowance:
            payload["quota"] = dict(allowance)
        return payload

    # Bug reports carry local crash evidence.  This forwarder is the ONE
    # code path every report_issue call from a kiln3d-only install takes,
    # so the attach lives here — after the bearer check (a refused call
    # reads no log), before the payload is serialized.
    if tool_name == "report_issue":
        kwargs = _with_local_log_tail(kwargs)

    import json
    import urllib.error
    import urllib.request

    from kiln.api_device import device_fingerprint_headers
    from kiln.version_check import _current_version
    try:
        headers = {"Content-Type": "application/json"}
        headers["Authorization"] = f"Bearer {bearer}"
        # Name this device so the hosted activation cap can count it: a
        # license-key bearer on a metered tool is metered per-device, and
        # the server rejects a card-less license-bearer call once the cap
        # is enforced.  Harmless on the paired-OAuth (JWT) path.
        headers.update(device_fingerprint_headers())
        # Announce our version so the hosted server can apply a minimum-version
        # floor (e.g. force an upgrade for a release with new terms / fixes).
        # A client that never sends this is treated as below the floor.
        headers["X-Kiln-Client-Version"] = _current_version()
        req = urllib.request.Request(
            f"{api_url.rstrip('/')}/api/tools/{tool_name}",
            data=json.dumps(kwargs).encode() if kwargs else None,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # Preserve the server's own error body when present — it usually
        # carries a structured ``code`` + ``error`` the agent can act on
        # (e.g. tier-gate denials, quota-exhaustion messages).
        try:
            body = json.loads(exc.read().decode("utf-8"))
            if isinstance(body, dict):
                return body
        except Exception:
            pass
        return {
            "status": "error",
            "error": f"Kiln API rejected '{tool_name}' (HTTP {exc.code}).",
            "code": "KILN_API_HTTP_ERROR",
            "tool": tool_name,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": f"Failed to reach Kiln server: {exc}",
            "code": "SERVER_UNREACHABLE",
        }


def _register_pro_tool_stubs(mcp_instance) -> None:
    """Register stub tools for pro features from the tool manifest.

    Reads ``pro_tool_manifest.json`` (generated by kiln-pro, bundled with
    public Kiln releases) and creates lightweight proxy stubs.  This lets
    agents and users DISCOVER all pro tools — product generators, print
    intelligence, cloud sync, decoration, etc. — even when kiln-pro isn't
    installed.

    Each stub either:
    - Proxies to the REST API server (if ``KILN_API_URL`` is set)
    - Returns a helpful setup message explaining how to access the feature

    Adding a new pro tool to kiln-pro's manifest automatically makes it
    discoverable here with zero additional code.
    """
    import inspect as _inspect
    import json

    manifest_path = Path(__file__).parent / "pro_tool_manifest.json"
    if not manifest_path.exists():
        logger.debug("No pro tool manifest at %s — no stubs registered", manifest_path)
        return

    try:
        manifest = json.loads(manifest_path.read_text())
    except Exception as exc:
        logger.warning("Failed to load pro tool manifest: %s", exc)
        return

    tools = manifest.get("tools", [])
    if not tools:
        return

    # JSON Schema type → Python type for signature reconstruction
    _type_map = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }

    registered = 0
    for tool_def in tools:
        name = tool_def.get("name", "")
        if not name or name.startswith("_"):
            continue

        description = tool_def.get("description", name)
        # Say what this costs, in the one place an agent always reads.
        # An agent that knows the tier can tell the user "that needs
        # Pro" before spending a call to find out.
        tier = str(tool_def.get("tier") or "").strip().lower()
        if tier and tier != "free":
            _PRO_TOOL_TIERS[name] = tier
            # Only if the manifest did not already say it.  The generator
            # writes "Requires Kiln Business. / Upgrade: <url>" into the
            # description itself, so appending unconditionally made every paid
            # tool state its paywall TWICE, in two wordings, to the agent whose
            # job is to relay it — the one sentence that has to read cleanly.
            if f"requires kiln {tier}" not in description.lower():
                description = (
                    f"{description}\n\n"
                    f"Requires Kiln {tier.capitalize()}. "
                    f"Pricing: https://kiln3d.com/pricing"
                )
        # Metered tools carry their real monthly allowance; unmetered ones
        # carry no block at all, and get no entry, so the account wall can
        # only ever state a number the server actually charges.
        quota = tool_def.get("quota")
        if isinstance(quota, dict) and quota:
            _PRO_TOOL_QUOTA[name] = quota
        params_schema = tool_def.get("parameters", {})

        # Build the stub function.  Closures capture `name` by reference,
        # so we use a factory to freeze the value.
        def _make_stub(_name: str):
            def _stub(**kwargs):
                return _pro_api_call(_name, **kwargs)
            return _stub

        stub = _make_stub(name)

        # Reconstruct a typed Python signature from the JSON Schema so
        # that FastMCP generates the correct tool schema for clients.
        properties = params_schema.get("properties", {})
        required_set = set(params_schema.get("required", []))

        sig_params = []
        for pname, pschema in properties.items():
            # Resolve type — handle anyOf (Optional) by picking first
            # concrete type.
            json_type = pschema.get("type", "string")
            if json_type not in _type_map and "anyOf" in pschema:
                for variant in pschema["anyOf"]:
                    if variant.get("type") in _type_map:
                        json_type = variant["type"]
                        break

            py_type = _type_map.get(json_type, str)
            is_required = pname in required_set

            if is_required:
                default = _inspect.Parameter.empty
            elif "default" in pschema:
                default = pschema["default"]
            else:
                default = None

            sig_params.append(
                _inspect.Parameter(
                    pname,
                    _inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=default,
                    annotation=py_type,
                )
            )

        stub.__signature__ = _inspect.Signature(
            sig_params, return_annotation=dict,
        )
        stub.__name__ = name
        stub.__qualname__ = name
        stub.__doc__ = description
        stub.__module__ = "kiln.pro_stubs"

        try:
            mcp_instance.tool()(stub)
            registered += 1
        except Exception as exc:
            logger.debug("Failed to register stub %s: %s", name, exc)

    logger.info(
        "Registered %d pro tool stubs from manifest (%d categories)",
        registered,
        len(manifest.get("categories", {})),
    )


def _ensure_internal_tool_plugins_registered() -> None:
    """Register internal MCP tool plugins; idempotent and self-healing.

    Tool-schema generation, agent-loop tool discovery, and other in-process
    callers import :mod:`kiln.server` without running :func:`main`. Those
    paths still need the plugin-backed tools to exist on the shared MCP
    instance, or they get a false, incomplete capability surface.

    **Self-healing behaviour for kiln-pro plugins.**  This function is
    auto-called once at the bottom of :mod:`kiln.server` module load
    (the line right above "Backward-compatible re-exports").  When that
    auto-call fires *during* the module's own load, the kiln-pro plugin
    discovery can silently produce zero registered tools — a circular-
    import / FastMCP-state interaction that bites because most kiln-pro
    plugins do ``import kiln.server`` lazily inside their ``register()``
    methods, and kiln.server is only partially loaded at that moment.

    Rather than papering over the silent failure, this function detects
    it (post-call check: are any ``kiln_pro.*`` tools actually in the
    registry?) and *leaves the success flag False so the next call
    retries*.  By the time any caller invokes the function a second time
    — explicitly, e.g. from ``kiln_pro.generate_manifest`` or the REST
    server's startup — the import chain has settled and the retry
    succeeds.  The first call still registers the kiln-side plugins
    (which is load-order safe), so the partial-state window only
    affects kiln-pro tools.

    Terminal states (flag set True, no further retry):
      * kiln-pro tools registered successfully
      * kiln-pro not installed (ImportError → stubs registered)
      * kiln-pro raised a non-ImportError during registration (logged
        at WARNING; retrying wouldn't help)
    """
    global _INTERNAL_TOOL_PLUGINS_REGISTERED
    if _INTERNAL_TOOL_PLUGINS_REGISTERED:
        return

    register_all_plugins(
        _DedupingToolRegistrationProxy(mcp),
        plugin_package="kiln.plugins",
    )

    # Load kiln-pro plugins if kiln-pro is installed (paid users only).
    # This discovers tools like set_speed_percent, batch_decorate, etc.
    try:
        import kiln_pro  # noqa: F401

        register_all_plugins(
            _DedupingToolRegistrationProxy(mcp),
            plugin_package="kiln_pro.plugins",
        )

        # Verify the registration actually populated the registry.
        # When this function is called mid-kiln.server-load, kiln-pro
        # plugin discovery can silently produce zero registered tools.
        # In that case, leave the flag False so the next call retries.
        has_pro = any(
            (getattr(t.fn, "__module__", "") or "").startswith("kiln_pro")
            for t in mcp._tool_manager.list_tools()
        )
        if has_pro:
            logger.info("kiln-pro plugins loaded successfully")
        else:
            # Silent zero-registration during the mid-load window.  Do
            # NOT log at WARNING here — the situation is benign as long
            # as some later call retries (which it will, because the
            # flag stays False).  DEBUG is enough for diagnosis.
            logger.debug(
                "kiln-pro plugin registration produced 0 tools "
                "(likely mid-kiln.server-load) — deferring; next call "
                "to _ensure_internal_tool_plugins_registered() will retry"
            )
            return  # NOT a bug — leaves _INTERNAL_TOOL_PLUGINS_REGISTERED False
    except ImportError:
        # kiln-pro not installed — register lightweight stubs so agents
        # and users can DISCOVER pro tools and call them via the REST API.
        _register_pro_tool_stubs(mcp)
    except Exception as exc:
        # Genuine error during registration (not the silent zero-
        # registration above).  Retrying won't help; log + commit to
        # the terminal state so we don't loop forever.
        logger.warning("Failed to load kiln-pro plugins: %s", exc)

    _INTERNAL_TOOL_PLUGINS_REGISTERED = True


def _ensure_pro_plugins_registered() -> None:
    """Explicit alias for callers that want to be defensive about pro plugins.

    Now redundant with the self-healing logic baked into
    :func:`_ensure_internal_tool_plugins_registered` — but kept as a
    documented entry point for callers (manifest generators, REST tool
    discovery, agent skill listings) that want the *intent* to be
    visible at the call site.

    Safe to call multiple times.  Idempotent.  No-op for free-tier
    callers (no kiln-pro installed).
    """
    _ensure_internal_tool_plugins_registered()


#: Hard ceiling on how long a signal-driven shutdown may take before the
#: process is ended regardless.  Generous: with the heater watchdog's
#: interruptible doze every stop below completes in well under a second.
_SHUTDOWN_DEADLINE_S = 10.0


def _graceful_shutdown(
    hard_exit=os._exit, deadline_s: float = _SHUTDOWN_DEADLINE_S
) -> None:
    """Stop background services, then END the process — guaranteed.

    The signal path must finish with ``os._exit``, never ``sys.exit``.
    ``sys.exit`` raises SystemExit inside whatever frame the signal
    interrupted — the asyncio selector, for ``mcp.run()`` — so the event
    loop unwinds abnormally and anyio never sends its worker threads
    their shutdown command.  Those workers are non-daemon and block
    forever on ``queue.get()``, so interpreter shutdown then joins them
    forever: the process survives its own exit, immortally.  Measured
    live 2026-08-09 — SIGTERM ran this cleanup, ``sys.exit(0)`` fired,
    and the process sat in ``threading._shutdown`` indefinitely; on one
    machine 10 of 10 accumulated servers were in exactly that state.
    ``parent_watchdog`` learned the same lesson for its own exit path.

    Mechanics: a daemon dead-man timer arms FIRST, so a stop that
    wedges (a network-blocked adapter call, a stuck join) can delay
    death by at most *deadline_s*; each stop runs in its own try/except
    so one failure cannot skip the rest; and the exit call sits in a
    ``finally`` so there is no path out of this function that leaves
    the process alive.  ``hard_exit`` is injectable for tests only.
    """
    killer = threading.Timer(deadline_s, lambda: hard_exit(0))
    killer.daemon = True
    killer.start()
    stops = (
        lambda: _get_scheduler().stop(),
        lambda: _get_webhook_mgr().stop(),
        lambda: _get_heater_watchdog().stop(),
        lambda: _get_stream_proxy().stop(),
        lambda: _get_cloud_sync() is not None and _get_cloud_sync().stop(),
    )
    try:
        for stop in stops:
            try:
                stop()
            except Exception as exc:
                logger.debug("shutdown: a service stop failed: %s", exc)
        for wid in list(_watchers):
            try:
                _watchers.pop(wid).stop()
            except Exception as exc:
                logger.debug(
                    "Failed to stop watcher %s during shutdown: %s", wid, exc
                )
    finally:
        hard_exit(0)


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

    # Kick a non-blocking PyPI update check so a "new version available"
    # nudge can surface in the instructions + get_started/kiln_health this
    # session.  Daemon thread; warms ~/.kiln/update_check.json and exits.
    try:
        from kiln.version_check import kick_background_check

        kick_background_check()
    except Exception:  # noqa: BLE001 -- never block startup on the nudge
        pass

    # Loud, unmissable banner telling the user which config source the
    # server is actually using.  The scenario we want to rule out: a
    # stale env var (e.g. leftover in .mcp.json, a wrapper shell, or an
    # inherited os.execv env) silently shadows ~/.kiln/config.yaml.
    # Editing the YAML file looks like it does nothing, and the printer
    # rejects auth with no hint why.  Emitting the source loudly at
    # startup turns a half-day debugging session into a one-line check.
    logger.info("Kiln printer config source: %s", _PRINTER_CONFIG_SOURCE)

    # Surface env-vs-YAML disagreement as a warning regardless of who won.
    # When YAML wins (the default) we still want the user to know that
    # stale env vars are hanging around in their process tree — silent
    # precedence swallowing would be the new footgun if we didn't tell
    # them.  When env wins (explicit override) the warning doubles as a
    # reminder that their YAML file is being shadowed on purpose.
    try:
        env_host = os.environ.get("KILN_PRINTER_HOST", "")
        env_key = os.environ.get("KILN_PRINTER_API_KEY", "")
        env_serial = os.environ.get("KILN_PRINTER_SERIAL", "")
        if env_host:
            from kiln.cli.config import _read_config_file as _read_yaml
            from kiln.cli.config import get_config_path as _get_cfg_path

            _yaml = _read_yaml(_get_cfg_path()) or {}
            _active = _yaml.get("active_printer") or "default"
            _printers = _yaml.get("printers") or {}
            _yaml_printer = (
                _printers.get(_active, {}) or _printers.get("default", {}) or {}
            )
            if _yaml_printer:
                yaml_key = str(
                    _yaml_printer.get("access_code")
                    or _yaml_printer.get("api_key")
                    or ""
                )
                yaml_host = str(_yaml_printer.get("host", ""))
                yaml_serial = str(_yaml_printer.get("serial", ""))
                mismatches: list[str] = []
                if env_key and yaml_key and env_key != yaml_key:
                    mismatches.append(
                        f"api_key (env={_key_fingerprint(env_key)}, "
                        f"yaml={_key_fingerprint(yaml_key)})"
                    )
                if yaml_host and env_host != yaml_host:
                    mismatches.append(f"host (env={env_host}, yaml={yaml_host})")
                if yaml_serial and env_serial and env_serial != yaml_serial:
                    mismatches.append(
                        f"serial (env={env_serial}, yaml={yaml_serial})"
                    )
                if mismatches:
                    if _PRINTER_CONFIG_SOURCE.startswith("~/.kiln/config.yaml"):
                        logger.warning(
                            "Stale KILN_PRINTER_* env vars detected in the "
                            "MCP process environment; YAML is winning as "
                            "designed, but you may want to clear them to "
                            "avoid confusion.  Mismatch: %s",
                            "; ".join(mismatches),
                        )
                    else:
                        logger.warning(
                            "Printer config MISMATCH between env vars and "
                            "~/.kiln/config.yaml: %s. Env vars are winning "
                            "(KILN_PRINTER_CONFIG_IGNORE_YAML=1 is set or "
                            "YAML is empty); your config.yaml edits are "
                            "being ignored.  Unset the env vars to switch "
                            "to YAML.",
                            "; ".join(mismatches),
                        )
    except Exception:  # noqa: BLE001 — warning is best-effort
        pass

    # Auto-register printers so the scheduler can dispatch jobs, fleet
    # queries work, and tools that accept a printer_name argument can
    # find them by name without requiring an explicit register_printer
    # call.  config.yaml is the source of truth: we register every
    # entry it declares (not just the env-resolved default).  Any
    # failure is logged at WARNING level so a silently-empty registry
    # can never again masquerade as a healthy startup.
    _registry = _get_registry()
    if _PRINTER_HOST and _registry.count == 0:
        try:
            adapter = _get_adapter()
            _registry.register("default", adapter)
            logger.info(
                "Auto-registered 'default' printer from %s",
                _PRINTER_CONFIG_SOURCE.split(" (", 1)[0],
            )
        except Exception as exc:
            logger.warning(
                "Could not auto-register 'default' printer from %s: %s. "
                "Tools that take a printer_name will miss until you call "
                "register_printer() or fix the config.",
                _PRINTER_CONFIG_SOURCE.split(" (", 1)[0],
                _sanitize_log_msg(str(exc)),
            )

    # Also register any *additional* printers declared in config.yaml
    # beyond the env-resolved default.  Missing entries here aren't
    # fatal — _resolve_adapter() still lazy-loads on miss — but
    # eagerly registering them makes fleet_status and similar
    # directory-style queries correct out of the box.
    _config_printers = _read_config_printers()
    for _name, _entry in _config_printers.items():
        if _name in _registry:
            continue
        try:
            _adapter = _build_adapter_from_config_entry(_name, _entry)
            _registry.register(_name, _adapter)
            logger.info("Auto-registered printer %r from ~/.kiln/config.yaml", _name)
        except Exception as _exc:  # noqa: BLE001 — surface but don't crash
            logger.warning(
                "Could not auto-register config.yaml printer %r: %s. "
                "Printer remains listed in config but is not in the live "
                "registry — tools will lazy-build it on first use.",
                _name,
                _sanitize_log_msg(str(_exc)),
            )

    # Auto-register marketplace adapters from env credentials
    _init_marketplace_registry()
    if _marketplace_registry.count > 0:
        logger.info("Marketplace sources: %s", ", ".join(_marketplace_registry.connected))

    # Subscribe bed level manager to job events
    _get_bed_level_mgr().subscribe_events()

    # Discover and activate third-party plugins (entry-point based)
    _get_plugin_mgr().discover()
    _get_plugin_mgr().activate_all(
        PluginContext(
            event_bus=_get_event_bus(),
            registry=_get_registry(),
            queue=_get_queue(),
            mcp=mcp,
            db=get_db(),
        )
    )

    # Load internal tool plugins from kiln/plugins/.
    # Tools are being migrated to kiln/plugins/ for modularity — each
    # plugin module registers its own tools via the ToolPlugin protocol.
    # See kiln/plugins/marketplace_tools.py for the migration pattern.
    _ensure_internal_tool_plugins_registered()

    # Rebuild MCP instructions now that config, printers, marketplaces,
    # and plugins are all loaded.  This replaces the static fallback with
    # a context-aware summary of the user's actual capabilities.
    # ``instructions`` is a read-only property on the server class, so the
    # compat helper writes to the lowlevel server object (whose attribute
    # name differs across SDK majors).
    set_instructions(mcp, _build_instructions())

    # Initialise cloud sync from saved config
    _saved_sync = get_db().get_setting("cloud_sync_config")
    if _saved_sync:
        import json as _json

        try:
            _cs = CloudSyncManager(
                db=get_db(),
                event_bus=_get_event_bus(),
                config=SyncConfig.from_dict(_json.loads(_saved_sync)),
            )
            _set_cloud_sync(_cs)
            _cs.start()
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

    # Anonymous daily heartbeat (one ping per day, daemon thread).  The
    # scheduler — not the one-shot — so a server kept alive across days
    # reports each day instead of only its startup day.
    try:
        from kiln.heartbeat import start_heartbeat_scheduler

        start_heartbeat_scheduler()
    except Exception:
        pass  # Never let telemetry failure affect startup

    # Wire billing alert manager (lazy init on first access).
    try:
        _get_billing_alert_mgr()
    except Exception:
        logger.debug("Billing alert manager not initialized", exc_info=True)

    # Start fulfillment order monitor if fulfillment is available.
    try:
        monitor = _get_fulfillment_monitor()
        if monitor is not None:
            monitor.start()
    except Exception:
        logger.debug("Fulfillment monitor not started", exc_info=True)

    # Start background services
    _get_scheduler().start()
    _get_webhook_mgr().start()
    _get_heater_watchdog().start()
    logger.info("Kiln scheduler, webhook delivery, and heater watchdog started")

    # Graceful shutdown handler
    def _shutdown_handler(signum: int, frame: Any) -> None:
        try:
            sig_name = signal.Signals(signum).name
        except (ValueError, AttributeError):
            sig_name = f"signal {signum}"
        logger.info("Received %s — shutting down gracefully...", sig_name)
        _graceful_shutdown()

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    # Atexit as fallback
    atexit.register(_get_scheduler().stop)
    atexit.register(_get_webhook_mgr().stop)
    atexit.register(_get_heater_watchdog().stop)
    atexit.register(_get_stream_proxy().stop)
    if _get_cloud_sync() is not None:
        atexit.register(_get_cloud_sync().stop)

    def _stop_all_watchers() -> None:
        for wid in list(_watchers):
            try:
                _watchers.pop(wid).stop()
            except Exception as exc:
                logger.debug("Failed to stop watcher %s during atexit: %s", wid, exc)

    atexit.register(_stop_all_watchers)

    # One-line identity banner on stderr.  MCP owns stdout (JSON-RPC);
    # stderr is free.  Silent startup is a bug: a paid user whose CLI
    # isn't signed in has no visible signal that their tier isn't
    # loaded — they only find out when a pro tool call fails with
    # TIER_REQUIRED.  One line at startup turns that into a pre-flight
    # check the user sees BEFORE Claude Desktop even shows the tool
    # results panel.
    _print_startup_banner()

    # Orphan watchdog: if the MCP host (Claude Code, Claude Desktop,
    # ...) dies without closing stdin, this process gets adopted by
    # init/launchd and ``mcp.run()`` blocks forever waiting for
    # JSON-RPC traffic that will never arrive.  The watchdog polls
    # PPID every 30s and exits cleanly when the original parent is
    # gone.  No-op when the process was started under a supervisor
    # (PPID=1 from the start).  See ``parent_watchdog.py`` for the
    # rationale + the ``KILN_DISABLE_ORPHAN_WATCHDOG`` escape hatch.
    from kiln.parent_watchdog import start_parent_watchdog
    start_parent_watchdog()

    # Sibling pile-up check: the watchdog above only fires when a
    # parent DIES; the common leak is a parent that lives on as an
    # idle husk after its session ends, keeping its server alive.
    # Nothing external reaps those, so each new server checks the
    # process table at boot and warns on stderr when the machine has
    # accumulated more ``kiln serve`` processes than any plausible
    # number of live sessions.  Shared detector: kiln.serve_siblings
    # (also wired into health_check / kiln_health / get_started /
    # ``kiln doctor``).
    from kiln.serve_siblings import log_sibling_warning_at_startup
    log_sibling_warning_at_startup()

    # Community-contribution self-heal: flush any contributions a previous
    # session queued but couldn't send (offline / crash).  Silent,
    # opt-in-gated, best-effort on a daemon thread so it never delays boot
    # or blocks on the network — the durability half of "opt in once, then
    # it just works."
    try:
        from kiln.community_sync import community_opt_in_enabled
        if community_opt_in_enabled():
            from kiln import community_outbox
            threading.Thread(
                target=community_outbox._safe_drain,
                daemon=True,
                name="kiln-community-outbox-startup-drain",
            ).start()
    except Exception:
        logger.debug("community outbox startup drain skipped", exc_info=True)

    # The inline 3D stage.  Runs HERE, after every tool and plugin has
    # registered, because it stamps the mesh-producing tools — done any
    # earlier it would stamp an empty registry.
    try:
        from kiln import local_stage, stage_cache

        # Pull the stage document now, on a daemon thread, so the first
        # design of a session finds it already cached instead of waiting on
        # a download with an empty panel.
        stage_cache.warm()
        local_stage.install(mcp)
    except Exception:
        logger.debug("inline stage not installed", exc_info=True)

    # The update offer, on the FIRST tool result of the session.  The
    # server instructions already carry it, but that is one sentence in
    # a long preamble read once on connect — and a session that never
    # calls get_started sees nothing else.  Riding a real result puts it
    # in the agent's context while it is composing a reply.  Installed
    # here, after the stage, so both lowlevel wrappers compose in a
    # deterministic order.
    try:
        from kiln import update_nudge

        update_nudge.install(mcp)
    except Exception:
        logger.debug("update nudge not installed", exc_info=True)

    mcp.run()


def _print_startup_banner() -> None:
    """Emit a single identity line on stderr so anyone launching
    `kiln serve` — whether via Claude Desktop, Claude Code, a custom
    MCP client, or by hand — sees at a glance whether this process is
    signed in and which tier it's running as.

    Output shapes:

        ✓ Kiln MCP. Signed in as adam@example.com (Pro).
        ⚠ Kiln MCP. Not signed in — run `kiln signin` to connect your Kiln tier.

    Never raises: if tier resolution throws for any reason, we fall
    through to the "not signed in" shape rather than crashing the
    server at launch.  A broken banner beats a broken server.
    """
    try:
        import sys

        tier_label = "Free"
        email = ""
        try:
            # Tier resolution lives in kiln-pro when installed; fall
            # back to the free-tier stub (always pip-available).  Read
            # the ~/.kiln/auth_tokens.json directly for email — that's
            # the file the CLI populates on `kiln signin` / `kiln pair`.
            current_tier = get_tier()
            tier_value = getattr(current_tier, "value", str(current_tier))
            tier_label = str(tier_value).title()
            try:
                auth_home = os.environ.get("KILN_AUTH_HOME") or str(Path.home())
                tokens_path = Path(auth_home) / ".kiln" / "auth_tokens.json"
                if tokens_path.is_file():
                    import json as _json
                    data = _json.loads(tokens_path.read_text(encoding="utf-8"))
                    email = str(data.get("email") or "")
            except Exception:
                pass
        except Exception:
            # Resolution failed — treat as FREE for the banner.
            pass

        if email and tier_label.lower() != "free":
            msg = f"\u2713 Kiln MCP. Signed in as {email} ({tier_label})."
        elif email:
            msg = f"\u2713 Kiln MCP. Signed in as {email} (Free)."
        else:
            msg = (
                "\u26a0 Kiln MCP. Not signed in \u2014 run `kiln signin` "
                "or `kiln pair <code>` to connect your Kiln tier."
            )

        # Print directly to stderr bypassing the logger — loggers can
        # be silenced by env vars or JSON formatters, and this is a
        # human affordance, not a log line.  Users piping stderr to a
        # file still see it; Claude Desktop surfaces stderr in its
        # MCP server diagnostics panel.
        print(msg, file=sys.stderr, flush=True)
    except Exception:
        # Never let banner output break startup.
        pass


# ---------------------------------------------------------------------------
# Extended toolset — material substitution, recovery, health monitoring,
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
    """Find substitute filament materials when your preferred material is unavailable (returns ranked list).

    Checks a built-in knowledge base of FDM filament compatibility and returns
    ranked alternatives with trade-off descriptions.  For a single best match,
    use ``get_best_material_substitute`` instead.

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
        sub_dicts = [s.to_dict() for s in subs]
        # Food-safety preservation (kiln-pro feature; free-tier silently
        # skips): if the source material is food_safe=yes, mark any
        # substitute that ISN'T food_safe=yes as a safety regression so
        # callers don't silently swap a pet bowl's PETG for ABS.
        try:
            from kiln_pro.material_safety import assess_food_safety  # noqa: WPS433
        except ImportError:
            assess_food_safety = None  # type: ignore[assignment]
        food_safety_check: dict | None = None
        if assess_food_safety is not None:
            source_verdict = assess_food_safety(material)
            if source_verdict["food_safe"] == "yes":
                regressions: list[str] = []
                for d in sub_dicts:
                    sub_name = (
                        d.get("substitute_material")
                        or d.get("substitute")
                        or d.get("material")
                        or d.get("name")
                    )
                    if not sub_name:
                        continue
                    sub_verdict = assess_food_safety(str(sub_name))
                    sub_food_safe = sub_verdict["food_safe"]
                    d["food_safe"] = sub_food_safe
                    if sub_food_safe != "yes":
                        regressions.append(str(sub_name))
                food_safety_check = {
                    "source_food_safe": "yes",
                    "regressions": regressions,
                    "warning": (
                        "Source material is food_safe=yes but some substitutes are "
                        f"NOT: {regressions}. Do not silently swap for food-contact "
                        "products (pet bowls, kitchen items). Filter or disclose."
                    ) if regressions else None,
                }
        result = {
            "success": True,
            "material": material,
            "substitutes": sub_dicts,
            "count": len(sub_dicts),
        }
        if food_safety_check is not None:
            result["food_safety"] = food_safety_check
        return result
    except Exception as exc:
        logger.exception("Error in find_material_substitute")
        return _error_dict(f"Failed to find material substitutes: {exc}", code="SUBSTITUTION_ERROR")


@mcp.tool()
def get_best_material_substitute(material: str) -> dict:
    """Get the single best substitute for a filament material (quick shortcut).

    Returns one top-ranked alternative. For a full ranked list with trade-off
    details and filtering, use ``find_material_substitute`` instead.

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
def get_material_properties(material_id: str) -> dict:
    """Get a material's public safety and printing-property profile.

    Returns the public thermal, chemical-safety, and process-design floor.
    Deeper engineering questions are answered one at a time by kiln-pro
    (https://kiln3d.com).

    Args:
        material_id: Material key (e.g. ``"petg"``, ``"tpu"``, ``"cf_pla"``).
            Case-insensitive.
    """
    if err := _check_auth("intel"):
        return err
    try:
        from kiln.design_intelligence import (
            get_public_material_profile,
            list_public_material_profiles,
        )

        profile = get_public_material_profile(material_id)
        if profile is None:
            available = [p.material_id for p in list_public_material_profiles()]
            return _error_dict(
                f"Unknown material '{material_id}'. Available: {', '.join(available)}",
                code="NOT_FOUND",
            )
        result = {"success": True, "material": profile.to_dict()}
        # Per-reagent durability (will a printed part survive diesel /
        # acetone / bleach / vinegar / UV …) is served one query at a
        # time by check_chemical_resistance — this bulk response carries
        # only the free safety floor, never the curated matrix.
        result["chemical_resistance"] = {
            "per_reagent_tool": "check_chemical_resistance",
            "note": "Safety warnings are always free; cited survival "
            "verdicts are a kiln-pro feature — https://kiln3d.com/pricing.",
        }
        return result
    except Exception as exc:
        logger.exception("Error in get_material_properties")
        return _error_dict(
            f"Failed to get material properties: {exc}",
            code="INTERNAL_ERROR",
        )


@mcp.tool()
def check_printer_material_support(
    printer_id: str,
    material_id: str,
) -> dict:
    """Check if a printer supports one specific material.

    Returns compatibility status (``"compatible"`` or ``"needs_upgrade"``),
    required hardware upgrades (enclosure, hardened nozzle, dry box, etc.),
    and material-specific notes for the printer.

    **See also:** ``check_printer_material_compatibility`` for the same
    check with design-intelligence context and alternative suggestions.

    Args:
        printer_id: Printer model identifier (e.g. ``"bambu_a1"``,
            ``"ender3"``, ``"prusa_mk4"``).
        material_id: Material to check (e.g. ``"petg"``). Required so this
            hosted surface cannot enumerate a printer's complete matrix.
    """
    if err := _check_auth("intel"):
        return err
    if not isinstance(material_id, str) or not material_id.strip():
        return _error_dict(
            "Provide one material_id to check.",
            code="INVALID_INPUT",
        )
    try:
        from kiln.design_intelligence import (
            check_printer_material_compatibility,
            list_compatibility_printers,
        )

        material_id = material_id.strip()
        report = check_printer_material_compatibility(printer_id, material_id)
        if report is None:
            available = list_compatibility_printers()
            return _error_dict(
                f"No compatibility data for '{printer_id}'. Available printers: {', '.join(available)}",
                code="NOT_FOUND",
            )
        result: dict[str, Any] = {
            "success": True,
            "printer_id": report.printer_id,
            "materials": report.materials,
        }
        mat_lower = material_id.lower()
        if mat_lower in report.materials:
            mat_info = report.materials[mat_lower]
            result["summary"] = (
                f"{material_id.upper()} is "
                f"{mat_info.get('status', 'unknown')} on {printer_id}"
            )
            if mat_info.get("upgrades_needed"):
                result["summary"] += (
                    f" (needs: {', '.join(mat_info['upgrades_needed'])})"
                )
        else:
            result["summary"] = (
                f"No compatibility data for '{material_id}' on {printer_id}"
            )
        return result
    except Exception as exc:
        logger.exception("Error in check_printer_material_support")
        return _error_dict(
            f"Failed to check printer material support: {exc}",
            code="INTERNAL_ERROR",
        )


@mcp.tool()
def compare_material_properties(
    material_a: str,
    material_b: str,
) -> dict:
    """Compare two materials using a fixed public safety/process field set.

    Use this when deciding between materials for a project (e.g. PLA vs PETG
    for an outdoor bracket) or when switching materials for a reprint. Deeper
    engineering trade-offs are answered one question at a time by kiln-pro
    (https://kiln3d.com).

    Args:
        material_a: First material (e.g. ``"pla"``).
        material_b: Second material (e.g. ``"petg"``).
    """
    if err := _check_auth("intel"):
        return err
    try:
        from kiln.design_intelligence import get_public_material_profile

        prof_a = get_public_material_profile(material_a)
        prof_b = get_public_material_profile(material_b)
        if prof_a is None or prof_b is None:
            missing = material_a if prof_a is None else material_b
            return _error_dict(
                f"Unknown material '{missing}'.",
                code="NOT_FOUND",
            )

        a_id = prof_a.material_id
        b_id = prof_b.material_id

        def _fixed_diff(
            a: dict,
            b: dict,
            fields: tuple[tuple[str, str], ...],
        ) -> dict:
            return {
                output_key: {
                    a_id: a.get(source_key),
                    b_id: b.get(source_key),
                }
                for output_key, source_key in fields
            }

        # Build practical summary
        ta = prof_a.thermal
        tb = prof_b.thermal
        summary_lines: list[str] = []
        temp_a = ta.get("print_temp_range_c", [0, 0])
        temp_b = tb.get("print_temp_range_c", [0, 0])
        if temp_a[0] != temp_b[0]:
            summary_lines.append(
                f"Print temp: {prof_a.display_name} {temp_a[0]}-{temp_a[1]}C "
                f"vs {prof_b.display_name} {temp_b[0]}-{temp_b[1]}C"
            )
        bed_a = ta.get("bed_temp_range_c", [0, 0])
        bed_b = tb.get("bed_temp_range_c", [0, 0])
        if bed_a[0] != bed_b[0]:
            summary_lines.append(
                f"Bed temp: {prof_a.display_name} {bed_a[0]}-{bed_a[1]}C "
                f"vs {prof_b.display_name} {bed_b[0]}-{bed_b[1]}C"
            )
        warp_a = ta.get("warping_tendency", "unknown")
        warp_b = tb.get("warping_tendency", "unknown")
        if warp_a != warp_b:
            summary_lines.append(f"Warping: {prof_a.display_name} {warp_a} vs {prof_b.display_name} {warp_b}")

        return {
            "success": True,
            "materials": [a_id, b_id],
            "thermal": _fixed_diff(
                ta,
                tb,
                (
                    ("print_temp_range_c", "print_temp_range_c"),
                    ("bed_temp_range_c", "bed_temp_range_c"),
                    ("glass_transition_c", "glass_transition_c"),
                    ("heat_deflection_c", "heat_deflection_c"),
                    ("max_service_temp_c", "max_service_temp_c"),
                    ("warping_tendency", "warping_tendency"),
                ),
            ),
            "design_limits": _fixed_diff(
                prof_a.design_limits,
                prof_b.design_limits,
                (
                    ("min_wall_mm", "min_wall_thickness_mm"),
                    ("max_overhang_deg", "max_unsupported_overhang_deg"),
                    ("max_bridge_mm", "max_bridge_length_mm"),
                    ("min_hole_diameter_mm", "min_hole_diameter_mm"),
                ),
            ),
            "summary": summary_lines,
        }
    except Exception as exc:
        logger.exception("Error in compare_material_properties")
        return _error_dict(
            f"Failed to compare materials: {exc}",
            code="INTERNAL_ERROR",
        )


@mcp.tool()
def build_material_overrides(
    material_id: str,
    printer_id: str | None = None,
) -> dict:
    """Auto-generate slicer override dict for a specific material.

    Combines material thermal data (from the material database) with
    printer-specific tuning (from printer intelligence) to produce a
    ready-to-use JSON override dict for ``reslice_with_overrides`` or
    ``run_reslice_and_print``.

    This is the key tool for material switching — call it to get the
    correct temperatures, speeds, and retraction settings when changing
    from one material to another.

    Example workflow::

        # 1. Get overrides for PETG on your printer
        overrides = build_material_overrides("petg", "bambu_a1")
        # 2. Reslice and print with those overrides
        run_reslice_and_print(model_path, overrides=json.dumps(overrides["overrides"]))

    Args:
        material_id: Target material (e.g. ``"petg"``, ``"tpu"``).
        printer_id: Optional printer model for printer-specific tuning.
            If omitted, uses material database defaults.
    """
    if err := _check_auth("slicer"):
        return err
    try:
        from kiln.design_intelligence import get_material_profile

        profile = get_material_profile(material_id)
        if profile is None:
            return _error_dict(
                f"Unknown material '{material_id}'.",
                code="NOT_FOUND",
            )

        thermal = profile.thermal
        overrides: dict[str, str] = {}

        # Temperature overrides from material database
        temp_range = thermal.get("print_temp_range_c", [])
        if len(temp_range) >= 2:
            # Use midpoint of the recommended range
            mid_temp = (temp_range[0] + temp_range[1]) // 2
            overrides["temperature"] = str(mid_temp)
            # First layer slightly hotter for adhesion
            overrides["first_layer_temperature"] = str(mid_temp + 5)

        bed_range = thermal.get("bed_temp_range_c", [])
        if len(bed_range) >= 2:
            overrides["bed_temperature"] = str((bed_range[0] + bed_range[1]) // 2)
            overrides["first_layer_bed_temperature"] = str(bed_range[1])

        # Material-specific speed/retraction adjustments
        mat_lower = material_id.lower()
        if mat_lower in ("petg", "cf_petg", "petg_cf", "pet_cf", "petg_hf"):
            overrides.setdefault("perimeter_speed", "40")
            overrides.setdefault("retract_length", "4.0")
            overrides.setdefault("retract_speed", "30")
        elif mat_lower == "tpu_85a":
            # Ultra-soft TPU — even slower than standard TPU
            overrides.setdefault("perimeter_speed", "15")
            overrides.setdefault("infill_speed", "15")
            overrides.setdefault("retract_length", "0.8")
            overrides.setdefault("retract_speed", "15")
        elif mat_lower in ("tpu", "tpu_95a"):
            overrides.setdefault("perimeter_speed", "20")
            overrides.setdefault("infill_speed", "20")
            overrides.setdefault("retract_length", "1.0")
            overrides.setdefault("retract_speed", "20")
        elif mat_lower in ("abs", "asa", "asa_plus", "hips"):
            overrides.setdefault("perimeter_speed", "40")
            overrides.setdefault("retract_length", "3.5")
        elif mat_lower in ("nylon", "cf_nylon", "pa6_gf"):
            overrides.setdefault("perimeter_speed", "35")
            overrides.setdefault("retract_length", "4.0")
            overrides.setdefault("retract_speed", "25")
        elif mat_lower in ("polycarbonate", "pc_abs"):
            overrides.setdefault("perimeter_speed", "35")
            overrides.setdefault("retract_length", "3.5")

        # Printer-specific tuning (overrides material defaults if available)
        printer_tuning: dict[str, Any] | None = None
        if printer_id:
            try:
                from kiln.printer_intelligence import get_material_settings

                mp = get_material_settings(printer_id, material_id)
                if mp is not None:
                    printer_tuning = {
                        "hotend_temp": mp.hotend,
                        "bed_temp": mp.bed,
                        "fan_speed": mp.fan,
                        "notes": mp.notes,
                    }
                    # Printer-specific temps override material defaults
                    overrides["temperature"] = str(mp.hotend)
                    overrides["first_layer_temperature"] = str(mp.hotend + 5)
                    overrides["bed_temperature"] = str(mp.bed)
            except (KeyError, ValueError, TypeError):
                pass  # Fall back to material database defaults

        return {
            "success": True,
            "material": material_id,
            "printer_id": printer_id,
            "overrides": overrides,
            "printer_tuning": printer_tuning,
            "notes": (
                f"Slicer overrides for {profile.display_name}"
                + (f" on {printer_id}" if printer_id else "")
                + ". Pass this as the 'overrides' parameter to "
                "reslice_with_overrides or run_reslice_and_print."
            ),
        }
    except Exception as exc:
        logger.exception("Error in build_material_overrides")
        return _error_dict(
            f"Failed to build material overrides: {exc}",
            code="INTERNAL_ERROR",
        )


@mcp.tool()
def reprint_with_material(
    file_path: str,
    material_id: str,
    printer_name: str | None = None,
    printer_id: str | None = None,
    extra_overrides: str | None = None,
    use_ams: bool | None = None,
    ams_mapping: str | None = None,
) -> dict:
    """Reprint a model with a different material — auto-adjusts temperatures,
    speeds, and retraction for the new material.

    One-shot convenience tool: looks up the target material's optimal slicer
    settings, merges any extra overrides you provide, reslices the model,
    runs a safety check, uploads to the printer, and starts the print.

    Use this when you want to reprint an existing model in a different
    material (e.g. PLA → PETG for outdoor durability, or PLA → TPU for
    flexibility). The tool handles all the slicer parameter changes
    automatically.

    Example: "Reprint my grip extension in PETG instead of PLA"::

        reprint_with_material(
            file_path="/path/to/grip_extension.stl",
            material_id="petg",
            printer_name="my_bambu",
            printer_id="bambu_a1",
            use_ams=True,
            ams_mapping="[1]",  # PETG is in AMS slot 1
        )

    Requires PrusaSlicer or OrcaSlicer installed locally.

    Args:
        file_path: Path to the model file (STL, 3MF, STEP, OBJ).
        material_id: Target material (e.g. ``"petg"``, ``"tpu"``).
        printer_name: Registered printer name in fleet. If omitted,
            uses the default printer.
        printer_id: Printer model ID for profile selection
            (e.g. ``"bambu_a1"``, ``"ender3"``).
        extra_overrides: Optional JSON string of additional slicer
            overrides to merge on top of the material defaults
            (e.g. ``'{"fill_density": "30%"}'``).
        use_ams: Enable AMS filament feeding (Bambu printers).
        ams_mapping: JSON string of AMS slot indices (e.g. ``"[1]"``).
            Maps each extruder/filament to an AMS tray position.
    """
    if err := _check_auth("print"):
        return err
    try:
        import json as _json

        # Step 1: Build material-specific overrides
        mat_result = build_material_overrides(material_id, printer_id)
        if not mat_result.get("success"):
            return mat_result

        overrides = dict(mat_result["overrides"])

        # Step 2: Merge extra overrides if provided
        if extra_overrides:
            try:
                extra = _json.loads(extra_overrides)
                if isinstance(extra, dict):
                    overrides.update(extra)
            except _json.JSONDecodeError as exc:
                return _error_dict(
                    f"Invalid JSON in extra_overrides: {exc}",
                    code="VALIDATION_ERROR",
                )

        # Step 3: Delegate to run_reslice_and_print
        result = run_reslice_and_print(
            model_path=file_path,
            printer_name=printer_name,
            printer_id=printer_id,
            overrides=_json.dumps(overrides),
            use_ams=use_ams,
            ams_mapping=ams_mapping,
        )

        # Enrich the result with material context
        if isinstance(result, dict) and result.get("success"):
            result["material"] = material_id
            result["material_overrides_applied"] = overrides
            result["notes"] = (
                f"Resliced and printing with {material_id.upper()} settings. "
                f"Overrides applied: {', '.join(f'{k}={v}' for k, v in overrides.items())}"
            )

        return result
    except Exception as exc:
        logger.exception("Error in reprint_with_material")
        return _error_dict(
            f"Failed to reprint with material: {exc}",
            code="INTERNAL_ERROR",
        )


@mcp.tool()
def smart_reprint(
    file_name: str,
    material_id: str,
    printer_name: str | None = None,
    printer_id: str | None = None,
    search_dirs: str | None = None,
    extra_overrides: str | None = None,
    auto_ams: bool = True,
    brief_id: str = "",
) -> dict:
    """Smart one-shot material-switch reprint — finds the model, detects the
    right AMS slot, adjusts slicer settings, and prints.

    This is the highest-level reprinting tool. Give it a file name (or
    partial name) and a target material, and it handles everything:

    1. **Find the model**: Searches print history for the file name, then
       searches common local directories for the source STL/3MF/STEP file.
    2. **Check AMS**: Reads AMS tray status to find which slot has the
       target material loaded. Auto-selects the matching slot.
    3. **Build overrides**: Generates material-specific slicer overrides
       (temperature, speed, retraction) for the target material.
    4. **Reslice + print**: Reslices the model with new settings and
       starts the print with the correct AMS mapping.

    Example: "Reprint my grip extension in PETG"::

        smart_reprint(
            file_name="grip_extension",
            material_id="petg",
            printer_name="my_bambu",
            printer_id="bambu_a1",
        )

    The tool will find ``grip_extension.stl`` on disk, detect that PETG
    is loaded in AMS slot 1, adjust temps/speeds for PETG, reslice, and
    start printing — all in one call.

    Saved-goal carry-forward: when the source model on disk has a
    ``<file>.intent.json`` sidecar tagged with a saved goal, that
    goal's id is auto-recovered and surfaced as ``brief_id`` in the
    result so downstream ``record_print_outcome`` correctly links the
    reprint back to the goal. Pass ``brief_id="..."`` explicitly to
    override the sidecar derivation (rare — useful for one-off
    re-attributions).

    Args:
        file_name: Full or partial file name to search for (e.g.
            ``"grip_extension"`` or ``"grip_extension.stl"``).
            Searched in print history first, then in local directories.
        material_id: Target material (e.g. ``"petg"``, ``"tpu"``).
        printer_name: Registered printer name in fleet.
        printer_id: Printer model ID for profile selection.
        search_dirs: Optional JSON array of extra directories to search
            for the model file (e.g. ``'["/home/user/models"]'``).
        extra_overrides: Optional JSON string of additional slicer
            overrides (e.g. ``'{"fill_density": "30%"}'``).
        auto_ams: If ``True`` (default), automatically detect AMS slot
            for the target material. Set to ``False`` to skip AMS
            detection (useful for non-Bambu printers).
        brief_id: Optional saved-goal id from ``design_session``.  When
            omitted, the source model's intent sidecar (if any) is read
            and the saved goal's id is derived from its ``generator``
            field — so a reprint of a brief-attached design keeps the
            goal link automatically.  Best-effort: missing kiln-pro or
            missing sidecar silently skips.
    """
    if err := _check_auth("print"):
        return err
    try:
        import glob as _glob
        import json as _json
        import os as _os

        steps_log: list[dict[str, Any]] = []

        # ---------------------------------------------------------------
        # Step 1: Find the source model file
        # ---------------------------------------------------------------
        model_extensions = (".stl", ".3mf", ".step", ".stp", ".obj")
        found_path: str | None = None

        # 1a. Check if file_name is already an absolute path that exists
        if _os.path.isfile(file_name):
            found_path = file_name
            steps_log.append(
                {
                    "step": "find_model",
                    "method": "direct_path",
                    "path": found_path,
                }
            )
        else:
            # 1b. Search common directories
            default_dirs = [
                _os.path.expanduser("~/Downloads"),
                _os.path.expanduser("~/Documents"),
                _os.path.expanduser("~/Desktop"),
                _os.path.expanduser("~/models"),
                _os.path.expanduser("~/3d_prints"),
                "/tmp",
            ]
            if search_dirs:
                try:
                    extra_dirs = _json.loads(search_dirs)
                    if isinstance(extra_dirs, list):
                        default_dirs = [str(d) for d in extra_dirs] + default_dirs
                except _json.JSONDecodeError:
                    pass

            # Strip extension from search name for flexible matching
            base_name = file_name
            for ext in model_extensions:
                if base_name.lower().endswith(ext):
                    base_name = base_name[: -len(ext)]
                    break

            candidates: list[tuple[str, float]] = []
            for search_dir in default_dirs:
                if not _os.path.isdir(search_dir):
                    continue
                for ext in model_extensions:
                    # Exact match
                    exact = _os.path.join(search_dir, f"{base_name}{ext}")
                    if _os.path.isfile(exact):
                        candidates.append((exact, _os.path.getmtime(exact)))
                    # Case-insensitive glob
                    for match in _glob.glob(
                        _os.path.join(search_dir, f"*{base_name}*{ext}"),
                    ):
                        if _os.path.isfile(match):
                            candidates.append((match, _os.path.getmtime(match)))

            if candidates:
                # Pick the most recently modified match
                candidates.sort(key=lambda x: x[1], reverse=True)
                found_path = candidates[0][0]
                steps_log.append(
                    {
                        "step": "find_model",
                        "method": "directory_search",
                        "path": found_path,
                        "candidates_found": len(candidates),
                    }
                )
            else:
                # 1c. Check print history for file name hints
                try:
                    history = print_history(status="completed", limit=50)
                    if history.get("success") and history.get("records"):
                        for rec in history["records"]:
                            rec_name = rec.get("file_name", "")
                            if base_name.lower() in rec_name.lower():
                                steps_log.append(
                                    {
                                        "step": "find_model",
                                        "method": "history_match",
                                        "history_file": rec_name,
                                        "note": "Found in history but source model not on disk",
                                    }
                                )
                                break
                except Exception:
                    pass

        if found_path is None:
            searched = ", ".join(d for d in default_dirs if _os.path.isdir(d))
            return _error_dict(
                f"Could not find model file matching '{file_name}'. "
                f"Searched directories: {searched}. "
                f"Provide the full path or add search_dirs.",
                code="NOT_FOUND",
            )

        # ---------------------------------------------------------------
        # Step 2: AMS slot detection (Bambu printers)
        # ---------------------------------------------------------------
        detected_ams_mapping: str | None = None
        detected_use_ams: bool | None = None
        ams_slot_info: dict[str, Any] | None = None

        if auto_ams:
            try:
                ams_result = ams_status()
                if ams_result.get("success"):
                    mat_lower = material_id.lower()
                    # Map common material IDs to AMS tray_type strings
                    mat_aliases: dict[str, list[str]] = {
                        "pla": ["PLA"],
                        "pla_plus": ["PLA", "PLA+", "PLA-S"],
                        "pla_matte": ["PLA", "PLA-S"],
                        "pla_tough": ["PLA", "PLA-S"],
                        "silk_pla": ["PLA", "Silk PLA"],
                        "wood_pla": ["PLA", "Wood PLA"],
                        "cf_pla": ["PLA-CF", "PLA"],
                        "petg": ["PETG"],
                        "petg_hf": ["PETG", "PETG-HF"],
                        "cf_petg": ["PETG-CF", "PETG"],
                        "petg_cf": ["PETG-CF", "PETG"],
                        "abs": ["ABS"],
                        "asa": ["ASA"],
                        "asa_plus": ["ASA"],
                        "tpu": ["TPU"],
                        "tpu_95a": ["TPU"],
                        "tpu_85a": ["TPU"],
                        "nylon": ["PA", "Nylon"],
                        "cf_nylon": ["PA-CF", "PA"],
                        "pa6_gf": ["PA6-GF", "PA"],
                        "polycarbonate": ["PC"],
                        "pc_abs": ["PC", "PC-ABS"],
                        "hips": ["HIPS"],
                        "pva": ["PVA"],
                        "pet_cf": ["PET-CF"],
                    }
                    expected_types = mat_aliases.get(mat_lower, [mat_lower.upper()])

                    # Scan AMS trays for a matching material
                    best_slot: int | None = None
                    best_remain: int = -1
                    for unit in ams_result.get("units", []):
                        for tray in unit.get("trays", []):
                            tray_type = (tray.get("tray_type") or "").strip()
                            if tray_type in expected_types:
                                remain = tray.get("remain", 0)
                                if remain > best_remain:
                                    best_remain = remain
                                    best_slot = tray.get("slot", 0)
                                    # `remain` is a real reading only for an
                                    # RFID-tagged spool — AMS Lite reports a
                                    # placeholder, flagged by the adapter.
                                    remaining_known = bool(tray.get("remaining_known"))
                                    ams_slot_info = {
                                        "slot": best_slot,
                                        "tray_type": tray_type,
                                        "color": tray.get("tray_color", ""),
                                        "remain_pct": remain if remaining_known else None,
                                    }

                    if best_slot is not None:
                        detected_ams_mapping = _json.dumps([best_slot])
                        detected_use_ams = True
                        steps_log.append(
                            {
                                "step": "ams_detection",
                                "found": True,
                                "slot": best_slot,
                                "tray_type": ams_slot_info["tray_type"] if ams_slot_info else "",
                                "remain_pct": ams_slot_info["remain_pct"] if ams_slot_info else None,
                            }
                        )
                    else:
                        steps_log.append(
                            {
                                "step": "ams_detection",
                                "found": False,
                                "note": (
                                    f"No AMS tray with {material_id.upper()} found. "
                                    "Printing without AMS mapping — load the material "
                                    "in an AMS slot or use the external spool holder."
                                ),
                            }
                        )
            except Exception:
                steps_log.append(
                    {
                        "step": "ams_detection",
                        "found": False,
                        "note": "AMS query failed (non-Bambu printer or not connected)",
                    }
                )

        # ---------------------------------------------------------------
        # Step 2.5: D2 — saved-goal carry-forward.
        # When the caller didn't supply brief_id, try to recover it
        # from the source model's intent sidecar.  Best-effort: kiln-pro
        # not installed / no sidecar / unparseable generator string
        # silently leaves resolved_brief_id None, matching the pre-D2
        # baseline (no goal link on the reprint).
        # ---------------------------------------------------------------
        resolved_brief_id: str | None = brief_id or None
        if resolved_brief_id is None:
            try:
                from kiln_pro.intent_verification import load_intent_sidecar
                intent = load_intent_sidecar(found_path)
                if (
                    intent is not None
                    and isinstance(intent.generator, str)
                    and intent.generator.startswith("design_brief:")
                ):
                    candidate = intent.generator.split(":", 1)[1].strip()
                    if candidate:
                        resolved_brief_id = candidate
                        steps_log.append({
                            "step": "brief_carry_forward",
                            "method": "sidecar_derived",
                            "brief_id": resolved_brief_id,
                        })
            except Exception:
                logger.debug(
                    "smart_reprint: brief carry-forward skipped (best-effort)",
                    exc_info=True,
                )

        # ---------------------------------------------------------------
        # Step 3: Delegate to reprint_with_material
        # ---------------------------------------------------------------
        result = reprint_with_material(
            file_path=found_path,
            material_id=material_id,
            printer_name=printer_name,
            printer_id=printer_id,
            extra_overrides=extra_overrides,
            use_ams=detected_use_ams,
            ams_mapping=detected_ams_mapping,
        )

        # Enrich result with smart_reprint context
        if isinstance(result, dict):
            result["smart_reprint_steps"] = steps_log
            result["model_path"] = found_path
            if ams_slot_info:
                result["ams_slot_selected"] = ams_slot_info
            # D2: surface brief_id so the caller's subsequent
            # record_print_outcome call can link the print back to
            # the saved goal without re-reading the sidecar.
            if resolved_brief_id is not None:
                result["brief_id"] = resolved_brief_id

        return result
    except Exception as exc:
        logger.exception("Error in smart_reprint")
        return _error_dict(
            f"Failed in smart_reprint: {exc}",
            code="INTERNAL_ERROR",
        )


@mcp.tool()
def multi_material_print(
    objects_json: str,
    printer_name: str | None = None,
    printer_id: str | None = None,
    auto_ams: bool = True,
    extra_overrides: str | None = None,
    slicer_path: str | None = None,
) -> dict:
    """Print multiple objects in different materials/colors on one build plate.

    Takes a JSON array of objects, each with a model file and material
    assignment. Builds a multi-material 3MF file with per-object filament
    assignments, slices it, auto-maps materials to AMS slots, and prints.

    This is how you print "object A in red PLA, object B in black PETG"
    in a single print job.

    Example: Print a bracket in PETG and a cover in PLA::

        multi_material_print(
            objects_json='[
                {"file_path": "/path/to/bracket.stl", "material_id": "petg"},
                {"file_path": "/path/to/cover.stl", "material_id": "pla"}
            ]',
            printer_name="my_bambu",
            printer_id="bambu_a1",
        )

    Each object in the JSON array supports:
        - ``file_path`` (required): Path to STL/OBJ/GLB mesh file.
        - ``material_id`` (required): Material identifier (e.g. ``"petg"``).
        - ``name`` (optional): Display name for the object.
        - ``color`` (optional): Hex color override (e.g. ``"#FF0000"``).
        - ``group`` (optional): Objects sharing a group index are placed
          coincident (for meshes that share one coordinate space, like a
          body and its inlay). By default every object is its own group
          and gets its own spot on the plate.

    The tool automatically:
        1. Looks up each material's properties (temps, colors)
        2. Arranges the objects side by side on the plate (per ``group``)
           and builds a multi-object 3MF with per-object material assignments
        3. Generates merged slicer overrides (uses the highest-temp material)
        4. Checks AMS slots for matching materials
        5. Slices and prints with correct AMS mapping

    Requires PrusaSlicer or OrcaSlicer installed locally.

    The emitted 3MF (``multi_material_3mf`` in the result) also opens in
    Bambu Studio, which keeps the per-object materials but re-derives
    print settings itself — the result's ``slicer_note`` explains this;
    relay it to the user when handing over the file.

    Args:
        objects_json: JSON array of objects with ``file_path`` and
            ``material_id`` keys (see example above).
        printer_name: Registered printer name in fleet.
        printer_id: Printer model ID for profile selection.
        auto_ams: Auto-detect AMS slot mapping (default ``True``).
        extra_overrides: Additional slicer overrides JSON.
        slicer_path: Explicit path to slicer binary.
    """
    if err := _check_auth("print"):
        return err
    try:
        import json as _json
        import os as _os
        import tempfile

        # Parse input
        try:
            objects = _json.loads(objects_json)
            if not isinstance(objects, list) or not objects:
                return _error_dict(
                    "objects_json must be a non-empty JSON array.",
                    code="VALIDATION_ERROR",
                )
        except _json.JSONDecodeError as exc:
            return _error_dict(
                f"Invalid JSON in objects_json: {exc}",
                code="VALIDATION_ERROR",
            )

        # Validate each object
        for i, obj in enumerate(objects):
            if not isinstance(obj, dict):
                return _error_dict(
                    f"Object at index {i} must be a JSON object.",
                    code="VALIDATION_ERROR",
                )
            if "file_path" not in obj:
                return _error_dict(
                    f"Object at index {i} missing 'file_path'.",
                    code="VALIDATION_ERROR",
                )
            if "material_id" not in obj:
                return _error_dict(
                    f"Object at index {i} missing 'material_id'.",
                    code="VALIDATION_ERROR",
                )
            if not _os.path.isfile(obj["file_path"]):
                return _error_dict(
                    f"File not found: {obj['file_path']}",
                    code="NOT_FOUND",
                )
            file_ext = _os.path.splitext(obj["file_path"])[1].lower()
            if file_ext not in (".stl", ".obj", ".glb"):
                return _error_dict(
                    f"Object at index {i} has unsupported format {file_ext!r} "
                    f"(need .stl, .obj, or .glb).",
                    code="VALIDATION_ERROR",
                )
            if "group" in obj:
                try:
                    obj["group"] = int(obj["group"])
                except (TypeError, ValueError):
                    return _error_dict(
                        f"Object at index {i} has non-integer 'group': "
                        f"{obj['group']!r}.",
                        code="VALIDATION_ERROR",
                    )

        # Step 1: Look up material properties for each object
        from kiln.design_intelligence import get_material_profile

        # Key on (material_id, color) so the same material in two
        # different colors gets separate filament slots.
        unique_filaments: dict[tuple[str, str], int] = {}
        filament_idx = 0
        build_objects: list[dict[str, Any]] = []

        for obj in objects:
            mat_id = obj["material_id"].lower()
            profile = get_material_profile(mat_id)
            if profile is None:
                return _error_dict(
                    f"Unknown material '{obj['material_id']}'.",
                    code="NOT_FOUND",
                )

            # Resolve color — prefer user-specified, then material default
            color = obj.get("color")
            if not color:
                # Use a distinct default color per filament index
                default_colors = [
                    "#FFFFFFFF",
                    "#FF0000FF",
                    "#0000FFFF",
                    "#00FF00FF",
                    "#000000FF",
                    "#FFFF00FF",
                    "#FF00FFFF",
                    "#00FFFFFF",
                ]
                # Assign a default based on how many filaments we've seen
                color = default_colors[filament_idx % len(default_colors)]

            filament_key = (mat_id, color)
            if filament_key not in unique_filaments:
                unique_filaments[filament_key] = filament_idx
                filament_idx += 1

            build_objects.append(
                {
                    "file_path": obj["file_path"],
                    "filament_index": unique_filaments[filament_key],
                    "name": obj.get("name", _os.path.basename(obj["file_path"])),
                    "color": color,
                    "material_name": profile.display_name,
                }
            )

        # Derive unique material IDs for thermal checks
        unique_mat_ids = {mat_id for mat_id, _color in unique_filaments}

        # Step 1b: Thermal compatibility check across all materials
        # A single-nozzle printer uses ONE temperature for all materials.
        # If materials have incompatible thermal ranges, refuse the print.
        mat_profiles: dict[str, Any] = {}
        for mat_id in unique_mat_ids:
            prof = get_material_profile(mat_id)
            if prof is not None:
                mat_profiles[mat_id] = prof

        if len(mat_profiles) > 1:
            nozzle_ranges = []
            bed_ranges = []
            for mid, prof in mat_profiles.items():
                thermal = prof.thermal if hasattr(prof, "thermal") else {}
                temp_range = thermal.get("print_temp_range_c", [190, 220])
                bed_range = thermal.get("bed_temp_range_c", [50, 70])
                nozzle_ranges.append((mid, temp_range))
                bed_ranges.append((mid, bed_range))

            # Check nozzle overlap: all materials must have overlapping ranges
            combined_low = max(r[0] for _, r in nozzle_ranges)
            combined_high = min(r[1] for _, r in nozzle_ranges)
            if combined_low > combined_high:
                names = [f"{mid} ({r[0]}-{r[1]}C)" for mid, r in nozzle_ranges]
                return _error_dict(
                    f"Incompatible nozzle temperatures — no overlapping range: "
                    f"{', '.join(names)}. A single-nozzle printer cannot print "
                    f"materials with non-overlapping temperature ranges in one job. "
                    f"Consider printing these objects separately.",
                    code="MATERIAL_INCOMPATIBLE",
                )

            # Check bed temp gap: >25C difference is risky
            all_bed_temps = [t for _, r in bed_ranges for t in r]
            bed_spread = max(all_bed_temps) - min(all_bed_temps)
            if bed_spread > 40:
                names = [f"{mid} ({r[0]}-{r[1]}C)" for mid, r in bed_ranges]
                return _error_dict(
                    f"Risky bed temperature spread ({bed_spread}C) across materials: "
                    f"{', '.join(names)}. Low-temp materials may warp or deform on "
                    f"a bed optimized for high-temp materials. Consider printing "
                    f"objects with similar bed temperature requirements together.",
                    code="MATERIAL_INCOMPATIBLE",
                )

        # Step 2: Arrange on the plate, then compose the multi-material 3MF.
        # Each object is its own arrangement group by default, so separate
        # objects land side by side instead of stacked at the origin.
        # Objects that must stay coincident (a body and an inlay sharing one
        # coordinate space) declare the same "group" index.
        from kiln.multicolor_3mf import auto_arrange_parts, compose_multicolor_3mf

        part_specs: list[dict[str, Any]] = []
        for i, (obj, built) in enumerate(zip(objects, build_objects, strict=True)):
            part_specs.append(
                {
                    "stl_path": built["file_path"],
                    "extruder": built["filament_index"] + 1,
                    "name": built["name"],
                    "color": built["color"],
                    "material": built["material_name"],
                    "group": obj.get("group", i),
                }
            )

        output_3mf = _os.path.join(tempfile.gettempdir(), "kiln_multi_material.3mf")
        try:
            try:
                positioned = auto_arrange_parts(part_specs, printer_id=printer_id)
            except ValueError:
                # printer_id not in the supported-model catalog — arrange on
                # the default plate size instead of refusing the print.
                positioned = auto_arrange_parts(part_specs)
            compose_result = compose_multicolor_3mf(positioned, output_path=output_3mf)
        except Exception as exc:
            return _error_dict(
                f"Failed to build multi-material 3MF: {exc}",
                code="INTERNAL_ERROR",
            )
        if not compose_result.get("success"):
            return _error_dict(
                f"Failed to build multi-material 3MF: {compose_result.get('error')}",
                code="INTERNAL_ERROR",
            )

        # Step 3: Build merged overrides (use highest-temp material)
        max_temp = 0
        max_bed = 0
        dominant_mat: str | None = None
        dominant_overrides: dict[str, str] = {}
        for mat_id in unique_mat_ids:
            mat_result = build_material_overrides(mat_id, printer_id)
            if mat_result.get("success"):
                ov = mat_result["overrides"]
                temp = int(ov.get("temperature", "0"))
                bed = int(ov.get("bed_temperature", "0"))
                if temp > max_temp:
                    max_temp = temp
                    dominant_mat = mat_id
                    dominant_overrides = dict(ov)
                if bed > max_bed:
                    max_bed = bed

        # Use the dominant (highest temp) material's full overrides
        merged_overrides: dict[str, str] = {}
        if dominant_mat:
            merged_overrides = dominant_overrides
            # Override bed temp with the max across all materials
            merged_overrides["bed_temperature"] = str(max_bed)

        # Merge extra overrides
        if extra_overrides:
            try:
                extra = _json.loads(extra_overrides)
                if isinstance(extra, dict):
                    merged_overrides.update(extra)
            except _json.JSONDecodeError:
                pass

        # Step 4: AMS slot mapping
        ams_mapping_list: list[int] | None = None
        use_ams_flag: bool | None = None
        ams_info: list[dict[str, Any]] = []

        if auto_ams:
            try:
                ams_result = ams_status()
                if ams_result.get("success"):
                    mat_type_map = {
                        "pla": ["PLA"],
                        "pla_plus": ["PLA", "PLA+"],
                        "pla_matte": ["PLA"],
                        "pla_tough": ["PLA"],
                        "petg": ["PETG"],
                        "petg_hf": ["PETG", "PETG-HF"],
                        "cf_petg": ["PETG-CF"],
                        "abs": ["ABS"],
                        "asa": ["ASA"],
                        "tpu": ["TPU"],
                        "tpu_95a": ["TPU"],
                        "tpu_85a": ["TPU"],
                        "nylon": ["PA", "Nylon"],
                    }

                    def _normalize_hex(h: str) -> str:
                        """Strip '#' and alpha, uppercase for comparison."""
                        h = h.lstrip("#").upper()
                        if len(h) == 8:
                            h = h[:6]  # strip alpha channel
                        return h

                    # Sort filaments by index for deterministic mapping
                    sorted_filaments = sorted(unique_filaments.items(), key=lambda kv: kv[1])

                    mapping = []
                    all_found = True
                    for (mat_id, req_color), _fil_idx in sorted_filaments:
                        expected = mat_type_map.get(mat_id, [mat_id.upper()])
                        req_hex = _normalize_hex(req_color)
                        found_slot: int | None = None
                        # First pass: match both material type AND color
                        for unit in ams_result.get("units", []):
                            for tray in unit.get("trays", []):
                                ttype = (tray.get("tray_type") or "").strip()
                                tray_color = _normalize_hex(tray.get("tray_color") or tray.get("color") or "")
                                if ttype in expected and tray_color == req_hex and tray.get("slot") not in mapping:
                                    found_slot = tray.get("slot", 0)
                                    break
                            if found_slot is not None:
                                break
                        # Fallback: match material type only (ignore color)
                        if found_slot is None:
                            for unit in ams_result.get("units", []):
                                for tray in unit.get("trays", []):
                                    ttype = (tray.get("tray_type") or "").strip()
                                    if ttype in expected and tray.get("slot") not in mapping:
                                        found_slot = tray.get("slot", 0)
                                        break
                                if found_slot is not None:
                                    break
                        if found_slot is not None:
                            mapping.append(found_slot)
                            ams_info.append(
                                {
                                    "material": mat_id,
                                    "color": req_color,
                                    "slot": found_slot,
                                    "tray_type": (tray.get("tray_type") or "").strip(),
                                }
                            )
                        else:
                            all_found = False
                            break

                    if all_found and mapping:
                        ams_mapping_list = mapping
                        use_ams_flag = True
            except Exception as _ams_exc:
                logger.debug("AMS query failed in multi_material_print: %s", _ams_exc)

        # Step 5: Slice and print
        result = run_reslice_and_print(
            model_path=output_3mf,
            printer_name=printer_name,
            printer_id=printer_id,
            overrides=_json.dumps(merged_overrides) if merged_overrides else None,
            slicer_path=slicer_path,
            use_ams=use_ams_flag,
            ams_mapping=_json.dumps(ams_mapping_list) if ams_mapping_list else None,
        )

        # Enrich result
        if isinstance(result, dict):
            result["multi_material"] = True
            result["objects"] = [
                {
                    "name": o["name"],
                    "material": o["material_name"],
                    "filament_index": o["filament_index"],
                    "x_mm": round(positioned[i].x, 2),
                    "y_mm": round(positioned[i].y, 2),
                }
                for i, o in enumerate(build_objects)
            ]
            result["materials_used"] = list(unique_mat_ids)
            result["dominant_material"] = dominant_mat
            result["ams_mapping"] = ams_info if ams_info else None
            result["multi_material_3mf"] = output_3mf
            if compose_result.get("slicer_note"):
                result["slicer_note"] = compose_result["slicer_note"]
            # Warn when AMS mapping was not established
            if len(unique_filaments) > 1 and not ams_info:
                result["ams_warning"] = (
                    "No AMS slot mapping was established. Multi-material per-object "
                    "assignment requires a Bambu printer with AMS, or a multi-extruder "
                    "setup (e.g. Prusa XL tool changer). On single-extruder printers "
                    "without AMS, all objects will print with the same filament."
                )

        return result
    except Exception as exc:
        logger.exception("Error in multi_material_print")
        return _error_dict(
            f"Failed in multi_material_print: {exc}",
            code="INTERNAL_ERROR",
        )


@mcp.tool()
def merge_multicolor_gcode(
    parts: str,
    output_path: str = "",
) -> dict:
    """Merge separately-sliced gcode files into one multi-tool gcode.

    Uses a batched strategy that minimises tool changes for multi-color
    prints.  Parts with overlapping Z ranges are printed in tool order
    within the overlap zone, then remaining layers continue above.

    This is the key step between slicing individual parts and wrapping
    as a Bambu 3MF.  The merged gcode contains T0/T1/... tool change
    commands that ``wrap_gcode_as_3mf`` converts to M620/M621 AMS
    load sequences.

    **Precondition:** Parts must be **XY-disjoint** (non-overlapping
    footprints on the build plate).  The batched merge prints each
    tool's layers independently in the overlap zone — overlapping XY
    regions will cause collisions.

    Args:
        parts: JSON array of part objects.  Each must have:

            - ``gcode_path``: Path to the sliced ``.gcode`` file.
            - ``tool_index``: Tool number (0, 1, ...) for AMS mapping.
            - ``name``: Human-readable name (e.g. ``"body_grey"``).

            Example::

                [
                  {"gcode_path": "/path/body.gcode", "tool_index": 0, "name": "body"},
                  {"gcode_path": "/path/qr.gcode", "tool_index": 1, "name": "qr_pads"}
                ]

        output_path: Output file path.  Defaults to a temp directory.

    Returns a dict with ``output_path``, merge phases, layer count,
    and estimated print time.
    """
    if err := _check_auth("slicer"):
        return err
    try:
        import json as _json

        parsed_parts = _json.loads(parts) if isinstance(parts, str) else parts
        if not isinstance(parsed_parts, list):
            return _error_dict("parts must be a JSON array of part objects.")
        for p in parsed_parts:
            if not isinstance(p, dict):
                return _error_dict("Each part must be a JSON object.")
            for key in ("gcode_path", "tool_index", "name"):
                if key not in p:
                    return _error_dict(f"Missing required key {key!r} in part: {p}")

        from kiln.slicer import merge_multipart_gcode

        result = merge_multipart_gcode(
            parsed_parts,
            output_path=output_path or None,
        )
        return {"success": True, **result}
    except ValueError as exc:
        return _error_dict(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in merge_multicolor_gcode")
        return _error_dict(
            f"Failed to merge gcode: {exc}",
            code="INTERNAL_ERROR",
        )


@mcp.tool()
def multi_color_copies(
    model_path: str,
    copies: int | None = None,
    ams_slots: list[int] | None = None,
    colors: list[str] | None = None,
    material: str = "PLA",
    spacing_mm: float = 10.0,
    printer_id: str | None = None,
    slicer_path: str | None = None,
) -> dict:
    """Print multiple copies of the same model, each in a different AMS color.

    Takes a single model file and produces a multi-color print where each
    copy uses a different AMS filament slot.  Perfect for "print 4 lids
    in 4 different colors" workflows.

    **Auto-detect mode** (default): omit *copies*, *ams_slots*, and
    *colors* — the tool queries the AMS, finds all loaded trays matching
    *material*, and prints one copy per loaded tray.

    **Manual mode**: specify *ams_slots* (and optionally *colors*) to
    choose exactly which AMS trays to use and how many copies.

    Requires PrusaSlicer or OrcaSlicer installed locally.  The printer
    must be idle and have an AMS with loaded filament.

    The emitted 3MF (``multi_color_3mf`` in the result) also opens in
    Bambu Studio, which keeps the per-copy colors but re-derives print
    settings itself — the result's ``slicer_note`` explains this; relay
    it to the user when handing over the file.

    :param model_path: Path to the model file (STL or OBJ).
    :param copies: Number of copies.  Auto-detected from AMS if omitted.
    :param ams_slots: Explicit AMS slot indices (0-based) per copy.
        E.g. ``[0, 1, 2, 3]`` for all 4 AMS Lite trays.
    :param colors: Hex color strings per copy for slicer preview.
        E.g. ``["#FF0000", "#00FF00", "#0000FF", "#FFFF00"]``.
        Auto-read from AMS if omitted.
    :param material: Material type filter for AMS auto-detect
        (default ``"PLA"``).  Only trays matching this type are used.
    :param spacing_mm: Gap between copies on the plate (default 10 mm).
        Copies are arranged side by side, centered on the plate.
    :param printer_id: Printer model ID for slicer profile selection and
        plate-size lookup when arranging the copies.
    :param slicer_path: Explicit path to slicer binary.
    :returns: Dict with print result, object details, and AMS mapping.
    """
    if err := _check_auth("print"):
        return err
    try:
        import json as _json
        import tempfile

        # --- Validate model file ---
        if not os.path.isfile(model_path):
            return _error_dict(f"File not found: {model_path}", code="FILE_NOT_FOUND")
        ext = os.path.splitext(model_path)[1].lower()
        if ext not in (".stl", ".obj"):
            return _error_dict(
                f"multi_color_copies requires an STL or OBJ file, got {ext!r}. "
                f"If you have a .gcode.3mf, extract the mesh first or use "
                f"the original STL/OBJ source file.",
                code="VALIDATION_ERROR",
            )

        # --- Resolve AMS slots and colors ---
        resolved_slots: list[int] = []
        resolved_colors: list[str] = []
        ams_warning: str | None = None

        if ams_slots is not None:
            # Manual mode: user specified exact slots
            resolved_slots = list(ams_slots)
            if copies is not None and copies != len(resolved_slots):
                return _error_dict(
                    f"copies ({copies}) doesn't match ams_slots length "
                    f"({len(resolved_slots)}). Provide one slot per copy.",
                    code="VALIDATION_ERROR",
                )
            # Best-effort: verify the requested slots actually hold filament,
            # so a 6-copy request against a 4-tray AMS fails now, not mid-print.
            try:
                ams_check = ams_status()
            except Exception:
                ams_check = None
            if ams_check and ams_check.get("success"):
                loaded_slots = {
                    int(tray.get("slot", 0))
                    for unit in ams_check.get("units", [])
                    for tray in unit.get("trays", [])
                    if (tray.get("tray_type") or "").strip()
                }
                missing = sorted(s for s in resolved_slots if s not in loaded_slots)
                if missing:
                    return _error_dict(
                        f"AMS slot(s) {missing} have no filament loaded. "
                        f"Loaded slots: {sorted(loaded_slots)}. Pick loaded "
                        f"slots or load filament first.",
                        code="NO_MATERIAL",
                    )
            else:
                ams_warning = (
                    "Could not verify ams_slots against the AMS — proceeding "
                    "with the requested slots unchecked."
                )
        else:
            # Auto-detect mode: query AMS for loaded trays
            try:
                ams_result = ams_status()
                if not ams_result.get("success"):
                    return _error_dict(
                        "Could not query AMS status. Specify ams_slots manually.",
                        code="AMS_ERROR",
                    )
                mat_upper = material.upper().strip()
                for unit in ams_result.get("units", []):
                    for tray in unit.get("trays", []):
                        ttype = (tray.get("tray_type") or "").strip().upper()
                        if ttype == mat_upper or mat_upper in ttype:
                            slot = int(tray.get("slot", 0))
                            color_hex = tray.get("tray_color", "000000FF")
                            # Convert RRGGBBAA to #RRGGBB
                            if len(color_hex) >= 6:
                                resolved_slots.append(slot)
                                resolved_colors.append(f"#{color_hex[:6]}")
            except Exception as exc:
                return _error_dict(
                    f"AMS query failed: {exc}. Specify ams_slots manually.",
                    code="AMS_ERROR",
                )

            if not resolved_slots:
                return _error_dict(
                    f"No AMS trays found with material type '{material}'. "
                    f"Check that filament is loaded or specify ams_slots manually.",
                    code="NO_MATERIAL",
                )

        # Apply copies limit if specified
        if copies is not None:
            resolved_slots = resolved_slots[:copies]
            resolved_colors = resolved_colors[:copies]

        n_copies = len(resolved_slots)
        if n_copies < 2:
            return _error_dict(
                "Need at least 2 AMS slots for multi-color copies. For single-color, use start_print directly.",
                code="VALIDATION_ERROR",
            )
        if n_copies > 16:
            return _error_dict(
                "Maximum 16 copies supported.",
                code="VALIDATION_ERROR",
            )

        # Fill in colors if not provided
        if colors is not None:
            resolved_colors = list(colors[:n_copies])
        # Pad colors if too few
        default_colors = [
            "#FF0000",
            "#00FF00",
            "#0000FF",
            "#FFFF00",
            "#FF00FF",
            "#00FFFF",
            "#FFFFFF",
            "#000000",
            "#FF8000",
            "#8000FF",
            "#0080FF",
            "#FF0080",
            "#80FF00",
            "#00FF80",
            "#808080",
            "#C0C0C0",
        ]
        while len(resolved_colors) < n_copies:
            resolved_colors.append(default_colors[len(resolved_colors) % len(default_colors)])

        # --- Arrange the copies and build the multi-color 3MF ---
        # Each copy is its own arrangement group, so the copies land side by
        # side (spacing_mm apart) — never stacked at the origin — and gets a
        # unique extruder so the printer pulls from different AMS slots.
        from kiln.multicolor_3mf import auto_arrange_parts, compose_multicolor_3mf

        model_name = os.path.splitext(os.path.basename(model_path))[0]
        part_specs: list[dict[str, Any]] = []
        for i in range(n_copies):
            part_specs.append(
                {
                    "stl_path": model_path,
                    "extruder": i + 1,
                    "name": f"{model_name}_color_{i + 1}",
                    "color": resolved_colors[i],
                    "material": material,
                    "group": i,
                }
            )

        output_3mf = os.path.join(tempfile.gettempdir(), f"kiln_multi_color_{model_name}.3mf")
        try:
            try:
                positioned = auto_arrange_parts(
                    part_specs, gap_mm=spacing_mm, printer_id=printer_id
                )
            except ValueError:
                # printer_id not in the supported-model catalog — arrange on
                # the default plate size instead of refusing the print.
                positioned = auto_arrange_parts(part_specs, gap_mm=spacing_mm)
            compose_result = compose_multicolor_3mf(positioned, output_path=output_3mf)
        except Exception as exc:
            return _error_dict(
                f"Failed to build multi-color 3MF: {exc}",
                code="INTERNAL_ERROR",
            )
        if not compose_result.get("success"):
            return _error_dict(
                f"Failed to build multi-color 3MF: {compose_result.get('error')}",
                code="INTERNAL_ERROR",
            )

        # --- Build slicer overrides for the material ---
        overrides: dict[str, str] = {}
        try:
            mat_overrides = build_material_overrides(material.lower(), printer_id)
            if mat_overrides.get("success"):
                overrides = dict(mat_overrides["overrides"])
        except Exception:
            pass  # best-effort — slicer profile defaults are fine

        # --- Slice and print with explicit AMS mapping ---
        result = run_reslice_and_print(
            model_path=output_3mf,
            printer_id=printer_id,
            overrides=_json.dumps(overrides) if overrides else None,
            slicer_path=slicer_path,
            use_ams=True,
            ams_mapping=_json.dumps(resolved_slots),
        )

        # Enrich result
        if isinstance(result, dict):
            result["multi_color_copies"] = True
            result["copies"] = n_copies
            result["objects"] = [
                {
                    "name": p.name,
                    "color": p.color,
                    "ams_slot": resolved_slots[i],
                    "x_mm": round(p.x, 2),
                    "y_mm": round(p.y, 2),
                }
                for i, p in enumerate(positioned)
            ]
            result["ams_mapping"] = [
                {"copy": i + 1, "slot": s, "color": resolved_colors[i]} for i, s in enumerate(resolved_slots)
            ]
            result["multi_color_3mf"] = output_3mf
            if compose_result.get("slicer_note"):
                result["slicer_note"] = compose_result["slicer_note"]
            if ams_warning:
                result["ams_warning"] = ams_warning

        return result
    except Exception as exc:
        logger.exception("Error in multi_color_copies")
        return _error_dict(
            f"Failed in multi_color_copies: {exc}",
            code="INTERNAL_ERROR",
        )


@mcp.tool()
def extract_file_metadata(file_path: str) -> dict:
    """Extract metadata from a 3D printing file (.gcode, .3mf, .stl, .ufp).

    Parses file headers for estimated print time, layer count, filament usage,
    dimensions, slicer info, and material hints — without re-slicing.

    .. note::
        For multi-object .gcode.3mf files, also consider using
        ``list_plate_objects()`` to see individual objects on the plate.

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
    fan_speed_pct: float | None = None,
    flow_rate_pct: float | None = None,
) -> dict:
    """Save a checkpoint during an active print for accurate resume.

    The checkpoint is keyed by ``(printer_name, job_id)`` and read
    automatically by :func:`detect_print_failure` so that the resulting
    :class:`FailureReport` carries known-good Z / layer / temps.  The
    resume planner uses this for accurate ``resume_z_mm`` instead of
    estimating from ``z_per_layer * resume_layer`` — meaningfully more
    accurate when the print uses variable-layer-height slicing.

    Args:
        printer_name: Name of the printer running the job.
        job_id: Unique job identifier.
        z_height: Current Z height in mm.
        layer_number: Current layer number (0-based).
        hotend_temp: Hotend temperature at checkpoint time (Celsius).
        bed_temp: Bed temperature at checkpoint time (Celsius).
        filament_used_mm: Filament consumed so far in mm.
        fan_speed_pct: Part-cooling fan speed (0-100).
        flow_rate_pct: Flow-rate multiplier (default 100).
    """
    if err := _check_auth("print"):
        return err

    try:
        from kiln.print_recovery import get_recovery_engine

        engine = get_recovery_engine()
        cp = engine.save_checkpoint(
            printer_name=printer_name,
            job_id=job_id,
            z_height_mm=z_height or 0.0,
            layer_number=layer_number or 0,
            hotend_temp_c=hotend_temp or 0.0,
            bed_temp_c=bed_temp or 0.0,
            filament_used_mm=filament_used_mm or 0.0,
            fan_speed_pct=fan_speed_pct or 0.0,
            flow_rate_pct=flow_rate_pct or 100.0,
        )
        return {"success": True, "checkpoint": cp.to_dict()}
    except ValueError as exc:
        return _error_dict(str(exc), code="VALIDATION_ERROR")
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
    """Plan a recovery strategy from a printer + job + failure type.

    Convenience wrapper that synthesizes a :class:`FailureReport` from
    the supplied args (using the latest checkpoint for the printer/job
    when available) and runs it through the same planner used by
    ``plan_failure_recovery``.

    **Which recovery tool to use:**

    - Have a printer_name + job_id from a failed print? → ``plan_print_recovery`` (this tool)
    - Have a failure_id from ``detect_print_failure``? → ``plan_failure_recovery``

    Args:
        printer_name: Name of the printer that failed.
        job_id: The failed job's identifier.
        failure_type: Type of failure (thermal_runaway, layer_shift,
            adhesion_failure, filament_runout, nozzle_clog,
            communication_loss, power_loss, blob_detected, spaghetti,
            stringing, warping).  Defaults to ``communication_loss``.
    """
    if err := _check_auth("print"):
        return err

    try:
        import uuid as _uuid
        from datetime import datetime, timezone

        from kiln.print_recovery import (
            FailureReport,
            FailureType,
            get_recovery_engine,
        )

        engine = get_recovery_engine()
        # Resolve the failure type, defaulting to communication_loss
        # which has a safe wait_and_retry strategy and matches the
        # most common "I lost contact with my print" scenario.
        try:
            ft = FailureType((failure_type or "communication_loss").lower())
        except ValueError:
            return _error_dict(
                f"Unknown failure_type: {failure_type!r}.  "
                f"Valid values: {[t.value for t in FailureType]}",
                code="VALIDATION_ERROR",
            )

        # Pull the latest checkpoint for the (printer, job) pair so the
        # planner can read accurate resume Z / layer / temps.
        checkpoint = engine.get_latest_checkpoint(printer_name, job_id)

        synth = FailureReport(
            failure_id=str(_uuid.uuid4()),
            failure_type=ft,
            detected_at=datetime.now(tz=timezone.utc).isoformat(),
            printer_name=printer_name,
            job_name=job_id,
            failed_layer=checkpoint.layer_number if checkpoint else None,
            failure_z_mm=checkpoint.z_height_mm if checkpoint else None,
            severity="critical" if ft == FailureType.THERMAL_RUNAWAY else "high",
            evidence=[f"plan_print_recovery invoked with failure_type={ft.value}"],
            checkpoint=checkpoint,
        )
        plan = engine.plan_recovery(synth)
        return {"success": True, "recommendation": plan.to_dict()}
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
    if err := _check_auth("firmware"):
        return err
    if block := _emergency_latch_error("firmware_resume_print", _resolve_effective_printer_name(printer_name)):
        return block

    try:
        adapter = _get_registry().get(printer_name) if printer_name else _get_adapter()

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

        # Stash a resume checkpoint so any subsequent recovery planning
        # has the firmware-level resume state to read.  Replaces the
        # previous best-effort log call into RecoveryManager (which was
        # broken — TypeError-ing on every invocation pre-cleanup).
        try:
            from kiln.print_recovery import get_recovery_engine

            get_recovery_engine().save_checkpoint(
                printer_name=printer_name,
                job_id=job_id,
                z_height_mm=z_height_mm,
                layer_number=layer_number or 0,
                hotend_temp_c=hotend_temp_c,
                bed_temp_c=bed_temp_c,
                fan_speed_pct=fan_speed_pct,
                flow_rate_pct=flow_rate_pct,
            )
        except Exception as exc:
            logger.debug(
                "Failed to stash resume checkpoint (non-fatal): %s", exc,
            )

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

    Runs periodic checks covering connectivity, temperature stability,
    print job health (layer progress stalls, error codes), and active
    error detection.  Alerts are generated when anomalies are found.

    :param printer_name: Printer to monitor.
    :param interval_seconds: Seconds between health checks (default 30).

    See also: ``stop_printer_health_monitoring()``,
    ``check_printer_health()``, ``printer_status()``.
    """
    if err := _check_auth("monitoring"):
        return err

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

    Cancels the periodic health-check loop started by
    ``start_printer_health_monitoring()``.  Active alerts are cleared.
    Monitoring can be restarted at any time by calling
    ``start_printer_health_monitoring()`` again.

    :param printer_name: Printer to stop monitoring.
    """
    if err := _check_auth("monitoring"):
        return err

    try:
        from kiln.print_health_monitor import get_print_health_monitor

        monitor = get_print_health_monitor()
        monitor.stop_monitoring(printer_name)
        return {"success": True, "printer": printer_name, "monitoring": "stopped"}
    except Exception as exc:
        logger.exception("Error in stop_printer_health_monitoring")
        return _error_dict(f"Failed to stop health monitoring: {exc}", code="MONITORING_ERROR")


# estimate_print_progress — moved to plugins/estimate_tools.py

# route_print_job, fleet_submit_job, fleet_job_status, fleet_utilization
# — extracted to plugins/fleet_tools.py


# cache_design, list_cached_designs, get_cached_design
# — extracted to plugins/cache_tools.py


# store_credential, list_credentials, retrieve_credential
# — extracted to plugins/credential_tools.py


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


# acquire_printer_lock, release_printer_lock
# — extracted to plugins/printer_management_tools.py


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


# check_firmware_status, update_printer_firmware, rollback_printer_firmware
# — extracted to plugins/firmware_tools.py


# ---------------------------------------------------------------------------
# Lightweight print status (token-efficient polling)
# ---------------------------------------------------------------------------


@mcp.tool()
def print_status_lite(printer_name: str | None = None) -> dict:
    """Lightweight print status — minimal fields for efficient agent polling.

    Returns only state, completion %, file name, ETA, and temps.
    Use this for frequent polling during prints. For full detail
    (capabilities, all flags, full job data), use ``printer_status``.
    For a formatted text report with cost estimate and health commentary,
    use ``monitor_print``.

    Args:
        printer_name: Target printer.  Omit for the default printer.
    """
    try:
        adapter = _get_registry().get(printer_name) if printer_name else _get_adapter()
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


# Enterprise admin tools -- moved to plugins/enterprise_tools.py
# (export_audit_trail, lock_safety_profile, unlock_safety_profile,
#  manage_team_member, printer_usage_summary, uptime_report,
#  encryption_status, rotate_encryption_key, database_status,
#  report_printer_overage, configure_sso, sso_login_url,
#  sso_exchange_code, sso_status)

# ---------------------------------------------------------------------------
# Print trend analysis & ambient safety
# ---------------------------------------------------------------------------


@mcp.tool()
def printer_trend_analysis(
    printer_name: str,
    *,
    lookback_days: int | None = None,
) -> dict:
    """Analyze local print history trends for a printer.

    Uses only data already stored in the local database — nothing
    leaves the machine.  Returns health score, failure rate trends,
    duration trends, recurring failure modes, and material reliability.

    Args:
        printer_name: Printer to analyze.
        lookback_days: How far back to look (default 30 days).
    """
    try:
        from kiln.persistence import get_db
        from kiln.print_trend_analysis import analyze_printer_trends

        db = get_db()
        report = analyze_printer_trends(
            printer_name,
            db=db,
            lookback_days=lookback_days,
        )
        return {"success": True, "trend_report": report.to_dict()}
    except Exception as exc:
        logger.exception("Error in printer_trend_analysis")
        return _error_dict(f"Failed to analyze printer trends: {exc}", code="TREND_ANALYSIS_ERROR")


@mcp.tool()
def check_ambient_conditions(
    *,
    material: str | None = None,
) -> dict:
    """Check if the printer's chamber temperature is safe for a material.

    Reads the current chamber temperature from the connected printer
    and checks it against material-specific thermal limits.  Warns
    about conditions like:
    - Chamber too hot for PLA (softening risk)
    - Chamber too cold for ABS/ASA (warping risk)
    - Thermal runaway (exceeds printer safety profile max)
    - Cool-down advisory after a high-temp print

    All checks are local — no data leaves the machine.

    Args:
        material: Filament material type (e.g. "PLA", "ABS", "PETG").
                  If not provided, only checks against printer max.
    """
    try:
        adapter = _get_adapter()
        state = adapter.get_state()
        chamber_temp = state.chamber_temp_actual

        # Get max chamber temp from safety profile if available
        max_chamber = None
        try:
            from kiln.safety_profiles import get_profile

            printer_model = os.environ.get("KILN_PRINTER_MODEL", "default")
            profile = get_profile(printer_model)
            max_chamber = profile.max_chamber_temp
        except Exception:
            pass  # No profile available — skip that check

        from kiln.ambient_safety import check_ambient_safety

        result = check_ambient_safety(
            chamber_temp_c=chamber_temp,
            material=material,
            max_chamber_temp_c=max_chamber,
        )
        return {"success": True, "ambient_safety": result.to_dict()}
    except (PrinterError, RuntimeError) as exc:
        return _error_dict(
            f"Failed to check ambient conditions: {exc}. "
            "Check that the printer is online and supports chamber temperature reporting."
        )
    except Exception as exc:
        logger.exception("Error in check_ambient_conditions")
        return _error_dict(f"Failed to check ambient conditions: {exc}", code="AMBIENT_CHECK_ERROR")


def _finish_decoration_result(result_dict: dict, *, content: str) -> dict:
    """Common tail for every SUCCESSFUL decorate_surface exit.

    Quota tile, managed-asset lineage, then the inspect bundle.  It is one
    helper rather than a copy per exit because the two success paths (the
    curved-wall text branch and the main carve) had already drifted into
    duplicate tails once — a third would have been written the same way.

    The lineage half is the honest-warning wire: carving the stored artwork
    of a saved preset is legal and sometimes correct, but the result is NOT
    that preset's recorded settings, and saying so is the only thing that
    stops a blocked caller's fallback from being described as the real
    thing.  Best-effort throughout — a decoration that already succeeded is
    never failed by its own annotations.
    """
    try:
        from kiln.decoration_quota import decoration_quota_status

        result_dict["quota"] = decoration_quota_status()
    except Exception:
        pass

    try:
        from kiln.decoration.managed_assets import describe_managed_asset

        managed = describe_managed_asset(content)
        if managed:
            result_dict["managed_asset"] = managed
            warnings = result_dict.setdefault("warnings", [])
            if isinstance(warnings, list):
                warnings.append(managed["warning"])
    except Exception:
        logger.debug("managed-asset annotation failed", exc_info=True)

    try:
        from kiln_pro.plugins.git_render_tools import attach_inspect_bundle

        return attach_inspect_bundle(result_dict, level="quick")
    except ImportError:
        return result_dict


@mcp.tool()
def decorate_surface(
    model_path: str,
    content: str,
    face: str = "auto",
    depth_mm: float = 0.0,
    mode: str = "deboss",
    scale: float = 0.7,
    material: str = "PLA",
    content_type: str = "auto",
    offset_x_mm: float = 0.0,
    offset_y_mm: float = 0.0,
    image_style: str = "auto",
    placement: str = "center",
    absolute_size_mm: float = 0.0,
    svg_id: str = "",
    svg_layer: str = "",
    template_id: str = "",
) -> dict:
    """Put any image, text, or pattern onto a 3D model surface.

    For repeating patterns (wood grain, camo, honeycomb) that tile across
    the entire surface, use ``apply_geometric_texture`` or
    ``apply_procedural_texture`` instead.  This tool is for one-off
    content placement (logos, text, images).

    Takes a model and content (image file, text string, SVG) and returns
    a new STL with the content embossed or debossed onto the surface.
    Automatically detects the best face for placement and scales content
    to fit.  Single-color coin-relief style — no multi-material needed.

    **Content types** (auto-detected from *content* string):

    - **Image file** (PNG/JPG/SVG): ``"/path/to/photo.jpg"``
    - **Text**: ``"text:KILN"`` or ``"text:Hello World"``

    **Image styles** for raster images:

    - ``"auto"`` — detects the image kind: a logo/wordmark/line-art
      image routes to ``"stencil"`` (crisp traced strokes); a
      continuous-tone photo routes to ``"coin"``
    - ``"coin"`` — histogram-equalized posterize, best for FDM coin-relief
    - ``"portrait"`` — edge-detected line art
    - ``"composite"`` — posterize base + edge overlay hybrid
    - ``"medallion"`` — coin + raised border ring (premium look)
    - ``"photo"`` — simple 3-level posterize
    - ``"stencil"`` (alias ``"logo"``) — the mark's ink is traced into
      vector strokes and carved directly: crisp edges, no background
      tile, correct orientation.  The right choice for brand logos.
    - ``"lithophane"`` — full gradient for backlit prints

    **Examples**::

        decorate_surface(model_path="coaster.stl", content="photo.jpg",
                         mode="deboss", depth_mm=1.5, image_style="coin")

        decorate_surface(model_path="keychain.stl", content="text:KILN",
                         face="top", depth_mm=0.5)

    Requires OpenSCAD installed locally for compilation.

    :param model_path: Path to the base model (STL or OBJ).
    :param content: What to put on the surface — file path (PNG/JPG/SVG)
        or ``"text:..."`` for text.
    :param face: Which face to decorate.  ``"auto"`` picks the largest
        flat face.  Also accepts ``"top"``, ``"bottom"``, ``"front"``,
        ``"back"``, ``"left"``, ``"right"``.  A deboss now carves into
        the body on every cardinal face, and ``offset_x/y_mm`` place
        face-locally (see below).  ``top``/``bottom``/``front`` are the
        battle-tested three; ``back`` carves and offsets correctly but
        content may still land rotated 180° (content orientation is
        unaddressed by the placement fix); on ``left``/``right`` the
        carve lands but the offset axis scaling is less verified.  Prefer
        the front-facing three when exact placement matters.
        ``"wall"`` wraps TEXT around the upright round wall of a cup,
        vase or bowl (STL, text + deboss only).  Letters stay legible and
        read correctly from outside; size and carve depth may be adjusted
        to keep them readable and the wall sound, and the response
        reports what was actually used.  On the wall, *scale* sets letter
        height as a share of the wall and *absolute_size_mm* pins it
        exactly; *offset_x/y_mm* and *placement* do not apply (the line
        sits centred on the front at mid-height).  The wrap engine ships
        with kiln-pro and is included on the hosted service
        (api.kiln3d.com) on every tier; without it locally, use a flat
        face.
    :param depth_mm: Emboss/deboss depth in mm.  ``0`` = auto based on
        *material* (e.g. 0.6 mm for PLA, 1.2 mm for TPU).
    :param mode: ``"deboss"`` (cut into surface) or ``"emboss"`` (raised).
    :param scale: Fraction of the face to cover (0.1-1.0, default 0.7).
    :param material: Material for depth auto-tuning (default ``"PLA"``).
    :param content_type: Override auto-detection: ``"svg"``, ``"image"``,
        ``"text"``.  Default ``"auto"`` detects from *content*.
    :param offset_x_mm: Placement offset from the face centre along the
        face's own WIDTH axis, in mm — positive slides the content
        toward the content's right.  Offsets are FACE-LOCAL: they are
        applied inside the face-aligning rotation, so they always move
        the art in the face plane, never along its normal.
    :param offset_y_mm: Same, along the face's HEIGHT axis — positive
        slides the content toward the content's top.  Measured
        world-axis mapping per face: top +y, bottom −y, front +z,
        back −z.
    :param image_style: Image preprocessing style.  ``"auto"`` uses
        ``"coin"`` for photos.  See docstring for all options.
    :param placement: Named position preset for content placement.
        ``"center"`` (default), ``"top"``, ``"bottom"``, ``"top-rim"``,
        ``"bottom-rim"``.  Use ``"bottom"`` for text below a centered
        portrait on a coaster.  Manual ``offset_x/y_mm`` is added on
        top of the preset for fine-tuning.
    :param svg_id: Target a specific SVG group by ``id`` attribute when using
        SVG ``import()`` fallback on OpenSCAD 2024+.  E.g. ``"icon"`` targets
        ``<g id="icon">``.  No effect on the native polygon path.
    :param svg_layer: Target a specific SVG layer by ``layer`` attribute when
        using SVG ``import()`` fallback on OpenSCAD 2024+.  E.g.
        ``"foreground"`` targets a layer named ``foreground``.
    :param template_id: Optional template ID (e.g. ``"nameplate"``), used
        for provenance tracking only.  To auto-fill ``face``, ``depth_mm``,
        ``mode``, ``scale``, and ``image_style`` from a template's curated
        decoration profile, call ``resolve_template_decoration()`` first
        (kiln-pro) and pass its results explicitly.
    :returns: Dict with output STL path, preview info, and metadata.
    """
    if err := _check_auth("design:decorate"):
        return err

    # --- Decoration quota (3/month for free tier) ---
    try:
        from kiln.decoration_quota import check_decoration_quota

        ok, quota_err = check_decoration_quota()
        if not ok:
            return quota_err
    except Exception:
        pass  # Fail open — don't block on quota errors

    import tempfile

    # --- Check provenance sidecar for design recipe defaults ---
    _provenance_info: dict[str, Any] = {}
    try:
        recipe_path = os.path.join(os.path.dirname(os.path.abspath(model_path)), ".kiln_recipe.json")
        if os.path.isfile(recipe_path):
            from kiln.design_recipe import DesignRecipe

            _recipe = DesignRecipe.load(recipe_path)
            if _recipe.name:
                _provenance_info["recipe_name"] = _recipe.name
            _provenance_info["recipe_version"] = _recipe.version
            if _recipe.design_id:
                _provenance_info["design_id"] = _recipe.design_id
            if _recipe.prompt:
                _provenance_info["prompt"] = _recipe.prompt
            if _recipe.generation_provider:
                _provenance_info["generation_provider"] = _recipe.generation_provider

            # Use recipe's design_id as template_id if caller didn't provide one
            if not template_id and _recipe.design_id:
                template_id = _recipe.design_id
                logger.debug("Provenance: using design_id %r as template_id", template_id)

            # Pre-populate material from recipe if caller left the default.
            # Note: material=="PLA" is treated as "no explicit preference"
            # since PLA is the function's default.  If the recipe specifies
            # a different material, use it.
            recipe_material = _recipe.parameters.get("material") or (
                _recipe.provenance and _recipe.provenance.get("material")
            )
            if recipe_material and recipe_material != material and material == "PLA":
                material = recipe_material
                _provenance_info["material_source"] = "recipe"
                logger.debug("Provenance: material set to %r from recipe", material)

            # Pre-populate face/depth hints from recipe parameters
            recipe_params = _recipe.parameters
            if face == "auto" and recipe_params.get("decoration_face"):
                face = recipe_params["decoration_face"]
                _provenance_info["face_source"] = "recipe"
                logger.debug("Provenance: face set to %r from recipe", face)
            if depth_mm == 0.0 and recipe_params.get("decoration_depth_mm"):
                depth_mm = float(recipe_params["decoration_depth_mm"])
                _provenance_info["depth_source"] = "recipe"
                logger.debug("Provenance: depth set to %.1f from recipe", depth_mm)

            logger.info(
                "Provenance sidecar loaded for %s (design=%s, v%d)",
                os.path.basename(model_path),
                _recipe.design_id or "n/a",
                _recipe.version,
            )
    except Exception:
        logger.debug("Provenance sidecar check failed", exc_info=True)

    # --- Validate model ---
    if not os.path.isfile(model_path):
        return _error_dict(f"Model not found: {model_path}", code="FILE_NOT_FOUND")
    model_ext = os.path.splitext(model_path)[1].lower()
    if model_ext not in (".stl", ".obj"):
        return _error_dict(
            f"decorate_surface requires STL or OBJ, got {model_ext!r}.",
            code="VALIDATION_ERROR",
        )

    work_dir = os.path.join(tempfile.gettempdir(), "kiln_decorate_surface")
    os.makedirs(work_dir, mode=0o700, exist_ok=True)
    warnings: list[str] = []

    try:
        # --- Step 1: Detect content type ---
        ctype = content_type.lower().strip()
        if ctype == "auto":
            if content.lower().startswith("text:"):
                ctype = "text"
            elif os.path.isfile(content):
                ext = os.path.splitext(content)[1].lower()
                if ext == ".svg":
                    ctype = "svg"
                elif ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif"):
                    ctype = "image"
                else:
                    return _error_dict(
                        f"Unsupported image format: {ext!r}. Use PNG, JPG, or SVG.",
                        code="UNSUPPORTED_FORMAT",
                    )
            else:
                return _error_dict(
                    f"Cannot resolve content: {content!r}. Provide a file path or 'text:...' for text.",
                    code="INVALID_CONTENT",
                )

        # --- Curved-wall path: face="wall" wraps TEXT around the upright
        # round wall of a cup, vase or bowl.  The wrap engine ships with
        # kiln-pro; the hosted service (api.kiln3d.com) includes it on
        # every tier.  Text + deboss only by design: images and repeating
        # patterns on curved walls route through apply_image_texture /
        # apply_procedural_texture, which own curved-surface projection.
        if face.lower().strip() == "wall":
            if ctype != "text":
                return _error_dict(
                    "face='wall' supports text content only ('text:...'). "
                    "For images or patterns on curved walls use "
                    "apply_image_texture or apply_procedural_texture.",
                    code="INVALID_CONTENT",
                )
            if mode != "deboss":
                return _error_dict(
                    "face='wall' text is deboss-only (carved into the "
                    "wall); emboss on a curved wall is not supported yet.",
                    code="INVALID_MODE",
                )
            wall_text_val = content.split(":", 1)[1].strip()
            if not wall_text_val:
                return _error_dict(
                    "No text to wrap — pass content='text:YOUR TEXT'.",
                    code="INVALID_CONTENT",
                )
            # The wrap engine works from the mesh geometry, so it needs an
            # STL.  The generic check above admits OBJ for the flat path.
            if os.path.splitext(model_path)[1].lower() != ".stl":
                return _error_dict(
                    "face='wall' needs an STL. Convert with "
                    "import_external_mesh, or use a flat face for this "
                    "model.",
                    code="UNSUPPORTED_FORMAT",
                )

            try:
                from kiln_pro.bridge import pro_features

                wall_engine = getattr(pro_features, "wall_text", None)
                if wall_engine is None:
                    raise ImportError("wall-text engine unavailable")
            except ImportError:
                return _error_dict(
                    "Curved-wall text wrapping runs on the engine that "
                    "ships with kiln-pro — included on the hosted service "
                    "(api.kiln3d.com) on every tier. Locally without it, "
                    "place text on a flat face instead (face='front', "
                    "'top', ...).",
                    code="ENGINE_UNAVAILABLE",
                )

            from kiln.decoration_helpers import TextDoesNotFitError

            wall_depth = depth_mm if depth_mm > 0 else 1.0
            # Placement controls that only mean something on a flat face —
            # say so rather than accepting them and doing nothing.
            if offset_x_mm or offset_y_mm or placement != "center":
                warnings.append(
                    "offset_x_mm / offset_y_mm / placement don't apply to "
                    "wall text yet — it wraps centred on the front of the "
                    "wall at mid-height."
                )
            # scale is "fraction of the face to cover"; on a wall that reads
            # as a share of the wall's height.  The 0.5 factor keeps the
            # default scale (0.7) on the engine's own default band share.
            wall_kwargs: dict[str, Any] = {}
            if absolute_size_mm > 0:
                wall_kwargs["target_size_mm"] = absolute_size_mm
            elif scale > 0:
                wall_kwargs["band_fraction"] = min(0.9, scale * 0.5)

            try:
                wall_result = wall_engine.wrap_text_on_mesh_wall(
                    model_path,
                    wall_text_val,
                    depth_mm=wall_depth,
                    output_dir=work_dir,
                    **wall_kwargs,
                )
            except TextDoesNotFitError as exc:
                err = _error_dict(
                    "Wall text won't fit legibly.  "
                    + "; ".join(exc.verdict.get("warnings", [])),
                    code="TEXT_DOES_NOT_FIT",
                )
                err["suggestions"] = exc.verdict.get("suggestions", [])
                return err
            except getattr(
                wall_engine, "NoRoundWallError", ValueError
            ) as exc:
                return _error_dict(str(exc), code="NO_ROUND_WALL")
            except ValueError as exc:
                return _error_dict(str(exc), code="INVALID_MODEL")

            output_stl = wall_result["stl_path"]
            result_dict = {
                "success": True,
                "message": (
                    f"Wrapped text around the wall of "
                    f"{os.path.basename(model_path)} "
                    f"({wall_result['wrapped_deg']:.0f}° of arc at "
                    f"{wall_result['size_mm']:.1f}mm letters)."
                ),
                "output_stl": output_stl,
                "file_size_bytes": (
                    os.path.getsize(output_stl)
                    if os.path.isfile(output_stl)
                    else 0
                ),
                "face": {
                    "name": "wall",
                    "radius_mm": wall_result["radius_mm"],
                    "z_mm": wall_result["z_mm"],
                    "wrapped_deg": wall_result["wrapped_deg"],
                },
                "decoration": {
                    "content_type": "text",
                    "mode": "deboss",
                    # What was actually carved, not what was asked for —
                    # size and depth may be adjusted to keep the text
                    # readable and the wall sound.
                    "depth_mm": wall_result["meta"].get("depth_mm", wall_depth),
                    "requested_depth_mm": wall_depth,
                    "text_size_mm": wall_result["size_mm"],
                    "material": material,
                },
                "compile_time_seconds": wall_result.get(
                    "compile_time_seconds"
                ),
                "scad_path": wall_result["scad_path"],
            }
            combined_warnings = [*warnings, *wall_result.get("warnings", [])]
            if combined_warnings:
                result_dict["warnings"] = combined_warnings
            if _provenance_info:
                result_dict["provenance"] = _provenance_info

            # Same tail as the flat path: telemetry, quota tile, preview.
            try:
                from kiln.daily_stats import record_event

                record_event("decorations", detail="text_wall")
            except Exception:
                pass
            return _finish_decoration_result(result_dict, content=content)

        # --- Step 2: Find the target face (needed before SVG prep for sizing) ---
        from kiln.surface_intelligence import (
            find_largest_flat_face,
            find_named_face,
        )

        face_lower = face.lower().strip()
        if face_lower == "auto":
            face_info = find_largest_flat_face(model_path)
        else:
            face_info = find_named_face(model_path, face_lower)

        face_width_mm = face_info.get("width_mm", 0)

        # --- Step 3: Prepare content ---
        content_info: dict[str, Any] = {}

        if ctype == "svg":
            # SVG logo decoration is a Pro feature
            try:
                from kiln_pro.bridge import pro_features

                if pro_features is None:
                    raise ImportError("kiln-pro not installed")
            except ImportError:
                return _error_dict(
                    tier_required_message(
                        "SVG logo decoration",
                        "pro",
                        "Free tier supports PNG/JPG photos and text",
                    ),
                    code="PRO_REQUIRED",
                    extra=signin_hint_fields(),
                )

            from kiln.image_to_surface import prepare_svg_for_emboss

            # Pass face size so SVG strokes scale to printable widths.
            # Minimum stroke = 1.5mm physical (v4 coaster used size*0.04 = 1.92mm
            # on a 48mm logo — thick enough for clean FDM lines).
            target_mm = face_width_mm * scale if face_width_mm > 0 else 0
            content_info = prepare_svg_for_emboss(
                content,
                work_dir,
                min_physical_width_mm=1.5,
                target_size_mm=target_mm,
            )

        elif ctype == "image":
            from kiln.image_to_surface import prepare_image_for_emboss

            effective_style = image_style
            if effective_style == "auto":
                # Bi-level marks (logos, wordmarks, line art) carve as
                # traced vector strokes; continuous-tone photos keep the
                # heightmap relief path.
                try:
                    from kiln.mark_geometry import is_bilevel_image

                    effective_style = (
                        "stencil" if is_bilevel_image(content) else "coin"
                    )
                except Exception:  # noqa: BLE001 — detector never blocks a decorate
                    effective_style = "coin"

            if effective_style in ("stencil", "logo"):
                # Crisp mark path: trace the ink into native polygon()
                # geometry and carve ONLY the strokes — no tile frame, no
                # background carve, no pixel staircase, no mirroring.
                try:
                    from kiln.image_to_surface import prepare_logo_image_for_emboss

                    content_info = prepare_logo_image_for_emboss(content, work_dir)
                except (ValueError, ImportError):
                    logger.warning(
                        "Mark trace failed for %s — heightmap stencil fallback",
                        content,
                        exc_info=True,
                    )
                    content_info = prepare_image_for_emboss(
                        content,
                        work_dir,
                        max_resolution=250,
                        invert=(mode == "deboss"),
                        style="stencil",
                        flip_rows=True,
                    )
            else:
                coin_like = effective_style in ("coin", "medallion")
                content_info = prepare_image_for_emboss(
                    content,
                    work_dir,
                    max_resolution=250 if coin_like else 200,
                    invert=(mode == "deboss"),
                    style=effective_style,
                    # surface() reads rows bottom-to-top; EVERY heightmap
                    # style needs the row flip or the relief renders
                    # upside-down (only the coin path had this right).
                    flip_rows=True,
                )

        elif ctype == "text":
            import math

            from kiln.decoration_helpers import _FDM_TEXT_LEGIBILITY_FLOOR_MM
            from kiln.emboss_generator import (
                TextMeasureError,
                measure_text_block_mm,
            )
            from kiln.image_to_surface import generate_text_image

            text_content = content.split(":", 1)[1] if ":" in content else content
            # Size the text to the face with MEASURED metrics so it can
            # never run off the edges.  Kiln knows the face parametrically
            # and can measure the exact rendered text width (probe
            # compile), so the fit is math, not a heuristic — the old
            # baked size=48 rendered "KILN" 146mm wide on a 90mm coaster.
            #
            # Round faces (coaster/tray tops — detected by the face's
            # real area: a disc fills ~78.5% of its bbox) get the
            # inscribed-circle treatment: the text BLOCK's diagonal must
            # sit inside the usable radius, with an ample margin, so no
            # letter corner ever kisses the rim.
            fit_w = face_width_mm if face_width_mm > 0 else 50.0
            fit_h = face_info.get("height_mm", 0) or fit_w
            face_area = face_info.get("area_mm2", 0.0)
            is_round = (
                fit_w > 0
                and fit_h > 0
                and face_area > 0
                and face_area / (fit_w * fit_h) < 0.9
            )
            ample_margin = max(4.0, 0.06 * min(fit_w, fit_h))
            if absolute_size_mm <= 0:
                try:
                    t_w, t_h, _, _ = measure_text_block_mm(text_content)
                    k = t_h / t_w if t_w > 0 else 0.3
                    if is_round:
                        r_use = min(fit_w, fit_h) / 2.0 - ample_margin
                        allowed_w = (2.0 * r_use) / math.sqrt(1.0 + k * k)
                    else:
                        allowed_w = min(
                            fit_w - 2.0 * ample_margin,
                            (fit_h - 2.0 * ample_margin) / k,
                        )
                    if allowed_w <= 0:
                        return _error_dict(
                            f"Face ({fit_w:.0f}x{fit_h:.0f}mm) is too small "
                            "to carry readable text.",
                            code="TEXT_DOES_NOT_FIT",
                        )
                    absolute_size_mm = allowed_w
                    # The emboss generator fits the font so the measured
                    # width equals this target; warn if that lands below
                    # the FDM legibility floor.
                    projected_font = 48.0 * allowed_w / t_w
                    if projected_font < _FDM_TEXT_LEGIBILITY_FLOOR_MM:
                        warnings.append(
                            f"Fitted text is ~{projected_font:.1f}mm tall — "
                            f"below the {_FDM_TEXT_LEGIBILITY_FLOOR_MM:.0f}mm "
                            "FDM legibility floor. Shorter text or a larger "
                            "face will read better."
                        )
                except TextMeasureError:
                    # No probe available (OpenSCAD missing): target the
                    # usable span conservatively — a round face gets the
                    # inscribed-square factor so corners stay off the rim.
                    usable = min(fit_w, fit_h) - 2.0 * ample_margin
                    absolute_size_mm = max(1.0, usable * (0.71 if is_round else 1.0))
            content_info = generate_text_image(text_content, work_dir)

        else:
            return _error_dict(
                f"Unknown content_type: {content_type!r}.",
                code="VALIDATION_ERROR",
            )

        # --- Step 3.5: Print intelligence warnings ---
        if face_info.get("face_name") == "bottom":
            center_z = face_info.get("center", (0, 0, 0))[2]
            if center_z < 0.5:
                warnings.append(
                    "Bottom face touches the print bed — details will be "
                    "flattened by bed adhesion. Consider face='top' instead."
                )

        # --- Step 4: Resolve depth ---
        from kiln.emboss_generator import get_default_depth

        effective_depth = depth_mm if depth_mm > 0 else get_default_depth(material)

        nozzle_mm = 0.4
        if effective_depth < nozzle_mm * 0.5:
            warnings.append(
                f"Depth {effective_depth:.1f}mm < half nozzle ({nozzle_mm}mm). "
                f"Details may not be visible. Try >= {nozzle_mm * 0.75:.1f}mm."
            )

        # --- Step 5: Generate OpenSCAD ---
        from kiln.emboss_generator import generate_emboss_scad

        scad_result = generate_emboss_scad(
            model_path=os.path.abspath(model_path),
            content_info=content_info,
            face=face_info,
            output_dir=work_dir,
            depth_mm=effective_depth,
            mode=mode,
            scale=scale,
            absolute_size_mm=absolute_size_mm,
            offset_x_mm=offset_x_mm,
            offset_y_mm=offset_y_mm,
            placement=placement,
            svg_id=svg_id,
            svg_layer=svg_layer,
        )
        # Collect warnings from emboss generator (e.g. absolute_size_mm clamping)
        if scad_result.get("warnings"):
            warnings.extend(scad_result["warnings"])

        # --- Step 6: Compile to STL ---
        from kiln.emboss_generator import compile_embossed_model

        compile_result = compile_embossed_model(
            scad_result["scad_path"],
            scad_result["output_stl_path"],
            timeout=600,
        )

        if not compile_result.get("success"):
            result_dict: dict[str, Any] = {
                "status": "compile_failed",
                "message": (f"OpenSCAD compilation failed. Error: {compile_result.get('error', 'unknown')}"),
                "scad_path": scad_result["scad_path"],
                "compile_result": compile_result,
            }
            if warnings:
                result_dict["warnings"] = warnings
            return result_dict

        # --- Step 6b: Verify boolean actually cut ---
        from kiln.emboss_generator import check_boolean_success

        output_stl_path = compile_result["stl_path"]
        abs_model = os.path.abspath(model_path)
        boolean_ok = check_boolean_success(abs_model, output_stl_path)

        # More aggressive check for SVGs: OpenSCAD's import() + difference()
        # can produce an output that's slightly different in size but has NO
        # visible geometry change.  If output < input + 1000 bytes, treat as
        # failed and trigger the heightmap fallback.
        if boolean_ok and ctype == "svg":
            try:
                _in_sz = os.path.getsize(abs_model)
                _out_sz = os.path.getsize(output_stl_path)
                if _out_sz < _in_sz + 1000:
                    boolean_ok = False
                    logger.debug(
                        "SVG boolean size delta too small (%d → %d, delta=%d < 1000) — treating as failed",
                        _in_sz,
                        _out_sz,
                        _out_sz - _in_sz,
                    )
            except OSError:
                pass

        if not boolean_ok and ctype == "svg":
            # SVG boolean failed — rasterize and re-carve.  Trace the
            # raster back into vector strokes first (still frameless);
            # only if THAT fails fall to the heightmap stencil, which
            # carves the whole tile.
            try:
                from kiln.image_to_surface import (
                    prepare_image_for_emboss,
                    prepare_logo_image_for_emboss,
                    rasterize_svg_to_png,
                )

                raster_png = os.path.join(work_dir, "svg_rasterized.png")
                rasterize_svg_to_png(content, raster_png, width_px=2048)
                try:
                    content_info = prepare_logo_image_for_emboss(
                        raster_png, work_dir
                    )
                except (ValueError, ImportError):
                    content_info = prepare_image_for_emboss(
                        raster_png,
                        work_dir,
                        max_resolution=400,
                        invert=(mode == "deboss"),
                        style="stencil",
                        edge_enhance=False,
                        flip_rows=True,
                    )
                scad_result = generate_emboss_scad(
                    model_path=abs_model,
                    content_info=content_info,
                    face=face_info,
                    output_dir=work_dir,
                    depth_mm=effective_depth,
                    mode=mode,
                    scale=scale,
                    absolute_size_mm=absolute_size_mm,
                    offset_x_mm=offset_x_mm,
                    offset_y_mm=offset_y_mm,
                    placement=placement,
                    svg_id=svg_id,
                    svg_layer=svg_layer,
                )
                compile_result = compile_embossed_model(
                    scad_result["scad_path"],
                    scad_result["output_stl_path"],
                    timeout=600,
                )
                if compile_result.get("success"):
                    warnings.append(
                        "SVG boolean produced no geometry change. Fell back to heightmap rasterization (succeeded)."
                    )
                else:
                    warnings.append(
                        "SVG boolean and heightmap fallback both failed. "
                        "Convert SVG to PNG and use content_type='image'."
                    )
            except Exception as fallback_exc:
                logger.debug("SVG heightmap fallback failed: %s", fallback_exc)
                warnings.append("SVG boolean produced no geometry change. Convert to PNG and use content_type='image'.")
        elif not boolean_ok:
            warnings.append(
                "Output STL is similar in size to input — the boolean may not have produced visible geometry changes."
            )

        # --- Step 7: Build result ---
        output_stl = compile_result["stl_path"]
        file_size = compile_result.get("file_size", 0)

        result_dict = {
            "success": True,
            "message": (
                f"Successfully {mode}ed content onto "
                f"{face_info.get('face_name', 'surface')} face of "
                f"{os.path.basename(model_path)}."
            ),
            "output_stl": output_stl,
            "file_size_bytes": file_size,
            "face": {
                "name": face_info.get("face_name"),
                "area_mm2": round(face_info.get("area_mm2", 0), 1),
                "width_mm": round(face_info.get("width_mm", 0), 1),
                "height_mm": round(face_info.get("height_mm", 0), 1),
            },
            "decoration": {
                "content_type": ctype,
                "mode": mode,
                "depth_mm": effective_depth,
                "scale": scale,
                "material": material,
                "image_style": image_style,
            },
            "compile_time_seconds": compile_result.get("compile_time_seconds"),
            "scad_path": scad_result["scad_path"],
        }
        if _provenance_info:
            result_dict["provenance"] = _provenance_info
        if warnings:
            result_dict["warnings"] = warnings

        # Telemetry: count decoration with type detail
        try:
            from kiln.daily_stats import record_event
            record_event("decorations", detail=ctype or "unknown")
        except Exception:
            pass

        # Quiet quota tile on a SUCCESSFUL decoration, so a free/local caller
        # sees where they stand ("2 of 3 used") before the wall instead of
        # only hitting it as an error next time. Best-effort — never blocks
        # a decoration that already succeeded.
        return _finish_decoration_result(result_dict, content=content)

    except FileNotFoundError as exc:
        return _error_dict(str(exc), code="FILE_NOT_FOUND")
    except ValueError as exc:
        return _error_dict(str(exc), code="VALIDATION_ERROR")
    except Exception as exc:
        logger.exception("Error in decorate_surface")
        return _error_dict(
            f"Failed in decorate_surface: {exc}",
            code="INTERNAL_ERROR",
        )


_ensure_internal_tool_plugins_registered()


# ---------------------------------------------------------------------------
# Backward-compatible re-exports for functions that moved to plugins.
# Tests and external code that import these names from kiln.server will
# still work.  Each name is bound to the actual plugin function so that
# ``patch("kiln.server.X")`` works correctly in tests.
# ---------------------------------------------------------------------------

_PLUGIN_REEXPORTS = [
    "await_generation", "download_generated_model", "generate_and_print",
    "generate_model", "generation_status", "list_generation_providers",
    "validate_generated_mesh", "get_started", "reslice_with_overrides",
    "kiln_health", "browse_models", "download_model", "list_model_categories",
    "model_details", "model_files", "search_models", "validate_gcode",
    "list_print_pipelines", "pipeline_status", "pipeline_pause",
    "pipeline_resume", "pipeline_abort", "pipeline_retry_step",
    "monitor_print_vision", "watch_print", "watch_print_status",
    "stop_watch_print", "start_monitored_print", "first_layer_status",
]

for _name in _PLUGIN_REEXPORTS:
    _tool = mcp._tool_manager._tools.get(_name)
    if _tool is not None:
        globals()[_name] = _tool.fn

# Re-export private names from monitoring_tools plugin for test compatibility.
from kiln.plugins.monitoring_tools import (  # noqa: E402, F401, I001
    _PHASE_HINTS,
    _PrintWatcher,
    _detect_phase,
)


if __name__ == "__main__":
    main()
