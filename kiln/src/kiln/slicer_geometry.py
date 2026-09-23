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
re-arranged, or different part), and the block says so instead of drawing
a skirt around the wrong object.

A slicer may also TURN the part: OrcaSlicer's arrange is free to rotate a
plate a quarter turn while it packs it (measured 2026-09-23 on a jar-and-
lid plate, sliced 90 degrees from the placement the person approved).
A footprint whose width and depth are the model's depth and width is that
turn, not a different part, and the block says so with a ``pose`` — the
rotation about Z the slicer applied — so the stage can draw the part the
way it will print.  Which way it turned (90 or 270) cannot be read off a
bounding box, so it is settled by the first layer: the printed first
layer's footprint is rasterised against the mesh's own floor turned each
way, and the turn whose floor it matches wins.  A part whose floor looks
the same both ways is drawn the same both ways, so the tie is harmless.
A mismatch is still refused: a 180-degree turn of an asymmetric floor is
recognised the same way, and a plate whose objects were re-packed
individually matches no turn at all.

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
         "layer_index": "<b64 Uint16Array, print layer per segment>",
         "tool_index": "<b64 Uint8Array, active tool per segment>",
         "line_width_mm": 0.42,            # the slicer's ;WIDTH:, else 0.45
         "count": 40, "layers": 1, "z_min": 0.2, "z_max": 0.2,
         "bounds": {"min": [x, y, z], "max": [x, y, z]}},
        ...
      ],
      "filaments": ["#FFFFFF", "#C81E1E", "#161616"],   # per tool; [] if unnamed
      "model_footprint": {"min": [x, y], "max": [x, y]},   # mesh XY, always
      "offset_mm": [dx, dy, dz],        # bed coords + offset = mesh coords
      "pose": {"rotation_deg": 0,       # the turn about Z the slicer applied
               "resolved": true},       # false: a quarter turn whose way
                                        # could not be read (no floor)
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
from collections.abc import Callable
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
    "skirt", "brim", "raft", "prime_tower", "support", "support_interface", "shield",
)

#: Display names — what a legend prints for each class.
CLASS_LABELS: dict[str, str] = {
    "skirt": "Skirt",
    "brim": "Brim",
    "raft": "Raft",
    "prime_tower": "Prime tower",
    "support": "Supports",
    "support_interface": "Support interface",
    "shield": "Shield",
}

#: The SCAFFOLDING — what the printer builds to hold or seat the part and
#: what comes off afterwards.  A stage draws these in a fixed identity
#: colour, never the filament: scaffolding is shape information ("where
#: will supports stand, what will they touch"), and painted in the part's
#: own filament a dense support reads as a block of the part.  The legend
#: still names the filament each prints in.  Skirt, brim and the prime
#: tower keep their filament colours — there the colour IS the information.
SCAFFOLD_CLASSES: tuple[str, ...] = ("support", "support_interface", "raft", "shield")

#: Keyword → class, first match wins, checked in this order.  "skirt"
#: sits first so PrusaSlicer's joint "Skirt/Brim" lands as a skirt (the
#: label itself is kept, so the brim is still named); the tower spellings
#: precede the bare "tower" so the specific one is what matched.
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
    ("interface", "support_interface"),  # Support interface (Orca/Bambu), Support material interface (Prusa), SUPPORT-INTERFACE (Cura)
    ("support", "support"),      # Support material, Support transition, SUPPORT
    ("shield", "shield"),        # ooze shield, draft shield
)

#: Labels that are neither the part nor an extra: the slicer's own
#: start/end blocks (a purge line at the bed edge is the printer's start
#: G-code, not a slicer feature) and firmware-driven moves.
_IGNORED_KEYWORDS: tuple[str, ...] = ("custom",)

_TYPE_RE = re.compile(r"^\s*;\s*(?:TYPE|FEATURE)\s*:\s*(.+?)\s*$", re.IGNORECASE)
_S3D_FEATURE_RE = re.compile(r"^\s*;\s*feature\s+(.+?)\s*$", re.IGNORECASE)
#: One boundary per layer: ``;LAYER_CHANGE`` (PrusaSlicer, SuperSlicer,
#: OrcaSlicer), ``; CHANGE_LAYER`` (Bambu Studio), ``;LAYER:n`` (Cura),
#: ``; layer n,`` (Simplify3D).  PrusaSlicer's ``;BEFORE_LAYER_CHANGE`` /
#: ``;AFTER_LAYER_CHANGE`` bracket the same change and must NOT count again.
_LAYER_RE = re.compile(
    r"^\s*;\s*(?:LAYER_CHANGE|CHANGE_LAYER|LAYER\s*:|layer\s+\d)",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"([A-Za-z])\s*(-?\d*\.?\d+(?:[eE][-+]?\d+)?)")
_SLICER_RE = re.compile(
    r"^\s*;\s*(?:generated by|Generated with|G-Code generated by)\s+(.+?)\s*(?:\s+on\s+.*)?$",
    re.IGNORECASE,
)
#: ``; filament_colour = #FFFFFF;#C81E1E;#161616`` — Orca / Bambu Studio /
#: PrusaSlicer / SuperSlicer, one entry per tool, in the config block at the
#: END of the file (so the whole file is scanned, not just the header).
#: ``extruder_colour`` is the same list keyed by extruder on older presets.
_FILAMENT_COLOUR_RE = re.compile(
    r"^\s*;\s*(?:filament_colour|filament_color|extruder_colour|extruder_color)\s*=\s*(.+?)\s*$",
    re.IGNORECASE,
)
_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_TOOL_RE = re.compile(r"^T(\d{1,2})\b")
#: ``;WIDTH:0.42`` — the extrusion width of the paths that follow (Orca,
#: Bambu Studio, PrusaSlicer, SuperSlicer).  A stage draws a path at its
#: real width, so a brim reads as a brim and not as a hairline.
_WIDTH_RE = re.compile(r"^\s*;\s*WIDTH\s*:\s*(\d*\.?\d+)\s*$", re.IGNORECASE)
#: What a stage draws a path at when the file never said (a common FDM
#: line width, and Cura's default).
DEFAULT_LINE_WIDTH_MM = 0.45
#: The density the grams estimate assumes (PLA; PETG and ABS sit within
#: a tenth of it) — the legend marks the figure as an estimate.
FILAMENT_DENSITY_G_CM3 = 1.24
#: The layer height assumed when a class spans a single layer.
DEFAULT_LAYER_HEIGHT_MM = 0.2


def _layer_height(tops: set[float]) -> float:
    """The layer height a class was printed at: the median step between
    its distinct layer tops, or a common default when it has one layer."""
    zs = sorted(tops)
    steps = sorted(b - a for a, b in zip(zs, zs[1:], strict=False) if 0.02 <= b - a <= 1.0)
    return steps[len(steps) // 2] if steps else DEFAULT_LAYER_HEIGHT_MM

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
    tools: list[int] = field(default_factory=list)       # active T per segment
    widths: dict[float, int] = field(default_factory=dict)  # ;WIDTH: seen → count
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
    #: ``#RRGGBB`` per tool index, from the slicer's own ``filament_colour``
    #: (Orca, Bambu Studio, PrusaSlicer, SuperSlicer) — empty when the
    #: file names none, never guessed.
    filament_colors: list[str]
    #: Model-toolpath footprint ``(x_min, y_min, x_max, y_max)``, or None.
    model_footprint: tuple[float, float, float, float] | None
    model_z_min: float | None
    labelled: bool                         # any feature label seen at all
    truncated: bool
    segments_total: int
    #: The model's first printed layer as ``(x0, y0, x1, y1)`` segments in
    #: bed coordinates (capped) — the printed footprint, for telling which
    #: way a turned part was turned.
    first_layer_segments: list[tuple[float, float, float, float]] = field(default_factory=list)


def _slicer_from_header(lines: list[str]) -> str | None:
    for line in lines:
        m = _SLICER_RE.match(line)
        if m:
            return m.group(1).strip()[:64]
    return None


def _split_raft(buckets: dict[str, _ClassBucket], model_zmin: float) -> None:
    """Support laid entirely BELOW the part's first layer is the raft the
    part stands on.  Orca, Bambu Studio and PrusaSlicer label raft layers
    as support (Cura numbers them as negative layers), so the label alone
    never says which is which; the height does.  Moved segment by segment
    with its layer and tool, so a raft is drawn as the slab it is and the
    supports above it as the lattice they are.  Nothing moves for a job
    with no raft: the first support layer stands at the part's own first
    layer, never under it."""
    floor = model_zmin - 1e-3
    for cls in ("support", "support_interface"):
        bucket = buckets.get(cls)
        if bucket is None:
            continue
        seg = bucket.segments
        keep_seg: list[float] = []
        keep_lay: list[int] = []
        keep_tool: list[int] = []
        raft_seg: list[float] = []
        raft_lay: list[int] = []
        raft_tool: list[int] = []
        for idx, lyr in enumerate(bucket.layers):
            base = idx * 6
            piece = seg[base:base + 6]
            if max(piece[2], piece[5]) <= floor:
                raft_seg.extend(piece)
                raft_lay.append(lyr)
                raft_tool.append(bucket.tools[idx])
            else:
                keep_seg.extend(piece)
                keep_lay.append(lyr)
                keep_tool.append(bucket.tools[idx])
        if not raft_seg:
            continue
        raft = buckets.get("raft")
        if raft is None:
            raft = buckets["raft"] = _ClassBucket(cls="raft", label=CLASS_LABELS["raft"])
        raft.segments.extend(raft_seg)
        raft.layers.extend(raft_lay)
        raft.tools.extend(raft_tool)
        for width, n in bucket.widths.items():
            raft.widths[width] = raft.widths.get(width, 0) + n
        raft.z_min = min(raft.z_min, *raft_seg[2::3])
        raft.z_max = max(raft.z_max, *raft_seg[2::3])
        if keep_seg:
            bucket.segments, bucket.layers, bucket.tools = keep_seg, keep_lay, keep_tool
            bucket.z_min = min(keep_seg[2::3])
            bucket.z_max = max(keep_seg[2::3])
        else:
            del buckets[cls]


def _loops(bucket: _ClassBucket) -> list[list[int]]:
    """Segment indices grouped into the loops the slicer laid them as: a
    loop runs while each segment starts where the last one ended, on the
    same layer.  Travels between loops are never segments, so a break in
    the chain is a new loop."""
    seg = bucket.segments
    loops: list[list[int]] = []
    for idx, lyr in enumerate(bucket.layers):
        base = idx * 6
        if loops and bucket.layers[loops[-1][-1]] == lyr:
            prev = loops[-1][-1] * 6
            if abs(seg[prev + 3] - seg[base]) < 1e-3 and abs(seg[prev + 4] - seg[base + 1]) < 1e-3:
                loops[-1].append(idx)
                continue
        loops.append([idx])
    return loops


def _convex_hull(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Andrew's monotone chain — the outline a skirt is offset from."""
    uniq = sorted(set(pts))
    if len(uniq) < 3:
        return uniq

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for q in uniq:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], q) <= 0:
            lower.pop()
        lower.append(q)
    upper: list[tuple[float, float]] = []
    for q in reversed(uniq):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], q) <= 0:
            upper.pop()
        upper.append(q)
    return lower[:-1] + upper[:-1]


def _poly_distance(x: float, y: float, hull: list[tuple[float, float]]) -> float:
    """Distance from a point to a convex polygon's edge, zero inside it."""
    inside = True
    best = math.inf
    n = len(hull)
    for i in range(n):
        ax, ay = hull[i]
        bx, by = hull[(i + 1) % n]
        if (bx - ax) * (y - ay) - (by - ay) * (x - ax) < 0:
            inside = False
        vx, vy = bx - ax, by - ay
        length2 = vx * vx + vy * vy
        s = 0.0 if length2 == 0 else max(0.0, min(1.0, ((x - ax) * vx + (y - ay) * vy) / length2))
        best = min(best, math.hypot(x - (ax + s * vx), y - (ay + s * vy)))
    return 0.0 if inside else best


def _rect_distance(x: float, y: float, rect: tuple[float, float, float, float]) -> float:
    """Distance from a point to a rectangle's edge, zero inside it."""
    dx = max(rect[0] - x, 0.0, x - rect[2])
    dy = max(rect[1] - y, 0.0, y - rect[3])
    return math.hypot(dx, dy)


def _split_brim(
    buckets: dict[str, _ClassBucket],
    footprint: tuple[float, float, float, float],
    first_layer_pts: list[tuple[float, float]] | None = None,
) -> None:
    """PrusaSlicer and SuperSlicer write one label for both loops
    ("Skirt/Brim"); Cura writes SKIRT for a brim too.  A slicer that
    names its brim needs nothing.  Otherwise the loops say what the label
    cannot: a brim hugs the part — its first loop stands one line out
    from the outline, and each loop one line out from the last — while a
    skirt stands off, past a gap no brim loop ever has.  Loops are walked
    outward from the part; the first gap wider than a few lines ends the
    brim, and everything past it is the skirt.  A skirt with no loop
    against the part moves nothing, whatever its distance.

    Distances are to the part's FIRST-LAYER OUTLINE (its convex hull — the
    shape a slicer offsets a skirt from), not to its bounding box: a skirt
    hugging two round parts cuts across the box's empty corners and would
    read as touching."""
    skirt = buckets.get("skirt")
    if skirt is None or "brim" in buckets or not skirt.segments:
        return
    hull = _convex_hull(first_layer_pts) if first_layer_pts else []
    if len(hull) >= 3:
        def dist(x: float, y: float) -> float:
            return _poly_distance(x, y, hull)
    else:
        def dist(x: float, y: float) -> float:
            return _rect_distance(x, y, footprint)
    width = max(skirt.widths, key=skirt.widths.get) if skirt.widths else DEFAULT_LINE_WIDTH_MM
    seg = skirt.segments
    loops = _loops(skirt)
    distances: list[float] = []
    for loop in loops:
        d = math.inf
        for idx in loop:
            base = idx * 6
            d = min(d, dist(seg[base], seg[base + 1]), dist(seg[base + 3], seg[base + 4]))
        distances.append(d)
    order = sorted(range(len(loops)), key=lambda i: distances[i])
    if not order or distances[order[0]] > 1.5 * width:
        return                                   # nothing against the part: all skirt
    gap = max(1.0, 3.0 * width)
    brim_loops: set[int] = set()
    last = distances[order[0]]
    for i in order:
        if distances[i] - last > gap:
            break
        brim_loops.add(i)
        last = distances[i]
    if len(brim_loops) == len(loops):
        skirt.cls = "brim"
        skirt.label = CLASS_LABELS["brim"]
        buckets["brim"] = buckets.pop("skirt")
        return
    brim = buckets["brim"] = _ClassBucket(cls="brim", label=CLASS_LABELS["brim"])
    keep_seg: list[float] = []
    keep_lay: list[int] = []
    keep_tool: list[int] = []
    for li, loop in enumerate(loops):
        target = brim if li in brim_loops else None
        for idx in loop:
            base = idx * 6
            piece = seg[base:base + 6]
            if target is not None:
                target.segments.extend(piece)
                target.layers.append(skirt.layers[idx])
                target.tools.append(skirt.tools[idx])
            else:
                keep_seg.extend(piece)
                keep_lay.append(skirt.layers[idx])
                keep_tool.append(skirt.tools[idx])
    brim.widths = dict(skirt.widths)
    brim.z_min = min(brim.segments[2::3])
    brim.z_max = max(brim.segments[2::3])
    skirt.segments, skirt.layers, skirt.tools = keep_seg, keep_lay, keep_tool
    skirt.z_min = min(keep_seg[2::3])
    skirt.z_max = max(keep_seg[2::3])
    skirt.label = CLASS_LABELS["skirt"]        # the joint label is resolved, not kept


def _drop_unload_strokes(buckets: dict[str, _ClassBucket]) -> None:
    """A solid's layers standing on air ABOVE its top are not the solid:
    they are the end-of-print filament unload — Orca rams the filament
    out over the tower spot at the print's final height, and writes the
    ram as an extrusion under the tower's label.  The slicer's own
    preview draws that stroke floating over the tower; a person asks what
    it is.  It is dropped here, before any stage sees it: a run of fewer
    than three layers past a gap of several layers, above the last run
    that is a solid.  Gaps INSIDE a tower (sparse layers) are kept."""
    for cls in ("prime_tower", "raft"):
        bucket = buckets.get(cls)
        if bucket is None or not bucket.segments:
            continue
        tops: dict[int, float] = {}
        seg = bucket.segments
        for idx, lyr in enumerate(bucket.layers):
            base = idx * 6
            tops[lyr] = max(tops.get(lyr, -math.inf), seg[base + 2], seg[base + 5])
        layers = sorted(tops)
        if len(layers) < 2:
            continue
        first_h = max(0.05, tops[layers[1]] - tops[layers[0]])
        gap = max(3 * first_h, 1.0)
        runs: list[list[int]] = [[layers[0]]]
        for lyr in layers[1:]:
            if tops[lyr] - tops[runs[-1][-1]] <= gap:
                runs[-1].append(lyr)
            else:
                runs.append([lyr])
        solid = [r for r in runs if len(r) >= 3]
        if not solid:
            continue
        ceiling = tops[solid[-1][-1]]
        above = {lyr for lyr in layers if tops[lyr] > ceiling + 1e-6}
        if not above:
            continue
        keep_seg: list[float] = []
        keep_lay: list[int] = []
        keep_tool: list[int] = []
        for idx, lyr in enumerate(bucket.layers):
            if lyr in above:
                continue
            base = idx * 6
            keep_seg.extend(seg[base:base + 6])
            keep_lay.append(lyr)
            keep_tool.append(bucket.tools[idx])
        bucket.segments, bucket.layers, bucket.tools = keep_seg, keep_lay, keep_tool
        bucket.z_min = min(keep_seg[2::3])
        bucket.z_max = max(keep_seg[2::3])


def parse_slicer_features(
    gcode_path: str | os.PathLike[str],
    *,
    on_move: Callable[[str, float, float, float, float, float, float, bool, int, int], None] | None = None,
) -> ParsedFeatures:
    """One streaming pass over *gcode_path*: extra-class segments, the
    model footprint, and the labels the file used.

    A segment is an extrusion — positive filament while the head moves in
    XY.  Retract/unretract-in-place, travels, and Z-only moves are not
    toolpaths and are never counted.  Arcs (``G2``/``G3`` with ``I``/``J``)
    are expanded to chords so a skirt fitted with arcs stays a loop.

    *on_move*, when given, hears EVERY XY move the head makes, extruding
    or not — ``(cls, x0, y0, z0, x1, y1, z1, extruding, layer, tool)`` in
    bed coordinates, arcs already chorded — so a caller that needs the
    whole path (a clearance check sweeps travels as well as toolpaths)
    reads it off this one pass instead of keeping a second parser that
    would drift from this one.  A callback that raises stops the pass
    with its own exception; the stage's own readers pass none.

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
    tool = 0
    filament_colors: list[str] = []
    cur_width: float | None = None

    fp_xmin = fp_ymin = math.inf
    fp_xmax = fp_ymax = -math.inf
    model_zmin = math.inf
    model_first_layer: int | None = None
    first_pts: list[tuple[float, float]] = []   # the part's first-layer outline, for the brim split
    first_segs: list[tuple[float, float, float, float]] = []   # the same layer, as segments
    segments_total = 0
    truncated = False

    def note_extra(cls: str, label: str, pts: list[tuple[float, float, float]]) -> None:
        nonlocal segments_total, truncated
        bucket = buckets.get(cls)
        if bucket is None:
            # PrusaSlicer's one label for both loops ("Skirt/Brim") is kept
            # as the file wrote it until the loops are told apart by
            # geometry (``_split_brim``); a file whose loops never hug the
            # part keeps the label, so the brim it named is not hidden.
            shown = label if "/" in label else CLASS_LABELS.get(cls, label)
            bucket = buckets[cls] = _ClassBucket(cls=cls, label=shown)
        if cur_width is not None:
            bucket.widths[cur_width] = bucket.widths.get(cur_width, 0) + max(1, len(pts) - 1)
        for (x0, y0, z0), (x1, y1, z1) in zip(pts, pts[1:], strict=False):
            if segments_total >= _HARD_SEGMENT_CEILING:
                truncated = True
                return
            bucket.segments.extend((x0, y0, z0, x1, y1, z1))
            bucket.layers.append(layer)
            bucket.tools.append(tool)
            bucket.z_min = min(bucket.z_min, z0, z1)
            bucket.z_max = max(bucket.z_max, z0, z1)
            segments_total += 1

    def note_model(pts: list[tuple[float, float, float]]) -> None:
        nonlocal fp_xmin, fp_ymin, fp_xmax, fp_ymax, model_zmin, model_first_layer
        if model_first_layer is None:
            model_first_layer = layer
        if layer == model_first_layer and len(first_pts) < 60000:
            first_pts.extend((px, py) for px, py, _pz in pts)
            first_segs.extend(
                (ax, ay, bx, by)
                for (ax, ay, _az), (bx, by, _bz) in zip(pts, pts[1:], strict=False)
            )
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
                mw = _WIDTH_RE.match(line)
                if mw:
                    try:
                        w = float(mw.group(1))
                        cur_width = w if 0.05 <= w <= 5.0 else None
                    except ValueError:
                        pass
                    continue
                m = _FILAMENT_COLOUR_RE.match(line)
                if m and not filament_colors:
                    # One entry per tool; anything that is not a hex colour
                    # (an empty slot, a name) leaves the list unusable and
                    # is dropped whole — a wrong colour is worse than none.
                    parts = [c.strip().upper() for c in m.group(1).split(";")]
                    if parts and all(_HEX_RE.match(c) for c in parts):
                        filament_colors = parts
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
            if head[0] == "T":
                # A tool change — every slicer emits a bare ``T<n>`` for it,
                # Bambu Studio and Orca beside their own M620/M621 wrap.
                # ``T?``/``T-1`` (Prusa: "no tool") leave the tool as is.
                tm = _TOOL_RE.match(head)
                if tm:
                    tool = int(tm.group(1))
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
            extruding = de > _E_EPS and moved
            pts: list[tuple[float, float, float]] | None = None
            if moved and (extruding or on_move is not None):
                if head in ("G2", "G3", "G02", "G03"):
                    pts = _arc_points(
                        x, y, z, nx, ny, nz,
                        words.get("I"), words.get("J"), words.get("R"),
                        clockwise=head in ("G2", "G02"),
                    )
                else:
                    pts = [(x, y, z), (nx, ny, nz)]
            if extruding and pts is not None:
                if not layer_markers and last_extrude_z is not None and nz > last_extrude_z + 1e-4:
                    layer += 1
                last_extrude_z = nz
                if cur_cls == "model":
                    note_model(pts)
                elif cur_cls != "ignore":
                    note_extra(cur_cls, cur_label or cur_cls, pts)
            if on_move is not None and pts is not None:
                for (ax, ay, az), (bx, by, bz) in zip(pts, pts[1:], strict=False):
                    on_move(cur_cls, ax, ay, az, bx, by, bz, extruding, layer, tool)
            x, y, z, e = nx, ny, nz, ne

    if not math.isinf(model_zmin):
        _split_raft(buckets, model_zmin)
    _drop_unload_strokes(buckets)
    footprint = None
    if fp_xmax > fp_xmin and fp_ymax > fp_ymin:
        footprint = (fp_xmin, fp_ymin, fp_xmax, fp_ymax)
        _split_brim(buckets, footprint, first_pts)
    return ParsedFeatures(
        buckets=buckets,
        labels=labels,
        slicer=_slicer_from_header(header),
        model_footprint=footprint,
        model_z_min=None if math.isinf(model_zmin) else model_zmin,
        labelled=bool(labels),
        filament_colors=filament_colors,
        truncated=truncated,
        segments_total=segments_total,
        first_layer_segments=first_segs,
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


#: Rotations about Z a slicer's arrange can apply to a part, in degrees.
_TURNS = (0, 90, 180, 270)

#: Occupancy-grid cell for the first-layer-against-floor comparison, mm.
_FLOOR_CELL_MM = 2.0

#: Height above the mesh floor within which a face counts as the floor, mm.
_FLOOR_BAND_MM = 0.6

#: How much better (in intersection-over-union) a turn must match the
#: printed first layer than no turn before the stage turns the part.  A
#: part whose floor looks the same either way never clears this, and is
#: drawn unturned — the same picture.
_TURN_MARGIN = 0.15

#: The intersection-over-union below which the printed first layer does
#: not recognisably match the mesh's floor turned the chosen way, and the
#: pose is reported as unresolved.
_RESOLVED_FLOOR_MATCH = 0.5


def _turn_xy(xy: Any, deg: int, cx: float, cy: float) -> Any:
    """*xy* (N×2) rotated *deg* about ``(cx, cy)``, counter-clockwise seen
    from above (+Z)."""
    import numpy as np

    if deg % 360 == 0:
        return np.asarray(xy, dtype=float)
    rad = math.radians(deg)
    c, sn = math.cos(rad), math.sin(rad)
    arr = np.asarray(xy, dtype=float)
    dx = arr[:, 0] - cx
    dy = arr[:, 1] - cy
    out = np.empty_like(arr)
    out[:, 0] = cx + dx * c - dy * sn
    out[:, 1] = cy + dx * sn + dy * c
    return out


def _segments_to_points(segs: Any, step: float) -> Any:
    """Points every *step* mm along each ``(x0, y0, x1, y1)`` segment,
    endpoints included (N×2)."""
    import numpy as np

    arr = np.asarray(segs, dtype=float).reshape(-1, 4)
    if arr.shape[0] == 0:
        return np.zeros((0, 2))
    lengths = np.hypot(arr[:, 2] - arr[:, 0], arr[:, 3] - arr[:, 1])
    counts = np.maximum(2, np.ceil(lengths / step).astype(int) + 1)
    total = int(counts.sum())
    if total > 400_000:  # a budget, not a precision claim
        scale = 400_000 / total
        counts = np.maximum(2, (counts * scale).astype(int))
    idx = np.repeat(np.arange(arr.shape[0]), counts)
    # t in [0, 1] within each segment
    starts = np.cumsum(counts) - counts
    local = np.arange(idx.shape[0]) - np.repeat(starts, counts)
    t = local / np.maximum(1, np.repeat(counts - 1, counts))
    x = arr[idx, 0] + (arr[idx, 2] - arr[idx, 0]) * t
    y = arr[idx, 1] + (arr[idx, 3] - arr[idx, 1]) * t
    return np.column_stack([x, y])


def _floor_points(vertices: Any, faces: Any, step: float) -> Any:
    """Points sampled over the mesh's floor — the faces whose three corners
    all lie within :data:`_FLOOR_BAND_MM` of the lowest vertex — every
    *step* mm, in the mesh's own XY (N×2).  A mesh with no such face (a
    point contact) yields its floor-band vertices instead."""
    import numpy as np

    v = np.asarray(vertices, dtype=float).reshape(-1, 3)
    if v.shape[0] == 0:
        return np.zeros((0, 2))
    z_floor = float(v[:, 2].min())
    low = v[:, 2] <= z_floor + _FLOOR_BAND_MM
    f = np.asarray(faces, dtype=np.int64).reshape(-1, 3) if faces is not None else np.zeros((0, 3), int)
    if f.shape[0]:
        f = f[(f < v.shape[0]).all(axis=1)]
        cap = f[low[f].all(axis=1)]
    else:
        cap = f
    if cap.shape[0] == 0:
        return v[low][:, :2]
    a, b, c = v[cap[:, 0], :2], v[cap[:, 1], :2], v[cap[:, 2], :2]
    # A barycentric lattice per triangle, dense enough for the cell size,
    # built one lattice size at a time so a floor of a hundred thousand
    # small triangles is a handful of array operations, not a loop.
    longest = np.maximum.reduce([
        np.hypot(*(b - a).T), np.hypot(*(c - b).T), np.hypot(*(a - c).T),
    ])
    n = np.clip(np.ceil(longest / step).astype(int), 1, 64)
    pts = [a, b, c]
    for k in np.unique(n):
        k = int(k)
        if k <= 1:
            continue
        sel = n == k
        i, j = np.meshgrid(np.arange(k + 1), np.arange(k + 1), indexing="ij")
        keep = (i + j) <= k
        u = (i[keep] / k)[None, :, None]
        w = (j[keep] / k)[None, :, None]
        aa, bb, cc = a[sel][:, None, :], b[sel][:, None, :], c[sel][:, None, :]
        pts.append((aa + (bb - aa) * u + (cc - aa) * w).reshape(-1, 2))
    return np.vstack(pts)


def _occupancy(xy: Any, origin: tuple[float, float], shape: tuple[int, int], cell: float) -> Any:
    import numpy as np

    grid = np.zeros(shape, dtype=bool)
    if xy.shape[0] == 0:
        return grid
    ix = np.floor((xy[:, 0] - origin[0]) / cell).astype(int)
    iy = np.floor((xy[:, 1] - origin[1]) / cell).astype(int)
    ok = (ix >= 0) & (iy >= 0) & (ix < shape[0]) & (iy < shape[1])
    grid[ix[ok], iy[ok]] = True
    return grid


def _turn_scores(
    parsed: ParsedFeatures,
    mesh_min: tuple[float, float, float],
    mesh_max: tuple[float, float, float],
    mesh_floor: tuple[Any, Any],
    offset_xy: tuple[float, float],
    candidates: tuple[int, ...],
) -> dict[int, float]:
    """Intersection-over-union between the printed first layer and the
    mesh's floor turned each candidate way, both on one occupancy grid in
    mesh XY.  Empty when either side has nothing to compare."""
    import numpy as np

    gcode = _segments_to_points(parsed.first_layer_segments, _FLOOR_CELL_MM / 2.0)
    if gcode.shape[0] == 0:
        return {}
    gcode = gcode + np.asarray(offset_xy, dtype=float)
    floor = _floor_points(mesh_floor[0], mesh_floor[1], _FLOOR_CELL_MM / 2.0)
    if floor.shape[0] == 0:
        return {}
    cx = (float(mesh_min[0]) + float(mesh_max[0])) / 2.0
    cy = (float(mesh_min[1]) + float(mesh_max[1])) / 2.0
    half = max(float(mesh_max[0]) - cx, float(mesh_max[1]) - cy) + 2 * _FLOOR_CELL_MM
    origin = (cx - half, cy - half)
    n = int(math.ceil(2 * half / _FLOOR_CELL_MM)) + 1
    printed = _occupancy(gcode, origin, (n, n), _FLOOR_CELL_MM)
    scores: dict[int, float] = {}
    for deg in candidates:
        turned = _occupancy(_turn_xy(floor, deg, cx, cy), origin, (n, n), _FLOOR_CELL_MM)
        union = int((printed | turned).sum())
        scores[deg] = (int((printed & turned).sum()) / union) if union else 0.0
    return scores


def align_pose(
    parsed: ParsedFeatures,
    mesh_min: tuple[float, float, float],
    mesh_max: tuple[float, float, float],
    *,
    mesh_floor: tuple[Any, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """How the slice sits on this mesh — ``{"rotation_deg", "resolved",
    "offset"}`` — or ``(None, reason)`` when the slice provably is not of
    this model.

    ``rotation_deg`` is the turn about Z the slicer applied to the part
    (0, 90, 180 or 270; counter-clockwise seen from above), ``offset`` the
    ``(dx, dy, dz)`` that carries bed coordinates into the mesh's frame
    once the mesh has been turned that way about its own bbox centre.

    Lateral: the model toolpaths' footprint centre → the mesh bbox centre.
    Vertical: the bed (``z = 0``) → the mesh floor.  The footprint SIZE
    must agree with the mesh's — as it is, or turned a quarter — within a
    line width and a little: an extrusion centreline sits half a line
    inside the surface, so the toolpath box is always a hair smaller than
    the mesh, never larger.  A 3 % tolerance on the mesh's own extent never
    turns a quarter turn into a match, because the jar-and-lid plate that
    taught this was 114.5 × 52.7 mm: its turned footprint missed by 60 mm
    on each axis, and the check said "different part" — which it was not.

    *mesh_floor* is ``(vertices, faces)`` of the mesh in its own frame;
    with it, the way a turn went is settled against the printed first
    layer (see the module docstring), and ``resolved`` says whether that
    layer recognisably matched the floor turned the chosen way.  Without
    it a quarter turn is reported as 90 with ``resolved`` false; 0 and 180
    are told apart only with a floor, and default to 0.
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
    upright = abs(gw - mw) <= tol_w and abs(gd - md) <= tol_d
    quarter = abs(gw - md) <= tol_d and abs(gd - mw) <= tol_w
    if not upright and not quarter:
        return None, (
            f"the slice's footprint ({gw:.1f} × {gd:.1f} mm) does not match "
            f"this model ({mw:.1f} × {md:.1f} mm) — it was sliced from a "
            "different, scaled, or re-arranged part"
        )
    dx = (float(mesh_min[0]) + float(mesh_max[0])) / 2.0 - (fx0 + fx1) / 2.0
    dy = (float(mesh_min[1]) + float(mesh_max[1])) / 2.0 - (fy0 + fy1) / 2.0
    dz = float(mesh_min[2])
    offset = (dx, dy, dz)

    candidates: tuple[int, ...] = tuple(
        deg for deg in _TURNS if (upright if deg in (0, 180) else quarter)
    )
    scores: dict[int, float] = {}
    if mesh_floor is not None and len(candidates) > 1:
        try:
            scores = _turn_scores(parsed, mesh_min, mesh_max, mesh_floor, (dx, dy), candidates)
        except Exception:  # noqa: BLE001 — an unreadable floor is "unresolved", not a crash
            logger.debug("first-layer comparison failed", exc_info=True)
            scores = {}

    resolved = True
    if scores:
        best = max(candidates, key=lambda d: (scores.get(d, 0.0), -d))
        if upright:
            # Unturned is the default; a turn must clearly beat it.
            rotation = best if scores[best] - scores.get(0, 0.0) > _TURN_MARGIN else 0
        else:
            rotation = best
        # "Resolved" means the printed first layer actually looks like the
        # mesh's floor turned that way — not merely that one guess scored
        # above another.  A floor the comparison could not recognise leaves
        # the turn a guess, and the pose says so.
        resolved = scores.get(rotation, 0.0) >= _RESOLVED_FLOOR_MATCH
    elif upright:
        rotation = 0
    else:
        rotation = 90
        resolved = False
    return {"rotation_deg": rotation, "resolved": resolved, "offset": offset}, None


def align_to_mesh(
    parsed: ParsedFeatures,
    mesh_min: tuple[float, float, float],
    mesh_max: tuple[float, float, float],
) -> tuple[tuple[float, float, float] | None, str | None]:
    """The ``(dx, dy, dz)`` that carries bed coordinates into the mesh's
    frame, or ``(None, reason)`` when the slice provably is not of this
    model — :func:`align_pose` without the turn.  The offset is the same
    whichever way the slicer turned the part, because a turn about the
    bbox centre leaves that centre where it was."""
    pose, reason = align_pose(parsed, mesh_min, mesh_max)
    if pose is None:
        return None, reason
    return pose["offset"], None


# ---------------------------------------------------------------------------
# The block
# ---------------------------------------------------------------------------


def _b64_f32(values: list[float]) -> str:
    import numpy as np

    return base64.b64encode(
        np.asarray(values, dtype="<f4").tobytes()
    ).decode("ascii")


def _b64_u16(values: list[int]) -> str:
    import numpy as np

    return base64.b64encode(np.asarray(values, dtype="<u2").tobytes()).decode("ascii")


def _b64_u8(values: list[int]) -> str:
    import numpy as np

    return base64.b64encode(np.asarray(values, dtype="u1").tobytes()).decode("ascii")


def _decode_f32(b64: str) -> Any:
    import numpy as np

    return np.frombuffer(base64.b64decode(b64), dtype="<f4").reshape(-1, 6).copy()


def _sample_stride(parsed: ParsedFeatures, max_segments: int) -> int:
    """The smallest layer stride that brings the block under *max_segments*.

    Counted per LAYER, not per segment: a million-segment support forest
    is tallied once into a few hundred layer counts, and each candidate
    stride then costs a pass over those counts, not over the segments.
    """
    total = sum(b.count for b in parsed.buckets.values())
    if total <= max_segments:
        return 1
    per_layer: dict[int, int] = {}
    for b in parsed.buckets.values():
        for lyr in b.layers:
            per_layer[lyr] = per_layer.get(lyr, 0) + 1
    layers = sorted(per_layer)
    if len(layers) <= 1:
        return 1  # one layer cannot be sampled; the ceiling still bounds it
    first = layers[0]
    stride = 2
    while stride < len(layers):
        kept = sum(
            n for lyr, n in per_layer.items()
            if lyr == first or (lyr - first) % stride == 0
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
    layer_idx: list[int] = []
    tool_idx: list[int] = []
    layers_kept: set[int] = set()
    seg = bucket.segments
    xmin = ymin = zmin = math.inf
    xmax = ymax = zmax = -math.inf
    length = 0.0
    tops: set[float] = set()
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
        length += math.hypot(x1 - x0, y1 - y0, z1 - z0)
        tops.add(round(max(z0, z1), 3))
        layer_idx.append(min(lyr, 65535))
        tool_idx.append(min(bucket.tools[idx], 255))
        layers_kept.add(lyr)
        xmin = min(xmin, x0, x1)
        xmax = max(xmax, x0, x1)
        ymin = min(ymin, y0, y1)
        ymax = max(ymax, y0, y1)
        zmin = min(zmin, z0, z1)
        zmax = max(zmax, z0, z1)
    if not out:
        return None
    width = round(
        max(bucket.widths, key=bucket.widths.get) if bucket.widths else DEFAULT_LINE_WIDTH_MM, 3
    )
    return {
        "class": bucket.cls,
        "label": bucket.label,
        "segments": _b64_f32(out),
        "layer_index": _b64_u16(layer_idx),
        "tool_index": _b64_u8(tool_idx),
        "count": len(out) // 6,
        "layers": len(layers_kept),
        # The width the stage draws these paths at: the ``;WIDTH:`` the
        # slicer wrote for most of them, else a common default.
        "line_width_mm": width,
        # What this class costs: the path length laid, and the filament it
        # takes at that width and this file's layer height (a PLA-density
        # estimate — the legend says "≈").  A person deciding whether to
        # keep supports wants the grams, the way the slicer's own legend
        # gives them.
        "length_mm": round(length, 1),
        "filament_g_est": round(length * width * _layer_height(tops) * FILAMENT_DENSITY_G_CM3 / 1000, 2),
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
    mesh_floor: tuple[Any, Any] | None = None,
) -> dict[str, Any]:
    """The ``kiln.slicer_features.v1`` block for *gcode_path*, aligned to a
    mesh whose bbox is ``mesh_min``..``mesh_max`` (mesh space, z up), in
    the MESH frame.  *mesh_floor* — ``(vertices, faces)`` in the mesh's
    frame — lets :func:`align_pose` tell which way a turned part was
    turned; the block's ``pose`` says what it decided.

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

    pose, reason = align_pose(parsed, mesh_min, mesh_max, mesh_floor=mesh_floor)
    if pose is None:
        return unavailable_block(reason or "unaligned", _source(path, parsed))
    offset = pose["offset"]
    pose_out = {"rotation_deg": int(pose["rotation_deg"]), "resolved": bool(pose["resolved"])}
    if not parsed.buckets:
        return {
            "kind": FEATURE_KIND,
            "available": True,
            "frame": "mesh",
            "units": "mm",
            "source": _source(path, parsed),
            "features": [],
            "filaments": list(parsed.filament_colors),
            "model_footprint": _footprint_dict(parsed, offset),
            "offset_mm": [round(v, 4) for v in offset],
            "pose": pose_out,
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
        "filaments": list(parsed.filament_colors),
        "model_footprint": _footprint_dict(parsed, offset),
        "offset_mm": [round(v, 4) for v in offset],
        "pose": pose_out,
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


def mesh_geometry(
    mesh_path: str | os.PathLike[str],
) -> tuple[tuple[float, float, float], tuple[float, float, float], Any, Any] | None:
    """Mesh-space ``(min, max, vertices, faces)`` of a mesh file, or ``None``.

    Reads the file the same way the payload encoder does — a 3MF through
    Kiln's own standard-library reader, everything else through trimesh —
    so the bbox a sidecar is aligned to is the bbox the stage will measure,
    and the floor a turn is settled against is the floor the stage draws.
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
        return (
            (float(lo[0]), float(lo[1]), float(lo[2])),
            (float(hi[0]), float(hi[1]), float(hi[2])),
            np.asarray(mesh.vertices, dtype=float),
            np.asarray(mesh.faces, dtype=np.int64),
        )
    except Exception:  # noqa: BLE001 — no bounds, no sidecar
        logger.debug("mesh geometry unavailable", exc_info=True)
        return None


def mesh_bounds(
    mesh_path: str | os.PathLike[str],
) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    """Mesh-space ``(min, max)`` of a mesh file, or ``None`` —
    :func:`mesh_geometry` without the geometry."""
    geo = mesh_geometry(mesh_path)
    return None if geo is None else (geo[0], geo[1])


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
        geo = mesh_geometry(mesh_path)
        if geo is None:
            return None
        lo, hi, verts, faces = geo
        budget = MAX_EXTRA_SEGMENTS
        for _ in range(4):
            block = slicer_features_block(
                gcode, lo, hi, max_segments=budget, mesh_floor=(verts, faces),
            )
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


def payload_mesh_frame(payload: dict[str, Any]) -> tuple[Any, Any] | None:
    """The payload's ``(vertices, faces)`` back in MESH space (z up), or
    ``None`` when it carries no geometry — the viewer rotation undone,
    ``(x, y, z)_viewer → (x, -z, y)_mesh``."""
    try:
        import numpy as np

        positions = payload.get("positions")
        if not isinstance(positions, str) or not positions:
            return None
        v = np.frombuffer(base64.b64decode(positions), dtype="<f4").reshape(-1, 3).astype(float)
        mesh = np.empty_like(v)
        mesh[:, 0] = v[:, 0]
        mesh[:, 1] = -v[:, 2]
        mesh[:, 2] = v[:, 1]
        indices = payload.get("indices")
        faces = (
            np.frombuffer(base64.b64decode(indices), dtype="<u4").reshape(-1, 3).astype(np.int64)
            if isinstance(indices, str) and indices
            else None
        )
        return mesh, faces
    except Exception:  # noqa: BLE001 — no floor is "unresolved", never a crash
        logger.debug("payload geometry unreadable", exc_info=True)
        return None


def apply_pose_to_payload(payload: dict[str, Any], rotation_deg: int) -> dict[str, Any]:
    """Turn the payload's part *rotation_deg* about Z (mesh up) around its
    own bbox centre, in place — positions, normals and bbox together, so
    the payload can never describe the part somewhere its vertices are
    not — and record it as ``payload["pose"]``.

    This is how the stage draws a part the way the slicer will print it:
    the one function every door's payload goes through, called by
    :func:`attach_to_payload` (local doors) and
    :func:`attach_block_to_payload` (the hosted door) from the block's own
    ``pose``.  A turn of 0 records nothing and changes nothing.  Never
    raises.
    """
    try:
        deg = int(rotation_deg) % 360
        if deg == 0 or not isinstance(payload, dict) or payload.get("downgraded"):
            return payload
        positions = payload.get("positions")
        bbox = payload.get("bbox")
        if not isinstance(positions, str) or not isinstance(bbox, dict):
            return payload
        lo, hi = bbox.get("min"), bbox.get("max")
        if not (isinstance(lo, list) and isinstance(hi, list) and len(lo) == 3 and len(hi) == 3):
            return payload
        import numpy as np

        cx = (float(lo[0]) + float(hi[0])) / 2.0
        cy = (float(lo[1]) + float(hi[1])) / 2.0
        rad = math.radians(deg)
        c, sn = math.cos(rad), math.sin(rad)

        def _turn_viewer(b64: str, about: bool) -> tuple[str, Any]:
            arr = np.frombuffer(base64.b64decode(b64), dtype="<f4").reshape(-1, 3).astype(float)
            mx = arr[:, 0] - (cx if about else 0.0)
            my = -arr[:, 2] - (cy if about else 0.0)       # viewer z is -mesh y
            nx = mx * c - my * sn + (cx if about else 0.0)
            ny = mx * sn + my * c + (cy if about else 0.0)
            out = arr.copy()
            out[:, 0] = nx
            out[:, 2] = -ny
            return base64.b64encode(out.astype("<f4", copy=False).tobytes()).decode("ascii"), out

        payload["positions"], turned = _turn_viewer(positions, True)
        normals = payload.get("normals")
        if isinstance(normals, str) and normals:
            payload["normals"], _ = _turn_viewer(normals, False)
        # The bbox from the turned vertices themselves — the exact rule
        # stand_on_plate keeps: bbox and positions move as one.
        mesh_x = turned[:, 0]
        mesh_y = -turned[:, 2]
        new_lo = [round(float(mesh_x.min()), 4), round(float(mesh_y.min()), 4), lo[2]]
        new_hi = [round(float(mesh_x.max()), 4), round(float(mesh_y.max()), 4), hi[2]]
        bbox["min"], bbox["max"] = new_lo, new_hi
        bbox["size"] = [round(new_hi[i] - new_lo[i], 4) for i in range(3)]
        payload["pose"] = {
            "rotation_deg": deg,
            "about": "z",
            "source": "slice",
            "note": (
                f"drawn as the slicer placed it: turned {deg} degrees from the "
                "file's own placement"
            ),
        }
    except Exception:  # noqa: BLE001 — an unturned part beats a dead stage
        logger.debug("pose not applied", exc_info=True)
    return payload


def attach_block_to_payload(
    payload: dict[str, Any] | None, block: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Stamp an already-built MESH-frame *block* (a sidecar) onto a
    ``kiln.mesh.v1`` *payload*, in the payload's frame.

    The hosted door's route: the slice never reaches the server, only the
    block the owner's install built from it, aligned to the mesh FILE's
    bbox.  The payload has since been centred on the plate, so the block
    is slid by the same distance — the payload's bbox centre against the
    block's own ``model_footprint`` centre, which by construction IS the
    file bbox's centre — then rotated into the viewer frame.  An
    unavailable block rides as it is; ``None`` attaches nothing.  Never
    raises.
    """
    try:
        if not isinstance(payload, dict) or not isinstance(block, dict):
            return payload
        if block.get("kind") != FEATURE_KIND or payload.get("downgraded"):
            return payload
        if not block.get("available"):
            payload[PAYLOAD_KEY] = dict(block)
            return payload
        import copy

        block = copy.deepcopy(block)
        pose = block.get("pose") if isinstance(block.get("pose"), dict) else None
        if pose and pose.get("rotation_deg"):
            apply_pose_to_payload(payload, int(pose["rotation_deg"]))
        if block.get("frame") == "viewer":
            payload[PAYLOAD_KEY] = block
            return payload
        bbox = payload.get("bbox") if isinstance(payload.get("bbox"), dict) else None
        fp = block.get("model_footprint") if isinstance(block.get("model_footprint"), dict) else None
        if bbox and fp:
            lo, hi = bbox.get("min"), bbox.get("max")
            fmin, fmax = fp.get("min"), fp.get("max")
            if (
                isinstance(lo, list) and isinstance(hi, list)
                and isinstance(fmin, list) and isinstance(fmax, list)
                and len(lo) >= 2 and len(hi) >= 2 and len(fmin) >= 2 and len(fmax) >= 2
            ):
                dx = (float(lo[0]) + float(hi[0])) / 2.0 - (float(fmin[0]) + float(fmax[0])) / 2.0
                dy = (float(lo[1]) + float(hi[1])) / 2.0 - (float(fmin[1]) + float(fmax[1])) / 2.0
                shift_block(block, dx, dy)
        payload[PAYLOAD_KEY] = to_viewer_frame(block)
    except Exception:  # noqa: BLE001 — extras never break the stage
        logger.debug("slicer block not attached", exc_info=True)
    return payload


def attach_to_payload(
    payload: dict[str, Any] | None,
    gcode_path: str | os.PathLike[str] | None,
    *,
    max_segments: int = MAX_EXTRA_SEGMENTS,
) -> dict[str, Any] | None:
    """Stamp the viewer-frame block onto a ``kiln.mesh.v1`` *payload*.

    Aligned to the payload's OWN ``bbox`` — whatever centring the plate
    door already applied is therefore baked in.  When the slice says the
    slicer turned the part, the payload is turned the same way first
    (:func:`apply_pose_to_payload`), so the stage shows the part as it
    will print and the extras land around THAT.  A downgraded payload
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
            max_segments=max_segments, mesh_floor=payload_mesh_frame(payload),
        )
        pose = block.get("pose") if isinstance(block.get("pose"), dict) else None
        if block.get("available") and pose and pose.get("rotation_deg"):
            apply_pose_to_payload(payload, int(pose["rotation_deg"]))
        payload[PAYLOAD_KEY] = to_viewer_frame(block)
    except Exception:  # noqa: BLE001 — extras never break the stage
        logger.debug("slicer geometry not attached", exc_info=True)
    return payload
