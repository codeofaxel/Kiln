"""What is on the build plate, as far as Kiln can honestly say.

Every motion Kiln sends an idle printer starts the same way: raise the
head a few millimetres, then travel.
A travel collision on that family is SILENT -- no fault code, no
read-back -- and the vendor's own Z home on the mini presses the nozzle
onto the plate.  Whether either is safe depends on one fact the printer
cannot report: **is there a part on the plate, and how tall is it?**

This module is the record that answers it.  Written at the two moments
Kiln can be sure of -- a print Kiln started (the plate now holds a part),
and a print seen ending (the part is still there) -- by a person who
says the plate is empty (``plate_clear=True`` on ``home_axes`` or
``park_head``, or ``kiln plate clear``), and by a LOOK through the
machine's camera (:func:`look`, :func:`mark_from_camera`).

**The camera is a source here, not an afterthought.**  A machine with a
camera -- the printer's own, or one the person registered against it
(``camera_snapshot_url``), which works on every adapter -- can answer
"is there a part on the plate" without anyone walking to it.  Kiln fetches
the frame and screens it for usability; the LOOKING is done by eyes that
can see, the agent's or the person's, and their answer lands here with
who did the looking recorded in ``source``.  Kiln ships no local vision
model and does not pretend to: a camera with nobody to look through it
leaves the record ``unknown`` and says a camera could settle it.

The two directions are deliberately not symmetric, for the same reason
the default is ``unknown``.  A look that says "something is there" is
acted on immediately -- it can only ever stop a motion, never start one --
and it overrides a ``clear`` record, because a stale ``clear`` is how the
head meets a part.  A look that says "empty" is recorded as ``clear``
with the camera as its source, which is what lets a fleet route onto it;
the one motion that must never act on a look, the Z home that presses the
nozzle onto the plate, asks on its own call every time regardless of the
record (see :meth:`~kiln.printers.base.PrinterAdapter._plate_gate`), so
it is unaffected.  Read by every door that moves the head:
:meth:`~kiln.printers.base.PrinterAdapter.home_axes`,
:meth:`~kiln.printers.base.PrinterAdapter.park_head`, the ``plate_status``
tool, ``kiln plate`` and ``kiln doctor`` -- and, because the file a print
starts from carries the maker's own start sequence (which drives the head
across the plate), by every door that starts a print (:func:`start_refusal`)
and every door that slices for one
(:func:`kiln.plugins.slicer_tools._apply_plate_placement`).

**The default is "unknown", and unknown refuses.**  This is the opposite of
the engagement record next door (``printers/engagement.py``), whose torn or
missing file reads as "no engagement" so a bookkeeping fault never locks a
user out of their printer.  Here the failure directions are not symmetric:
a torn file that read as "clear" would send the head across a part a
few millimetres up, silently.  A torn file that reads as "unknown" costs the person one
look at the plate.  So a missing file, a malformed one, a future schema,
and a machine with no record all read as ``unknown`` -- and the gates treat
``unknown`` exactly as they did before this record existed: ask.

The record is per machine, keyed by :func:`kiln.registry.machine_fingerprint`
(serial, else address), so it survives a process restart and a DHCP lease
change, and so two printers never share a plate.  Same store shape and
atomic write as the engagement record.  Nothing here raises into a caller.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
import zipfile
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_STORE_NAME = "plate_state.json"
_SCHEMA_VERSION = 1

#: The three things the record can say.  ``unknown`` is what every reader
#: gets when nothing trustworthy is on file.
STATUSES = ("unknown", "occupied", "clear")

#: What wrote a record that came from a look, and who did the looking.
#: ``source`` carries ``camera:<judged_by>`` so a reader can tell a person
#: at the machine from an agent reading a frame, and weigh it accordingly.
CAMERA_SOURCE_PREFIX = "camera:"
#: Who may be recorded as having looked.  An unknown judge is refused
#: rather than written as an anonymous look.
CAMERA_JUDGES = ("agent", "human")

#: How long a recorded part has to have sat before a refusal says it has
#: most likely been taken off: a finished print is rarely left on a plate
#: for a day.  This changes the sentence a refusal speaks, never the gate --
#: an old record is weaker evidence, not a different record, and only a look
#: or a person's word clears it (:func:`offer_look`).
LIKELY_GONE_AFTER_HOURS = 24.0

#: A person's own word that the plate is empty, and how it reached Kiln.
#: ``human``: said where the assistant does not hold the pen -- at this
#: computer's terminal, or in a dialog the person's app drew.
#: ``human_relayed``: typed in a chat and passed on by the assistant
#: (``look_at_plate(person_says=...)``), kept with the words typed.  Both
#: read as clear; the record says which, so nobody mistakes one for the
#: other afterwards.
SAID_DIRECTLY = "human"
SAID_IN_CHAT = "human_relayed"
PERSON_SOURCES = (SAID_DIRECTLY, SAID_IN_CHAT)

#: What the person answered when their app asked them about the plate
#: directly, for the call being served: set by the tool-call wrapper, read
#: by ``look_at_plate``.  Empty when nobody could be asked.
WORD_CONFIRMED = "confirmed"
WORD_DECLINED = "declined"

#: How long a frame of the plate stays good as the picture a start rests
#: on.  A look taken for a print that starts minutes later is about this
#: plate; one from this morning is about a plate that may have changed.
LOOK_GOOD_FOR_SECONDS = 10 * 60.0
#: Judged frames are kept, on this computer only, so a person can see what
#: a "clear" rested on.  This many, newest first; older ones are removed.
KEPT_LOOKS = 50
_LOOKS_DIR = "plate_looks"

#: The block the 3D stage draws an occupied plate from, and the shape the
#: placement verdict's ``occupancy`` carries (:mod:`kiln._pro_placement_bridge`).
OCCUPANCY_KIND = "kiln.plate_occupancy.v1"

#: The code every door that starts a print refuses with while the plate
#: still holds the last one (:func:`start_refusal`).
START_NOT_YET_CODE = "PLATE_OCCUPIED_START_NOT_YET"
#: A quiet-start file planned for a plate that is not this one any more:
#: something was printed, cleared or moved since the plan was made.
PLATE_CHANGED_CODE = "PLATE_CHANGED_SINCE_PLAN"
#: What a person is told when the plate holds a part and the file has no
#: quiet-start plan to open with.  The wrap says it when it refuses to write
#: a printer file (a file with the printer's own start would home Z onto the
#: part).  It names no tier: the plan can be missing because the caller's
#: tier does not include it, or because no tier would start this print (a
#: printer Kiln has not seen start quietly, a lift its travel cannot reach),
#: and the wrap cannot tell which.  The verdict's own start sentence, on the
#: same response, says which (``kiln.plugins.slicer_tools._attach_placement``).
PRINT_AROUND_SENTENCE = (
    "Kiln won't write a printer file while the plate still holds the last print: clear the plate and say so to "
    "print again."
)

#: What a sentence strips before it names the part on the plate.  The one
#: prettifier in public Kiln; its list matches kiln-pro's own, so the two
#: halves name the same part the same way.
_MODEL_EXTENSIONS = (".gcode.3mf", ".3mf", ".gcode", ".stl", ".obj", ".step")


def pretty_job_name(file_name: str | None) -> str:
    """``jar_v2.gcode.3mf`` -> ``jar v2``: what a sentence calls the part on the plate."""
    base = os.path.basename(str(file_name or ""))
    for ext in _MODEL_EXTENSIONS:
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    return " ".join(base.replace("_", " ").replace("-", " ").split()) or "the last part"

#: A G-code body longer than this is not scanned for its height: a scan
#: cut short would report a height that is too LOW, which is the dangerous
#: direction, so a file over the cap reports no height at all.
_MAX_SCAN_BYTES = 256 * 1024 * 1024
_SCAN_CHUNK = 4 * 1024 * 1024

#: PrusaSlicer writes ``;Z:<height>`` at every layer change; Kiln's own
#: slicer output and Kiln-wrapped 3MFs carry these in the body.  The largest
#: one is the part's top.
_Z_COMMENT_RE = re.compile(rb";Z:(\d+(?:\.\d+)?)")
#: Bambu Studio / OrcaSlicer write the height once, in the header block.
_MAX_Z_HEADER_RE = re.compile(r"^;\s*max_z_height:\s*(\d+(?:\.\d+)?)", re.MULTILINE)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _kiln_dir() -> Path:
    """``~/.kiln`` (override with ``KILN_HOME``), created on demand."""
    d = Path(os.environ.get("KILN_HOME", "").strip() or (Path.home() / ".kiln"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _store_path() -> Path:
    return _kiln_dir() / _STORE_NAME


def _read_store() -> dict[str, Any]:
    """The record file, or an empty one.  Never raises.

    Empty is what makes every machine read as ``unknown``: a truncated,
    hand-edited or future-version file must not read as "clear" (see the
    module docstring), and an empty store is the reading that asks.
    """
    try:
        raw = _store_path().read_text()
    except (OSError, ValueError):
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.debug("plate-state store unreadable; every plate reads as unknown", exc_info=True)
        return {}
    if not isinstance(data, dict) or data.get("version") != _SCHEMA_VERSION:
        return {}
    if not isinstance(data.get("machines"), dict):
        return {}
    return data


def _write_store(data: dict[str, Any]) -> None:
    """Replace the record atomically.  Never raises into a caller.

    Each write gets a temp file of its own, exactly as the engagement store
    does: two writers at once must not move a half-written file into place,
    and a torn record here would read as ``unknown`` -- safe, but a person
    asked again for no reason.
    """
    data["version"] = _SCHEMA_VERSION
    data.setdefault("machines", {})
    tmp: str | None = None
    try:
        path = _store_path()
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, path)
        tmp = None
    except (OSError, ValueError, TypeError):
        logger.debug("plate-state store could not be written", exc_info=True)
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _seconds_since(stamp: str) -> float | None:
    """Seconds since an ISO *stamp*; ``None`` when it cannot be read."""
    try:
        when = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.astimezone()
    return (datetime.now().astimezone() - when).total_seconds()


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


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
    #: The print that put the LAST part there; ``jobs`` holds every part,
    #: first to last, when a second one was started the quiet way beside
    #: the first.  ``job`` is always ``jobs[-1]``.
    job: PlateJob | None = None
    note: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    jobs: tuple[PlateJob, ...] = ()
    #: The frame a camera-sourced record was judged from, when one had
    #: just been handed over: ``{"frame": path, "frame_at": iso}``.  Empty
    #: for a record nobody looked at a picture for.
    look: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.jobs and self.job is not None:
            object.__setattr__(self, "jobs", (self.job,))
        elif self.jobs and self.job is None:
            object.__setattr__(self, "job", self.jobs[-1])

    @property
    def occupied(self) -> bool:
        return self.status == "occupied"

    @property
    def tallest_mm(self) -> float | None:
        """The tallest recorded part, or ``None`` when no height is known."""
        heights = [j.max_z_mm for j in self.jobs if j.max_z_mm is not None]
        return max(heights) if heights else None

    @property
    def clear(self) -> bool:
        return self.status == "clear"

    @property
    def from_camera(self) -> bool:
        """True when a look through the machine's camera wrote this record."""
        return self.source.startswith(CAMERA_SOURCE_PREFIX)

    @property
    def looked_by(self) -> str | None:
        """Who did the looking (``agent`` / ``human``), or ``None`` if nobody did."""
        if not self.from_camera:
            return None
        return self.source[len(CAMERA_SOURCE_PREFIX):] or None

    def fresh_look(self) -> dict[str, Any] | None:
        """The look this record rests on, when it is one a start may rest
        on: the plate was SEEN clear, in a frame Kiln still holds, taken
        within :data:`LOOK_GOOD_FOR_SECONDS`.  ``None`` for anything less
        -- a clear nobody looked for, a look with no frame, an old one."""
        if not (self.clear and self.from_camera):
            return None
        frame, taken = self.look.get("frame"), self.look.get("frame_at")
        if not (isinstance(frame, str) and frame and isinstance(taken, str) and os.path.isfile(frame)):
            return None
        age = _seconds_since(taken)
        if age is None or age > LOOK_GOOD_FOR_SECONDS:
            return None
        return {"frame": frame, "frame_at": taken, "judged_by": self.looked_by}

    def fresh_say_so(self) -> dict[str, Any] | None:
        """A person's own word that the plate is empty, given within
        :data:`LOOK_GOOD_FOR_SECONDS`; ``None`` otherwise.  What settles a
        plate the camera showed and eyes could not judge: the person at
        the machine outranks a picture, for as long as their word is
        about the plate as it is now."""
        if not (self.clear and self.source in PERSON_SOURCES and self.since):
            return None
        age = _seconds_since(self.since)
        if age is None or age > LOOK_GOOD_FOR_SECONDS:
            return None
        who = "person" if self.source == SAID_DIRECTLY else "person_via_assistant"
        return {"frame": None, "frame_at": self.since, "judged_by": who}

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

    def age_hours(self) -> float | None:
        """Hours since this record was written, or ``None`` when it cannot say."""
        if not self.since:
            return None
        try:
            when = datetime.fromisoformat(self.since)
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.astimezone()
        return max(0.0, (datetime.now().astimezone() - when).total_seconds() / 3600.0)

    def recorded_ago(self) -> str:
        """``3 days ago`` / ``5 hours ago`` / ``12 minutes ago``; ``""`` when unknown."""
        hours = self.age_hours()
        if hours is None:
            return ""
        if hours >= 48:
            return f"{int(hours // 24)} days ago"
        if hours >= 24:
            return "a day ago"
        if hours >= 2:
            return f"{int(hours)} hours ago"
        if hours >= 1:
            return "an hour ago"
        minutes = int(hours * 60)
        return f"{minutes} minutes ago" if minutes >= 2 else "a moment ago"

    @property
    def likely_gone(self) -> bool:
        """A recorded part old enough that it has most likely been taken off."""
        hours = self.age_hours()
        return self.occupied and hours is not None and hours >= LIKELY_GONE_AFTER_HOURS

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
            if self.source == SAID_DIRECTLY:
                who = "a person said so"
            elif self.source == SAID_IN_CHAT:
                who = "a person said so, passed on by their assistant"
            elif self.from_camera:
                who = f"seen empty through the camera by {'a person' if self.looked_by == 'human' else 'an agent'}"
            else:
                who = self.source
            return f"the plate was cleared at {self.since_clock()} ({who})"
        return "Kiln has no record of what is on the plate"

    def holds_sentence(self) -> str:
        """``The last print, jar v2, is still on the plate (since 18:12, about 42 mm tall).``

        The one opening every refusal about an occupied plate shares.  The
        height clause is dropped, never printed as ``None``, when the record
        could not read the file's height.
        """
        if len(self.jobs) > 1:
            names = [pretty_job_name(j.file) for j in self.jobs]
            listed = ", ".join(names[:-1]) + f" and {names[-1]}"
            tallest = self.tallest_mm
            tall = f", the tallest about {tallest:g} mm" if tallest is not None else ""
            return f"The last prints, {listed}, are still on the plate (since {self.since_clock()}{tall})."
        job = self.job
        tall = f", about {job.max_z_mm:g} mm tall" if job is not None and job.max_z_mm is not None else ""
        name = pretty_job_name(job.file if job is not None else "")
        return f"The last print, {name}, is still on the plate (since {self.since_clock()}{tall})."

    def plate_changed_sentence(self) -> str:
        """Why a quiet-start file planned for another plate does not start."""
        return (
            f"{self.holds_sentence()} This file was planned for a different plate than the one Kiln has on "
            "record now, so it won't start it. Slice it again beside what is there, or clear the plate and say so."
        )

    def start_refusal_sentence(self) -> str:
        """Why no print starts while the plate holds the last one."""
        return (
            f"{self.holds_sentence()} Kiln can't start a print onto an occupied plate yet — the "
            "printer's own start sequence drives the head across it — so it won't start this one. "
            "Clear the plate and say so."
        )

    def occupancy(self, bed_mm: Any = None) -> dict[str, Any] | None:
        """The :data:`OCCUPANCY_KIND` block from the record's own box, or ``None``
        when the plate is not occupied.

        Same shape the placement verdict carries, built from the record
        alone: one occupant -- the job's file, its footprint box and its
        height as recorded, each ``None`` when Kiln could not derive it from
        the file (a part of unknown size is still a part) -- no proposal,
        ``source: "record_box"``.  *bed_mm* is the plate's ``[x, y]`` when
        the caller knows it.
        """
        if not self.occupied:
            return None
        try:
            bed = [float(bed_mm[0]), float(bed_mm[1])] if bed_mm else None
        except (TypeError, ValueError, IndexError):
            bed = None
        occupied = [
            {
                "name": pretty_job_name(job.file),
                "rect_mm": list(job.footprint_mm) if job.footprint_mm else None,
                "top_mm": job.max_z_mm,
            }
            for job in self.jobs
        ] or [{"name": "a part", "rect_mm": None, "top_mm": None}]
        return {
            "kind": OCCUPANCY_KIND,
            "bed_mm": bed,
            "occupied": occupied,
            "proposed": None,
            "source": "record_box",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "machine": self.machine,
            "status": self.status,
            "source": self.source,
            "since": self.since,
            "job": self.job.to_dict() if self.job else None,
            "jobs": [j.to_dict() for j in self.jobs],
            "fingerprint": fingerprint(self) if self.occupied else None,
            "note": self.note,
            "description": self.describe(),
            # How old the record is, in words.  A part recorded days ago is
            # weaker evidence than one recorded an hour ago, and a reader
            # should not have to do date arithmetic to notice.
            "recorded_ago": self.recorded_ago() or None,
            # Whether a look wrote this, and whose eyes.  A reader that
            # weighs a camera answer differently from a person at the
            # machine needs both, and neither is derivable from `source`
            # without knowing this module's spelling.
            "from_camera": self.from_camera,
            "looked_by": self.looked_by,
            # The picture a camera-sourced record was judged from, kept on
            # this computer; ``None`` when no picture stands behind it.
            "look": dict(self.look) or None,
        }

    @classmethod
    def from_dict(cls, machine: str, data: Any) -> PlateState:
        """A record row, or ``unknown`` for anything that is not a well-formed one."""
        if not isinstance(data, dict) or data.get("status") not in STATUSES:
            return cls(machine=machine)
        since = data.get("since")
        listed = data.get("jobs")
        jobs: list[PlateJob] = []
        if isinstance(listed, list):
            jobs = [j for j in (PlateJob.from_dict(entry) for entry in listed) if j is not None]
        if not jobs:
            job = PlateJob.from_dict(data.get("job"))
            jobs = [job] if job is not None else []
        return cls(
            machine=machine,
            status=str(data["status"]),
            source=str(data.get("source") or "unknown"),
            since=since if isinstance(since, str) else None,
            job=jobs[-1] if jobs else None,
            jobs=tuple(jobs),
            note=str(data.get("note") or ""),
            look=dict(data["look"]) if isinstance(data.get("look"), dict) else {},
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
    machine = machine_id(adapter)
    if not machine:
        return PlateState(machine="", note="this printer reports neither a serial nor an address, so nothing can be recorded for it")
    try:
        return PlateState.from_dict(machine, _read_store().get("machines", {}).get(machine))
    except Exception:  # noqa: BLE001
        logger.debug("plate-state read failed", exc_info=True)
        return PlateState(machine=machine)


def start_refusal(
    adapter: Any, *, resume: bool = False, file_name: str | None = None, local_path: str | None = None,
) -> dict[str, Any] | None:
    """The one gate every door that starts a print calls, before the start.

    ``None`` when the plate is clear or unrecorded; otherwise the refusal
    every start door returns, in the standard error envelope
    (``{"success": False, "error": {"code", "message", "retryable"}}`` --
    the same shape :func:`kiln.server._error_dict` builds -- with the
    record and its occupancy block beside it, and a look at the plate:
    how old the record is and, where the machine has a camera, a frame
    saved to ``snapshot_path`` for eyes to judge; see :func:`offer_look`).
    The reason is physical: the file a print starts from carries the
    maker's own start sequence, which drives the head across the plate at
    a few millimetres, so a part left there is hit before the first layer.  A *resume* is that same job,
    still on the plate where it paused, and passes.

    A QUIET-START file passes too, when its contract names this plate as
    it stands now: *file_name* (the printer-side name, joined to Kiln's
    own copy through the slice ledger) or *local_path* (the file itself)
    is read for Kiln's quiet-start header, and a header whose plate
    fingerprint and machine match the record hands the decision to the
    live judge every start passes through
    (:func:`kiln.printers.print_gate.evaluate_quiet_start`), which asks
    the printer itself.  A header for a plate that has changed since the
    plan refuses with :data:`PLATE_CHANGED_CODE`.  Never raises.
    """
    if resume:
        return None
    try:
        state = read(adapter)
    except Exception:  # noqa: BLE001 -- an unreadable record reads as unknown, which passes
        return None
    if not state.occupied:
        return None
    contract = quiet_start_contract_for(file_name, local_path=local_path)
    if contract is not None:
        planned_plate = str(contract.get("planned_for_plate") or "")
        planned_machine = str(contract.get("planned_for_machine") or "")
        if planned_plate == fingerprint(state) and planned_machine and planned_machine == _machine_contract_id(adapter):
            return None
        # Planned beside a part and the plate has changed since: the remedy
        # is the plan's, not a look.
        return {
            "success": False,
            "error": {"code": PLATE_CHANGED_CODE, "message": state.plate_changed_sentence(), "retryable": False},
            "plate": state.to_dict(),
            "occupancy": state.occupancy(None),
        }
    # The record says a part is there; it does not say the part is STILL
    # there.  So the refusal hands over a look, where the machine has a
    # camera, and says how old the record is -- rather than asking a person
    # to vouch for a plate Kiln could have looked at.
    offer = offer_look(adapter, state)
    return {
        "success": False,
        "error": {
            "code": START_NOT_YET_CODE,
            "message": f"{state.start_refusal_sentence()} {offer.sentence}",
            "retryable": False,
        },
        "plate": state.to_dict(),
        "occupancy": state.occupancy(None),
        **offer.fields(),
    }


def fingerprint(state: PlateState) -> str:
    """What the record says is on this plate, as one short hash: the
    machine and every part's file, footprint and top.  A quiet-start file
    carries the fingerprint of the plate it was planned for, and starts
    only while the record still reads the same.  ``since`` is left out on
    purpose: a print seen ending re-stamps the moment, not the parts.
    """
    import hashlib

    parts: list[str] = [state.machine]
    for job in state.jobs:
        rect = ",".join(f"{v:.1f}" for v in job.footprint_mm) if job.footprint_mm else "-"
        top = f"{job.max_z_mm:.1f}" if job.max_z_mm is not None else "-"
        parts.append(f"{os.path.basename(job.file)}|{rect}|{top}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _machine_contract_id(adapter: Any) -> str:
    """The identity a quiet-start file is bound to -- the same one the
    same-bed retry binds to, so one gate reads both."""
    try:
        from kiln.printers.print_gate import same_bed_machine_id

        return same_bed_machine_id(adapter)
    except Exception:  # noqa: BLE001
        return ""


def quiet_start_contract_for(file_name: str | None, *, local_path: str | None = None) -> dict[str, str] | None:
    """Kiln's quiet-start header from the file behind *file_name*, or ``None``.

    *local_path* wins when given; else the slice ledger joins the
    printer-side name to the wrap Kiln wrote, and a bare path that exists
    is read as itself.  Never raises.
    """
    try:
        from kiln.printers.print_gate import read_quiet_start_contract

        for candidate in _local_candidates(file_name, local_path):
            contract = read_quiet_start_contract(candidate)
            if contract is not None:
                return contract
    except Exception:  # noqa: BLE001 -- an unreadable file carries no contract
        logger.debug("quiet-start contract lookup failed", exc_info=True)
    return None


def _local_candidates(file_name: str | None, local_path: str | None) -> list[str]:
    out: list[str] = []
    if isinstance(local_path, str) and local_path and os.path.isfile(local_path):
        out.append(local_path)
    if isinstance(file_name, str) and file_name:
        if os.path.isfile(file_name):
            out.append(file_name)
        try:
            from kiln.monitor_twin import sliced_entry_for

            entry = sliced_entry_for(file_name)
            for key in ("wrapped", "output"):
                path = entry.get(key) if isinstance(entry, dict) else None
                if isinstance(path, str) and os.path.isfile(path):
                    out.append(path)
        except Exception:  # noqa: BLE001
            logger.debug("slice ledger lookup failed", exc_info=True)
    return out


def quiet_start_flags(contract: dict[str, str]) -> dict[str, bool]:
    """The start-command switches the contract says are off, as the kwargs
    every start door hands the adapter."""
    names = [n.strip() for n in str(contract.get("switched_off") or "").split(",") if n.strip()]
    return {name: False for name in names}


def plate_occupancy(adapter: Any) -> PlateState:
    """The one door every motion gate calls.

    Today it is the record and nothing else.  Kept as its own name so the
    gates in ``home_axes`` / ``park_head`` / the doors never grow a second
    way of asking, and so anything that later enriches the answer (a
    device record learned from the printer's own prints) lands here once.
    """
    return read(adapter)


def _write_state(
    adapter: Any, *, status: str, source: str, job: PlateJob | None, note: str,
    jobs: tuple[PlateJob, ...] | list[PlateJob] | None = None,
    look: dict[str, Any] | None = None,
) -> None:
    machine = machine_id(adapter)
    if not machine:
        return
    all_jobs = list(jobs) if jobs else ([job] if job is not None else [])
    try:
        store = _read_store() or {"machines": {}}
        row: dict[str, Any] = {
            "status": status,
            "source": source,
            "since": _now_iso(),
            "job": all_jobs[-1].to_dict() if all_jobs else None,
            "jobs": [j.to_dict() for j in all_jobs],
            "note": note,
        }
        if look:
            row["look"] = look
        store.setdefault("machines", {})[machine] = row
        # Any record written is about the plate as it is now; a frame
        # handed over before it belongs to no later look.
        frames = store.get("frames")
        if isinstance(frames, dict):
            frames.pop(machine, None)
        _write_store(store)
    except Exception:  # noqa: BLE001 -- bookkeeping never breaks the motion it describes
        logger.debug("plate-state write failed", exc_info=True)


def mark_occupied(
    adapter: Any, job: PlateJob | dict[str, Any] | None, *, source: str = "kiln_started_print", note: str = "",
    keep_previous: bool = False,
) -> None:
    """The plate holds a part.

    *job* may be ``None`` when the caller knows only that something is there
    (a print seen ending that Kiln did not start); then the jobs already on
    record, if any, are kept -- the parts have not changed, only the moment.
    *keep_previous* is a print started the quiet way BESIDE what was there:
    the earlier parts stay on record and this one joins them.
    """
    try:
        if isinstance(job, dict):
            job = PlateJob.from_dict(job)
        previous = read(adapter)
        if job is None:
            jobs = list(previous.jobs) if previous.occupied else []
        elif keep_previous and previous.occupied:
            jobs = [*previous.jobs, job]
        else:
            jobs = [job]
        _write_state(adapter, status="occupied", source=source, job=jobs[-1] if jobs else None, jobs=jobs, note=note)
    except Exception:  # noqa: BLE001
        logger.debug("mark_occupied failed", exc_info=True)


_plate_word_answer: ContextVar[str] = ContextVar("kiln_plate_word_answer", default="")


def note_plate_word_answer(answer: str) -> None:
    """Record, for the call being served, what the person answered when
    their app asked them about the plate; ``""`` forgets it."""
    _plate_word_answer.set(str(answer or ""))


def plate_word_answer() -> str:
    """:data:`WORD_CONFIRMED`, :data:`WORD_DECLINED`, or ``""`` when nobody
    could be asked on this call."""
    return _plate_word_answer.get()


#: Words that turn "clear" into something else.  A person who is not sure
#: has not said the plate is empty -- and neither has one who says they
#: WILL empty it ("I'll clear it later", "once it's clear") or asks for it
#: to be emptied ("clear the bed for me"): "clear" and "empty" are verbs
#: too, and only the statement that it IS so counts.
_NOT_A_CLEAR = re.compile(
    r"\?|\b(not|isn'?t|ain'?t|wasn'?t|never|unsure|maybe|probably|think|guess|should|might|almost|"
    r"don'?t|doesn'?t|can'?t|cannot|if|"
    r"will|won'?t|(i|we|it|that|you)'?ll|gonna|later|tomorrow|soon|once|when|until|unless|before|after|"
    r"need|needs|must|please|let|wait|(can|could|would) (you|u|we|i)|"
    r"(to|and|then) (clear|empty)|(clear|empty) (the|my|it|off|out|this|that))\b"
)
_A_CLEAR = re.compile(r"\b(clear|cleared|empty|emptied|nothing on)\b")


def says_plate_is_clear(words: str | None) -> bool:
    """Whether a person's typed words say, flatly, that the plate is empty.

    Read narrowly on purpose: "bed clear", "printbed clear", "the plate is
    empty", "nothing on it".  A question, a hedge ("should be clear", "I
    think so") or a negation is not a statement that it is empty, and
    neither is a bare "yes" -- the words have to carry it themselves,
    because they are what is kept on the record.
    """
    text = " ".join(str(words or "").lower().split())
    if not text or len(text) > 200:
        return False
    return bool(_A_CLEAR.search(text)) and not _NOT_A_CLEAR.search(text)


def mark_clear(adapter: Any, source: str, *, note: str = "") -> None:
    """A person says the plate is empty.  It stays clear until the next print starts.

    The record answers the row question for home X and park.  It never
    answers for a Z home that presses the nozzle onto the plate: that
    motion reads ``plate_clear`` on its own call, every time (see
    :meth:`~kiln.printers.base.PrinterAdapter._plate_gate`).
    """
    _write_state(adapter, status="clear", source=source, job=None, note=note)


# ---------------------------------------------------------------------------
# The look: what the machine's camera can settle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlateLook:
    """What Kiln can offer of a look at one machine's plate, right now.

    ``available`` is whether a frame was obtained and is worth looking at.
    ``camera`` is where it came from (``user_supplied`` for a camera the
    person registered against this printer, ``printer`` for the machine's
    own, ``None`` for a machine with neither).  ``image_b64`` is the frame
    for eyes that can see it; ``why`` says, in one sentence, what stopped a
    look when one could not be offered.  Kiln judges nothing here.
    """

    available: bool
    camera: str | None = None
    image_b64: str | None = None
    media_type: str | None = None
    why: str = ""

    @property
    def possible(self) -> bool:
        """Whether this machine could answer the question if someone looked."""
        return self.camera is not None

    def to_dict(self) -> dict[str, Any]:
        # The frame is deliberately NOT in here: this dict rides answers and
        # logs, and a base64 JPEG in either is noise at best.  A caller that
        # wants the frame takes `image_b64` off the object.
        return {
            "available": self.available,
            "possible": self.possible,
            "camera": self.camera,
            "media_type": self.media_type,
            "why": self.why,
        }


def camera_of(adapter: Any) -> str | None:
    """Which camera this machine has (``user_supplied`` / ``printer``), or ``None``.

    Asked before a refusal is worded, so a machine that COULD answer the
    question is never told to go and look by hand.  Never raises.

    Read the way every adapter states it:
    :attr:`~kiln.printers.base.PrinterAdapter.snapshot_source` is a
    property.  This used to CALL it, which raised on every real adapter
    (a string is not callable), was swallowed as "cannot say", and read
    every printer Kiln supports as having no camera -- so no look was
    ever offered or taken on a real machine, while the tests, whose
    stand-in handed over a function, stayed green.  Only the two values
    an adapter can state count; anything else is no camera.
    """
    try:
        source = adapter.snapshot_source
    except Exception:  # noqa: BLE001 -- a machine that cannot say has no camera Kiln can use
        return None
    return source if source in ("user_supplied", "printer") else None


#: How Kiln came to know a machine has a camera.
CAMERA_SEEN = "seen"                # it gave Kiln a picture
CAMERA_PERSON_SAID = "person_said"  # the person said so, at their own terminal
_CAMERA_SOURCES = ("printer", "user_supplied")


def remember_camera(adapter: Any, source: str | None, how: str = CAMERA_SEEN) -> None:
    """Keep, for this machine, that a camera exists and whose it is.

    *source* is the adapter's own word for where the picture came from:
    ``"printer"`` (through the printer's own connection) or
    ``"user_supplied"`` (a camera the person registered beside it).  The
    two are kept apart, so a camera on a tripod is never remembered as the
    printer's own.  A camera once seen is never unlearned by a failed
    look: a camera that does not answer today is a camera that is not
    answering, not a printer that never had one.  Written once per source;
    a look that only confirms what is known writes nothing.  Never raises.
    """
    machine = machine_id(adapter)
    if not machine or source not in _CAMERA_SOURCES:
        return
    try:
        store = _read_store() or {"machines": {}}
        known = store.setdefault("cameras", {}).setdefault(machine, {})
        had = known.get(source) if isinstance(known.get(source), dict) else None
        if had is not None and (had.get("how") == CAMERA_SEEN or how != CAMERA_SEEN):
            return  # nothing new: already seen, or already said and only said again
        known[source] = {"how": how, "first_at": (had or {}).get("first_at") or _now_iso()}
        _write_store(store)
    except Exception:  # noqa: BLE001 -- forgetting costs one question later; it never breaks a look
        logger.debug("camera not remembered", exc_info=True)


def cameras_on_record(adapter: Any) -> dict[str, dict[str, Any]]:
    """What :func:`remember_camera` has kept for this machine, by source.
    Empty when nothing is known.  Never raises."""
    machine = machine_id(adapter)
    if not machine:
        return {}
    try:
        known = (_read_store().get("cameras") or {}).get(machine)
        return {k: dict(v) for k, v in known.items() if k in _CAMERA_SOURCES and isinstance(v, dict)} if isinstance(known, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def knows_a_camera(adapter: Any) -> str | None:
    """Which camera Kiln knows this machine has -- whether or not it is
    answering right now -- or ``None`` when it knows of none.

    ``"user_supplied"``: a camera the person registered, for as long as it
    is registered.  Taking it away takes the knowledge with it; having
    once seen through it says nothing about the printer itself.
    ``"printer"``: the machine's own, known one of three ways -- its
    backend says every machine it drives leaves the factory with one
    (``camera_fitted_at_factory``), Kiln has had a picture through the
    printer's own connection before, or the person said it has one.
    ``None`` also for a backend that cannot read a camera at all.
    """
    source = camera_of(adapter)
    if source != "printer":
        return source  # a registered camera, or no way to read one
    try:
        if adapter.camera_fitted_at_factory is True:
            return "printer"
    except Exception:  # noqa: BLE001 -- an adapter that cannot say has not said yes
        pass
    return "printer" if "printer" in cameras_on_record(adapter) else None


def look(adapter: Any) -> PlateLook:
    """Fetch a frame of this machine's plate for someone to look at.

    Kiln's whole part in a look: get the frame, screen it for whether it is
    worth looking at, hand it over.  The judging is done by eyes -- the
    agent's or the person's -- and their answer comes back through
    :func:`mark_from_camera`.  Never raises, never moves a head, never
    writes the plate's record.  (A camera that answers with a real picture
    is remembered as one this machine has: :func:`remember_camera`.)
    """
    camera = camera_of(adapter)
    if camera is None:
        return PlateLook(False, None, why="this printer has no camera Kiln can read, and none is registered for it")
    try:
        frame = adapter.get_snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.debug("plate look: snapshot failed", exc_info=True)
        return PlateLook(False, camera, why=f"the camera did not answer ({str(exc)[:120]})")
    if not frame or not isinstance(frame, (bytes, bytearray)):
        return PlateLook(False, camera, why="the camera answered with no image")
    try:
        from kiln.snapshot_analysis import analyze_snapshot, image_dimensions

        size = image_dimensions(frame)
        # The dimensions matter here specifically: a thumbnail cannot show
        # whether a part is on the bed, and the screen skips that check
        # when it is not told the size.
        verdict = analyze_snapshot(frame, width=size[0] if size else None, height=size[1] if size else None)
        if verdict.valid:
            # A real picture came back, usable or not (a capped lens is
            # still a camera): this machine has one.
            remember_camera(adapter, camera)
        if not verdict.valid or not verdict.usable_quality:
            # The camera is there and answering, but the frame cannot settle
            # anything -- lens capped, light off, too small to read.  Saying
            # which is the difference between "fix your camera" and "go look".
            why = "; ".join(verdict.warnings) if verdict.warnings else "the picture is not clear enough to judge"
            return PlateLook(False, camera, why=why)
    except Exception:  # noqa: BLE001 -- the screen is a courtesy; a frame still beats none
        logger.debug("plate look: snapshot screening failed", exc_info=True)
    import base64 as _base64

    return PlateLook(
        True, camera, image_b64=_base64.b64encode(frame).decode("ascii"),
        media_type="image/png" if frame[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg",
    )


def mark_from_camera(
    adapter: Any, *, seen: str, judged_by: str, note: str = "", job: PlateJob | None = None,
) -> str | None:
    """Record what a look saw.  Returns the new status, or ``None`` when refused.

    *seen* is ``"clear"`` (the plate is empty) or ``"occupied"`` (something is
    on it); *judged_by* is ``"agent"`` or ``"human"`` -- who actually looked.
    An unrecognised value for either is refused rather than written, because
    a record nobody can attribute is worse than no record.

    The asymmetry this module's docstring states lives here.  ``occupied``
    is written whatever the record said before: a look that sees a part can
    only stop a motion, and a stale ``clear`` is exactly what it exists to
    catch.  ``clear`` is written as a camera-sourced record, which reads as
    clear to every door that weighs the record -- and the Z home that
    presses the nozzle onto the plate asks on its own call regardless, so
    the one motion that must not act on a look does not.
    """
    if seen not in ("clear", "occupied") or judged_by not in CAMERA_JUDGES:
        logger.debug("plate look refused: seen=%r judged_by=%r", seen, judged_by)
        return None
    source = f"{CAMERA_SOURCE_PREFIX}{judged_by}"
    try:
        # The picture this verdict is about: the frame last handed over for
        # this machine, kept so a person can see what the verdict rested on.
        judged = _keep_judged_frame(adapter)
        if seen == "occupied":
            previous = read(adapter)
            # A look cannot say WHICH part is there or how tall it is.  When
            # the record already names parts, they are kept -- the look
            # confirms them, it does not replace them with a blank.
            jobs = list(previous.jobs) if previous.occupied and job is None else ([job] if job is not None else [])
            _write_state(
                adapter, status="occupied", source=source, job=jobs[-1] if jobs else None, jobs=jobs, note=note,
                look=judged,
            )
            return "occupied"
        _write_state(adapter, status="clear", source=source, job=None, note=note, look=judged)
        return "clear"
    except Exception:  # noqa: BLE001 -- bookkeeping never breaks the door that called it
        logger.debug("mark_from_camera failed", exc_info=True)
        return None


def camera_could_settle(adapter: Any) -> str | None:
    """One clause a refusal can append when a camera could answer instead.

    ``None`` when the machine has no camera, so a refusal that has nothing
    to offer does not offer it.  Reads the declared camera only and never
    fetches a frame -- a survey of many machines words its rows with this;
    a refusal about one machine hands over the frame itself
    (:func:`offer_look`).
    """
    camera = camera_of(adapter)
    if camera is None:
        return None
    whose = "the camera you registered for it" if camera == "user_supplied" else "this printer's own camera"
    return f"or look through {whose} and tell Kiln what you see"


#: What a person says when they have looked themselves -- the fallback every
#: offer ends on, and the default for a door that names no verb of its own.
SAY_SO = (
    "`kiln plate clear`, or plate_clear=true on park_head; or, in a chat, the person's own words that it "
    "is clear, passed exactly as typed to look_at_plate(person_says=...)"
)


@dataclass(frozen=True)
class LookOffer:
    """What a refusal about a recorded part hands over instead of assuming.

    ``sentence`` is appended to the refusal; ``snapshot_path`` is the frame
    on disk for eyes to judge (``None`` without a usable one); ``look`` says
    which camera and, when there is no frame, why.
    """

    sentence: str
    look: PlateLook
    snapshot_path: str | None = None
    recorded_ago: str = ""
    likely_gone: bool = False

    def fields(self) -> dict[str, Any]:
        """The keys a refusal carries beside its sentence."""
        return {
            "snapshot_path": self.snapshot_path,
            "look": {
                **self.look.to_dict(),
                "recorded_ago": self.recorded_ago or None,
                "likely_gone": self.likely_gone,
                "settle_with": "look_at_plate" if self.snapshot_path else None,
            },
        }


def save_frame(found: PlateLook) -> str | None:
    """Write a look's frame where eyes can open it; ``None`` when it cannot be."""
    if not found.available or not found.image_b64:
        return None
    try:
        import base64 as _base64
        import time as _time

        suffix = "png" if found.media_type == "image/png" else "jpg"
        path = Path(tempfile.gettempdir()) / f"kiln_plate_{int(_time.time() * 1000)}.{suffix}"
        path.write_bytes(_base64.b64decode(found.image_b64))
        return str(path)
    except Exception:  # noqa: BLE001 -- a frame that cannot be saved is reported as no frame
        logger.debug("plate look: frame could not be saved", exc_info=True)
        return None


def hand_over_frame(adapter: Any, found: PlateLook) -> str | None:
    """Save a look's frame for eyes to judge, and remember that THIS frame
    is the one now in front of them -- so the verdict that follows
    (:func:`mark_from_camera`) is recorded against the picture it was
    about.  The path, or ``None`` when there is no frame to hand over."""
    path = save_frame(found)
    machine = machine_id(adapter)
    if not path or not machine:
        return path
    try:
        store = _read_store() or {"machines": {}}
        store.setdefault("frames", {})[machine] = {"path": path, "at": _now_iso()}
        _write_store(store)
    except Exception:  # noqa: BLE001 -- the frame is still handed over; only the link to a verdict is lost
        logger.debug("plate look: handed-over frame not remembered", exc_info=True)
    return path


def _keep_judged_frame(adapter: Any) -> dict[str, Any] | None:
    """The frame last handed over for this machine, moved to where it is
    kept: ``{"frame": path, "frame_at": iso}``.  ``None`` when no frame
    was handed over within :data:`LOOK_GOOD_FOR_SECONDS` -- a verdict with
    no recent picture behind it is recorded as before, with no look."""
    machine = machine_id(adapter)
    if not machine:
        return None
    try:
        handed = (_read_store().get("frames") or {}).get(machine)
        if not isinstance(handed, dict):
            return None
        path, taken = handed.get("path"), handed.get("at")
        if not (isinstance(path, str) and isinstance(taken, str) and os.path.isfile(path)):
            return None
        age = _seconds_since(taken)
        if age is None or age > LOOK_GOOD_FOR_SECONDS:
            return None
        import shutil

        kept_dir = _kiln_dir() / _LOOKS_DIR
        kept_dir.mkdir(parents=True, exist_ok=True)
        kept = kept_dir / os.path.basename(path)
        shutil.copyfile(path, kept)
        os.chmod(kept, 0o600)
        for old in sorted(kept_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[KEPT_LOOKS:]:
            with contextlib.suppress(OSError):
                old.unlink()
        return {"frame": str(kept), "frame_at": taken}
    except Exception:  # noqa: BLE001 -- the verdict is still recorded, without a picture
        logger.debug("plate look: judged frame not kept", exc_info=True)
        return None


#: What a look can vouch for, for a start nobody is being asked about.
LOOK_NO_CAMERA = "no_camera"   # nothing to look through; the plate is not checked
LOOK_CLEAR = "clear"           # seen clear, in a fresh frame Kiln holds
LOOK_OCCUPIED = "occupied"     # the record says a part is there; the start gate refuses
LOOK_NEEDED = "needed"         # a frame is ready; eyes have not judged it yet
LOOK_BLIND = "blind"           # there is a camera, and it gave nothing usable


@dataclass(frozen=True)
class UnaskedStartLook:
    """The plate, for a print about to start with no person asked."""

    verdict: str
    camera: str | None = None
    frame: str | None = None
    frame_at: str | None = None
    judged_by: str | None = None
    why: str = ""

    def evidence(self) -> dict[str, Any]:
        """What the start rested on, for its audit line and its result."""
        if self.verdict == LOOK_CLEAR:
            # ``frame`` is None when a person's word, not a picture, is
            # what the start rested on; ``judged_by`` says which.
            return {
                "checked": True, "camera": self.camera, "frame": self.frame,
                "frame_at": self.frame_at, "judged_by": self.judged_by,
            }
        return {"checked": False, "camera": self.camera, "why": self.why or self.verdict}


def look_for_unasked_start(adapter: Any) -> UnaskedStartLook:
    """What stands between a standing permission and the motors, when
    nobody is being asked: a look at the plate.

    A person saying yes to a print can see their own printer.  A print
    started under a standing permission has nobody looking, so where the
    machine has a camera Kiln can read, the plate has to have been SEEN
    clear -- in a frame taken within :data:`LOOK_GOOD_FOR_SECONDS` and
    still on this computer.  The record alone is not enough: ``clear``
    from this morning says nothing about a print sent since from the
    maker's own app.

    Five answers.  No camera: nothing to check with, said as such.  A
    fresh look on record: clear, with its frame -- or a person's own word
    that the plate is empty, given as recently, which is how a picture
    eyes could not judge gets settled.  A record that says a part is
    there: the start gate's own refusal handles it.  Otherwise a
    frame is fetched now: usable, it is handed over to be judged
    (:func:`hand_over_frame`); not usable, the camera is blind and the
    reason is given.  Never raises, never moves a head, never writes the
    plate record.
    """
    try:
        camera = camera_of(adapter)
        if camera is None:
            return UnaskedStartLook(LOOK_NO_CAMERA, why="this printer has no camera Kiln can use")
        state = read(adapter)
        if state.occupied:
            return UnaskedStartLook(LOOK_OCCUPIED, camera)
        fresh = state.fresh_look() or state.fresh_say_so()
        if fresh is not None:
            return UnaskedStartLook(LOOK_CLEAR, camera, **fresh)
        found = look(adapter)
        path = hand_over_frame(adapter, found)
        if not found.available or not path:
            why = (found.why or "it gave no picture").rstrip(". ")
            return UnaskedStartLook(LOOK_BLIND, camera, why=why)
        return UnaskedStartLook(LOOK_NEEDED, camera, frame=path, frame_at=_now_iso())
    except Exception as exc:  # noqa: BLE001 -- a look that failed has seen nothing
        logger.debug("look for an unasked start failed", exc_info=True)
        return UnaskedStartLook(LOOK_BLIND, None, why=f"Kiln could not look ({str(exc)[:120]})")


def offer_look(adapter: Any, state: PlateState | None = None, *, say_so: str = SAY_SO) -> LookOffer:
    """Look instead of assume: what every refusal about a recorded part offers.

    A record says a part was there when it was written.  Whether it is
    still there is a question the machine's camera can usually answer and
    the record cannot -- and a record days old most likely describes a
    part someone took off long ago.  So a door that refuses over a
    recorded part calls this, and its refusal then carries:

    * how old the record is, and -- past :data:`LIKELY_GONE_AFTER_HOURS`
      -- that the part has most likely been taken off;
    * where the machine has a camera, a frame of the plate saved to disk,
      and the one call that records what the frame shows
      (``look_at_plate``, with ``seen``);
    * where it has none, or the camera gave nothing usable, the reason
      and the person's own doors (*say_so*).

    Kiln judges nothing here and clears nothing: the record changes only
    when eyes -- an agent's on the frame, or a person's on the plate --
    say what they saw.  A stale ``occupied`` costs one look; a wrong
    ``clear`` drives a head into a part, so age alone never clears it.
    The Z home that presses the nozzle onto the plate does not use this:
    that motion asks a person on every call (see
    :meth:`~kiln.printers.base.PrinterAdapter._plate_gate`).  Never
    raises, never moves a head, never writes the record.
    """
    try:
        if state is None:
            state = read(adapter)
        ago = state.recorded_ago() if state.occupied else ""
        likely_gone = state.likely_gone
        age = ""
        if ago:
            age = f"Kiln recorded that {ago} and has not looked since"
            age += (
                " -- a finished part is rarely left on a plate that long, so it has most likely been taken off. "
                if likely_gone else ". "
            )
        found = look(adapter)
        path = hand_over_frame(adapter, found)
        if found.camera is None:
            sentence = (
                f"{age}This printer has no camera Kiln can read, so look at the plate yourself, "
                f"then say so: {say_so}."
            )
        else:
            whose = "the camera you registered for it" if found.camera == "user_supplied" else "this printer's own camera"
            if path:
                # The frame is a file here; an agent whose host cannot open
                # files sees it by calling look_at_plate, which hands the
                # picture over as an image.
                sentence = (
                    f"{age}Here is the plate now, through {whose}: {path} (or call look_at_plate to see it). "
                    'Look at the picture, then call look_at_plate with seen="clear" if the plate is empty, or '
                    'seen="occupied" if anything is on it or you cannot tell -- and ask again. '
                    f"(Standing at the machine? Say so yourself: {say_so}.)"
                )
            else:
                why = (found.why or "it gave no picture").rstrip(". ")
                sentence = (
                    f"{age}{whose[0].upper()}{whose[1:]} could settle it, but {why}. Look at the plate "
                    f"yourself, then say so: {say_so}."
                )
        return LookOffer(sentence=sentence, look=found, snapshot_path=path, recorded_ago=ago, likely_gone=likely_gone)
    except Exception:  # noqa: BLE001 -- the offer is a courtesy; the refusal stands without it
        logger.debug("offer_look failed", exc_info=True)
        return LookOffer(
            sentence=f"Look at the plate, then say so: {say_so}.",
            look=PlateLook(False, None, why="Kiln could not look"),
        )


def mark_unknown(adapter: Any, why: str) -> None:
    """Forget what was known; the next motion asks again."""
    _write_state(adapter, status="unknown", source="reset", job=None, note=why)


# ---------------------------------------------------------------------------
# Geometry of the file a print was started from
# ---------------------------------------------------------------------------


def _scan_max_z(stream: Any, size_hint: int | None) -> float | None:
    """The largest ``;Z:`` comment in a G-code body, or ``None``.

    Chunked on bytes so a big file costs a fraction of a second, with an
    overlap so a comment split across chunks is still seen.  Over the cap
    the answer is ``None``, never a partial maximum.
    """
    if size_hint is not None and size_hint > _MAX_SCAN_BYTES:
        return None
    best: float | None = None
    seen = 0
    tail = b""
    while True:
        chunk = stream.read(_SCAN_CHUNK)
        if not chunk:
            break
        seen += len(chunk)
        if seen > _MAX_SCAN_BYTES:
            return None
        buf = tail + chunk
        for match in _Z_COMMENT_RE.finditer(buf):
            z = float(match.group(1))
            if best is None or z > best:
                best = z
        tail = buf[-32:]
    return best


def _header_max_z(head: str) -> float | None:
    m = _MAX_Z_HEADER_RE.search(head)
    return float(m.group(1)) if m else None


def _union_bbox(objects: Any) -> list[float] | None:
    boxes: list[list[float]] = []
    for obj in objects if isinstance(objects, list) else []:
        bbox = obj.get("bbox") if isinstance(obj, dict) else None
        if isinstance(bbox, list) and len(bbox) == 4:
            try:
                boxes.append([float(v) for v in bbox])
            except (TypeError, ValueError):
                continue
    if not boxes:
        return None
    return [
        min(b[0] for b in boxes), min(b[1] for b in boxes),
        max(b[2] for b in boxes), max(b[3] for b in boxes),
    ]


def geometry_of(path: str, *, plate_number: int = 1) -> tuple[list[float] | None, float | None]:
    """``(footprint_mm, max_z_mm)`` for a local G-code or 3MF, each ``None`` when not derivable.

    Height: the largest PrusaSlicer ``;Z:`` layer comment in the body wins.
    Kiln slices with PrusaSlicer and its Bambu wrapper keeps the body, so
    that covers every file Kiln made.  The header's ``max_z_height`` is
    trusted only where it is provably a slicer's own: a standalone G-code
    (Kiln never writes a header into one), or a 3MF whose plate JSON lists
    objects -- Kiln's wrapper writes an EMPTY ``bbox_objects`` and a
    ``max_z_height`` that falls back to 10.0 when the body has no layer
    comments, and a fallback read as a real height is exactly the wrong
    direction for a clearance check.

    Footprint: the union of the plate JSON's object boxes, so only a real
    Bambu Studio / OrcaSlicer export has one.  A file that yields nothing is
    still a part on the plate; the caller records it with no size.
    """
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as zf:
                names = set(zf.namelist())
                footprint: list[float] | None = None
                objects_listed = False
                for candidate in (f"Metadata/plate_{plate_number}.json", f"metadata/plate_{plate_number}.json"):
                    if candidate in names:
                        plate = json.loads(zf.read(candidate).decode("utf-8", errors="replace"))
                        objects = plate.get("bbox_objects") if isinstance(plate, dict) else None
                        objects_listed = bool(objects)
                        footprint = _union_bbox(objects)
                        break
                max_z: float | None = None
                for candidate in (f"Metadata/plate_{plate_number}.gcode", f"metadata/plate_{plate_number}.gcode"):
                    if candidate in names:
                        info = zf.getinfo(candidate)
                        with zf.open(candidate) as member:
                            max_z = _scan_max_z(member, info.file_size)
                        if max_z is None and objects_listed:
                            with zf.open(candidate) as member:
                                max_z = _header_max_z(member.read(8192).decode("utf-8", errors="replace"))
                        break
                return footprint, max_z
        with open(path, "rb") as handle:
            max_z = _scan_max_z(handle, os.path.getsize(path))
        if max_z is None:
            with open(path, "rb") as handle:
                max_z = _header_max_z(handle.read(8192).decode("utf-8", errors="replace"))
        return None, max_z
    except Exception:  # noqa: BLE001 -- geometry is a courtesy; its absence is recorded, never invented
        logger.debug("plate geometry of %s not derivable", path, exc_info=True)
        return None, None


def _local_files_for(file_name: str) -> list[str]:
    """Local files that ARE the printer-side *file_name*, most useful first.

    A path that exists is itself.  Otherwise the slice ledger
    (``kiln.monitor_twin``) joins the printer-side name to the G-code Kiln
    sliced and the 3MF it wrapped -- the same join the Monitor's twin uses.
    """
    found: list[str] = []
    if file_name and os.path.isfile(file_name):
        found.append(file_name)
    try:
        from kiln.monitor_twin import sliced_entry_for

        entry = sliced_entry_for(file_name)
    except Exception:  # noqa: BLE001
        entry = None
    if entry:
        for key in ("output", "wrapped"):
            candidate = entry.get(key)
            if isinstance(candidate, str) and os.path.isfile(candidate) and candidate not in found:
                found.append(candidate)
    return found


def job_for_start(adapter: Any, file_name: str, *, plate_number: int | None = None) -> PlateJob:
    """The :class:`PlateJob` a print of *file_name* puts on the plate.

    Geometry from the first local file that yields it; ``None`` fields when
    none does.  Never raises.
    """
    footprint: list[float] | None = None
    max_z: float | None = None
    try:
        for path in _local_files_for(file_name):
            fp, mz = geometry_of(path, plate_number=int(plate_number or 1))
            footprint = footprint if footprint is not None else fp
            max_z = max_z if max_z is not None else mz
            if footprint is not None and max_z is not None:
                break
    except Exception:  # noqa: BLE001
        logger.debug("plate job geometry lookup failed", exc_info=True)
    return PlateJob(
        file=os.path.basename(str(file_name or "")) or str(file_name),
        footprint_mm=footprint,
        max_z_mm=max_z,
        printer_id=declared_model_of(adapter),
    )


def declared_model_of(adapter: Any) -> str | None:
    """The model *adapter* was declared with, lower-cased, or ``None``.

    The one accessor every motion door reads,
    :meth:`~kiln.printers.base.PrinterAdapter.declared_printer_model`:
    ``_printer_model`` (the Bambu adapter's own copy) or the safety profile
    every config.yaml door binds with ``set_safety_profile`` -- so a Klipper
    or Marlin machine declared in config.yaml records its model at print
    start exactly as a Bambu does.  Never the global resolver: that answers
    for the default printer, and a second machine's plate must not carry
    the first machine's model.  A duck-typed object without the accessor
    is read by the same function, unbound, so there is one definition.
    """
    try:
        accessor = getattr(adapter, "declared_printer_model", None)
        if callable(accessor):
            declared = accessor()
        else:
            from kiln.printers.base import PrinterAdapter

            declared = PrinterAdapter.declared_printer_model(adapter)
    except Exception:  # noqa: BLE001 -- a model Kiln cannot read is a model it does not record
        return None
    return str(declared or "").strip().lower() or None


def mark_occupied_by_start(
    adapter: Any, file_name: str, *, plate_number: int | None = None, beside: bool = False,
) -> None:
    """A print Kiln started: the plate now holds *file_name*.  Never raises.

    *beside* is a quiet start: the part joins what the record already holds
    instead of replacing it.
    """
    try:
        mark_occupied(
            adapter, job_for_start(adapter, file_name, plate_number=plate_number),
            source="kiln_started_print", keep_previous=beside,
        )
    except Exception:  # noqa: BLE001
        logger.debug("plate-state start note failed", exc_info=True)


# ---------------------------------------------------------------------------
# Clearance and the kiln-pro planner's door
# ---------------------------------------------------------------------------


def raise_clearance_mm(station: dict[str, Any] | None) -> float | None:
    """How high the vendor's own first raise lifts the head before it travels.

    ``raise_before_travel.probe_up_mm - back_down_mm`` from the station
    record; ``None`` without a record.
    A part on the plate at least this tall stands in the path of the very
    next move (home X crosses the head's current row at this height).
    """
    try:
        r = (station or {}).get("raise_before_travel")
        if not isinstance(r, dict):
            return None
        return float(r["probe_up_mm"]) - float(r["back_down_mm"])
    except (KeyError, TypeError, ValueError):
        return None


def plan_motion_around_plate(
    state: PlateState,
    station: dict[str, Any] | None,
    *,
    action: str,
    clearance_mm: float | None,
    printer_model: str | None = None,
) -> list[dict[str, Any]] | None:
    """kiln-pro's motion planner, when it is installed; otherwise ``None``.

    The hook, not the planner.  Public Kiln refuses to move a head across a
    recorded part; kiln-pro (https://kiln3d.com) may know a path around it.
    Contract of ``kiln_pro.bridge.plan_motion_around_plate``:

    * inputs: ``record`` (this :class:`PlateState` as a dict -- status, the
      job's file / footprint / height, and the ``printer_id`` the print was
      STARTED on), ``station`` (the model's verified position record, or
      ``None``; its ``printer_id`` names the machine as it is declared NOW),
      ``action`` (``"home"`` or ``"park"``), ``clearance_mm`` (the vendor
      raise the part would have to clear);
    * output: a list of steps, each a dict with ``label``, ``you_will_see``,
      ``stops_when`` and ``gcode`` (a list of lines; ``leaves`` optional),
      run INSTEAD of Kiln's own sequence and reported as
      ``sequence_source: "kiln_pro_motion_plan"`` -- or ``None``, meaning
      no plan, and public Kiln refuses and asks the person as it would have.

    *printer_model* is the model the adapter is declared as now, resolved
    by the door that asks (the catalogue key its own motion facts came
    from).  It travels to the planner as the station record's
    ``printer_id`` -- a station of its own when the door has none -- so
    the planner plans for the connected machine and refuses when the
    record's ``job.printer_id`` names another: a printer re-declared since
    the print started must not be moved by a plan for the old model.
    Without it the planner has only the record's word.

    What comes back is a plan, not a permission: the door reads every line
    against the part (:meth:`~kiln.printers.base.PrinterAdapter._detour_around_part`)
    before anything is sent.  Anything malformed, and anything raised,
    reads as ``None``.
    """
    try:
        from kiln_pro.bridge import plan_motion_around_plate as _pro_plan
    except ImportError:
        return None
    model = str(printer_model or "").strip()
    if model:
        station = {**(station if isinstance(station, dict) else {}), "printer_id": model}
    try:
        plan = _pro_plan(record=state.to_dict(), station=station, action=action, clearance_mm=clearance_mm)
    except Exception:  # noqa: BLE001 -- a planner fault is "no plan", never a motion
        logger.debug("kiln-pro motion planner raised; refusing as without it", exc_info=True)
        return None
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
