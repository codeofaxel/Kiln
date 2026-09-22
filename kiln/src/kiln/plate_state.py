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
says the plate is empty (``plate_clear=True`` on ``home_axes``, or
``kiln plate clear``), and by a LOOK through the machine's camera
(:func:`look`, :func:`mark_from_camera`).

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

#: The block the 3D stage draws an occupied plate from, and the shape the
#: placement verdict's ``occupancy`` carries (:mod:`kiln._pro_placement_bridge`).
OCCUPANCY_KIND = "kiln.plate_occupancy.v1"

#: The code every door that starts a print refuses with while the plate
#: still holds the last one (:func:`start_refusal`).
START_NOT_YET_CODE = "PLATE_OCCUPIED_START_NOT_YET"
#: A quiet-start file planned for a plate that is not this one any more:
#: something was printed, cleared or moved since the plan was made.
PLATE_CHANGED_CODE = "PLATE_CHANGED_SINCE_PLAN"
#: What a person is told when the plate holds a part and there is no quiet
#: start for it on their plan: the two doors, one sentence.  The wrap says
#: it when it refuses to write a printer file (a file with the printer's own
#: start would home Z onto the part); the served verdict's own sentence names
#: the part as well.  Printing around what is on the plate is a kiln-pro
#: feature (https://kiln3d.com/pricing).
PRINT_AROUND_SENTENCE = (
    "Kiln won't write a printer file while the plate still holds the last print: clear the plate and say so to "
    "print again, or print around it on Kiln Pro (https://kiln3d.com/pricing)."
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
            if self.source == "human":
                who = "a person said so"
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
                "name": os.path.basename(job.file),
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
            # Whether a look wrote this, and whose eyes.  A reader that
            # weighs a camera answer differently from a person at the
            # machine needs both, and neither is derivable from `source`
            # without knowing this module's spelling.
            "from_camera": self.from_camera,
            "looked_by": self.looked_by,
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
    record and its occupancy block beside it).  The reason is physical: the
    file a print starts from carries the maker's own start sequence, which
    drives the head across the plate at a few millimetres, so a part left
    there is hit before the first layer.  A *resume* is that same job,
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
        return {
            "success": False,
            "error": {"code": PLATE_CHANGED_CODE, "message": state.plate_changed_sentence(), "retryable": False},
            "plate": state.to_dict(),
            "occupancy": state.occupancy(None),
        }
    return {
        "success": False,
        "error": {"code": START_NOT_YET_CODE, "message": state.start_refusal_sentence(), "retryable": False},
        "plate": state.to_dict(),
        "occupancy": state.occupancy(None),
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
) -> None:
    machine = machine_id(adapter)
    if not machine:
        return
    all_jobs = list(jobs) if jobs else ([job] if job is not None else [])
    try:
        store = _read_store() or {"machines": {}}
        store.setdefault("machines", {})[machine] = {
            "status": status,
            "source": source,
            "since": _now_iso(),
            "job": all_jobs[-1].to_dict() if all_jobs else None,
            "jobs": [j.to_dict() for j in all_jobs],
            "note": note,
        }
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
    """
    try:
        source = adapter.snapshot_source()
    except Exception:  # noqa: BLE001 -- a machine that cannot say has no camera Kiln can use
        return None
    return str(source) if source else None


def look(adapter: Any) -> PlateLook:
    """Fetch a frame of this machine's plate for someone to look at.

    Kiln's whole part in a look: get the frame, screen it for whether it is
    worth looking at, hand it over.  The judging is done by eyes -- the
    agent's or the person's -- and their answer comes back through
    :func:`mark_from_camera`.  Never raises, never moves a head, never
    writes the record.
    """
    camera = camera_of(adapter)
    if camera is None:
        return PlateLook(False, None, why="this printer has no camera Kiln can read, and none is registered for it")
    try:
        frame = adapter.get_snapshot()
    except Exception as exc:  # noqa: BLE001
        logger.debug("plate look: snapshot failed", exc_info=True)
        return PlateLook(False, camera, why=f"the camera did not answer ({str(exc)[:120]})")
    if not frame:
        return PlateLook(False, camera, why="the camera answered with no image")
    try:
        from kiln.snapshot_analysis import analyze_snapshot, image_dimensions

        size = image_dimensions(frame)
        # The dimensions matter here specifically: a thumbnail cannot show
        # whether a part is on the bed, and the screen skips that check
        # when it is not told the size.
        verdict = analyze_snapshot(frame, width=size[0] if size else None, height=size[1] if size else None)
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
        if seen == "occupied":
            previous = read(adapter)
            # A look cannot say WHICH part is there or how tall it is.  When
            # the record already names parts, they are kept -- the look
            # confirms them, it does not replace them with a blank.
            jobs = list(previous.jobs) if previous.occupied and job is None else ([job] if job is not None else [])
            _write_state(adapter, status="occupied", source=source, job=jobs[-1] if jobs else None, jobs=jobs, note=note)
            return "occupied"
        _write_state(adapter, status="clear", source=source, job=None, note=note)
        return "clear"
    except Exception:  # noqa: BLE001 -- bookkeeping never breaks the door that called it
        logger.debug("mark_from_camera failed", exc_info=True)
        return None


def camera_could_settle(adapter: Any) -> str | None:
    """One clause a refusal can append when a camera could answer instead.

    ``None`` when the machine has no camera, so a refusal that has nothing
    to offer does not offer it.
    """
    camera = camera_of(adapter)
    if camera is None:
        return None
    whose = "the camera you registered for it" if camera == "user_supplied" else "this printer's own camera"
    return f"or look through {whose} and tell Kiln what you see"


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
