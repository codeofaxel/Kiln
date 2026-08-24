"""Update-availability check for the locally-installed ``kiln3d`` package.

Compares the running version against the latest published on PyPI and
surfaces a non-blocking nudge through three surfaces:

* the CLI (a yellow stderr banner, interactive terminals only);
* the MCP server instructions (read by every agent on connect);
* the ``get_started`` / ``kiln_health`` tools (a structured ``update`` block).

Design mirrors :mod:`kiln.community_sync` so it carries no new dependency
and no new risk:

* stdlib :mod:`urllib` for the fetch — nothing to install;
* a disk cache at ``~/.kiln/update_check.json`` with a 24h TTL;
* the network fetch runs in a daemon thread and **never blocks** a tool
  call or CLI command — callers read whatever is already cached and the
  thread warms the cache for next time;
* opt-out via ``KILN_NO_UPDATE_CHECK`` (or the generic ``KILN_OFFLINE``);
* every failure path is non-fatal — no network, PyPI down, corrupt cache:
  the check silently yields "no nudge".

The nudge is advisory only.  Kiln never upgrades itself in-process —
swapping the code version under a running printer-control session is a
safety hazard.  The CLI exposes an explicit ``kiln upgrade`` command for
users who want the one-liner run for them.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

# The pip distribution name (NOT the import name ``kiln``) — this is what
# users type to upgrade and what PyPI indexes.
PACKAGE_NAME = "kiln3d"
_PYPI_JSON_URL = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"
UPGRADE_COMMAND = f"pip install --upgrade {PACKAGE_NAME}"

# Releases are infrequent; a daily check is plenty and keeps PyPI traffic
# negligible.  Resolved against the cache file's ``checked_at`` stamp.
_CACHE_TTL_SECONDS = 24 * 3600
_FETCH_TIMEOUT = 4.0

# One in-flight background refresh at a time, process-wide.
_refresh_lock = threading.Lock()
_refresh_in_flight = False


def _cache_path() -> Path:
    # Resolved at call time so ``HOME`` overrides (tests, sandboxes) work.
    return Path.home() / ".kiln" / "update_check.json"


def update_check_enabled() -> bool:
    """Whether this install should check PyPI for a newer Kiln.

    On by default.  Disabled by ``KILN_NO_UPDATE_CHECK`` (or the generic
    ``KILN_OFFLINE``) set to a truthy value — for CI, air-gapped boxes,
    and users who simply don't want the network call.
    """
    truthy = ("1", "true", "yes", "on")
    disabled = (
        os.environ.get("KILN_NO_UPDATE_CHECK", "").strip().lower() in truthy
        or os.environ.get("KILN_OFFLINE", "").strip().lower() in truthy
    )
    return not disabled


def _current_version() -> str:
    try:
        import kiln

        return getattr(kiln, "__version__", "unknown")
    except Exception:  # noqa: BLE001 -- version introspection is best-effort
        return "unknown"


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------


def _release_tuple(version: str) -> tuple[int, ...]:
    """Leading numeric release segment as an int tuple (``1.1.5.2`` -> ``(1,1,5,2)``).

    Pre-release / dev / local suffixes are dropped.  This is the fallback
    used only when :mod:`packaging` is unavailable; it compares release
    segments, which is correct for ordinary releases and conservatively
    declines to nudge on pre-releases (they reduce to their release tuple).
    """
    match = re.match(r"\d+(?:\.\d+)*", version.strip())
    if not match:
        return ()
    return tuple(int(part) for part in match.group(0).split("."))


def is_newer(latest: str, current: str) -> bool:
    """True if *latest* is a strictly newer release than *current*.

    Prefers :class:`packaging.version.Version` (full PEP 440 semantics);
    falls back to a release-tuple compare when ``packaging`` isn't
    importable.  Any unparseable input yields ``False`` — we never nudge
    on a version we can't reason about.
    """
    if not latest or not current or current == "unknown":
        return False
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return Version(latest) > Version(current)
        except InvalidVersion:
            return False
    except ImportError:
        lt, ct = _release_tuple(latest), _release_tuple(current)
        return bool(lt) and bool(ct) and lt > ct


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------


def _load_cache() -> dict[str, Any] | None:
    try:
        with _cache_path().open() as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _is_stale(cache: dict[str, Any]) -> bool:
    return (time.time() - float(cache.get("checked_at", 0))) > _CACHE_TTL_SECONDS


def _write_cache(latest: str, highlights: list[str] | tuple[str, ...] = ()) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            json.dump(
                {
                    "latest": latest,
                    "checked_at": time.time(),
                    "highlights": list(highlights),
                },
                f,
            )
    except OSError as exc:
        _logger.debug("Update-check cache write failed: %s", exc)


# ---------------------------------------------------------------------------
# Network fetch (daemon-thread only — never on the caller's hot path)
# ---------------------------------------------------------------------------

# The latest release's upgrade highlights, embedded in the package README as an
# HTML comment (invisible on the rendered PyPI page) and therefore present in
# the SAME PyPI JSON response the update check already fetches — no second
# request, no new endpoint.  This is the only way an older client can name
# features of a release that postdates it: its own package can't know them.
# Kept in lockstep with README_HIGHLIGHTS_RE in kiln-pro's
# scripts/audit_version_posture.py, which reconciles the block's wording.
_HIGHLIGHTS_RE = re.compile(
    r"<!--\s*kiln-highlights:\s*(?P<version>[0-9][0-9A-Za-z.\-]*)\s*\n"
    r"(?P<body>.*?)\n\s*kiln-highlights:end\s*-->",
    re.DOTALL,
)
# Defensive caps on remote-sourced copy: the description is fetched data, so a
# surprising block must degrade to "fewer/shorter highlights", never to a
# flooded tool result.
_MAX_HIGHLIGHTS = 3
_MAX_HIGHLIGHT_CHARS = 200


def _parse_highlights(description: Any, version: str) -> list[str]:
    """Highlight clauses recorded for exactly ``version``, else ``[]``.

    A block naming any other version is ignored — highlights must describe
    the release the nudge is steering the user toward, never a stale one.
    """
    if not isinstance(description, str) or not description:
        return []
    for match in _HIGHLIGHTS_RE.finditer(description):
        if match.group("version") != version:
            continue
        items = [
            line.strip()[2:].strip()
            for line in match.group("body").splitlines()
            if line.strip().startswith("* ")
        ]
        return [item[:_MAX_HIGHLIGHT_CHARS] for item in items if item][:_MAX_HIGHLIGHTS]
    return []


def _fetch_release_info() -> dict[str, Any] | None:
    """One PyPI JSON fetch → ``{"latest": str, "highlights": [str, ...]}``."""
    try:
        import urllib.request

        req = urllib.request.Request(
            _PYPI_JSON_URL,
            headers={
                "Accept": "application/json",
                "User-Agent": f"{PACKAGE_NAME}/{_current_version()}",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            if resp.status >= 300:
                _logger.debug("PyPI update check status: %s", resp.status)
                return None
            data = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 -- network/JSON errors are non-fatal
        _logger.debug("PyPI update check failed (non-fatal): %s", exc)
        return None

    info = data.get("info") or {}
    latest = info.get("version")
    if not isinstance(latest, str) or not latest:
        return None
    return {
        "latest": latest,
        "highlights": _parse_highlights(info.get("description"), latest),
    }


def _fetch_latest_from_pypi() -> str | None:
    """Latest published version only — for the explicit ``kiln upgrade`` path."""
    info = _fetch_release_info()
    return info["latest"] if info else None


def _refresh_runner() -> None:
    global _refresh_in_flight
    try:
        info = _fetch_release_info()
        if info:
            _write_cache(info["latest"], info.get("highlights") or ())
    finally:
        with _refresh_lock:
            _refresh_in_flight = False


def kick_background_check() -> None:
    """Start a background PyPI check if one isn't already running.

    Returns immediately.  Safe to call from server startup and from every
    tool call / CLI command — the in-flight guard collapses repeated calls
    into a single daemon thread.
    """
    global _refresh_in_flight
    if not update_check_enabled():
        return
    with _refresh_lock:
        if _refresh_in_flight:
            return
        _refresh_in_flight = True
    try:
        threading.Thread(
            target=_refresh_runner, daemon=True, name="kiln-update-check"
        ).start()
    except Exception as exc:  # noqa: BLE001 -- thread spawn failure is non-fatal
        _logger.debug("Update-check thread spawn failed: %s", exc)
        with _refresh_lock:
            _refresh_in_flight = False


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------


def check_for_update(current_version: str | None = None) -> dict[str, Any] | None:
    """Return an update nudge dict, or ``None`` when there's nothing to say.

    Never blocks: reads the disk cache and, if it's missing or stale,
    kicks a background refresh for next time.  On a cold cache the first
    call returns ``None`` (nothing to compare yet) and the daemon thread
    warms it — the nudge appears on the next call/session.

    Nudge dict::

        {
            "available": True,
            "current": "1.1.5.1",
            "latest":  "1.1.5.2",
            "command": "pip install --upgrade kiln3d",
            "summary": "Kiln 1.1.5.2 is available (you're on 1.1.5.1).",
        }

    When the latest release published upgrade highlights (see
    :func:`_parse_highlights`), the dict also carries ``highlights`` — a short
    list of what the new version is worth updating for, so a surface can sell
    the update instead of only announcing it.
    """
    if not update_check_enabled():
        return None

    current = current_version or _current_version()
    if current in ("", "unknown"):
        return None

    cache = _load_cache()
    if cache is None or _is_stale(cache):
        kick_background_check()
    if cache is None:
        return None

    latest = cache.get("latest")
    if not isinstance(latest, str) or not is_newer(latest, current):
        return None

    # Frame it as an offer the agent can act on, not just a command to echo.
    # Lazy import keeps the version_policy <-> version_check cycle clean.
    from kiln.version_policy import evaluate

    verdict = evaluate(current, latest=latest)
    info: dict[str, Any] = {
        "available": True,
        "current": current,
        "latest": latest,
        "command": UPGRADE_COMMAND,
        "summary": verdict.headline,
        "offer": verdict.offer,
        # The tool an agent calls once the user says "yes, update it."
        "action": "upgrade_kiln",
    }
    # Cached alongside the version they describe, so they can't go stale
    # independently; re-capped on read because the cache file is user-writable.
    raw = cache.get("highlights")
    if isinstance(raw, list):
        highlights = [
            item.strip()[:_MAX_HIGHLIGHT_CHARS]
            for item in raw
            if isinstance(item, str) and item.strip()
        ][:_MAX_HIGHLIGHTS]
        if highlights:
            info["highlights"] = highlights
    return info


def update_banner_line(current_version: str | None = None) -> str | None:
    """One-line human nudge for the CLI banner and the MCP instructions.

    ``None`` when no update is available (or checks are disabled).
    """
    info = check_for_update(current_version=current_version)
    if not info:
        return None
    return f"Kiln {info['latest']} is available (you're on {info['current']}). Update: {info['command']}"


def latest_version() -> str | None:
    """Blocking, bounded fetch of the latest published version.

    For the explicit ``kiln upgrade`` path, where the user is already
    waiting on a network round-trip.  Do NOT call on a hot path — use
    :func:`check_for_update` (cache-backed, non-blocking) there.
    """
    return _fetch_latest_from_pypi()
