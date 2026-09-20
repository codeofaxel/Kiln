"""Homing a printer that still has the failed part on its plate.

kiln-pro's same-bed retry prints the failed part again, shifted into a
clear zone of the *same* bed, with the failure still sitting beside it.
Its artifact deliberately emits no homing: a ``G28`` or a bed scan would
drive the head into the remnant.  So public Kiln's pre-print gate takes
that one file class on a contract and, as its last condition, asks the
firmware whether X, Y and Z are homed *right now*
(:func:`kiln.printers.print_gate.evaluate_same_bed_retry`).  A machine
power-cycled since the failure answers "no", and the retry is refused.

That refusal is right for a machine Kiln knows nothing about.  It is
wrong for one where Kiln already knows what is on the plate: the plate
record (:mod:`kiln.plate_state`) names the failed part's footprint and its
height, taken from the file the print started from.  Knowing that, homing
*around* the part is a motion, not a gamble -- and refusing it costs the
owner a print they could factually have made.

This module is that decision, and only that decision.  It owns no motion:
kiln-pro's planner produces the path, the adapter's own ``home_axes`` door
runs it (that door is the plate-aware gate -- it is never bypassed, and
``plate_clear`` is never passed, because the plate is *not* clear), and
the firmware's own read afterwards is the only proof that it worked.

What public Kiln contributes is the judgement, in this order, each step a
refusal that names itself:

1. the retry was refused ONLY because the machine is not homed;
2. the plate record says ``occupied``, with BOTH a footprint and a height
   -- an unknown plate and a part of unknown size never move a head;
3. the catalogue has a motion record for the declared model;
4. kiln-pro's planner answers with a plan for this plate;
5. every line of that plan clears the part: nothing crosses the part's row
   until the head is proven above it, nothing descends over its footprint,
   no line is one Kiln cannot read, and any Z reference lands where the
   part is not;
6. the lines the adapter actually sent are the lines Kiln validated;
7. the firmware, asked again, reports x, y and z homed.

**What this refuses, and why it matters.**  Two vendor-cited facts decide
most machines:

* ``unhomed_move_policy`` -- whether the firmware will move an axis that
  is not homed.  The lift that clears the part is exactly such a move, so
  a machine whose vendor has not said (every Bambu) cannot be promised it,
  and one that refuses outright (Klipper's default: Voron, QIDI) cannot be
  lifted at all -- though its own homing routine may still do the lifting.
* ``home_routine_travels_blind`` -- whether the firmware's own ``G28``
  moves the head sideways before Z is known.  A plan that hands the motion
  to that routine is a plan Kiln cannot see inside, so Kiln vouches for it
  only where the vendor has said it does not travel blind.

Together those leave **every Bambu refused**, with the reason: the A1,
P1S and X1C home Z by pressing the nozzle onto the plate, their own
routine travels before Z is known, and no vendor statement says an
unhomed Z move is accepted.  An honest "not on this machine" beats a plan
that could press a nozzle into the part.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

#: How far above the part's top the head must be proven before it may
#: cross the part's row.
CLEARANCE_MARGIN_MM = 2.0

#: How far outside the part's footprint a Z reference must press.  A
#: nozzle landing at the very edge of a recorded box is not "outside" it:
#: the record is derived from the file, not measured off the plate.
PRESS_MARGIN_MM = 5.0

#: The G words this decision can reason about.  Anything else -- a macro
#: name, an arc, a coordinate-system change -- is a line Kiln cannot judge
#: against the part, and an unjudgeable line refuses the whole plan.
_INERT_G = frozenset({"G4", "G21"})
_MOVE_G = frozenset({"G0", "G1"})
#: M words that move the reference Kiln is reasoning about, or drop the
#: head: disabling steppers lets a Z screw back-drive, and an offset
#: command moves the very coordinates the clearance is measured in.
_FORBIDDEN_M = frozenset({"M17", "M18", "M84", "M92", "M206", "M290", "M851"})

_WORD = re.compile(r"([A-Z])(-?\d*\.?\d+)")


# ---------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------


def _answer(
    code: str,
    reason: str,
    *,
    homed: bool = False,
    plan_id: str | None = None,
    press: list[float] | None = None,
    part: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    sent: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": homed,
        "homed": homed,
        "code": code,
        "reason": reason,
        "plan_id": plan_id,
        "press_point_mm": press,
        "part": part,
        "homed_evidence": evidence,
        "gcode_sent": list(sent) if sent is not None else None,
    }


def _audit(adapter: Any, decision: dict[str, Any]) -> dict[str, Any]:
    """One line per decision, with what it rested on.

    Read by ``kiln doctor`` and by anything replaying the recovery: a
    refusal says why, a homing says which plan ran and where its Z
    reference pressed.
    """
    machine = _machine(adapter) or "-"
    if decision.get("homed"):
        press = decision.get("press_point_mm")
        logger.info(
            "home_around_part: homed-around-part on %s: plan=%s press=%s axes=%s",
            machine,
            decision.get("plan_id") or "-",
            f"X{press[0]:g} Y{press[1]:g}" if press else "none (Z home is off the plate)",
            ",".join((decision.get("homed_evidence") or {}).get("axes") or []) or "-",
        )
    else:
        logger.info(
            "home_around_part: refused on %s (%s): %s",
            machine, decision.get("code"), decision.get("reason"),
        )
    return decision


def _machine(adapter: Any) -> str:
    try:
        from kiln.printers.print_gate import same_bed_machine_id

        return same_bed_machine_id(adapter)
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# The plan, read line by line against the part
# ---------------------------------------------------------------------------


def plan_id_of(steps: list[dict[str, Any]]) -> str:
    """A short stable name for one plan, from the lines it would send.

    The plan contract carries no identifier, and the audit line has to be
    able to say *which* plan ran.  A digest of the exact G-code answers
    that and changes the moment a single line does.
    """
    body = "\n".join(_lines_of(steps))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def _lines_of(steps: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for step in steps:
        for line in step.get("gcode") or []:
            out.append(str(line))
    return out


def _words(body: str) -> dict[str, float]:
    return {m.group(1): float(m.group(2)) for m in _WORD.finditer(body)}


def _outside(point: tuple[float, float], footprint: list[float], margin: float) -> bool:
    x0, y0, x1, y1 = footprint
    x, y = point
    return (
        x < min(x0, x1) - margin or x > max(x0, x1) + margin
        or y < min(y0, y1) - margin or y > max(y0, y1) + margin
    )


class _PlanRefused(Exception):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class _PlanReader:
    """Walks a plan's G-code, carrying what is known about the head.

    Nothing here is inferred: a height is "proven" only when Kiln can name
    the arithmetic that produced it, and everything else is unknown, which
    refuses.
    """

    def __init__(self, motion: Any, *, footprint: list[float], height: float) -> None:
        self.motion = motion
        self.footprint = footprint
        self.need = height + CLEARANCE_MARGIN_MM
        self.height = height
        self.relative = False
        self.lift = 0.0          # proven mm of upward travel since the plan began
        self.z_homed = False
        self.z_abs: float | None = None
        self.x: float | None = None
        self.y: float | None = None
        self.press: list[float] | None = None

    # -- what is known -----------------------------------------------------

    @property
    def cleared(self) -> bool:
        """True when the nozzle is provably above the part.

        Before homing, the only honest proof is a *relative* lift: the
        nozzle cannot be below the plate, so lifting by the part's height
        plus a margin clears it from wherever it happens to be.  An
        absolute Z means nothing on an axis that is not homed.
        """
        if self.lift >= self.need:
            return True
        return self.z_homed and self.z_abs is not None and self.z_abs >= self.need

    @property
    def over_clear_ground(self) -> bool:
        return (
            self.x is not None and self.y is not None
            and _outside((self.x, self.y), self.footprint, PRESS_MARGIN_MM)
        )

    # -- the press point ---------------------------------------------------

    def press_point(self) -> list[float] | None:
        """Where this machine's Z reference lands, once it is proven clear
        of the part.  ``None`` when nothing descends onto the plate."""
        m = self.motion
        if not m.z_home_descends_onto_plate:
            return None
        where = m.z_home_xy_mm
        if where is None:
            raise _PlanRefused(
                "HOME_AROUND_PART_NO_SAFE_PRESS_POINT",
                f"{m.printer_id} finds Z by {m.describe_z_home()}, and the vendor has not "
                f"said where that lands. Kiln will not press a nozzle at a spot it cannot "
                f"name while a part is on the plate. Clear the plate and home normally.",
            )
        src = m.source("z_home_xy_mm")
        if src is None or not src.settled:
            raise _PlanRefused(
                "HOME_AROUND_PART_NO_SAFE_PRESS_POINT",
                f"where {m.printer_id} presses to find Z rests on an unsettled source, and a "
                f"Z reference beside a part is not started on one.",
            )
        if not _outside((where[0], where[1]), self.footprint, PRESS_MARGIN_MM):
            raise _PlanRefused(
                "HOME_AROUND_PART_PRESS_ON_PART",
                f"{m.printer_id} finds Z by {m.describe_z_home()} -- X{where[0]:g} Y{where[1]:g} -- "
                f"and the failed part covers that spot (it runs X{self.footprint[0]:g}-{self.footprint[2]:g} "
                f"Y{self.footprint[1]:g}-{self.footprint[3]:g}). There is no way to home this machine "
                f"without pressing into the part. Clear the plate and home normally.",
            )
        self.press = [float(where[0]), float(where[1])]
        return self.press

    def _may_move_unhomed(self, line: str) -> None:
        policy = self.motion.unhomed_move_policy
        if policy in ("clamped", "unclamped"):
            return
        if policy == "refused":
            raise _PlanRefused(
                "HOME_AROUND_PART_UNHOMED_MOVE_NOT_PROMISED",
                f"{self.motion.printer_id}'s firmware refuses any move until its axes are homed, "
                f"so the lift that would clear the part ({line}) cannot run. Clear the plate and "
                f"home normally.",
            )
        raise _PlanRefused(
            "HOME_AROUND_PART_UNHOMED_MOVE_NOT_PROMISED",
            f"the vendor has not said what {self.motion.printer_id} does with a move before its "
            f"axes are homed, so the lift that would clear the part ({line}) cannot be promised. "
            f"Clear the plate and home normally, or reslice and print.",
        )

    # -- one line ----------------------------------------------------------

    def read(self, raw: str) -> None:
        line = raw.split(";")[0].strip()
        if not line:
            return
        upper = line.upper()
        word = upper.split()[0]
        body = upper[len(word):]
        if word in _INERT_G:
            return
        if word == "G90":
            self.relative = False
            return
        if word == "G91":
            self.relative = True
            return
        if word.startswith("M"):
            if word in _FORBIDDEN_M:
                raise _PlanRefused(
                    "HOME_AROUND_PART_PLAN_UNREADABLE",
                    f"the plan sends {line}, which moves or releases the very position the "
                    f"clearance over the part is measured from.",
                )
            return
        if word.startswith("G29"):
            raise _PlanRefused(
                "HOME_AROUND_PART_PLAN_PROBES_THE_BED",
                f"the plan sends {line} -- a bed probe crosses the whole plate, including the "
                f"{self.height:g} mm part standing on it.",
            )
        if word.startswith("G28"):
            self._home(line, _letters(body))
            return
        if word in _MOVE_G:
            self._move(line, _words(body))
            return
        raise _PlanRefused(
            "HOME_AROUND_PART_PLAN_UNREADABLE",
            f"the plan sends {line}, which Kiln cannot read as a motion. A line it cannot "
            f"judge against the part is a line it will not run beside one.",
        )

    def _home(self, line: str, axes: set[str]) -> None:
        if not axes:  # a bare G28: the firmware's own routine, in its own order
            if self.motion.home_routine_travels_blind is not False:
                said = (
                    "moves the head sideways before Z is known"
                    if self.motion.home_routine_travels_blind
                    else "may move the head sideways before Z is known -- the vendor has not said"
                )
                raise _PlanRefused(
                    "HOME_AROUND_PART_PLAN_DELEGATES_BLIND_HOME",
                    f"the plan hands the motion to {self.motion.printer_id}'s own homing routine "
                    f"({line}), and that routine {said}. Kiln cannot see inside it, so it will not "
                    f"run it with a {self.height:g} mm part on the plate. Clear the plate and home "
                    f"normally.",
                )
            self.press_point()
            self._forget_position(z_homed=True)
            return
        if not self.cleared:
            raise _PlanRefused(
                "HOME_AROUND_PART_PLAN_TRAVELS_LOW",
                f"the plan sends {line} before the head is proven {self.need:g} mm up -- clear of "
                f"the {self.height:g} mm part on the plate. A travel across the part's row below "
                f"its top raises no fault on most machines; it just breaks the part off.",
            )
        if "Z" in axes:
            point = self.press_point()
            self._forget_position(z_homed=True)
            if point is not None:
                self.x, self.y = point[0], point[1]
            return
        self.x = self.y = None

    def _move(self, line: str, words: dict[str, float]) -> None:
        has_xy = "X" in words or "Y" in words
        if has_xy and not self.cleared:
            raise _PlanRefused(
                "HOME_AROUND_PART_PLAN_TRAVELS_LOW",
                f"the plan sends {line} before the head is proven {self.need:g} mm up -- clear of "
                f"the {self.height:g} mm part on the plate.",
            )
        if "Z" in words:
            dz = words["Z"]
            if self.relative:
                if dz > 0:
                    if not self.z_homed:
                        self._may_move_unhomed(line)
                    self.lift += dz
                    if self.z_abs is not None:
                        self.z_abs += dz
                elif dz < 0:
                    if not self.over_clear_ground:
                        raise _PlanRefused(
                            "HOME_AROUND_PART_PLAN_DESCENDS_OVER_PART",
                            f"the plan sends {line} -- the head comes down -- and Kiln cannot show "
                            f"it is standing clear of the part's footprint when it does.",
                        )
                    self.lift = 0.0
                    if self.z_abs is not None:
                        self.z_abs += dz
            else:
                if not self.z_homed:
                    raise _PlanRefused(
                        "HOME_AROUND_PART_PLAN_UNREADABLE",
                        f"the plan sends {line} -- an absolute Z -- on an axis that is not homed, "
                        f"so Kiln cannot say how high that leaves the nozzle above the part.",
                    )
                if dz < self.need and not self.over_clear_ground:
                    raise _PlanRefused(
                        "HOME_AROUND_PART_PLAN_DESCENDS_OVER_PART",
                        f"the plan sends {line}, below the {self.height:g} mm part, without "
                        f"showing the head is clear of its footprint.",
                    )
                self.z_abs = dz
                self.lift = 0.0
        if has_xy:
            if self.relative:
                self.x = self.y = None
            else:
                if "X" in words:
                    self.x = words["X"]
                if "Y" in words:
                    self.y = words["Y"]
            if not self.cleared and not self.over_clear_ground:
                raise _PlanRefused(
                    "HOME_AROUND_PART_PLAN_DESCENDS_OVER_PART",
                    f"the plan sends {line}, which ends over the part with the nozzle below its top.",
                )

    def _forget_position(self, *, z_homed: bool) -> None:
        self.z_homed = self.z_homed or z_homed
        self.z_abs = None
        self.lift = 0.0
        self.x = self.y = None


def _letters(body: str) -> set[str]:
    return {c for c in body if c in "XYZ"}


def validate_plan(
    steps: list[dict[str, Any]], motion: Any, *, footprint: list[float], height: float,
) -> list[float] | None:
    """The press point a valid plan lands on, or raise :class:`_PlanRefused`.

    ``None`` means this machine's Z reference never touches the plate, so
    there is no press point to name.
    """
    reader = _PlanReader(motion, footprint=footprint, height=height)
    for line in _lines_of(steps):
        reader.read(line)
    return reader.press


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def evaluate_home_around_part(adapter: Any, *, refusal: dict[str, Any] | None = None) -> dict[str, Any]:
    """Judge, without moving anything, whether Kiln may home around the part.

    ``code`` is ``HOME_AROUND_PART_READY`` when it may, and a named
    refusal otherwise.  *refusal* is the verdict
    :func:`~kiln.printers.print_gate.evaluate_same_bed_retry` gave; it is
    the only refusal this answers, and any other one is left alone.
    """
    if not isinstance(refusal, dict) or refusal.get("code") != "SAME_BED_RETRY_NOT_HOMED":
        got = (refusal or {}).get("code") if isinstance(refusal, dict) else None
        return _answer(
            "HOME_AROUND_PART_NOT_THE_REFUSAL",
            "homing around the part answers exactly one refusal -- a same-bed retry whose "
            "machine reports its axes not homed" + (f"; this one is {got}" if got else "")
            + ". Nothing else is a reason to move a head beside a part.",
        )

    from kiln.plate_state import plate_occupancy

    state = plate_occupancy(adapter)
    if not state.occupied:
        return _answer(
            "HOME_AROUND_PART_PLATE_UNKNOWN",
            f"Kiln will not home around a part it has no record of: {state.describe()}. "
            f"Look at the plate, then say so (`kiln plate clear`, or plate_clear=true on "
            f"home_axes) and home normally.",
        )
    job = state.job
    if job is None or job.max_z_mm is None:
        return _answer(
            "HOME_AROUND_PART_PART_HEIGHT_UNKNOWN",
            f"the plate holds {job.file if job else 'a part'}, but Kiln could not derive how tall "
            f"it is from the file the print started from -- and the height is the whole question. "
            f"Clear the plate and home normally.",
            part=_part(job),
        )
    if not job.footprint_mm or len(job.footprint_mm) != 4:
        return _answer(
            "HOME_AROUND_PART_FOOTPRINT_UNKNOWN",
            f"the plate holds {job.file}, {job.max_z_mm:g} mm tall, but Kiln does not know where on "
            f"the plate it stands, so it cannot say a homing move misses it. Clear the plate and "
            f"home normally.",
            part=_part(job),
        )
    part = _part(job)
    height = float(job.max_z_mm)
    footprint = [float(v) for v in job.footprint_mm]

    motion = None
    try:
        motion = adapter.motion_facts()
    except Exception:  # noqa: BLE001 -- an unreadable catalogue is "Kiln does not know"
        logger.debug("motion record lookup failed", exc_info=True)
    if motion is None:
        return _answer(
            "HOME_AROUND_PART_NO_MOTION_RECORD",
            "Kiln has no motion record for this printer -- what finds Z, where it lands, and what "
            "the firmware does with an unhomed move are all looked up by model. It will not plan a "
            "path around a part on a machine it cannot describe.",
            part=part,
        )

    from kiln import plate_state as _plate_state

    try:
        steps = _plate_state.plan_motion_around_plate(
            state, None, action="home", clearance_mm=None,
        )
    except Exception:  # noqa: BLE001 -- a planner fault is "no plan", never a motion
        logger.debug("motion planner raised", exc_info=True)
        steps = None
    if not steps:
        return _answer(
            "HOME_AROUND_PART_NO_PLAN",
            f"nothing here can plan a path around {job.file}. Public Kiln refuses to move a head "
            f"across a recorded part; the motion planner that knows a way around one is Kiln Pro's "
            f"(https://kiln3d.com). Clear the plate and home normally.",
            part=part,
        )
    steps = [dict(s) for s in steps]
    try:
        press = validate_plan(steps, motion, footprint=footprint, height=height)
    except _PlanRefused as refused:
        return _answer(refused.code, refused.reason, part=part, plan_id=plan_id_of(steps))
    return _answer(
        "HOME_AROUND_PART_READY",
        f"the plan clears the {height:g} mm part"
        + (f" and finds Z at X{press[0]:g} Y{press[1]:g}, off its footprint"
           if press else " and this machine's Z home never touches the plate"),
        part=part, plan_id=plan_id_of(steps), press=press,
    )


def home_around_part(adapter: Any, *, refusal: dict[str, Any] | None = None) -> dict[str, Any]:
    """Judge, then -- only if every condition holds -- home around the part.

    The motion goes through the adapter's own ``home_axes``: that door is
    the plate-aware gate, it asks the same planner, and it is never
    bypassed.  ``plate_clear`` is never passed, because the plate is not
    clear and saying otherwise would erase the record that made this
    decision possible.

    Afterwards two things are checked, and both must hold before the
    caller may start anything: the lines the adapter reports sending are
    the lines Kiln validated, and the firmware -- asked again -- reports
    x, y and z homed.  ``homed`` is True only then.
    """
    verdict = evaluate_home_around_part(adapter, refusal=refusal)
    if verdict["code"] != "HOME_AROUND_PART_READY":
        return _audit(adapter, verdict)

    plan_id = verdict["plan_id"]
    part = verdict["part"]
    press = verdict["press_point_mm"]
    try:
        result = adapter.home_axes(axes="XYZ")
    except Exception as exc:  # noqa: BLE001 -- the door's own refusal, in its own words
        return _audit(adapter, _answer(
            "HOME_AROUND_PART_HOME_REFUSED",
            f"the printer's own homing door refused: {exc}",
            plan_id=plan_id, press=press, part=part,
        ))
    if getattr(result, "success", False) is not True:
        return _audit(adapter, _answer(
            "HOME_AROUND_PART_HOME_FAILED",
            f"the homing did not complete: {getattr(result, 'message', '') or 'no reason given'}",
            plan_id=plan_id, press=press, part=part,
        ))

    sent = (getattr(result, "details", None) or {}).get("gcode")
    sent = [str(line) for line in sent] if isinstance(sent, list) else None
    if sent is None or hashlib.sha256("\n".join(sent).encode("utf-8")).hexdigest()[:12] != plan_id:
        return _audit(adapter, _answer(
            "HOME_AROUND_PART_PLAN_NOT_RUN",
            "the printer was sent a homing sequence that is not the plan Kiln judged against the "
            "part, so Kiln cannot vouch for what just moved. Look at the plate before doing "
            "anything else.",
            plan_id=plan_id, press=press, part=part, sent=sent or [],
        ))

    try:
        axes = adapter.homed_axes_now()
    except Exception as exc:  # noqa: BLE001
        return _audit(adapter, _answer(
            "HOME_AROUND_PART_HOMED_READ_FAILED",
            f"the homing ran, but which axes are homed could not be read back from the printer "
            f"({exc}); nothing is started on a read that did not answer.",
            plan_id=plan_id, press=press, part=part, sent=sent,
        ))
    evidence = {
        "axes": None if axes is None else sorted(str(a).lower() for a in axes),
        "field": _field(adapter),
        "read_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "backend": getattr(adapter, "name", None) or type(adapter).__name__,
    }
    if axes is None:
        return _audit(adapter, _answer(
            "HOME_AROUND_PART_HOMED_UNKNOWN",
            "the homing ran, but this printer cannot report whether it is homed, and a retry does "
            "not start beside a part on a guess.",
            plan_id=plan_id, press=press, part=part, evidence=evidence, sent=sent,
        ))
    missing = sorted({"x", "y", "z"} - set(evidence["axes"]))
    if missing:
        return _audit(adapter, _answer(
            "HOME_AROUND_PART_NOT_HOMED",
            f"the homing ran and the printer still reports {','.join(evidence['axes']) or 'no axis'} "
            f"homed -- not {','.join(missing)}. Nothing was started.",
            plan_id=plan_id, press=press, part=part, evidence=evidence, sent=sent,
        ))
    return _audit(adapter, _answer(
        "HOME_AROUND_PART_HOMED",
        f"homed around the {part['height_mm']:g} mm part still on the plate"
        + (f", finding Z at X{press[0]:g} Y{press[1]:g}" if press else "")
        + "; the printer now reports x, y and z homed.",
        homed=True, plan_id=plan_id, press=press, part=part, evidence=evidence, sent=sent,
    ))


def _field(adapter: Any) -> str | None:
    try:
        return adapter.homed_axes_field()
    except Exception:  # noqa: BLE001
        return None


def _part(job: Any) -> dict[str, Any] | None:
    if job is None:
        return None
    return {
        "file": job.file,
        "height_mm": job.max_z_mm,
        "footprint_mm": list(job.footprint_mm) if job.footprint_mm else None,
    }
