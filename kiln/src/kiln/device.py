"""Anonymous device fingerprint for usage heartbeats.

Computes a stable, salted hash of the OS-level machine identifier so
that the heartbeat (``kiln/heartbeat.py``) can distinguish "many
installs on the same device" (e.g. a user who reinstalls Kiln, or runs
it in throwaway Docker containers) from "many devices."  Without this,
``installation_id`` over-counts users by the reinstall × multi-device
factor.

What we hash, by platform:

    macOS    — IOPlatformUUID  (via ``ioreg -d2 -c IOPlatformExpertDevice``)
    Linux    — ``/etc/machine-id`` (fallback ``/var/lib/dbus/machine-id``)
    Windows  — HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid

The hash is ``sha256(<machine_id> || "|" || <salt>)`` truncated to 32
hex chars — 128 bits, enough that collisions are vanishingly unlikely
across a fleet that's orders of magnitude larger than ours.

Privacy posture (mirrored in PRIVACY.md §3.1):

    1. ONE-WAY.  We cannot recover the OS machine ID from the
       fingerprint.  No reverse-lookup table is stored anywhere.
    2. SALT-SCOPED.  The salt (``kiln-device-fingerprint-v1``) is
       Kiln-specific.  Other products that hash the same machine ID
       with a different salt produce different fingerprints, so our
       data cannot be cross-correlated with theirs.
    3. DEVICE, NOT PERSON.  Stable across Kiln reinstalls and OS
       restarts; resets when the OS is reinstalled (most platforms
       regenerate machine IDs on OS reinstall).  Two roommates
       sharing a laptop = one fingerprint.  One person with two
       laptops = two fingerprints.
    4. FAIL-SOFT.  ``get_device_fingerprint()`` returns ``""`` if
       the platform tool is missing, refuses to read, or any other
       error — the heartbeat then sends without a fingerprint
       rather than crashing.  Default-ON telemetry must never break
       Kiln itself.
    5. CACHED.  Computed at most once per process, so the
       per-heartbeat overhead is zero after the first call.

Disable telemetry entirely (including the fingerprint) with
``KILN_TELEMETRY=false`` — see ``kiln/heartbeat.py``.

Usage::

    from kiln.device import get_device_fingerprint

    fp = get_device_fingerprint()  # "" on failure, 32 hex chars on success
"""
from __future__ import annotations

import hashlib
import logging
import platform
import subprocess
import threading
from pathlib import Path

_logger = logging.getLogger(__name__)

# Versioned salt — bumping the suffix invalidates every prior
# fingerprint at once, which is how we'd handle a hypothetical
# salt-rotation event.  Public salt is fine: its job is to scope
# the hash to Kiln, not to be a secret.
_SALT = "kiln-device-fingerprint-v1"

# 128 bits.  At 1M devices the birthday-collision probability is
# < 1.5e-30, which is good enough.  Smaller than 32 hex would risk
# collisions in fleets we can plausibly grow into; larger costs
# storage with no decision-relevant gain.
_FINGERPRINT_LEN = 32

# Sentinel: ``None`` = first call hasn't happened yet; ``""`` = we
# tried and failed (don't keep retrying every heartbeat); a 32-hex
# string = success.
_lock = threading.Lock()
_cached_fingerprint: str | None = None


def _read_macos_machine_id() -> str:
    """Pull IOPlatformUUID from ``ioreg``.

    The output looks like::

        ...
        |   "IOPlatformUUID" = "8A1E2C04-1234-5678-9ABC-DEF012345678"
        ...

    We split on the first ``=`` and strip quotes/whitespace.  Returns
    "" if ``ioreg`` isn't on PATH, the line isn't found, or the
    value is empty.
    """
    try:
        result = subprocess.run(
            ["ioreg", "-d2", "-c", "IOPlatformExpertDevice"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
        _logger.debug("device: ioreg unavailable (%s)", exc)
        return ""
    if result.returncode != 0:
        _logger.debug("device: ioreg returned %s", result.returncode)
        return ""
    for line in result.stdout.splitlines():
        if "IOPlatformUUID" not in line:
            continue
        # Defensive parse: split once on '=', strip whitespace and the
        # surrounding double-quotes.  ioreg's output format hasn't
        # changed in a decade but we don't trust it blindly.
        _, _, rhs = line.partition("=")
        candidate = rhs.strip().strip('"').strip()
        if candidate:
            return candidate
    return ""


def _read_linux_machine_id() -> str:
    """Read ``/etc/machine-id``, fallback to ``/var/lib/dbus/machine-id``.

    Both paths return the same canonical machine ID on systemd-based
    distros; the dbus path is the legacy location on older Linux.
    Returns "" if neither file is readable or both are empty.
    """
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            content = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            _logger.debug("device: %s unavailable (%s)", path, exc)
            continue
        if content:
            return content
    return ""


def _read_windows_machine_id() -> str:
    """Read ``HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid``.

    Uses the ``winreg`` standard-library module.  Returns "" if the
    module isn't available (we're not on Windows after all), the key
    is missing, or the read raises.
    """
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
    except OSError as exc:
        _logger.debug("device: MachineGuid unavailable (%s)", exc)
        return ""
    return str(value or "").strip()


def _read_machine_id() -> str:
    """Dispatch to the platform-specific reader.

    Returns "" on any unsupported OS or any failure inside the
    reader.  Used by ``get_device_fingerprint`` and as the test
    seam — tests monkeypatch this to inject deterministic IDs.
    """
    sysname = platform.system().lower()
    if sysname == "darwin":
        return _read_macos_machine_id()
    if sysname == "linux":
        return _read_linux_machine_id()
    if sysname == "windows":
        return _read_windows_machine_id()
    _logger.debug("device: unsupported platform %r", sysname)
    return ""


def _hash(machine_id: str) -> str:
    """Compute the salted, truncated SHA-256 fingerprint."""
    h = hashlib.sha256()
    h.update(machine_id.encode("utf-8"))
    h.update(b"|")
    h.update(_SALT.encode("utf-8"))
    return h.hexdigest()[:_FINGERPRINT_LEN]


def get_device_fingerprint() -> str:
    """Return the device fingerprint, computing it if needed.

    Returns:
        A 32-character lowercase hex string identifying the device,
        or ``""`` if the platform's machine ID couldn't be read.

    Threadsafe.  Caches the result for the lifetime of the process,
    including the negative ("") result so we don't spawn ``ioreg``
    every heartbeat on a misconfigured macOS box.
    """
    global _cached_fingerprint
    if _cached_fingerprint is not None:
        return _cached_fingerprint

    with _lock:
        # Double-checked: another thread may have populated while we
        # were waiting for the lock.
        if _cached_fingerprint is not None:
            return _cached_fingerprint

        machine_id = _read_machine_id()
        if not machine_id:
            _cached_fingerprint = ""
            return ""

        _cached_fingerprint = _hash(machine_id)
        return _cached_fingerprint


def _reset_cache_for_tests() -> None:
    """Test-only hook: clear the module-level cache.

    Tests need to exercise ``get_device_fingerprint`` repeatedly with
    different machine IDs.  Public so the test module can import it
    without monkeypatching private state.  Not part of the public
    API — production code never calls this.
    """
    global _cached_fingerprint
    with _lock:
        _cached_fingerprint = None
