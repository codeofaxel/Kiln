"""How finely Kiln cuts a curve into flat facets.

A mesh has no curves: every circle, cylinder, sphere and sweep is a ring
of flat facets, and the slicer traces those flats exactly -- measured
2026-09-22, a 45 mm circle meshed as 60 facets slices to 60 straight
moves per layer, turning 6 degrees at every corner, in both PrusaSlicer
2.9.4 and OrcaSlicer 2.3.2.  Every layer turns at the same angles, so the
corners stack into vertical lines up the part.

One rule decides the count, by physical size, for every curve:

* :data:`FACET_WIDTH_MM` -- no facet wider than a 0.4 mm nozzle.  A flat
  narrower than the bead that draws it cannot print as a flat.
* :data:`FACET_ANGLE_DEG` -- no more than this between neighbouring
  facets.  It takes over on large curves (above ~23 mm radius), where
  nozzle-wide facets would multiply triangles for nothing.

A curve gets whichever of the two asks for FEWER facets: a facet that
meets either bound is already below what reaches the print.  At 1 degree
a facet sits no deeper inside the true circle than the 0.005 mm chord
floor :mod:`kiln.step_import` sets for STEP geometry, out to a 262 mm
circle -- and past that floor the slicer merges facets back together on
its own (same measurement: a 45 mm circle meshed with 354 or 720 facets
slices to about 130 moves either way).  Finer buys nothing at the nozzle.

These are OpenSCAD's ``$fs`` and ``$fa``, and that is how the rule
reaches geometry: :data:`SCAD_TRAILER` states it once in every file
Kiln builds a parametric part from (see
:func:`kiln.parametric.render_template_scad`), so a template need never
type a facet count.  OpenSCAD reads the same two values for text
outlines and for the slices of a tapered ``linear_extrude``.  A
deliberate polygon -- a hex nut trap -- still says ``$fn = 6``, and an
explicit ``$fn`` always wins.

Source Kiln is handed -- an agent's own OpenSCAD -- gets the rule the
same way, through :func:`apply_rule`, which the OpenSCAD engine runs on
every file it compiles: it is written INTO the file, never applied out
of sight, so the source a design keeps rebuilds the part it made.  A
file that already says how finely to cut its curves at the top level is
left exactly as written, and so is one with no curve for the rule to
reach.
"""

from __future__ import annotations

import math
import re

#: Widest facet, in mm: one 0.4 mm nozzle (OpenSCAD's ``$fs``).
FACET_WIDTH_MM = 0.4

#: Largest turn between neighbouring facets, in degrees (``$fa``).
FACET_ANGLE_DEG = 1.0


def fragments(radius_mm: float) -> int:
    """Facets in a full circle of this radius under the rule.

    OpenSCAD's own count for ``$fn = 0`` (``get_fragments_from_r``):
    the smaller of the angle and width bounds, never under five.

    :param radius_mm: Circle radius in mm.
    :returns: Facet count for a full circle.
    """
    by_angle = 360.0 / FACET_ANGLE_DEG
    by_width = radius_mm * 2 * math.pi / FACET_WIDTH_MM
    return max(math.ceil(min(by_angle, by_width)), 5)


#: The rule as OpenSCAD source, appended to every parametric part Kiln
#: builds.  It goes at the END: OpenSCAD applies a top-level assignment to
#: the whole file wherever it sits, and a line above a template's
#: parameter block would end that block for
#: :func:`kiln.parametric.parse_openscad_parameters`.  ``curve_fragments``
#: is the count above, for a sweep a template builds by hand (a helical
#: thread's stations) so it steps at the same resolution as the surface
#: it meets.
SCAD_TRAILER = (
    "\n"
    "// Curve resolution (kiln.curve_resolution): facets no wider than "
    f"{FACET_WIDTH_MM:g} mm\n"
    f"// or {FACET_ANGLE_DEG:g} degree apart, whichever needs fewer.\n"
    f"$fa = {FACET_ANGLE_DEG:g};\n"
    f"$fs = {FACET_WIDTH_MM:g};\n"
    "function curve_fragments(r) = $fn > 0 ? max(floor($fn), 3)"
    " : ceil(max(min(360 / $fa, r * 2 * PI / $fs), 5));\n"
)

#: One line of advice for anyone writing OpenSCAD for Kiln -- the prompts
#: Kiln hands a model and the workflow notes agents read at the start.
AGENT_ADVICE = (
    "Leave $fn out for round shapes: Kiln cuts every curve by its size "
    f"(flats no wider than {FACET_WIDTH_MM:g} mm or {FACET_ANGLE_DEG:g} degree "
    "apart). Set $fn only for a deliberate polygon, such as $fn = 6 for a hexagon."
)

# A string or a comment is not code; either may mention "$fn = ...".
_NOT_CODE_RE = re.compile(r'"(?:\\.|[^"\\])*"|/\*.*?\*/|//[^\n]*', re.S)
# What the rule can reach: shapes and operations OpenSCAD cuts with
# $fn/$fa/$fs, code that reads those variables, and library code pulled in
# by include/use, which this scan cannot see into.
_CAN_CURVE_RE = re.compile(
    r"\b(?:circle|cylinder|sphere|rotate_extrude|text|offset|linear_extrude"
    r"|include|use)\b|\$f[nas]\b"
)
_SCAN_RE = re.compile(r"[{}()\[\]]|\$(fn|fa|fs)\s*=(?!=)")


def own_resolution(scad_code: str) -> str | None:
    """The top-level ``$fn`` / ``$fa`` / ``$fs`` assignment a file makes.

    Only an assignment at the top of the file decides resolution for the
    whole part; one inside a module, a call or a ``let`` is scoped to it.

    :param scad_code: OpenSCAD source.
    :returns: The assignment as written (``"$fn = 48"``), or ``None``.
    """
    text = _NOT_CODE_RE.sub(lambda m: '""' if m.group(0)[0] == '"' else "", scad_code)
    depth = 0
    for match in _SCAN_RE.finditer(text):
        token = match.group(0)
        if token in "{([":
            depth += 1
        elif token in "})]":
            depth -= 1
        elif depth == 0:
            end = text.find(";", match.end())
            value = text[match.end():end if end >= 0 else None].strip()
            return f"${match.group(1)} = {value}"
    return None


def apply_rule(scad_code: str) -> str:
    """*scad_code* with the rule written into it, unless it has its own.

    Idempotent.  A file comes back unchanged when it already carries
    :data:`SCAD_TRAILER`, sets its own resolution at the top level, or has
    nothing the rule could change -- no curve, no library code -- so a
    box-only design keeps its source byte for byte.

    :param scad_code: OpenSCAD source.
    :returns: The source OpenSCAD should compile.
    """
    if SCAD_TRAILER in scad_code or own_resolution(scad_code) is not None:
        return scad_code
    code = _NOT_CODE_RE.sub(lambda m: '""' if m.group(0)[0] == '"' else "", scad_code)
    if not _CAN_CURVE_RE.search(code):
        return scad_code
    return scad_code + SCAD_TRAILER
