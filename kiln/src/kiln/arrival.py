"""A model that arrives from outside Kiln: where it came from, kept beside it.

Geometry nobody at Kiln drew comes in two ways: a cloud generator's
download (Tripo, Meshy, Stability, Gemini) and a marketplace download.
Until 2026-10-01 both handed back a bare file path.  The stage never
opened on it, and nothing remembered where it came from, which is the
fact a person needs to credit a designer, keep to a license, or know
that a generator's "40 mm calibration cube" arrived 1.0 units across
(a live Tripo job, 2026-09-30: 1,459,204 faces, in no real unit).

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
  door's result, with the size check a shape nobody measured needs.

A note beside the file rather than a store under ``~/.kiln``: it
describes one file and travels with it, the same convention as the
decoration-face and design-history notes Kiln already keeps beside a
mesh.
"""

from __future__ import annotations

import json
import logging
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
        # A note cut short by a crash fails to parse, and reads as no note.
        with open(note, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=2)
        return note
    except Exception:  # noqa: BLE001 — a download is never lost for its note
        logger.debug("arrival note not written for %s", file_path, exc_info=True)
        return None


def read(file_path: str) -> Arrival | None:
    """The note for *file_path*, or ``None`` when there is none, it is
    unreadable, or it describes different bytes than the file now holds."""
    try:
        with open(note_path_for(file_path), encoding="utf-8") as fh:
            body = json.load(fh)
        if not isinstance(body, dict) or body.get("schema") != SCHEMA_VERSION:
            return None
        if body.get("file") != _identity(file_path):
            return None
        known = {f.name for f in fields(Arrival)}
        values = {k: v for k, v in (body.get("arrival") or {}).items() if k in known}
        return Arrival(**values) if values.get("kind") and values.get("by") else None
    except (OSError, ValueError, TypeError):
        return None


def carry(from_path: str, to_path: str) -> None:
    """Give *to_path* the note of *from_path*: the same model in another
    file (an OBJ or GLB written out as STL) came from the same place."""
    arrival = read(from_path)
    if arrival is not None:
        record(to_path, arrival)


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


def size_check(arrival: Arrival, size: tuple[float, float, float]) -> str:
    """What to settle about the size before printing, or ``""``.

    A generator that was never given a size gets the one sentence that is
    always true of it.  Everything else, a generator told to draw in
    millimetres included, gets the units reading a download gets: being
    told to draw in millimetres is not proof that it did, and the reading
    is silent for any printable size.
    """
    if arrival.real_size is False:
        across = " x ".join(f"{v:.3g}" for v in size)
        return (
            f"{arrival.by} was asked for a shape, not a size: this arrived "
            f"{across} across in no real unit, and a slicer reads those numbers "
            "as millimetres. Set its size before printing: "
            "rescale_model(file_path, max_dimension_mm=<the size you want>)."
        )
    from kiln.generation.validation import unit_verdict

    return unit_verdict(max(size)).describe_unchanged()


def announce(
    result: dict[str, Any],
    file_path: str,
    *,
    size: tuple[float, float, float] | None = None,
) -> dict[str, Any]:
    """Put where *file_path* came from on a door's *result*, in place.

    Adds ``came_from`` (the line) and ``arrival`` (the note), and
    ``size_check`` when *size* — the file's measured extent — needs a
    word before printing.  When the numbers are not millimetres, the
    result's ``dimensions`` summary stops saying they are.  A file with
    no note adds nothing.
    """
    arrival = read(file_path)
    if arrival is None:
        return result
    result["came_from"] = arrival.line()
    result["arrival"] = arrival.to_dict()
    if size and max(size) > 0:
        check = size_check(arrival, size)
        if check:
            result["size_check"] = check
        dimensions = result.get("dimensions")
        if arrival.real_size is False and isinstance(dimensions, dict):
            dimensions["summary"] = " x ".join(f"{v:.3g}" for v in size) + " in no real unit (see size_check)"
    return result
