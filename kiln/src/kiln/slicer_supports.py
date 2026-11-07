"""Supports a slice decides for itself, at the chokepoint every door shares.

A part whose printability report said ``needs_supports=True`` was sliced
without them by every tool door, and the estimate those doors returned was
of the unsupported file: on a desk stand with a 70 mm cantilevered arm the
report recommended "Enable supports" while the G-code beside it carried
``support_material = 0``, no support lines, and a time ~50 minutes short
(measured 2026-10-19: 2h14m without, 3h04m with).  Only ``kiln slice
--support-mode auto`` ever turned them on, and that decision lived in the
CLI alone.

This module is that decision, made where the material identity is made
(:mod:`kiln.slicer_filament`): inside :func:`kiln.slicer.slice_file`, so a
slice cannot leave without it whichever door it entered by.

The rule, in order:

* a profile Kiln did not write is the caller's own, every value of which
  they chose -- read, never changed;
* a Kiln-written profile whose caller STATED ``support_material`` (a
  ``reslice_with_overrides`` override, ``kiln slice --support-mode
  minimal`` / ``off``) is theirs too;
* a slicer project keeps the support settings it saved
  (:func:`kiln.project_plates.saved_supports`): it stands where its person
  arranged it, and those are the supports it was arranged for.  Supports
  the project left off stay off, and the answer says when the engine reads
  the part the other way.  Where the project has them on, the faces the
  engine measured as printing on their own are still kept clear, as on any
  other slice and even when the engine reads the part as needing none:
  that only takes supports away, so nothing it does can put one off the
  bed;
* otherwise the printability engine reads the part and gives the one
  verdict every door gives (:func:`kiln.printability.support_verdict`);
  when it says the part needs supports the slice gets them, under exactly
  the faces that verdict counted as overhangs -- the slicer's threshold is
  written from the same angle -- standing on the plate or the model, and
  under bridges too; the faces it said print on their own (a screw
  thread's flanks beside the shelf that needs supports) are painted as
  support blockers in the file the slicer is handed (:func:`kept_off_input`),
  because a slicer turned on for one overhang supports every slope it
  finds and packs a thread's grooves; and the file the slicer wrote is read
  back, so a slice that asked for supports and got none says so;
* when the one reason is a piece that starts on nothing (a foot a layer
  over the plate, a tooth hanging from the top of a hole), the supports
  are asked for under that piece and nowhere else: its faces are painted
  to be supported (:func:`asked_under_input`) and the slicer supports only
  what is painted.  Where the file cannot be painted the slice keeps the
  supports above, for the whole part;
* a part the engine cannot read is sliced as it was, and the answer says
  the check was not made rather than reading like a part that needs none.

Every result carries the answer (:class:`SliceSupports`, on
``SliceResult.supports``) so a door's reply says whether supports are in
the file and in the estimate, and why.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import zipfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from kiln.gcode import feature_label

logger = logging.getLogger(__name__)

#: The PrusaSlicer key that turns supports on; :mod:`kiln.slicer_orca`
#: carries it, and the two :func:`support_settings` writes beside it, into
#: the Orca dialect.
SUPPORT_KEY = "support_material"

#: The suffix a witnessed decision's reason carries when the file has none.
_PLACED_NONE = "; the slicer placed none"

#: Who decided the answer.
DECIDED_STATED = "stated"        # the caller's own key, left alone
DECIDED_PROJECT = "project"      # the settings the project itself saved
DECIDED_ANALYSIS = "analysis"    # the printability engine read the part
DECIDED_NOT_CHECKED = "not_checked"  # the engine could not read it

@dataclass(frozen=True)
class SliceSupports:
    """What the slice did about supports, and why."""

    #: Whether the file the slicer makes carries supports.
    enabled: bool
    #: ``stated`` / ``project`` / ``analysis`` / ``not_checked``.
    decided_by: str
    #: The measurement or statement behind the answer, in a few words.
    reason: str
    #: The keys this layer wrote into the profile; empty when it wrote none.
    settings: dict[str, str] = field(default_factory=dict)
    #: The faces supports are kept off, by index into the triangles the
    #: verdict judged (:attr:`kiln.printability.SupportJudgement.support_kept_off_faces`).
    #: Bookkeeping for :func:`kept_off_input`; never served.
    kept_off: tuple[int, ...] = field(default=(), repr=False)
    #: How many faces of the file the slicer was handed carry a blocker.
    kept_off_painted: int = 0
    #: The faces supports are asked for under and nowhere else, by the same
    #: index (:attr:`kiln.printability.SupportJudgement.support_under_faces`):
    #: set when the one reason for supports is a piece that starts on
    #: nothing.  Bookkeeping for :func:`asked_under_input`; never served.
    under: tuple[int, ...] = field(default=(), repr=False)
    #: How many faces of the file the slicer was handed are painted to be
    #: supported; above zero, the slice supports nothing else.  Never served.
    under_painted: int = field(default=0, repr=False)

    @property
    def note(self) -> str:
        """One sentence for a reply or a terminal."""
        placed_none = self.reason.endswith(_PLACED_NONE)
        asked_for = self.reason[: -len(_PLACED_NONE)] if placed_none else self.reason
        if self.decided_by == DECIDED_STATED:
            if placed_none:
                return f"supports asked for, but the slicer placed none ({asked_for})"
            state = "on" if self.enabled else "off"
            return f"supports {state}, as stated ({self.reason})"
        if self.decided_by == DECIDED_PROJECT:
            if placed_none and self.kept_off_painted:
                return ("no supports placed: the project has them on, and Kiln kept them off the faces it "
                        "measured as printing on their own")
            if placed_none:
                return f"supports asked for, {asked_for}, but the slicer placed none"
            if self.enabled and self.kept_off_painted:
                return f"supports on, {self.reason}; none on the parts that print on their own"
            return f"{'supports on' if self.enabled else 'no supports'}, {self.reason}"
        if self.decided_by == DECIDED_NOT_CHECKED:
            return f"no supports; whether the part needs them was {self.reason}"
        if placed_none:
            return f"the part needs supports but the slicer placed none: {asked_for}"
        # Asked for under one piece alone, the faces that would have been
        # kept clear carry no supports either: the sentence is the same one.
        if self.enabled and (self.kept_off_painted or (self.under_painted and self.kept_off)):
            return f"supports on: {self.reason}; none on the parts that print on their own"
        if self.enabled:
            return f"supports on: {self.reason}"
        return f"no supports: {self.reason}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for bookkeeping in ("kept_off", "under", "under_painted"):
            d.pop(bookkeeping)
        d["note"] = self.note
        return d


def support_settings(overhang_threshold_deg: float, *, build_plate_only: bool = False) -> dict[str, str]:
    """The slicer keys that support every face Kiln counts as an overhang.

    *overhang_threshold_deg* is Kiln's angle, from vertical
    (:func:`kiln.printability.overhang_threshold_deg`); both slicers take
    the slope from horizontal, so the threshold written is its complement.
    Measured 2026-10-19 on PrusaSlicer 2.9.4 and OrcaSlicer, 0.2 mm layers:
    a 50 degree underside is supported at 60 and left alone at 40, and a
    40 x 20 mm underside at exactly the written angle is supported.  A
    small face near the angle can still come back with nothing under it
    (a 20 x 6 mm end face at 40 degrees got none below 50); the file is
    read back after the slice (:func:`witnessed`), so that is said rather
    than reported as supported.

    Supports stand on the model as well as the plate, and bridges are
    supported too: a cap over its own base gets nothing from plate-only
    supports, and PrusaSlicer's default leaves a bridge the verdict called
    too long unsupported (both measured, both zero support lines).
    *build_plate_only* is the caller's explicit choice (``kiln slice
    --support-mode minimal``), never the engine's.
    """
    slope = max(1.0, min(89.0, 90.0 - float(overhang_threshold_deg)))
    return {
        SUPPORT_KEY: "1",
        "support_material_buildplate_only": "1" if build_plate_only else "0",
        "support_material_threshold": f"{slope:g}",
        "dont_support_bridges": "0",
    }


#: A support feature as each slicer Kiln drives labels it: ``Support
#: material`` (PrusaSlicer), ``Support`` (OrcaSlicer, Bambu Studio), and the
#: interface and transition labels each adds after the word.  A label that
#: only starts with the letters ("Supported ...") is not one.
_SUPPORT_LABEL = re.compile(r"support\b", re.IGNORECASE)


def gcode_support_lines(gcode_path: str) -> int:
    """Support features the file carries, in every driven slicer's spelling.

    The support body, its interface and its transition all count: a support
    a few layers tall is written as interface alone.
    """
    try:
        with open(gcode_path, encoding="utf-8", errors="replace") as fh:
            return sum(
                1 for line in fh
                if line.startswith(";") and _SUPPORT_LABEL.match(feature_label(line) or "")
            )
    except OSError:
        return 0


def witnessed(decision: SliceSupports, gcode_path: str | None) -> SliceSupports:
    """*decision* checked against the file the slicer wrote.

    A decision says what Kiln asked for; the file says what it got.  A
    slice that asked for supports and carries none is said -- a cap over
    its own base under plate-only supports, a bridge the slicer chose not
    to support -- rather than reported as supported because the key was
    set.  Measured 2026-10-19: both shapes came back "supports on" with
    zero support lines before this check existed.
    """
    if not decision.enabled or not gcode_path:
        return decision
    if gcode_support_lines(gcode_path) > 0:
        return decision
    return replace(decision, enabled=False, reason=decision.reason + _PLACED_NONE)


#: The inputs a slice can be handed with faces kept off supports: meshes
#: Kiln reads facet for facet, so a painted copy is the same part, and a
#: 3MF, which is handed over as its own file with the paint added.
_KEPT_OFF_INPUTS = (".stl", ".obj", ".3mf")

#: The paint state both slicer families read as "no supports here"
#: (EnforcerBlockerType::BLOCKER in PrusaSlicer and in the Bambu fork).
_BLOCKER_STATE = 2
#: The state both read as "supports here" (EnforcerBlockerType::ENFORCER).
_ENFORCER_STATE = 1

#: The slicer key that, off, leaves supports only where a face is painted to
#: be supported (:mod:`kiln.slicer_orca` carries it into the Orca dialect).
PAINTED_ONLY_KEY = "support_material_auto"


def _corners_key(tri: Any) -> tuple:
    """A triangle named by its corners, whatever order they are listed in."""
    return tuple(sorted(tuple(v) for v in tri))


def kept_off_input(
    input_path: str,
    decision: SliceSupports,
    out_dir: str,
    *,
    bed_centre: tuple[float, float] | None,
) -> tuple[str, int] | None:
    """*input_path* as a 3MF with the faces *decision* keeps supports off
    painted as support blockers, ``(path, faces painted)``; ``None`` when
    nothing is kept off or the input is not one this paints.  A 3MF comes
    back as a copy of itself with the paint added (:func:`_kept_off_3mf`);
    the rest of this describes a mesh.

    Every facet of the file is written, in its order, so the slicer slices
    the caller's own part; a facet is painted when the verdict's mesh holds
    it among the kept-off faces.  The paint is written in both families'
    spellings, PrusaSlicer's ``slic3rpe:custom_supports`` and Bambu Studio's
    and OrcaSlicer's ``paint_supports``, as the colour paint is
    (:func:`kiln.multicolor_3mf.painted_state_string`).

    A slicer arranges a loose mesh and takes a 3MF where it stands, so the
    part is placed where the mesh would have gone: centred on
    *bed_centre*, or where the file has it when that is ``None`` (the
    caller asked the slicer not to arrange), and on the plate either way.
    """
    if not (decision.enabled and decision.kept_off):
        return None
    return _painted_input(input_path, decision.kept_off, _BLOCKER_STATE, out_dir, bed_centre=bed_centre, every=False)


def asked_under_input(
    input_path: str,
    decision: SliceSupports,
    out_dir: str,
    *,
    bed_centre: tuple[float, float] | None,
) -> tuple[str, int] | None:
    """*input_path* as a 3MF with the faces *decision* asks for supports
    under painted to be supported, ``(path, faces painted)``, for a slice
    that supports only what is painted; ``None`` when it asks for none, the
    input is not one this paints, or any one of the faces could not be
    painted.  All or nothing: a slice that supports only painted faces and
    was handed a file with one missing would leave that piece on nothing,
    so the caller keeps supports on for the whole part instead.

    The file is written as :func:`kept_off_input` writes its own.
    """
    if not (decision.enabled and decision.under):
        return None
    return _painted_input(input_path, decision.under, _ENFORCER_STATE, out_dir, bed_centre=bed_centre, every=True)


def _painted_input(
    input_path: str,
    faces: tuple[int, ...],
    state: int,
    out_dir: str,
    *,
    bed_centre: tuple[float, float] | None,
    every: bool,
) -> tuple[str, int] | None:
    """*input_path* as a 3MF with *faces* (indices into the triangles the
    verdict judged) painted with the support *state*; *every* asks for all
    of them or no file."""
    suffix = Path(input_path).suffix.lower()
    if suffix not in _KEPT_OFF_INPUTS:
        return None
    from kiln.multicolor_3mf import painted_state_string
    from kiln.printability import _parse_mesh, _prepare_parsed

    try:
        facets, vertices = _parse_mesh(input_path)
        judged = _prepare_parsed(list(facets), vertices)[0]
    except (OSError, ValueError):
        logger.debug("Kept-off faces not painted: %s could not be read", input_path, exc_info=True)
        return None
    if every and not all(0 <= i < len(judged) for i in faces):
        return None
    kept = {_corners_key(judged[i]) for i in faces if 0 <= i < len(judged)}
    paint = painted_state_string(state)
    if suffix == ".3mf":
        return _kept_off_3mf(input_path, kept, out_dir, paint, every=every)
    corner_ids: dict[tuple[float, ...], int] = {}
    rows: list[str] = []
    painted = 0
    found: set[tuple] = set()
    for tri in facets:
        a, b, c = (corner_ids.setdefault(tuple(v), len(corner_ids)) for v in tri)
        key = _corners_key(tri)
        if key in kept:
            painted += 1
            found.add(key)
            rows.append(
                f'<triangle v1="{a}" v2="{b}" v3="{c}" '
                f'slic3rpe:custom_supports="{paint}" paint_supports="{paint}"/>'
            )
        else:
            rows.append(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>')
    if not painted or (every and len(found) < len(kept)):
        return None
    xs, ys, zs = zip(*corner_ids, strict=True)
    tx = 0.0 if bed_centre is None else bed_centre[0] - (min(xs) + max(xs)) / 2.0
    ty = 0.0 if bed_centre is None else bed_centre[1] - (min(ys) + max(ys)) / 2.0
    tz = -min(zs)
    model = "".join([
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        '<model unit="millimeter" xml:lang="en-US" ',
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" ',
        'xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06">\n',
        '<metadata name="Application">Kiln</metadata>\n',
        '<metadata name="slic3rpe:Version3mf">1</metadata>\n',
        '<metadata name="BambuStudio:3mfVersion">1</metadata>\n',
        '<resources><object id="1" type="model"><mesh><vertices>\n',
        "\n".join(f'<vertex x="{x:.9g}" y="{y:.9g}" z="{z:.9g}"/>' for x, y, z in corner_ids),
        "\n</vertices><triangles>\n",
        "\n".join(rows),
        "\n</triangles></mesh></object></resources>\n",
        f'<build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 {tx:.9g} {ty:.9g} {tz:.9g}"/></build>\n',
        "</model>\n",
    ])
    path = os.path.join(out_dir, f"{Path(input_path).stem}.3mf")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("3D/3dmodel.model", model)
    return path, painted


#: PrusaSlicer's namespace, which its support paint attribute is written in.
_SLIC3RPE_NS = "http://schemas.slic3r.org/3mf/2017/06"
_OBJECT_TAG = re.compile(r"<object\b[^>]*>")
_OBJECT_ID = re.compile(r"\bid\s*=\s*[\"'](\d+)[\"']")
_TRIANGLE_TAG = re.compile(r"<triangle\b[^>]*?/>")
_MODEL_TAG = re.compile(r"<model\b[^>]*>")
#: A core element written with a namespace prefix, or a triangle written as
#: an open and a close tag: shapes this reader does not rewrite.
_NOT_REWRITTEN = re.compile(r"<\w+:(?:model|object|mesh|triangles|triangle)\b|<triangle\b[^>]*[^/]>")
#: Support paint already on a triangle, in either family's spelling.
_OWN_SUPPORT_PAINT = re.compile(r"\b(?:slic3rpe:custom_supports|paint_supports)\s*=\s*[\"'][^\"']")


def _kept_off_3mf(
    input_path: str, kept: set[tuple], out_dir: str, blocker: str, *, every: bool = False,
) -> tuple[str, int] | None:
    """A copy of the 3MF at *input_path* with the *kept* faces painted
    *blocker* (a support paint state, as the slicers write one),
    ``(path, triangles painted)``; ``None`` when none can be.

    The copy is the caller's own file: every member, every object, every
    placement and every attribute as it was (colour paint included), with
    the two support attributes added to the triangles of the kept faces.  A
    triangle that already carries support paint is the person's own and is
    left as it is.  An object the build places more than once is painted
    only where every placement keeps the face off, so a blocker never lands
    under a copy that needs the support.  A file whose model is written in
    a shape this reader does not rewrite is handed over unpainted.

    *every* is for paint that asks for supports, where the careful side is
    the other one: the face is painted when any placement holds it, and
    there is no file unless every one of *kept* was found and painted.
    """
    from kiln.generation.validation import _placed_3mf_meshes
    from kiln.untrusted_zip import UntrustedZipFile

    errors: list[str] = []
    # (model part, object id) -> the ordinals every placement keeps off.
    wanted: dict[tuple[str, int], set[int]] = {}
    seen: set[tuple] = set()
    for placed, _verts, tris, ordinals in _placed_3mf_meshes(Path(input_path), errors, with_ordinals=True):
        here = {ordinal for ordinal, tri in zip(ordinals, tris, strict=True) if _corners_key(tri) in kept}
        seen.update(_corners_key(tri) for tri in tris if _corners_key(tri) in kept)
        key = (placed.part, int(placed.object_id))
        if key not in wanted:
            wanted[key] = here
        else:
            wanted[key] = wanted[key] | here if every else wanted[key] & here
    wanted = {key: ordinals for key, ordinals in wanted.items() if ordinals}
    if errors or not wanted or (every and len(seen) < len(kept)):
        return None

    attributes = f' slic3rpe:custom_supports="{blocker}" paint_supports="{blocker}"'
    path = os.path.join(out_dir, os.path.basename(input_path))
    painted = 0
    try:
        with UntrustedZipFile(input_path) as zin, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                part = {ordinals_key[1]: ordinals for ordinals_key, ordinals in wanted.items()
                        if ordinals_key[0].lstrip("/") == info.filename.lstrip("/")}
                if part:
                    text, count = _paint_model_part(data.decode("utf-8"), part, attributes)
                    if text is None:
                        raise ValueError(f"{info.filename} is not written in a shape Kiln rewrites")
                    painted += count
                    data = text.encode("utf-8")
                zout.writestr(info, data)
    except (OSError, ValueError, KeyError, UnicodeDecodeError, zipfile.BadZipFile):
        logger.debug("Kept-off faces not painted in %s", input_path, exc_info=True)
        with contextlib.suppress(OSError):
            os.remove(path)
        return None
    if not painted or (every and painted < sum(len(ordinals) for ordinals in wanted.values())):
        with contextlib.suppress(OSError):
            os.remove(path)
        return None
    return path, painted


def _paint_model_part(text: str, wanted: dict[int, set[int]], attributes: str) -> tuple[str | None, int]:
    """*text* (one model part) with *attributes* added to the wanted
    triangles of each wanted object's mesh, and how many were painted;
    ``(None, 0)`` when the part is not written in a shape this rewrites."""
    if _NOT_REWRITTEN.search(text):
        return None, 0
    out: list[str] = []
    at = painted = 0
    for opening in _OBJECT_TAG.finditer(text):
        found = _OBJECT_ID.search(opening.group(0))
        ordinals = wanted.get(int(found.group(1))) if found else None
        if not ordinals or opening.start() < at:
            continue
        # The object's first mesh, as the reader that judged it read it.
        end = text.find("</object>", opening.end())
        start = text.find("<triangles", opening.end(), end if end >= 0 else len(text))
        stop = text.find("</triangles>", start) if start >= 0 else -1
        if start < 0 or stop < 0:
            return None, 0
        for ordinal, tag in enumerate(_TRIANGLE_TAG.finditer(text, start, stop)):
            if ordinal not in ordinals or _OWN_SUPPORT_PAINT.search(tag.group(0)):
                continue
            out.append(text[at:tag.end() - 2])
            out.append(attributes)
            at = tag.end() - 2
            painted += 1
    if not painted:
        return text, 0
    out.append(text[at:])
    text = "".join(out)
    # The prefix the PrusaSlicer attribute is written with has to be declared.
    root = _MODEL_TAG.search(text)
    if root is None:
        return None, 0
    if "xmlns:slic3rpe=" not in root.group(0):
        text = text[:root.end() - 1] + f' xmlns:slic3rpe="{_SLIC3RPE_NS}"' + text[root.end() - 1:]
    return text, painted


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    "</Types>"
)
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
    "</Relationships>"
)


def decide_supports(
    input_path: str,
    *,
    material: str | None = None,
    nozzle_diameter: float | None = None,
    on_anyway: bool = False,
) -> SliceSupports:
    """Read *input_path* and decide whether its slice gets Kiln's supports.

    *on_anyway* says the slice carries supports whatever this reads (a
    project saved with them on): the faces to keep clear are then read
    even for a part that needs none.

    *material* chooses the overhang and bridge thresholds (PLA bridges
    farther than TPU); *nozzle_diameter* is the size the slice is for, so
    the check and the slice judge the same part the same way.  Never
    raises: a part the engine cannot read is ``not_checked``, said.

    Nothing is kept between calls: one process serves every customer on
    the hosted server, and a reading takes 0.1 s on an ordinary part.
    """
    from kiln.printability import judge_supports, support_verdict

    try:
        judged = judge_supports(
            input_path, material=(material or "pla").lower(), nozzle_diameter=nozzle_diameter,
            **({"clear_with_none_held": True} if on_anyway else {}),
        )
    except Exception as exc:  # noqa: BLE001 -- the slice stands without supports, and says so
        logger.debug("Support check could not read %s: %s", input_path, exc)
        return SliceSupports(
            enabled=False, decided_by=DECIDED_NOT_CHECKED,
            reason=f"not checked: {' '.join(str(exc).split())}",
        )
    reason = support_verdict(judged)
    if reason:
        return SliceSupports(
            enabled=True, decided_by=DECIDED_ANALYSIS, reason=reason,
            settings=support_settings(judged.overhang_threshold_deg),
            kept_off=tuple(judged.support_kept_off_faces),
            under=tuple(getattr(judged, "support_under_faces", ())),
        )
    return SliceSupports(
        enabled=False, decided_by=DECIDED_ANALYSIS, reason="no overhang or bridge needs them",
        kept_off=tuple(judged.support_kept_off_faces) if on_anyway else (),
    )


def ensure_profile_supports(
    profile: str | None,
    input_path: str,
    *,
    material: str | None = None,
    stated: bool = False,
    project: dict[str, str] | None = None,
) -> tuple[str | None, SliceSupports]:
    """Return a profile path whose supports are DECIDED, and the decision.

    A caller's statement is read off the profile (:class:`ProfileOrigin`)
    and honoured; otherwise :func:`decide_supports` reads the part and the
    keys it chose are written through
    :func:`kiln.slicer_profiles.profile_with_overrides` -- the door every
    Kiln-written ``.ini`` passes through.  *stated* ``True`` records them as
    the caller's (a door deciding once for every copy it slices), so the
    next layer leaves them alone; the default is Kiln's own layer, which the
    next slice of another part must be free to decide again.

    *project* is the support settings a slicer project saved
    (:func:`kiln.project_plates.saved_supports`).  They stand in for the
    engine's, after the caller's own: the project keeps its arrangement,
    and the engine's wider supports can leave the bed beside a part at its
    edge.

    A path that names no file is returned as given, with the check not
    made: the slicer runner raises "Profile file not found" where it always
    did.
    """
    from kiln.slicer_orca import ini_to_settings
    from kiln.slicer_profiles import profile_origin, profile_with_overrides

    if profile and not os.path.isfile(profile):
        return profile, SliceSupports(
            enabled=False, decided_by=DECIDED_NOT_CHECKED, reason="not checked: the profile names no file",
        )

    settings: dict[str, str] = {}
    origin = profile_origin(profile)
    if profile:
        try:
            settings = ini_to_settings(profile)
        except (OSError, UnicodeDecodeError):
            logger.debug("Profile %s could not be read; handed on untouched", profile, exc_info=True)
            return profile, SliceSupports(
                enabled=False, decided_by=DECIDED_NOT_CHECKED,
                reason="not checked: the profile could not be read",
            )

    if profile and not origin.kiln:
        # The caller's own file: every value in it is theirs.
        return profile, SliceSupports(
            enabled=settings.get(SUPPORT_KEY, "0").strip() == "1",
            decided_by=DECIDED_STATED, reason="the profile is the caller's own",
        )
    if SUPPORT_KEY in origin.stated:
        return profile, SliceSupports(
            enabled=settings.get(SUPPORT_KEY, "0").strip() == "1",
            decided_by=DECIDED_STATED, reason="the caller stated support_material",
        )

    nozzle = _first_number(settings.get("nozzle_diameter"))
    decision = decide_supports(
        input_path, material=material, nozzle_diameter=nozzle,
        **({"on_anyway": True} if project and project.get(SUPPORT_KEY) == "1" else {}),
    )
    if project and SUPPORT_KEY in project:
        decision = _as_the_project_saved(project, decision)
    # A key the caller stated beside support_material (their own threshold,
    # plate-only) is theirs and stays; the engine writes the rest.
    to_write = {key: value for key, value in decision.settings.items() if key not in origin.stated}
    if not to_write:
        return profile, decision

    # Named for the printer profile it derives from, as the material layer
    # names its file, so the slice is still counted against that printer.
    prefix = f"{Path(profile).stem}_" if profile else None
    return profile_with_overrides(profile, to_write, prefix=prefix, stated=stated), decision


def _as_the_project_saved(project: dict[str, str], own: SliceSupports) -> SliceSupports:
    """The project's saved support settings as the decision; *own* is what
    the engine read of the same part.

    Whether supports are on, and how, is the project's.  The engine's
    reading is said when it differs.  Where the project has them on, the
    faces the engine measured as printing on their own are kept clear of
    them, whether the engine wants supports elsewhere or reads the part as
    needing none at all: a slicer with supports on packs a thread's grooves
    either way.  An underside that starts in mid-air is never among those
    faces, so what the engine cannot vouch for is left to the slicer.
    """
    enabled = project[SUPPORT_KEY] == "1"
    reason = "as the project saved them"
    kept_off: tuple[int, ...] = ()
    if own.decided_by == DECIDED_ANALYSIS:
        if enabled:
            kept_off = own.kept_off
            if not own.enabled:
                reason += "; Kiln's own check reads this plate as needing none"
        elif own.enabled:
            reason += f"; Kiln's own check reads this plate as needing them ({own.reason})"
    return SliceSupports(
        enabled=enabled, decided_by=DECIDED_PROJECT, reason=reason, settings=dict(project), kept_off=kept_off,
    )


def _first_number(value: str | None) -> float | None:
    """The first number of a scalar or ``,``-joined vector setting."""
    if not value:
        return None
    try:
        return float(str(value).split(",")[0].strip())
    except ValueError:
        return None
