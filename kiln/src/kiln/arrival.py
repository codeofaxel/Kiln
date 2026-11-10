"""A model that arrives from outside Kiln: where it came from, kept beside it.

Geometry nobody at Kiln drew comes in two ways: a cloud generator's
download (Tripo, Meshy, Stability, Gemini) and a marketplace download.
A bare file path handed back from either is not enough: the stage never
opens on it, and nothing remembers where it came from, which is the
fact a person needs to credit a designer, keep to a license, or check
the size (a generator's download need not carry real units).

This module is that memory.  The ENGINES write it: every provider's
``download_result`` and every marketplace's ``download_file`` leave a
note through their base classes, beside the usage counting already
there, so the tools, the CLI and the pipelines all leave the same note
without each remembering to.  The doors read it:

* :func:`record` writes ``<file>.arrival.json`` beside the file, bound
  to the file's bytes, so a different file later saved under the same
  name is never credited to the wrong source;
* :func:`read` gives back the :class:`Arrival` for a file, or ``None``;
* :func:`announce` puts the line a person reads (``came_from``) on a
  door's result, with the size check a shape nobody measured needs;
* :func:`record_pose` / :func:`read_pose` keep, in the same note, which way
  up the file arrived and what Kiln did about it
  (:mod:`kiln.arrival_pose`), and :func:`announce` says that too (``pose``).

A note beside the file rather than a store under ``~/.kiln``: it
describes one file and travels with it, the same convention as the
decoration-face and design-history notes Kiln already keeps beside a
mesh.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from typing import Any

logger = logging.getLogger(__name__)

#: Bumped on a breaking change to the note's layout; an older note reads
#: as no note, never as a wrong one.
SCHEMA_VERSION = 1

#: Appended to the file's own path: ``benchy.stl`` -> ``benchy.stl.arrival.json``.
NOTE_SUFFIX = ".arrival.json"

#: A prompt or a listing title is quoted in the line up to this many
#: characters.  The whole text stays in the note.
_QUOTE_CHARS = 80

GENERATED = "generated"
DOWNLOADED = "downloaded"

#: The stage payload's name for :func:`stage_block`.  The viewer reads only
#: a kind it knows, so a later layout opts in rather than half-rendering.
STAGE_KIND = "kiln.arrival.v1"


@dataclass(frozen=True)
class Arrival:
    """Where one file came from.

    ``real_size`` says whether the numbers in the file are millimetres a
    person chose: ``True`` for a generator that draws in millimetres,
    ``False`` for one that was asked for a shape and picked its own scale,
    ``None`` for a download, whose units only its size can speak to.
    """

    kind: str
    by: str
    name: str = ""
    creator: str = ""
    license: str = ""
    url: str = ""
    prompt: str = ""
    job_id: str = ""
    model_id: str = ""
    file_id: str = ""
    real_size: bool | None = None

    def line(self) -> str:
        """The one sentence a person reads about where this came from."""
        if self.kind == DOWNLOADED and not self._listing_read():
            return (
                f"Downloaded from {self.by}. Its listing was not read, so who "
                "designed it and its license are not recorded."
            )
        said = self.caption()
        if self.kind == DOWNLOADED and self.url:
            said += f" ({self.url})"
        return said + "."

    def caption(self) -> str:
        """The same, as the stage labels it: no link it cannot follow, no
        closing period, like every other chip on the stage."""
        if self.kind == GENERATED:
            asked = f' from "{_quote(self.prompt)}"' if self.prompt else ""
            return f"Generated with {self.by}{asked}"
        if not self._listing_read():
            return f"Downloaded from {self.by} · designer and license not recorded"
        said = f"Downloaded from {self.by}"
        if self.name:
            said += f': "{_quote(self.name)}"'
        if self.creator:
            said += f" by {self.creator}"
        return said + (f", licensed {self.license}" if self.license else ", no license stated on the listing")

    def _listing_read(self) -> bool:
        return bool(self.name or self.creator or self.url)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _quote(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= _QUOTE_CHARS:
        return text
    cut = text[:_QUOTE_CHARS].rsplit(" ", 1)[0] or text[:_QUOTE_CHARS]
    return cut.rstrip(" ,.;:") + "…"


def note_path_for(file_path: str) -> str:
    """Where the note for *file_path* lives."""
    return file_path + NOTE_SUFFIX


def _identity(file_path: str) -> str | None:
    """The file's content hash, as the print gate identifies a file."""
    from kiln.preview_gate import hash_file

    digest = hash_file(file_path)
    return None if digest.startswith("NO_FILE:") else digest


def record(file_path: str, arrival: Arrival) -> str | None:
    """Write the note for *file_path*.  Returns its path, or ``None`` when it
    could not be written (no such file, a read-only folder).  Never raises:
    a download is never lost for the sake of its note."""
    try:
        identity = _identity(file_path)
        if identity is None:
            return None
        note = note_path_for(file_path)
        body = {"schema": SCHEMA_VERSION, "file": identity, "arrival": arrival.to_dict()}
        held = _body(file_path)
        if held and isinstance(held.get("pose"), dict):
            body["pose"] = held["pose"]
        # A note cut short by a crash fails to parse, and reads as no note.
        with open(note, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=2)
        return note
    except Exception:  # noqa: BLE001 — a download is never lost for its note
        logger.debug("arrival note not written for %s", file_path, exc_info=True)
        return None


def _body(file_path: str) -> dict[str, Any] | None:
    """The note's body, or ``None`` when there is none, it is unreadable, or
    it describes different bytes than the file now holds."""
    try:
        with open(note_path_for(file_path), encoding="utf-8") as fh:
            body = json.load(fh)
        if not isinstance(body, dict) or body.get("schema") != SCHEMA_VERSION:
            return None
        if body.get("file") != _identity(file_path):
            return None
        return body
    except (OSError, ValueError, TypeError):
        return None


def read(file_path: str) -> Arrival | None:
    """The note for *file_path*, or ``None`` when there is none, it is
    unreadable, or it describes different bytes than the file now holds."""
    body = _body(file_path)
    if body is None:
        return None
    try:
        known = {f.name for f in fields(Arrival)}
        values = {k: v for k, v in (body.get("arrival") or {}).items() if k in known}
        return Arrival(**values) if values.get("kind") and values.get("by") else None
    except (ValueError, TypeError):
        return None


#: What a saved goal keeps about where one of its versions came from: the
#: arrival, never where the file sits, because the goal outlives the file
#: being on any one computer.
GOAL_FIELDS = ("kind", "by", "name", "creator", "license", "url", "model_id", "job_id")


def goal_record(file_path: str) -> dict[str, str]:
    """The :data:`GOAL_FIELDS` of the note beside *file_path*, empty ones
    dropped; ``{}`` when there is no note (Kiln made the file here)."""
    noted = read(file_path)
    if noted is None:
        return {}
    return {name: str(getattr(noted, name)) for name in GOAL_FIELDS if getattr(noted, name, "")}


def record_pose(file_path: str, pose: dict[str, Any]) -> str | None:
    """Keep *pose* -- which way up the file arrived and what Kiln did about
    it (:func:`kiln.arrival_pose.stand_up`'s record) -- in the note beside
    *file_path*, next to where it came from.  A note for other bytes is
    replaced; the arrival it held, if any, is the door's to carry over.
    Returns the note's path, or ``None`` when it could not be written.
    Never raises."""
    try:
        identity = _identity(file_path)
        if identity is None:
            return None
        body = _body(file_path) or {"schema": SCHEMA_VERSION, "file": identity}
        body["pose"] = dict(pose)
        note = note_path_for(file_path)
        with open(note, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=2)
        return note
    except Exception:  # noqa: BLE001 -- a part stands without its note
        logger.debug("pose note not written for %s", file_path, exc_info=True)
        return None


def read_pose(file_path: str) -> dict[str, Any] | None:
    """The pose record kept beside *file_path* (see :func:`record_pose`), or
    ``None``: no note, or a note about other bytes."""
    body = _body(file_path)
    pose = body.get("pose") if body else None
    return dict(pose) if isinstance(pose, dict) and pose.get("status") else None


@dataclass(frozen=True)
class Held:
    """The note beside a file, read before the file is rewritten (:func:`hold`)."""

    body: dict[str, Any] | None

    def follow(self, to_path: str, *, size_chosen: bool = False) -> str | None:
        """Write the held note beside *to_path*, bound to its bytes now.

        Every key of the note is kept (where it came from, the pose).  With
        *size_chosen* the arrival says ``real_size`` is true: the numbers
        are millimeters someone chose.  A fit to a bed is not a choice of
        size and leaves ``real_size`` as it was.  Returns the note's path,
        or ``None`` when there was no note or it could not be written.
        Never raises.
        """
        if self.body is None:
            return None
        try:
            identity = _identity(to_path)
            if identity is None:
                return None
            body = dict(_body(to_path) or {})
            body.update({k: v for k, v in self.body.items() if k not in ("schema", "file")})
            if size_chosen and isinstance(body.get("arrival"), dict):
                body["arrival"] = {**body["arrival"], "real_size": True}
            body["schema"] = SCHEMA_VERSION
            body["file"] = identity
            note = note_path_for(to_path)
            with open(note, "w", encoding="utf-8") as fh:
                json.dump(body, fh, indent=2)
            return note
        except Exception:  # noqa: BLE001 -- a resized model stands without its note
            logger.debug("arrival note not carried to %s", to_path, exc_info=True)
            return None


def hold(file_path: str) -> Held:
    """The note beside *file_path* as it is now, to :meth:`Held.follow` the
    model to the bytes a rewrite leaves (a resize in place, a scaled copy)."""
    return Held(_body(file_path))


def carry(from_path: str, to_path: str) -> None:
    """Give *to_path* the note of *from_path*: the same model in another
    file (an OBJ or GLB written out as STL) came from the same place."""
    hold(from_path).follow(to_path)


def stage_block(file_path: str) -> dict[str, Any] | None:
    """What the 3D stage says about where *file_path* came from, or ``None``.

    The person looking at the stage reads it there, not only the agent:
    a generator's shape drawn on a real bed at 1 mm across is a speck
    nobody can explain unless the stage says the numbers were never
    millimetres (``real_size`` false).
    """
    arrival = read(file_path)
    if arrival is None:
        return None
    return {"kind": STAGE_KIND, "came_from": arrival.caption(), "real_size": arrival.real_size}


def note_generation(provider: Any, result: Any) -> None:
    """The note a generation provider's download leaves (see
    ``GenerationProvider.__init_subclass__``).  Never raises."""
    try:
        if not provider.drawn_elsewhere:
            return
        record(
            result.local_path,
            Arrival(
                kind=GENERATED,
                by=provider.display_name,
                prompt=result.prompt or "",
                job_id=result.job_id or "",
                real_size=provider.sets_real_size,
            ),
        )
    except Exception:  # noqa: BLE001 — the download stands without its note
        logger.debug("generation arrival note skipped", exc_info=True)


def note_download(by: str, file_path: str, file_id: Any) -> None:
    """The note a marketplace download leaves (see
    ``MarketplaceAdapter.__init_subclass__``).  The listing is not known
    here; a door that read it records the fuller note over this one."""
    record(file_path, Arrival(kind=DOWNLOADED, by=by, file_id=str(file_id)))


def extent(bounding_box: dict[str, Any] | None) -> tuple[float, float, float] | None:
    """A mesh check's bounding box as ``(x, y, z)`` extents, or ``None``."""
    bb = bounding_box
    if not bb:
        return None
    return (
        bb.get("x_max", 0) - bb.get("x_min", 0),
        bb.get("y_max", 0) - bb.get("y_min", 0),
        bb.get("z_max", 0) - bb.get("z_min", 0),
    )


def measure(file_path: str) -> tuple[dict[str, Any], dict[str, Any] | None, tuple[float, float, float] | None]:
    """``(validation, dimensions, size)`` for a mesh that just arrived: the
    mesh check, its extent in the file's own numbers, and that extent as
    the tuple :func:`announce` takes."""
    from kiln.generation import validate_mesh

    val = validate_mesh(file_path)
    size = extent(val.bounding_box)
    if size is None:
        return val.to_dict(), None, None
    w, d, h = size
    dimensions = {
        "width_mm": round(w, 2),
        "depth_mm": round(d, 2),
        "height_mm": round(h, 2),
        "summary": f"{w:.1f} x {d:.1f} x {h:.1f} mm",
    }
    return val.to_dict(), dimensions, size


def size_check(
    arrival: Arrival,
    size: tuple[float, float, float],
    *,
    arrived_as: str = "",
    read_scale: float = 1.0,
) -> str:
    """What to settle about the size before printing, or ``""``.

    A generator that was never given a size gets the one sentence that is
    always true of it.  Everything else, a generator told to draw in
    millimetres included, gets the units reading a download gets: being
    told to draw in millimetres is not proof that it did, and the reading
    is silent for any printable size.  A file that arrived as glTF
    (*arrived_as*, its original format) is told how its numbers were read:
    *read_scale* is what Kiln multiplied them by
    (:func:`kiln.mesh_frame.gltf_unit_scale`, 1000 for a file read in
    metres), and *size* is the result (:func:`_gltf_reading`).
    """
    if arrival.real_size is False:
        across = " x ".join(_as_said(v) for v in size)
        return (
            f"{arrival.by} was asked for a shape, not a size: this arrived "
            f"{across} across in no real unit, and a slicer reads those numbers "
            f"as millimetres. Ask the person how big it should be, then set it before printing: {RESIZE_CALL}, "
            "or target_height_mm=<the height they want> once it stands the way it prints."
        )
    if arrived_as.lower().lstrip(".") in _GLTF_TYPES:
        said = _gltf_reading(max(size), read_scale)
        if said:
            return said
    from kiln.generation.validation import unit_verdict

    return unit_verdict(max(size)).describe_unchanged()


def _read_scale_of(file_path: str) -> float:
    """What :func:`kiln.mesh_frame.load_mesh` multiplies the glTF at
    *file_path* by, read from the file; ``1.0`` for any other file, or one
    that cannot be read (the size sentence then falls back to the general
    reading)."""
    if os.path.splitext(file_path)[1].lower().lstrip(".") not in _GLTF_TYPES:
        return 1.0
    from kiln.mesh_frame import gltf_read_scale

    try:
        return float(gltf_read_scale(file_path))
    except Exception:  # noqa: BLE001 -- an unreadable file gets the general reading
        return 1.0


#: The call that gives a model the size the person asks for, up or down.
RESIZE_CALL = "rescale_model(file_path, longest_side_mm=<the size the person wants>)"

#: The code a print-service quote answers with when nobody chose the size.
SIZE_NOT_CHOSEN = "SIZE_NOT_CHOSEN"


def size_not_chosen(file_path: str) -> dict[str, Any] | None:
    """Why *file_path* may not be priced by a print service, or ``None``.

    A file whose note says ``real_size`` is false holds numbers in no real
    unit, and a print service prices them as millimeters, so every door that
    asks a print service for a price asks this first, before anything is
    uploaded.  The answer carries ``message``, ``code``
    (:data:`SIZE_NOT_CHOSEN`), ``extents`` (the measured ``[x, y, z]`` in the
    file's own numbers, absent when the file cannot be measured) and
    ``next_call``.  A file with no note, or with a size that is real or
    unknown, gives ``None``.
    """
    arrival = read(file_path)
    if arrival is None or arrival.real_size is not False:
        return None
    size = None
    try:
        size = measure(file_path)[2]
    except Exception:  # noqa: BLE001 -- the refusal stands without the numbers
        logger.debug("could not measure %s", file_path, exc_info=True)
    across = f" (it measures {' x '.join(_as_said(v) for v in size)} across in no real unit)" if size else ""
    refusal: dict[str, Any] = {
        "code": SIZE_NOT_CHOSEN,
        "message": (
            f"This model came from {arrival.by} with no real size{across}. A print service would price "
            "those numbers as millimeters, so nothing was quoted. Ask the person how big it should be, "
            f"set it with {RESIZE_CALL}, then quote again."
        ),
        "next_call": {
            "tool": "rescale_model",
            "args": {"file_path": file_path, "longest_side_mm": "<the size the person wants, in mm>"},
        },
    }
    if size:
        refusal["extents"] = [round(v, 4) for v in size]
    return refusal


def priced_size(file_path: str) -> tuple[dict[str, Any], str] | None:
    """The size a print service prices *file_path* at: the dimensions in
    millimeters and the line the person is told, or ``None`` when the file
    cannot be measured.  Never raises."""
    try:
        dimensions = measure(file_path)[1]
    except Exception:  # noqa: BLE001 -- a quote never fails for its size line
        logger.debug("could not measure %s", file_path, exc_info=True)
        return None
    if not dimensions:
        return None
    return dimensions, f"Priced at the size in the file: {dimensions['summary']} (width x depth x height)."


#: glTF's file types.  The glTF 2.0 specification (Coordinate System and
#: Units): "The units for all linear distances are meters."
_GLTF_TYPES = frozenset({"glb", "gltf"})


def _as_said(value: float) -> str:
    """A size as a sentence says it: three significant figures, and whole
    numbers from a thousand up rather than ``1e+03``."""
    return f"{value:.0f}" if abs(value) >= 1000 else f"{value:.3g}"


def _gltf_reading(largest_mm: float, read_scale: float) -> str:
    """The size sentence for a glTF file: how Kiln read its numbers, or ``""``
    to fall back to the general reading.

    A glTF's numbers are metres by its own specification, so a 50 mm part
    saved to it arrives as 0.05, and Kiln reads such a file in metres
    (:func:`kiln.mesh_frame.gltf_unit_scale`): *read_scale* says it did, and
    *largest_mm* is the size that made.  A glTF read as written gets a
    sentence only when it is under the size Kiln speaks up about
    (``_UNIT_NOTICE_BELOW_MM``) and would be bigger than any printer as
    metres: it is told both readings, since only the person knows which the
    file meant.  At a printable size as written it is read in silence, like
    any other file of a printable size.
    """
    from kiln.generation.validation import _PRINTABLE_MAX_MM, _UNIT_NOTICE_BELOW_MM
    from kiln.mesh_frame import GLTF_METRES_TO_MM

    if largest_mm <= 0:
        return ""
    if read_scale == GLTF_METRES_TO_MM:
        return (
            f"This is a glTF file, and glTF measures in metres: Kiln read it that way, "
            f"{_as_said(largest_mm)} mm at its largest."
        )
    in_metres = largest_mm * GLTF_METRES_TO_MM
    if largest_mm < _UNIT_NOTICE_BELOW_MM and in_metres > _PRINTABLE_MAX_MM:
        return (
            f"This is a glTF file, and glTF measures in metres, but read that way it would be "
            f"{_as_said(in_metres)} mm at its largest, bigger than any printer in Kiln's catalog, so Kiln "
            f"read it as millimetres: {_as_said(largest_mm)} mm. If it is in metres, ask the person how big "
            f"it should be and set it with {RESIZE_CALL}, or print it in sections with split_mesh_to_fit."
        )
    return ""


def announce(
    result: dict[str, Any],
    file_path: str,
    *,
    size: tuple[float, float, float] | None = None,
    arrived_as: str | None = None,
    read_scale: float | None = None,
) -> dict[str, Any]:
    """Put where *file_path* came from on a door's *result*, in place.

    Adds ``came_from`` (the line) and ``arrival`` (the note), and
    ``size_check`` when *size* — the file's measured extent — needs a
    word before printing.  When the numbers are not millimetres, the
    result's ``dimensions`` summary stops saying they are.  A file with
    no note adds nothing.  *arrived_as* is the format the file came in
    when a door converted it (a GLB handed on as an STL); otherwise the
    file's own suffix says.  *read_scale* is what Kiln multiplied a glTF's
    numbers by when it read them (the conversion record's ``read_scale``);
    left out, a glTF at *file_path* is asked itself.
    """
    pose = read_pose(file_path)
    if pose is not None:
        # Which way up it arrived and what Kiln did about it, in the
        # person's words; the record's own housekeeping stays in the note.
        result["pose"] = {k: v for k, v in pose.items() if k not in ("schema", "judged_at")}
    arrival = read(file_path)
    if arrival is None:
        return result
    result["came_from"] = arrival.line()
    result["arrival"] = arrival.to_dict()
    if size and max(size) > 0:
        if read_scale is None:
            read_scale = _read_scale_of(file_path)
        check = size_check(
            arrival, size, arrived_as=arrived_as or os.path.splitext(file_path)[1], read_scale=read_scale
        )
        if check:
            result["size_check"] = check
        dimensions = result.get("dimensions")
        if arrival.real_size is False and isinstance(dimensions, dict):
            dimensions["summary"] = " x ".join(_as_said(v) for v in size) + " in no real unit (see size_check)"
    return result
