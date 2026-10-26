"""The layer height a part was designed in, kept beside it and honoured at the slice.

A photo relief is sculpted in whole layers (``levels = depth / layer
height``), and ``decorate_surface`` said which layer height it was made
for -- then every slice door printed the part at whatever layer the profile
happened to carry.  Nearly every bundled profile states none, so the
slicer's own default did: a 1.6 mm relief designed as eight 0.2 mm levels
was sliced at 0.3 mm and printed as six, the terraces between them merged
(measured 2026-10-26 on a headshot coaster).  Kiln stated the layer and
then ignored it.

So the make writes the layer height beside the part (``<file>.layer.json``,
bound to the file's bytes like the colour and arrival notes), the copies a
slice door makes of the part carry it, and :func:`ensure_profile_layer`
reads it inside :func:`kiln.slicer.slice_file` -- the one place every
slicing door funnels through, where the filament and the supports are
decided -- so the slice prints the part in the layers it was designed in,
whichever door it entered by.

The rule, in order:

* a part with no note is sliced as it was, and nothing is said;
* a profile Kiln did not write is the caller's own, every value of which
  they chose -- read, never changed; the reply says what the relief gets;
* a Kiln-written profile whose caller STATED ``layer_height`` (a
  ``reslice_with_overrides`` override, their own profile) is theirs too:
  kept, and the reply says how many of the designed levels print at it;
* a designed layer the fitted nozzle cannot lay well (outside the window
  :func:`kiln.slicer_profiles._layer_window_mm` gives) is not applied; the
  profile's own layer stands and the reply says why;
* otherwise the designed layer is written into the profile, as Kiln's own
  layer, replaceable by the next slice of another part.

The note is kept on a part's saved versions too (:func:`keep_on_record`,
under :data:`RECORD_KEY` in the version's provenance), so a version
restored, pulled or cloned onto another computer is sliced in its designed
layers.

Every result carries the answer (:class:`SliceLayer`, on
``SliceResult.layer``) so a door's reply says which layer the part printed
in, and why.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Appended to the file's own path: ``part.stl`` -> ``part.stl.layer.json``.
NOTE_SUFFIX = ".layer.json"
SCHEMA_VERSION = 1

#: The PrusaSlicer key; :mod:`kiln.slicer_orca` carries it into the Orca dialect.
LAYER_KEY = "layer_height"

#: Who decided the slice's layer.
DECIDED_RECORD = "record"          # the part's own note, written into the profile
DECIDED_STATED = "stated"          # the caller's key or profile, left alone
DECIDED_NOZZLE = "outside_nozzle"  # the note asks for a layer this nozzle cannot lay
DECIDED_NOT_CHECKED = "not_checked"  # the profile could not be read

#: Where a saved version keeps the note: in its record's provenance, as the
#: note itself, so it goes wherever the record goes and stays bound to the
#: bytes it describes.
RECORD_KEY = "designed_layer"

#: The line both slicers write into the G-code for the layer they used.
_GCODE_LAYER_RE = re.compile(r"^;\s*layer_height\s*=\s*([0-9.]+)", re.MULTILINE)


@dataclass(frozen=True)
class DesignedLayer:
    """What a part was designed for: its layer height, and the levels it holds."""

    height_mm: float
    #: Whole layers the designed feature spans, when it was made in layers.
    levels: int | None = None
    #: The feature's depth, when the levels are its depth in layers.
    depth_mm: float | None = None
    #: The door that made it.
    made_by: str = ""
    #: Why, in a few words.
    why: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SliceLayer:
    """What the slice did about the part's designed layer, and why."""

    #: The layer the slice prints at; ``None`` when the profile leaves it to
    #: the slicer's own default.
    height_mm: float | None
    #: ``record`` / ``stated`` / ``outside_nozzle`` / ``not_checked``.
    decided_by: str
    designed: DesignedLayer
    #: The designed levels that print at ``height_mm``; ``None`` when unknown.
    levels_printed: int | None
    reason: str
    #: The keys this layer wrote into the profile; empty when it wrote none.
    settings: dict[str, str] = field(default_factory=dict)

    @property
    def note(self) -> str:
        """One sentence for a reply or a terminal."""
        d = self.designed
        made = f"{d.levels} levels of {d.height_mm:g} mm" if d.levels else f"{d.height_mm:g} mm layers"
        if self.decided_by == DECIDED_RECORD:
            return f"Sliced in {d.height_mm:g} mm layers, as the part was designed ({made})."
        kept = f"{self.height_mm:g} mm" if self.height_mm else "the slicer's own layer height"
        shown = ""
        if d.levels and self.levels_printed is not None:
            shown = f" {self.levels_printed} of its {d.levels} levels print at that height."
        return f"Sliced at {kept} ({self.reason}); the part was designed in {made}.{shown}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "height_mm": self.height_mm,
            "decided_by": self.decided_by,
            "designed": self.designed.to_dict(),
            "levels_printed": self.levels_printed,
            "reason": self.reason,
            "settings": dict(self.settings),
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# The note beside the file
# ---------------------------------------------------------------------------


def note_path_for(file_path: str) -> str:
    return file_path + NOTE_SUFFIX


def _identity(file_path: str) -> str | None:
    """The file's content hash, as the print gate identifies a file."""
    from kiln.preview_gate import hash_file

    digest = hash_file(file_path)
    return None if digest.startswith("NO_FILE:") else digest


def record(file_path: str, layer: DesignedLayer) -> str | None:
    """Write the note for *file_path*.  Returns its path, or ``None`` when it
    could not be written.  Never raises: a make is never lost for its note."""
    try:
        if not (math.isfinite(layer.height_mm) and layer.height_mm > 0):
            return None
        identity = _identity(file_path)
        if identity is None:
            return None
        note = note_path_for(file_path)
        if os.path.islink(note):
            return None
        body = {"schema": SCHEMA_VERSION, "file": identity, "layer": layer.to_dict()}
        tmp = f"{note}.{os.getpid()}.tmp"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp, flags, 0o644)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(body, fh, indent=2)
            os.replace(tmp, note)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return note
    except Exception:  # noqa: BLE001 -- never costs the make
        logger.debug("layer note not written for %s", file_path, exc_info=True)
        return None


def _layer_of(body: Any, identity: str | None) -> DesignedLayer | None:
    """The layer a note *body* states, when it describes the bytes *identity* names."""
    try:
        if not isinstance(body, dict) or body.get("schema") != SCHEMA_VERSION:
            return None
        if identity is None or body.get("file") != identity:
            return None
        values = body.get("layer") or {}
        height = float(values.get("height_mm"))
        if not (math.isfinite(height) and height > 0):
            return None
        levels = values.get("levels")
        depth = values.get("depth_mm")
        return DesignedLayer(
            height_mm=height,
            levels=int(levels) if levels else None,
            depth_mm=float(depth) if depth else None,
            made_by=str(values.get("made_by") or "")[:64],
            why=str(values.get("why") or "")[:200],
        )
    except (ValueError, TypeError, AttributeError):
        return None


def _note_body(file_path: str) -> dict[str, Any] | None:
    try:
        with open(note_path_for(file_path), encoding="utf-8") as fh:
            body = json.load(fh)
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def read(file_path: str | None) -> DesignedLayer | None:
    """The note for *file_path*, or ``None`` when there is none, it is
    unreadable, or it describes different bytes than the file now holds."""
    if not file_path:
        return None
    body = _note_body(file_path)
    return None if body is None else _layer_of(body, _identity(file_path))


def carry(from_path: str | None, to_path: str | None) -> None:
    """Give *to_path* the note of *from_path*: a centred, placed or laid-out
    copy of the part is designed in the same layers as the part."""
    if not from_path or not to_path or os.path.abspath(from_path) == os.path.abspath(to_path):
        return
    layer = read(from_path)
    if layer is not None:
        record(to_path, layer)


# ---------------------------------------------------------------------------
# The note on a saved version
# ---------------------------------------------------------------------------


def keep_on_record(provenance: dict[str, Any] | None, mesh_path: str | None) -> dict[str, Any] | None:
    """Return *provenance* saying which layer *mesh_path* was designed in,
    with the note beside the file too.

    The answer is the note beside the mesh, else the one the record already
    keeps when it describes the mesh's bytes; it is written into the record
    under :data:`RECORD_KEY` and, when the file has no note of its own,
    beside the file.  Every door that writes a version's record with its
    mesh, or writes a version's mesh back to disk from its record, calls
    this, so the slice reads the note wherever the version went.

    A record naming no mesh keeps no layer; one whose mesh is not on this
    computer is returned as it is.  Never raises.
    """
    kept = provenance.get(RECORD_KEY) if isinstance(provenance, dict) else None
    try:
        if mesh_path and not os.path.isfile(mesh_path):
            return provenance
        value: dict[str, Any] | None = None
        if mesh_path:
            identity = _identity(mesh_path)
            beside = _note_body(mesh_path)
            if _layer_of(beside, identity) is not None:
                value = beside
            elif (layer := _layer_of(kept, identity)) is not None:
                value = kept
                record(mesh_path, layer)
    except Exception:  # noqa: BLE001 -- a version is never lost for its note
        logger.debug("designed layer not kept for %s", mesh_path, exc_info=True)
        return provenance
    if value is None and kept is None:
        return provenance
    out = dict(provenance or {})
    if value is None:
        out.pop(RECORD_KEY, None)
    else:
        out[RECORD_KEY] = value
    return out


# ---------------------------------------------------------------------------
# The decision, at the slicing chokepoint
# ---------------------------------------------------------------------------


def levels_at(designed: DesignedLayer, height_mm: float | None) -> int | None:
    """How many of the designed levels print at *height_mm*: the whole layers
    the feature's depth spans, never more than it was designed with."""
    if not height_mm or not designed.levels:
        return None
    depth = designed.depth_mm or designed.levels * designed.height_mm
    return max(1, min(designed.levels, int(math.floor(depth / height_mm + 1e-6))))


def witnessed(decision: SliceLayer | None, gcode_path: str | None) -> SliceLayer | None:
    """*decision* checked against the file the slicer wrote: the layer the
    G-code names is the one that prints, whatever was asked for."""
    if decision is None or not gcode_path or not os.path.isfile(gcode_path):
        return decision
    try:
        with open(gcode_path, encoding="utf-8", errors="replace") as fh:
            match = _GCODE_LAYER_RE.search(fh.read())
    except OSError:
        return decision
    if not match:
        return decision
    wrote = float(match.group(1))
    if decision.height_mm is not None and abs(wrote - decision.height_mm) < 1e-6:
        return decision
    return SliceLayer(
        height_mm=wrote,
        decided_by=decision.decided_by if decision.height_mm is not None else DECIDED_STATED,
        designed=decision.designed,
        levels_printed=levels_at(decision.designed, wrote),
        reason=decision.reason if decision.height_mm is not None else "the slicer's own default",
        settings=decision.settings,
    )


def _first_number(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(str(value).split(",")[0].strip())
    except ValueError:
        return None


def ensure_profile_layer(
    profile: str | None,
    input_path: str,
    *,
    nozzle_mm: float | None = None,
) -> tuple[str | None, SliceLayer | None]:
    """Return a profile path whose layer height is DECIDED for *input_path*,
    and the decision -- ``None`` for a part that carries no note.

    *nozzle_mm* is the size the slice is for; left out, the profile's own
    ``nozzle_diameter`` answers.  The designed layer is written through
    :func:`kiln.slicer_profiles.profile_with_overrides` as Kiln's own layer
    (``stated=False``), so the next slice of another part is free to decide
    again.
    """
    designed = read(input_path)
    if designed is None:
        return profile, None

    from kiln.slicer_orca import ini_to_settings
    from kiln.slicer_profiles import _layer_window_mm, profile_origin, profile_with_overrides

    if profile and not os.path.isfile(profile):
        return profile, SliceLayer(
            None, DECIDED_NOT_CHECKED, designed, None, "the profile names no file",
        )
    settings: dict[str, str] = {}
    origin = profile_origin(profile)
    if profile:
        try:
            settings = ini_to_settings(profile)
        except (OSError, UnicodeDecodeError):
            logger.debug("Profile %s could not be read; handed on untouched", profile, exc_info=True)
            return profile, SliceLayer(
                None, DECIDED_NOT_CHECKED, designed, None, "the profile could not be read",
            )
    own = _first_number(settings.get(LAYER_KEY))

    if profile and not origin.kiln:
        return profile, SliceLayer(
            own, DECIDED_STATED, designed, levels_at(designed, own), "the profile is the caller's own",
        )
    if LAYER_KEY in origin.stated:
        return profile, SliceLayer(
            own, DECIDED_STATED, designed, levels_at(designed, own),
            f"{LAYER_KEY} {own:g} mm was asked for" if own else f"{LAYER_KEY} was asked for",
        )

    nozzle = nozzle_mm or _first_number(settings.get("nozzle_diameter"))
    if nozzle:
        low, high = _layer_window_mm(nozzle)
        if not (low - 1e-9 <= designed.height_mm <= high + 1e-9):
            return profile, SliceLayer(
                own, DECIDED_NOZZLE, designed, levels_at(designed, own),
                f"a {nozzle:g} mm nozzle lays {low:g}-{high:g} mm layers, not {designed.height_mm:g} mm",
            )
    if own is not None and abs(own - designed.height_mm) < 1e-9:
        return profile, SliceLayer(
            own, DECIDED_RECORD, designed, designed.levels, "the profile already prints it",
        )
    to_write = {LAYER_KEY: f"{designed.height_mm:g}"}
    prefix = f"{Path(profile).stem}_" if profile else None
    return (
        profile_with_overrides(profile, to_write, prefix=prefix, stated=False),
        SliceLayer(designed.height_mm, DECIDED_RECORD, designed, designed.levels, "the part's own note", to_write),
    )
