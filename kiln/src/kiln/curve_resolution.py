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
"""

from __future__ import annotations

import math

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
