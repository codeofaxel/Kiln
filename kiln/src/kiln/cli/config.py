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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from kiln.printer_backends import (
    DEFAULT_SERIAL_BAUDRATE,
    PRINTER_TYPES,
    format_printer_types,
    missing_needs,
    needs_sentence,
)

logger = logging.getLogger(__name__)


# Renamed printer-type aliases.  A saved config that still pins an old name
# keeps working — we normalize on read and warn once, so users update at
# their leisure instead of hitting "unsupported printer type".  Every door
# that reads a type from a user (env, config.yaml, the register_printer
# tool) runs it through _normalize_printer_type; an alias that only some
# doors honour is the same bug with extra steps.
_LEGACY_PRINTER_TYPE_ALIASES = {
    "prusaconnect": "prusalink",
    # "serial" named the wire protocol; users and the README call the
    # backend USB.  The identifier sense of the word is spoken for by
    # `--serial` (a Bambu serial number), which is what it should mean.
    "serial": "usb",
}
_warned_legacy_printer_types: set[str] = set()


def _normalize_printer_type(ptype: str) -> str:
    """Map a renamed/legacy printer-type string to its current name.

    Returns *ptype* unchanged when it isn't a known legacy alias.
    """
    canonical = _LEGACY_PRINTER_TYPE_ALIASES.get(ptype)
    if canonical is None:
        return ptype
    if ptype not in _warned_legacy_printer_types:
        _warned_legacy_printer_types.add(ptype)
        logger.warning(
            "Printer type %r is now called %r; using %r. "
            "Update your config's `type:` to silence this notice.",
            ptype,
            canonical,
            canonical,
        )
    return canonical

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
    "prints_dir",
}


def get_config_path() -> Path:
    """Return the default config file path (``~/.kiln/config.yaml``)."""
    return Path.home() / ".kiln" / "config.yaml"


def _normalize_host(host: str, printer_type: str = "octoprint") -> str:
    """Normalize *host* for the given printer type.

    HTTP-based backends (OctoPrint, Moonraker, Creality, Prusa Link) get an
    ``http://`` scheme prefix if missing.  MQTT/FTPS backends (Bambu)
    need a raw hostname/IP — no scheme is prepended.
    """
    host = host.strip()
    if printer_type in ("bambu", "elegoo"):
        # Strip any accidental scheme — MQTT/FTPS/WebSocket need raw host.
        host = re.sub(r"^https?://", "", host, flags=re.IGNORECASE)
        return host.rstrip("/")
    if printer_type == "usb":
        # Serial port paths (e.g. /dev/ttyUSB0, COM3) — return as-is.
        return host
    if host and not re.match(r"^https?://", host, re.IGNORECASE):
        host = "http://" + host
    return host.rstrip("/")


def _validate_printer_url(url: str, *, printer_type: str = "octoprint", probe: bool = True) -> tuple[str, list[str]]:
    """Validate and clean a printer host URL.

    Performs the following checks:

    1. Strips trailing slashes.
    2. Ensures URL has an ``http://`` or ``https://`` scheme (for HTTP
       backends).  Bambu printers use raw hostnames — no scheme is added.
    3. Ensures URL does not end with a path separator.
    4. When *probe* is on, attempts a basic HTTP HEAD request (5 s
       timeout) to verify reachability.  A failure produces a warning but
       does not refuse the URL; it does hold the caller for up to 5 s.
    5. Returns warnings for any malformed-looking URLs.

    :param url: Raw printer URL or hostname.
    :param printer_type: Backend type (``"octoprint"``, ``"moonraker"``,
        ``"creality"``, ``"bambu"``, ``"prusalink"``, ``"duet"``).
    :param probe: Ask the address over the network.  Off for a caller that
        has already asked (see :func:`_check_printer_url`).
    :returns: ``(cleaned_url, warnings)`` where *warnings* is a list of
        human-readable strings (empty if everything looks good).
    """
    cleaned, warnings, _answered = _check_printer_url(url, printer_type=printer_type, probe=probe)
    return cleaned, warnings


def _check_printer_url(
    url: str, *, printer_type: str = "octoprint", probe: bool = True
) -> tuple[str, list[str], bool | None]:
    """:func:`_validate_printer_url`, plus whether the address answered.

    The third value is ``True`` when the HEAD request got any answer,
    ``False`` when it failed, and ``None`` when nothing was asked: *probe*
    off, a backend that is not spoken to over HTTP (Bambu, Elegoo, USB),
    an address too malformed to clean, or ``requests`` not installed.  A
    caller that goes on to talk to the printer reads it, so an address
    that did not answer is not asked again at 30 s a try.
    """
    warnings: list[str] = []

    if not url or not url.strip():
        return "", ["URL is empty"], None

    cleaned = _normalize_host(url, printer_type)

    if not cleaned:
        return "", ["URL is empty after normalization"], None

    # Bambu and Elegoo printers use raw hostnames -- skip HTTP scheme checks.
    if printer_type in ("bambu", "elegoo"):
        # Basic hostname sanity check
        if " " in cleaned:
            warnings.append(f"Hostname contains spaces: {cleaned!r}")
        return cleaned, warnings, None

    # USB printers use port paths -- skip all HTTP/URL validation.
    if printer_type == "usb":
        return cleaned, warnings, None

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

    # Connectivity check (best-effort: a miss warns, never refuses)
    if not probe:
        return cleaned, warnings, None
    try:
        import requests
    except ImportError:
        return cleaned, warnings, None  # requests not available in this context
    try:
        requests.head(cleaned, timeout=5, allow_redirects=True)
    except Exception as exc:
        warnings.append(
            f"Could not reach {cleaned} (HEAD request failed: {exc}). "
            "The printer may be offline or the URL may be incorrect."
        )
        return cleaned, warnings, False

    return cleaned, warnings, True


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
        ptype = _normalize_printer_type(os.environ.get("KILN_PRINTER_TYPE", "octoprint"))
        return {
            "type": ptype,
            "host": _normalize_host(env_host, ptype),
            "api_key": os.environ.get("KILN_PRINTER_API_KEY", ""),
            "access_code": os.environ.get("KILN_PRINTER_ACCESS_CODE", ""),
            "serial": os.environ.get("KILN_PRINTER_SERIAL", ""),
            "printer_model": os.environ.get("KILN_PRINTER_MODEL", ""),
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
                f"Supported types: {format_printer_types(quote='')}"
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
    cfg["type"] = _normalize_printer_type(str(cfg.get("type", "octoprint")))
    cfg["host"] = _normalize_host(str(cfg.get("host", "")), str(cfg.get("type", "octoprint")))
    return cfg


# ---------------------------------------------------------------------------
# Save / mutate
# ---------------------------------------------------------------------------

#: Entry keys that name ONE physical machine: a Bambu serial, an Elegoo
#: mainboard id.  Types without one are known only by an address, which
#: legitimately changes when the router hands out a new lease.
_IDENTITY_KEYS: tuple[str, ...] = ("serial", "mainboard_id")

#: Keys that describe one user-supplied camera.  Replaced together: a new
#: snapshot URL beside a kept stream URL could describe two cameras.
_CAMERA_KEYS: tuple[str, ...] = ("camera_snapshot_url", "camera_stream_url")

#: A credential is issued for one machine.  Kiln sends it to whatever
#: address the entry names, so it never follows a name to an address that
#: nothing proves belongs to the same machine.  A camera URL is in this
#: set because a stream URL may embed a password.
_BOUND_TO_MACHINE_KEYS: tuple[str, ...] = (
    "api_key",
    "access_code",
    *_IDENTITY_KEYS,
    *_CAMERA_KEYS,
)


class PrinterNameTaken(ValueError):
    """A save would put a different machine under a name already saved.

    Raised before anything is written.  ``suggested_name`` is the first
    free ``<name>-<n>``, for a door that offers one.
    """

    def __init__(self, message: str, *, name: str, suggested_name: str) -> None:
        super().__init__(message)
        self.name = name
        self.suggested_name = suggested_name


@dataclass(frozen=True)
class PrinterSaveResult:
    """What :func:`save_printer` did to the config file.

    ``outcome`` is ``"added"`` (a new name), ``"updated"`` (the same
    machine, matched by serial or by an unchanged address) or
    ``"replaced"`` (a printer with no serial-style identity whose address
    changed, so Kiln cannot tell whether it is the same machine).
    ``kept`` names the saved fields the caller did not resend and that
    were carried over; ``dropped`` names the saved fields a ``"replaced"``
    save left behind because they belong to the machine at the old
    address (its credential, its serial, its camera).  Names only, never
    values.
    """

    path: Path
    name: str
    outcome: str
    previous_host: str | None = None
    host: str | None = None
    kept: tuple[str, ...] = ()
    dropped: tuple[str, ...] = ()

    @property
    def notice(self) -> str | None:
        """One sentence for the person, or ``None`` for a new name."""
        if self.outcome == "added":
            return None
        kept = f" Kept {', '.join(self.kept)} from the saved entry." if self.kept else ""
        if self.outcome == "updated":
            moved = (
                f" Its address changed from {self.previous_host} to {self.host}."
                if self.previous_host and self.host and self.previous_host != self.host
                else ""
            )
            return f"Updated existing printer {self.name!r}.{moved}{kept}"
        was = f" (was at {self.previous_host})" if self.previous_host else ""
        dropped = (
            f" Not carried over: {', '.join(self.dropped)}; enter them again if this is "
            "the same printer."
            if self.dropped
            else ""
        )
        check = (
            " If this is a different machine, check those, or save it under another name."
            if self.kept
            else ""
        )
        return (
            f"Replaced the saved entry for {self.name!r}{was}. Kiln cannot tell from an "
            f"address alone whether this is the same machine.{dropped}{kept}{check}"
        )


@dataclass(frozen=True)
class SavedPrinterMatch:
    """The saved entry a save under a name in use builds on, judged.

    ``outcome`` is ``"updated"`` (the same machine, by serial or by an
    unchanged address) or ``"replaced"`` (no serial-style identity and the
    address changed).  ``entry`` is what the new entry inherits: a copy of
    the saved one, less the fields a ``"replaced"`` save leaves behind,
    which ``dropped`` names.
    """

    outcome: str
    entry: dict[str, Any]
    previous_host: str | None
    dropped: tuple[str, ...] = ()


def _printer_identity(entry: dict[str, Any]) -> str | None:
    """The serial-style id that names *entry*'s machine, or ``None``."""
    for key in _IDENTITY_KEYS:
        value = str(entry.get(key) or "").strip()
        if value:
            return value.upper()
    return None


def _identity_from_args(printer_type: str, serial: str | None) -> str | None:
    """The id a save of this type would store from *serial*, or ``None``.

    Only Bambu (``serial``) and Elegoo (``mainboard_id``) store one.
    """
    if _normalize_printer_type(printer_type) not in ("bambu", "elegoo"):
        return None
    value = str(serial or "").strip()
    return value.upper() or None


def _free_printer_name(name: str, printers: dict[str, Any]) -> str:
    n = 2
    while f"{name}-{n}" in printers:
        n += 1
    return f"{name}-{n}"


def _same_host(a: Any, b: Any) -> bool:
    return str(a or "").strip().rstrip("/").lower() == str(b or "").strip().rstrip("/").lower()


def _judge_existing_printer(
    name: str,
    printers: dict[str, Any],
    printer_type: str,
    identity: str | None,
) -> dict[str, Any] | None:
    """The saved entry a save under *name* would update, or ``None`` for a new name.

    Raises :class:`PrinterNameTaken` when the saved entry is a different
    machine: another printer type, or another serial.  The one decision
    every door reaches, through :func:`save_printer` or
    :func:`saved_printer_for_update`.
    """
    existing = printers.get(name)
    if not isinstance(existing, dict):
        return None
    suggestion = _free_printer_name(name, printers)
    old_type = _normalize_printer_type(str(existing.get("type") or ""))
    old_host = str(existing.get("host") or "?")
    remedy = (
        f"Save this one under another name (for example {suggestion!r}), or remove the "
        f"saved one first (kiln remove {name})."
    )
    if old_type != _normalize_printer_type(printer_type):
        raise PrinterNameTaken(
            f"{name!r} is already saved for a different kind of printer ({old_type or 'unknown'} "
            f"at {old_host}); this one is {printer_type}. {remedy}",
            name=name,
            suggested_name=suggestion,
        )
    old_identity = _printer_identity(existing)
    if identity and old_identity and identity != old_identity:
        raise PrinterNameTaken(
            f"A different printer is already saved as {name!r}: serial ending "
            f"{old_identity[-4:]}, at {old_host}. {remedy} To change that printer, "
            f"register it again under {name!r} with its own serial.",
            name=name,
            suggested_name=suggestion,
        )
    return existing


def _match_saved_printer(
    name: str,
    printers: dict[str, Any],
    existing: dict[str, Any],
    host: str,
    identity: str | None,
) -> SavedPrinterMatch:
    """How a save at *host* relates to *existing*, already judged by type and serial.

    Raises :class:`PrinterNameTaken` when the saved entry names a machine
    by serial, this save names none, and the address changed: nothing
    shows it is that machine, so neither its serial nor its credential
    may be carried to the new address.
    """
    old_host = str(existing.get("host") or "") or None
    old_identity = _printer_identity(existing)
    same_machine = bool(identity and identity == old_identity)
    if same_machine or _same_host(old_host, host):
        return SavedPrinterMatch("updated", dict(existing), old_host)
    if old_identity:
        suggestion = _free_printer_name(name, printers)
        raise PrinterNameTaken(
            f"{name!r} is saved for a printer with serial ending {old_identity[-4:]}, at "
            f"{old_host or '?'}; this save names no serial and a different address, so Kiln "
            f"cannot tell whether it is that printer. Pass its serial to update it, or save "
            f"this one under another name (for example {suggestion!r}).",
            name=name,
            suggested_name=suggestion,
        )
    inherited = {k: v for k, v in existing.items() if k not in _BOUND_TO_MACHINE_KEYS}
    dropped = tuple(k for k in _BOUND_TO_MACHINE_KEYS if k in existing)
    return SavedPrinterMatch("replaced", inherited, old_host, dropped)


def saved_printer_for_update(
    name: str,
    printer_type: str,
    host: str,
    *,
    serial: str | None = None,
    config_path: Path | None = None,
    session: dict[str, dict[str, Any]] | None = None,
) -> SavedPrinterMatch | None:
    """Ask, without writing, what saving *name* would build on.

    Returns the :class:`SavedPrinterMatch` a :func:`save_printer` call with
    these arguments would make, ``None`` for a new name, and raises
    :class:`PrinterNameTaken` exactly when that call would.  For a door
    that builds a live adapter before it saves: the adapter then carries
    the same model and camera the saved entry will, and nothing a
    ``"replaced"`` save leaves behind.  *host* is the address as the door
    will save it.

    *session* holds printers a door registered without saving, as entries
    shaped like the saved ones.  They are judged by the same rule and win
    over the file for the same name, because the session's printer is the
    one a registration under that name would replace.
    """
    raw = _read_config_file(config_path or get_config_path())
    saved = raw.get("printers")
    printers: dict[str, Any] = dict(saved) if isinstance(saved, dict) else {}
    printers.update(session or {})
    if not printers:
        return None
    identity = _identity_from_args(printer_type, serial)
    existing = _judge_existing_printer(name, printers, printer_type, identity)
    if existing is None:
        return None
    return _match_saved_printer(name, printers, existing, host, identity)


def unsaved_printer_entry(
    printer_type: str,
    host: str,
    *,
    serial: str | None = None,
    printer_model: str | None = None,
    camera_snapshot_url: str | None = None,
    camera_stream_url: str | None = None,
) -> dict[str, Any]:
    """A printer registered without saving, in the shape :func:`save_printer` writes.

    For a door that keeps unsaved registrations beside the config file so
    a later save under the same name is judged by one rule.  Carries no
    credential: the judgement never needs one.
    """
    return _printer_entry(
        printer_type,
        host,
        serial=serial,
        printer_model=printer_model,
        camera_snapshot_url=camera_snapshot_url,
        camera_stream_url=camera_stream_url,
    )


def _printer_entry(
    printer_type: str,
    host: str,
    *,
    api_key: str | None = None,
    access_code: str | None = None,
    serial: str | None = None,
    printer_model: str | None = None,
    baudrate: int | None = None,
    camera_snapshot_url: str | None = None,
    camera_stream_url: str | None = None,
) -> dict[str, Any]:
    """The config entry these arguments describe -- only the fields given."""
    entry: dict[str, Any] = {
        "type": printer_type,
        "host": host,
    }
    # Duet carries the machine password (M551) in the generic api_key slot.
    if printer_type in ("octoprint", "moonraker", "creality", "duet"):
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
    elif printer_type == "prusalink":
        if api_key:
            entry["api_key"] = api_key
    elif printer_type == "usb":
        # For USB printers, 'host' stores the serial port path.
        pass

    if printer_model:
        entry["printer_model"] = printer_model

    # Record a baud rate only when it differs from the default, so changing
    # the default later stays an edit rather than a migration of every
    # config that pinned it.
    if printer_type == "usb" and baudrate and baudrate != DEFAULT_SERIAL_BAUDRATE:
        entry["baudrate"] = int(baudrate)

    if camera_snapshot_url:
        entry["camera_snapshot_url"] = camera_snapshot_url.strip()
    if camera_stream_url:
        entry["camera_stream_url"] = camera_stream_url.strip()
    return entry



def save_printer(
    name: str,
    printer_type: str,
    host: str,
    *,
    api_key: str | None = None,
    access_code: str | None = None,
    serial: str | None = None,
    printer_model: str | None = None,
    baudrate: int | None = None,
    camera_snapshot_url: str | None = None,
    camera_stream_url: str | None = None,
    set_active: bool = True,
    config_path: Path | None = None,
    probe: bool = True,
) -> PrinterSaveResult:
    """Add a printer to the config file, or update the one saved under *name*.

    A save under a name already in use builds on the saved entry: only the
    fields this call passes are changed, so a model or camera stated once
    survives a later save that changes the address.  It is refused with
    :class:`PrinterNameTaken`, before anything is written, when the saved
    entry is a different machine -- another printer type, or another
    serial / mainboard id -- or a serial-bearing entry re-saved with no
    serial at a new address, which nothing shows to be the same machine.
    A printer with no serial-style id whose address changed is
    ``"replaced"``: its model is kept, but its credential, serial and
    camera stay with the old address rather than being sent to a machine
    that may be a different one, and the result's ``notice`` names them.
    Passing either camera URL replaces both saved camera URLs.

    *baudrate* applies to serial printers only; omit it to let the adapter
    fall back to :data:`DEFAULT_SERIAL_BAUDRATE`.

    *camera_snapshot_url* / *camera_stream_url* record a camera the user
    supplies (an http(s) still or MJPEG URL, or an rtsp(s) stream) for any
    printer type.  They are stored as given — a stream URL may embed a
    password, and this file already holds the printer's own secret in the
    same trust — and are read back through
    :func:`kiln.printers.base.apply_external_camera`.

    *probe* asks the address over the network before saving it (up to 5 s
    when nothing answers); a caller that has just asked passes ``False``.

    Returns a :class:`PrinterSaveResult`: the config file's path, and
    whether the printer was added, updated or replaced.
    """
    path = config_path or get_config_path()
    raw = _read_config_file(path)
    printers = raw.setdefault("printers", {})
    if not isinstance(printers, dict):
        raw["printers"] = printers = {}

    identity = _identity_from_args(printer_type, serial)
    existing = _judge_existing_printer(name, printers, printer_type, identity)

    cleaned_host, url_warnings = _validate_printer_url(host, printer_type=printer_type, probe=probe)

    entry = _printer_entry(
        printer_type,
        cleaned_host,
        api_key=api_key,
        access_code=access_code,
        serial=serial,
        printer_model=printer_model,
        baudrate=baudrate,
        camera_snapshot_url=camera_snapshot_url,
        camera_stream_url=camera_stream_url,
    )

    outcome = "added"
    previous_host: str | None = None
    kept: tuple[str, ...] = ()
    dropped: tuple[str, ...] = ()
    if existing is not None:
        match = _match_saved_printer(name, printers, existing, cleaned_host, identity)
        base = match.entry
        if any(key in entry for key in _CAMERA_KEYS):
            for key in _CAMERA_KEYS:
                base.pop(key, None)
        if printer_type == "usb" and baudrate is not None:
            base.pop("baudrate", None)
        kept = tuple(k for k in base if k not in entry and k not in ("type", "host"))
        outcome, previous_host, dropped = match.outcome, match.previous_host, match.dropped
        entry = {**base, **entry}

    printers[name] = entry

    if set_active or "active_printer" not in raw:
        raw["active_printer"] = name

    raw.setdefault("settings", {"timeout": 30, "retries": 3})
    _write_config_file(path, raw)

    if url_warnings:
        for w in url_warnings:
            logger.warning("Printer %r URL: %s", name, w)

    return PrinterSaveResult(
        path=path,
        name=name,
        outcome=outcome,
        previous_host=previous_host,
        host=cleaned_host,
        kept=kept,
        dropped=dropped,
    )


def set_printer_model(
    name: str,
    printer_model: str,
    *,
    config_path: Path | None = None,
) -> Path:
    """Record which catalogue model the saved printer *name* is.

    Changes that one field of that one entry.  Raises :class:`ValueError`
    when no printer of that name is saved.  Returns the path to the config
    file.
    """
    path = config_path or get_config_path()
    raw = _read_config_file(path)
    printers = raw.get("printers")
    if not isinstance(printers, dict) or not isinstance(printers.get(name), dict):
        raise ValueError(f"Printer {name!r} not found.")
    printers[name]["printer_model"] = printer_model
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
    ptype = _normalize_printer_type(cfg.get("type", ""))
    if ptype not in PRINTER_TYPES:
        return False, f"Unknown printer type: {ptype!r}"

    host = cfg.get("host", "")
    if not host:
        return False, "host is required"

    gaps = missing_needs(ptype, cfg)
    if gaps:
        keys = " and ".join(need.key for need in gaps)
        return False, f"{needs_sentence(ptype, gaps)} In config.yaml it goes under {keys}."

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


# ---------------------------------------------------------------------------
# Prints directory
# ---------------------------------------------------------------------------

_DEFAULT_PRINTS_DIR = Path.home() / "Kiln" / "prints"


def get_prints_dir(
    *,
    config_path: Path | None = None,
) -> Path:
    """Return the base directory for saved print artifacts.

    Resolution order:
        1. ``KILN_PRINTS_DIR`` environment variable.
        2. ``prints_dir`` key in the config file.
        3. Default: ``~/Kiln/prints/``.

    The directory is created automatically if it does not exist.
    """
    # 1. Env var
    env_val = os.environ.get("KILN_PRINTS_DIR", "")
    if env_val:
        prints_dir = Path(env_val).expanduser()
    else:
        # 2. Config file
        path = config_path or get_config_path()
        raw = _read_config_file(path)
        cfg_val = raw.get("prints_dir", "")
        if cfg_val:
            prints_dir = Path(str(cfg_val)).expanduser()
        else:
            # 3. Default
            prints_dir = _DEFAULT_PRINTS_DIR

    prints_dir.mkdir(parents=True, exist_ok=True)
    return prints_dir


def get_project_prints_dir(
    project_name: str,
    *,
    config_path: Path | None = None,
) -> Path:
    """Create and return a project-specific prints directory.

    Creates ``<prints_dir>/<project_name>/`` with standard subdirectories:
    ``stl``, ``gcode``, ``3mf``, and ``previews``.

    Returns the project directory path (the parent of the subdirectories).
    """
    base = get_prints_dir(config_path=config_path)
    project_dir = base / project_name
    for subdir in ("stl", "gcode", "3mf", "previews"):
        (project_dir / subdir).mkdir(parents=True, exist_ok=True)
    return project_dir
