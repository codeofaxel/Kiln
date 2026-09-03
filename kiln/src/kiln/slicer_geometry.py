"""Slicer-added geometry for Kiln's 3D stage — what prints that the model never contained.

WHY THIS EXISTS
---------------
The stage renders the user's model.  The printer prints the model PLUS
whatever the slicer added around it: a skirt loop, a brim, a prime (wipe)
tower on multi-colour jobs, supports, a raft.  Measured 2026-09-03 on a
Bambu Lab A1 printing a three-colour painted jar: the first layer carried a
skirt spanning 157 mm and a 30 mm prime tower, neither of which had ever
appeared on the stage — so the user was looking at shapes on his plate with
no idea what they were.  On that job the tower was also the single biggest
line in the print time.  Seeing it has real value beyond curiosity.

WHERE THE TRUTH IS
------------------
This geometry only exists after slicing, and the sliced G-code is the ONLY
honest source for it.  Every slicer Kiln drives labels its toolpaths by
feature — ``;TYPE:Skirt`` / ``;TYPE:Prime tower`` (OrcaSlicer, Bambu
Studio), ``;TYPE:Skirt/Brim`` / ``;TYPE:Wipe tower`` / ``;TYPE:Support
material`` (PrusaSlicer, SuperSlicer), ``;TYPE:SKIRT`` / ``;TYPE:PRIME-TOWER``
/ ``;TYPE:SUPPORT`` (Cura), ``; feature skirt`` / ``; feature prime pillar``
(Simplify3D) — so this module reads those labels and hands the stage the
real extrusion segments, classified.  Re-deriving the shapes from the
slicer config (``skirt_distance``, ``wipe_tower_x``…) would be a second
implementation of the slicer's own maths, and it WOULD drift; nothing here
predicts, it only reports what a slice already decided.

The labels are matched by KEYWORD, not by an exact table, so a slicer that
spells a feature its own way still lands in the right class, and a label
nobody has seen is treated as part of the model — the conservative reading,
because hiding real part geometry under an "extras" toggle would be worse
than showing an extra as part.  The raw label always rides along, so a
consumer can see what the file actually said.

ALIGNMENT — THE SAME FRAME AS THE PART
--------------------------------------
G-code lives in printer-bed coordinates; the model on the stage lives in
its file's coordinates, then gets centred on the plate.  The two are joined
by the MODEL's own toolpaths: the footprint of every extrusion that is not
an extra is the part as the slicer placed it, so its centre maps to the
mesh's bounding-box centre and the first layer rests on the mesh's floor.
The join is checked before it is believed — a footprint whose size does
not match the model's means the slice is of something else (a scaled,
rotated, or different part), and the block says so instead of drawing a
skirt around the wrong object.

A zero-length ``G1 E0.8`` (an unretract at a travel destination) is not a
toolpath and is never counted: measured on the jar, those alone pushed the
"outer wall" footprint 35 mm into the prime tower.

THE BLOCK
---------
A JSON-safe dict, ``kiln.slicer_features.v1``, riding a ``kiln.mesh.v1``
payload as its optional ``slicer`` key (see :mod:`kiln.mesh_payload`)::

    {
      "kind": "kiln.slicer_features.v1",
      "available": true,
      "frame": "viewer",                 # or "mesh" — see FRAMES below
      "units": "mm",
      "source": {"filename": "jar.gcode", "slicer": "OrcaSlicer 2.3.2",
                 "labels": {"Skirt": "skirt", "Prime tower": "prime_tower"}},
      "features": [
        {"class": "skirt", "label": "Skirt",
         "segments": "<b64 Float32Array, x0 y0 z0 x1 y1 z1 per segment>",
         "count": 40, "layers": 1, "z_min": 0.2, "z_max": 0.2,
         "bounds": {"min": [x, y, z], "max": [x, y, z]}},
        ...
      ],
      "model_footprint": {"min": [x, y], "max": [x, y]},   # mesh XY, always
      "offset_mm": [dx, dy, dz],        # bed coords + offset = mesh coords
      "segments_total": 15997,
      "sampled_layer_stride": 1,        # >1 when the budget forced sampling
      "truncated": false
    }

or, when the G-code cannot honestly be drawn around this model::

    {"kind": "kiln.slicer_features.v1", "available": false,
     "reason": "…", "source": {...}}

An unavailable block still rides: a stage that sees one can say "no slicer
geometry for this model" instead of silently showing nothing.

FRAMES
------
``"mesh"`` is the canonical frame: z-up millimetres in the MODEL FILE's
coordinates (the mesh bbox the block was aligned to).  The hosted /view
page and the desktop app load the mesh file itself, so this is the frame
they need.  ``"viewer"`` is the same geometry rotated the way
:mod:`kiln.mesh_payload` rotates positions — ``(x, y, z)_mesh → (x, z,
-y)_viewer`` — which is what the inline panel reads, because its positions
already arrive that way.  :func:`to_viewer_frame` is the one converter;
:func:`shift_block` moves a block by the plate-centring offset so it can
never end up describing the part somewhere its vertices are not.

BUDGET
------
A prime tower is a few hundred layers of dense fill; supports can be more.
The block carries at most :data:`MAX_EXTRA_SEGMENTS` segments: over that,
whole layers are dropped at a uniform stride (the first layer of every
class always survives — that is the plate story) and the stride is
recorded, so a stage can label what it shows as sampled rather than pass
it off as complete.

Stateless: path in, dict out.  No disk writes, no network.
"""

from __future__ import annotations

import base64
import logging
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Versioned discriminator for the block.
FEATURE_KIND = "kiln.slicer_features.v1"

#: Where the block rides on a ``kiln.mesh.v1`` payload.
PAYLOAD_KEY = "slicer"

#: Segment budget for one block — the arithmetic: 24 bytes raw per segment,
#: 32 base64, so 60k segments is ~1.9 MB encoded, comfortably inside the
#: inline panel's 6 MB budget beside an 80k-triangle mesh.
MAX_EXTRA_SEGMENTS = 60_000

#: Collection stops here regardless of budget — a pathological support job
#: must not turn a stage payload into a memory event.  Recorded as
#: ``truncated`` when it fires.
_HARD_SEGMENT_CEILING = 1_500_000

#: The extras, in the order a stage lists them (plate-first, then what
#: stands beside and under the part).
EXTRA_CLASSES: tuple[str, ...] = (
    "skirt", "brim", "raft", "prime_tower", "support", "shield",
)

#: Display names — what a legend prints for each class.
CLASS_LABELS: dict[str, str] = {
    "skirt": "Skirt",
    "brim": "Brim",
    "raft": "Raft",
    "prime_tower": "Prime tower",
    "support": "Supports",
    "shield": "Shield",
}

#: Keyword → class, first match wins, checked in this order.  Specific
#: before general: "support" would otherwise catch nothing wrong, but
#: "tower" must beat "wipe" so a future "wipe" feature does not become a
#: tower, and "skirt/brim" is one PrusaSlicer label for both.
_CLASS_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("skirt", "skirt"),          # Skirt, Skirt/Brim, SKIRT
    ("brim", "brim"),            # Brim, BRIM
    ("raft", "raft"),            # Raft, RAFT (Cura folds raft into SUPPORT)
    ("prime tower", "prime_tower"),
    ("prime-tower", "prime_tower"),
    ("prime_tower", "prime_tower"),
    ("prime pillar", "prime_tower"),   # Simplify3D
    ("wipe tower", "prime_tower"),     # PrusaSlicer / SuperSlicer
    ("purge tower", "prime_tower"),
    ("tower", "prime_tower"),
    ("support", "support"),      # Support material, Support interface, SUPPORT
    ("shield", "shield"),        # ooze shield, draft shield
)

#: Labels that are neither the part nor an extra: the slicer's own
#: start/end blocks (a purge line at the bed edge is the printer's start
#: G-code, not a slicer feature) and firmware-driven moves.
_IGNORED_KEYWORDS: tuple[str, ...] = ("custom",)

_TYPE_RE = re.compile(r"^\s*;\s*(?:TYPE|FEATURE)\s*:\s*(.+?)\s*$", re.IGNORECASE)
_S3D_FEATURE_RE = re.compile(r"^\s*;\s*feature\s+(.+?)\s*$", re.IGNORECASE)
_LAYER_RE = re.compile(
    r"^\s*;\s*(?:LAYER_CHANGE|CHANGE_LAYER|LAYER\s*:|AFTER_LAYER_CHANGE|layer\s+\d)",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"([A-Za-z])\s*(-?\d*\.?\d+(?:[eE][-+]?\d+)?)")
_SLICER_RE = re.compile(
    r"^\s*;\s*(?:generated by|Generated with|G-Code generated by)\s+(.+?)\s*(?:\s+on\s+.*)?$",
    re.IGNORECASE,
)

_XY_EPS = 1e-6
_E_EPS = 1e-6
_ARC_CHORD_MM = 1.0
_ARC_MAX_STEP_RAD = math.radians(12.0)


def classify_feature(label: str | None) -> str:
    """Slicer feature label → ``"skirt" | "brim" | "raft" | "prime_tower" |
    "support" | "shield" | "model" | "ignore"``.

    Keyword-matched and case-insensitive, so every slicer dialect lands
    without a per-slicer table.  An unrecognised label is ``"model"`` on
    purpose — see the module docstring.
    """
    text = (label or "").strip().lower()
    if not text:
        return "model"
    for keyword in _IGNORED_KEYWORDS:
        if keyword in text:
            return "ignore"
    for keyword, cls in _CLASS_KEYWORDS:
        if keyword in text:
            return cls
    return "model"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass
class _ClassBucket:
    """Segments of one extra class, with the per-segment layer id kept
    beside them so the budget can drop whole layers."""

    cls: str
    label: str
    segments: list[float] = field(default_factory=list)  # 6 floats each
    layers: list[int] = field(default_factory=list)
    z_min: float = math.inf
    z_max: float = -math.inf

    @property
    def count(self) -> int:
        return len(self.layers)


@dataclass
class ParsedFeatures:
    """What one pass over a G-code file found, in bed coordinates."""

    buckets: dict[str, _ClassBucket]
    labels: dict[str, str]                 # raw label → class
    slicer: str | None
    #: Model-toolpath footprint ``(x_min, y_min, x_max, y_max)``, or None.
    model_footprint: tuple[float, float, float, float] | None
    model_z_min: float | None
    labelled: bool                         # any feature label seen at all
    truncated: bool
    segments_total: int


def _slicer_from_header(lines: list[str]) -> str | None:
    for line in lines:
        m = _SLICER_RE.match(line)
        if m:
            return m.group(1).strip()[:64]
    return None


def parse_slicer_features(gcode_path: str | os.PathLike[str]) -> ParsedFeatures:
    """One streaming pass over *gcode_path*: extra-class segments, the
    model footprint, and the labels the file used.

    A segment is an extrusion — positive filament while the head moves in
    XY.  Retract/unretract-in-place, travels, and Z-only moves are not
    toolpaths and are never counted.  Arcs (``G2``/``G3`` with ``I``/``J``)
    are expanded to chords so a skirt fitted with arcs stays a loop.

    Raises ``FileNotFoundError`` for a missing file; never raises on
    content — a malformed line is skipped, not fatal.
    """
    path = Path(gcode_path)
    if not path.is_file():
        raise FileNotFoundError(f"G-code not found: {path}")

    buckets: dict[str, _ClassBucket] = {}
    labels: dict[str, str] = {}
    header: list[str] = []

    # Machine state.  Defaults are what every slicer assumes at file start:
    # absolute XYZ (G90), absolute E (M82) until told otherwise.
    x = y = z = 0.0
    e = 0.0
    abs_xyz = True
    abs_e = True
    cur_label: str | None = None
    cur_cls = "model"
    layer = 0
    layer_markers = False
    last_extrude_z: float | None = None

    fp_xmin = fp_ymin = math.inf
    fp_xmax = fp_ymax = -math.inf
    model_zmin = math.inf
    segments_total = 0
    truncated = False

    def note_extra(cls: str, label: str, pts: list[tuple[float, float, float]]) -> None:
        nonlocal segments_total, truncated
        bucket = buckets.get(cls)
        if bucket is None:
            bucket = buckets[cls] = _ClassBucket(cls=cls, label=CLASS_LABELS.get(cls, label))
        for (x0, y0, z0), (x1, y1, z1) in zip(pts, pts[1:], strict=False):
            if segments_total >= _HARD_SEGMENT_CEILING:
                truncated = True
                return
            bucket.segments.extend((x0, y0, z0, x1, y1, z1))
            bucket.layers.append(layer)
            bucket.z_min = min(bucket.z_min, z0, z1)
            bucket.z_max = max(bucket.z_max, z0, z1)
            segments_total += 1

    def note_model(pts: list[tuple[float, float, float]]) -> None:
        nonlocal fp_xmin, fp_ymin, fp_xmax, fp_ymax, model_zmin
        for px, py, pz in pts:
            if px < fp_xmin:
                fp_xmin = px
            if px > fp_xmax:
                fp_xmax = px
            if py < fp_ymin:
                fp_ymin = py
            if py > fp_ymax:
                fp_ymax = py
            if pz < model_zmin:
                model_zmin = pz

    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            if line[0] == ";":
                if len(header) < 40:
                    header.append(line)
                m = _TYPE_RE.match(line) or _S3D_FEATURE_RE.match(line)
                if m:
                    cur_label = m.group(1).strip()
                    cur_cls = labels.get(cur_label)
                    if cur_cls is None:
                        cur_cls = classify_feature(cur_label)
                        labels[cur_label] = cur_cls
                    continue
                if _LAYER_RE.match(line):
                    layer_markers = True
                    layer += 1
                continue
            code = line.split(";", 1)[0]
            if not code:
                continue
            head = code.split(None, 1)[0].upper()
            if head in ("G90",):
                abs_xyz = True
                continue
            if head in ("G91",):
                abs_xyz = False
                continue
            if head in ("M82",):
                abs_e = True
                continue
            if head in ("M83",):
                abs_e = False
                continue
            if head == "G92":
                for axis, val in _WORD_RE.findall(code[3:]):
                    a = axis.upper()
                    v = float(val)
                    if a == "E":
                        e = v
                    elif a == "X":
                        x = v
                    elif a == "Y":
                        y = v
                    elif a == "Z":
                        z = v
                continue
            if head not in ("G0", "G1", "G2", "G3", "G00", "G01", "G02", "G03"):
                continue

            words = dict(
                (axis.upper(), float(val))
                for axis, val in _WORD_RE.findall(code[len(head):])
            )
            nx = words.get("X")
            ny = words.get("Y")
            nz = words.get("Z")
            if abs_xyz:
                nx = x if nx is None else nx
                ny = y if ny is None else ny
                nz = z if nz is None else nz
            else:
                nx = x + (nx or 0.0)
                ny = y + (ny or 0.0)
                nz = z + (nz or 0.0)
            ne_word = words.get("E")
            if ne_word is None:
                de = 0.0
                ne = e
            elif abs_e:
                ne = ne_word
                de = ne - e
            else:
                de = ne_word
                ne = e + ne_word

            moved = abs(nx - x) > _XY_EPS or abs(ny - y) > _XY_EPS
            if de > _E_EPS and moved:
                if head in ("G2", "G3", "G02", "G03"):
                    pts = _arc_points(
                        x, y, z, nx, ny, nz,
                        words.get("I"), words.get("J"), words.get("R"),
                        clockwise=head in ("G2", "G02"),
                    )
                else:
                    pts = [(x, y, z), (nx, ny, nz)]
                if not layer_markers and last_extrude_z is not None and nz > last_extrude_z + 1e-4:
                    layer += 1
                last_extrude_z = nz
                if cur_cls == "model":
                    note_model(pts)
                elif cur_cls != "ignore":
                    note_extra(cur_cls, cur_label or cur_cls, pts)
            x, y, z, e = nx, ny, nz, ne

    footprint = None
    if fp_xmax > fp_xmin and fp_ymax > fp_ymin:
        footprint = (fp_xmin, fp_ymin, fp_xmax, fp_ymax)
    return ParsedFeatures(
        buckets=buckets,
        labels=labels,
        slicer=_slicer_from_header(header),
        model_footprint=footprint,
        model_z_min=None if math.isinf(model_zmin) else model_zmin,
        labelled=bool(labels),
        truncated=truncated,
        segments_total=segments_total,
    )


def _arc_points(
    x0: float, y0: float, z0: float,
    x1: float, y1: float, z1: float,
    i: float | None, j: float | None, r: float | None,
    *, clockwise: bool,
) -> list[tuple[float, float, float]]:
    """Chords along a G2/G3 arc, start and end included.

    ``I``/``J`` (centre offset from the start) is what slicers emit; an
    ``R``-form arc, or one with no centre at all, degrades to its chord —
    a straight line is a smaller lie than a guessed circle.
    """
    if i is None and j is None:
        return [(x0, y0, z0), (x1, y1, z1)]
    cx = x0 + (i or 0.0)
    cy = y0 + (j or 0.0)
    radius = math.hypot(x0 - cx, y0 - cy)
    if radius < 1e-6:
        return [(x0, y0, z0), (x1, y1, z1)]
    a0 = math.atan2(y0 - cy, x0 - cx)
    a1 = math.atan2(y1 - cy, x1 - cx)
    sweep = a1 - a0
    if clockwise:
        if sweep >= 0:
            sweep -= 2 * math.pi
    else:
        if sweep <= 0:
            sweep += 2 * math.pi
    # A full circle (start == end) is the one place the sign rule above
    # yields ±2π, which is exactly right.
    steps = max(
        2,
        int(math.ceil(abs(sweep) / _ARC_MAX_STEP_RAD)),
        int(math.ceil(abs(sweep) * radius / _ARC_CHORD_MM)),
    )
    pts: list[tuple[float, float, float]] = []
    for k in range(steps + 1):
        t = k / steps
        a = a0 + sweep * t
        pts.append((cx + radius * math.cos(a), cy + radius * math.sin(a), z0 + (z1 - z0) * t))
    pts[0] = (x0, y0, z0)
    pts[-1] = (x1, y1, z1)
    return pts


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------


def align_to_mesh(
    parsed: ParsedFeatures,
    mesh_min: tuple[float, float, float],
    mesh_max: tuple[float, float, float],
) -> tuple[tuple[float, float, float] | None, str | None]:
    """The ``(dx, dy, dz)`` that carries bed coordinates into the mesh's
    frame, or ``(None, reason)`` when the slice provably is not of this
    model.

    Lateral: the model toolpaths' footprint centre → the mesh bbox centre.
    Vertical: the bed (``z = 0``) → the mesh floor.  The footprint SIZE
    must agree with the mesh's within a line width and a little — an
    extrusion centreline sits half a line inside the surface, so the
    toolpath box is always a hair smaller than the mesh, never larger.
    """
    if not parsed.labelled:
        return None, (
            "the G-code carries no feature labels, so slicer-added toolpaths "
            "cannot be told apart from the part"
        )
    if parsed.model_footprint is None:
        return None, "no model toolpaths were found in the G-code"
    fx0, fy0, fx1, fy1 = parsed.model_footprint
    mw = float(mesh_max[0]) - float(mesh_min[0])
    md = float(mesh_max[1]) - float(mesh_min[1])
    gw = fx1 - fx0
    gd = fy1 - fy0
    tol_w = max(1.5, 0.03 * mw)
    tol_d = max(1.5, 0.03 * md)
    if abs(gw - mw) > tol_w or abs(gd - md) > tol_d:
        return None, (
            f"the slice's footprint ({gw:.1f} × {gd:.1f} mm) does not match "
            f"this model ({mw:.1f} × {md:.1f} mm) — it was sliced from a "
            "different, scaled, or rotated part"
        )
    dx = (float(mesh_min[0]) + float(mesh_max[0])) / 2.0 - (fx0 + fx1) / 2.0
    dy = (float(mesh_min[1]) + float(mesh_max[1])) / 2.0 - (fy0 + fy1) / 2.0
    dz = float(mesh_min[2])
    return (dx, dy, dz), None


# ---------------------------------------------------------------------------
# The block
# ---------------------------------------------------------------------------


def _b64_f32(values: list[float]) -> str:
    import numpy as np

    return base64.b64encode(
        np.asarray(values, dtype="<f4").tobytes()
    ).decode("ascii")


def _decode_f32(b64: str) -> Any:
    import numpy as np

    return np.frombuffer(base64.b64decode(b64), dtype="<f4").reshape(-1, 6).copy()


def _sample_stride(parsed: ParsedFeatures, max_segments: int) -> int:
    total = sum(b.count for b in parsed.buckets.values())
    if total <= max_segments:
        return 1
    layers = sorted({lyr for b in parsed.buckets.values() for lyr in b.layers})
    if len(layers) <= 1:
        return 1  # one layer cannot be sampled; the ceiling still bounds it
    stride = 2
    while stride < len(layers):
        kept = sum(
            1 for b in parsed.buckets.values()
            for lyr in b.layers
            if lyr == layers[0] or (lyr - layers[0]) % stride == 0
        )
        if kept <= max_segments:
            break
        stride += 1
    return stride


def _feature_entry(
    bucket: _ClassBucket, offset: tuple[float, float, float], keep: set[int] | None,
) -> dict[str, Any] | None:
    dx, dy, dz = offset
    out: list[float] = []
    layers_kept: set[int] = set()
    seg = bucket.segments
    xmin = ymin = zmin = math.inf
    xmax = ymax = zmax = -math.inf
    for idx, lyr in enumerate(bucket.layers):
        if keep is not None and lyr not in keep:
            continue
        base = idx * 6
        x0 = seg[base] + dx
        y0 = seg[base + 1] + dy
        z0 = seg[base + 2] + dz
        x1 = seg[base + 3] + dx
        y1 = seg[base + 4] + dy
        z1 = seg[base + 5] + dz
        out.extend((x0, y0, z0, x1, y1, z1))
        layers_kept.add(lyr)
        xmin = min(xmin, x0, x1)
        xmax = max(xmax, x0, x1)
        ymin = min(ymin, y0, y1)
        ymax = max(ymax, y0, y1)
        zmin = min(zmin, z0, z1)
        zmax = max(zmax, z0, z1)
    if not out:
        return None
    return {
        "class": bucket.cls,
        "label": bucket.label,
        "segments": _b64_f32(out),
        "count": len(out) // 6,
        "layers": len(layers_kept),
        "z_min": round(zmin, 4),
        "z_max": round(zmax, 4),
        "bounds": {
            "min": [round(xmin, 4), round(ymin, 4), round(zmin, 4)],
            "max": [round(xmax, 4), round(ymax, 4), round(zmax, 4)],
        },
    }


def _source(path: Path, parsed: ParsedFeatures | None) -> dict[str, Any]:
    return {
        "filename": path.name,  # basename only — never the full path
        "slicer": parsed.slicer if parsed else None,
        "labels": dict(parsed.labels) if parsed else {},
    }


def unavailable_block(reason: str, source: dict[str, Any] | None = None) -> dict[str, Any]:
    """The honest empty block — carried, never silently omitted."""
    return {
        "kind": FEATURE_KIND,
        "available": False,
        "reason": reason,
        "source": source or {"filename": None, "slicer": None, "labels": {}},
    }


def slicer_features_block(
    gcode_path: str | os.PathLike[str],
    mesh_min: tuple[float, float, float],
    mesh_max: tuple[float, float, float],
    *,
    max_segments: int = MAX_EXTRA_SEGMENTS,
) -> dict[str, Any]:
    """The ``kiln.slicer_features.v1`` block for *gcode_path*, aligned to a
    mesh whose bbox is ``mesh_min``..``mesh_max`` (mesh space, z up), in
    the MESH frame.

    Never raises: every failure is an ``available: false`` block with a
    reason a person can read.
    """
    path = Path(gcode_path)
    try:
        parsed = parse_slicer_features(path)
    except FileNotFoundError:
        return unavailable_block("the sliced G-code is no longer on disk", _source(path, None))
    except Exception as exc:  # noqa: BLE001 — a stage never dies over furniture
        logger.debug("slicer geometry parse failed", exc_info=True)
        return unavailable_block(f"the G-code could not be read: {exc}", _source(path, None))

    offset, reason = align_to_mesh(parsed, mesh_min, mesh_max)
    if offset is None:
        return unavailable_block(reason or "unaligned", _source(path, parsed))
    if not parsed.buckets:
        return {
            "kind": FEATURE_KIND,
            "available": True,
            "frame": "mesh",
            "units": "mm",
            "source": _source(path, parsed),
            "features": [],
            "model_footprint": _footprint_dict(parsed, offset),
            "offset_mm": [round(v, 4) for v in offset],
            "segments_total": 0,
            "sampled_layer_stride": 1,
            "truncated": parsed.truncated,
        }

    stride = _sample_stride(parsed, max_segments)
    keep: set[int] | None = None
    if stride > 1:
        layers = sorted({lyr for b in parsed.buckets.values() for lyr in b.layers})
        first = layers[0]
        keep = {lyr for lyr in layers if lyr == first or (lyr - first) % stride == 0}

    features: list[dict[str, Any]] = []
    for cls in EXTRA_CLASSES:
        bucket = parsed.buckets.get(cls)
        if bucket is None:
            continue
        entry = _feature_entry(bucket, offset, keep)
        if entry is not None:
            features.append(entry)

    return {
        "kind": FEATURE_KIND,
        "available": True,
        "frame": "mesh",
        "units": "mm",
        "source": _source(path, parsed),
        "features": features,
        "model_footprint": _footprint_dict(parsed, offset),
        "offset_mm": [round(v, 4) for v in offset],
        "segments_total": sum(f["count"] for f in features),
        "sampled_layer_stride": stride,
        "truncated": parsed.truncated,
    }


def _footprint_dict(parsed: ParsedFeatures, offset: tuple[float, float, float]) -> dict[str, Any]:
    fx0, fy0, fx1, fy1 = parsed.model_footprint or (0.0, 0.0, 0.0, 0.0)
    dx, dy, _ = offset
    return {
        "min": [round(fx0 + dx, 4), round(fy0 + dy, 4)],
        "max": [round(fx1 + dx, 4), round(fy1 + dy, 4)],
    }


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


def to_viewer_frame(block: dict[str, Any]) -> dict[str, Any]:
    """A copy of a MESH-frame block in the viewer frame — the rotation
    :mod:`kiln.mesh_payload` bakes into positions, ``(x, y, z) → (x, z,
    -y)``.  A block already in the viewer frame is returned as is.
    ``model_footprint`` stays in mesh XY (it names the plate axes)."""
    if not isinstance(block, dict) or not block.get("available"):
        return block
    if block.get("frame") == "viewer":
        return block
    out = dict(block)
    out["frame"] = "viewer"
    feats: list[dict[str, Any]] = []
    for f in block.get("features") or []:
        arr = _decode_f32(f["segments"])
        rot = arr.copy()
        rot[:, 1] = arr[:, 2]
        rot[:, 2] = -arr[:, 1]
        rot[:, 4] = arr[:, 5]
        rot[:, 5] = -arr[:, 4]
        b = f.get("bounds") or {}
        lo, hi = b.get("min") or [0, 0, 0], b.get("max") or [0, 0, 0]
        entry = dict(f)
        entry["segments"] = _b64_f32(rot.reshape(-1).tolist())
        entry["bounds"] = {
            "min": [lo[0], lo[2], -hi[1]],
            "max": [hi[0], hi[2], -lo[1]],
        }
        feats.append(entry)
    out["features"] = feats
    return out


def shift_block(block: dict[str, Any], dx: float, dy: float) -> dict[str, Any]:
    """Move a block by a plate-centring offset given in MESH x/y, in place.

    In the mesh frame that is ``x += dx, y += dy``; in the viewer frame
    mesh +y is viewer −z, so ``x += dx, z -= dy`` — the same rule
    :func:`kiln.stage_plate.stand_on_plate` applies to positions, so the
    block and the part cannot part company.
    """
    if not isinstance(block, dict) or not block.get("available"):
        return block
    if not dx and not dy:
        return block
    viewer = block.get("frame") == "viewer"
    for f in block.get("features") or []:
        arr = _decode_f32(f["segments"])
        arr[:, 0] += dx
        arr[:, 3] += dx
        if viewer:
            arr[:, 2] -= dy
            arr[:, 5] -= dy
        else:
            arr[:, 1] += dy
            arr[:, 4] += dy
        f["segments"] = _b64_f32(arr.reshape(-1).tolist())
        b = f.get("bounds")
        if isinstance(b, dict):
            for key in ("min", "max"):
                v = b.get(key)
                if isinstance(v, list) and len(v) == 3:
                    v[0] = round(v[0] + dx, 4)
                    if viewer:
                        v[2] = round(v[2] - dy, 4)
                    else:
                        v[1] = round(v[1] + dy, 4)
    fp = block.get("model_footprint")
    if isinstance(fp, dict):
        for key in ("min", "max"):
            v = fp.get(key)
            if isinstance(v, list) and len(v) == 2:
                v[0] = round(v[0] + dx, 4)
                v[1] = round(v[1] + dy, 4)
    off = block.get("offset_mm")
    if isinstance(off, list) and len(off) == 3:
        off[0] = round(off[0] + dx, 4)
        off[1] = round(off[1] + dy, 4)
    return block


# ---------------------------------------------------------------------------
# Sidecar door — the block as a file, for a stage that loads the mesh itself
# ---------------------------------------------------------------------------

#: Ceiling for a sidecar that rides a 30-minute link upload.  The mesh
#: itself may be 64 MB; the extras must never cost more than a fraction of
#: it, and a block over this is re-budgeted rather than dropped.
MAX_SIDECAR_BYTES = 2 * 1024 * 1024


def mesh_bounds(
    mesh_path: str | os.PathLike[str],
) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    """Mesh-space ``(min, max)`` of a mesh file, or ``None``.

    Reads the file the same way the payload encoder does — a 3MF through
    Kiln's own standard-library reader, everything else through trimesh —
    so the bbox a sidecar is aligned to is the bbox the stage will measure.
    """
    try:
        import numpy as np
        import trimesh

        from kiln.mesh_payload import _load_3mf_stdlib, _scene_to_single_mesh

        path = Path(mesh_path)
        loaded = _load_3mf_stdlib(path) if path.suffix.lower() == ".3mf" else trimesh.load(str(path))
        mesh = _scene_to_single_mesh(loaded, path) if isinstance(loaded, trimesh.Scene) else loaded
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
            return None
        lo = np.asarray(mesh.bounds[0], dtype=float)
        hi = np.asarray(mesh.bounds[1], dtype=float)
        return (float(lo[0]), float(lo[1]), float(lo[2])), (float(hi[0]), float(hi[1]), float(hi[2]))
    except Exception:  # noqa: BLE001 — no bounds, no sidecar
        logger.debug("mesh bounds unavailable", exc_info=True)
        return None


def sidecar_for_mesh(
    mesh_path: str | os.PathLike[str],
    gcode_path: str | os.PathLike[str] | None = None,
) -> bytes | None:
    """The MESH-frame block for *mesh_path* as JSON bytes, or ``None``.

    What a local install sends beside the mesh when it stages a 30-minute
    link: the hosted page loads the mesh file itself, so it wants the
    block in the file's own frame.  Resolves the slice through the same
    door the payload uses (:func:`kiln.stage_plate.resolve_sliced_gcode`),
    so the link and the inline panel can never disagree about which slice
    belongs to the part.  Over :data:`MAX_SIDECAR_BYTES` the block is
    rebuilt at a tighter budget; an unavailable block is NOT sent — a
    link with no extras is the ordinary case, not a report.
    """
    import json

    try:
        from kiln.stage_plate import resolve_sliced_gcode

        gcode = resolve_sliced_gcode(str(mesh_path), str(gcode_path) if gcode_path else None)
        if not gcode:
            return None
        bounds = mesh_bounds(mesh_path)
        if bounds is None:
            return None
        budget = MAX_EXTRA_SEGMENTS
        for _ in range(4):
            block = slicer_features_block(gcode, bounds[0], bounds[1], max_segments=budget)
            if not block.get("available") or not block.get("features"):
                return None
            raw = json.dumps(block, separators=(",", ":")).encode("utf-8")
            if len(raw) <= MAX_SIDECAR_BYTES:
                return raw
            budget //= 2
    except Exception:  # noqa: BLE001 — a link without extras still links
        logger.debug("slicer sidecar not built", exc_info=True)
    return None


def load_sidecar(raw: bytes | str | None) -> dict[str, Any] | None:
    """Parse sidecar bytes back into a block, or ``None`` for anything that
    is not a ``kiln.slicer_features.v1`` block — a consumer never renders
    a shape it cannot vouch for."""
    import json

    try:
        block = json.loads(raw) if raw else None
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(block, dict) or block.get("kind") != FEATURE_KIND:
        return None
    return block


# ---------------------------------------------------------------------------
# Payload door
# ---------------------------------------------------------------------------


def attach_to_payload(
    payload: dict[str, Any] | None,
    gcode_path: str | os.PathLike[str] | None,
    *,
    max_segments: int = MAX_EXTRA_SEGMENTS,
) -> dict[str, Any] | None:
    """Stamp the viewer-frame block onto a ``kiln.mesh.v1`` *payload*.

    Aligned to the payload's OWN ``bbox`` — whatever centring the plate
    door already applied is therefore baked in.  A downgraded payload
    (no geometry) gets nothing: there is no part on the stage to stand a
    skirt around.  ``None`` for *gcode_path* attaches nothing at all —
    absence of a slice is the ordinary case, not an unavailable block.

    Never raises.
    """
    try:
        if not isinstance(payload, dict) or gcode_path is None:
            return payload
        if payload.get("downgraded") or not isinstance(payload.get("bbox"), dict):
            return payload
        bbox = payload["bbox"]
        lo, hi = bbox.get("min"), bbox.get("max")
        if not (isinstance(lo, list) and isinstance(hi, list) and len(lo) == 3 and len(hi) == 3):
            return payload
        block = slicer_features_block(
            gcode_path, (lo[0], lo[1], lo[2]), (hi[0], hi[1], hi[2]),
            max_segments=max_segments,
        )
        payload[PAYLOAD_KEY] = to_viewer_frame(block)
    except Exception:  # noqa: BLE001 — extras never break the stage
        logger.debug("slicer geometry not attached", exc_info=True)
    return payload
