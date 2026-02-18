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

import contextlib
import logging
import os
import re
import stat
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Valid top-level keys in the config file.  Used for schema validation.
_KNOWN_KEYS: set[str] = {
    "printers",
    "active_printer",
    "settings",
    "billing",
    "licensing",
    "trusted_printers",
    "autonomy",
    "monitoring",
}


def get_config_path() -> Path:
    """Return the default config file path (``~/.kiln/config.yaml``)."""
    return Path.home() / ".kiln" / "config.yaml"


def _normalize_host(host: str, printer_type: str = "octoprint") -> str:
    """Normalize *host* for the given printer type.

    HTTP-based backends (OctoPrint, Moonraker, Prusa Link) get an
    ``http://`` scheme prefix if missing.  MQTT/FTPS backends (Bambu)
    need a raw hostname/IP — no scheme is prepended.
    """
    host = host.strip()
    if printer_type in ("bambu", "elegoo"):
        # Strip any accidental scheme — MQTT/FTPS/WebSocket need raw host.
        host = re.sub(r"^https?://", "", host, flags=re.IGNORECASE)
        return host.rstrip("/")
    if printer_type == "serial":
        # Serial port paths (e.g. /dev/ttyUSB0, COM3) — return as-is.
        return host
    if host and not re.match(r"^https?://", host, re.IGNORECASE):
        host = "http://" + host
    return host.rstrip("/")


def _validate_printer_url(url: str, *, printer_type: str = "octoprint") -> tuple[str, list[str]]:
    """Validate and clean a printer host URL.

    Performs the following checks:

    1. Strips trailing slashes.
    2. Ensures URL has an ``http://`` or ``https://`` scheme (for HTTP
       backends).  Bambu printers use raw hostnames — no scheme is added.
    3. Ensures URL does not end with a path separator.
    4. Attempts a basic HTTP HEAD request (5 s timeout) to verify
       reachability.  A failure produces a warning but does **not** block
       registration.
    5. Returns warnings for any malformed-looking URLs.

    :param url: Raw printer URL or hostname.
    :param printer_type: Backend type (``"octoprint"``, ``"moonraker"``,
        ``"bambu"``, ``"prusaconnect"``).
    :returns: ``(cleaned_url, warnings)`` where *warnings* is a list of
        human-readable strings (empty if everything looks good).
    """
    warnings: list[str] = []

    if not url or not url.strip():
        return "", ["URL is empty"]

    cleaned = _normalize_host(url, printer_type)

    if not cleaned:
        return "", ["URL is empty after normalization"]

    # Bambu and Elegoo printers use raw hostnames -- skip HTTP scheme checks.
    if printer_type in ("bambu", "elegoo"):
        # Basic hostname sanity check
        if " " in cleaned:
            warnings.append(f"Hostname contains spaces: {cleaned!r}")
        return cleaned, warnings

    # Serial printers use port paths -- skip all HTTP/URL validation.
    if printer_type == "serial":
        return cleaned, warnings

    # Scheme check (already handled by _normalize_host, but be explicit)
    if not re.match(r"^https?://", cleaned, re.IGNORECASE):
        warnings.append(f"URL missing http:// or https:// scheme: {cleaned!r}. Prepending http://.")
        cleaned = "http://" + cleaned

    # Strip trailing path separators (beyond the authority)
    cleaned = cleaned.rstrip("/")

    # Check for double slashes after scheme (malformed)
    scheme_end = cleaned.index("://") + 3
    authority = cleaned[scheme_end:]
    if "//" in authority:
        warnings.append(f"URL contains double slashes in path: {cleaned!r}. This may indicate a malformed URL.")

    # Check for obviously bad patterns
    if " " in cleaned:
        warnings.append(f"URL contains spaces: {cleaned!r}")
    if ".." in authority:
        warnings.append(f"URL contains '..' traversal: {cleaned!r}")

    # Connectivity check (best-effort, does not block)
    try:
        import requests

        requests.head(cleaned, timeout=5, verify=False, allow_redirects=True)
    except ImportError:
        pass  # requests not available in this context
    except Exception as exc:
        warnings.append(
            f"Could not reach {cleaned} (HEAD request failed: {exc}). "
            "The printer may be offline or the URL may be incorrect."
        )

    return cleaned, warnings


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def _check_file_permissions(path: Path) -> None:
    """Warn if *path* is readable by group or others.

    Skipped on Windows where POSIX permission semantics do not apply.
    """
    if sys.platform == "win32":
        return
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH):
            logger.warning(
                "Config file %s has overly permissive permissions (mode %04o). Recommended: chmod 600 %s",
                path,
                stat.S_IMODE(mode),
                path,
            )
    except OSError:
        pass


def _check_dir_permissions(dir_path: Path) -> None:
    """Warn and fix if the config directory is accessible by others.

    Skipped on Windows where POSIX permission semantics do not apply.
    """
    if sys.platform == "win32":
        return
    try:
        dir_mode = stat.S_IMODE(dir_path.stat().st_mode)
        if dir_mode & 0o077:
            logger.warning(
                "~/.kiln/ directory has overly permissive permissions (mode %04o). Run 'chmod 700 ~/.kiln/' to fix.",
                dir_mode,
            )
        dir_path.chmod(0o700)
    except OSError:
        pass


def _validate_config_schema(data: dict[str, Any], path: Path) -> None:
    """Log warnings for unknown or missing keys in the config file."""
    if not data:
        return

    # Warn on unknown top-level keys
    unknown = set(data.keys()) - _KNOWN_KEYS
    for key in sorted(unknown):
        logger.warning(
            "Config file %s contains unknown key %r (expected one of: %s)",
            path,
            key,
            ", ".join(sorted(_KNOWN_KEYS)),
        )

    # Warn on missing recommended keys
    if "printers" not in data:
        logger.warning(
            "Config file %s is missing 'printers' section -- "
            "run 'kiln setup' for guided setup or 'kiln auth' to add a printer",
            path,
        )


def _read_config_file(path: Path) -> dict[str, Any]:
    """Read and parse the YAML config file; return ``{}`` on any failure."""
    if not path.is_file():
        return {}
    _check_file_permissions(path)
    _check_dir_permissions(path.parent)
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict):
            return {}
        _validate_config_schema(data, path)
        return data
    except yaml.YAMLError as exc:
        logger.warning(
            "Config file %s has invalid YAML: %s — run 'kiln setup' to regenerate, or fix manually.",
            path,
            exc,
        )
        return {}
    except OSError as exc:
        logger.warning("Could not read config file %s: %s", path, exc)
        return {}


def _write_config_file(path: Path, data: dict[str, Any]) -> None:
    """Write *data* to the YAML config file, creating dirs as needed.

    Sets file permissions to ``0600`` (owner read/write only) since the
    config may contain API keys and access codes.  Also enforces ``0700``
    on the parent directory.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _check_dir_permissions(path.parent)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            path.chmod(0o600)


def load_printer_config(
    printer_name: str | None = None,
    *,
    config_path: Path | None = None,
) -> dict[str, Any]:
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
        ptype = os.environ.get("KILN_PRINTER_TYPE", "octoprint")
        return {
            "type": ptype,
            "host": _normalize_host(env_host, ptype),
            "api_key": os.environ.get("KILN_PRINTER_API_KEY", ""),
            "access_code": os.environ.get("KILN_PRINTER_ACCESS_CODE", ""),
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
                "No printers configured. Run 'kiln setup' for guided network discovery and setup, "
                "or add one manually with:\n"
                "  kiln auth --name my-printer --host <IP_OR_HOSTNAME> --type <TYPE> --api-key <KEY>\n"
                "Supported types: octoprint, moonraker, bambu, prusaconnect"
            )
        else:
            raise ValueError(
                "Multiple printers configured but no active printer set.  "
                "Run 'kiln use <name>' to select one, or pass --printer."
            )

    if name not in printers:
        raise ValueError(
            f"Printer {name!r} not found in config. "
            f"Available: {', '.join(printers.keys())}. "
            f"Run 'kiln printers' to list all, or 'kiln use <name>' to switch."
        )

    cfg = dict(printers[name])
    cfg.setdefault("timeout", settings.get("timeout", 30))
    cfg.setdefault("retries", settings.get("retries", 3))
    cfg["host"] = _normalize_host(str(cfg.get("host", "")), str(cfg.get("type", "octoprint")))
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
    printer_model: str | None = None,
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

    cleaned_host, url_warnings = _validate_printer_url(host, printer_type=printer_type)

    entry: dict[str, Any] = {
        "type": printer_type,
        "host": cleaned_host,
    }
    if printer_type == "octoprint" or printer_type == "moonraker":
        if api_key:
            entry["api_key"] = api_key
    elif printer_type == "bambu":
        if access_code:
            entry["access_code"] = access_code
        if serial:
            entry["serial"] = serial
    elif printer_type == "elegoo":
        # Elegoo SDCP uses no auth; optional mainboard_id for identification.
        if serial:
            entry["mainboard_id"] = serial
    elif printer_type == "prusaconnect":
        if api_key:
            entry["api_key"] = api_key
        if printer_model:
            entry["printer_model"] = printer_model
    elif printer_type == "serial":
        # For serial printers, 'host' stores the serial port path.
        pass

    printers[name] = entry

    if set_active or "active_printer" not in raw:
        raw["active_printer"] = name

    raw.setdefault("settings", {"timeout": 30, "retries": 3})
    _write_config_file(path, raw)

    if url_warnings:
        for w in url_warnings:
            logger.warning("Printer %r URL: %s", name, w)

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
        raise ValueError(f"Printer {name!r} not found.  Available: {', '.join(printers.keys()) or '(none)'}")

    raw["active_printer"] = name
    _write_config_file(path, raw)


def list_printers(
    *,
    config_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return a list of saved printers with name, type, host, active flag."""
    path = config_path or get_config_path()
    raw = _read_config_file(path)
    printers = raw.get("printers", {})
    active = raw.get("active_printer", "")

    result: list[dict[str, Any]] = []
    for name, cfg in printers.items():
        if not isinstance(cfg, dict):
            continue
        result.append(
            {
                "name": name,
                "type": cfg.get("type", "unknown"),
                "host": cfg.get("host", ""),
                "active": name == active,
            }
        )
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


def validate_printer_config(cfg: dict[str, Any]) -> tuple[bool, str | None]:
    """Check that a printer config dict has the required fields.

    Returns ``(True, None)`` or ``(False, error_message)``.
    """
    ptype = cfg.get("type", "")
    if ptype not in ("octoprint", "moonraker", "bambu", "elegoo", "prusaconnect", "serial"):
        return False, f"Unknown printer type: {ptype!r}"

    host = cfg.get("host", "")
    if not host:
        return False, "host is required"

    if ptype == "octoprint" and not cfg.get("api_key"):
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
) -> dict[str, Any]:
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
        with contextlib.suppress(ValueError):
            limits["max_per_order_usd"] = float(env_max)
    env_cap = os.environ.get("KILN_BILLING_MONTHLY_CAP")
    if env_cap:
        with contextlib.suppress(ValueError):
            limits["monthly_cap_usd"] = float(env_cap)

    return billing


def save_billing_config(
    data: dict[str, Any],
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


# ---------------------------------------------------------------------------
# Trusted printers whitelist
# ---------------------------------------------------------------------------


def get_trusted_printers(
    *,
    config_path: Path | None = None,
) -> list[str]:
    """Return the list of trusted printer hostnames/IPs.

    Checks ``KILN_TRUSTED_PRINTERS`` env var first (comma-separated).
    Falls back to the ``trusted_printers`` list in the config file.
    """
    env_val = os.environ.get("KILN_TRUSTED_PRINTERS", "")
    if env_val:
        return [h.strip() for h in env_val.split(",") if h.strip()]

    path = config_path or get_config_path()
    raw = _read_config_file(path)
    trusted = raw.get("trusted_printers", [])
    if not isinstance(trusted, list):
        return []
    return [str(h) for h in trusted]


def add_trusted_printer(
    host: str,
    *,
    config_path: Path | None = None,
) -> None:
    """Add a hostname/IP to the trusted printers list."""
    host = host.strip()
    if not host:
        raise ValueError("host is required")

    path = config_path or get_config_path()
    raw = _read_config_file(path)
    trusted = raw.get("trusted_printers", [])
    if not isinstance(trusted, list):
        trusted = []

    if host not in trusted:
        trusted.append(host)
        raw["trusted_printers"] = trusted
        _write_config_file(path, raw)


def remove_trusted_printer(
    host: str,
    *,
    config_path: Path | None = None,
) -> None:
    """Remove a hostname/IP from the trusted printers list.

    Raises :class:`ValueError` if the host is not in the list.
    """
    host = host.strip()
    path = config_path or get_config_path()
    raw = _read_config_file(path)
    trusted = raw.get("trusted_printers", [])
    if not isinstance(trusted, list):
        trusted = []

    if host not in trusted:
        raise ValueError(f"Printer {host!r} is not in the trusted list.")

    trusted.remove(host)
    raw["trusted_printers"] = trusted
    _write_config_file(path, raw)


def is_trusted_printer(
    host: str,
    *,
    config_path: Path | None = None,
) -> bool:
    """Check whether a hostname/IP is in the trusted printers list."""
    trusted = get_trusted_printers(config_path=config_path)
    return host.strip() in trusted
