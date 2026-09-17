"""Public-Kiln → kiln-pro motion bridge: vendor-cited head motion, the plate
record, and the cool-down choreography.

Public Kiln owns the doors and the floor: the ``home_axes`` / ``park_head``
/ ``wipe_nozzle`` / ``purge_filament`` templates, step mode's contract, the
consent rule for a Z home that presses the nozzle onto the plate, the
heater-off-and-retract finish after every filament op, and every refusal's
wording.  What a week at the machine taught -- how a given model is raised,
parked, wiped and homed in its maker's own order, the per-model station
records, what is on the plate and how tall, and the cool-down the maker's
own start sequence runs -- is served by kiln-pro, at no charge to the free
tier, and reached only through this file.

Every helper here returns ``None`` (or ``False``) when kiln-pro is absent,
so a caller branches on one value and never on an import.  Nothing here
gates a tier: entitlement is decided inside kiln-pro's overlay projection.
The public behaviour without kiln-pro is the honest floor, never a stub
that claims success: a Bambu model refuses to home, park or wipe by name;
a purge runs in place and says so; the plate reads as unknown, so a Z home
onto the plate asks the person every time.

Contract (mirrored by ``kiln_pro.motion``; pinned on both sides):

* ``station_supports(adapter, station, capability)`` -> ``(ok, why)`` or
  ``None``.  *capability* is one of ``purge`` / ``wipe`` / ``park`` /
  ``home_z`` / ``home_z_on_plate``.  A refusal quotes the record's own
  reason.
* ``home_axes_impl(adapter, axes, options)`` -> ``HomeResult`` or ``None``;
  may raise ``HomingUnsupported`` / ``PlateClearRequired`` / ``PrinterError``
  exactly as the public template documents.
* ``park_head_impl(adapter, options)`` -> ``HomeResult`` or ``None``.
* ``wipe_nozzle_impl(adapter, plan)`` -> ``FilamentOpResult`` or ``None``;
  may raise ``FilamentHandlingUnsupported``.
* ``purge_scripts(adapter, plan, placement)`` -> ``(pre_gcode, post_gcode,
  after_sentence)`` or ``None`` -- the park-over-chute lines sent before the
  heater and the snap-and-shake lines that ride after the extrude.
* ``park_for_firmware_routine(adapter, plan)`` -> placement dict or ``None``
  -- parks before the firmware's own change-filament routine and says so.
* ``read_homed_axes(adapter)`` -> ``set[str]`` or ``None``.
* ``cool_under_fan(adapter, result)`` -> the answer's sentence, or ``None``
  (the public finish then reports the plain heater-off).
* ``plate_occupancy(adapter)`` -> ``PlateState`` or ``None`` (unknown).
* ``mark_occupied_by_start(adapter, file_name, plate_number)``,
  ``mark_occupied(adapter, job, source)``, ``mark_clear(adapter, source,
  note)`` -> ``True`` when recorded, ``False`` without kiln-pro.
* ``plan_motion_around_plate(state, station, action, clearance_mm)`` ->
  list of step dicts or ``None``.

Used by ``kiln.printers.base`` (the templates and the finish),
``kiln.printers.bambu`` (the three Bambu doors), ``kiln.plate_state`` (the
record's public face), ``kiln.server`` (the print-ended note) and
``kiln.plugins.homing_tools`` (``plate_status`` / ``kiln plate``).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _motion() -> Any | None:
    """``kiln_pro.motion`` when it is installed, else ``None``.

    Imported on every call rather than cached at import time: the public
    package is imported before an embedding host has decided whether the
    overlay is present, and a test that installs or removes the pro
    package mid-process must see the change.
    """
    try:
        from kiln_pro import motion  # type: ignore[import-not-found]
    except ImportError:
        return None
    except Exception:  # noqa: BLE001 -- a broken pro install degrades to the floor
        logger.debug("kiln_pro.motion failed to import; serving the public floor", exc_info=True)
        return None
    return motion


def available() -> bool:
    return _motion() is not None


def _call(name: str, *args: Any, **kwargs: Any) -> Any | None:
    """Call ``kiln_pro.motion.<name>``; ``None`` when there is no such door.

    Exceptions the contract documents (``HomingUnsupported``,
    ``PlateClearRequired``, ``PrinterError``, ``FilamentHandlingUnsupported``)
    are the pro side's honest refusals and pass through untouched; anything
    else is logged and read as "not served", so a fault in the overlay is
    a degrade to the floor, never a motion.
    """
    motion = _motion()
    if motion is None:
        return None
    fn = getattr(motion, name, None)
    if fn is None:
        return None
    from kiln.printers.base import PrinterError

    try:
        return fn(*args, **kwargs)
    except PrinterError:
        raise
    except Exception:  # noqa: BLE001
        logger.debug("kiln_pro.motion.%s raised; serving the public floor", name, exc_info=True)
        return None


def station_supports(adapter: Any, station: dict[str, Any] | None, capability: str) -> tuple[bool, str] | None:
    return _call("station_supports", adapter, station, capability)


def home_axes_impl(adapter: Any, axes: str, options: dict[str, Any]) -> Any | None:
    return _call("home_axes_impl", adapter, axes, options)


def park_head_impl(adapter: Any, options: dict[str, Any]) -> Any | None:
    return _call("park_head_impl", adapter, options)


def wipe_nozzle_impl(adapter: Any, plan: Any) -> Any | None:
    return _call("wipe_nozzle_impl", adapter, plan)


def purge_scripts(adapter: Any, plan: Any, placement: dict[str, Any]) -> tuple[list[str] | None, list[str] | None, str | None] | None:
    return _call("purge_scripts", adapter, plan, placement)


def park_for_firmware_routine(adapter: Any, plan: Any) -> dict[str, Any] | None:
    return _call("park_for_firmware_routine", adapter, plan)


def read_homed_axes(adapter: Any) -> set[str] | None:
    return _call("read_homed_axes", adapter)


def cool_under_fan(adapter: Any, result: Any) -> str | None:
    return _call("cool_under_fan", adapter, result)


def plate_occupancy(adapter: Any) -> Any | None:
    return _call("plate_occupancy", adapter)


def mark_occupied_by_start(adapter: Any, file_name: str, plate_number: int | None = None) -> bool:
    return bool(_call("mark_occupied_by_start", adapter, file_name, plate_number))


def mark_occupied(adapter: Any, job: Any, source: str) -> bool:
    return bool(_call("mark_occupied", adapter, job, source))


def mark_clear(adapter: Any, source: str, note: str = "") -> bool:
    return bool(_call("mark_clear", adapter, source, note))


def plan_motion_around_plate(state: Any, station: dict[str, Any] | None, action: str, clearance_mm: float | None) -> list[dict[str, Any]] | None:
    return _call("plan_motion_around_plate", state, station, action, clearance_mm)
