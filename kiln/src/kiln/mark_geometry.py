"""Compile 2D marks (SVG vector art or bi-level raster logos) into OpenSCAD geometry.

The decoration pipeline carves or raises a *mark* — a logo, wordmark, or
piece of line art — on a model face.  The only construction that survives
every OpenSCAD version and every mesh complexity is native ``polygon()``
geometry inside a boolean (``import()`` of SVGs silently no-ops in
``difference()`` on some versions, and ``surface()`` heightmaps carve the
whole rectangular tile — background, frame and all — instead of just the
ink).  This module is the single compiler from *any* mark source to that
proven representation:

* :func:`parse_svg_to_mark` — a real SVG parser (paths with the full
  command set, shapes, groups, transforms, even-odd holes), replacing the
  regex extraction that only understood ``<polygon>``/``<rect>``/``<circle>``
  and therefore dropped every ``<path>``-based logo on the floor.
* :func:`trace_image_to_mark` — a bi-level raster tracer (threshold →
  boundary walk → simplification) so a PNG/JPG logo carves as crisp
  vector strokes instead of a pixel-stepped heightmap with a tile frame.

Both produce a :class:`MarkGeometry`: rings **centered on the origin** in
OpenSCAD's Y-up frame, with exact content bounds.  Centering here means
the downstream placement math cannot be wrong — the emboss generator's
translate/scale composes around ``(0, 0)`` regardless of viewBox origins,
whitespace padding, or image margins.

Zero third-party dependencies beyond Pillow (already optional-required by
the image pipeline); the SVG side is pure stdlib.
"""

from __future__ import annotations

import logging
import math
import re
import xml.etree.ElementTree as ET
from bisect import bisect_right
from dataclasses import dataclass, field

_logger = logging.getLogger(__name__)

Point = tuple[float, float]
Ring = list[Point]

# Curve flattening: segments per cubic/quadratic Bezier, and the maximum
# angular step when sampling elliptical arcs.  16 segments keeps chord
# error < 0.1% of the curve span — invisible at FDM scale — while staying
# tiny in the emitted SCAD.
_BEZIER_SEGMENTS = 16
_ARC_MAX_STEP_RAD = math.radians(6.0)

# Dash ceiling: a user-supplied SVG can pair a hairline dasharray with a
# long path, and every dash costs several polygons in the emitted SCAD.
# Past this many the mark stops being printable detail, so the stroke is
# drawn solid instead — loudly, never silently.
_MAX_DASH_RUNS = 2000

# A zero-length dash is SVG's dotted-line idiom ("0 12" with round caps):
# it renders as a bare linecap.  Giving it this much length lets the cap
# machinery draw the dot — far below one extruder track, so it never shows
# up as length in the print.
_DOT_EPS = 1e-6

# Speck filter: rings whose area is below (this fraction of the mark's
# bounding-box diagonal)² are anti-aliasing noise, not geometry.  Kept
# relative so tiny-viewBox icons don't lose real detail.
_SPECK_DIAG_FRACTION = 0.002


# ---------------------------------------------------------------------------
# MarkGeometry — the compiled result
# ---------------------------------------------------------------------------


@dataclass
class MarkGeometry:
    """Origin-centered 2D mark geometry ready for OpenSCAD emission.

    ``groups`` is a list of ring-lists.  Rings within one group are
    rendered with even-odd semantics (a ring inside another ring is a
    hole — one ``polygon(points, paths)`` call); separate groups union
    together (matching SVG paint order, where distinct elements overlay
    rather than XOR).  Raster traces always compile to a single group
    because traced boundaries can never overlap.
    """

    groups: list[list[Ring]] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0

    @property
    def is_empty(self) -> bool:
        return not any(self.groups)

    def content_bounds_info(self) -> dict:
        """Bounds in the shape the emboss generator consumes.

        Geometry is origin-centered by construction, so the content
        center the generator derives (``x_min + width/2``) is exactly
        ``(0, 0)`` and its centering translate becomes a no-op that can
        never mis-place the mark.
        """
        return {
            "content_x_min": round(-self.width / 2, 4),
            "content_y_min": round(-self.height / 2, 4),
            "content_width": round(self.width, 4),
            "content_height": round(self.height, 4),
        }

    def to_scad(self) -> str:
        """Emit ``union() { polygon(...); ... }`` for the whole mark.

        One ``polygon()`` per group with all of the group's rings in
        ``paths`` — OpenSCAD applies even-odd containment, which is how
        holes (letter counters, the inner band of an outlined mark)
        render correctly without any winding bookkeeping.

        The union is wrapped in a tiny ``offset(delta=+eps)``.  Art
        routinely contains sub-polygons that touch edge-on without
        overlapping (a glyph stem meeting its diagonal, tangent strokes);
        unioning tangent regions leaves a degenerate contact that
        extrudes into pinched edges — 4+ triangles meeting along one
        line, a non-manifold defect no mesh repair pass can sew (it is
        not a hole).  Growing the evaluated region by eps first turns
        every tangency into a real overlap, so the union comes out as one
        clean contour and the extrusion is watertight.  eps is 0.05% of
        the mark's largest dimension — microns at print scale, far below
        anything a nozzle can express.  Growing only (never shrinking
        back) is deliberate: a grow-then-shrink closing re-creates the
        near-tangency on the way back in.

        The output must NOT be wrapped in OpenSCAD ``fill()`` by callers:
        fill() erases exactly the holes this representation preserves.
        """
        parts: list[str] = []
        for rings in self.groups:
            rings = [r for r in rings if len(r) >= 3]
            if not rings:
                continue
            points: list[str] = []
            paths: list[str] = []
            idx = 0
            for ring in rings:
                points.extend(f"[{x:.4f},{y:.4f}]" for x, y in ring)
                paths.append(
                    "[" + ",".join(str(i) for i in range(idx, idx + len(ring))) + "]"
                )
                idx += len(ring)
            parts.append(
                "polygon(points=["
                + ",".join(points)
                + "], paths=["
                + ",".join(paths)
                + "], convexity=10);"
            )
        if not parts:
            return ""
        body = "\n    ".join(parts)
        eps = max(self.width, self.height) * 5e-4
        if eps > 0:
            return f"offset(delta={eps:.4f}) union() {{\n    {body}\n}}"
        return f"union() {{\n    {body}\n}}"

    def to_contour_groups(self) -> list[list[dict]] | None:
        """The mark as plain-data contours with RESOLVED hole/island roles.

        For consumers that rebuild the mark as explicit boolean geometry
        (an analytic CAD kernel, a clipper library) rather than rendering
        it through OpenSCAD's even-odd ``polygon(paths=...)``.  One inner
        list per group, mirroring :meth:`to_scad`'s union-of-groups; each
        entry is ``{"points": [[x, y], ...], "hole": bool}``, ordered by
        nesting depth so applying them in sequence — add the ring when
        ``hole`` is False, subtract when True — reproduces the group's
        even-odd fill exactly: outer contour minus its counters, with an
        enclosed island (a ring inside a hole) coming back as material.

        A partial overlap between SAME-winding rings is a filled-stroke
        crossing (a glyph's stem meeting its diagonal): SVG's nonzero
        fill renders it solid, so both rings stay additive and their
        union is the intent.  Returns ``None`` only when OPPOSITE-winding
        rings partially overlap — a hole crossing its own outer boundary
        — which neither nonzero fill nor nested contours can express
        coherently.  Disjoint and strictly nested rings — every real
        logo, wordmark, counter and island — resolve normally.
        """
        out_groups: list[list[dict]] = []
        for rings in self.groups:
            rings = [r for r in rings if len(r) >= 3]
            if not rings:
                continue
            signed = [_ring_area(r) for r in rings]
            areas = [abs(a) for a in signed]
            n = len(rings)
            depths = [0] * n
            for i in range(n):
                for j in range(n):
                    if i == j or areas[j] < areas[i]:
                        continue
                    if areas[j] == areas[i] and j < i:
                        continue  # equal-area pairs examined once
                    rel = _ring_containment(rings[i], rings[j])
                    if rel == "partial":
                        if signed[i] * signed[j] < 0:
                            return None
                        continue  # same winding: union — both stay additive
                    if rel == "inside" and areas[j] > areas[i]:
                        depths[i] += 1
            order = sorted(range(n), key=lambda k: depths[k])
            out_groups.append(
                [
                    {
                        "points": [
                            [round(x, 4), round(y, 4)] for x, y in rings[k]
                        ],
                        "hole": depths[k] % 2 == 1,
                    }
                    for k in order
                ]
            )
        return out_groups or None


def _finalize(
    groups: list[list[Ring]], *, min_ring_area: float | None = None
) -> MarkGeometry | None:
    """Flip Y (source frames are Y-down), drop specks, center, measure."""
    raw_pts = [pt for rings in groups for ring in rings for pt in ring]
    if not raw_pts:
        return None
    if min_ring_area is None:
        rxs = [p[0] for p in raw_pts]
        rys = [p[1] for p in raw_pts]
        diag = math.hypot(max(rxs) - min(rxs), max(rys) - min(rys))
        min_ring_area = (diag * _SPECK_DIAG_FRACTION) ** 2

    xs: list[float] = []
    ys: list[float] = []
    flipped: list[list[Ring]] = []
    for rings in groups:
        out_rings: list[Ring] = []
        for ring in rings:
            if len(ring) < 3 or abs(_ring_area(ring)) < min_ring_area:
                continue
            fr = [(x, -y) for x, y in ring]
            out_rings.append(fr)
            for x, y in fr:
                xs.append(x)
                ys.append(y)
        if out_rings:
            flipped.append(out_rings)
    if not xs:
        return None
    cx = (min(xs) + max(xs)) / 2
    cy = (min(ys) + max(ys)) / 2
    centered = [
        [[(x - cx, y - cy) for x, y in ring] for ring in rings] for rings in flipped
    ]
    return MarkGeometry(
        groups=centered,
        width=max(xs) - min(xs),
        height=max(ys) - min(ys),
    )


def _ring_area(ring: Ring) -> float:
    """Signed shoelace area."""
    a = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return a / 2


def _point_in_ring(x: float, y: float, ring: Ring) -> bool:
    """Even-odd ray cast — is (x, y) inside *ring*?"""
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xt = x1 + (y - y1) / (y2 - y1) * (x2 - x1)
            if x < xt:
                inside = not inside
    return inside


def _ring_containment(inner: Ring, outer: Ring) -> str:
    """``"inside"`` / ``"outside"`` / ``"partial"`` — *inner* vs *outer*.

    Samples edge MIDPOINTS of *inner* (a shared vertex between tangent
    contours sits exactly on the other ring's boundary, where a ray cast
    is ambiguous; a midpoint generically does not).  All-in is nested,
    all-out is disjoint, a genuine mix is a partial overlap — which
    even-odd XORs and nested contours cannot express.  One or two
    ambiguous samples out of many are tolerated as tangency noise.
    """
    n = len(inner)
    step = max(1, n // 9)
    hits = 0
    total = 0
    for i in range(0, n, step):
        x1, y1 = inner[i]
        x2, y2 = inner[(i + 1) % n]
        if _point_in_ring((x1 + x2) / 2, (y1 + y2) / 2, outer):
            hits += 1
        total += 1
    if total == 0:
        return "outside"
    frac = hits / total
    if frac >= 0.8:
        return "inside"
    if frac <= 0.2:
        return "outside"
    return "partial"


# ---------------------------------------------------------------------------
# SVG side
# ---------------------------------------------------------------------------

_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)  # (a, b, c, d, e, f) column-major SVG matrix


def _mat_mul(m1: tuple, m2: tuple) -> tuple:
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def _mat_apply(m: tuple, x: float, y: float) -> Point:
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def _parse_transform(text: str) -> tuple:
    """Compose an SVG ``transform`` attribute into one affine matrix."""
    m = _IDENTITY
    for op, args_s in re.findall(r"(\w+)\s*\(([^)]*)\)", text or ""):
        args = [float(v) for v in re.findall(r"[-+0-9.eE]+", args_s)]
        op = op.lower()
        if op == "matrix" and len(args) == 6:
            t = tuple(args)
        elif op == "translate":
            tx = args[0] if args else 0.0
            ty = args[1] if len(args) > 1 else 0.0
            t = (1, 0, 0, 1, tx, ty)
        elif op == "scale":
            sx = args[0] if args else 1.0
            sy = args[1] if len(args) > 1 else sx
            t = (sx, 0, 0, sy, 0, 0)
        elif op == "rotate":
            ang = math.radians(args[0]) if args else 0.0
            ca, sa = math.cos(ang), math.sin(ang)
            t = (ca, sa, -sa, ca, 0, 0)
            if len(args) >= 3:
                cx, cy = args[1], args[2]
                t = _mat_mul(_mat_mul((1, 0, 0, 1, cx, cy), t), (1, 0, 0, 1, -cx, -cy))
        elif op == "skewx" and args:
            t = (1, 0, math.tan(math.radians(args[0])), 1, 0, 0)
        elif op == "skewy" and args:
            t = (1, math.tan(math.radians(args[0])), 0, 1, 0, 0)
        else:
            continue
        m = _mat_mul(m, t)
    return m


_WHITE_FILLS = {"#fff", "#ffffff", "white", "rgb(255,255,255)", "rgb(255, 255, 255)"}


def _resolve_paint(elem: ET.Element, inherited: dict) -> dict:
    """Resolve fill/stroke with CSS-attribute + style= + inheritance."""
    paint = dict(inherited)
    style = elem.get("style", "")
    decls = {}
    for part in style.split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            decls[k.strip().lower()] = v.strip()
    for key in (
        "fill", "stroke", "stroke-width", "stroke-linecap",
        "stroke-linejoin", "stroke-miterlimit",
        "stroke-dasharray", "stroke-dashoffset",
        "opacity", "fill-opacity", "stroke-opacity", "display",
    ):
        val = decls.get(key, elem.get(key))
        if val is not None:
            paint[key] = val
    return paint


def _elem_prop(elem: ET.Element, key: str) -> str | None:
    """Read a NON-inherited presentation property off one element.

    ``_resolve_paint`` is for properties that cascade; this is for the ones
    that apply only where they are written, such as ``vector-effect``.
    """
    for part in elem.get("style", "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            if k.strip().lower() == key:
                return v.strip()
    return elem.get(key)


def _num(value: str | None, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(re.sub(r"(px|pt|mm|cm|in)$", "", value.strip()))
    except ValueError:
        return default


_FLOAT_RE = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")


def _flatten_cubic(p0: Point, p1: Point, p2: Point, p3: Point) -> list[Point]:
    pts = []
    for i in range(1, _BEZIER_SEGMENTS + 1):
        t = i / _BEZIER_SEGMENTS
        mt = 1 - t
        x = (
            mt * mt * mt * p0[0]
            + 3 * mt * mt * t * p1[0]
            + 3 * mt * t * t * p2[0]
            + t * t * t * p3[0]
        )
        y = (
            mt * mt * mt * p0[1]
            + 3 * mt * mt * t * p1[1]
            + 3 * mt * t * t * p2[1]
            + t * t * t * p3[1]
        )
        pts.append((x, y))
    return pts


def _flatten_quad(p0: Point, p1: Point, p2: Point) -> list[Point]:
    pts = []
    for i in range(1, _BEZIER_SEGMENTS + 1):
        t = i / _BEZIER_SEGMENTS
        mt = 1 - t
        x = mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0]
        y = mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1]
        pts.append((x, y))
    return pts


def _flatten_arc(
    p0: Point,
    rx: float,
    ry: float,
    rot_deg: float,
    large_arc: bool,
    sweep: bool,
    p1: Point,
) -> list[Point]:
    """SVG endpoint arc → sampled polyline (W3C endpoint→center conversion)."""
    if rx == 0 or ry == 0 or p0 == p1:
        return [p1]
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(rot_deg)
    cphi, sphi = math.cos(phi), math.sin(phi)
    dx2, dy2 = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1p = cphi * dx2 + sphi * dy2
    y1p = -sphi * dx2 + cphi * dy2
    lam = (x1p / rx) ** 2 + (y1p / ry) ** 2
    if lam > 1:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large_arc == sweep:
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cphi * cxp - sphi * cyp + (p0[0] + p1[0]) / 2
    cy = sphi * cxp + cphi * cyp + (p0[1] + p1[1]) / 2

    def _angle(ux: float, uy: float, vx: float, vy: float) -> float:
        dot = ux * vx + uy * vy
        norm = math.hypot(ux, uy) * math.hypot(vx, vy)
        ang = math.acos(max(-1.0, min(1.0, dot / norm))) if norm else 0.0
        return -ang if ux * vy - uy * vx < 0 else ang

    theta1 = _angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dtheta = _angle(
        (x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry
    )
    if not sweep and dtheta > 0:
        dtheta -= 2 * math.pi
    elif sweep and dtheta < 0:
        dtheta += 2 * math.pi
    steps = max(2, int(math.ceil(abs(dtheta) / _ARC_MAX_STEP_RAD)))
    pts = []
    for i in range(1, steps + 1):
        th = theta1 + dtheta * i / steps
        xe = cx + rx * math.cos(th) * cphi - ry * math.sin(th) * sphi
        ye = cy + rx * math.cos(th) * sphi + ry * math.sin(th) * cphi
        pts.append((xe, ye))
    pts[-1] = p1  # land exactly on the endpoint
    return pts


def _parse_path_d(d: str) -> list[tuple[Ring, bool]]:
    """Interpret a path ``d`` attribute into ``(points, was_closed)`` subpaths.

    Supports the full SVG 1.1 command set with relative variants and
    implicit command repetition.  ``was_closed`` is True for an explicit
    ``Z`` (or a subpath that returns to its start point).  Fill consumers
    treat every subpath as closed — a filled path renders as if closed —
    but stroke consumers must keep the distinction: sealing an open
    stroked path draws a stroke-width band across a deliberate gap.
    """
    tokens: list[str] = []
    for m in re.finditer(r"[MmLlHhVvCcSsQqTtAaZz]|" + _FLOAT_RE.pattern, d):
        tokens.append(m.group(0))
    try:
        return _interpret_path_tokens(tokens)
    except (IndexError, ValueError):
        _logger.debug("Malformed path data — element skipped", exc_info=True)
        return []


def _interpret_path_tokens(tokens: list[str]) -> list[tuple[Ring, bool]]:
    subpaths: list[tuple[Ring, bool]] = []
    ring: Ring = []
    cur: Point = (0.0, 0.0)
    start: Point = (0.0, 0.0)
    prev_cubic_ctrl: Point | None = None
    prev_quad_ctrl: Point | None = None
    i = 0
    cmd = ""

    def _next_floats(n: int) -> list[float]:
        nonlocal i
        vals = [float(tokens[i + k]) for k in range(n)]
        i += n
        return vals

    def _end_subpath(explicit_close: bool) -> None:
        nonlocal ring
        if explicit_close:
            if len(ring) >= 3:
                subpaths.append((ring, True))
        elif len(ring) >= 2:
            # No Z: closed only if the subpath returns to its start.
            returns_to_start = (
                len(ring) >= 3
                and math.hypot(ring[0][0] - ring[-1][0], ring[0][1] - ring[-1][1]) < 1e-9
            )
            subpaths.append((ring, returns_to_start))
        ring = []

    while i < len(tokens):
        tok = tokens[i]
        if re.match(r"^[A-Za-z]$", tok):
            cmd = tok
            i += 1
            if cmd in "Zz":
                cur = start
                _end_subpath(True)
                continue
        elif not cmd:
            break

        rel = cmd.islower()
        c = cmd.upper()
        if c == "M":
            x, y = _next_floats(2)
            if rel:
                x, y = cur[0] + x, cur[1] + y
            _end_subpath(False)
            cur = start = (x, y)
            ring = [cur]
            cmd = "l" if rel else "L"  # subsequent implicit pairs are linetos
        elif c == "L":
            x, y = _next_floats(2)
            if rel:
                x, y = cur[0] + x, cur[1] + y
            cur = (x, y)
            ring.append(cur)
        elif c == "H":
            (x,) = _next_floats(1)
            cur = (cur[0] + x if rel else x, cur[1])
            ring.append(cur)
        elif c == "V":
            (y,) = _next_floats(1)
            cur = (cur[0], cur[1] + y if rel else y)
            ring.append(cur)
        elif c in ("C", "S"):
            if c == "C":
                x1, y1, x2, y2, x, y = _next_floats(6)
            else:
                x2, y2, x, y = _next_floats(4)
                if prev_cubic_ctrl is not None:
                    x1, y1 = 2 * cur[0] - prev_cubic_ctrl[0], 2 * cur[1] - prev_cubic_ctrl[1]
                else:
                    x1, y1 = cur
                if rel:
                    x1, y1 = x1 - cur[0], y1 - cur[1]  # keep in relative frame below
            if rel:
                x1, y1 = cur[0] + x1, cur[1] + y1
                x2, y2 = cur[0] + x2, cur[1] + y2
                x, y = cur[0] + x, cur[1] + y
            pts = _flatten_cubic(cur, (x1, y1), (x2, y2), (x, y))
            ring.extend(pts)
            prev_cubic_ctrl = (x2, y2)
            prev_quad_ctrl = None
            cur = (x, y)
            continue  # control-point bookkeeping already done
        elif c in ("Q", "T"):
            if c == "Q":
                x1, y1, x, y = _next_floats(4)
                if rel:
                    x1, y1 = cur[0] + x1, cur[1] + y1
            else:
                x, y = _next_floats(2)
                if prev_quad_ctrl is not None:
                    x1, y1 = 2 * cur[0] - prev_quad_ctrl[0], 2 * cur[1] - prev_quad_ctrl[1]
                else:
                    x1, y1 = cur
            if rel:
                x, y = cur[0] + x, cur[1] + y
            pts = _flatten_quad(cur, (x1, y1), (x, y))
            ring.extend(pts)
            prev_quad_ctrl = (x1, y1)
            prev_cubic_ctrl = None
            cur = (x, y)
            continue
        elif c == "A":
            rx, ry, rot, laf, sf, x, y = _next_floats(7)
            if rel:
                x, y = cur[0] + x, cur[1] + y
            pts = _flatten_arc(cur, rx, ry, rot, laf != 0, sf != 0, (x, y))
            ring.extend(pts)
            cur = (x, y)
        else:
            i += 1  # unknown token — skip defensively
            continue
        prev_cubic_ctrl = None
        prev_quad_ctrl = None

    _end_subpath(False)
    return subpaths


def _dedupe_consecutive(pts: list[Point], closed: bool) -> list[Point]:
    """Drop repeated points — they carry no direction, so joins on them
    would be degenerate (and a closed subpath that restates its start
    would grow a zero-length closing segment)."""
    clean: list[Point] = [pts[0]]
    for p in pts[1:]:
        if math.hypot(p[0] - clean[-1][0], p[1] - clean[-1][1]) > 1e-9:
            clean.append(p)
    if (
        closed
        and len(clean) > 1
        and math.hypot(clean[0][0] - clean[-1][0], clean[0][1] - clean[-1][1]) < 1e-9
    ):
        clean.pop()
    return clean


def _parse_dasharray(value: str | None) -> list[float] | None:
    """Interpret ``stroke-dasharray`` into dash/gap lengths, or None for solid.

    Per SVG an odd-length list repeats to make dash/gap pairs, and a list
    containing a negative value is invalid — invalid means solid, not
    unrendered.  Percentages resolve against the viewport diagonal, which
    this parser does not track, so they fall back to solid rather than
    silently dashing at the wrong scale.
    """
    if not value:
        return None
    text = value.strip().lower()
    if text in ("none", "inherit") or "%" in text:
        return None
    vals = [float(m.group(0)) for m in _FLOAT_RE.finditer(text)]
    if not vals or any(v < 0 for v in vals) or sum(vals) <= 0:
        return None
    return vals * 2 if len(vals) % 2 else vals


def _dash_runs(
    pts: list[Point], closed: bool, pattern: list[float], offset: float
) -> list[list[Point]] | None:
    """Split a polyline into the drawn runs of a dash pattern.

    Returns None when the pattern would produce more runs than the mark
    can carry, so the caller can fall back to a solid stroke.
    """
    path = [*pts, pts[0]] if closed else list(pts)
    cum = [0.0]
    for a, b in zip(path, path[1:], strict=False):
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    total = cum[-1]
    period = sum(pattern)
    if total <= 0 or period <= 0:
        return None

    def _at(dist: float) -> Point:
        i = min(max(bisect_right(cum, dist) - 1, 0), len(path) - 2)
        span = cum[i + 1] - cum[i]
        t = 0.0 if span <= 0 else (dist - cum[i]) / span
        return (
            path[i][0] + (path[i + 1][0] - path[i][0]) * t,
            path[i][1] + (path[i + 1][1] - path[i][1]) * t,
        )

    runs: list[list[Point]] = []
    # dashoffset is a distance INTO the pattern at the path's start, so the
    # pattern's own origin sits that far back along the path.
    cycle = -(offset % period)
    while cycle < total:
        pos = cycle
        for i, length in enumerate(pattern):
            lo, hi = pos, pos + length
            pos = hi
            if i % 2 or lo > total or hi < 0:
                continue  # odd entries are gaps; even ones may fall off the path
            is_dot = length <= 0  # a dot is authored zero-length, not clipped to it
            lo, hi = max(lo, 0.0), min(hi, total)
            if not is_dot and hi - lo <= _DOT_EPS:
                continue  # the path boundary trimmed this dash away entirely
            if is_dot:
                # Zero-length dash: a dot, drawn by the linecap alone.
                a = _at(lo)
                b = _at(min(lo + _DOT_EPS, total))
                if a == b:
                    b = _at(max(lo - _DOT_EPS, 0.0))
                if a == b:
                    continue
                run = [a, b]
            else:
                run = [_at(lo)]
                run.extend(p for k, p in enumerate(path) if lo < cum[k] < hi)
                run.append(_at(hi))
            runs.append(run)
            if len(runs) > _MAX_DASH_RUNS:
                return None
        cycle += period
    return runs


def _stroke_segments_to_rings(
    pts: list[Point],
    width: float,
    closed: bool,
    linecap: str = "butt",
    linejoin: str = "miter",
    miterlimit: float = 4.0,
    dasharray: list[float] | None = None,
    dashoffset: float = 0.0,
) -> list[Ring]:
    """Expand a stroked polyline into filled quads + join/cap geometry.

    One quad per segment, plus a plug at each interior joint for the wedge
    the two quads leave on the outside of the turn (shaped by
    ``stroke-linejoin``), plus an end treatment on open subpaths (shaped by
    ``stroke-linecap``).  All rings UNION — the caller emits each as its own
    even-odd group.
    """
    if width <= 0 or len(pts) < 2:
        return []
    pts = _dedupe_consecutive(pts, closed)
    if closed and len(pts) < 3:
        closed = False  # nothing left to close around
    if len(pts) < 2:
        return []

    if dasharray:
        runs = _dash_runs(pts, closed, dasharray, dashoffset)
        if runs is None:
            _logger.warning(
                "stroke-dasharray produces over %d dashes — drawing the stroke "
                "solid instead", _MAX_DASH_RUNS,
            )
        else:
            # Each dash is its own open run: capped at both ends, joined
            # wherever it happens to span one of the original vertices.
            rings: list[Ring] = []
            for run in runs:
                # A butt cap on a zero-length dash draws nothing.  The bound
                # is loose because interpolating the dot's two points back
                # out of the path leaves float dust on its length.
                if linecap == "butt" and len(run) == 2 and (
                    math.hypot(run[1][0] - run[0][0], run[1][1] - run[0][1])
                    <= _DOT_EPS * 4
                ):
                    continue
                rings.extend(
                    _stroke_segments_to_rings(
                        run, width, False, linecap, linejoin, miterlimit
                    )
                )
            return rings

    rings: list[Ring] = []
    hw = width / 2

    def _arc(center: Point, start_angle: float, sweep: float) -> Ring:
        steps = max(1, math.ceil(abs(sweep) / _ARC_MAX_STEP_RAD))
        return [
            (
                center[0] + hw * math.cos(start_angle + sweep * k / steps),
                center[1] + hw * math.sin(start_angle + sweep * k / steps),
            )
            for k in range(steps + 1)
        ]

    seg_pairs = list(zip(pts, pts[1:], strict=False))
    if closed:
        seg_pairs.append((pts[-1], pts[0]))
    dirs: list[Point] = []
    for (x1, y1), (x2, y2) in seg_pairs:
        dx, dy = x2 - x1, y2 - y1
        ln = math.hypot(dx, dy)
        dirs.append((dx / ln, dy / ln))
        nx, ny = -dy / ln * hw, dx / ln * hw
        rings.append(
            [
                (x1 + nx, y1 + ny),
                (x2 + nx, y2 + ny),
                (x2 - nx, y2 - ny),
                (x1 - nx, y1 - ny),
            ]
        )

    def _join_ring(p: Point, d1: Point, d2: Point) -> Ring | None:
        cross = d1[0] * d2[1] - d1[1] * d2[0]
        dot = d1[0] * d2[0] + d1[1] * d2[1]
        if abs(cross) < 1e-12 and dot > 0:
            return None  # straight through — the quads already abut
        # The wedge opens on the outside of the turn: right of a left turn.
        side = -1.0 if cross > 0 else 1.0
        u1 = (-d1[1] * side, d1[0] * side)  # unit outward normals
        u2 = (-d2[1] * side, d2[0] * side)
        o1 = (p[0] + u1[0] * hw, p[1] + u1[1] * hw)
        o2 = (p[0] + u2[0] * hw, p[1] + u2[1] * hw)

        join = linejoin
        if join == "miter":
            denom = 1.0 + u1[0] * u2[0] + u1[1] * u2[1]
            if denom > 1e-9:
                mx = (u1[0] + u2[0]) / denom * hw
                my = (u1[1] + u2[1]) / denom * hw
                # |M - P| / hw is exactly SVG's miter ratio, 1/sin(theta/2).
                if math.hypot(mx, my) <= miterlimit * hw:
                    return [p, o1, (p[0] + mx, p[1] + my), o2]
            join = "bevel"  # spike past the limit, or a 180° cusp
        if join == "round":
            a1 = math.atan2(u1[1], u1[0])
            a2 = math.atan2(u2[1], u2[0])
            sweep = (a2 - a1 + math.pi) % (2 * math.pi) - math.pi  # short way
            return [p, *_arc(p, a1, sweep)]
        return [p, o1, o2]  # bevel

    joint_idx = range(len(pts)) if closed else range(1, len(pts) - 1)
    for i in joint_idx:
        ring = _join_ring(pts[i], dirs[i - 1], dirs[i])
        # A 180° cusp bevels to three collinear points — no area, and a
        # degenerate polygon downstream.  The segment quads already meet
        # cleanly there, so dropping it loses nothing.
        if ring and abs(_ring_area(ring)) > 1e-12:
            rings.append(ring)

    if not closed and linecap in ("round", "square"):
        # Outward direction at each end: away from the neighbouring point.
        for end, out in ((pts[0], (-dirs[0][0], -dirs[0][1])), (pts[-1], dirs[-1])):
            nx, ny = -out[1] * hw, out[0] * hw
            if linecap == "round":
                rings.append(_arc(end, math.atan2(ny, nx), -math.pi))
                continue
            ux, uy = out[0] * hw, out[1] * hw  # extend half a width
            rings.append(
                [
                    (end[0] + nx, end[1] + ny),
                    (end[0] + nx + ux, end[1] + ny + uy),
                    (end[0] - nx + ux, end[1] - ny + uy),
                    (end[0] - nx, end[1] - ny),
                ]
            )
    return rings


def parse_svg_to_mark(
    svg_text: str, *, min_stroke_units: float = 0.0
) -> MarkGeometry | None:
    """Parse SVG markup into origin-centered mark geometry.

    Returns ``None`` when no fillable/strokable geometry could be
    extracted (e.g. an SVG made entirely of ``<text>``) — callers fall
    back to OpenSCAD's own ``import()`` in that case.

    White fills are treated as background and skipped (standard logo
    semantics: dark ink on light ground).  Subpaths of one element keep
    even-odd containment, so letter counters and outlined bands carve as
    true holes.
    """
    # XML hardening for user-supplied SVGs: stdlib ElementTree already
    # refuses external-entity expansion, and billion-laughs requires
    # <!ENTITY> declarations — which no legitimate SVG carries.  Refuse
    # entity declarations outright and drop the (unused) DOCTYPE so the
    # parser never sees an internal subset.
    if re.search(r"<!ENTITY", svg_text, re.IGNORECASE):
        _logger.warning("SVG rejected: entity declarations are not allowed")
        return None
    svg_text = re.sub(r"<!DOCTYPE[^>[]*(\[[^\]]*\])?[^>]*>", "", svg_text, flags=re.IGNORECASE | re.DOTALL)
    try:
        root = ET.fromstring(svg_text)
    except ET.ParseError as exc:
        _logger.debug("SVG parse failed: %s", exc)
        return None

    groups: list[list[Ring]] = []
    skip_tags = {
        "defs", "clippath", "mask", "symbol", "marker", "pattern",
        "metadata", "title", "desc", "style", "text", "tspan", "script",
    }

    def _walk(elem: ET.Element, matrix: tuple, paint: dict) -> None:
        tag = elem.tag.rsplit("}", 1)[-1].lower()
        if tag in skip_tags:
            return
        paint = _resolve_paint(elem, paint)
        if paint.get("display", "").lower() == "none":
            return
        matrix = _mat_mul(matrix, _parse_transform(elem.get("transform", "")))

        fill = (paint.get("fill") or "black").strip().lower()
        stroke = (paint.get("stroke") or "none").strip().lower()
        has_fill = fill not in ("none", "transparent") and fill not in _WHITE_FILLS
        has_stroke = stroke not in ("none", "transparent") and stroke not in _WHITE_FILLS
        # Carved geometry is binary, so only fully transparent paint can be
        # honored — a half-opaque mark still has to be one depth or none.
        if _num(paint.get("opacity"), 1.0) == 0 or _num(paint.get("fill-opacity"), 1.0) == 0:
            has_fill = False
        if _num(paint.get("opacity"), 1.0) == 0 or _num(paint.get("stroke-opacity"), 1.0) == 0:
            has_stroke = False

        stroke_w = _num(paint.get("stroke-width"), 1.0)
        if (_elem_prop(elem, "vector-effect") or "").strip().lower() == "non-scaling-stroke":
            # The width is meant to survive ancestor transforms, so pre-divide
            # it by their scale and let the transform put it back.  Exact for
            # any similarity (translate/rotate/uniform scale); under an
            # anisotropic scale one scalar width cannot be right in both axes,
            # so sqrt|det| puts it on the geometric mean of the two.
            det = abs(matrix[0] * matrix[3] - matrix[1] * matrix[2])
            if det > 1e-12:
                stroke_w /= math.sqrt(det)
        stroke_w = max(stroke_w, min_stroke_units)
        linecap = (paint.get("stroke-linecap") or "butt").strip().lower()
        if linecap not in ("butt", "round", "square"):
            linecap = "butt"
        linejoin = (paint.get("stroke-linejoin") or "miter").strip().lower()
        if linejoin not in ("miter", "round", "bevel"):
            # SVG2 adds miter-clip/arcs; both degrade to their miter/round base.
            linejoin = "round" if linejoin == "arcs" else "miter"
        miterlimit = max(1.0, _num(paint.get("stroke-miterlimit"), 4.0))
        dasharray = _parse_dasharray(paint.get("stroke-dasharray"))
        dashoffset = _num(paint.get("stroke-dashoffset"), 0.0)

        local_rings: list[Ring] = []
        stroke_pts: list[tuple[list[Point], bool]] = []  # (points, closed)

        if tag == "path":
            subpaths = _parse_path_d(elem.get("d", ""))
            if has_fill:
                # Fill renders open subpaths as if closed — auto-close.
                local_rings.extend(r for r, _ in subpaths if len(r) >= 3)
            elif has_stroke:
                stroke_pts.extend(subpaths)
        elif tag in ("polygon", "polyline"):
            pts = []
            for m in _FLOAT_RE.finditer(elem.get("points", "")):
                pts.append(float(m.group(0)))
            pairs = [(pts[k], pts[k + 1]) for k in range(0, len(pts) - 1, 2)]
            closed = tag == "polygon"
            if has_fill and len(pairs) >= 3:
                local_rings.append(pairs)
            elif has_stroke and len(pairs) >= 2:
                stroke_pts.append((pairs, closed))
        elif tag == "rect":
            x, y = _num(elem.get("x")), _num(elem.get("y"))
            w, h = _num(elem.get("width")), _num(elem.get("height"))
            if w > 0 and h > 0:
                ring = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
                if has_fill:
                    local_rings.append(ring)
                elif has_stroke:
                    stroke_pts.append((ring, True))
        elif tag in ("circle", "ellipse"):
            cx, cy = _num(elem.get("cx")), _num(elem.get("cy"))
            rx = _num(elem.get("r")) if tag == "circle" else _num(elem.get("rx"))
            ry = _num(elem.get("r")) if tag == "circle" else _num(elem.get("ry"))
            if rx > 0 and ry > 0:
                ring = [
                    (cx + rx * math.cos(2 * math.pi * k / 64),
                     cy + ry * math.sin(2 * math.pi * k / 64))
                    for k in range(64)
                ]
                if has_fill:
                    local_rings.append(ring)
                elif has_stroke:
                    stroke_pts.append((ring, True))
        elif tag == "line":
            pts = [
                (_num(elem.get("x1")), _num(elem.get("y1"))),
                (_num(elem.get("x2")), _num(elem.get("y2"))),
            ]
            if has_stroke:
                stroke_pts.append((pts, False))

        if local_rings:
            groups.append(
                [[_mat_apply(matrix, x, y) for x, y in ring] for ring in local_rings]
            )
        for pts, closed in stroke_pts:
            expanded = _stroke_segments_to_rings(
                pts, stroke_w, closed, linecap, linejoin, miterlimit,
                dasharray, dashoffset,
            )
            if expanded:
                # Stroke quads overlap at joints — they must UNION, not
                # XOR, so each quad/octagon is its own even-odd group.
                for ring in expanded:
                    groups.append([[_mat_apply(matrix, x, y) for x, y in ring]])

        for child in list(elem):
            _walk(child, matrix, paint)

    _walk(root, _IDENTITY, {})
    return _finalize(groups)


# ---------------------------------------------------------------------------
# Raster side — bi-level trace
# ---------------------------------------------------------------------------


def _turn_angle(prev: Point, cur: Point, cand: Point) -> float:
    """CCW turn angle from the incoming direction to a candidate edge."""
    dx, dy = cur[0] - prev[0], cur[1] - prev[1]
    vx, vy = cand[0] - cur[0], cand[1] - cur[1]
    return math.atan2(dx * vy - dy * vx, dx * vx + dy * vy)


def _otsu_threshold(hist: list[int]) -> int:
    """Otsu's method on a 256-bin histogram.

    Ties resolve to the MIDDLE of the maximal-variance plateau: on a
    clean two-spike histogram every split between the spikes scores the
    same, and taking the first would park the threshold ON the dark
    spike — misclassifying it in margin-based checks downstream.
    """
    total = sum(hist)
    if total == 0:
        return 128
    sum_all = sum(i * h for i, h in enumerate(hist))
    sum_b = 0.0
    w_b = 0
    best_lo, best_hi, best_var = 128, 128, -1.0
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best_var + 1e-9:
            best_var, best_lo, best_hi = var, t, t
        elif var >= best_var - 1e-9:
            best_hi = t
    return (best_lo + best_hi) // 2


def _load_flattened_grayscale(image_path: str, max_dim: int):
    """Open → EXIF-orient → resolve transparency → grayscale → bound size.

    Transparency goes through the one shared resolver
    (:func:`kiln.image_to_surface._flatten_alpha_on_white`) so the trace
    door and the heightmap door cannot drift apart: both composite onto
    white, and both fall back to alpha-as-ink when a white or near-white
    mark on a transparent surround would otherwise vanish entirely.  This
    module used to carry its own inline copy of the composite, which is
    exactly how the two doors diverge.
    """
    from PIL import Image, ImageOps

    from kiln.image_to_surface import _flatten_alpha_on_white

    img = ImageOps.exif_transpose(Image.open(image_path))
    img = _flatten_alpha_on_white(img).convert("L")
    if max(img.size) > max_dim:
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)
    return img


def is_bilevel_image(image_path: str) -> bool:
    """Heuristic: is this a logo/mark (two tonal populations) vs a photo?

    A mark thresholds cleanly: after an Otsu split, nearly every pixel
    sits close to its side's population.  Photos have broad mid-tone
    mass and fail the envelope.  Used by ``image_style="auto"`` routing.
    """
    try:
        img = _load_flattened_grayscale(image_path, 256)
    except Exception:  # noqa: BLE001 — unreadable image: let the main path report it
        return False
    hist = img.histogram()
    total = sum(hist)
    if not total:
        return False
    t = _otsu_threshold(hist)
    dark = sum(hist[:t + 1])
    # Pixels far from the threshold on either side — i.e. decisively
    # black-side or white-side.  Anti-aliased edges are a thin minority.
    margin = 36
    decisive = sum(hist[: max(0, t - margin)]) + sum(hist[min(256, t + margin):])
    ink_fraction = min(dark, total - dark) / total
    return decisive / total >= 0.88 and ink_fraction >= 0.002


def _dp_open_chain(chain: Ring, epsilon: float) -> Ring:
    """Iterative Douglas-Peucker on an open polyline (recursion-safe)."""
    n = len(chain)
    if n <= 2:
        return list(chain)
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi - lo < 2:
            continue
        (x1, y1), (x2, y2) = chain[lo], chain[hi]
        dx, dy = x2 - x1, y2 - y1
        ln = math.hypot(dx, dy)
        best_d, best_i = -1.0, lo
        for i in range(lo + 1, hi):
            px, py = chain[i]
            d = (
                abs(dy * px - dx * py + x2 * y1 - y2 * x1) / ln
                if ln > 1e-12
                else math.hypot(px - x1, py - y1)
            )
            if d > best_d:
                best_d, best_i = d, i
        if best_d > epsilon:
            keep[best_i] = True
            stack.append((lo, best_i))
            stack.append((best_i, hi))
    return [chain[i] for i in range(n) if keep[i]]


def _simplify_ring(ring: Ring, epsilon: float) -> Ring:
    """Douglas-Peucker for a closed ring (split at two extreme anchors)."""
    n = len(ring)
    if n <= 4:
        return ring
    # Anchor 1: index 0.  Anchor 2: farthest point from anchor 1.
    ax, ay = ring[0]
    far_i = max(range(n), key=lambda i: (ring[i][0] - ax) ** 2 + (ring[i][1] - ay) ** 2)
    if far_i == 0:
        return ring
    half1 = _dp_open_chain(ring[: far_i + 1], epsilon)
    half2 = _dp_open_chain(ring[far_i:] + [ring[0]], epsilon)
    return half1[:-1] + half2[:-1]


def trace_image_to_mark(
    image_path: str,
    *,
    max_dim: int = 800,
    threshold: int | None = None,
    simplify_px: float = 0.75,
) -> MarkGeometry | None:
    """Trace a bi-level raster into crisp, origin-centered polygon rings.

    Threshold (Otsu by default) → walk the ink/ground boundary along
    pixel edges (ink kept on the left, so outers come out CCW and holes
    CW) → Douglas-Peucker at sub-pixel epsilon, which collapses the
    pixel staircase into straight strokes and smooth curves.  The result
    carves ONLY the ink: no tile frame, no background carve, no mirror —
    and a tiny mesh instead of a 100k-triangle heightmap.

    Returns ``None`` when the image is unreadable or traces to nothing.
    """
    try:
        from PIL import ImageFilter
    except ImportError:
        _logger.warning(
            "Raster mark tracing requires Pillow. Install with: pip install pillow"
        )
        return None
    try:
        img = _load_flattened_grayscale(image_path, max_dim)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Cannot read image for tracing: %s", exc)
        return None
    img = img.filter(ImageFilter.MedianFilter(3))  # despeckle before threshold
    w, h = img.size
    if w < 2 or h < 2:
        return None
    thr = threshold if threshold is not None else _otsu_threshold(img.histogram())
    px = img.load()

    def ink(c: int, r: int) -> bool:
        return 0 <= c < w and 0 <= r < h and px[c, r] <= thr

    # Directed boundary edges on the pixel-corner lattice, ink on the
    # left, in math coordinates (y up): pixel (c, r) spans
    # x ∈ [c, c+1], y ∈ [h-1-r, h-r].
    edges: dict[Point, list[Point]] = {}

    def _add(p1: Point, p2: Point) -> None:
        edges.setdefault(p1, []).append(p2)

    for r in range(h):
        y0, y1 = float(h - 1 - r), float(h - r)
        for c in range(w):
            if not ink(c, r):
                continue
            x0, x1 = float(c), float(c + 1)
            if not ink(c, r + 1):
                _add((x0, y0), (x1, y0))  # bottom, +x
            if not ink(c + 1, r):
                _add((x1, y0), (x1, y1))  # right, +y
            if not ink(c, r - 1):
                _add((x1, y1), (x0, y1))  # top, -x
            if not ink(c - 1, r):
                _add((x0, y1), (x0, y0))  # left, -y

    if not edges:
        return None

    def _walk_loop(start: Point, first: Point) -> Ring:
        ring: Ring = [start]
        prev, cur = start, first
        while cur != start:
            ring.append(cur)
            outs = edges.get(cur, [])
            if not outs:
                return []  # broken chain — shouldn't happen on a valid mask
            if len(outs) == 1:
                nxt = outs.pop()
            else:
                # Saddle corner (diagonal ink touch): prefer the sharpest
                # left turn so the two loops stay separate simple rings.
                nxt = max(outs, key=lambda cand: _turn_angle(prev, cur, cand))
                outs.remove(nxt)
            prev, cur = cur, nxt
        return ring

    rings: list[Ring] = []
    for start in list(edges.keys()):
        while edges.get(start):
            first = edges[start].pop()
            loop = _walk_loop(start, first)
            if len(loop) >= 4:
                rings.append(loop)
    # Tidy the edge map as loops consume entries
    rings = [_simplify_ring(r, simplify_px) for r in rings]

    # Rings are already in math coords (y up); _finalize flips y, so
    # pre-flip back to image orientation to cancel it out.
    rings = [[(x, -y) for x, y in ring] for ring in rings]
    return _finalize([rings])
