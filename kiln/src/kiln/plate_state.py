"""What is on the build plate, as far as Kiln can honestly say.

Every motion Kiln sends an idle printer starts the same way: raise the
head a few millimetres, then travel.  A travel collision on some families
is SILENT -- no fault code, no read-back -- and some models home Z by
pressing the nozzle onto the plate.  Whether either is safe depends on one
fact the printer cannot report: **is there a part on the plate, and how
tall is it?**

This module is the public face of the record that answers it.  The record
itself -- written when Kiln starts a print, re-asserted when one is seen
ending, cleared only by a person, with the part's height read from the file
where Kiln can -- is kept by kiln-pro and served to the free tier through
:mod:`kiln._pro_motion_bridge`.  Without it every plate reads as
``unknown``, and unknown is the floor: a Z home that presses the nozzle
onto the plate asks the person on every call, and nothing else changes.

**The default is "unknown", and unknown asks.**  A missing record, a
missing kiln-pro, and a machine with no durable identity all read the same
way, because the two failure directions are not symmetric: a record that
read "clear" by mistake would send the head across a part a few
millimetres up, silently; "unknown" costs the person one look at the plate.

What stays here is the contract every reader shares -- the two dataclasses,
the machine key, and the raise arithmetic -- so ``home_axes``, ``park_head``,
``plate_status``, ``kiln plate`` and ``kiln doctor`` speak one shape whether
or not the record is served.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

#: The three things the record can say.  ``unknown`` is what every reader
#: gets when nothing trustworthy is on file.
STATUSES = ("unknown", "occupied", "clear")

_NOT_SERVED = (
    "the plate record is served through Kiln's hosted service (kiln-pro) and no "
    "served record answered on this install, so the plate reads as unknown"
)


@dataclass(frozen=True)
class PlateJob:
    """The print that put a part on the plate, and its geometry when Kiln had it.

    ``footprint_mm`` is ``[x0, y0, x1, y1]`` in plate coordinates and
    ``max_z_mm`` the part's top, both ``None`` when Kiln could not derive
    them from the file -- a part of unknown size is still a part.
    """

    file: str
    footprint_mm: list[float] | None = None
    max_z_mm: float | None = None
    printer_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "footprint_mm": list(self.footprint_mm) if self.footprint_mm else None,
            "max_z_mm": self.max_z_mm,
            "printer_id": self.printer_id,
        }

    @classmethod
    def from_dict(cls, data: Any) -> PlateJob | None:
        if not isinstance(data, dict) or not isinstance(data.get("file"), str):
            return None
        footprint = data.get("footprint_mm")
        try:
            fp = [float(v) for v in footprint] if isinstance(footprint, list) and len(footprint) == 4 else None
        except (TypeError, ValueError):
            fp = None
        max_z = data.get("max_z_mm")
        try:
            mz = float(max_z) if max_z is not None else None
        except (TypeError, ValueError):
            mz = None
        pid = data.get("printer_id")
        return cls(file=data["file"], footprint_mm=fp, max_z_mm=mz, printer_id=pid if isinstance(pid, str) else None)


@dataclass(frozen=True)
class PlateState:
    """What the record says about one machine's plate."""

    machine: str
    status: str = "unknown"
    source: str = "no_record"
    since: str | None = None
    job: PlateJob | None = None
    note: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def occupied(self) -> bool:
        return self.status == "occupied"

    @property
    def clear(self) -> bool:
        return self.status == "clear"

    def since_clock(self) -> str:
        """``18:12`` for today, ``Sep 15 18:12`` otherwise, the raw stamp when unparsable."""
        if not self.since:
            return "an unknown time"
        try:
            when = datetime.fromisoformat(self.since)
        except ValueError:
            return self.since
        if when.date() == datetime.now().astimezone().date():
            return when.strftime("%H:%M")
        return when.strftime("%b %d %H:%M")

    def describe(self) -> str:
        """One clause a refusal can quote: what is there, since when, how tall."""
        if self.status == "occupied":
            what = f"the plate still holds {self.job.file}" if self.job else "the plate still holds a part"
            if self.job and self.job.max_z_mm is not None:
                height = f"up to {self.job.max_z_mm:g} mm tall"
            else:
                height = "height unknown"
            return f"{what} since {self.since_clock()}, {height}"
        if self.status == "clear":
            who = "a person said so" if self.source == "human" else self.source
            return f"the plate was cleared at {self.since_clock()} ({who})"
        return "Kiln has no record of what is on the plate"

    def to_dict(self) -> dict[str, Any]:
        return {
            "machine": self.machine,
            "status": self.status,
            "source": self.source,
            "since": self.since,
            "job": self.job.to_dict() if self.job else None,
            "note": self.note,
            "description": self.describe(),
        }

    @classmethod
    def from_dict(cls, machine: str, data: Any) -> PlateState:
        """A record row, or ``unknown`` for anything that is not a well-formed one."""
        if not isinstance(data, dict) or data.get("status") not in STATUSES:
            return cls(machine=machine)
        since = data.get("since")
        return cls(
            machine=machine,
            status=str(data["status"]),
            source=str(data.get("source") or "unknown"),
            since=since if isinstance(since, str) else None,
            job=PlateJob.from_dict(data.get("job")),
            note=str(data.get("note") or ""),
        )


def machine_id(adapter: Any) -> str:
    """Durable identity for *adapter*'s machine, or ``""`` when it has none.

    The engagement record already answers this (serial, else address, and
    ``""`` for an adapter whose only identity is its object id -- a record
    that outlives the process cannot be keyed by that).  One answer, asked
    there.
    """
    try:
        from kiln.printers.engagement import machine_id as _engagement_machine_id

        return _engagement_machine_id(adapter)
    except Exception:  # noqa: BLE001
        return ""


def read(adapter: Any) -> PlateState:
    """The record for *adapter*'s machine; ``unknown`` when there is none."""
    from kiln import _pro_motion_bridge as _bridge

    machine = machine_id(adapter)
    if not machine:
        return PlateState(machine="", note="this printer reports neither a serial nor an address, so nothing can be recorded for it")
    try:
        served = _bridge.plate_occupancy(adapter)
    except Exception:  # noqa: BLE001 -- a served fault reads as unknown, never as clear
        logger.debug("plate-state read failed", exc_info=True)
        served = None
    if isinstance(served, PlateState):
        return served
    return PlateState(machine=machine, note=_NOT_SERVED)


def plate_occupancy(adapter: Any) -> PlateState:
    """The one door every motion gate calls.

    Kept as its own name so the gates in ``home_axes`` / ``park_head`` /
    the doors never grow a second way of asking, and so anything that later
    enriches the answer lands here once.
    """
    return read(adapter)


def mark_occupied(adapter: Any, job: PlateJob | dict[str, Any] | None, *, source: str = "kiln_started_print", note: str = "") -> bool:
    """The plate holds a part.  ``True`` when recorded, ``False`` when nothing keeps the record."""
    from kiln import _pro_motion_bridge as _bridge

    try:
        if isinstance(job, dict):
            job = PlateJob.from_dict(job)
        return _bridge.mark_occupied(adapter, job, source)
    except Exception:  # noqa: BLE001
        logger.debug("mark_occupied failed", exc_info=True)
        return False


def mark_clear(adapter: Any, source: str, *, note: str = "") -> bool:
    """A person says the plate is empty.  ``True`` when recorded.

    The record answers the row question for home X and park.  It never
    answers for a Z home that presses the nozzle onto the plate: that
    motion reads ``plate_clear`` on its own call, every time (see
    :meth:`~kiln.printers.base.PrinterAdapter._plate_gate`).
    """
    from kiln import _pro_motion_bridge as _bridge

    return _bridge.mark_clear(adapter, source, note)


def mark_occupied_by_start(adapter: Any, file_name: str, *, plate_number: int | None = None) -> bool:
    """A print Kiln started: the plate now holds *file_name*.  Never raises."""
    from kiln import _pro_motion_bridge as _bridge

    try:
        return _bridge.mark_occupied_by_start(adapter, file_name, plate_number)
    except Exception:  # noqa: BLE001
        logger.debug("plate-state start note failed", exc_info=True)
        return False


def raise_clearance_mm(station: dict[str, Any] | None) -> float | None:
    """How high the vendor's own first raise lifts the head before it travels.

    ``raise_before_travel.probe_up_mm - back_down_mm`` from the station
    record; ``None`` without a record.  A part on the plate at least this
    tall stands in the path of the very next move.
    """
    try:
        r = (station or {}).get("raise_before_travel")
        if not isinstance(r, dict):
            return None
        return float(r["probe_up_mm"]) - float(r["back_down_mm"])
    except (KeyError, TypeError, ValueError):
        return None


def plan_motion_around_plate(
    state: PlateState, station: dict[str, Any] | None, *, action: str, clearance_mm: float | None
) -> list[dict[str, Any]] | None:
    """A served plan for moving around a recorded part, or ``None``.

    The hook, not the planner.  Public Kiln refuses to move a head across a
    recorded part; a served plan is a list of steps, each a dict with
    ``label``, ``you_will_see``, ``stops_when`` and ``gcode`` (a list of
    lines; ``leaves`` optional), run INSTEAD of Kiln's own sequence and
    reported as ``sequence_source: "kiln_pro_motion_plan"``.  ``None`` means
    no plan, and the refusal stands.  Anything malformed reads as ``None``.
    """
    from kiln import _pro_motion_bridge as _bridge

    plan = _bridge.plan_motion_around_plate(state, station, action, clearance_mm)
    if not isinstance(plan, list) or not plan:
        return None
    steps: list[dict[str, Any]] = []
    for raw in plan:
        if not isinstance(raw, dict):
            return None
        gcode = raw.get("gcode")
        if not all(isinstance(raw.get(k), str) and raw.get(k) for k in ("label", "you_will_see", "stops_when")):
            return None
        if not isinstance(gcode, list) or not all(isinstance(line, str) for line in gcode):
            return None
        leaves = raw.get("leaves") or []
        if not isinstance(leaves, list) or not all(isinstance(line, str) for line in leaves):
            return None
        steps.append({
            "label": raw["label"], "you_will_see": raw["you_will_see"], "stops_when": raw["stops_when"],
            "gcode": list(gcode), "leaves": list(leaves),
        })
    return steps
