"""Public Kiln -> Kiln's servers: Kiln Pro's answers for an assembly's joints.

Validating an assembly (:func:`kiln.assembly.validate_assembly`, one joint
at a time through :func:`kiln.assembly.validate_joint`, and the clearance
advice in :func:`kiln.assembly.get_clearance_recommendation`) checks every
joint against the standard design rules on every install.  Kiln Pro
(https://kiln3d.com/pricing) adds three answers, and on an install without
kiln-pro this module is where they are asked for, through the same served
tools an agent calls:

* a glued joint: which glue joins the two plastics (:data:`ADHESIVE_TOOL`);
* a screw driven into a printed part (a machine, wood, sheet-metal or
  self-tapping screw): the hole it threads into (:data:`SCREW_HOLE_TOOL`);
* a joint whose parts move: the gap this printer and material need
  (:data:`RUNNING_CLEARANCE_TOOL`).

And on every plan, a joint declared with a motion is posed through its range
by Kiln's servers (:data:`JOINT_SWEEP_TOOL`), all of a validation's moving
joints in one request; with kiln-pro on this computer the same answer is made
here.  Composing an assembly asks, in that same request, which of its parts
come off the printer as one piece (:meth:`Asking.pieces`), on every plan too.
The three Kiln Pro answers are asked only when the signed-in plan
includes Kiln Pro, and once per question in one validation (:class:`Asking`, held open by :func:`asking`):
an assembly with twenty joints asks each distinct question once, and after
the first miss asks nothing more.  The materials, the screw's size and the
printer's name go up; nothing is kept on either side.

A miss (offline, signed out, no answer, refused) is said once per
validation (:meth:`Asking.note`): the joints were checked against the
standard rules only, never read as passing Kiln Pro's checks, and a paying
caller is never pitched the plan they hold.  A sign-in that has ended is told
to sign back in, never to upgrade.  One answer can be set aside without
ending the rest (a server older than this install, which does not read the
kind of screw sent up): that is said once too (:attr:`Asking.remarks`), that
joint gets no hole, and every later question is still asked; a kind the
servers sized as another (a rivet as a machine screw) keeps its hole and is
said once the same way.  Nothing here raises.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from kiln.served_answer import (
    SESSION_EXPIRED_CODE,
    Miss,
    classify_answer,
    classify_transport_error,
    local_fault,
    sentence,
)

logger = logging.getLogger(__name__)

#: The served tool that picks the glue for two printed plastics.
ADHESIVE_TOOL = "recommend_adhesive"
#: The served tool whose ``hardware`` block is the hole a screw threads into.
SCREW_HOLE_TOOL = "recommend_joint_strategy"
#: The served tool that works out the gap for parts that move.
RUNNING_CLEARANCE_TOOL = "recommend_running_clearance"
#: The served wire that poses an assembly's moving joints through their
#: ranges and says where the parts meet.  Asked on every plan (a sweep is
#: a reading of the person's own parts), once per validation for all of
#: its moving joints; with kiln-pro on this computer it answers here
#: (:data:`JOINT_SWEEP_LOCAL`).
JOINT_SWEEP_TOOL = "sweep_assembly_joints"
JOINT_SWEEP_LOCAL = "kiln_pro.joints.wire"
#: The most bytes of part files one sweep request carries, after gzip; an
#: assembly over it is not swept, and says so.
JOINT_SWEEP_MAX_GZ_BYTES = 24 * 1024 * 1024
#: A sweep reads every pose it needs; the check waits this long for one.
_SWEEP_TIMEOUT_S: float = 120.0
#: The sweep request's flag that also asks which parts print as one piece,
#: and the key the answer comes back under.
PIECES_ARG = "pieces"
#: Said when the answer came from servers (or a kiln-pro on this computer)
#: that do not read the flag yet.
_PIECES_NOT_READ_YET = (
    "This computer's Kiln is newer than the Kiln that answered: it does not yet say "
    "which parts print as one piece, so the pieces are left unsaid and a stage shows "
    "this file the way it shows any other."
)
#: :data:`SCREW_HOLE_TOOL`'s argument naming the kind of screw (a wood
#: screw, a sheet-metal screw); sent only when the screw is not a machine
#: screw, which servers from before the argument size as before.
SCREW_KIND_ARG = "fastener_family"
#: The key the servers say the kind the hole was SIZED FOR back under
#: whenever a kind was sent (a kind the sizing does not know -- a rivet, a
#: concrete anchor -- comes back as the ``machine_screw`` it was sized as).
#: A server from before the argument refuses the argument by name
#: (:data:`_UNKNOWN_PARAMETERS`); a proxy between could drop it instead, and
#: then a machine-screw hole comes back without this key.  Either way the
#: kind was not read, and that, never the hole's shape, is how an older
#: server shows.
SCREW_KIND_READ = "fastener_family_read"
#: Said once per validation, when the servers sized the hole for another
#: kind than the one sent.  The hole is kept: a machine-screw hole is a real
#: starting point for a kind nobody has a table for, and the line says to
#: check it.
_SIZED_AS_ANOTHER_KIND = "A {asked} was sized as a {read}; check the hole."
#: How Kiln's servers refuse an argument a tool does not take: this prefix,
#: then the names, comma-separated.
_UNKNOWN_PARAMETERS = "Unknown parameters: "
#: Said once per validation when Kiln's servers did not read the kind.  It
#: ends nothing: the glue, the gaps and every other joint are still asked.
_SERVERS_KNOW_NO_SCREW_KIND = (
    "This computer's Kiln is newer than Kiln's servers' answer for a {kind}: "
    "Kiln Pro's joint checks there do not yet size that kind of screw, so "
    "its hole was not sized. Test-fit that joint before relying on it."
)
#: A validation is waiting on each answer; the standard rules stand if this
#: passes.
_CONSULT_TIMEOUT_S: float = 6.0

#: What :meth:`Asking._answer` returns when the caller's plan does not
#: include Kiln Pro: nothing was asked, and nothing is said about it here.
_BELOW_PLAN = object()

_current: contextvars.ContextVar[Asking | None] = contextvars.ContextVar(
    "kiln_assembly_asking", default=None,
)


@dataclass(frozen=True)
class ServedGap:
    """Kiln Pro's gap for parts that move, as Kiln's servers gave it."""

    recommended_mm: float
    modeled_quantity: str
    confidence: str
    binding_term: str
    expected_as_printed_mm: float
    mating: str


class Asking:
    """One validation's questions to Kiln's servers: each asked once, none
    after the first miss, and that miss said once."""

    def __init__(self) -> None:
        self._plan: str | None = None
        self._answers: dict[tuple[Any, ...], Any] = {}
        self.miss: Miss | None = None
        self._said = False
        #: Lines about one answer that end nothing (an older server's
        #: machine-screw hole for another kind of screw; a kind sized as
        #: another), each said once, in the order they arose.
        self.remarks: list[str] = []
        self._remarked = 0

    @property
    def remark(self) -> str | None:
        """The first remark, or ``None``."""
        return self.remarks[0] if self.remarks else None

    def _remark(self, line: str) -> None:
        if line not in self.remarks:
            self.remarks.append(line)

    def adhesive_line(self, material_a: str, material_b: str) -> str | None:
        """Kiln Pro's glue for a glued joint, as one line.

        ``None`` when the caller's plan does not include it (the validator
        shows its own offer); ``""`` when there is nothing to add: the
        servers named no glue for the pair, or a miss :meth:`note` says.
        """
        answer = self._answer(ADHESIVE_TOOL, material_a=material_a, material_b=material_b)
        if answer is _BELOW_PLAN:
            return None
        picks = answer.get("recommendations") if isinstance(answer, dict) else None
        first = picks[0] if isinstance(picks, list) and picks else None
        name = first.get("name") if isinstance(first, dict) else None
        if not isinstance(name, str) or not name.strip():
            return ""
        pair = f"{material_a}↔{material_b}" if material_b and material_b != material_a else material_a
        return (
            f"Glued {pair}: Kiln Pro recommends {name.strip()} for this bond — call "
            "recommend_adhesive for surface prep, the full cure schedule, and alternatives."
        )

    def screw_hole(self, fastener: Any, material: str, printer_id: str | None) -> dict[str, Any] | None:
        """The hole a screw driven into a printed part threads into, or
        ``None``.

        Asked for any screw into a printed part (or one whose surface is not
        stated): a machine screw, a wood screw, a sheet-metal or self-tapping
        screw, any other kind.  A heat-set insert threads the screw into
        metal, and a screw through to another material is a clearance hole;
        both get ``None``, as every screw does below Kiln Pro.  The screw's
        kind goes up (spelled the way the servers read it) unless it is a
        machine screw, so servers that know only machine screws answer those
        as before.  A server that did not read the kind refuses the argument
        by name, or (a proxy having dropped it) answers without saying it
        back; either way no hole is attached -- it is never asked again
        without the kind, since a machine-screw hole is the wrong hole for
        a wood screw -- and it is remarked once, ending nothing else this
        validation.  A server that read the kind and sized the hole for
        another (``fastener_family_read`` differs from what was sent: a
        rivet sized as a machine screw) keeps its hole and is remarked
        once too, so the person checks it.
        """
        size = str(getattr(fastener, "size", "") or "").strip()
        family = _screw_kind(getattr(fastener, "family", ""))
        surface = str(getattr(fastener, "surface_type", None) or "printed").strip().lower()
        if not size or surface != "printed" or "heat_set_insert" in family:
            return None
        kwargs: dict[str, Any] = {
            "fastener_size": size,
            "material": material,
            "printer_id": printer_id or "",
            "substrate": "plastic_print",
        }
        if family and "machine" not in family:
            kwargs[SCREW_KIND_ARG] = family
        answer = self._answer(SCREW_HOLE_TOOL, **kwargs)
        if not isinstance(answer, dict):
            return None
        if SCREW_KIND_ARG in kwargs and (
            _refuses_unknown(answer, SCREW_KIND_ARG) or SCREW_KIND_READ not in answer
        ):
            # Kiln's servers predate the argument: this computer's Kiln is
            # newer than their answer for this kind of screw.
            self._remark(_SERVERS_KNOW_NO_SCREW_KIND.format(kind=family.replace("_", " ")))
            return None
        block = answer.get("hardware")
        if not isinstance(block, dict):
            return None
        read = str(answer.get(SCREW_KIND_READ) or "").strip()
        if SCREW_KIND_ARG in kwargs and read and read != family:
            self._remark(_SIZED_AS_ANOTHER_KIND.format(
                asked=family.replace("_", " "), read=read.replace("_", " "),
            ))
        return dict(block)

    def running_clearance(self, mating: str, material: str, printer_id: str) -> ServedGap | None:
        """Kiln Pro's gap for parts that move on this printer in this
        material, or ``None`` (the standard minimum stands)."""
        answer = self._answer(
            RUNNING_CLEARANCE_TOOL, mating=mating, material=material, printer_id=printer_id,
        )
        if not isinstance(answer, dict):
            return None
        if answer.get("upgrade_hint"):
            # The servers answered with the standard band and an offer: they
            # read this caller's plan as below Kiln Pro.  Their words say so.
            self._missed(Miss("refused", detail=str(answer["upgrade_hint"])))
            return None
        try:
            return ServedGap(
                recommended_mm=float(answer["recommended_mm"]),
                modeled_quantity=str(answer.get("modeled_quantity") or ""),
                confidence=str(answer.get("confidence") or ""),
                binding_term=str(answer.get("binding_term") or ""),
                expected_as_printed_mm=float(answer["expected_as_printed_mm"]),
                mating=str(answer.get("mating") or mating),
            )
        except (KeyError, TypeError, ValueError):
            self._missed(Miss("unanswered", detail="no gap in the answer"))
            return None

    def joint_sweep(
        self, interface: Any, parts_by_id: dict[str, Any], interfaces: list[Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        """The sweep of *interface*'s declared motion, or why there is none.

        ``(sweep, None)`` with the served ``MotionSweep`` as a dict;
        ``(None, why)`` otherwise, *why* a clause for the person, a
        :class:`kiln.joint_record.RecordContradiction` when the record
        contradicts itself (worked out here, never asked).  Every moving
        joint of *interfaces* (up to
        :data:`kiln.joint_record.MAX_JOINTS_PER_CHECK`) is asked at once, the
        first time any of them is.
        """
        from kiln.joint_record import GEAR_NOT_SWEPT, SWEPT_KINDS, RecordContradiction, moving_group

        motion = getattr(interface, "motion", None)
        if motion is None:
            return None, None
        if motion.kind not in SWEPT_KINDS:
            return None, GEAR_NOT_SWEPT
        if interface.part_a_id == interface.part_b_id:
            return None, RecordContradiction(f"the joint names part '{interface.part_a_id}' on both sides")
        for pid in (interface.part_a_id, interface.part_b_id):
            if pid not in parts_by_id:
                return None, f"part '{pid}' is not in the assembly"
        _moving, loop = moving_group(interface, interfaces or [interface], list(parts_by_id))
        if loop is not None:
            return None, RecordContradiction(
                f"part '{interface.part_b_id}' is also joined to part '{interface.part_a_id}' "
                f"through {' -> '.join(repr(p) for p in loop)}, so it cannot move on this joint alone"
            )
        key = ("joint_sweep", id(interfaces))
        if key not in self._answers:
            self._answers[key] = _sweep_all(parts_by_id, interfaces or [interface])
        answers, miss = self._answers[key]
        if miss is not None:
            self._missed(miss)
            return None, _SWEEP_MISSES.get(miss.cause, "Kiln's servers did not sweep it")
        index = next((i for i, other in enumerate(interfaces or [interface]) if other is interface), None)
        found = answers.get(index)
        if found is None:
            return None, "Kiln's servers gave no sweep for it"
        return found

    def pieces(
        self, parts_by_id: dict[str, Any], interfaces: list[Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Which of the assembly's parts come off the printer as one piece.

        ``(answer, None)`` with ``{"pieces": [...], "kept_apart": [...]}`` as
        Kiln's servers gave it (the caller checks its shape,
        :func:`kiln.assembly_stage.accepted`); ``(None, sentence)`` when it
        is not known, the sentence saying why and what to do.  Asked on
        every plan, once, and in the same request as the sweeps of
        *interfaces* when this is asked before them.
        """
        key = ("pieces", id(interfaces))
        if key not in self._answers:
            sweeps_key = ("joint_sweep", id(interfaces))
            joints, miss, read = _assembly_answers(
                parts_by_id, interfaces, sweeps=sweeps_key not in self._answers, pieces=True,
            )
            self._answers.setdefault(sweeps_key, (joints, miss))
            self._answers[key] = (read, miss)
        read, miss = self._answers[key]
        if miss is not None:
            return None, sentence(
                miss,
                feature="piece reading",
                on_the_line="Which of this assembly's parts print as one piece is read by Kiln's servers",
                cannot="read it",
                wont="left the pieces unsaid: a stage shows this file the way it shows any other",
                then="compose again",
            )
        if isinstance(read, dict):
            return read, None
        return None, (f"The pieces are left unsaid: {read}." if isinstance(read, str) else _PIECES_NOT_READ_YET)

    def note(self) -> str | None:
        """The sentence for this validation's miss, then each of its
        :attr:`remarks` in turn, each the first time it is asked for;
        ``None`` after that, and when there is nothing to say."""
        if self.miss is not None and not self._said:
            self._said = True
            return sentence(
                self.miss,
                feature="joint checks",
                on_the_line="Kiln Pro's joint checks come from Kiln's servers",
                cannot="run them",
                wont=(
                    "checked against the standard rules only, without Kiln Pro's glue "
                    "pick, screw-hole sizes or gaps for moving parts"
                ),
                safe_remedy="Test-fit the parts before relying on them",
                then="check again",
            )
        if self._remarked < len(self.remarks):
            self._remarked += 1
            return self.remarks[self._remarked - 1]
        return None

    def _answer(self, tool: str, **kwargs: Any) -> Any:
        """The servers' answer to *tool* with *kwargs*, asked at most once;
        :data:`_BELOW_PLAN`, or ``None`` after a miss."""
        key = (tool, *sorted((k, str(v)) for k, v in kwargs.items()))
        if key in self._answers:
            return self._answers[key]
        if self.miss is not None:
            return None
        plan = self._plan_here()
        if plan not in ("pro", "below"):
            # A sign-in that has ended (its own words): the account may well
            # hold the plan, so sign back in, never upgrade.
            self._missed(Miss("signed_out", SESSION_EXPIRED_CODE, plan))
            return None
        if plan == "below":
            return _BELOW_PLAN
        answer, miss = _ask(tool, kwargs)
        if miss is not None:
            self._missed(miss)
            return None
        self._answers[key] = answer
        return answer

    def _plan_here(self) -> str:
        """``pro`` when the signed-in plan includes Kiln Pro, ``below`` when
        it does not, else the sentence for a sign-in that has ended.  Read
        once per validation."""
        if self._plan is None:
            self._plan = "pro" if _plan_includes_pro() else (_lapsed_signin() or "below")
        return self._plan

    def _missed(self, miss: Miss) -> None:
        if self.miss is None:
            self.miss = miss
            logger.debug("assembly joint checks not served (%s)", miss.cause)


@contextlib.contextmanager
def asking() -> Iterator[Asking]:
    """Hold one validation's questions open: the session already open, else
    a new one for the length of the block."""
    held = _current.get()
    if held is not None:
        yield held
        return
    session = Asking()
    token = _current.set(session)
    try:
        yield session
    finally:
        _current.reset(token)


def current() -> Asking:
    """The open validation's questions, or a fresh set for a lone one."""
    return _current.get() or Asking()


def _screw_kind(family: Any) -> str:
    """A fastener's family spelled the way Kiln's servers read it: lowercase,
    spaces and hyphens as underscores (``"Machine Screw"`` and
    ``"machine-screw"`` are ``"machine_screw"``)."""
    return str(family or "").strip().lower().replace("-", "_").replace(" ", "_")


def _plan_includes_pro() -> bool:
    """The signed-in account's plan covers Kiln Pro.  Anything unreadable is
    the free plan."""
    try:
        from kiln.licensing import LicenseTier, check_tier

        covered, _why = check_tier(LicenseTier.PRO)
        return bool(covered)
    except Exception:  # noqa: BLE001
        return False


def _lapsed_signin() -> str:
    try:
        from kiln.auth_session import lapsed_signin_message

        return lapsed_signin_message()
    except Exception:  # noqa: BLE001
        return ""


def _ask(tool: str, kwargs: dict[str, Any]) -> tuple[dict[str, Any] | None, Miss | None]:
    """``(answer, None)`` from Kiln's servers, or ``(None, miss)``."""
    if sys.modules.get("kiln_pro") is not None:
        # kiln-pro is in this process (Kiln's servers themselves): the answer
        # is made here, and a piece missing from it is not asked of itself.
        return None, Miss("unanswered", detail="not asked of this process")
    try:
        from kiln.server import _pro_api_call
    except Exception:  # noqa: BLE001
        return None, Miss("unanswered", detail="this install could not open its connection")
    try:
        answer = _pro_api_call(
            tool,
            _timeout=_CONSULT_TIMEOUT_S,
            # The person asked to check their joints; Kiln Pro's answers for
            # them are fetched on its own.
            _asked_by_user=False,
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001 -- the network is a miss, never a traceback
        return None, classify_transport_error(exc)
    if not isinstance(answer, dict):
        return None, Miss("unanswered", detail="no answer")
    if SCREW_KIND_ARG in kwargs and _refuses_unknown(answer, SCREW_KIND_ARG):
        # The servers do not take the screw's kind: an older server, not a
        # miss.  :meth:`Asking.screw_hole` reads the refusal and says so.
        return answer, None
    miss = classify_answer(answer)
    if miss is not None:
        return None, miss
    return answer, None


#: Why a joint has no sweep, by the cause of the miss (the validation's own
#: note says the full sentence once).
_SWEEP_MISSES = {
    "offline": "this computer could not reach Kiln's servers, which sweep it",
    "signed_out": "Kiln is signed out, and Kiln's servers sweep it for a signed-in account",
    "unanswered": "Kiln's servers did not answer",
    "refused": "Kiln's servers did not sweep it",
}


def _moving_indices(interfaces: list[Any]) -> list[int]:
    """Where in *interfaces* the joints a check sweeps sit: the first
    :data:`kiln.joint_record.MAX_JOINTS_PER_CHECK` that move in a kind that
    is swept."""
    from kiln.joint_record import MAX_JOINTS_PER_CHECK, SWEPT_KINDS

    return [
        i for i, iface in enumerate(interfaces)
        if getattr(getattr(iface, "motion", None), "kind", None) in SWEPT_KINDS
    ][:MAX_JOINTS_PER_CHECK]


def _mesh_bytes(path: Any) -> bytes | str:
    """A part's geometry as a binary STL of the triangles Kiln reads from its
    file, or why it cannot be sent.  Only the parsed mesh ever leaves: a file
    that is not a mesh Kiln reads (by its kind, then by parsing it) is refused
    here, whatever path the record names."""
    import struct

    import numpy as np

    from kiln.generation.validation import _OTHER_FORMATS, _parse_stl

    if path.suffix.lower() not in {".stl", *_OTHER_FORMATS}:
        return f"'{path.name}' is not a mesh or CAD file Kiln reads"
    errors: list[str] = []
    triangles, _vertices = _parse_stl(path, errors)
    if errors or triangles is None or len(triangles) == 0:
        return f"'{path.name}' could not be read as a mesh"
    corners = np.asarray(triangles, dtype="<f4").reshape(-1, 9)
    rows = np.zeros((len(corners), 12), dtype="<f4")
    rows[:, 3:] = corners  # each row: a zero normal, then the three corners
    packed = np.zeros(len(corners), dtype=[("row", "<f4", (12,)), ("attr", "<u2")])
    packed["row"] = rows
    return b"\0" * 80 + struct.pack("<I", len(corners)) + packed.tobytes()


def _sweep_request(
    parts_by_id: dict[str, Any], interfaces: list[Any], *, here: bool, sweeps: bool = True, pieces: bool = False,
) -> dict[str, Any] | str:
    """The ``joint_sweep_request/1`` document for every moving joint (none
    when *sweeps* is off), asking for the pieces too when *pieces* is on, or
    the clause saying why none could be sent.  *here*: kiln-pro answers on
    this computer, so files go by path; otherwise each part's parsed mesh
    travels as a binary STL, gzipped (never the file as it is on disk)."""
    import base64
    import gzip
    from pathlib import Path

    chosen = _moving_indices(interfaces) if sweeps else []
    parts = []
    carried = 0
    for pid, part in parts_by_id.items():
        path = Path(str(part.file_path))
        if not path.is_file():
            return f"there is no file at {part.file_path} for part '{pid}'"
        if here:
            sent: dict[str, Any] = {"path": str(path)}
        else:
            mesh = _mesh_bytes(path)
            if isinstance(mesh, str):
                return f"part '{pid}' was not sent: {mesh}"
            body = gzip.compress(mesh)
            carried += len(body)
            if carried > JOINT_SWEEP_MAX_GZ_BYTES:
                return (
                    "the parts' files are too large to send for a sweep "
                    f"(more than {JOINT_SWEEP_MAX_GZ_BYTES // (1024 * 1024)} MB compressed)"
                )
            sent = {"name": f"part-{len(parts)}.stl", "gz_b64": base64.b64encode(body).decode("ascii")}
        parts.append({
            "part_id": pid,
            "file": sent,
            "position_mm": [float(c) for c in getattr(part, "position_mm", (0.0, 0.0, 0.0))],
            "rotation_deg": [float(c) for c in getattr(part, "rotation_deg", (0.0, 0.0, 0.0))],
            "material": str(getattr(part, "material", "") or ""),
        })
    request: dict[str, Any] = {
        "schema": "joint_sweep_request/1",
        "parts": parts,
        "interfaces": [iface.to_dict() for iface in interfaces],
        "sweep": chosen,
    }
    if pieces:
        request[PIECES_ARG] = True
    return request


def _sweep_all(
    parts_by_id: dict[str, Any], interfaces: list[Any],
) -> tuple[dict[int, tuple[dict[str, Any] | None, str | None]], Miss | None]:
    """Every moving joint's sweep, by its place in *interfaces*: from
    kiln-pro when it is on this computer, else from Kiln's servers."""
    joints, miss, _pieces = _assembly_answers(parts_by_id, interfaces)
    return joints, miss


def _assembly_answers(
    parts_by_id: dict[str, Any], interfaces: list[Any], *, sweeps: bool = True, pieces: bool = False,
) -> tuple[dict[int, tuple[dict[str, Any] | None, str | None]], Miss | None, dict[str, Any] | str | None]:
    """One request for an assembly's parts: every moving joint's sweep by its
    place in *interfaces* (*sweeps*), and which parts print as one piece
    (*pieces*).  From kiln-pro when it is on this computer, else from Kiln's
    servers.  ``(joints, miss, pieces)``: *pieces* is the answer as given, a
    clause saying why nothing was asked, or ``None`` when it was not asked
    or the answer did not carry it."""
    try:
        from kiln_pro.joints.wire import answer as local_answer  # noqa: F401
    except ImportError:
        here = False
    else:
        here = True
    request = _sweep_request(parts_by_id, interfaces, here=here, sweeps=sweeps, pieces=pieces)
    if isinstance(request, str):
        # Nothing was asked: the reason is this computer's (a file missing,
        # too much to send), so each joint says it, and no miss is noted.
        asked = _moving_indices(interfaces) if sweeps else []
        return {i: (None, request) for i in asked}, None, request if pieces else None
    reply, miss = _sent(request, here=here)
    if miss is None and pieces and _refuses_unknown(reply, PIECES_ARG):
        # Servers from before the flag: the sweeps are asked again without
        # it, and the pieces are said to be unread.
        request.pop(PIECES_ARG)
        if not sweeps:
            return {}, None, None
        reply, miss = _sent(request, here=here)
    if miss is None:
        miss = classify_answer(reply) if isinstance(reply, dict) else Miss("unanswered", detail="no answer")
    if miss is not None:
        return {}, miss, None
    if not isinstance(reply, dict) or reply.get("schema") != "joint_sweep_answer/1":
        return {}, Miss("unanswered", detail="an answer that is not a sweep"), None
    out: dict[int, tuple[dict[str, Any] | None, str | None]] = {}
    for joint in reply.get("joints") or []:
        if not isinstance(joint, dict) or not isinstance(joint.get("index"), int):
            continue
        sweep = joint.get("sweep") if isinstance(joint.get("sweep"), dict) else None
        why = joint.get("why_not") if isinstance(joint.get("why_not"), str) else None
        out[joint["index"]] = (sweep, None if sweep is not None else (why or "Kiln's servers gave no sweep for it"))
    if PIECES_ARG not in request:
        return out, None, None
    read = reply.get(PIECES_ARG)
    if isinstance(read, dict):
        return out, None, read
    why = reply.get("pieces_why_not")  # read as data: a clause, kept short
    return out, None, why.strip().rstrip(".")[:300] if isinstance(why, str) and why.strip() else None


def _sent(request: dict[str, Any], *, here: bool) -> tuple[Any, Miss | None]:
    """*request* answered by kiln-pro on this computer (*here*) or by Kiln's
    servers: ``(reply, None)``, or ``(None, miss)`` when no reply came."""
    if here:
        try:
            from kiln_pro.joints.wire import answer as local_answer

            return local_answer(request, local_files=True), None
        except Exception as exc:  # noqa: BLE001 -- a validation never fails for want of a sweep
            return None, local_fault(exc)
    try:
        from kiln.server import _pro_api_call
    except Exception:  # noqa: BLE001
        return None, Miss("unanswered", detail="this install could not open its connection")
    try:
        # The person asked for the check, and the parts' files are their
        # own: they go as given (the wire guard cannot read inside a
        # compressed file, and walking megabytes of one costs seconds).
        return _pro_api_call(JOINT_SWEEP_TOOL, _timeout=_SWEEP_TIMEOUT_S, _asked_by_user=True, **request), None
    except Exception as exc:  # noqa: BLE001 -- the network is a miss, never a traceback
        return None, classify_transport_error(exc)


def _refuses_unknown(answer: Any, name: str) -> bool:
    """*answer* is Kiln's servers refusing *name* as an argument the tool
    does not take (HTTP 400, ``{"success": false, "error": "Unknown
    parameters: ..."}``, passed through the served door as the server wrote
    it)."""
    if not isinstance(answer, dict) or answer.get("success") is not False:
        return False
    error = answer.get("error")
    if not isinstance(error, str) or not error.startswith(_UNKNOWN_PARAMETERS):
        return False
    return name in [n.strip() for n in error[len(_UNKNOWN_PARAMETERS):].split(",")]


__all__ = [
    "ADHESIVE_TOOL",
    "JOINT_SWEEP_TOOL",
    "PIECES_ARG",
    "RUNNING_CLEARANCE_TOOL",
    "SCREW_HOLE_TOOL",
    "SCREW_KIND_ARG",
    "SCREW_KIND_READ",
    "Asking",
    "ServedGap",
    "asking",
    "current",
]
