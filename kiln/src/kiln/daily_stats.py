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
import threading
from datetime import date
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

_STATS_PATH: Path = Path.home() / ".kiln" / "daily_stats.json"
_lock = threading.Lock()

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
    }


def _read() -> dict[str, Any]:
    """Read today's stats, resetting if the date rolled over."""
    try:
        if _STATS_PATH.is_file():
            data = json.loads(_STATS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("date") == str(date.today()):
                return data
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
    """
    if event_type not in _VALID_EVENTS:
        return
    try:
        with _lock:
            data = _read()
            # Increment top-level counter
            data[event_type] = data.get(event_type, 0) + 1

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
    }
