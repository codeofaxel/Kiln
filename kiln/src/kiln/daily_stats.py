"""Lightweight daily event counters for telemetry.

Tracks prints, generations, decorations, textures, slices, and
marketplace downloads completed today.  Supports detailed breakdowns
(e.g. texture name, decoration type, marketplace source).

Read by the heartbeat module for Supabase reporting.  Never blocks,
never errors visibly.

File: ``~/.kiln/daily_stats.json``
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import date
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

_STATS_PATH: Path = Path.home() / ".kiln" / "daily_stats.json"
_lock = threading.Lock()

# A real MCP tool name: lowercase, starts alpha, 3-65 chars.  The
# per-tool counter only records names matching this so a weird
# callable ``__name__`` (or anything not a genuine tool) can't ride
# into the anonymous heartbeat as a "tool".
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,64}$")
# Cap distinct tool names tracked per day.  A real user touches well
# under this; the cap just stops the local file (and the heartbeat
# payload) from growing without bound.  Existing names keep counting
# once the cap is reached; only brand-new names past the cap are dropped.
_TOOL_CALLS_MAX_DISTINCT = 300

# Valid event types (top-level counters).
_VALID_EVENTS = frozenset({
    "prints", "generations", "decorations", "textures",
    "slices", "downloads", "print_hours",
})


def _empty_day() -> dict[str, Any]:
    """Return a fresh day's stats."""
    return {
        "date": str(date.today()),
        "prints": 0,
        "generations": 0,
        "decorations": 0,
        "textures": 0,
        "slices": 0,
        "downloads": 0,
        "print_hours": 0.0,
        # Detailed breakdowns (name → count)
        "texture_names": {},       # {"tiger_stripe": 3, "custom": 1}
        "decoration_types": {},    # {"photo": 2, "qr": 1, "text": 5}
        "slicer_profiles": {},     # {"BambuLab A1 0.4": 2}
        "marketplace_sources": {}, # {"thingiverse": 3, "makerworld": 1}
        # Tier-denial counters: tool_name → number of TIER_REQUIRED
        # rejections today.  This is the key funnel-leak signal for
        # "user paid on the web but never synced their local agent" —
        # every denial here is a user reaching for a locked door they
        # should already have a key to.  Rolled up in the daily
        # heartbeat so we can see which tools are driving upgrades
        # vs. which are just hitting unsynced machines.
        "tier_denials": {},        # {"fleet_status": 2, "texture_apply": 1}
        # Per-tool call counts: tool_name → times called today.  Counts
        # EVERY local tool dispatch (not just the six outcome events),
        # so the anonymous heartbeat can finally show what unsigned
        # local users actually do — the "tools per month" signal that
        # was previously invisible for anyone not signed in.  Names +
        # counts only, never arguments or paths.
        "tool_calls": {},          # {"generate_coaster": 4, "slice_model": 2}
    }


# Counter keys carried forward when a day rolls over, so the daily
# heartbeat can report a COMPLETE day instead of a partial one.
_ROLLOVER_COUNTERS = (
    "prints", "generations", "decorations",
    "textures", "slices", "downloads", "print_hours",
)


def _archive_completed_day(data: dict[str, Any]) -> dict[str, Any]:
    """Return a fresh day that remembers the day that just ended.

    The heartbeat fires ONCE, when the Kiln server starts, and reads
    whatever counters exist at that instant.  Since you must start the
    server to do anything with Kiln, today's counters are ~always zero
    at that moment — so the reported activity systematically undercounts
    (2026-07-25: 17 of 671 production heartbeats carried any print at
    all, which read as "nobody prints" rather than "we sample before the
    work happens").  Preserving the finished day lets the heartbeat send
    a whole day's real numbers, one day behind.
    """
    fresh = _empty_day()
    previous = {"date": data.get("date")}
    for key in _ROLLOVER_COUNTERS:
        previous[key] = data.get(key, 0)
    if previous["date"]:
        fresh["previous"] = previous
    return fresh


def _read() -> dict[str, Any]:
    """Read today's stats, archiving the prior day if it rolled over."""
    try:
        if _STATS_PATH.is_file():
            data = json.loads(_STATS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if data.get("date") == str(date.today()):
                    return data
                # A day ended: keep its totals so the next heartbeat can
                # report a complete day rather than an empty one.
                return _archive_completed_day(data)
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return _empty_day()


def _write(data: dict[str, Any]) -> None:
    """Persist stats to disk.  Best-effort, never raises."""
    try:
        _STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        _logger.debug("Could not write daily stats: %s", exc)


def record_event(event_type: str, *, detail: str | None = None) -> None:
    """Increment a daily counter.  Thread-safe, never raises.

    :param event_type: One of ``prints``, ``generations``,
        ``decorations``, ``textures``, ``slices``, ``downloads``.
    :param detail: Optional sub-category name for breakdowns.
        For textures: the texture name (e.g. ``"tiger_stripe"``).
        For decorations: the content type (e.g. ``"qr"``).
        For slices: the slicer profile name.
        For downloads: the marketplace name (e.g. ``"thingiverse"``).

    .. note::

        ``textures`` events also increment ``decorations`` and record the
        texture name in ``decoration_types`` (under the
        ``procedural_texture`` key).  The separate ``textures`` counter is
        kept for backward compatibility.
    """
    if event_type not in _VALID_EVENTS:
        return
    try:
        with _lock:
            data = _read()
            # Increment top-level counter
            data[event_type] = data.get(event_type, 0) + 1

            # Textures are a decoration subtype — also increment decorations
            if event_type == "textures":
                data["decorations"] = data.get("decorations", 0) + 1
                dec_types = data.get("decoration_types", {})
                if not isinstance(dec_types, dict):
                    dec_types = {}
                dec_types["procedural_texture"] = (
                    dec_types.get("procedural_texture", 0) + 1
                )
                data["decoration_types"] = dec_types

            # Increment breakdown if detail provided
            if detail:
                _DETAIL_KEYS = {
                    "textures": "texture_names",
                    "decorations": "decoration_types",
                    "slices": "slicer_profiles",
                    "downloads": "marketplace_sources",
                }
                breakdown_key = _DETAIL_KEYS.get(event_type)
                if breakdown_key:
                    breakdown = data.get(breakdown_key, {})
                    if not isinstance(breakdown, dict):
                        breakdown = {}
                    breakdown[detail] = breakdown.get(detail, 0) + 1
                    data[breakdown_key] = breakdown

            _write(data)
    except Exception as exc:
        _logger.debug("record_event(%s) failed: %s", event_type, exc)


def record_print_hours(hours: float) -> None:
    """Add print hours to today's total.  Thread-safe, never raises."""
    try:
        with _lock:
            data = _read()
            data["print_hours"] = round(data.get("print_hours", 0.0) + hours, 2)
            _write(data)
    except Exception as exc:
        _logger.debug("record_print_hours failed: %s", exc)


def record_tier_denial(tool_name: str) -> None:
    """Increment the TIER_REQUIRED denial counter for ``tool_name``.

    Called from :func:`requires_tier`'s error path (both the public-Kiln
    stub and the kiln-pro decorator).  Lets a post-hoc look at the
    daily heartbeat show exactly which tools are hit by users whose
    machines aren't synced to their paid tier — the funnel-leak
    signal we were missing.

    Thread-safe, never raises — if anything goes wrong we drop the
    event rather than interfering with tool-call error paths.
    """
    name = (tool_name or "").strip() or "<unknown>"
    # Tight bound on the label: the heartbeat JSON column has a size
    # budget and someone passing a massive __name__ from a weird
    # callable shouldn't be able to blow through it.  64 chars is
    # enough for every tool name we ship and then some.
    if len(name) > 64:
        name = name[:64]
    try:
        with _lock:
            data = _read()
            buckets = data.get("tier_denials", {})
            if not isinstance(buckets, dict):
                buckets = {}
            buckets[name] = int(buckets.get(name, 0)) + 1
            data["tier_denials"] = buckets
            _write(data)
    except Exception as exc:
        _logger.debug("record_tier_denial(%s) failed: %s", tool_name, exc)


def record_tool_call(tool_name: str) -> None:
    """Increment the per-tool call counter for ``tool_name``.

    Called once per local tool dispatch (see ``server._record_local_tool_call``)
    so the anonymous daily heartbeat can report which tools are used, not
    just the six outcome events.  This is the only anonymous view of what
    a NOT-signed-in local user does — the per-user ledger only syncs when
    signed in.

    Only genuine tool names (``_TOOL_NAME_RE``) are recorded, and no more
    than ``_TOOL_CALLS_MAX_DISTINCT`` distinct names per day.  Names +
    counts only — never arguments.  Thread-safe, never raises.
    """
    name = (tool_name or "").strip()
    if not _TOOL_NAME_RE.match(name):
        return  # not a real tool name — drop rather than pollute the map
    try:
        with _lock:
            data = _read()
            buckets = data.get("tool_calls", {})
            if not isinstance(buckets, dict):
                buckets = {}
            if name not in buckets and len(buckets) >= _TOOL_CALLS_MAX_DISTINCT:
                return  # cap distinct names; existing ones still counted below
            buckets[name] = int(buckets.get(name, 0)) + 1
            data["tool_calls"] = buckets
            _write(data)
    except Exception as exc:
        _logger.debug("record_tool_call(%s) failed: %s", tool_name, exc)


def get_daily_stats() -> dict[str, Any]:
    """Return today's counters and breakdowns."""
    data = _read()
    return {
        "prints": data.get("prints", 0),
        "generations": data.get("generations", 0),
        "decorations": data.get("decorations", 0),
        "textures": data.get("textures", 0),
        "slices": data.get("slices", 0),
        "downloads": data.get("downloads", 0),
        "print_hours": data.get("print_hours", 0.0),
        "texture_names": data.get("texture_names", {}),
        "decoration_types": data.get("decoration_types", {}),
        "slicer_profiles": data.get("slicer_profiles", {}),
        "marketplace_sources": data.get("marketplace_sources", {}),
        "tier_denials": data.get("tier_denials", {}),
        "tool_calls": data.get("tool_calls", {}),
        # The last COMPLETE day's counters (see _archive_completed_day).
        # The heartbeat reports these because the same-day counters it
        # can see at server startup are structurally near-empty.
        "previous_day": data.get("previous") or {},
    }
