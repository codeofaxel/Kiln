"""What Kiln itself is watching on this printer, right now.

The coverage statement has two layers.  Layer 1 is what the PRINTER's own
detectors watch, from the makers' pages (kiln-pro).  This module is the
input for layer 2 — what KILN adds on this machine — and it reports FACTS
read from the watchers actually running in this process: the print
watchdog Kiln attaches to a print it started, an opt-in health session, a
background watch, whether a camera Kiln can read exists, and whether the
kiln-pro vision detector is armed to read the frames.  Never what a
watcher could do in principle: an unattached watchdog is reported
unattached, a print Kiln did not start has no watchdog, and a camera
nobody registered is not readable.

Each watcher's rules are described here in plain words, beside the code
that runs them, and travel WITH the state — a reader off this machine (the
hosted monitor door) then renders what THIS Kiln actually runs, not what
its own copy of Kiln would.  Which failure classes those rules cover, and
how to say it, is kiln-pro's judgement; public Kiln states the facts.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The wire's own version, so a reader can tell a state it does not know
#: how to read from one it does.
WATCH_STATE_KIND = "kiln.watch.v1"


def _watcher_words() -> dict[str, dict[str, Any]]:
    """Every Kiln watcher's rules in plain words, derived from the code that
    runs them — the thresholds are read, never retyped."""
    from kiln.print_health_monitor import MonitorPolicy
    from kiln.print_watchdog import (
        DEFAULT_BED_DROP_C,
        DEFAULT_POLL_INTERVAL,
        DEFAULT_STALL_SECONDS,
        DEFAULT_TOOL_DROP_C,
    )

    health = MonitorPolicy()
    return {
        "watchdog": {
            "title": "the print watchdog",
            "attached": "to every print Kiln starts, for as long as it runs",
            "poll_seconds": DEFAULT_POLL_INTERVAL,
            "acts": "emergency-stops the printer on a red flag",
            "red": {
                "print_error": "an error the printer itself reports",
                "hms_blocklist": "a printer fault code on the block list",
                "tool_drop": f"the hotend dropping {DEFAULT_TOOL_DROP_C:.0f} °C below its target",
                "bed_drop": f"the bed dropping {DEFAULT_BED_DROP_C:.0f} °C below its target",
                "tool_warmup_timeout": "the hotend never reaching its target",
                "bed_warmup_timeout": "the bed never reaching its target",
                "stalled_layer": f"no progress for {DEFAULT_STALL_SECONDS:.0f} s while printing",
            },
            "yellow": {
                "wifi_weak": "a weak Wi-Fi signal",
                "chamber_fan_stalled": "a stalled chamber fan",
                "tool_warmup_slow": "the hotend warming slowly",
                "bed_warmup_slow": "the bed warming slowly",
            },
        },
        "health": {
            "title": "a health session",
            "started_by": "start_printer_health_monitoring",
            "checks": {
                "thermal_drift": (
                    f"the hotend or bed drifting more than {health.temp_drift_threshold:.0f} °C "
                    f"from its target ({health.temp_drift_threshold * 2:.0f} °C is critical)"
                ),
                "progress_stall": f"no progress for {health.stall_timeout} s",
                "filament_sensor": "the printer's own filament sensor, when it reports one",
                "power_draw": "an unusual power draw, when the printer reports it",
                "webcam_quality": "the camera feed going dark or frozen",
                "connection": "the printer dropping off the network",
            },
            "acts": "pauses the print on a confident failure; cancels only on a sustained thermal critical and only when told to",
        },
        "watch": {
            "title": "a background watch",
            "started_by": "watch_print",
            "checks": {
                "camera_vs_progress": "the camera showing the bed changing while progress stands still",
                "progress_stall": "no progress past its stall timeout",
                "terminal_state": "the print ending, failing, or the printer going offline",
            },
            "acts": "collects snapshots for your agent's eyes; cancels at a chosen percent when asked",
        },
        "first_layer": {
            "title": "first-layer snapshots",
            "started_by": "start_monitored_print",
            "checks": {
                "first_layer_frames": "snapshots of the first layers for your agent's eyes",
            },
            "acts": "pauses the print when your agent reports a failure",
        },
        "vision": {
            "title": "the vision detector",
            "needs": "a background watch or first-layer snapshots, and a camera Kiln can read",
            "checks": {
                "telemetry_camera_mismatch": "the spaghetti or tangle signature: the bed changing on camera while progress is stuck",
                "camera_stale": "a camera feed frozen while the print advances",
            },
            "acts": "raises an alert the recovery engine reads",
        },
    }


def _camera_state(adapter: Any) -> dict[str, Any]:
    from kiln.printers.base import adapter_has_camera

    if adapter is None:
        return {"readable": False, "source": None}
    registered = getattr(adapter, "external_camera", None)
    if registered is not None:
        return {"readable": True, "source": "registered"}
    own = bool(getattr(getattr(adapter, "capabilities", None), "can_snapshot", False))
    readable = adapter_has_camera(adapter)
    return {"readable": readable, "source": "printer" if readable and own else None}


def _watchdog_state(printer_name: str) -> dict[str, Any]:
    from kiln import server as _srv

    with _srv._print_watchdogs_lock:
        watchdog = _srv._print_watchdogs.get(printer_name)
    if watchdog is None:
        return {"attached": False, "running": False}
    try:
        status = watchdog.status()
    except Exception as exc:  # noqa: BLE001 — a watchdog that cannot report is not running
        logger.debug("watchdog status unreadable for %r: %s", printer_name, exc)
        return {"attached": True, "running": False}
    return {
        "attached": True,
        "running": bool(status.get("running")),
        "poll_seconds": getattr(watchdog, "_poll_interval", None),
        "stall_seconds": getattr(watchdog, "_stall_seconds", None),
        "red_flags": len(status.get("red_flags") or []),
        "yellow_flags": len(status.get("yellow_flags") or []),
    }


def _health_state(printer_name: str) -> dict[str, Any]:
    from kiln.print_health_monitor import MonitorStatus, get_print_health_monitor

    monitor = get_print_health_monitor()
    sessions = monitor.list_sessions(printer_name=printer_name, status=MonitorStatus.MONITORING)
    if not sessions:
        return {"active": False}
    policy = sessions[-1].policy
    background = getattr(monitor, "_background_monitors", {}).get(printer_name)
    return {
        "active": True,
        "interval_seconds": getattr(background, "interval_seconds", None)
        or policy.check_interval_seconds,
        "stall_seconds": policy.stall_timeout,
        "temp_drift_c": policy.temp_drift_threshold,
        "auto_pause": bool(policy.auto_pause_on_failure),
        "auto_cancel_on_emergency": bool(policy.auto_cancel_on_emergency),
    }


def _watch_state(printer_name: str) -> dict[str, Any]:
    from kiln import server as _srv

    live = []
    for watcher in list(getattr(_srv, "_watchers", {}).values()):
        if getattr(watcher, "_printer_name", None) != printer_name:
            continue
        thread = getattr(watcher, "_thread", None)
        if thread is not None and thread.is_alive():
            live.append(watcher)
    if not live:
        return {"active": False, "count": 0}
    return {
        "active": True,
        "count": len(live),
        "snapshot_interval": min(
            int(getattr(w, "_snapshot_interval", 0) or 0) for w in live
        ) or None,
        "stall_seconds": min(int(getattr(w, "_stall_timeout", 0) or 0) for w in live) or None,
    }


def _vision_state() -> dict[str, Any]:
    from kiln.server import _pro_bridge

    pro = _pro_bridge()
    armed = False
    if pro is not None:
        try:
            armed = bool(pro.is_available("vision"))
        except Exception:  # noqa: BLE001 — an unreadable bridge is an unarmed detector
            armed = False
    return {"armed": armed}


def kiln_watch_state(
    printer_name: str | None, *, adapter: Any = None, state_word: str | None = None
) -> dict[str, Any]:
    """The live facts about what Kiln is watching on *printer_name*.

    Reads this process's registries — never a promise.  Every part is
    read on its own, so one failing reader costs its own block and never
    the others; the block is then reported in its "nothing" shape rather
    than dropped, because a missing block is indistinguishable from a
    watcher that is off.

    *adapter* is the printer's adapter when the caller holds one (no
    adapter means no camera can be read); *state_word* is the machine
    state the caller has already read, so ``printing`` says whether a
    print is on the machine without a second network read — ``None``
    when the caller did not have one.
    """
    from kiln import server as _srv
    from kiln.monitor_payload import is_active_print_state

    try:
        name = _srv._resolve_effective_printer_name(printer_name)
    except Exception:  # noqa: BLE001 — an unresolvable name still gets a state, keyed as given
        name = printer_name or ""

    state: dict[str, Any] = {
        "kind": WATCH_STATE_KIND,
        "printer_name": name,
        "printing": is_active_print_state(state_word) if isinstance(state_word, str) else None,
    }
    readers = (
        ("camera", lambda: _camera_state(adapter), {"readable": False, "source": None}),
        ("watchdog", lambda: _watchdog_state(name), {"attached": False, "running": False}),
        ("health", lambda: _health_state(name), {"active": False}),
        ("watch", lambda: _watch_state(name), {"active": False, "count": 0}),
        ("vision", _vision_state, {"armed": False}),
        ("watchers", _watcher_words, {}),
    )
    for key, read, fallback in readers:
        try:
            state[key] = read()
        except Exception as exc:  # noqa: BLE001 — one reader's failure is its own block only
            logger.debug("watch state reader %s failed for %r: %s", key, name, exc)
            state[key] = dict(fallback)
    return state
