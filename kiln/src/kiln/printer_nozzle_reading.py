"""Read the nozzle a connected printer holds on record for itself.

A printer keeps its own record of the nozzle fitted to it -- a setting a
person entered on its screen or in its configuration -- and acts on that
record when it prints.  Kiln keeps a separate record of what you told Kiln
is fitted.  This module reads the printer's, over the connection the
registry already holds, and hands it back stamped with where it came from
and when the machine said it.  Comparing the two, and saying what to do
about a difference, is kiln-pro's job (https://kiln3d.com); a Kiln install
that talks to a printer sends this reading along with the request so the
comparison can be made for that machine.

Every backend answers through one door, :meth:`PrinterAdapter.read_nozzle_setting`;
a backend whose protocol holds no such setting returns ``None``, and so does
this module.  ``None`` means "could not read", never "agrees".  Nothing is
sent to the printer and nothing on it is changed.
"""

from __future__ import annotations

import concurrent.futures
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from kiln.printer_identity import fingerprint_of, resolve_machine

logger = logging.getLogger(__name__)

#: How long a reading may wait on the printer.  A registered machine that is
#: switched off must not hold a question for an adapter's full retry ladder.
LIVE_READ_DEADLINE_S = 5.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


def _said_at(age_seconds: Any) -> str:
    """When the machine stated this: the reported age subtracted from now.

    A backend answering from a cache stamps the reading with the moment the
    machine spoke, not the moment Kiln asked, so an old echo cannot pass for
    a fresh confirmation.
    """
    try:
        age = float(age_seconds)
    except (TypeError, ValueError):
        return _iso(_now())
    if age <= 0:
        return _iso(_now())
    return _iso(_now() - timedelta(seconds=age))


def _with_deadline(fn, seconds: float):
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(fn)
    try:
        return future.result(timeout=seconds)
    except concurrent.futures.TimeoutError:
        logger.debug("printer_nozzle_reading: read past %.1fs deadline", seconds)
        return None
    finally:
        pool.shutdown(wait=False)


def observe_printer_nozzle(printer_id: str) -> dict[str, Any] | None:
    """What the machine *printer_id* names holds as its nozzle, or ``None``.

    *printer_id* is a registered name, or a model id that exactly one
    registered machine has (:func:`kiln.printer_identity.resolve_machine`).
    The machine is read only when Kiln may drive it
    (:func:`kiln.printers.engagement.check_command`) and within
    :data:`LIVE_READ_DEADLINE_S`.  Returns a plain dict:

    ``machine``, ``fingerprint``, ``family`` (the adapter's name),
    ``resolved_by`` (``"name"`` / ``"model"``), ``material`` (the word the
    machine used, or ``None``), ``nozzle_diameter_mm``, ``read_from``,
    ``firmware_version``, ``state_age_seconds``, ``stale_after_seconds``,
    ``read_at`` (when the machine said it), and ``value_kind``
    (``"configured"``: a setting, never a measurement).
    """
    try:
        resolved = resolve_machine(printer_id)
        if resolved is None:
            return None
        adapter, registered_name, resolved_by = resolved
        try:
            from kiln.printers.engagement import check_command

            if check_command(adapter, "get_state") is not None:
                return None
        except ImportError:
            pass
        read = getattr(adapter, "read_nozzle_setting", None)
        if not callable(read):
            return None
        setting = _with_deadline(read, LIVE_READ_DEADLINE_S)
        if setting is None or getattr(setting, "is_empty", lambda: True)():
            return None
        return {
            "machine": registered_name,
            "fingerprint": fingerprint_of(adapter),
            "family": str(getattr(adapter, "name", "") or "").casefold(),
            "resolved_by": resolved_by,
            "material": setting.material,
            "nozzle_diameter_mm": setting.diameter_mm,
            "read_from": setting.source,
            "firmware_version": setting.firmware_version,
            "state_age_seconds": setting.age_seconds,
            "stale_after_seconds": setting.stale_after_seconds,
            "read_at": _said_at(setting.age_seconds),
            "value_kind": "configured",
        }
    except Exception:  # noqa: BLE001 -- a read that fails is a read that did not happen
        logger.debug("printer_nozzle_reading: read unavailable", exc_info=True)
        return None


#: The hosted nozzle tools that accept a reading from the caller's own Kiln.
#: The stub for each reads the local machine and sends the result with the
#: request; the server compares it with the record it holds.
TOOLS_THAT_TAKE_A_READING: frozenset[str] = frozenset(
    {"get_nozzle_state", "set_nozzle_state", "record_nozzle_replacement"}
)


def with_local_reading(tool_name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """*kwargs* for a hosted call, plus this machine's reading when the tool
    takes one and the caller did not supply one.  The reading is a fact
    about the caller's own printer, gathered here because only this process
    can reach it."""
    if tool_name not in TOOLS_THAT_TAKE_A_READING or kwargs.get("printer_reading") is not None:
        return kwargs
    printer_id = kwargs.get("printer_id")
    if not isinstance(printer_id, str) or not printer_id.strip():
        return kwargs
    observation = observe_printer_nozzle(printer_id.strip())
    if observation is None:
        return kwargs
    return {**kwargs, "printer_reading": observation}


__all__ = [
    "LIVE_READ_DEADLINE_S",
    "TOOLS_THAT_TAKE_A_READING",
    "observe_printer_nozzle",
    "with_local_reading",
]
