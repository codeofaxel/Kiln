"""Lightweight daily event counters for telemetry.

Tracks prints, generations, decorations, and textures completed today.
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

# Valid event types.
_VALID_EVENTS = frozenset({"prints", "generations", "decorations", "textures"})


def _read() -> dict[str, Any]:
    """Read today's stats, resetting if the date rolled over."""
    try:
        if _STATS_PATH.is_file():
            data = json.loads(_STATS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("date") == str(date.today()):
                return data
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {
        "date": str(date.today()),
        "prints": 0,
        "generations": 0,
        "decorations": 0,
        "textures": 0,
    }


def _write(data: dict[str, Any]) -> None:
    """Persist stats to disk.  Best-effort, never raises."""
    try:
        _STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        _logger.debug("Could not write daily stats: %s", exc)


def record_event(event_type: str) -> None:
    """Increment a daily counter.  Thread-safe, never raises.

    :param event_type: One of ``prints``, ``generations``,
        ``decorations``, ``textures``.
    """
    if event_type not in _VALID_EVENTS:
        return
    try:
        with _lock:
            data = _read()
            data[event_type] = data.get(event_type, 0) + 1
            _write(data)
    except Exception as exc:
        _logger.debug("record_event(%s) failed: %s", event_type, exc)


def get_daily_stats() -> dict[str, int]:
    """Return today's counters (all default to 0)."""
    data = _read()
    return {
        "prints": data.get("prints", 0),
        "generations": data.get("generations", 0),
        "decorations": data.get("decorations", 0),
        "textures": data.get("textures", 0),
    }
