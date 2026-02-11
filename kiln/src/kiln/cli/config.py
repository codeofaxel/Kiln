"""Configuration management for the Kiln CLI.

Handles loading, saving, and validating printer configurations stored
in ``~/.kiln/config.yaml``.  Supports multiple named printers with a
configurable active default.

Precedence (highest first):
    1. CLI flags (``--printer``, ``--host``, etc.)
    2. Environment variables (``KILN_PRINTER_HOST``, etc.)
    3. Config file (``~/.kiln/config.yaml``)
"""

from __future__ import annotations

import logging
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)


def get_config_path() -> Path:
    """Return the default config file path (``~/.kiln/config.yaml``)."""
    return Path.home() / ".kiln" / "config.yaml"


def _normalize_host(host: str) -> str:
    """Ensure *host* has an HTTP(S) scheme and no trailing slash."""
    host = host.strip()
    if host and not re.match(r"^https?://", host, re.IGNORECASE):
        host = "http://" + host
    return host.rstrip("/")


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def _check_file_permissions(path: Path) -> None:
    """Warn if *path* is readable by group or others."""
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH):
            logger.warning(
                "Config file %s has overly permissive permissions "
                "(mode %04o). Recommended: chmod 600 %s",
                path,
                stat.S_IMODE(mode),
                path,
            )
    except OSError:
        pass


def _read_config_file(path: Path) -> Dict[str, Any]:
    """Read and parse the YAML config file; return ``{}`` on any failure."""
    if not path.is_file():
        return {}
    _check_file_permissions(path)
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except (yaml.YAMLError, OSError):
        return {}


def _write_config_file(path: Path, data: Dict[str, Any]) -> None:
    """Write *data* to the YAML config file, creating dirs as needed.

    Sets file permissions to ``0600`` (owner read/write only) since the
    config may contain API keys and access codes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_printer_config(
    printer_name: str | None = None,
    *,
    config_path: Path | None = None,
) -> Dict[str, Any]:
    """Resolve the configuration for a single printer.

    Resolution order:
        1. If ``KILN_PRINTER_HOST`` is set, build config from env vars
           (ignores config file entirely).
        2. Otherwise look up *printer_name* (or the active printer) in
           the config file.

    Returns a dict with at minimum ``type`` and ``host`` keys.

    Raises :class:`ValueError` if no usable config is found.
    """
    # --- Env var fast path ------------------------------------------------
    env_host = os.environ.get("KILN_PRINTER_HOST", "")
    if env_host:
        return {
            "type": os.environ.get("KILN_PRINTER_TYPE", "octoprint"),
            "host": _normalize_host(env_host),
            "api_key": os.environ.get("KILN_PRINTER_API_KEY", ""),
            "access_code": os.environ.get("KILN_PRINTER_ACCESS_CODE", os.environ.get("KILN_PRINTER_API_KEY", "")),
            "serial": os.environ.get("KILN_PRINTER_SERIAL", ""),
        }

    # --- Config file path -------------------------------------------------
    path = config_path or get_config_path()
    raw = _read_config_file(path)
    printers = raw.get("printers", {})
    if not isinstance(printers, dict):
        printers = {}

    settings = raw.get("settings", {})
    if not isinstance(settings, dict):
        settings = {}

    # Determine which printer to use
    name = printer_name or raw.get("active_printer")
    if not name:
        if len(printers) == 1:
            name = next(iter(printers))
        elif len(printers) == 0:
            raise ValueError(
                "No printers configured.  Run 'kiln auth' to add one."
            )
        else:
            raise ValueError(
                "Multiple printers configured but no active printer set.  "
                "Run 'kiln use <name>' to select one, or pass --printer."
            )

    if name not in printers:
        raise ValueError(
            f"Printer {name!r} not found in config.  "
            f"Available: {', '.join(printers.keys())}"
        )

    cfg = dict(printers[name])
    cfg.setdefault("timeout", settings.get("timeout", 30))
    cfg.setdefault("retries", settings.get("retries", 3))
    cfg["host"] = _normalize_host(str(cfg.get("host", "")))
    return cfg


# ---------------------------------------------------------------------------
# Save / mutate
# ---------------------------------------------------------------------------


def save_printer(
    name: str,
    printer_type: str,
    host: str,
    *,
    api_key: str | None = None,
    access_code: str | None = None,
    serial: str | None = None,
    set_active: bool = True,
    config_path: Path | None = None,
) -> Path:
    """Add or update a printer in the config file.

    Returns the path to the config file.
    """
    path = config_path or get_config_path()
    raw = _read_config_file(path)
    printers = raw.setdefault("printers", {})
    if not isinstance(printers, dict):
        raw["printers"] = printers = {}

    entry: Dict[str, Any] = {
        "type": printer_type,
        "host": _normalize_host(host),
    }
    if printer_type == "octoprint":
        if api_key:
            entry["api_key"] = api_key
    elif printer_type == "moonraker":
        if api_key:
            entry["api_key"] = api_key
    elif printer_type == "bambu":
        if access_code:
            entry["access_code"] = access_code
        if serial:
            entry["serial"] = serial
    elif printer_type == "prusaconnect":
        if api_key:
            entry["api_key"] = api_key

    printers[name] = entry

    if set_active or "active_printer" not in raw:
        raw["active_printer"] = name

    raw.setdefault("settings", {"timeout": 30, "retries": 3})
    _write_config_file(path, raw)
    return path


def set_active_printer(
    name: str,
    *,
    config_path: Path | None = None,
) -> None:
    """Set the active printer in the config file.

    Raises :class:`ValueError` if the printer doesn't exist.
    """
    path = config_path or get_config_path()
    raw = _read_config_file(path)
    printers = raw.get("printers", {})

    if name not in printers:
        raise ValueError(
            f"Printer {name!r} not found.  "
            f"Available: {', '.join(printers.keys()) or '(none)'}"
        )

    raw["active_printer"] = name
    _write_config_file(path, raw)


def list_printers(
    *,
    config_path: Path | None = None,
) -> List[Dict[str, Any]]:
    """Return a list of saved printers with name, type, host, active flag."""
    path = config_path or get_config_path()
    raw = _read_config_file(path)
    printers = raw.get("printers", {})
    active = raw.get("active_printer", "")

    result: List[Dict[str, Any]] = []
    for name, cfg in printers.items():
        if not isinstance(cfg, dict):
            continue
        result.append({
            "name": name,
            "type": cfg.get("type", "unknown"),
            "host": cfg.get("host", ""),
            "active": name == active,
        })
    return result


def remove_printer(
    name: str,
    *,
    config_path: Path | None = None,
) -> None:
    """Remove a printer from the config file."""
    path = config_path or get_config_path()
    raw = _read_config_file(path)
    printers = raw.get("printers", {})

    if name not in printers:
        raise ValueError(f"Printer {name!r} not found.")

    del printers[name]

    if raw.get("active_printer") == name:
        if printers:
            raw["active_printer"] = next(iter(printers))
        else:
            raw.pop("active_printer", None)

    _write_config_file(path, raw)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_printer_config(cfg: Dict[str, Any]) -> Tuple[bool, str | None]:
    """Check that a printer config dict has the required fields.

    Returns ``(True, None)`` or ``(False, error_message)``.
    """
    ptype = cfg.get("type", "")
    if ptype not in ("octoprint", "moonraker", "bambu", "prusaconnect"):
        return False, f"Unknown printer type: {ptype!r}"

    host = cfg.get("host", "")
    if not host:
        return False, "host is required"

    if ptype == "octoprint":
        if not cfg.get("api_key"):
            return False, "api_key is required for OctoPrint printers"

    if ptype == "bambu":
        if not cfg.get("access_code"):
            return False, "access_code is required for Bambu printers"
        if not cfg.get("serial"):
            return False, "serial is required for Bambu printers"

    return True, None


# ---------------------------------------------------------------------------
# Billing configuration
# ---------------------------------------------------------------------------


def get_billing_config(
    *,
    config_path: Path | None = None,
) -> Dict[str, Any]:
    """Return the ``billing`` section of the config file.

    Returns an empty dict if the section doesn't exist.  Environment
    variable overrides:

    - ``KILN_BILLING_MAX_PER_ORDER`` → ``spend_limits.max_per_order_usd``
    - ``KILN_BILLING_MONTHLY_CAP``  → ``spend_limits.monthly_cap_usd``
    """
    path = config_path or get_config_path()
    raw = _read_config_file(path)
    billing = raw.get("billing", {})
    if not isinstance(billing, dict):
        billing = {}

    # Ensure user_id exists.
    if "user_id" not in billing:
        billing["user_id"] = str(uuid.uuid4())
        raw["billing"] = billing
        _write_config_file(path, raw)

    # Env var overrides for spend limits.
    limits = billing.setdefault("spend_limits", {})
    env_max = os.environ.get("KILN_BILLING_MAX_PER_ORDER")
    if env_max:
        try:
            limits["max_per_order_usd"] = float(env_max)
        except ValueError:
            pass
    env_cap = os.environ.get("KILN_BILLING_MONTHLY_CAP")
    if env_cap:
        try:
            limits["monthly_cap_usd"] = float(env_cap)
        except ValueError:
            pass

    return billing


def save_billing_config(
    data: Dict[str, Any],
    *,
    config_path: Path | None = None,
) -> None:
    """Write the ``billing`` section to the config file.

    Merges *data* into the existing billing section (does not clobber
    other config sections).
    """
    path = config_path or get_config_path()
    raw = _read_config_file(path)
    existing = raw.get("billing", {})
    if not isinstance(existing, dict):
        existing = {}
    existing.update(data)
    raw["billing"] = existing
    _write_config_file(path, raw)


def get_or_create_user_id(
    *,
    config_path: Path | None = None,
) -> str:
    """Return the user ID from billing config, creating one if needed."""
    billing = get_billing_config(config_path=config_path)
    return billing["user_id"]
