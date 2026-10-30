"""What this computer read off each Klipper printer's heater checks, kept.

Klipper checks every heater it drives (``[verify_heater]``): how far a
heater's temperature may stray from its target and how long it has to warm
before Klipper shuts it down.  A config edit or a firmware update can change
those settings, and the moment it shows is a print start.  This module reads
them off a Klipper printer this install already talks to, keeps one entry
per distinct state -- stamped with the firmware it came from and when it was
first and last seen -- and hands a served heat check the current state and
the last different one.  Judging them is the served check's; nothing here
says whether a setting is safe.

Kept in ``~/.kiln/heater_check_readings.json``, one record per MACHINE (the
registry's fingerprint: serial, else address -- never the name, which the
server registers twice), newest last, at most :data:`HISTORY_LIMIT` states.
Kiln's servers run no printer and keep none of this: every door skips on the
hosted deploy.

What leaves this computer (:func:`state_of`): the heater sections a printer
configures, by name, its ``verify_heater`` settings (four numbers each) and
the firmware version, with when each state was first and last seen.  Never
the rest of the configuration, the printer's address, or its fingerprint.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_STORE_NAME = "heater_check_readings.json"
_SCHEMA_VERSION = 1
#: Distinct states kept per machine.
HISTORY_LIMIT = 20
#: The settings Klipper's ``[verify_heater]`` takes.
VERIFY_KEYS = ("max_error", "check_gain_time", "hysteresis", "heating_gain")
#: A section that is a heater Klipper verifies.
_HEATER_SECTION = re.compile(r"heater_bed|extruder\d{0,2}|heater_generic [a-z0-9_]{1,32}")
_FIRMWARE = re.compile(r"[A-Za-z0-9._+-]{1,64}")
_MAX_HEATERS = 16
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and -1e7 < number < 1e7 else None


def state_of(config: Any, firmware_version: Any) -> dict[str, Any] | None:
    """A printer's heater-check state from its Klipper configuration
    (Moonraker's ``configfile.config``): the heaters it configures, by
    section name, and the ``verify_heater`` settings it gives each, as
    numbers.  ``None`` for anything that is not a configuration or names no
    heater."""
    if not isinstance(config, dict):
        return None
    heaters: list[str] = []
    verify: dict[str, dict[str, float]] = {}
    for section, body in config.items():
        name = str(section).strip().lower()
        if _HEATER_SECTION.fullmatch(name):
            heaters.append(name)
            continue
        target = name.removeprefix("verify_heater ").strip()
        if name.startswith("verify_heater ") and _HEATER_SECTION.fullmatch(target) and isinstance(body, dict):
            verify[target] = {
                key: number for key in VERIFY_KEYS if (number := _number(body.get(key))) is not None
            }
    if not heaters:
        return None
    version = firmware_version if isinstance(firmware_version, str) and _FIRMWARE.fullmatch(firmware_version) else None
    return {
        "heaters": sorted(heaters)[:_MAX_HEATERS],
        "verify_heater": {k: verify[k] for k in sorted(verify)[:_MAX_HEATERS]},
        "firmware_version": version,
    }


def read_state(adapter: Any) -> dict[str, Any] | None:
    """*adapter*'s heater-check state, read now, or ``None`` for a printer
    that keeps no Klipper configuration.  A printer that cannot be asked
    raises, so a caller can say it could not."""
    read_config = getattr(adapter, "get_printer_config", None)
    if not callable(read_config):
        return None
    config = read_config()
    read_version = getattr(adapter, "reported_firmware_version", None)
    version = None
    if callable(read_version):
        with contextlib.suppress(Exception):
            version = read_version()
    return state_of(config, version)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


def _hosted() -> bool:
    from kiln.runtime_env import is_hosted_multitenant

    return is_hosted_multitenant()


def _store_path() -> Path:
    home = Path(os.environ.get("KILN_HOME", "").strip() or (Path.home() / ".kiln"))
    home.mkdir(parents=True, exist_ok=True)
    return home / _STORE_NAME


def _read_store() -> dict[str, Any]:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"version": _SCHEMA_VERSION, "machines": {}}
    if not isinstance(data, dict) or data.get("version") != _SCHEMA_VERSION or not isinstance(data.get("machines"), dict):
        return {"version": _SCHEMA_VERSION, "machines": {}}
    return data


def _write_store(data: dict[str, Any]) -> None:
    tmp: str | None = None
    try:
        path = _store_path()
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, path)
        tmp = None
    except (OSError, ValueError, TypeError):
        logger.debug("heater-check readings could not be written", exc_info=True)
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _machine(adapter: Any) -> str | None:
    try:
        from kiln.registry import machine_fingerprint

        return machine_fingerprint(adapter) or None
    except Exception:  # noqa: BLE001 -- a machine Kiln cannot identify keeps no history
        return None


def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    keys = ("heaters", "verify_heater", "firmware_version")
    return all(a.get(k) == b.get(k) for k in keys)


def _view(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "current": dict(entries[-1]) if entries else None,
        "previous": dict(entries[-2]) if len(entries) > 1 else None,
        "watched_since": entries[0].get("first_seen") if entries else None,
    }


def remember(adapter: Any, state: dict[str, Any]) -> dict[str, Any]:
    """Keep *state* for *adapter*'s machine and return its view: the state
    now, the last different one, and when this computer first read it.  A
    state equal to the last one refreshes when it was last seen; a
    different one is a new entry.  On the hosted deploy, and for a machine
    Kiln cannot identify, nothing is kept and the view holds only now."""
    stamp = _now()
    entry = {**state, "first_seen": stamp, "last_seen": stamp}
    key = None if _hosted() else _machine(adapter)
    if key is None:
        return {"current": entry, "previous": None, "watched_since": None}
    with _LOCK:
        data = _read_store()
        entries = data["machines"].get(key)
        entries = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
        if entries and _same(entries[-1], state):
            entries[-1]["last_seen"] = stamp
        else:
            entries.append(entry)
            del entries[:-HISTORY_LIMIT]
        data["machines"][key] = entries
        _write_store(data)
    return _view(entries)


def stored(adapter: Any) -> dict[str, Any] | None:
    """*adapter*'s machine's view from what this computer kept, without
    asking the printer; ``None`` when it was never read."""
    key = None if _hosted() else _machine(adapter)
    if key is None:
        return None
    entries = _read_store()["machines"].get(key)
    entries = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    return _view(entries) if entries else None


def note_print_started(adapter: Any) -> None:
    """Read and keep a Klipper printer's heater checks as a print starts,
    on a thread of its own: the read is a configuration query, and a print
    start never waits on it or fails because of it."""
    if _hosted() or not callable(getattr(adapter, "get_printer_config", None)):
        return

    def read() -> None:
        try:
            state = read_state(adapter)
            if state is not None:
                remember(adapter, state)
        except Exception:  # noqa: BLE001 -- a reading never touches the print
            logger.debug("heater-check reading at print start failed", exc_info=True)

    threading.Thread(target=read, name="kiln-heater-check-reading", daemon=True).start()
